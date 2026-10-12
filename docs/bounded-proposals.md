# Bounded semantic placement ranking

`oabm.proposals` is a standalone opt-in boundary for one task:
`rank_equipment_placement_candidates`. It ranks supplied references to candidates
from the existing `propose_equipment_placement` producer. It does not recognize
new objects, change producer behavior, apply equipment, design circuits, call
providers or mutate a canonical model. This implements the bounded child #286;
#98 remains open for production adapters, integration and end-to-end acceptance.
The preserved `spike/98-bounded-image-registration` experiment remains separate.

## Source and proposal contracts

`contracts/bounded-proposal-v1.schema.json` defines request, result, routing
configuration and measured benchmark contracts, all explicitly versioned 1.0.0.
Source objects use the existing canonical `SourceSnapshot` and
`ModelSourceContext` definitions. Geometry uses canonical primitives. There is
no canonical schema extension or competing geometry representation.

A `PlacementContext` freezes the original model and all explicit producer
arguments. It recomputes the actual producer from those source facts; callers
cannot promote a copied candidate's `valid` flag. Stable candidate IDs bind the
entire candidate content. A ranking must include every supplied candidate once,
reference only existing source evidence, and choose a top candidate without
known producer clearance conflicts. Invalid alternatives may remain below it.
Source observations and owner-relative evidence remain separate from inferred
ranking evidence. A ranking's provenance is `ai-proposed`, `derivation=inferred`,
scoped to its output, and bounded by the supplied ranking confidence, producer
confidence and applicable source facts. It is never source-observed,
user-confirmed, code-verified or canonically accepted.

Every request binds the exact model content, level/region, candidates and
producer arguments, task/validator/prompt versions, question, optional pixels
and routing configuration. The model content fingerprint uses the existing
semantic entity-order-and-scope-set normalization; it is not a raw PDF digest.
An optional gray8 crop explicitly identifies that same model and level/region;
no crop-to-building registration is inferred. Result identity additionally
binds the dispatched adapter/provider/model alias, outcome and decision evidence.

Use `result.validate_against(model, options, config, current_request=...)` before
consuming a saved result. Self-consistent hashes alone cannot detect a changed
current question, prompt, crop, source or configuration. This method compares
that entire current request and recomputes source candidates. Hashes provide
content integrity, not authentication of a remote execution.

Unordered source entity collections, candidate sets, served-device sets and
provenance scope qualifiers are normalized. Geometry, provenance record history,
conflict-reason order in the normalized source, and semantic output ranking order
remain meaningful. Multiple opening/obstacle/constraint conflicts are computed
from the same normalized original model in capture and readback. No existing
producer/default exporter serialization changes.

## Configured roles and trusted dispatch

`RoutingConfig` requires explicit opaque endpoints (`adapter_id`, `provider_id`,
`model_alias`) for five caller-designated roles. There are no hard-coded SDKs,
API URLs, authentication, live calls or claims that an alias resolves to a
particular available remote model. In the requested policy:

- `text_glm` is the GLM 5.3 Flash baseline. Only a typed and revalidated
  `rate_limited` or `unavailable` failure invokes `text_codex`, the Codex
  availability fallback. Invalid output, clearance failure and unrelated
  exceptions do not trigger retry or model escalation.
- `vision_luna` is the GPT-6 Luna baseline. Invalid ordinary output is rejected;
  it does not itself authorize Sol. Caller-requested `benchmark_secondary`
  uses `vision_sol` only with measured failing Luna evidence for the exact
  task/prompt/validator/configuration/alias.
- `diagnostic_astra` is GPT-6 Astra diagnostic-only. It requires the explicit
  `diagnostic=True` request and a visibly diagnostic result. It is never reached
  by a normal text or vision fallback, including after Sol failure.

Provider output contains only rankings, confidence, bounded reasons and evidence
IDs. It cannot choose or spoof its provider, model, role or accepted state. The
trusted dispatcher records the configured endpoint actually invoked. Every
adapter invocation receives a disposable deep JSON copy. A mutating adapter,
including one that raises an availability error, cannot alter the original
source/request; fallback receives a fresh pristine copy.

## Measured vision gate

`measure_luna_benchmark` runs the configured Luna adapter on a frozen public
synthetic corpus. Each case contains its exact vision request and an expected
eligible candidate ID. The report retains one raw outcome or typed availability
receipt for every case. Success is recomputed by the deterministic output
validator and expected top candidate, rather than trusting reported totals.
Cases, thresholds, counts, outcomes, configuration, endpoint and versions are
bound by a complete benchmark content fingerprint; a separate fingerprint binds
the frozen corpus. Duplicate/missing/empty cases, invalid thresholds and mixed
bindings are rejected before any adapter invocation.

A Sol gate requires `success_count < required_success_count` and no availability
failures. Missing, passing, stale or tampered evidence refuses dispatch. A caller
can run a new benchmark with a different explicit threshold, but cannot edit a
saved threshold or outcome while retaining its previous binding. Ordinary CI
uses fakes; these trials prove gating behavior, not actual remote-model capability.
A real provider's performance would need an authorized separate benchmark.

## Bounds and usage

Inputs are limited to 512 source entities, 128 candidates/served devices, 4096
question characters and 262144 gray8 pixels. A preflight conservative position
bound (`wall segments × same-level spaces × 6`) must not exceed 128; multiplying
it by an edge-aware bound on space vertices, obstacle/constraint vertices,
opening checks and load checks must not exceed 100000. Relevant geometry is
also limited to 4096 vertices. These limits refuse
work before the existing producer runs; they do not silently sample candidates.
Crop encoded length must agree with its exact dimensions before decoding.
Portable documents are limited to 1000000 JSON bytes, ranking reasons to four
256-character strings per candidate and evidence references to eight IDs.
Benchmarks contain 1–16 cases, a preflight corpus budget of 500000 bytes and a
65536-byte raw-outcome budget per case. These are explicit component bounds,
not measured runtime promises or building-code requirements.

```python
from oabm.proposals import PlacementOptions, RankingRequest, dispatch_ranking

options = PlacementOptions(
    identity_key='caller-panel', equipment_type='panelboard', name=None,
    level_id='level:one', served_device_ids=(), mounting_height_m=1.5,
    working_depth_m=.9, working_width_m=.8,
)
request = RankingRequest.capture(
    model, options=options, question='Rank these supplied placements.',
    prompt_version='caller-rank-v1', policy_fingerprint=config.fingerprint,
)
result = dispatch_ranking(request, config, adapters=registered_fake_adapters)
result.validate_against(model, options, config, current_request=request)
assert result.accepted is False
```

A deterministic semantic scorer can replace a fake or future model adapter by
returning the identical ranking schema. No canonical rewrite is needed.
Registered geometry and known producer checks remain authoritative. This child
provides no physical placement application, service/capacity verification,
engineering approval, live OA operation or delivered customer document.
