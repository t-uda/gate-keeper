from __future__ import annotations

import argparse
import json
import sys

from gate_keeper import __version__
from gate_keeper.diagnostics import EXIT_OK, EXIT_USAGE

# Backend choices exposed by the registry (always includes auto).
_BACKEND_CHOICES = ["auto", "filesystem", "github", "llm-rubric", "external"]


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
    compile_parser.add_argument("document", help="path to a rule document")
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
    validate_parser.add_argument("rules", help="path to a rule document")
    validate_parser.add_argument("--target", required=True, help="artifact or PR to validate")
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
    from pathlib import Path

    from gate_keeper import classifier, parser

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
    from pathlib import Path

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


def _cmd_validate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from gate_keeper import classifier, parser, validator
    from gate_keeper.backends import is_registered
    from gate_keeper.diagnostics import compute_exit_code, render_json, render_text

    # Validate backend choice defensively (argparse choices= should catch most).
    backend = args.backend
    if backend != "auto" and not is_registered(backend):
        print(f"error: unknown backend {backend!r}", file=sys.stderr)
        return EXIT_USAGE

    # Read and compile the rule document.
    doc_path = Path(args.rules)
    if not doc_path.exists():
        print(f"error: {args.rules}: No such file or directory", file=sys.stderr)
        return EXIT_USAGE

    try:
        content = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {args.rules}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except UnicodeDecodeError as exc:
        print(f"error: {args.rules}: not valid UTF-8 ({exc.reason})", file=sys.stderr)
        return EXIT_USAGE

    ruleset = parser.parse(str(doc_path), content)
    ruleset = classifier.classify(ruleset)

    # Validate reproducibility argument.
    if args.reproducibility < 1:
        print(
            f"error: --reproducibility must be >= 1, got {args.reproducibility}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Run validation.
    report = validator.validate(ruleset, args.target, backend=backend, reproducibility=args.reproducibility)

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
    elif provider == "openai":
        key_value = env.get("OPENAI_API_KEY")
        if key_value:
            print(f"OPENAI_API_KEY: <{len(key_value)} chars>")
        else:
            print("OPENAI_API_KEY: <unset>")
        configured = bool(key_value)
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
    from pathlib import Path

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

    if args.format == "json":
        print(_bench.render_json(result))
    else:
        print(_bench.render_text(result))

    # Baseline diff (advisory, does not change the exit code).
    if args.baseline is not None:
        baseline_path = Path(args.baseline)
        try:
            delta = _bench.diff_baseline(result, baseline_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: --baseline: {exc}", file=sys.stderr)
            return EXIT_USAGE
        # Print delta after the main result so JSON consumers can split on the
        # second top-level object if they wish; for text, the human reads them
        # in order.
        if args.format == "json":
            print(json.dumps(delta.to_dict(), sort_keys=True, indent=2))
        else:
            print(_bench.render_baseline_delta_text(delta))

    return EXIT_OK


def _register_default_adapters() -> None:
    """Register adapters that ship with gate-keeper.

    Registration happens lazily at CLI entry rather than at module-import time
    of ``gate_keeper.adapters`` so test isolation in ``tests/test_external_backend.py``
    is preserved (those tests snapshot/restore an empty registry per test).
    """
    from gate_keeper.adapters.textlint import TextlintAdapter
    from gate_keeper.backends import external

    try:
        external.register(TextlintAdapter())
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
