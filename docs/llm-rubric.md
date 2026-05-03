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

## Default behavior (no provider configured)

If no provider is configured (the host-side dotenv file is absent or
incomplete), every `semantic_rubric` rule returns:

```
status:      unavailable
backend:     llm-rubric
evidence[0]: provider_unconfigured { rule_text, rule_kind, target }
remediation: Configure an LLM provider to enable semantic rule evaluation.
```

`unavailable` is a non-passing status; the CLI exits `1`. This is intentional
fail-closed behavior: an unconfigured evaluator must not silently pass rules.

## Host-side credential setup

`gate-keeper` reads provider credentials **only** from a per-project dotenv
file at:

```
/home/vscode/.config/hermes-projects/gate-keeper.env
```

It is loaded explicitly via `python-dotenv`'s `dotenv_values` into a
process-local dict; **`os.environ` is intentionally not consulted**. This is
deliberate — the env-passthrough route was rejected upstream because the
container's global API-key environment collides with the OAuth paths used by
Claude Code, Codex, and Gemini. See
[hermes-engineering#149](https://github.com/uda-lab/hermes-engineering/pull/149)
and the `docs/auth-matrix.md` "Issue #147" section.

Setup:

1. (host, one-time) After hermes-engineering devcontainer creation,
   `~/.config/hermes-projects/` exists with mode `700`.
2. (host) Create `~/.config/hermes-projects/gate-keeper.env` and `chmod 600`.
   Use one of:

   **Anthropic:**
   ```sh
   GATE_KEEPER_LLM_PROVIDER=anthropic
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   **OpenAI:**
   ```sh
   GATE_KEEPER_LLM_PROVIDER=openai
   OPENAI_API_KEY=sk-...
   ```

3. (container, automatic) `gate-keeper` reads the file via `python-dotenv`
   at backend invocation time.

If `GATE_KEEPER_LLM_PROVIDER` is missing, set to a value other than
`anthropic` or `openai`, or the corresponding API key is absent, behavior
falls back to the unconfigured `unavailable` diagnostic above.

## Rubric input/output shape

`_build_rubric_input()` in `src/gate_keeper/backends/llm_rubric.py` defines the
context passed to the model:

```json
{
  "rule_text": "<verbatim rule text from the document>",
  "rule_kind": "semantic_rubric",
  "target":    "<filesystem path or PR reference>"
}
```

### Structured judgment schema (`LlmJudgment`)

The model is instructed (via `RUBRIC_PROMPT_TEMPLATE`, prompt version `PROMPT_VERSION = "v1"`)
to respond with a JSON object matching the `LlmJudgment` dataclass:

```json
{
  "judgment":                    "pass" | "fail",
  "primary_reason":              "<one sentence>",
  "supporting_evidence_quotes":  ["<verbatim quote>", ...],
  "suggested_action":            "<concrete fix>" | null
}
```

Constraints enforced by `_parse_llm_judgment()`:
- `judgment` must be exactly `"pass"` or `"fail"`.
- `primary_reason` must be a non-empty string (single sentence).
- `supporting_evidence_quotes` must contain at least one entry when `judgment` is `"fail"`; may be empty on `"pass"`.
- `suggested_action` must be a non-empty string on `"fail"` and is coerced to `null` on `"pass"`.
- Extra fields in the JSON response are silently ignored (forward-compatible).

### Successful diagnostic shape

When a provider responds successfully, the diagnostic carries:

- `status`: `pass` or `fail`.
- `message`: the `primary_reason` from the structured judgment.
- `evidence[0]`: `{ kind: "llm_judgment", data: { model, prompt_version, judgment, primary_reason, supporting_evidence_quotes, suggested_action } }`.
- `remediation`: set to `suggested_action` when `status=fail`; `null` on `pass`.

## Failure modes

Any provider failure path maps to `unavailable` — never to `pass` or `fail`.
Failure modes recorded in `evidence[0]`:

| Failure | `evidence[0].kind` | `data.failure_mode` |
| --- | --- | --- |
| File missing or provider unset | `provider_unconfigured` | n/a |
| SDK/HTTP error, timeout, etc. | `provider_error` | exception class name |
| Response is not the expected JSON shape | `provider_error` | `unparseable_response` |

There is no retry. Investigate the failure mode, then rerun.

## Extending to additional providers

Currently `anthropic` and `openai` are wired. To add another provider:

1. Add a `_call_<provider>(api_key, system, user, model)` function returning the
   raw response text.
2. Add the provider name to `_SUPPORTED_PROVIDERS` and the dispatch branch in
   `check()`.
3. Document the env-file shape (`<PROVIDER>_API_KEY` plus
   `GATE_KEEPER_LLM_PROVIDER=<provider>`).
4. Add monkeypatched tests for `pass` / `fail` / provider error / unparseable
   response paths.

The backend registry, classifier, and validator already treat `llm-rubric` as
a first-class backend, so no other files need to change.

## Project trunk: semantic rubric quality

Wiring a real provider (gateway issue #51) is the entry point to this project's
completion trunk, not its end.  The owner's working definition of "completion"
for `gate-keeper` is the intellectual work that lifts the semantic rubric
backend to production-grade quality — prompt design, evaluation reproducibility,
cost / failure policy, hybrid deterministic+semantic rule kinds, semantic
self-gating, diagnostic output quality, and a severity ladder grounded in
measured reliability.

That work is tracked under the umbrella issue **#63 (semantic rubric backend
quality — project completion trunk)**.  Distribution and adoption polish
(PyPI publish, CI workflow templates, config file, getting-started guide,
multi-document composition, generic standard rule library) are *not* on this
trunk; the related issues (#52–#58) are deferred indefinitely.

The list in `docs/mvp-readiness.md` under "Upgrade Path After MVP" is an idea
dump from the MVP cut, not a prioritized roadmap.  Treat #63 as the source of
truth for what "next" means for semantic-rubric quality work, and treat gateway
issue #51 as the source of truth for provider wiring details.

Provider selection is **not fixed** — Anthropic and OpenAI are both viable
targets.  The one implementation constraint from issue #51 is credential
transport: load the provider env file explicitly via `python-dotenv` rather than
reading from `os.environ` directly (the `os.environ` pass-through route was
rejected to avoid collisions with the host container's global API-key
environment).  The extension-point guidance on this page (`GATE_KEEPER_LLM_PROVIDER`,
`OPENAI_API_KEY`, etc.) remains valid as-is.

## Why deterministic gates remain authoritative

`gate-keeper` is a **compiler for merge gates**, not an AI reviewer.  Its value
comes from reproducible, evidence-bearing results that CI pipelines can trust.

LLM evaluation adds coverage for rules that humans write in natural language but
that do not map cleanly to a file check or a GitHub API field.  It is a complement
to deterministic checks — not a replacement.

When a rule has both a deterministic and a semantic interpretation, prefer the
deterministic route.  The classifier does this automatically: it only routes to
`llm-rubric` after exhausting all GitHub and filesystem patterns.
