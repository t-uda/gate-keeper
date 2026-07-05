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

### Ad-hoc routing audit (`scripts/classify_semantic_candidates.py`)

A one-off utility script asks `gpt-5` (minimal reasoning) whether each
`semantic_rubric`-fallback rule in a rules file could be evaluated
deterministically by `filesystem`, `github`, or `external+textlint`, and
emits a JSON report for human triage. It does **not** change classifier
behavior; the report is read-only triage input. Not wired into bench or CI.

```sh
uv run python scripts/classify_semantic_candidates.py \
    --rules docs/dogfooding-rules.md \
    [--model openai:gpt-5] \
    [--out report.json]
```

Refs: issue #235, #75 (classifier feedback loop, slice 1).

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
$HOME/.config/hermes-projects/gate-keeper.env
```

The path resolves via `Path.home()` at runtime, so it matches the
developer's home directory in a devcontainer (`/home/vscode/...`),
on a hosted CI runner (`/home/runner/...`), or on a bare developer
machine.

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

#### Strict-mode override (`GATE_KEEPER_REQUIRE_MODEL`)

Opt-in via the same dotenv. When set to a truthy value (`1`, `true`,
`yes`; case-insensitive), an unset / blank `GATE_KEEPER_<PROVIDER>_MODEL`
raises an internal `ModelConfigurationError` that the backend surfaces
as:

- `Status.UNAVAILABLE`
- `evidence[0].kind = "provider_unconfigured"`
- `evidence[0].data.failure_mode = "model_unconfigured"`
- `evidence[0].data.provider`, `evidence[0].data.override_key`
- `remediation` names the override key and the strict-mode opt-out.

Intended use is CI environments whose provider key is allow-listed to a
specific model — the silent fallback would otherwise be rejected by the
provider and surface as opaque `provider_error` on every rule, masking
the real misconfiguration (issue #266 / #264). Strict mode is off by
default; existing developer dotenvs are unaffected.

### Restricted API key setup

Provider-specific key restriction is not a single shared mechanism — each
provider exposes a different surface, so the minimal-permission setup for
gate-keeper must be configured per provider.

**OpenAI (post-#248).** The backend calls
`client.responses.create(...)` (the `/v1/responses` endpoint). When
issuing a restricted API key from the OpenAI Platform UI, set only:

- `Responses` (`/v1/responses`): **Request**
- `Chat completions` (`/v1/chat/completions`): **None** — gate-keeper no
  longer touches this endpoint after #248.
- All other resources / capabilities: **None**.

This is the minimal endpoint surface required for `_call_openai` in
`src/gate_keeper/backends/llm_rubric.py`. Do not enable agent tools,
file search, web search, or code interpreter — gate-keeper does not use
any Responses API tool surface.

**Anthropic.** The Anthropic Console does not currently expose the
endpoint-by-endpoint capability matrix that OpenAI offers, so there is no
direct equivalent of `Responses: Request, Chat completions: None`.
Practical isolation for gate-keeper is workspace- and spend-scoped
instead:

- Create a dedicated Anthropic workspace (or at minimum a dedicated API
  key) used only by gate-keeper.
- Apply a low spend limit on that workspace/key so a runaway loop is
  bounded by billing rather than by capability scoping.
- Rotate the key independently of other Anthropic keys you hold.

The gate-keeper Anthropic backend uses the Messages API via
`client.messages.create(...)`; no further endpoint restriction is
available in the standard Console flow.

**Google AI Studio / Gemini (forward note).** gate-keeper does not
currently support a Gemini provider. If a Gemini backend is added later,
its restricted-key guidance will not match the OpenAI shape either — it
will use Google Cloud's mechanisms:

- Issue the key from a dedicated Google Cloud project used only by
  gate-keeper.
- Restrict the API key to the Generative Language API (Gemini) and deny
  all other APIs.
- Where applicable, add API-key application restrictions such as a fixed
  egress IP allowlist.
- Set low quota and billing limits on the project.

Treat OpenAI endpoint permissions, Anthropic workspace/spend isolation,
and Google Cloud API-key restrictions as three distinct mechanisms, not
one shared provider-neutral security model.

### CI exception: GitHub Actions secret projection

The host-side dotenv route is the only credential path the backend reads
from. CI environments do not have a developer-managed home directory, so
GitHub Actions workflows project the repository secret into the same
absolute dotenv path before invoking `gate-keeper`.

The dogfooding workflow (`.github/workflows/dogfooding.yml`,
introduced in #131) demonstrates the pattern:

```yaml
- name: Provision provider credentials
  if: steps.secret_guard.outputs.secret_present == 'true'
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: |
    set +x
    umask 077

    # Source the non-secret dogfooding config (#266). The model name
    # lives in a tracked file rather than a workflow literal so updating
    # it is a PR diff, not an Actions UI tweak.
    # shellcheck disable=SC1091
    . .github/dogfooding-config.env

    # Fail fast on missing / blank model config.
    if [ -z "${GATE_KEEPER_OPENAI_MODEL:-}" ]; then
      echo "::error::GATE_KEEPER_OPENAI_MODEL is unset or blank in .github/dogfooding-config.env" >&2
      exit 1
    fi

    mkdir -p "$HOME/.config/hermes-projects"
    printf 'GATE_KEEPER_LLM_PROVIDER=openai\nGATE_KEEPER_OPENAI_MODEL=%s\nGATE_KEEPER_REQUIRE_MODEL=1\nOPENAI_API_KEY=%s\n' \
      "$GATE_KEEPER_OPENAI_MODEL" "$OPENAI_API_KEY" \
      > "$HOME/.config/hermes-projects/gate-keeper.env"
    chmod 600 "$HOME/.config/hermes-projects/gate-keeper.env"
```

The backend's default dotenv path is computed at runtime as
`Path.home() / ".config/hermes-projects/gate-keeper.env"`. On
GitHub-hosted runners `$HOME=/home/runner`; in a devcontainer it is
typically `/home/vscode`. The projection target MUST follow `$HOME` so
the backend's read path matches the workflow's write path on every host
(issue #261).

`GATE_KEEPER_OPENAI_MODEL` is sourced from the tracked file
`.github/dogfooding-config.env` (#266) because the repository's
restricted CI key is allow-listed to a specific model only; without
that override the backend default (`gpt-4o-mini`) would be rejected by
the provider and surface as `provider_error` on every rule (issue
#264). The model name is not a secret — keeping it in a reviewable
in-repo file rather than a workflow literal or Actions variable means a
model update is a normal PR diff, with no out-of-tree GUI state to
maintain.

`GATE_KEEPER_REQUIRE_MODEL=1` is projected into the runtime dotenv
alongside the model name (#266). It switches the backend's
`_resolve_model` into strict mode: if `GATE_KEEPER_OPENAI_MODEL` (or
`GATE_KEEPER_ANTHROPIC_MODEL` for the anthropic provider) is unset or
blank, the backend emits `provider_unconfigured` with
`evidence[0].data.failure_mode = "model_unconfigured"` and names the
override key in the remediation string, rather than silently falling
back to the source default. The strict-mode key is opt-in via dotenv —
no developer flow that omits it changes behaviour. Recognised truthy
values: `1`, `true`, `yes` (case-insensitive).

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
- **No artifact upload of `$HOME`.** Treat the dotenv path as untrusted
  output; never include it in `actions/upload-artifact` patterns.
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

The model is instructed (via `RUBRIC_PROMPT_TEMPLATE`, prompt version
`PROMPT_VERSION = "v8"`) to return **line-range evidence references**:

```json
{
  "judgment": "pass" | "fail" | "unsupported",
  "unsupported_reason": "target_kind_mismatch" | "cross_artifact_predicate" | null,
  "primary_reason": "<one sentence>",
  "supporting_evidence_refs": [
    {"target_id": null, "path": "README.md", "line_start": 12, "line_end": 18}
  ],
  "suggested_action": "<concrete fix>" | null
}
```

Primary contract (#268):
- Prompt target blocks are rendered with stable 1-based line numbers.
- Each rendered artifact carries an explicit `Path:` header — either a
  filesystem path (`Path: <path>`) or the literal `Path: null` for inline /
  no-path single-target artifacts. The model's evidence-ref `path` field
  must match the rendered header (or be `null` when the header is
  `Path: null`).
- `supporting_evidence_refs` is validated against the exact prompt-visible
  corpus (single-target and multi-target paths share the same resolver).
- On success, gate-keeper reconstructs `supporting_evidence_quotes`
  mechanically from the referenced line ranges and emits both:
  `supporting_evidence_quotes` (compat) and `supporting_evidence_refs`
  (structured contract).

Malformed refs (out-of-range, reversed, non-integer, unknown `target_id`,
unquotable placeholder target, etc.) are treated as grader contract failures:
`status=unavailable`, `evidence.kind=invalid_evidence_reference`,
`failure_mode=grader_error`.

### Decline verdicts (`unsupported`)

An `unsupported` verdict means the model is **declining** to render a pass/fail
rather than fabricating one; `unsupported_reason` discriminates two cases, both
mapping to `Status.UNSUPPORTED` (a non-pass, fail-closed status):

- `target_kind_mismatch` (#169) — the rule carries an explicit `target_kind`
  annotation and the artefact provided is a different kind. Honoured only when
  the rule is annotated; a stray decline on an unannotated rule degrades to
  `status=unavailable` / `failure_mode=unsupported_without_target_kind`.
  Evidence kind: `target_kind_mismatch`.
- `cross_artifact_predicate` (#291) — deciding the rule requires examining
  artefacts absent from the rendered target (e.g. a completeness rule that
  ranges over a `src/` tree judged against only a doc). Honoured for **any**
  rule, annotated or not. Evidence kind: `cross_artifact_predicate`; the
  remediation directs the author to `params.target_scope` / multi-target (see
  `docs/semantic-rules.md` and `docs/design/multi-target.md` §9). The decline
  fires only when the absent artefacts are essential to the predicate — a rule
  that merely names another file but is decidable from the target is judged
  normally.

Under multi-call strategies a cross-artifact decline counts as an ordinary
`unsupported` vote (consensus) and is conclusive without escalation (review,
adaptive) — a re-run against the same insufficient target cannot recover it.

### Legacy quote fallback (#172)

Legacy quote-shaped outputs (`supporting_evidence_quotes` without refs) are
still accepted defensively. In that fallback path, the original substring
anti-fabrication validator remains active:

- Non-substring / fabricated quotes produce `status=unsupported`,
  `evidence.kind=llm_quote_fabrication`.
- Successful legacy outputs still emit `supporting_evidence_quotes` plus
  span metadata for compatibility.

### Span-based evidence offsets (#179)

After the substring-grounding validator accepts the model's quotes, the
backend additionally resolves each validated quote to a span pointing into
the **original** artifact text. Spans surface as a parallel list under
`evidence[0].data.supporting_evidence_spans` on every successful
`llm_judgment` evidence record. The pre-existing
`supporting_evidence_quotes` field is preserved verbatim — spans are
**additive**, not a replacement.

A span has the following shape:

```json
{
  "quote": "<the model's raw quote string, verbatim>",
  "artifact_index": 0,
  "start_offset": 142,
  "end_offset": 181,
  "start_line": 8,
  "start_column": 3,
  "end_line": 8,
  "end_column": 42,
  "match_normalisation": "exact" | "smart_quotes" | "whitespace"
}
```

- **Character offsets** (`start_offset`, `end_offset`) — Python `str`
  indices into the artifact text the model was shown (the same string the
  fabrication validator runs against; see "Parser-side substring
  grounding"). Half-open: `artifact_text[start_offset:end_offset]` slices
  the matched substring out of the **original** artifact, even when the
  match required normalisation. Byte offsets are not emitted; the artifact
  is held as a Python `str` and recovering bytes would require nailing an
  encoding for no extra evidence.
- **1-indexed line / column** (`start_line`, `start_column`, `end_line`,
  `end_column`) — for human-friendly rendering. Lines and columns are
  1-indexed; columns count Unicode code points within the line. End
  position is one past the last matched character (matching the half-open
  convention of `end_offset`).
- **`artifact_index: 0`** — reserved for the multi-artifact follow-up
  (#182). Single-artifact callers can ignore it; including it now keeps
  the wire format forward-compatible.
- **`match_normalisation`** — records which tolerance the resolver had
  to apply. `"exact"` means the quote was found verbatim; `"smart_quotes"`
  means the curly-quote / dash folding (see `_QUOTE_FOLDING`) was needed;
  `"whitespace"` means whitespace-run collapsing was needed (typically a
  line-wrapped quote in the artifact rendered as one line by the model).
  The resolver records the **weakest** normalisation that succeeded — an
  exact match always wins over a smart-quote match, and a smart-quote
  match always wins over a whitespace-collapse match.
- **`quote`** — the model's raw quote string, preserved verbatim so a
  consumer can correlate `supporting_evidence_spans[i]` with
  `supporting_evidence_quotes[i]` even when the in-artifact text differs
  (e.g. a smart-quote-folded match where the original artifact carries
  curly punctuation).

#### Duplicate quote handling

If a quote occurs multiple times in the artifact, the span points to the
**first match**. This is the simplest deterministic default and the one
humans usually want when asking "where did the model find this quote?".
A future iteration may add an "ambiguity flag" or all-matches list, but
the first-match contract is stable for the current evidence shape.

#### Fabricated quotes do not get spans

Quote fabrication still produces an `llm_quote_fabrication` diagnostic
(see "Parser-side substring grounding" above) — by definition, a
fabricated quote has no in-artifact location. The fabrication evidence
record carries the standard quote / telemetry fields and **does not**
include a `supporting_evidence_spans` field. Consumers that branch on
`evidence[0].kind == "llm_judgment"` get spans; consumers that branch
on `evidence[0].kind == "llm_quote_fabrication"` do not.

#### Compatibility

`supporting_evidence_quotes` remains the stable, authoritative
compatibility surface. A consumer that ignores
`supporting_evidence_spans` keeps working unchanged. A consumer that
needs location-aware rendering reads the spans list. If the resolver
fails to locate a validated quote (a guard against future drift between
the validator and the resolver), that quote is silently omitted from the
span list — the quote remains in `supporting_evidence_quotes`, so no
information is lost; only the optional location annotation is missing.

### Successful diagnostic shape

When a provider responds successfully, the diagnostic carries:

- `status`: `pass` or `fail`.
- `message`: the `primary_reason` from the structured judgment.
- `evidence[0]`: `{ kind: "llm_judgment", data: { model, prompt_version, judgment, primary_reason, supporting_evidence_quotes, supporting_evidence_spans, suggested_action, latency_ms, tokens_in, tokens_out, cost_estimate_usd } }`.
- `remediation`: set to `suggested_action` when `status=fail`; `null` on `pass`.

### Per-rule observability fields

Issue #76 adds three observability fields and issue #133 adds cost estimation to every
successful `llm_judgment` evidence entry, providing the data substrate for drift
detection, cost analysis, and per-rule SLA work:

| Field | Type | Source | Semantics |
| --- | --- | --- | --- |
| `latency_ms` | `int` (milliseconds) | `time.perf_counter()` around the SDK call | Wall-clock duration of the provider call, integer-rounded. |
| `tokens_in` | `int` | Anthropic `usage.input_tokens` / OpenAI Responses `usage.input_tokens` | Provider-reported prompt token count. |
| `tokens_out` | `int` | Anthropic `usage.output_tokens` / OpenAI Responses `usage.output_tokens` | Provider-reported completion token count. |
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

## Content-addressed local evaluation cache (`--eval-cache`, #69 Slice B)

Pass `--eval-cache` to enable a local disk cache that skips redundant provider
calls when the rule predicate, target content, provider, model, strategy, and
sampling configuration have not changed.

```sh
gate-keeper validate rules.md --target artifact.md --eval-cache
```

Enable via dotenv instead of the CLI flag:

```
# ~/.config/hermes-projects/gate-keeper.env  (or the project dotenv)
GATE_KEEPER_EVAL_CACHE=1
```

The CLI flag takes precedence over the dotenv value.

### Cache key

The cache key is a SHA-256 digest of a canonical JSON preimage containing:
`prompt_version`, `provider`, `resolved_model_id`, `rule_content_hash`,
`targets` (path + `content_sha256` per artifact), `strategy` (ID + shaping
params), `sampling` (effective descriptor after capability check),
`artifact_kind`, and `reproducibility_n`.

`rule_id`, `source`, and `severity` are deliberately excluded from the key:
on a cache hit they are overridden from the current rule object (§2.5
rehydration), so two rules with identical predicates share a cache entry.

### Cache location and eviction

Entries live under `.gate-keeper/cache/eval/<hex-digest>.json` relative to
the working directory. A size-cap eviction (default 500 MB, LRU by mtime)
runs after each store; no locking is required — concurrent double-computes
are tolerated and the last writer wins.

### Write policy

Only `PASS` and `FAIL` results are stored. `UNAVAILABLE`, `ERROR`, and
`UNSUPPORTED` are never cached (§2.4 fail-open write filter).

### Hit semantics

On a cache hit the provider is not called. A `cache_hit` evidence entry is
appended to the diagnostic with `provider_calls: 0` and `cost_usd: 0`; this
entry is never persisted in the cache file itself.

### Corruption recovery

If an entry has an unknown `cache_schema_version`, or its JSON is malformed,
the entry is treated as a miss and the result is recomputed. The cache never
raises an exception on read (§2.3 fail-open).

## Deterministic-mode sampling (`--deterministic`, #69)

Pass `--deterministic` to inject provider-specific sampling parameters that
reduce output variability across repeated runs.

```sh
gate-keeper validate rules.md --target artifact.md --deterministic
```

### Per-provider capability table

| Provider / model class | Extra params sent | Rationale |
| --- | --- | --- |
| Anthropic (all models) | `temperature=0.0` | Lowers sampling randomness; accepted by all Anthropic models. |
| OpenAI standard (e.g. `gpt-4o-mini`, `gpt-4o`) | `temperature=0.0` | Same effect as Anthropic. |
| OpenAI reasoning-class (`gpt-5*`, `o1*`, `o3*`) | _(none)_ | Reasoning-class models reject `temperature`; sending it causes a provider error. The flag is still recorded in evidence for traceability. |

The capability table (`_DETERMINISTIC_CAPABILITY_TABLE` in `llm_rubric.py`) is
the authoritative source; the reasoning-class check uses the same longest-prefix
match as the existing `_REASONING_EFFORT_TABLE`.

### Evidence fields

Two fields are added to every `llm_judgment` evidence entry regardless of
whether `--deterministic` was requested:

| Field | Type | Value |
| --- | --- | --- |
| `deterministic_mode` | `bool` | `true` when `--deterministic` was active, `false` otherwise. |
| `deterministic_params` | `dict \| null` | The params dict injected (e.g. `{"temperature": 0.0}`) when `--deterministic` is active; `null` otherwise. For OpenAI reasoning models the value is `{}` (empty dict — flag was active but no params were sent). |

### Fail-closed rejection handling

If a provider rejects the injected params at runtime (e.g. a future model that
does not accept `temperature`), the error is recorded as `provider_error`
evidence with the exception class name in `data.failure_mode`. The backend
never crashes; the rule is marked `unavailable`. Add the model prefix to
`_DETERMINISTIC_CAPABILITY_TABLE["openai_reasoning"]` (or the equivalent
Anthropic entry) to suppress the param for that model class.

## Failure modes

Any provider failure path maps to `unavailable` — never to `pass` or `fail`.
Quote fabrication maps to `unsupported` (the request succeeded but the
verdict is rejected as ungrounded).  Failure modes recorded in
`evidence[0]`:

| Failure | `status` | `evidence[0].kind` | `data.failure_mode` |
| --- | --- | --- | --- |
| File missing or provider unset | `unavailable` | `provider_unconfigured` | n/a |
| Blank/missing model under strict mode (#266) | `unavailable` | `provider_unconfigured` | `model_unconfigured` |
| SDK/HTTP error, timeout, etc. | `unavailable` | `provider_error` | exception class name |
| Response is not the expected JSON shape | `unavailable` | `provider_error` | `unparseable_response` |
| Quotes not substrings of artifact (#172) | `unsupported` | `llm_quote_fabrication` | n/a (offending quotes in `data.fabricated_quotes`) |

There is no retry. Investigate the failure mode, then rerun.

## Contributor testing: hermetic vs live-provider tests

### Default behaviour

`uv run pytest` is **always hermetic**.  A pytest autouse fixture in
`tests/conftest.py` monkeypatches `_load_env_file` to return `{}` for every
ordinary test, so no host credential file (e.g.
`/home/vscode/.config/hermes-projects/gate-keeper.env`) is ever read.

Tests that expect `provider_unconfigured` behaviour are therefore stable both
in CI (which has no dotenv) and on a developer machine that has a real provider
configured.

### Live-provider tests

Tests that exercise a real LLM provider must be decorated with
`@pytest.mark.live_llm`.  They are skipped automatically unless **both**
conditions are met at collection time:

1. `GATE_KEEPER_RUN_LIVE_LLM_TESTS=1` is set in the environment.
2. The project dotenv file contains a supported provider and the corresponding
   API key (same check as `_is_configured()`).

Run them explicitly:

```sh
GATE_KEEPER_RUN_LIVE_LLM_TESTS=1 uv run pytest -m live_llm
```

### Adding a new test that needs the real dotenv

Mark it `@pytest.mark.live_llm` and add a skip guard inside the test body if
it requires keys beyond what `_is_configured()` checks:

```python
import pytest

@pytest.mark.live_llm
def test_real_provider_smoke():
    ...
```

The conftest autouse fixture detects the marker and skips patching, so the
real `_load_env_file` runs and the test sees the actual dotenv contents.

### Do not monkeypatch `_load_env_file` in ordinary tests

Before this isolation was centralised, individual tests called
`monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: {...})`.
That pattern still works (the central fixture is applied first; a second
`monkeypatch.setattr` on the same attribute overrides it within that test), but
new tests should rely on the autouse stub and only patch when they need a
specific non-empty env dict.

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
- v4 (28 entries): 16 correct / 57.1%. The v3 → v4 transition (PR #176 / #175)
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
- v5 (#182, slice 1): introduces a multi-target rendering branch. When a
  rule declares `params.targets` (a non-empty list of `{id, kind, path}`
  entries), the legacy `## Target reference` block is replaced by a
  labelled `## Target artifacts (multi)` block with one subsection per
  artifact (`### Target <id> (kind: <kind>)`) and the model is asked to
  attribute each entry of `supporting_evidence_quotes` to a `target_id`.
  The parser accepts both the legacy plain-string entries and the
  multi-target object form `{"target_id": "...", "quote": "..."}`;
  internally the strings are joined and an optional parallel
  `supporting_evidence_quote_target_ids` list is surfaced on the success
  evidence dict for multi-target rules.  Single-target rules (no
  `params.targets`) render byte-identical text to v4 and emit the v4
  evidence wire shape unchanged — only the `PROMPT_VERSION` constant
  changes.  Slice 1 caps the list at 5 entries and uses concatenated
  artifact text for substring grounding; per-artifact substring
  attribution and dep-gates-manifest integration are deferred to
  slice 2.  Bench/baseline regeneration is also deferred (one new
  fixture lives in tree under
  `tests/fixtures/semantic/entries/multi-target-01-…` but
  `baseline.json` is left at the v4 capture until the slice-2 prompt
  evaluation runs).
- v6 (#225, slice 2): hardens multi-target quote attribution. The
  multi-target instruction block now declares the `{target_id, quote}`
  object form **required** for multi-target rules (slice 1 said it was
  preferred but tolerated plain-string entries). Backend enforcement is
  tightened in lockstep: a quote whose `target_id=A` must be a substring
  of artifact A's text specifically (the concatenated-text grounding
  used in slice 1 is replaced by per-target grounding); unknown
  `target_id` values fail closed; missing `target_id` on a multi-target
  rule fails closed; placeholder text from unreadable artifacts cannot
  be quoted (the placeholder ID is excluded from the corpus). The
  single-target path (rules without `params.targets`) is unchanged
  from v5. v5 and v6 evidence are **not** interchangeable — a v5-era
  multi-target evidence record showing
  `supporting_evidence_quote_target_ids=["first_id", ...]` may reflect
  the lenient slice-1 default attribution, whereas a v6 record only
  ever shows IDs the model itself emitted (or `null` after the strict
  fabrication guard). Baseline comparison runs must filter on
  `prompt_version`.
- v7 (#268): moves the primary evidence contract from free-form quote text
  to structured `supporting_evidence_refs` line ranges. Prompt artifacts are
  rendered with stable line numbers, backend validation reconstructs
  `supporting_evidence_quotes` mechanically from ranges, and malformed refs
  map to `status=unavailable` with
  `evidence.kind=invalid_evidence_reference` /
  `failure_mode=grader_error`. Legacy quote fabrication checks remain as a
  fallback path only.

### Per-model dispatch accuracy (#175)

The v4 prompt fixes the verbatim-parroting bug at the prompt-template
level, but `gpt-4o-mini` still misjudges the **artifact kind** itself in
some cases — for example, treating a short commit message as a "PR
description" so the artifact-kind dispatch never fires. This is a
model-capability limitation, not a prompt bug. When dispatch accuracy
matters (e.g. for dogfood gates that route per `target_kind`),
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

### Retrospective: v3 → v4 regression justification (#199)

This block retroactively applies the template above to the v3 → v4 prompt
bump (PR #176, merged 2026-05-09), which was not accompanied by a
justification block at merge time.

**Data provenance.** Both baselines are recoverable from the Git history:

- v3 baseline: commit `405da56` (`feat(llm-rubric): target_kind annotation + v3 prompt (#169) (#174)`) — `tests/fixtures/semantic/baseline.json` at that ref.
- v4 baseline: commit `c11d525` (`feat(llm-rubric): v4 prompt grounds rule.target_kind verbatim (#175) (#176)`) — same path.  Identical to the current committed baseline (no subsequent baseline changes).

#### Aggregate accuracy

| Metric              | v3    | v4    | Delta  |
|---------------------|-------|-------|--------|
| Entries             | 26    | 28    | +2     |
| Correct             | 14    | 16    | +2     |
| Accuracy            | 53.8% | 57.1% | +3.3pp |
| Reproducibility avg | 100%  | 100%  | —      |
| Model               | `gpt-4o-mini` | `gpt-4o-mini` | — |

Aggregate accuracy improved by 3.3 percentage points.  The improvement is a
combined effect of corpus growth and genuine fixes; the analysis below
separates them.

#### New fixtures added in v4 (not present in v3)

| Fixture ID | Expected | Actual (v4) | Status |
|------------|----------|-------------|--------|
| `target-kind-mismatch-02-commit-rule-on-pr` | `unsupported` | `unsupported` | FAIL |
| `target-kind-mismatch-03-commit-rule-on-commit-no-mismatch` | `pass` | `pass` | PASS |

`target-kind-mismatch-02` exercises the inverse direction of the
artifact-kind dispatch (a `commit_message` rule evaluated against a PR-body
artifact).  It is counted as a FAIL because the v4 baseline captured a
`reproducibility: 0.0` result — the model's `unsupported` verdict was not
stable across the three reproducibility runs, indicating that artifact-kind
dispatch at `gpt-4o-mini` is still unreliable in this direction.  This is a
**known model-capability limitation**, not a prompt regression; see "Per-model
dispatch accuracy" above.

`target-kind-mismatch-03` is a positive grounding case (same-kind rule and
artifact must NOT trigger the dispatch) and passes cleanly.

**These two fixtures are intentional additions linked directly to the v4
fix** (PR #176 §"Regression fixtures added").  Their presence in the corpus
makes the aggregate count increase from 26 → 28 and accounts for +1 net
correct across the new entries (+1 PASS, +1 FAIL).

#### Status changes on shared fixtures (v3 → v4)

Three of the 26 fixtures present in both versions changed status.

**Fixed (FAIL → PASS)**

| Fixture ID | v3 status | v4 status | Root cause |
|-----------|-----------|-----------|------------|
| `clarity-03-rule-text-single-claim` | FAIL (`unsupported`, repro=0.0) | PASS (`pass`, repro=1.0) | v3 produced quote-fabrication rejections (`llm_quote_fabrication`) on this fixture with zero reproducibility; v4's tighter artifact-kind block stabilised the model output and grounding. |
| `consistency-02-doc-drifts-from-flag` | FAIL (`unsupported`, repro=0.0) | PASS (`fail`, repro=1.0) | Same root cause: v3 fabricated quotes on this fixture; v4 produces grounded quotes consistently. |

Both fixes are **directly attributable to the v4 prompt change** (the unsupported-example leak fix eliminated a prompt lure that destabilised quote grounding on unrelated fixtures).

**Regressed (PASS → FAIL)**

| Fixture ID | v3 status | v4 status | Root cause |
|-----------|-----------|-----------|------------|
| `justification-01-changelog-explains-why` | PASS (`pass`, repro=1.0) | FAIL (`unsupported`, repro=0.0) | In v3 the model returned a stable `pass` verdict; in v4 it returns an `unsupported` verdict with zero reproducibility — the judgment oscillates across the three reproducibility runs. |

**Analysis.** The v3 `pass` verdict on this fixture was a true positive (the
fixture's expected value is `pass`).  In v4 the model oscillates between
`pass` and `unsupported`, yielding a non-reproducible majority that the
bench harness records as FAIL.  The underlying rule (`justification` — a
changelog entry must explain the user-visible *why*) and the fixture artifact
are unchanged; the instability is a **model-variance interaction** with the
new artifact-kind prompt block, not a deliberate prompt regression.

gpt-4o-mini at the v4 prompt is susceptible to stochastic variability on
borderline artifacts — the PR #176 bench delta table (reproducibility N=3)
noted this fixture as "one stochastic flip on `justification-01-changelog-explains-why`
(PASS → FAIL on an `UNSPECIFIED` rule, unrelated to target_kind grounding)".
The rule has no `target_kind` annotation, so the artifact-kind block does not
fire; the regression is pure model noise at this token budget and temperature.

**Rule-level recommendation for the regressed fixture.**

Accept the regression for now.  The fixture is not a target-kind-mismatch
case, so it is not a test of the feature v4 added.  Options for a future
prompt iteration:

1. **Rewrite the fixture** to use a higher-contrast artifact (one where the
   changelog more unambiguously explains or omits the user-visible reason).
   This is the lowest-risk fix — the rule itself is sound.
2. **Increase reproducibility N** on this fixture in CI to reduce stochastic
   noise (trade: higher bench cost).
3. **Accept the regression** until a v5 prompt addresses broader accuracy on
   `justification` category fixtures; the `justification-02` through
   `justification-04` fixtures are unaffected.

The regression is a net loss of 1 fixture against a net gain of 2 (the two
fixes above) on shared-corpus entries, plus +1 new PASS from corpus growth.
On a 28-entry corpus with gpt-4o-mini at temperature default, a ±1 entry
swing is within the expected model-variance noise band (approximately ±3.6%
per flip).

#### Net verdict

The v3 → v4 transition is **accepted**.

- Accuracy improved +3.3pp (53.8% → 57.1%) on a comparable corpus.
- The two fixes (`clarity-03`, `consistency-02`) are prompt-induced and
  directly attributable to eliminating the unsupported-example quote-lure.
- The single regression (`justification-01`) is model-variance noise at
  gpt-4o-mini temperature, unrelated to the target_kind feature.
- The two new fixtures are intentional and both correct the coverage gap that
  PR #175 addressed.

**Future prompt bumps must attach this justification block at PR merge time**,
not retroactively.  The template in "Regression tolerance and justification
template" above is the required format; attach it under a
`## Prompt regression justification` header in the PR description, referencing
the prior-version baseline commit SHA for reproducibility.

## Adaptive strategy escalation (`strategy=adaptive`)

The adaptive strategy runs a Tier 1 single-judge call and escalates to a
multi-call Tier 2 strategy only when the Tier 1 outcome is ambiguous. Two
escalation triggers exist:

| Tier 1 evidence kind        | Default behaviour              | Tier 2 strategy | Opt-in?                                                     | `adaptive_escalation_reason`     |
|-----------------------------|--------------------------------|-----------------|-------------------------------------------------------------|----------------------------------|
| `target_kind_mismatch`      | escalate (always)              | `consensus` (3) | n/a                                                         | `"tier1_unsupported"`            |
| `llm_quote_fabrication`     | commit Tier 1 UNSUPPORTED      | `review` (2)    | `params.adaptive_escalate_on_quote_fabrication: true` (#254) | `"tier1_quote_fabrication"`      |
| `provider_error` / `provider_unconfigured` / `strategy_unavailable` | fail-closed at Tier 1 | n/a             | n/a                                                         | `null`                           |
| `llm_judgment` (`pass` / `fail`) | commit Tier 1 verdict     | n/a             | n/a                                                         | `null`                           |

### Quote-fabrication recovery (`adaptive_escalate_on_quote_fabrication`, #254)

Default: `false`. When a rule sets this param true and the Tier 1 single
judge returns `llm_quote_fabrication` (one or more supporting quotes are
not substrings of the artifact), the adaptive strategy escalates to
`review` — a fresh primary + reviewer pair — instead of committing the
Tier 1 UNSUPPORTED verdict. The reviewer pass *still* runs every quote
through `_find_fabricated_quotes`; this path does not weaken the substring
contract. Three outcomes are possible:

- **Tier 2 grounded verdict (recovery succeeds).** Diagnostic carries
  `status=pass|fail`, `evidence[0].kind == "llm_adaptive"`,
  `adaptive_tier == 2`,
  `adaptive_escalation_reason == "tier1_quote_fabrication"`,
  `adaptive_recovery_outcome == "tier2_recovered"`, and the original
  Tier 1 fabrication evidence nested under
  `tier1_evidence = {"kind": "llm_quote_fabrication", "data": {...}}`.
- **Tier 2 also UNSUPPORTED (recovery fails).** Diagnostic propagates the
  *Tier 1* UNSUPPORTED with `evidence[0].kind == "llm_adaptive"`,
  `adaptive_recovery_outcome == "tier2_unsupported"`, and both
  `tier1_evidence` and `tier2_evidence` nested so the auditor can see the
  failure on both passes. No ungrounded verdict is ever emitted.
- **Param absent or false.** Behaviour is identical to today — Tier 1
  fabrication commits directly with `adaptive_tier == 1`,
  `adaptive_escalation_reason == null`.

**Cost note.** Enabling the param can roughly 3× provider calls on the
affected fraction of rules (1 fabricated Tier 1 + 2 review-pass Tier 2 =
3 calls per fabrication event). Aggregated `llm_call_count`,
`cost_estimate_usd_total`, and `latency_ms_total` in `evidence[0].data`
reflect the combined tier totals. Promote per-rule only after measuring
the fabrication rate against the rule's baseline; do not flip on at the
profile or project level.
