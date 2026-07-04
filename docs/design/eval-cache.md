# Design: Content-Addressed Local Evaluation Cache

> Status: internal process artifact — not user-facing reference.

Tracking issue: #69 (slice B). Cost lever for umbrella #277 (incremental audit
trunk). Design phase only — this note ratifies the cache key schema and storage
contract **before** implementation, per the two-slice plan on #69. Slice A
(`--deterministic` sampling mode) ships separately and is not gated on this note.

> **Ratification scope.** This note settles the load-bearing decisions for the
> #69 slice-B eval cache: the cache key preimage (§3), the post-aggregation
> cache boundary (§2.1 — the decision the rest of the design hangs on), the
> on-disk entry schema and its versioning (§4), storage atomicity without
> locking (§5), hit semantics (§6), what is and is not cacheable (§7), and
> rollout / eviction (§8). Implementation lands in a follow-up PR under #69.

---

## 1. Problem Statement

`gate-keeper validate` pays full LLM cost on every run. Under umbrella #277 the
common case becomes re-auditing a repository whose content barely moved: a
`--target-changed` run (S1, #278) narrows the candidate set, and per-rule scope
(S3, #279) narrows further, but every surviving `(rule, target)` pair still
issues fresh provider calls even when the rule text, the target content, the
model, and the prompt are byte-identical to a prior run. That is redundant
spend on a deterministic-in-practice question.

An incremental build system answers this with a content-addressed cache: derive
a key from everything that determines the output, and on a key hit return the
prior output without recomputing. The judgment a semantic-rubric rule produces
is a pure function of a bounded, enumerable set of inputs (§3). When that set is
unchanged, the prior verdict is reusable. This note pins the key so that a hit
is provably a hit and a miss is fail-open to recomputation — never a wrong-prompt
result served silently.

The cache multiplies S1/S3/S5 savings: those stages shrink *which* pairs are
evaluated; the cache removes provider calls for pairs whose inputs repeat across
runs (re-runs after an unrelated change, CI retries, multi-shard fan-out).

---

## 2. Settled Rules

The rules below are load-bearing. Slice B honours them. Later work may extend
but must not contradict them.

### 2.1 Cache the post-aggregation result only

**Decision: the cache unit is the single aggregated `Diagnostic` the validator
produces for one `(rule, target)` pair — after strategy aggregation *and* after
`--reproducibility` aggregation. The cache never memoizes an individual provider
call or an individual `_run_n` iteration.**

This is the decision the rest of the design depends on. Caching below the
aggregation boundary would fake the very independence that the aggregation
machinery exists to measure:

- **Reproducibility (`validator._run_n`).** `--reproducibility N` runs the check
  `N` times and majority-votes to *measure* run-to-run stability, emitting a
  `reproducibility_score` evidence record. If individual calls were cached, all
  `N` iterations would hit one cached call and the score would be a constant
  `1.0` — the metric would report perfect stability precisely because the cache
  fabricated it. The measurement would be destroyed by the optimization meant to
  accelerate it.
- **Consensus / review strategies (#184 / #185).** These panel multiple
  independent judgments and aggregate them. A per-call cache collapses the panel
  to a single reused voice, again fabricating agreement.
- **Adaptive strategy (#186 / #254).** Tier 1 runs a single judge; escalation to
  a Tier 2 panel fires only on data-dependent Tier-1 outcomes
  (`target_kind_mismatch`, or `llm_quote_fabrication` when
  `params.adaptive_escalate_on_quote_fabrication` is set). The call count is a
  function of the data, so there is no stable per-call boundary to key against in
  the first place.

The coherent boundary is therefore *above* both aggregations. Concretely, the
cache wraps the validator's per-rule dispatch seam — the point that today
chooses between `_run_n(check_fn, …)` (when `reproducibility > 1`) and a single
`check(rule, target)` — not `check()` itself and not the provider-call helpers
inside a strategy. On a cold key the full genuine computation runs (all `N`
iterations, all panel calls, honest reproducibility measurement) and its final
aggregated `Diagnostic` is stored. On a warm key that stored aggregate is
returned with zero provider calls. The cache memoizes an honest measurement; it
never lets one call masquerade as many.

**Corollary.** Because the cached artifact is the aggregate *over `N`*, the
reproducibility count `N` is part of the key (§3). An `N=1` result must never be
served to an `N=5` request, and vice versa.

### 2.2 The key is content-addressed over every determinant

The key is `sha256` over the canonical preimage in §3. Every component is an
input the judgment genuinely depends on; omitting any one lets the cache serve a
result computed for different inputs. Two omissions are called out because they
are silent:

- **`artifact_kind` (#191).** `PROMPT_VERSION` was deliberately *not* bumped for
  #191 (see `llm_rubric.py` history comment above `PROMPT_VERSION`): the template
  body is unchanged, but when the caller declares `--artifact-kind` the `Target
  reference` slot is filled with the file *content* instead of the file *path
  string*. Two evaluations of the same file at the same `PROMPT_VERSION` (v7)
  therefore send materially different prompts depending on `artifact_kind`.
  Omitting it from the key would serve a path-rendered verdict to a
  content-rendered request. It must be a distinct key component.
- **`strategy`.** `single` and `consensus` produce different judgment processes
  and different evidence shapes from the same rule and target. A key that
  ignored strategy would serve a single-judge verdict to a consensus request.

### 2.3 Cache miss and corruption are fail-open to recomputation

A cache is a cost optimization, not an evidence source. A miss, an unknown entry
schema version (§4), malformed JSON, or a `Diagnostic.from_dict` failure all
resolve to **recompute the real evaluation** and (on success) overwrite the
entry. None of these ever raises, and none ever collapses to `pass`.

This is deliberately the *opposite* polarity from the project's fail-closed
evidence rule. Fail-closed applies to missing *evidence*: a rule with no
grounding must not pass. A missing *cache entry* is not missing evidence — it
means the real, fully-grounded evaluation simply has to run. Conflating the two
would either break validation on a corrupt cache file or, worse, treat a cache
miss as a passing verdict. Neither is acceptable; recomputation is the only
correct response to a cache that cannot answer.

### 2.4 Only stable verdicts are written

The cache stores an entry only when the aggregated status is `PASS` or `FAIL`.
Transient and configuration states — `UNAVAILABLE`, `ERROR`,
`provider_error`, `provider_unconfigured` — are never written, so a flaky
provider or a missing key cannot poison the cache with a failure that a later
healthy run would keep serving. This mirrors `_run_n`, which returns early on the
first non-`pass`/`fail` diagnostic without synthesising a score. Deterministic
pre-provider outcomes (`multi_target_unsupported`, the #178
`target_kind_mismatch` precheck) cost no provider call, so caching them buys
nothing and they are left out of slice B; see §9.

### 2.5 Rule identity is rehydrated on hit, not keyed

**Decision: `rule_id`, `source`, and `severity` are excluded from the key (§3)
and are re-applied from the *current* rule when a cached diagnostic is returned.
The cache keys on what the model judges (predicate, params, target, model,
prompt); those three fields are rule-identity / reporting metadata that do not
change the judgment.**

The judgment a semantic-rubric evaluation produces is a pure function of the
rule *predicate* (text, kind, prompt-shaping params) and the model inputs — not
of which `rule_id` carries that predicate, where in the source it sits, or what
`severity` the author assigned. Two rules with byte-identical predicates
evaluated against the same target, model, and prompt therefore produce the same
judgment, and reusing one across the other is a legitimate, desirable hit.

But the stored `Diagnostic` (§4) carries `rule_id`, `source`, and `severity`
verbatim. If those fields were simply replayed, a hit for rule *B* keyed on a
predicate first computed for rule *A* would return *A*'s id, source location, and
severity — corrupting *B*'s report entry. Two safe resolutions exist; this design
takes the second because it preserves cross-rule reuse:

1. Fold `rule_id` / `source` / `severity` into the key. Correct, but it splits
   the cache per rule even when the judgment is identical, discarding reuse
   between duplicated-predicate rules.
2. **Store the judgment and rehydrate identity on hit.** On a hit the engine
   reconstructs the diagnostic from the cached payload and overrides `rule_id`,
   `source`, and `severity` with the current rule's values (a
   `dataclasses.replace`). `status`, `message`, `evidence`, and `remediation` —
   all predicate-derived — come from the cache unchanged. No evidence field
   embeds rule identity, so overriding the three top-level fields is sufficient.

This is why the `rule_content_hash` component (§3) hashes only
`{text, kind, target_kind, params∖{…}}` and deliberately omits `rule_id`,
`source`, and `severity`.

---

## 3. Cache Key Schema

The key is `sha256` of a canonical, sorted-key JSON serialization of the
preimage below (UTF-8, `ensure_ascii=false`, `sort_keys=true`, no insignificant
whitespace). The hex digest is the cache filename (§5). Storing the digest over a
structured preimage — rather than concatenating strings — avoids delimiter
ambiguity between components.

| Component | Source | Normalization | Why it is in the key |
|-----------|--------|---------------|----------------------|
| `prompt_version` | `PROMPT_VERSION` constant (`v7`) | verbatim string | The template body defines the question asked. v5→v6→v7 are not interchangeable (multi-target attribution, line-range evidence); a bump must miss all prior entries. |
| `provider` | `GATE_KEEPER_LLM_PROVIDER` (dotenv) | lowercased token | Same logical model name can resolve differently per provider; the provider selects the call path (`_call_anthropic` vs `_call_openai`). |
| `resolved_model_id` | `_resolve_model(provider, env)` output | the **resolved** id, not the raw override | Judgments differ by model. Keying the resolved id (post default-fallback / strict-mode resolution) means a blank override and its default constant collide correctly, and a model swap misses. |
| `rule_content_hash` | `sha256` over canonical `{text, kind, target_kind, params∖{strategy, adaptive_*, targets}}` | sorted-key JSON of the rule predicate | The rule predicate and its prompt-shaping params determine the judgment. Params broken out below are excluded to avoid double-counting; `rule_id` / `source` / `severity` are excluded and rehydrated on hit (§2.5). |
| `targets` | resolved artifact(s) | see §3.1 | Editing a target must invalidate; the rendered path is part of the prompt and evidence; multi-target order and per-entry identity are load-bearing. |
| `strategy` | `_resolve_strategy_id(rule)` + strategy-shaping params (`adaptive_escalate_on_quote_fabrication`, …) | resolved id (default `single`) plus a sorted sub-map of shaping params | `single`/`consensus`/`review`/`adaptive` and their escalation switches produce different processes and evidence. |
| `sampling` | effective sampling descriptor after slice-A capability resolution | canonical map, e.g. `{"temperature": 0}` or `{"mode": "provider_default"}` | Deterministic vs default sampling draw from different distributions; the *effective* descriptor (post capability-check, R10) is what was actually sent. |
| `artifact_kind` | `--artifact-kind` / rule dispatch | enum value, `None`→`"unspecified"` | #191 changes path-vs-content rendering **without** a `PROMPT_VERSION` bump (§2.2). Distinct component or the cache serves the wrong rendering. |
| `reproducibility_n` | validator `--reproducibility` | integer ≥ 1 | The cached artifact is the aggregate over `N` (§2.1 corollary); it is specific to `N`. |

### 3.1 Target hashing

`targets` is a list, in prompt-assembly order, of one entry per resolved
artifact:

Each entry carries **both** the rendered path and a content hash, because the v7
prompt renders both. `content_sha256` hashes the *rendered artifact text* — the
exact body `_render_numbered_artifact_text` placed under the `Path:` line, i.e.
what the model actually read (file content on the `--artifact-kind` /
incremental-audit path, where an edit to the file changes that body and
invalidates the entry). `path` is included because it is itself part of the
model-facing output, not just a lookup handle: `_render_numbered_artifact_text`
prepends a `Path: <path>` line to the numbered block, and the v7
`supporting_evidence_refs` evidence records a `path` field per reference. Two
files with identical bytes at different paths therefore send different prompts
*and* produce different evidence. Keying content alone would let a verdict and
evidence computed for `src/foo.py` be served for `tests/foo.py` with the same
bytes. Both components are needed: content guards edits, path guards renames /
distinct-path collisions.

- **Single-target rule.** One entry `{"id": null, "path": <rendered-path-or-null>,
  "content_sha256": <hex>}`. `content_sha256` hashes the *resolved artifact
  bytes* — the exact text `_resolve_artifact_input` fed to the model and to the
  substring-grounding check. `path` is the same string the prompt renders in its
  `Path:` line (the resolved target path, or `null` for an inline / path-less
  artifact). A target that resolves to no readable body (unresolvable PR ref,
  missing file) is not cacheable: the evaluation fails-closed to `UNAVAILABLE`
  and §2.4 keeps it out of the cache.

- **Multi-target rule (`params.targets`, #182).** One entry per declared spec,
  in list order, `{"id": <spec.id>, "kind": <spec.kind>, "path":
  <spec.path-or-null>, "content_sha256": <hex>}`. Entry `id`, `path`, and order
  are included because the v6 prompt renders a `Path:` line per artifact and
  attributes supporting quotes to specific `target_id`s in assembly order;
  reordering, renaming, or repathing entries changes the model-facing prompt and
  the evidence contract even with identical file contents. The
  `_parse_multi_targets` cap of 5 bounds this list.

### 3.2 Worked negative cases

Each row is a request that **must not** hit an entry produced by the row above
it, and the component that prevents it:

| First run | Second run | Guarding component |
|-----------|-----------|--------------------|
| audit `foo.py` (content A) | `foo.py` edited to content B | `targets[].content_sha256` |
| audit `src/foo.py` | audit `tests/foo.py`, identical bytes | `targets[].path` |
| `--artifact-kind code` on `foo.py` | no `--artifact-kind` on `foo.py` | `artifact_kind` |
| `strategy: single` | `strategy: consensus` | `strategy` |
| `--reproducibility 1` | `--reproducibility 5` | `reproducibility_n` |
| default model | `GATE_KEEPER_ANTHROPIC_MODEL` override | `resolved_model_id` |
| prompt `v7` | future `v8` | `prompt_version` |
| multi-target `[a, b]` | multi-target `[b, a]` | `targets` order |

Note the deliberate *non*-guard: two rules with byte-identical predicates but
different `rule_id` / `source` / `severity` **do** share a key — that is the
intended reuse. Correct per-rule metadata on the report is preserved by
rehydration on hit (§2.5), not by key separation.

---

## 4. Entry Schema and Versioning

One JSON file per key (§5). Shape:

```json
{
  "cache_schema_version": 1,
  "key": "<hex sha256 — matches filename>",
  "created_at": "<ISO-8601 UTC timestamp of first computation>",
  "prompt_version": "v7",
  "key_preimage": { "...": "the §3 components, for debuggability" },
  "diagnostic": { "...": "Diagnostic.to_dict() output" }
}
```

- **`cache_schema_version`** is an integer. On read, any value the running code
  does not recognise is treated as a **miss** (recompute + overwrite), never an
  error (§2.3). This is how the entry format evolves without a migration step:
  bump the version and all prior entries silently miss.
- **`diagnostic`** must round-trip `Diagnostic.from_dict(entry["diagnostic"])`
  without a schema break. `Diagnostic`/`Evidence` serialize losslessly through
  `to_dict`/`from_dict` (evidence is a plain `{kind, data}` list; `remediation`
  is optional). A `from_dict` failure is a corrupt entry → miss (§2.3). The
  cached `Diagnostic` is stored **before** the §6 cache-hit marker is attached,
  so the stored artifact is the genuine evaluation result and the hit marker is
  added fresh on each hit. Its `rule_id` / `source` / `severity` fields are the
  producing rule's; on a hit they are overridden with the *current* rule's values
  (§2.5) so a duplicated-predicate reuse does not leak the producer's identity.
- **`created_at`** is the first-computation timestamp. `Diagnostic` carries no
  timestamp field, so the entry supplies the "original evaluation time" surfaced
  on a hit (§6).
- **`key_preimage`** stores the §3 component values for debugging and collision
  triage. It is advisory: reads key on the filename digest, not on re-deriving
  from the preimage.

---

## 5. Storage and Atomicity

**Location.** `.gate-keeper/cache/eval/<hex-digest>.json`, one file per key,
under the working-directory `.gate-keeper/` tree that already holds
`dependency-manifest.yml` and `acks/`. Per-working-directory, git-ignored, never
shared (distributed cache is out of scope — §9, matching #69).

**Write path — atomic tmp + rename, no lock.** Write the entry to a temporary
file in the *same* directory, then `os.replace(tmp, final)`. `os.replace` is
atomic on POSIX within one filesystem, so a reader ever sees either the old
entry or the complete new entry — never a torn write. There is **no lockfile.**

The trade-off is deliberate (R11): **racing double-compute is tolerated;
corruption is not.** Two processes that miss the same key concurrently — realistic
now that dispatch is bounded-concurrent (#249, `ThreadPoolExecutor`) and CI may
fan out across shards — both compute and both rename. The loser wasted provider
calls, but both wrote the same logical verdict under the same key, and the atomic
rename guarantees whichever file wins is internally complete. A lock would trade
those occasional wasted calls for cross-process lock-file lifecycle complexity
(stale locks, crash recovery) that a fail-open optimization does not warrant.
Wasted calls are bounded and self-limiting; a corrupt half-written entry is not,
and the atomic rename rules it out.

---

## 6. Hit Semantics

On a key hit with a stable stored verdict:

- **Zero provider calls.** The stored `Diagnostic` is reconstructed and returned;
  no strategy runs, no `_run_n` iterates.
- **Rule identity is rehydrated.** `rule_id`, `source`, and `severity` are
  overridden with the current rule's values (§2.5) before the diagnostic is
  returned, so a hit against a byte-identical-predicate rule reports the current
  rule's identity, not the producer's.
- **A `cache_hit` evidence record is appended** to the returned diagnostic:

  ```json
  {
    "kind": "cache_hit",
    "data": {
      "cache_hit": true,
      "cached_at": "<entry.created_at>",
      "provider_calls": 0,
      "cost_usd": 0
    }
  }
  ```

  `cost_usd: 0` and `provider_calls: 0` describe **this run's** incremental spend.
  The original `llm_judgment` evidence retains its original
  `cost_estimate_usd` / `latency_ms` / token counts, so cost observability of
  *what the verdict cost to produce* survives — the marker reports what *this*
  run paid (nothing), and `cached_at` dates the original computation. Zeroing the
  stored telemetry instead would destroy the produced-cost signal; a distinct
  marker keeps both readable. This is a design choice made here, not inherited
  from #69.

The appended record is added on every hit and is not itself persisted into the
stored entry, so repeated hits do not accrete markers.

---

## 7. Rollout

**Opt-in first.** Slice B ships default-off behind two switches:

- `--eval-cache` CLI flag on `validate` — explicit per-invocation opt-in.
- `GATE_KEEPER_EVAL_CACHE=1` in the per-project dotenv — project-level default.
  Read through the same dotenv snapshot mechanism as
  `GATE_KEEPER_TOKEN_BUDGET` / model config (`_load_env_file`), **not**
  `os.environ`; the backend intentionally ignores process env to avoid host
  collisions (`docs/auth-matrix.md`). The CLI flag takes precedence over the
  dotenv value when both are present.

Truthy dotenv values follow the existing convention (`1 | true | yes | on`).

**Default-on is a later, telemetry-backed decision.** Turning the cache on by
default requires evidence of hit rate and, more importantly, of correctness (no
observed wrong-serve across a `PROMPT_VERSION` bump or a model swap). The
`cache_hit` markers (§6) are the raw telemetry for that decision, which lands
under #277's cost-lever tracking, not in slice B.

### 7.1 Size cap and eviction

Minimal but explicit. A single size cap, default 500 MB, overridable via
`GATE_KEEPER_EVAL_CACHE_MAX_MB` in the dotenv. Eviction is **best-effort LRU by
file mtime**: after a successful write, if the eval-cache directory exceeds the
cap, unlink oldest-mtime entries until under it. Eviction never blocks a
`validate` run and never raises — an unlink race (another process already
removed the file) is ignored. mtime is used rather than atime because atime is
unreliable across mount options; a hit may optionally `touch` its entry's mtime
to bias LRU toward retention, but this is not required for correctness. One file
per key means no index to maintain: eviction is a directory scan. Smarter
policies (hit-count weighting, TTL) are deferred until telemetry shows the flat
cap is inadequate.

---

## 8. Failure-Mode Summary

| Situation | Behaviour |
|-----------|-----------|
| Key miss | Recompute; store on stable verdict. |
| Unknown `cache_schema_version` | Miss → recompute → overwrite (§2.3). Never error. |
| Malformed JSON / `from_dict` failure | Miss → recompute → overwrite. Never error. |
| Aggregated status not `PASS`/`FAIL` | Not written (§2.4). |
| Concurrent miss on the same key | Both compute; atomic rename; last writer wins (§5, R11). |
| Cache disabled (default) | No read, no write — byte-identical to today. |
| Cap exceeded after write | Best-effort mtime-LRU eviction; never blocks or raises (§7.1). |

Every path either returns the genuine evaluation or a previously-computed
genuine evaluation. No path returns a verdict computed for different inputs, and
no path fails a rule because of a cache problem.

---

## 9. Deferred / Out of Scope

- **Distributed / shared cache.** Local per-working-directory only (matches #69
  "Out of scope").
- **Caching non-terminal outcomes.** `UNAVAILABLE` / `ERROR` /
  `provider_error` / `provider_unconfigured` are never cached (§2.4).
- **Caching deterministic pre-provider verdicts.** `multi_target_unsupported`
  and the #178 `target_kind_mismatch` precheck cost no provider call; slice B
  does not cache them.
- **Negative-result TTL / hit-count eviction.** §7.1 ships a flat size cap only.
- **Cross-run reproducibility bypass.** The cache never substitutes for an
  honest cold reproducibility measurement; it memoizes one (§2.1).
- **The S5 assembled-content hash (#281).** When S5 assembles a narrowed
  multi-file context, that assembled content becomes the `targets` content input
  to this same key schema. Slice B does not implement S5; the key schema is
  designed to accept it without change (§3.1 already hashes resolved artifact
  content in order).

---

## 10. References

- Umbrella: #277 (incremental audit trunk); cost-lever framing in the #277 stage
  table (S2-B).
- Hosting issue: #69 (two-slice plan; slice A = `--deterministic`, slice B =
  this cache), under semantic-rubric umbrella #63.
- `PROMPT_VERSION` history and the #191 no-bump rationale:
  `src/gate_keeper/backends/llm_rubric.py` (comment above `PROMPT_VERSION`).
- Reproducibility aggregation: `src/gate_keeper/validator.py` `_run_n`.
- Strategy machinery: `_STRATEGIES`, `_resolve_strategy_id`,
  `_run_adaptive_strategy` (#183–#186; adaptive escalation #254).
- Model / provider resolution and dotenv convention: `_resolve_model`,
  `_load_env_file`, `DOTENV_PATH`.
- Bounded-concurrent dispatch: #249 (`ThreadPoolExecutor` in `validator.py`).
- Pricing snapshot and cost estimation: `_MODEL_PRICING`, `_estimate_cost`.
- Multi-target parsing: `_parse_multi_targets`, `MultiTargetSpec` (#182).
