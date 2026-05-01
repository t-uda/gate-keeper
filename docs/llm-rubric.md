# LLM Rubric Backend

## Purpose

The `llm-rubric` backend handles rules that cannot be verified deterministically.
When the classifier cannot match a rule to a filesystem or GitHub pattern it falls
back to `kind=semantic_rubric` with `backend_hint=llm-rubric` and `confidence=low`.
These are rules that require reading and reasoning about content — e.g.
"documentation must be clear" — rather than checking a concrete predicate.

## Advisory status

LLM-evaluated rules are **advisory evidence, not authoritative gates**.

The classifier only routes a rule here when deterministic evidence is absent.
Even with a configured provider, the result reflects a probabilistic judgement on
natural-language text, not a machine-verifiable fact.  Deterministic backends
(`filesystem`, `github`) remain the authoritative control plane for merge gates.

Do not promote a `semantic_rubric` rule to `severity=error` without carefully
considering that:

- the result depends on prompt quality, model version, and token context;
- two runs of the same rule may produce different results;
- a PASS from the LLM does not guarantee the claim is true.

Use `severity=advisory` or `severity=warning` for semantic rules.

## Current MVP behavior

No LLM provider is configured in the MVP. Every `semantic_rubric` rule returns:

```
status:      unavailable
backend:     llm-rubric
evidence[0]: provider_unconfigured { rule_text, rule_kind, target }
remediation: Configure an LLM provider to enable semantic rule evaluation.
```

`unavailable` is a non-passing status; the CLI exits `1`.  This is intentional
fail-closed behavior: an unconfigured evaluator must not silently pass rules.

## Rubric input/output shape

When a provider is added, `_build_rubric_input()` in `src/gate_keeper/backends/llm_rubric.py`
defines the context passed to the model:

```json
{
  "rule_text": "<verbatim rule text from the document>",
  "rule_kind": "semantic_rubric",
  "target":    "<filesystem path or PR reference>"
}
```

The expected provider response is a `Diagnostic` with:

- `status`: `pass` or `fail` (never `unavailable` when the provider responds).
- `message`: a single-line summary of the evaluation.
- `evidence`: at minimum one entry with `kind="llm_judgment"` and the model's
  explanation in `data`.
- `remediation` (optional): suggested action when `status=fail`.

## Extension point

To add a provider:

1. Implement `_is_configured() -> bool` in `src/gate_keeper/backends/llm_rubric.py` to detect
   credentials (e.g. `GATE_KEEPER_LLM_PROVIDER`, `OPENAI_API_KEY`).
2. Add a provider client call in the `if _is_configured():` branch of `check()`.
3. Map the model response to a `Diagnostic` following the shape above.
4. Keep the unconfigured fallback (`_is_configured() == False`) unchanged so the
   fail-closed contract holds when credentials are absent.

No other files need to change: the backend registry, classifier, and validator
already treat `llm-rubric` as a first-class backend.

## Why deterministic gates remain authoritative

`gate-keeper` is a **compiler for merge gates**, not an AI reviewer.  Its value
comes from reproducible, evidence-bearing results that CI pipelines can trust.

LLM evaluation adds coverage for rules that humans write in natural language but
that do not map cleanly to a file check or a GitHub API field.  It is a complement
to deterministic checks — not a replacement.

When a rule has both a deterministic and a semantic interpretation, prefer the
deterministic route.  The classifier does this automatically: it only routes to
`llm-rubric` after exhausting all GitHub and filesystem patterns.
