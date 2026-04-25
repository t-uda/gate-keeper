from __future__ import annotations

import argparse

from gate_keeper import __version__


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    parser.error(f"{args.command!r} is planned but not implemented in the scaffold")
    return 2
