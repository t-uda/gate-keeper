# Local-LLM Strategy Profile Contract

**Issue:** #188 (under umbrella #164)
**Status:** Design spec — no runtime implementation.

---

## 1. Motivation

Consensus and reviewer strategies (#184, #185) multiply provider calls, which raises
cost on paid remote APIs. Local LLMs hosted via HTTP or CLI are free at the margin, so
the same strategies become cost-effective. This document defines the configuration
contract that lets future local-provider implementations plug into the same
`Strategy` / `StrategyTelemetry` abstraction introduced by #183, and how those
profiles connect to the model matrix (#177) for comparison with remote baselines.

The practical targets are lab/server deployments, private workflows, teaching/seminar
use, and environments where remote API spend must be minimised.

**Out of scope here:** implementing Ollama transport, GPU detection, local HTTP
routing, runtime consensus execution, or local CLI subprocess integration. Those are
implementation issues to be opened after this contract is accepted.

---

## 2. Profile YAML shape

Profiles are declared in a `llm_profiles` mapping in the project config file
(the exact config file location and load-path are TBD — see §7).

```yaml
llm_profiles:
  # A three-judge local-HTTP consensus profile.
  local_consensus:
    provider_kind: local_http           # required; see §3
    endpoint: "http://localhost:11434"  # required for local_http
    model: "gemma3:4b"                  # required
    strategy: consensus                 # required; one of single|consensus|review|adaptive
    panel_size: 3                       # optional; strategy-specific (default: 3 for consensus)
    reviewer: false                     # optional; adds a reviewer pass after consensus
    budget_usd_per_run: 0.0             # optional; 0 = no remote cost (local provider)
    max_latency_seconds: 120            # optional; advisory, surfaced in telemetry
    blocking_allowed: false             # optional; default false for local profiles (§5.3)
    telemetry:
      token_counts: optional            # provider may not report token usage
      latency_required: true            # latency_ms_total must appear in evidence

  # A single-judge local-CLI profile for Ollama or similar tool wrappers.
  local_single_cli:
    provider_kind: local_cli            # required
    command: ["ollama", "run", "llama3.2:3b"]  # required for local_cli
    model: "llama3.2:3b"               # required; used for evidence labelling
    strategy: single                    # single is always available
    budget_usd_per_run: 0.0
    max_latency_seconds: 60
    blocking_allowed: false
    telemetry:
      token_counts: optional
      latency_required: true

  # A fake profile for unit testing and CI without a real provider.
  fake_judge:
    provider_kind: fake                 # required; always available without external deps
    model: "fake-model-v1"             # required; arbitrary label
    strategy: single
    budget_usd_per_run: 0.0
    blocking_allowed: false
    telemetry:
      token_counts: optional
      latency_required: false
```

---

## 3. Required and optional fields

### 3.1 Provider-kind (`provider_kind`)

`provider_kind` identifies the **transport layer**, not the strategy. It is orthogonal
to `strategy`.

| `provider_kind` | Description | Extra required fields |
|---|---|---|
| `local_http` | OpenAI-compatible HTTP endpoint (Ollama, LM Studio, vLLM) | `endpoint` |
| `local_cli` | Subprocess CLI tool that reads prompt from stdin / args | `command` (list) |
| `fake` | In-process deterministic stub for tests; no network or subprocess | none |

The existing remote kinds (`anthropic`, `openai`) are not `provider_kind` values —
they are resolved from the dotenv credential file as today (#51). Local profiles
bypass the dotenv path entirely: no API key, no credential file, no pricing lookup.

### 3.2 Strategy id (`strategy`)

`strategy` identifies the **judgment algorithm**, not the transport. It maps directly
to the `KNOWN_STRATEGIES` registry introduced by #183:

| `strategy` | Description | Status |
|---|---|---|
| `single` | One provider call, one judgment. Current default. | Concrete (#183) |
| `consensus` | `panel_size` independent calls, deterministic aggregation. | Reserved (#184) |
| `review` | One base judge call, then one reviewer audit call. | Reserved (#185) |
| `adaptive` | Start cheap, escalate on fabrication/disagreement/failure. | Reserved (#186) |

Invoking `consensus`, `review`, or `adaptive` before their implementation issues land
returns `UNAVAILABLE` + `strategy_not_implemented` evidence (the fail-closed placeholder
wired by PR #197). A local profile with `strategy: consensus` is therefore valid
configuration that will fail-closed until #184 lands — it does not silently fall back
to `single`.

### 3.3 Strategy-specific optional fields

| Field | Applies to | Default | Description |
|---|---|---|---|
| `panel_size` | `consensus` | 3 | Number of independent judges in the panel. |
| `reviewer` | `consensus`, `adaptive` | false | Add a reviewer audit pass after aggregation. |
| `max_latency_seconds` | all | none | Advisory cap; surfaced in telemetry only. |
| `budget_usd_per_run` | all | none | Budget cap; for local providers set to `0.0`. |
| `blocking_allowed` | all | false | Whether this profile may be used in a required (non-advisory) rule. |

### 3.4 Telemetry sub-object

```yaml
telemetry:
  token_counts: optional | required | omit
  latency_required: true | false
```

- `token_counts: optional` — emit `tokens_in` / `tokens_out` when the provider
  reports them; do not fail when absent.
- `token_counts: required` — fail with `provider_error` / `missing_token_counts` when
  the provider does not report usage.
- `token_counts: omit` — never record token counts even if available.
- `latency_required: true` — always record `latency_ms_total` in evidence; fail if
  the implementation cannot measure it.

Default for local profiles: `token_counts: optional`, `latency_required: true`.

---

## 4. Evidence fields for local-provider calls

### 4.1 `llm_judgment` evidence (success path)

Local-provider calls produce the same `llm_judgment` evidence shape as remote
providers. Additions from the `strategy_meta` block introduced by #183:

```json
{
  "kind": "llm_judgment",
  "data": {
    "model":                       "gemma3:4b",
    "provider_kind":               "local_http",
    "profile_id":                  "local_consensus",
    "prompt_version":              "v4",
    "judgment":                    "pass",
    "primary_reason":              "...",
    "supporting_evidence_quotes":  ["..."],
    "supporting_evidence_spans":   [{"quote": "...", "start_offset": 14, ...}],
    "suggested_action":            null,
    "strategy_meta": {
      "strategy":                  "consensus",
      "call_count":                3,
      "models":                    ["gemma3:4b", "gemma3:4b", "gemma3:4b"],
      "cost_estimate_usd_total":   0.0,
      "latency_ms_total":          4800
    },
    "tokens_in":                   null,
    "tokens_out":                  null,
    "latency_ms":                  1640,
    "cost_estimate_usd":           null
  }
}
```

New fields compared to the remote `llm_judgment` schema:

| Field | Type | Notes |
|---|---|---|
| `provider_kind` | string | `local_http` / `local_cli` / `fake` |
| `profile_id` | string | The key from `llm_profiles` that resolved this call |
| `strategy_meta.cost_estimate_usd_total` | float or null | See §4.2 |

### 4.2 Cost representation for local providers

The existing remote path records `cost_estimate_usd: null` when the model is not in
`_MODEL_PRICING` (fail-closed: unknown cost is not guessed). For local providers:

- **`cost_estimate_usd: null`** — do not record per-call cost for local providers.
  Local models have no canonical per-token price. Setting to `null` preserves
  fail-closed semantics rather than guessing.
- **`strategy_meta.cost_estimate_usd_total: 0.0`** — the profile explicitly declares
  `budget_usd_per_run: 0.0`, meaning no remote API spend. The total cost is
  **exactly zero**, not unknown. Use `0.0` (float), not `null`, when the profile
  carries `budget_usd_per_run: 0.0`. This distinguishes "no remote spend" from
  "cost unknown".

Stated precisely:

| Scenario | `cost_estimate_usd` (per call) | `cost_estimate_usd_total` (aggregate) |
|---|---|---|
| Remote provider, known model | float (computed) | sum of per-call floats |
| Remote provider, unknown model | `null` | `null` (any null propagates) |
| Local provider, `budget_usd_per_run: 0.0` | `null` | `0.0` |
| Local provider, `budget_usd_per_run` unset | `null` | `null` |

The `StrategyTelemetry` docstring from PR #197 states: "`cost_estimate_usd_total` is
`None` iff any individual call had unknown pricing." For local profiles, the canonical
approach is to declare `budget_usd_per_run: 0.0` explicitly and have the loader inject
a `0.0` aggregate — not derived from individual call pricing — before constructing
`StrategyTelemetry`. The loader sets `cost_estimate_usd_total = 0.0` when the profile
declares `budget_usd_per_run == 0.0`; individual per-call `cost_estimate_usd` fields
remain `null`.

### 4.3 Multi-call aggregation (consensus / review)

When `strategy: consensus` with `panel_size: 3`:

- `call_count: 3`
- `latency_ms_total`: wall-clock sum across all three calls (or parallel wall-clock if
  calls are concurrent — implementation decides; must document which)
- `cost_estimate_usd_total: 0.0` (from `budget_usd_per_run: 0.0`)
- `models`: list of three model ids (may all be identical)

Sum-of-zeros is `0.0`, not `null`. The null-propagation rule from `StrategyTelemetry`
applies to unknown-pricing calls only; explicit `0.0` declarations short-circuit it.

### 4.4 Fabrication and disagreement in multi-call local paths

The parser-side substring grounding check (#172) runs on every individual call's
output, regardless of provider kind. A local model that fabricates quotes produces
`llm_quote_fabrication` evidence the same way a remote model does. In consensus mode,
quote fabrication on one panel member should not silently count toward the majority —
issue #184 defines the aggregation semantics, and local profiles must honour them.

---

## 5. Connection to consensus / review / adaptive strategies

### 5.1 How `JudgmentRequest` flows to local providers

`JudgmentRequest` (from #183) carries `rule`, `target`, and `artifact_kind`. A future
local-provider implementation adds `profile` (resolved `LocalProfile` object) to the
request, or resolves the profile in the strategy factory before constructing the
request. The `Strategy` Protocol's `__call__(request: JudgmentRequest) -> Diagnostic`
signature remains unchanged — profile resolution happens in the factory, not the
protocol.

The dispatch path in `check()`:

```
check(rule, target, artifact_kind)
  → resolve strategy_id from rule.params["strategy"] (default: "single")
  → resolve profile_id from rule.params["profile"] or global default
  → look up strategy in _STRATEGIES[strategy_id]
  → construct JudgmentRequest(rule, target, artifact_kind)
  → strategy(request) → Diagnostic
```

### 5.2 Consensus with local provider

```yaml
local_consensus:
  provider_kind: local_http
  endpoint: "http://localhost:11434"
  model: "gemma3:4b"
  strategy: consensus
  panel_size: 3
  budget_usd_per_run: 0.0
```

When `strategy: consensus` lands (#184), the strategy receives the `JudgmentRequest`
and uses the profile to make `panel_size` calls. Each call goes through the same
`_call_local_http` helper (to be implemented), the same JSON parser, and the same
quote-grounding validator. The `StrategyTelemetry` aggregates across calls:
`call_count=3`, `cost_estimate_usd_total=0.0`, `latency_ms_total=sum(call latencies)`.

### 5.3 Reviewer with local provider

```yaml
local_review:
  provider_kind: local_http
  endpoint: "http://localhost:11434"
  model: "gemma3:4b"
  strategy: review
  budget_usd_per_run: 0.0
```

When `strategy: review` lands (#185), two calls are made: base judge and reviewer.
`call_count=2`, `cost_estimate_usd_total=0.0`, `latency_ms_total=sum(both latencies)`.

### 5.4 `blocking_allowed: false`

Local profiles carry `blocking_allowed: false` by default. This mirrors the current
advisory posture of `llm-rubric` (docs/llm-rubric.md: "LLM-evaluated rules are
advisory evidence, not authoritative gates"). An implementation that encounters a
`semantic_rubric` rule with `severity: error` paired with a local profile that has
`blocking_allowed: false` should either:
(a) downgrade severity to `advisory` for that rule, or
(b) return `UNAVAILABLE` with a `blocking_disallowed` evidence kind.

Option (b) is preferred — it is fail-closed and makes the misconfiguration explicit.

### 5.5 Connection to model matrix (#177)

The model matrix bench harness (#195) runs a rule/artifact fixture set against
multiple provider configurations and compares accuracy, cost, and latency. Local
profiles should be representable as bench configurations so `uv run gate-keeper bench`
can compare:

- `gpt-4o-mini` single (remote baseline)
- `gemma3:4b` single (local)
- `gemma3:4b` consensus `panel_size=3` (local multi-call)

This requires the bench harness to accept a `--profile` flag or a bench-config YAML
that names a profile. The design for that flag is out of scope for this issue but
should be opened as part of the #177 matrix work.

---

## 6. Implementation sequence

The smallest viable sequence for a future implementation issue:

### Slice (a): provider-kind plumbing

Open a new issue to add `local_http` transport:

1. `_call_local_http(endpoint, model, system, user) -> str` — POST to
   `{endpoint}/v1/chat/completions` (OpenAI-compatible schema); parse `choices[0].message.content`.
2. No token-count guarantee — parse `usage` when present, tolerate absence.
3. No pricing table entry — `cost_estimate_usd` is always `null` from this helper.
4. Unit tests with a monkeypatched HTTP response (no real Ollama required).

`local_cli` transport can follow in a separate slice.

### Slice (b): profile loader

1. Define `LocalProfile` dataclass (fields from §3).
2. Add `_load_profiles(config_path: Path) -> dict[str, LocalProfile]` — reads
   `llm_profiles` from a YAML config file, validates required fields, raises on
   missing `provider_kind` or `model`.
3. Wire into `check()` via a new `rule.params["profile"]` key that the classifier
   can inject, or a top-level config default.

### Slice (c): consensus strategy concrete implementation (#184)

1. `_run_consensus_strategy(request: JudgmentRequest) -> Diagnostic`
2. Calls the provider `panel_size` times (sequential or concurrent — document
   the choice in evidence).
3. Aggregation logic: unanimous → final verdict; any fabrication → unsupported;
   disagreement → unsupported + `llm_consensus_disagreement`.
4. Constructs `StrategyTelemetry` with `call_count=panel_size`, aggregated latency,
   `cost_estimate_usd_total` from profile declaration.
5. Register in `_STRATEGIES["consensus"]` to replace the reserved placeholder.

### Slice (d): reviewer strategy (#185)

1. `_run_review_strategy(request: JudgmentRequest) -> Diagnostic`
2. Base judge call → parse → pass to reviewer prompt.
3. Reviewer structured audit (see #185 proposed shape).
4. `StrategyTelemetry` with `call_count=2`.
5. Register in `_STRATEGIES["review"]`.

---

## 7. Open questions and decision points

The following are genuine ambiguities from reading the current code and issue history.
They are not resolved by this document.

**Q1. Config file location and load order.**
There is no project-level config file today. Where does `llm_profiles:` live?
Candidates: `gate-keeper.yml` at project root, a `[tool.gate-keeper]` table in
`pyproject.toml`, or an explicit `--config` CLI flag. The dotenv path is
host-scoped; profiles are likely project-scoped. This needs a decision before the
profile loader (slice b) can be spec'd.

**Q2. How does a rule reference a profile?**
Two options: (a) `rule.params["profile"]` declared per-rule in the rule document
(e.g. `[profile: local_consensus]` annotation), or (b) a global default profile
selected at `gate-keeper validate` invocation time via a flag or config key.
Option (b) is simpler for the first implementation but prevents per-rule profile
selection that the model matrix (#177) may need.

**Q3. `cost_estimate_usd_total: 0.0` vs `null` for unbudgeted local profiles.**
This document recommends `0.0` when `budget_usd_per_run: 0.0` is explicitly set,
and `null` when the field is absent. If the profile loader treats an absent
`budget_usd_per_run` as "unknown cost" then some local deployments will show `null`
totals even though their real cost is zero. The owner should decide whether the
absence means "unknown" (fail-closed) or "zero by convention for local kinds".

**Q4. Sequential vs concurrent panel calls.**
Consensus mode (#184) can call judges sequentially or concurrently. Concurrent
calls reduce wall-clock latency but complicate error handling (one call fails mid-
flight). The choice affects `latency_ms_total` semantics (wall-clock vs sum).
This document defers to the #184 implementation issue.

**Q5. `local_cli` command timeout and error mapping.**
CLI-subprocess calls can hang or produce non-zero exit codes. The `external` backend
(docs/backend-external.md) has a `timeout_seconds` param and maps timeouts to
`cli_timeout` evidence. Local CLI profiles should reuse the same timeout/error
vocabulary — but the exact mapping needs to be specified in the slice (a) issue.

**Q6. Bench harness integration.**
The `--profile` flag or bench-config YAML for the model matrix (#177) is not
designed in this document. The bench harness introduced in #195 accepts provider
configuration via the existing dotenv path. Whether local profiles should be
activated via a separate bench flag or a new bench-config YAML is a design decision
for the #177 matrix work.
