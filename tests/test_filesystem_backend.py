from __future__ import annotations

from gate_keeper.backends.filesystem import evaluate_rule, evaluate_ruleset
from gate_keeper.models import Backend, Confidence, Rule, RuleKind, RuleSet, Severity, SourceLocation


def _rule(rule_id: str, kind: RuleKind, **params) -> Rule:
    return Rule(
        id=rule_id,
        title=rule_id,
        source=SourceLocation(path="rules.md", line=1),
        text=rule_id,
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.FILESYSTEM,
        confidence=Confidence.HIGH,
        params=params,
    )


def test_filesystem_backend_pass_fail_and_unavailable(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    (root / "notes.md").write_text("keep calm", encoding="utf-8")
    (root / "checklist.md").write_text("- [x] done\n- [x] done too\n", encoding="utf-8")

    report = evaluate_ruleset(
        RuleSet(
            rules=[
                _rule("exists", RuleKind.FILE_EXISTS, path="README.md"),
                _rule("required", RuleKind.TEXT_REQUIRED, path="notes.md", pattern="keep"),
                _rule("tasks", RuleKind.MARKDOWN_TASKS_COMPLETE, path="checklist.md"),
                _rule("missing text", RuleKind.TEXT_REQUIRED, path="missing.md", pattern="hello"),
                _rule("absent", RuleKind.FILE_ABSENT, path="forbidden.md"),
            ]
        ),
        root,
    )

    statuses = [diagnostic.status.value for diagnostic in report.diagnostics]
    assert statuses == ["pass", "pass", "pass", "unavailable", "pass"]


def test_filesystem_backend_reports_fail_for_missing_required_file(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    diag = evaluate_rule(_rule("exists", RuleKind.FILE_EXISTS, path="README.md"), root)
    assert diag.status.value == "fail"
    assert diag.backend is Backend.FILESYSTEM


def test_filesystem_backend_reports_unsupported_for_unsupported_kind(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    diag = evaluate_rule(_rule("semantic", RuleKind.SEMANTIC_RUBRIC), root)
    assert diag.status.value == "unsupported"
