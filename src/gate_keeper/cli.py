from __future__ import annotations

import argparse
import json
import sys

from gate_keeper import __version__
from gate_keeper.diagnostics import EXIT_OK, EXIT_USAGE


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

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate an artifact against a rule document",
    )
    validate_parser.add_argument("rules", help="path to a rule document")
    validate_parser.add_argument("--target", required=True, help="artifact or PR to validate")
    validate_parser.add_argument(
        "--backend",
        choices=["auto", "filesystem", "github", "llm"],
        default="auto",
        help="validation backend to use",
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

    ruleset = parser.parse(str(path), content)
    ruleset = classifier.classify(ruleset)

    output = {
        "document": {
            "path": str(path),
            "rules_count": len(ruleset.rules),
        },
        "rules": [rule.to_dict() for rule in ruleset.rules],
    }
    print(json.dumps(output, indent=2))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "compile":
        return _cmd_compile(args)

    parser.error(f"{args.command!r} is planned but not implemented in the scaffold")
    return 2
