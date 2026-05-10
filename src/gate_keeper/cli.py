from __future__ import annotations

import argparse
import glob as _glob
import json
import sys
from pathlib import Path

from gate_keeper import __version__
from gate_keeper.diagnostics import EXIT_OK, EXIT_USAGE
from gate_keeper.models import Backend, RuleSet, TargetKind

# Backend choices exposed by the registry (always includes auto).
_BACKEND_CHOICES = ["auto", "filesystem", "github", "llm-rubric", "external"]

# ``--artifact-kind`` accepts every TargetKind value except ``unspecified``
# (#178). Passing ``unspecified`` would be equivalent to omitting the flag,
# so we reject it explicitly to make the CLI behaviour unambiguous — a user
# who genuinely wants the legacy prompt-only behaviour omits the flag.
_ARTIFACT_KIND_CHOICES = sorted(member.value for member in TargetKind if member is not TargetKind.UNSPECIFIED)


class _IncludeError(Exception):
    """Raised when --include glob expansion or rule-id merge fails.

    Carries a pre-formatted human-readable message that the CLI prints to
    stderr. Always maps to ``EXIT_USAGE`` (exit code 2) per the issue #145
    spec.
    """


def _expand_include_globs(patterns: list[str]) -> list[Path]:
    """Expand --include glob patterns to a deterministic, sorted list of paths.

    Each pattern is resolved with ``glob.glob`` (relative to the current
    working directory). The combined match list is sorted lexicographically by
    string path so iteration order is stable across platforms (the spec
    guarantees a deterministic, documented include-expansion order).

    Each pattern must match at least one path; unmatched patterns raise
    ``_IncludeError`` per the empty-include policy in issue #145.
    """
    # Dedup on the resolved (canonical) path so different glob spellings of
    # the same physical file (e.g. ``a.md`` vs ``./a.md``, or distinct
    # patterns whose hits overlap) are merged into a single include. Without
    # this, the same file would be parsed twice and ``_check_duplicate_ids``
    # would surface a false ``EXIT_USAGE`` for an otherwise-valid bundle.
    seen_canonical: set[Path] = set()
    matched: list[str] = []
    for pattern in patterns:
        # ``glob`` expands shell-style globs; recursive ``**`` requires
        # ``recursive=True``. Patterns without metacharacters resolve to a
        # single path (or zero, which we report as an unmatched glob below).
        hits = _glob.glob(pattern, recursive=True)
        if not hits:
            raise _IncludeError(f"--include: no files matched pattern {pattern!r}")
        for hit in hits:
            # ``Path.resolve()`` can raise ``RuntimeError`` on symlink loops
            # (e.g. ``a.md -> b.md -> a.md``) and ``OSError`` on broken paths.
            # Translate either into ``_IncludeError`` so the CLI surfaces a
            # ``EXIT_USAGE`` (2) instead of an uncaught traceback — the
            # ``_cmd_compile`` / ``_cmd_validate`` wrappers only catch
            # ``_IncludeError``.
            try:
                canonical = Path(hit).resolve()
            except (OSError, RuntimeError) as exc:
                raise _IncludeError(f"--include: cannot resolve path {hit!r}: {exc}") from exc
            if canonical not in seen_canonical:
                seen_canonical.add(canonical)
                matched.append(hit)
    # Lexicographic sort on the string path keeps order deterministic across
    # platforms and across multiple --include flags. The retained spelling
    # for each physical file is the first one encountered, so per-rule
    # ``source.path`` reflects how the user wrote the include.
    matched.sort()
    return [Path(p) for p in matched]


def _read_ruleset_from_paths(paths: list[Path]) -> RuleSet:
    """Parse each Markdown document into a Rule list, then merge into one RuleSet.

    Each document is parsed with its own source path so per-rule
    ``SourceLocation`` entries point back to the originating file. Rule
    objects from every document are concatenated in path-order; classification
    happens once on the merged set.
    """
    from gate_keeper import classifier, parser

    merged_rules = []
    for path in paths:
        content = _read_text(path)
        # ``parser.parse`` is a pure parse step — it never makes network calls
        # and preserves source.path / source.line for each candidate rule.
        ruleset = parser.parse(str(path), content)
        merged_rules.extend(ruleset.rules)

    # Classify once over the merged list; classification is per-rule so
    # ordering is preserved and no document boundary matters here.
    classified = classifier.classify(RuleSet(rules=merged_rules))

    # Detect duplicate rule ids before validation/output. The default rule-id
    # scheme is ``rule-<stem>-L<line>`` so two files with the same stem at the
    # same line collide; per #145 we must fail clearly with both source
    # locations rather than silently rename.
    _check_duplicate_ids(classified)
    return classified


def _read_text(path: Path) -> str:
    """Read *path* as UTF-8 and translate IO errors to ``_IncludeError``."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _IncludeError(f"{path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise _IncludeError(f"{path}: not valid UTF-8 ({exc.reason})") from exc


def _check_duplicate_ids(ruleset: RuleSet) -> None:
    """Raise ``_IncludeError`` if two rules share the same id.

    The error message lists the duplicated id and the source path/line for
    both occurrences so the user can locate and rename either rule.
    """
    first_seen: dict[str, tuple[str, int]] = {}
    for rule in ruleset.rules:
        loc = (rule.source.path, rule.source.line)
        if rule.id in first_seen:
            prev_path, prev_line = first_seen[rule.id]
            raise _IncludeError(
                f"duplicate rule id {rule.id!r}: "
                f"first at {prev_path}:{prev_line}, "
                f"second at {loc[0]}:{loc[1]}"
            )
        first_seen[rule.id] = loc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate-keeper",
        description="Compile natural-language rules into verifiable checks.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    compile_parser = subparsers.add_parser(
        "compile",
        help="extract rules from a document into the rule IR",
    )
    # ``document`` stays a single positional path for the existing
    # single-document form. Composition uses ``--include`` instead, which
    # avoids ambiguity with multiple positional arguments (per issue #145
    # CLI-shape decision).
    compile_parser.add_argument(
        "document",
        nargs="?",
        default=None,
        help="path to a rule document (omit when using --include)",
    )
    compile_parser.add_argument(
        "--include",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "include Markdown rule documents matching GLOB; may be repeated. "
            "Globs expand in lexicographic path order; the merged result is "
            "one RuleSet."
        ),
    )
    compile_parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="output format (default: json)",
    )

    explain_parser = subparsers.add_parser(
        "explain",
        help="show how each rule in a document maps to a backend",
    )
    explain_parser.add_argument("document", help="path to a rule document")
    explain_parser.add_argument(
        "--format",
        choices=["text"],
        default="text",
        help="output format (default: text)",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate an artifact against a rule document",
    )
    validate_parser.add_argument(
        "rules",
        nargs="?",
        default=None,
        help=(
            "path to the rule input; interpreted as Markdown by default, or as "
            "Rule IR JSON when --rules-format ir is given. Omit when using --include."
        ),
    )
    validate_parser.add_argument(
        "--include",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "include Markdown rule documents matching GLOB; may be repeated. "
            "Globs expand in lexicographic path order; the merged result is "
            "one RuleSet. IR composition is not supported (use a single "
            "positional path with --rules-format ir for IR input)."
        ),
    )
    validate_parser.add_argument(
        "--target",
        required=True,
        action="append",
        dest="target",
        metavar="TARGET",
        help=(
            "artifact or PR to validate. May be specified multiple times to evaluate "
            "filesystem rules across several files; values may be paths, directories, "
            "or quoted globs. Repeated values are deduplicated and sorted "
            "lexicographically. Non-filesystem backends accept a single value only."
        ),
    )
    validate_parser.add_argument(
        "--rules-format",
        dest="rules_format",
        choices=["markdown", "ir"],
        default="markdown",
        help=(
            "format of the rules argument (default: markdown). 'ir' loads a "
            "precompiled RuleSet JSON file; the strict IR parser is used and the "
            "classifier is bypassed so hand-authored kind/backend_hint/params are "
            "preserved."
        ),
    )
    validate_parser.add_argument(
        "--backend",
        choices=_BACKEND_CHOICES,
        default="auto",
        help="validation backend to use (default: auto)",
    )
    validate_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    validate_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="expand structured rationale (llm-rubric) in text output",
    )
    validate_parser.add_argument(
        "--reproducibility",
        type=int,
        default=1,
        metavar="N",
        help=(
            "evaluate each LLM-rubric rule N times and record an agreement-rate "
            "(reproducibility_score) evidence entry (default: 1; non-LLM backends "
            "ignore this flag)"
        ),
    )
    validate_parser.add_argument(
        "--allow-command-adapter",
        action="store_true",
        default=False,
        help=(
            "enable the project-local 'command' external adapter for this run. "
            "WARNING: this lets the rule document execute arbitrary local commands "
            "(via params.argv). Pass this ONLY for rule documents you fully trust."
        ),
    )
    validate_parser.add_argument(
        "--artifact-kind",
        dest="artifact_kind",
        default=None,
        choices=_ARTIFACT_KIND_CHOICES,
        help=(
            "declare the kind of artifact passed via --target (#178). "
            "When set, llm-rubric rules whose `target_kind` annotation differs "
            "from this value short-circuit to UNSUPPORTED with "
            "`evidence.kind=target_kind_mismatch` BEFORE the model is called. "
            "Rules with `target_kind=unspecified` ignore this flag. Omit the "
            "flag to preserve the prompt-level behaviour."
        ),
    )

    subparsers.add_parser(
        "diagnose",
        help="report LLM-rubric provider credential state (length-only, never the key)",
    )

    bench_parser = subparsers.add_parser(
        "bench",
        help="evaluate a fixture-corpus benchmark against the LLM-rubric backend",
    )
    bench_parser.add_argument(
        "entries_dir",
        help="directory of JSON benchmark entries (e.g. tests/fixtures/semantic/entries/)",
    )
    bench_parser.add_argument(
        "--reproducibility",
        type=int,
        default=1,
        metavar="N",
        help=(
            "evaluate each entry N times and aggregate via majority vote (default: 1; ties break toward fail)"
        ),
    )
    bench_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    bench_parser.add_argument(
        "--baseline",
        default=None,
        metavar="PATH",
        help=(
            "compare the current run against a baseline JSON file "
            "(produced by `bench --format json`); prints regressions/fixes"
        ),
    )

    return parser


def _cmd_compile(args: argparse.Namespace) -> int:
    from gate_keeper import classifier, parser

    # Mutually-exclusive sources: exactly one of ``document`` or ``--include``.
    if args.document is None and not args.include:
        print(
            "error: compile requires either a document path or --include GLOB",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.document is not None and args.include:
        print(
            "error: compile accepts either a document path or --include, not both",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.include:
        # Composition path: expand globs, parse each, merge, classify, then
        # check for duplicate ids before emitting the IR.
        try:
            paths = _expand_include_globs(args.include)
            ruleset = _read_ruleset_from_paths(paths)
        except _IncludeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(json.dumps(ruleset.to_dict(), indent=2))
        return EXIT_OK

    # Single-document positional form (existing behaviour, unchanged).
    # ``args.document`` is non-None here: the input-validation block above
    # rejects (None, no --include) before we reach this branch.
    assert args.document is not None
    path = Path(args.document)

    if not path.exists():
        print(f"error: {args.document}: No such file or directory", file=sys.stderr)
        return EXIT_USAGE

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {args.document}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        print(f"error: {args.document}: not valid UTF-8 ({exc.reason})", file=sys.stderr)
        return EXIT_USAGE

    ruleset = parser.parse(str(path), content)
    ruleset = classifier.classify(ruleset)

    print(json.dumps(ruleset.to_dict(), indent=2))
    return EXIT_OK


def _cmd_explain(args: argparse.Namespace) -> int:
    from gate_keeper import classifier, parser
    from gate_keeper.diagnostics import render_explain_text

    path = Path(args.document)

    if not path.exists():
        print(f"error: {args.document}: No such file or directory", file=sys.stderr)
        return EXIT_USAGE

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {args.document}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        print(f"error: {args.document}: not valid UTF-8 ({exc.reason})", file=sys.stderr)
        return EXIT_USAGE

    ruleset = parser.parse(str(path), content)
    ruleset = classifier.classify(ruleset)

    rendered = render_explain_text(ruleset.rules)
    if rendered:
        print(rendered)
    return EXIT_OK


def _load_markdown_ruleset(path: Path) -> RuleSet | int:
    """Load a Markdown rule document and return a classified RuleSet.

    Returns ``EXIT_USAGE`` (int) on read errors so the caller can propagate
    the exit code without raising. Reads the file, parses it via
    ``gate_keeper.parser.parse``, and runs the classifier — this is the
    historical Markdown path.
    """
    from gate_keeper import classifier, parser

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {path}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        print(f"error: {path}: not valid UTF-8 ({exc.reason})", file=sys.stderr)
        return EXIT_USAGE

    ruleset = parser.parse(str(path), content)
    ruleset = classifier.classify(ruleset)
    return ruleset


def _load_ir_ruleset(path: Path) -> RuleSet | int:
    """Load a precompiled Rule IR JSON file via the strict RuleSet parser.

    The classifier is intentionally **not** invoked: hand-authored ``kind``,
    ``backend_hint``, and ``params`` fields must reach the validator
    unchanged (see issue #144). Errors map to ``EXIT_USAGE`` with a
    diagnostic that names the path and the parse/validation reason.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {path}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        print(f"error: {path}: not valid UTF-8 ({exc.reason})", file=sys.stderr)
        return EXIT_USAGE

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"error: {path}: invalid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        return RuleSet.from_dict(data)
    except ValueError as exc:
        print(f"error: {path}: invalid rule IR ({exc})", file=sys.stderr)
        return EXIT_USAGE


def _ruleset_has_filesystem_rule(ruleset: RuleSet) -> bool:
    """Return True when at least one rule in *ruleset* routes to the filesystem backend.

    Used by ``_cmd_validate`` to decide whether a single ``--target`` value
    that contains glob metacharacters but expanded to zero filesystem matches
    should be reinterpreted as literal text (issue #165).  Filesystem rules
    have a meaningful interpretation of an empty glob — the legacy
    fail-closed UNAVAILABLE outcome — so we must not silently rewrite the
    target for them; only when **no** rule needs a filesystem path is a
    glob-shaped literal unambiguously prose to forward to the backend.
    """
    return any(rule.backend_hint is Backend.FILESYSTEM for rule in ruleset.rules)


def _cmd_validate(args: argparse.Namespace) -> int:
    from gate_keeper import validator
    from gate_keeper.backends import is_registered
    from gate_keeper.diagnostics import compute_exit_code, render_json, render_text
    from gate_keeper.targets import TargetExpansionError, resolve_targets

    # Validate backend choice defensively (argparse choices= should catch most).
    backend = args.backend
    if backend != "auto" and not is_registered(backend):
        print(f"error: unknown backend {backend!r}", file=sys.stderr)
        return EXIT_USAGE

    # Mutually-exclusive sources: exactly one of ``rules`` or ``--include``.
    if args.rules is None and not args.include:
        print(
            "error: validate requires either a rules path or --include GLOB",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.rules is not None and args.include:
        print(
            "error: validate accepts either a rules path or --include, not both",
            file=sys.stderr,
        )
        return EXIT_USAGE

    rules_format = getattr(args, "rules_format", "markdown")
    if args.include:
        # Composition path: expand globs, parse each, merge, classify, then
        # check for duplicate ids before running validation. Markdown only —
        # IR composition is intentionally out of scope (use single positional
        # with --rules-format ir for IR input).
        if rules_format == "ir":
            print(
                "error: --include is incompatible with --rules-format ir (IR composition is not supported)",
                file=sys.stderr,
            )
            return EXIT_USAGE
        try:
            paths = _expand_include_globs(args.include)
            ruleset = _read_ruleset_from_paths(paths)
        except _IncludeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        # Single positional path. ``args.rules`` is non-None here: the
        # input-validation block above rejects (None, no --include) already.
        assert args.rules is not None
        doc_path = Path(args.rules)
        if not doc_path.exists():
            print(f"error: {args.rules}: No such file or directory", file=sys.stderr)
            return EXIT_USAGE
        if rules_format == "ir":
            loaded = _load_ir_ruleset(doc_path)
        else:
            loaded = _load_markdown_ruleset(doc_path)
        if isinstance(loaded, int):
            return loaded
        ruleset = loaded

    # Validate reproducibility argument.
    if args.reproducibility < 1:
        print(
            f"error: --reproducibility must be >= 1, got {args.reproducibility}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Resolve --target values into a single value passed to validator.
    #
    # Compatibility rule (issue #146): when exactly one --target is supplied
    # and it is a plain literal (not a directory, not a glob), forward the raw
    # string. This preserves every pre-#146 behaviour, including GitHub PR
    # references like ``owner/repo#1`` and PR URLs that contain ``?``/``#``
    # (which the GitHub backend's parser tolerates). Multi-target invocations
    # and directory/glob single-targets are resolved into a TargetSpec; the
    # chosen backend then either aggregates (filesystem) or fails closed
    # (github / llm-rubric / external).
    raw_targets: list[str] = list(args.target)
    target: object
    if len(raw_targets) == 1:
        from gate_keeper.backends._target import parse_target
        from gate_keeper.targets import looks_like_glob

        sole = raw_targets[0]
        sole_path = Path(sole)

        # Order matters: a literal directory or an existing literal file
        # always wins over glob detection so a real filename like
        # ``a[b].txt`` does not get mis-expanded as a pattern.
        #
        # The ``is_dir`` / ``is_file`` calls can raise ``OSError`` when the
        # token cannot be a real path on the host filesystem — most commonly
        # ``ENAMETOOLONG`` (errno 36) when a ``/``-segment exceeds NAME_MAX
        # (255 bytes on Linux), and ``EINVAL`` on some kernels for embedded
        # NULs.  Such inputs cannot possibly resolve to a path, so we treat
        # them as literal text and fall through to the non-path branch
        # rather than letting the traceback escape (issue #166).
        try:
            sole_is_dir = sole_path.is_dir()
            sole_is_file = sole_path.is_file()
        except OSError:
            sole_is_dir = False
            sole_is_file = False

        if sole_is_dir:
            try:
                target = resolve_targets(raw_targets)
            except TargetExpansionError as exc:
                print(f"error: --target: {exc}", file=sys.stderr)
                return EXIT_USAGE
        elif sole_is_file:
            target = sole
        elif looks_like_glob(sole):
            # Token has glob metacharacters but the literal path doesn't
            # exist. A GitHub PR URL with a query string (``?diff=split``)
            # falls into this bucket — recognise that case explicitly so
            # the github backend still receives the raw URL it needs.
            pr, _ = parse_target(sole)
            if pr is not None:
                target = sole
            else:
                try:
                    resolved = resolve_targets(raw_targets)
                except TargetExpansionError as exc:
                    print(f"error: --target: {exc}", file=sys.stderr)
                    return EXIT_USAGE
                # Issue #165: a literal ``--target`` containing glob
                # metacharacters (``*``/``?``/``[``) — common in markdown
                # PR-body text such as ``**bold** with [link](x)`` or a
                # GitHub-style checkbox ``- [x] item`` — is mis-routed
                # through ``resolve_targets`` even when the rules in this
                # ruleset have no filesystem backend.  When the expansion
                # produced zero filesystem matches AND no rule routes to
                # the filesystem backend AND the user did not force a
                # filesystem backend via ``--backend filesystem``, the
                # only sensible interpretation is "literal text"; fall
                # through to the raw string so the llm-rubric / github /
                # external backend receives the author-supplied content
                # instead of an empty TargetSpec.
                #
                # The check is intentionally narrow: a filesystem rule
                # with an empty glob still produces ``UNAVAILABLE`` (the
                # legacy fail-closed behaviour exercised by
                # ``test_empty_glob_fails_closed``), a non-filesystem
                # rule with a glob that actually matched files still
                # surfaces as ``multi_target_unsupported`` (the user
                # explicitly asked for multiple files; #74's job to lift
                # that restriction), and an explicit
                # ``--backend filesystem`` override still expects
                # filesystem-style target resolution regardless of the
                # ruleset's backend hints.
                forced_filesystem = backend == "filesystem"
                if not resolved.paths and not _ruleset_has_filesystem_rule(ruleset) and not forced_filesystem:
                    target = sole
                else:
                    target = resolved
        else:
            target = sole
    else:
        try:
            target = resolve_targets(raw_targets)
        except TargetExpansionError as exc:
            print(f"error: --target: {exc}", file=sys.stderr)
            return EXIT_USAGE

    # Toggle the project-local command adapter for this process only. The flag
    # is intentionally not propagated through the validator API — the adapter
    # owns its own enable state to keep the trust boundary obvious.
    from gate_keeper.adapters import command as command_adapter

    previous_enabled = command_adapter.is_enabled()
    command_adapter.set_enabled(bool(args.allow_command_adapter))
    # Resolve --artifact-kind into a TargetKind enum value once, here, so the
    # validator API stays typed (it accepts ``TargetKind | None``). argparse
    # has already validated the raw value against ``_ARTIFACT_KIND_CHOICES``,
    # so ``TargetKind(...)`` cannot raise — but if a future refactor widens
    # the choices we'd want the error to surface cleanly rather than crash.
    artifact_kind: TargetKind | None
    if getattr(args, "artifact_kind", None) is None:
        artifact_kind = None
    else:
        artifact_kind = TargetKind(args.artifact_kind)
    try:
        # Run validation.
        report = validator.validate(
            ruleset,
            target,
            backend=backend,
            reproducibility=args.reproducibility,
            artifact_kind=artifact_kind,
        )
    finally:
        command_adapter.set_enabled(previous_enabled)

    # Render output.
    if args.format == "json":
        print(render_json(report.diagnostics))
    else:
        rendered = render_text(report.diagnostics, verbose=args.verbose)
        if rendered:
            print(rendered)

    return compute_exit_code(report.diagnostics)


def _cmd_diagnose(args: argparse.Namespace) -> int:
    """Report LLM-rubric provider credential state.

    Prints dotenv path, whether the file exists, the configured provider
    (``GATE_KEEPER_LLM_PROVIDER``), the API-key character count (length
    only — never the key value), and a derived ``provider configured``
    line.

    The implementation reads the dotenv exactly once via
    ``llm_rubric._load_env_file``; the ``configured`` line is derived
    from that single snapshot to keep the four output lines internally
    consistent (a concurrent edit to the dotenv between two reads could
    otherwise produce a contradictory report). ``os.environ`` is
    intentionally not consulted, matching the backend policy documented
    in ``src/gate_keeper/backends/llm_rubric.py``.
    """
    del args  # diagnose takes no arguments

    from gate_keeper.backends import llm_rubric

    path = llm_rubric.DOTENV_PATH
    env = llm_rubric._load_env_file(path)
    provider = env.get("GATE_KEEPER_LLM_PROVIDER")

    print(f"dotenv path:        {path}")
    print(f"dotenv exists:      {'yes' if path.exists() else 'no'}")
    print(f"GATE_KEEPER_LLM_PROVIDER: {provider if provider is not None else '<unset>'}")

    # Distinguish three provider states for the api-key line:
    #   - unset or blank → report ``<unset>`` (not "unsupported")
    #   - supported (anthropic / openai) → report the matching key var,
    #     length-only
    #   - any other non-empty value → unsupported provider, no key var
    #     to consult
    # ``configured`` is derived from the same single snapshot rather
    # than calling ``_is_configured()`` again, so the four output lines
    # cannot drift if the dotenv is rewritten mid-call.
    if not provider:
        print("api key:            <unset>")
        configured = False
    elif provider == "anthropic":
        key_value = env.get("ANTHROPIC_API_KEY")
        if key_value:
            print(f"ANTHROPIC_API_KEY: <{len(key_value)} chars>")
        else:
            print("ANTHROPIC_API_KEY: <unset>")
        configured = bool(key_value)
        model = llm_rubric._resolve_model("anthropic", env)
        override = env.get("GATE_KEEPER_ANTHROPIC_MODEL", "").strip()
        print(f"model:              {model} ({'override' if override else 'default'})")
    elif provider == "openai":
        key_value = env.get("OPENAI_API_KEY")
        if key_value:
            print(f"OPENAI_API_KEY: <{len(key_value)} chars>")
        else:
            print("OPENAI_API_KEY: <unset>")
        configured = bool(key_value)
        model = llm_rubric._resolve_model("openai", env)
        override = env.get("GATE_KEEPER_OPENAI_MODEL", "").strip()
        print(f"model:              {model} ({'override' if override else 'default'})")
    else:
        print("api key:            <unsupported provider; no key reported>")
        configured = False

    print(f"provider configured: {'yes' if configured else 'no'}")

    return EXIT_OK


def _cmd_bench(args: argparse.Namespace) -> int:
    """Evaluate a fixture-corpus benchmark against the LLM-rubric backend.

    Loads JSON entries under *entries_dir*, evaluates each one ``--reproducibility``
    times via the LLM-rubric backend, and prints aggregate accuracy /
    reproducibility / token metrics plus a per-entry breakdown. Output format is
    ``text`` (default) or ``json``.

    Exit codes
    ----------
    - ``0`` (EXIT_OK) — bench ran end-to-end (entries loaded, evaluator returned
      a result). The bench surface is observational; a low accuracy is *not* a
      CLI-level failure (this lets dogfooding pipelines record regressions
      without turning the run red).
    - ``2`` (EXIT_USAGE) — bad arguments or unreadable entries directory.

    The ``--baseline`` option (when provided) compares the current run against a
    stored baseline JSON document. The framework lands here in #130; the
    canonical baseline file is added by the follow-up issue (#132).
    """
    from gate_keeper import bench as _bench

    entries_dir = Path(args.entries_dir)
    if not entries_dir.is_dir():
        print(
            f"error: {args.entries_dir}: not a directory or does not exist",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.reproducibility < 1:
        print(
            f"error: --reproducibility must be >= 1, got {args.reproducibility}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        result = _bench.run_bench(entries_dir, reproducibility=args.reproducibility)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Baseline diff (advisory, does not change the exit code).
    delta = None
    if args.baseline is not None:
        baseline_path = Path(args.baseline)
        try:
            delta = _bench.diff_baseline(result, baseline_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: --baseline: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if args.format == "json":
        # Emit a single JSON object so machine consumers (json.loads, jq, etc.)
        # can parse the output with one call regardless of whether --baseline is
        # present.  The baseline delta is embedded under "baseline_delta" when
        # requested; the key is absent otherwise.
        payload = result.to_dict()
        if delta is not None:
            payload["baseline_delta"] = delta.to_dict()
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(_bench.render_text(result))
        if delta is not None:
            print(_bench.render_baseline_delta_text(delta))

    return EXIT_OK


def _register_default_adapters() -> None:
    """Register adapters that ship with gate-keeper.

    Registration happens lazily at CLI entry rather than at module-import time
    of ``gate_keeper.adapters`` so test isolation in ``tests/test_external_backend.py``
    is preserved (those tests snapshot/restore an empty registry per test).

    The ``command`` adapter is registered here, but is gated behind the
    process-local enable flag in ``gate_keeper.adapters.command``. Without
    ``--allow-command-adapter`` it returns ``unavailable`` and never spawns
    a subprocess; see ``docs/cli-reference.md`` for the trust model.
    """
    from gate_keeper.adapters.command import CommandAdapter
    from gate_keeper.adapters.textlint import TextlintAdapter
    from gate_keeper.backends import external

    for adapter in (TextlintAdapter(), CommandAdapter()):
        try:
            external.register(adapter)
        except ValueError:
            # Already registered (e.g. main called twice in the same process).
            pass


def main(argv: list[str] | None = None) -> int:
    _register_default_adapters()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "compile":
        return _cmd_compile(args)

    if args.command == "explain":
        return _cmd_explain(args)

    if args.command == "validate":
        return _cmd_validate(args)

    if args.command == "diagnose":
        return _cmd_diagnose(args)

    if args.command == "bench":
        return _cmd_bench(args)

    parser.error(f"{args.command!r} is planned but not implemented in the scaffold")
    return 2
