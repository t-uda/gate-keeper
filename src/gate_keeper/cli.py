from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from gate_keeper import __version__
from gate_keeper.backends.filesystem import evaluate_ruleset
from gate_keeper.classifier import compile_document
from gate_keeper.diagnostics import exit_code_for_report, render_diagnostic_json, render_diagnostic_text
from gate_keeper.models import DiagnosticReport, RuleSet
from gate_keeper.parser import extract_candidates


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
        choices=["json", "text"],
        default="json",
        help="output format",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate an artifact against a rule document",
    )
    validate_parser.add_argument("rules", help="path to a rule document")
    validate_parser.add_argument("--target", required=True, help="artifact or PR to validate")
    validate_parser.add_argument(
        "--backend",
        choices=["auto", "filesystem", "github", "llm-rubric"],
        default="auto",
        help="validation backend to use",
    )
    validate_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="output format",
    )

    explain_parser = subparsers.add_parser(
        "explain",
        help="show how rules map to backends",
    )
    explain_parser.add_argument("document", help="path to a rule document")
    explain_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="output format",
    )

    return parser


def _read_document(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    except BrokenPipeError:  # pragma: no cover - shell pipeline behavior
        pass


def _compile_command(document: str, output_format: str) -> int:
    try:
        content = _read_document(document)
    except OSError as exc:
        sys.stderr.write(f"gate-keeper compile: unable to read {document!r}: {exc}\n")
        return 2
    ruleset, explanations = compile_document(document, content)
    if output_format == "json":
        _emit(json.dumps(ruleset.to_dict(), indent=2, sort_keys=True))
    else:
        lines = [f"{Path(document).name}: {len(ruleset.rules)} rule(s)"]
        for rule, explanation in zip(ruleset.rules, explanations, strict=True):
            lines.append(
                f"- {rule.id} [{rule.backend_hint.value}/{rule.kind.value}/{rule.confidence.value}] {rule.title}"
            )
            lines.append(f"  source: {rule.source.path}:{rule.source.line}")
            if rule.source.heading:
                lines.append(f"  heading: {rule.source.heading}")
            lines.append(f"  explanation: {explanation}")
        _emit("\n".join(lines))
    return 0


def _validate_command(document: str, target: str, backend: str, output_format: str) -> int:
    try:
        content = _read_document(document)
    except OSError as exc:
        sys.stderr.write(f"gate-keeper validate: unable to read {document!r}: {exc}\n")
        return 2

    ruleset, _ = compile_document(document, content)

    if backend == "filesystem" or backend == "auto":
        report = evaluate_ruleset(ruleset, target)
    else:
        sys.stderr.write(
            f"gate-keeper validate: backend {backend!r} is not implemented in this MVP\n"
        )
        return 1

    if output_format == "json":
        _emit(render_diagnostic_json(report))
    else:
        _emit(render_diagnostic_text(report) or "")
    return exit_code_for_report(report)


def _explain_command(document: str, output_format: str) -> int:
    try:
        content = _read_document(document)
    except OSError as exc:
        sys.stderr.write(f"gate-keeper explain: unable to read {document!r}: {exc}\n")
        return 2
    parsed = extract_candidates(document, content)
    ruleset, explanations = compile_document(document, content)
    if output_format == "json":
        payload = {
            "document": document,
            "candidate_count": len(parsed.candidates),
            "rules": [
                {
                    **rule.to_dict(),
                    "explanation": explanation,
                }
                for rule, explanation in zip(ruleset.rules, explanations, strict=True)
            ],
        }
        _emit(json.dumps(payload, indent=2, sort_keys=True))
    else:
        lines = [f"{document}: {len(ruleset.rules)} rule(s)"]
        for rule, explanation in zip(ruleset.rules, explanations, strict=True):
            lines.append(
                f"- {rule.id}: {rule.kind.value} -> {rule.backend_hint.value} ({rule.confidence.value})"
            )
            lines.append(f"  {explanation}")
        _emit("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "compile":
        return _compile_command(args.document, args.format)
    if args.command == "validate":
        return _validate_command(args.rules, args.target, args.backend, args.format)
    if args.command == "explain":
        return _explain_command(args.document, args.format)

    parser.error(f"unknown command: {args.command}")
    return 2
