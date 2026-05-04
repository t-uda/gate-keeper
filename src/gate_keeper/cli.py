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
        rendered = render_text(report.diagnostics)
        if rendered:
            print(rendered)

    return compute_exit_code(report.diagnostics)


def main(argv: list[str] | None = None) -> int:
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

    parser.error(f"{args.command!r} is planned but not implemented in the scaffold")
    return 2
