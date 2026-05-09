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
   # optional model override; defaults to claude-haiku-4-5
   GATE_KEEPER_ANTHROPIC_MODEL=claude-opus-4-7
   ```

   **OpenAI:**
   ```sh
   GATE_KEEPER_LLM_PROVIDER=openai
   OPENAI_API_KEY=sk-...
   # optional model override; defaults to gpt-4o-mini
   GATE_KEEPER_OPENAI_MODEL=gpt-4o
   ```

3. (container, automatic) `gate-keeper` reads the file via `python-dotenv`
   at backend invocation time.

If `GATE_KEEPER_LLM_PROVIDER` is missing, set to a value other than
`anthropic` or `openai`, or the corresponding API key is absent, behavior
falls back to the unconfigured `unavailable` diagnostic above.

### Model selection

Provider model selection is also dotenv-driven. The active model is
resolved from the same per-project file (no `os.environ` lookup) using:

| Provider | Override variable | Default |
| --- | --- | --- |
| `anthropic` | `GATE_KEEPER_ANTHROPIC_MODEL` | `claude-haiku-4-5` |
| `openai` | `GATE_KEEPER_OPENAI_MODEL` | `gpt-4o-mini` |

Behavior:

- An unset, missing, or whitespace-only override falls back to the source
  default constant.
- The provider-specific override only applies when the matching provider
  is active (e.g. `GATE_KEEPER_OPENAI_MODEL` is ignored when the provider
  is `anthropic`).
- The actual model used is recorded in `llm_judgment.evidence[0].data.model`
  on every successful run.
- If the override names a model that is not in the static `_MODEL_PRICING`
  snapshot/table (see "Per-rule observability fields" below),
  `cost_estimate_usd` will be `null` (fail-closed: cost is never guessed).
  Telemetry fields (`latency_ms`, `tokens_in`, `tokens_out`) are still
  recorded.

`gate-keeper diagnose` surfaces the resolved model and whether it came
from an override or the default.

### CI exception: GitHub Actions secret projection

The host-side dotenv route is the only credential path the backend reads
from. CI environments do not have a developer-managed home directory, so
GitHub Actions workflows project the repository secret into the same
absolute dotenv path before invoking `gate-keeper`.

The advisory dogfooding workflow (`.github/workflows/dogfooding.yml`,
introduced in #131) demonstrates the pattern:

```yaml
- name: Provision provider credentials
  if: steps.secret_guard.outputs.secret_present == 'true'
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: |
    set +x
    umask 077
    sudo install -d -m 700 -o "$USER" -g "$USER" /home/vscode
    sudo install -d -m 700 -o "$USER" -g "$USER" /home/vscode/.config
    sudo install -d -m 700 -o "$USER" -g "$USER" /home/vscode/.config/hermes-projects
    printf 'GATE_KEEPER_LLM_PROVIDER=openai\nOPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" \
      > /home/vscode/.config/hermes-projects/gate-keeper.env
    chmod 600 /home/vscode/.config/hermes-projects/gate-keeper.env
```

Constraints to preserve when adopting this pattern in another workflow:

- **Job-scoped secret guard.** Reference `secrets.OPENAI_API_KEY` from a
  preceding step that writes a boolean output, then gate downstream steps
  with `if: steps.<id>.outputs.secret_present == 'true'`. This keeps the
  workflow silent on forks and pre-secret states (no red X on PRs from
  contributors who do not own the secret).
- **No `set -x` and no `echo $VAR`.** Both leak secret material into the
  run log. Use `printf` with redirection only. Disable trace mode
  defensively (`set +x`) inside the credential step.
- **Mode 700 on the directory, mode 600 on the file.** `umask 077` plus
  `chmod 600` on the dotenv satisfy both, even when run as the runner
  user.
- **No artifact upload of `$HOME` or `/home/vscode`.** Treat the dotenv
  path as untrusted output; never include it in `actions/upload-artifact`
  patterns.
- **Scrub on completion.** Add an `if: always()` cleanup step that
  removes the projected dotenv. The runner is ephemeral, but the
  scrubbing step documents intent and survives reuse if anyone refactors
  the workflow to share runners.

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

The model is instructed (via `RUBRIC_PROMPT_TEMPLATE`, prompt version `PROMPT_VERSION = "v4"`)
to respond with a JSON object matching the `LlmJudgment` dataclass:

```json
{
  "judgment":                    "pass" | "fail",
  "primary_reason":              "<one sentence>",
  "supporting_evidence_quotes":  ["<near-verbatim substring of the target>", ...],
  "suggested_action":            "<concrete fix>" | null
}
```

Constraints enforced by `_parse_llm_judgment()`:
- `judgment` must be exactly `"pass"` or `"fail"`.
- `primary_reason` must be a non-empty string (single sentence).
- `supporting_evidence_quotes` must contain at least one entry for **every
  verdict — both `"pass"` and `"fail"`** (#168). The prompt additionally
  instructs the model to draw quotes as near-verbatim substrings of the
  target text and to favor representative content over opening-line
  citations on long artifacts.
- `suggested_action` must be a non-empty string on `"fail"` and is coerced to `null` on `"pass"`.
- Extra fields in the JSON response are silently ignored (forward-compatible).

### Parser-side substring grounding (#172)

Prompt-side discipline alone is not sufficient. Tick 6 of umbrella #164's
dogfood loop showed that on the first sample after the v2 prompt merged
the model returned six fail verdicts whose `supporting_evidence_quotes`
had **zero overlap** with the artifact (generic "fail-shaped" placeholder
strings). A model-instruction-only constraint is by design unreliable.

The backend therefore enforces the substring contract in `check()` after
`_parse_llm_judgment()` succeeds:

- Each entry of `supporting_evidence_quotes` must occur as a substring of
  the artifact text the rule is being evaluated against. Whitespace runs
  are normalised (collapsed to a single space) and a small set of smart
  punctuation pairs (curly single/double quotes, en/em dashes) is folded
  to their ASCII counterparts, so the model may copy with cosmetic
  differences. **Paraphrase or rewording is not tolerated.**
- The check runs against `str(target)` — exactly the string the prompt
  template renders into the `Target reference` block via `_build_prompt`.
  This matches what the model actually sees: the provider helpers do
  not give the model a file-read tool, so for path / PR-reference
  targets the model only ever has access to the reference string. The
  bench harness pre-resolves path targets to file contents before
  calling `check`, so for bench callers the target string is already the
  inline content.
- On any violation the verdict is **rejected**: the diagnostic returns
  `status=unsupported` with `evidence[0]` of kind
  `llm_quote_fabrication`. The evidence preserves the model's claimed
  judgment, primary reason, all returned quotes, the subset that failed
  containment (`fabricated_quotes`), and the standard telemetry / cost
  fields. `Diagnostic.remediation` advises the operator not to act on the
  verdict.
- Empty / whitespace-only quotes are treated as fabricated (zero-grounding
  is a degenerate case, not a forgivable normalisation difference).

This is option (A) from #172's design: reject the verdict rather than
strip-and-flag offending quotes. The user-visible signal "this verdict
came from a model that ignored the substring contract" is more valuable
than a verdict whose grounding has been silently degraded.

### Successful diagnostic shape

When a provider responds successfully, the diagnostic carries:

- `status`: `pass` or `fail`.
- `message`: the `primary_reason` from the structured judgment.
- `evidence[0]`: `{ kind: "llm_judgment", data: { model, prompt_version, judgment, primary_reason, supporting_evidence_quotes, suggested_action, latency_ms, tokens_in, tokens_out, cost_estimate_usd } }`.
- `remediation`: set to `suggested_action` when `status=fail`; `null` on `pass`.

### Per-rule observability fields

Issue #76 adds three observability fields and issue #133 adds cost estimation to every
successful `llm_judgment` evidence entry, providing the data substrate for drift
detection, cost analysis, and per-rule SLA work:

| Field | Type | Source | Semantics |
| --- | --- | --- | --- |
| `latency_ms` | `int` (milliseconds) | `time.perf_counter()` around the SDK call | Wall-clock duration of the provider call, integer-rounded. |
| `tokens_in` | `int` | Anthropic `usage.input_tokens` / OpenAI `usage.prompt_tokens` | Provider-reported prompt token count. |
| `tokens_out` | `int` | Anthropic `usage.output_tokens` / OpenAI `usage.completion_tokens` | Provider-reported completion token count. |
| `cost_estimate_usd` | `float \| null` | Static `_MODEL_PRICING` table (snapshot 2026-05-08) | Estimated USD cost for this call; `null` for unknown models (fail-closed). |

Pricing snapshot (2026-05-08) used for `cost_estimate_usd`:

| Model | Input (per 1M tokens) | Output (per 1M tokens) |
| --- | --- | --- |
| `gpt-4o-mini` | $0.15 | $0.60 |
| `claude-haiku-4-5` | $0.80 | $4.00 |

These fields appear **only on successful provider calls** that produce a
parseable, schema-conforming judgment. The `provider_unconfigured` and
`provider_error` evidence kinds — covering all non-success paths: provider
unset, the SDK call raised, the response omitted required usage fields, or
the response failed JSON parse / schema validation — intentionally do not
carry telemetry. Missing evidence is recorded as missing rather than
synthesised.

When `--reproducibility N` aggregates `N` runs, the `latency_ms` / `tokens_in`
/ `tokens_out` / `cost_estimate_usd` on the chosen majority-judgment evidence
reflect that single representative run, not an aggregate. The separate
`reproducibility_score` evidence entry (#68) carries no telemetry.

## Failure modes

Any provider failure path maps to `unavailable` — never to `pass` or `fail`.
Quote fabrication maps to `unsupported` (the request succeeded but the
verdict is rejected as ungrounded).  Failure modes recorded in
`evidence[0]`:

| Failure | `status` | `evidence[0].kind` | `data.failure_mode` |
| --- | --- | --- | --- |
| File missing or provider unset | `unavailable` | `provider_unconfigured` | n/a |
| SDK/HTTP error, timeout, etc. | `unavailable` | `provider_error` | exception class name |
| Response is not the expected JSON shape | `unavailable` | `provider_error` | `unparseable_response` |
| Quotes not substrings of artifact (#172) | `unsupported` | `llm_quote_fabrication` | n/a (offending quotes in `data.fabricated_quotes`) |

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

## Prompt iteration workflow

This section describes the process for changing and evaluating the LLM rubric
prompt (`RUBRIC_PROMPT_TEMPLATE` in `src/gate_keeper/backends/llm_rubric.py`).

### When to bump PROMPT_VERSION

Bump `PROMPT_VERSION` only when the prompt text changes in a way that materially
affects model behavior — for example, adding or removing evaluation criteria,
changing the output schema instructions, or reordering the rubric dimensions.

Do **not** bump for:

- Whitespace-only or punctuation edits that leave semantics unchanged.
- Comment-only changes in the source file.
- Reformatting that does not alter the rendered string the model receives.

The version is embedded in every `llm_judgment` evidence entry so that baseline
comparisons can detect prompt-driven drift. Bumping without a behavioral change
pollutes the signal; skipping a bump when behavior changes makes baselines
incomparable.

### Required steps when bumping PROMPT_VERSION

1. **Record the previous baseline.**  The current baseline lives at
   `tests/fixtures/semantic/baseline.json`.  Keep it or tag the commit before
   the bump so you can compare before/after.

2. **Run bench with the new prompt and compare to the committed baseline:**

   ```sh
   # Run the new prompt against the corpus and save results to a temp file.
   uv run gate-keeper bench tests/fixtures/semantic/entries/ \
     --reproducibility 3 --format json \
     > /tmp/new-baseline.json

   # Diff the new run against the committed baseline (not against itself).
   uv run gate-keeper bench tests/fixtures/semantic/entries/ \
     --reproducibility 3 \
     --baseline tests/fixtures/semantic/baseline.json
   ```

   The `--baseline` flag prints a delta showing accuracy change and any
   regressions (entries whose `status` flipped from PASS to FAIL) or fixes
   (FAIL → PASS).  The `--baseline` path must point at the **committed**
   baseline (`tests/fixtures/semantic/baseline.json`), not the new run
   output — diffing a run against itself produces a meaningless zero delta.

3. **If accuracy regression is present**, include the full delta output in the
   PR description under a `## Prompt regression analysis` header.

4. **Update `tests/fixtures/semantic/baseline.json`** to the new run output
   in the same PR as the `PROMPT_VERSION` bump, so that main always contains
   a baseline that corresponds to the currently checked-in prompt.  Replace
   the file with the `/tmp/new-baseline.json` from step 2.

### Baseline metrics (generated 2026-05-10)

The `tests/fixtures/semantic/baseline.json` file was generated with:

```sh
uv run gate-keeper bench tests/fixtures/semantic/entries/ \
  --reproducibility 3 --format json \
  > tests/fixtures/semantic/baseline.json
```

Measured results at prompt version `v4` (model `gpt-4o-mini`; for cost per
token see the pricing snapshot table in the "Per-rule observability fields"
section above):

| Metric | Value |
| --- | --- |
| Entries | 28 |
| Correct | 16 |
| Accuracy | 57.1% |
| Reproducibility (avg) | 100.0% |
| Reproducibility N | 3 |
| Tokens in | 65,288 |
| Tokens out | 6,289 |
| Latency (total) | 112,186 ms |
| Model | `gpt-4o-mini` |
| Prompt version | `v4` |

Reference history:

- v1 (24 entries): 19 correct / 79.2% accuracy / 31,080 tokens in.
- v2 (25 entries): 17 correct / 68.0%. The v1 → v2 transition added
  `justification-06-commit-message-rationale-past-opening` (L25
  first-line-quote bias from #168) and tightened
  `supporting_evidence_quotes` constraints; see PR #168.
- v3 (26 entries): 14 correct / 53.8%. The v2 → v3 transition added the
  optional `target_kind` annotation, the artifact-kind prompt block, and
  the `target-kind-mismatch-01` fixture; see PR #174 / #169.
- v4 (28 entries): 16 correct / 57.1%. The v3 → v4 transition (#175)
  rewrites the artifact-kind block to name the rule's annotated
  `target_kind` value, replaces the canned ``unsupported`` example with
  kind-neutral placeholders, and **gates the unsupported-example block
  and the artifact-kind dispatch instruction on annotation** so an
  unannotated rule never sees the lure of an `"unsupported"` schema
  option. (Pre-gating, an unannotated rule like
  `completeness-05-rule-doc-has-target-cue` would emit a stray
  `"unsupported"` verdict that the backend then degrades to
  `provider_error / unsupported_without_target_kind` — surfaced by
  Copilot review on PR #176.) v4 also adds two regression fixtures
  (`target-kind-mismatch-02-commit-rule-on-pr` and
  `target-kind-mismatch-03-commit-rule-on-commit-no-mismatch`) so the
  corpus exercises both directions of the dispatch and the positive
  grounding case. The qualitative win — primary_reason now grounds the
  rule's annotated kind verbatim ("The rule is annotated
  `commit_message` but the artifact provided is a pull request
  description.") — is the observable goal.

### Per-model dispatch accuracy (#175)

The v4 prompt fixes the verbatim-parroting bug at the prompt-template
level, but `gpt-4o-mini` still misjudges the **artifact kind** itself in
some cases — for example, treating a short commit message as a "PR
description" so the artifact-kind dispatch never fires. This is a
model-capability limitation, not a prompt bug. When dispatch accuracy
matters (e.g. for advisory dogfood gates that route per `target_kind`),
prefer a stronger model via the `GATE_KEEPER_OPENAI_MODEL` /
`GATE_KEEPER_ANTHROPIC_MODEL` dotenv override; gpt-4o-mini is appropriate
for the per-PR cost target but not for high-confidence artifact-kind
dispatch.

### Regression tolerance and justification template

Some regressions are acceptable — for example, when a prompt change improves
coverage for a harder category at the cost of one easier entry, or when the
previous baseline had known false positives that the new prompt corrects.

When accepting a regression, include this justification block in the PR
description:

```
## Prompt regression justification

Prompt version bumped: v<old> → v<new>

Accuracy delta: -X.X% (<N> entries regressed, <M> entries fixed)

Accepted because:
- <reason — e.g. "regressed entries were known false positives in v1">
- <reason — e.g. "net improvement in <category> category outweighs single regression">

Regressed entries:
- <entry-id>: was PASS (v<old>), now FAIL (v<new>) — <brief rationale>

Fixed entries:
- <entry-id>: was FAIL (v<old>), now PASS (v<new>) — <brief rationale>
```

A regression with no justification block is a blocker for merging the prompt
change.  The justification is the evidence that the change was reviewed, not
just checked in.
