"""Rule classifier: assigns kind, backend_hint, and confidence to parsed rules.

Deterministic pattern-matching only — no LLM calls.
Classification order: high-confidence GitHub first (explicit keywords), then
high-confidence filesystem (explicit predicates), then weaker heuristics, and
finally semantic fallback. Filesystem checks run before the PR-heading catch-all
so that file/text rules embedded in PR documents still route to the right backend.
"""
from __future__ import annotations

import re
from dataclasses import replace

from gate_keeper.models import Backend, Confidence, Rule, RuleKind, RuleSet

# ---------------------------------------------------------------------------
# GitHub patterns — ordered most-specific first within each tier
# ---------------------------------------------------------------------------

# Draft: require PR/pull-request context or explicit "not a draft" phrasing to
# avoid matching "The draft configuration file must exist" as a GitHub rule.
_DRAFT_RE = re.compile(
    r"\b(?:pr|pull\s+request)\b.{0,60}\bdraft\b"
    r"|\bdraft\b.{0,60}\b(?:pr|pull\s+request)\b"
    r"|\bnot\s+(?:be\s+)?(?:a\s+)?draft\b"
    r"|\bin\s+draft\s+(?:state|mode)\b",
    re.IGNORECASE,
)

_PR_OPEN_RE = re.compile(
    r"\b(?:pr|pull\s+request)\b.{0,50}\bopen\b"
    r"|\bnot\s+(?:be\s+)?(?:closed|merged)\b"
    r"|\bpr\s+(?:state|status)\b",
    re.IGNORECASE,
)

_CHECKS_HIGH_RE = re.compile(
    r"\b(?:ci\s+checks?|status\s+checks?|checks?\s+(?:must\s+)?(?:pass|succeed|success)"
    r"|build\s+(?:must\s+)?(?:pass|succeed|success))\b",
    re.IGNORECASE,
)
_CHECKS_MEDIUM_RE = re.compile(r"\bchecks?\b", re.IGNORECASE)

_THREADS_HIGH_RE = re.compile(
    r"\b(?:review\s+threads?|unresolved\s+threads?|threads?\s+(?:must\s+)?(?:be\s+)?resolved)\b",
    re.IGNORECASE,
)

_APPROVAL_HIGH_RE = re.compile(
    r"\b(?:non.?author\s+(?:approval|reviewers?)|independent\s+(?:review|approval))\b",
    re.IGNORECASE,
)
_APPROVAL_MEDIUM_RE = re.compile(r"\b(?:approval|approved|reviewer|approves?)\b", re.IGNORECASE)

_LABELS_HIGH_RE = re.compile(
    r"\b(?:blocking\s+labels?|labels?\s+absent|no\s+(?:blocking\s+)?labels?|do.not.merge|needs.decision)\b",
    re.IGNORECASE,
)
_LABELS_MEDIUM_RE = re.compile(r"\blabels?\b", re.IGNORECASE)

_PR_TASK_HIGH_RE = re.compile(
    r"\b(?:pr|pull\s+request)\b.{0,40}\b(?:tasks?|checkboxes?|checklists?)\b",
    re.IGNORECASE,
)

# Heading that signals this item belongs to a PR/GitHub section
_PR_HEADING_RE = re.compile(
    r"\b(?:pr|pull\s+request|merge\s+gate|merge\s+check|github)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Filesystem patterns
# ---------------------------------------------------------------------------

# Match the predicate phrase directly rather than requiring the literal word
# "file", so that "README.md must exist" is caught alongside "CHANGELOG file
# must exist". GitHub patterns run first, so PR-state predicates are already
# handled when we reach these checks.
_FILE_ABSENT_HIGH_RE = re.compile(
    r"\b(?:must\s+not\s+exist|should\s+not\s+exist"
    r"|must\s+be\s+(?:absent|removed|deleted)|is\s+absent)\b",
    re.IGNORECASE,
)

_FILE_EXISTS_HIGH_RE = re.compile(
    r"\b(?:must\s+exist|should\s+exist|must\s+be\s+present|is\s+(?:required|present))\b",
    re.IGNORECASE,
)

_TEXT_FORBIDDEN_HIGH_RE = re.compile(
    r"\b(?:must\s+not|should\s+not|never)\s+(?:contain|include)\b"
    r"|\bforbidden\s+text\b|\btext\s+forbidden\b",
    re.IGNORECASE,
)

_TEXT_REQUIRED_HIGH_RE = re.compile(
    r"\b(?:must|should)\s+(?:contain|include)\b"
    r"|\b(?:required|mandatory)\s+text\b|\btext\s+(?:required|mandatory)\b",
    re.IGNORECASE,
)

_PATH_MEDIUM_RE = re.compile(r"\b(?:path|glob|filename|directory|folder)\b", re.IGNORECASE)

# Mirrors the parser's normative-keyword set; absence of a normative keyword
# means the rule came from a bare task-checkbox line (bullets and paragraphs
# require a normative keyword to be extracted).
_NORMATIVE_RE = re.compile(
    r"\b(?:must\s+not|should\s+not|must|should|never|required|forbidden"
    r"|fail|block|ensure|require)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make(
    rule: Rule,
    kind: RuleKind,
    backend: Backend,
    confidence: Confidence,
    explanation: str,
) -> Rule:
    params = {**rule.params, "classifier_explanation": explanation}
    return replace(rule, kind=kind, backend_hint=backend, confidence=confidence, params=params)


def _classify_rule(rule: Rule) -> Rule:
    text = rule.text
    heading = rule.source.heading or ""

    # --- High-confidence GitHub ---
    if _DRAFT_RE.search(text):
        return _make(rule, RuleKind.GITHUB_NOT_DRAFT, Backend.GITHUB, Confidence.HIGH,
                     "explicit PR draft-state keyword")

    if _PR_OPEN_RE.search(text):
        return _make(rule, RuleKind.GITHUB_PR_OPEN, Backend.GITHUB, Confidence.HIGH,
                     "explicit PR open-state reference")

    if _CHECKS_HIGH_RE.search(text):
        return _make(rule, RuleKind.GITHUB_CHECKS_SUCCESS, Backend.GITHUB, Confidence.HIGH,
                     "explicit CI/status-check keyword")

    if _THREADS_HIGH_RE.search(text):
        return _make(rule, RuleKind.GITHUB_THREADS_RESOLVED, Backend.GITHUB, Confidence.HIGH,
                     "explicit review-thread or resolved-thread keyword")

    if _APPROVAL_HIGH_RE.search(text):
        return _make(rule, RuleKind.GITHUB_NON_AUTHOR_APPROVAL, Backend.GITHUB, Confidence.HIGH,
                     "explicit non-author-approval or independent-review keyword")

    if _LABELS_HIGH_RE.search(text):
        return _make(rule, RuleKind.GITHUB_LABELS_ABSENT, Backend.GITHUB, Confidence.HIGH,
                     "explicit blocking-label or do-not-merge keyword")

    if _PR_TASK_HIGH_RE.search(text):
        return _make(rule, RuleKind.GITHUB_TASKS_COMPLETE, Backend.GITHUB, Confidence.HIGH,
                     "explicit PR task/checklist reference")

    # --- Medium-confidence GitHub (keyword signals without full explicit phrasing) ---
    if _APPROVAL_MEDIUM_RE.search(text):
        return _make(rule, RuleKind.GITHUB_NON_AUTHOR_APPROVAL, Backend.GITHUB, Confidence.MEDIUM,
                     "approval/reviewer keyword without explicit non-author phrasing")

    if _LABELS_MEDIUM_RE.search(text):
        return _make(rule, RuleKind.GITHUB_LABELS_ABSENT, Backend.GITHUB, Confidence.MEDIUM,
                     "label keyword without explicit blocking context")

    if _CHECKS_MEDIUM_RE.search(text) and _PR_HEADING_RE.search(heading):
        return _make(rule, RuleKind.GITHUB_CHECKS_SUCCESS, Backend.GITHUB, Confidence.MEDIUM,
                     "check keyword under a PR/merge-gate heading")

    # --- High-confidence filesystem (checked before the PR-heading heuristic so
    #     that explicit file/text rules under a PR section route to filesystem) ---
    if _FILE_ABSENT_HIGH_RE.search(text):
        return _make(rule, RuleKind.FILE_ABSENT, Backend.FILESYSTEM, Confidence.HIGH,
                     "explicit must-not-exist or is-absent predicate")

    if _FILE_EXISTS_HIGH_RE.search(text):
        return _make(rule, RuleKind.FILE_EXISTS, Backend.FILESYSTEM, Confidence.HIGH,
                     "explicit must-exist or must-be-present predicate")

    if _TEXT_FORBIDDEN_HIGH_RE.search(text):
        return _make(rule, RuleKind.TEXT_FORBIDDEN, Backend.FILESYSTEM, Confidence.HIGH,
                     "explicit must-not-contain or forbidden-text wording")

    if _TEXT_REQUIRED_HIGH_RE.search(text):
        return _make(rule, RuleKind.TEXT_REQUIRED, Backend.FILESYSTEM, Confidence.HIGH,
                     "explicit must-contain or text-required wording")

    # --- Medium-confidence filesystem ---
    if _PATH_MEDIUM_RE.search(text):
        return _make(rule, RuleKind.PATH_MATCHES, Backend.FILESYSTEM, Confidence.MEDIUM,
                     "path/directory keyword without explicit existence predicate")

    # --- PR/GitHub heading heuristic (no stronger signal matched above) ---
    if _PR_HEADING_RE.search(heading):
        return _make(rule, RuleKind.GITHUB_TASKS_COMPLETE, Backend.GITHUB, Confidence.MEDIUM,
                     "item under a PR/merge-gate heading with no stronger signal")

    # --- Bare task-checkbox heuristic ---
    # Rules without a normative keyword can only have been emitted by the parser
    # from a task-checkbox line (bullets and paragraphs require normative keywords).
    if not _NORMATIVE_RE.search(text):
        return _make(rule, RuleKind.MARKDOWN_TASKS_COMPLETE, Backend.FILESYSTEM, Confidence.MEDIUM,
                     "no normative keyword — likely a bare task-checkbox item")

    # --- Fallback: ambiguous semantic judgement ---
    return rule  # retains semantic_rubric / llm-rubric / low confidence from parser


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(ruleset: RuleSet) -> RuleSet:
    """Classify all rules in *ruleset*, returning a new RuleSet with updated kind/backend/confidence."""
    return RuleSet(rules=[_classify_rule(rule) for rule in ruleset.rules])


def classify_rule(rule: Rule) -> Rule:
    """Classify a single rule, returning a new Rule with updated kind/backend/confidence."""
    return _classify_rule(rule)
