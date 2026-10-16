# Source-bound takeoff proposal experiment

This optional `oabm.proposals.takeoff` adapter extends the proposal boundary of
#98 with a narrow task: locate lighting marks within one supplied source region
and propose a type from explicit allowed choices. It is a sidecar, not a new
building model. It does not use the equipment-ranking request for detections.

## Boundary and routing

The existing `Endpoint`, `Portable` strict JSON transport, bounded validation
helpers, canonical `stable_id` and `Provenance` are reused. Shared model,
contracts, importer recognition and `dispatch_ranking` remain untouched.

`run_takeoff_experiment(request, endpoint, adapters=..., images=...,
experiment_reason=...)` requires explicit caller authorization for an experiment
and a registered provider-neutral callable. There is no provider SDK, credential
lookup, model download, network call or automatic retry in the module. The
callable receives disposable request/image mappings and trusted provider/model
metadata. An ordinary fake or later deterministic classifier can replace it.

This is an explicit experiment entry point, not a replacement production
routing policy. The GLM/Codex availability fallback, measured Luna-to-Sol gate and
diagnostic-only Astra policy remain in `dispatch_ranking`. A direct strong-model
experiment does not establish a permanent task upgrade or prove that Luna fails.

## Source and output contracts

- `SourcePage.capture` records stable source document identity, exact PDF
  SHA-256, 1-based page, absolute MediaBox, page rotation, displayed point
  dimensions and full-page rendered dimensions. It uses the shared
  `page_display_transform`, including nonzero MediaBox origins and quarter turns.
- `SourceImage` references one immutable PNG/JPEG by SHA-256 and exact crop bounds
  in the displayed full-page raster. The runner checks bytes, format and decoded
  dimensions before invoking the adapter. Renderer settings belong in the
  caller's execution record. Render using the MediaBox, apply PDF rotation, and
  disable non-source reviewer annotations. A CropBox/resized render needs an
  explicit upstream transform; it cannot be passed as a matching MediaBox crop.
- `TakeoffRequest` binds the exact ordered images with roles `target`,
  `floor_context`, `legend`, `schedule`, `notes`; one target; allowed type/tag
  choices; editable confidence threshold; question and task/prompt/validator
  versions. A legend is required. Schedule and notes must be supplied when
  applicable; the adapter cannot discover missing context by itself.
- `ProposedMark` includes local crop bounds, proposed type/tag or unknown,
  source-mark description, confidence, unknowns and supplied evidence image
  references. A type requires legend evidence; a tag requires schedule evidence.
  Descriptions and references are proposal evidence, not proof the visual match
  is correct. Unknown evidence IDs, unknown fields (including circuits,
  canonical entities or accepted state), malformed/outside boxes and invalid
  confidence fail closed.
- `TakeoffResult` binds request, raw output and configured endpoint. It always
  has `accepted=False` and `review_state="pending"`. Revalidate saved results
  against the current request and endpoint. Output hashes provide content
  integrity, not authenticated remote execution.

Boxes are `(x0,y0,x1,y1)` with positive area. Provider boxes use [0,1]
normalized coordinates within the target crop, origin top-left, +Y down.
`unresolved_observations()` maps them deterministically into full-page pixels
and normalized coordinates (top-left), then displayed page points (bottom-left,
+Y up). Every row retains crop bounds and source-to-display transform. These
are source-page bounds, never registered building geometry or metre coordinates.

Source page IDs bind the source snapshot; generated source-mark IDs bind exact
document revision, source page and source bounds. They do not use list position,
type, tag or confidence. Redrawing/moving a mark changes this geometry-derived
identity; independently localized or overlapping marks need explicit duplicate
review. Exact repeated boxes within one response are refused. Raw output order
is preserved and its fingerprint changes with order; exported rows sort by stable
mark ID. Separate generated proposal IDs also bind request and raw-output fingerprints. No deduplication or review decision rewrites the raw proposals.

## Uncertainty and review

The detached export uses the existing importer diagnostics envelope:
`kind`, `page`, `source_element_id`, `position_pt`, `status`, `reason`.
It adds explicit source coordinates and takeoff proposal evidence. Canonical
provenance is `source_kind="ai-proposed"`, `derivation="inferred"`. A low
confidence item gains a threshold reason; even a high confidence item stays
unresolved/pending. No model, device, port, route, circuit, or
`ElectricalInstanceHint` is produced or mutated.

These sidecars do not enter `build_confidence_review` as canonical entities:
its existing `EvidenceReference` requires an actual current canonical owner.
Independent review must retain a separate decision ledger keyed by mark ID and
raw-output fingerprint. Accepting a label does not establish registered geometry,
circuiting or BIM acceptance. This is the evidence/review slice of #241, not its
price-at-risk, 3D presentation, self-check or OA completion.

## Private evaluation and benchmark export

Keep source images, customer context, raw proposals, overlays, review decisions
and results outside this public repository. Do not read manual marks or answer
keys during inference. Freeze the raw response before independent review.
Use a contact sheet or source image overlay with item IDs, then inspect missed
regions, overlapping boxes, leader/callout confusion and count mismatches.

The existing private visual benchmark harness can consume a new private data
root without repository mutation. Retain its conventions:

1. `manifest.json` has `schema_version:1`, case ID and ordered input sets.
2. Every input image and prompt has a relative path and exact SHA-256.
3. Each run records configured model, resolved model receipt if available,
   experiment variant/settings, raw result path/hash and final-output pointer.
4. Exact request evidence contains prompt text and ordered successful image
   reads. Use the harness's `captured_in_request_evidence` pointer convention.
5. Human labels and run adjudications live under `evaluation-only/`, never in
   inference input sets. Pending labels must remain empty and adjudication
   pending. Do not invent a human-confirmed label or score your own response.
6. A multi-turn self-inspection lacks an isolated exact-model provider receipt
   and includes earlier development context. Record those limitations; do not
   describe it as a controlled model comparison. A fresh frozen-context run and
   independent missed/duplicate review are required for measured accuracy.

Full-floor coverage is obtained through bounded regions and explicit coverage
notes. It is an inspection record, not a completeness certification. Schedule
quantities may be whole-building totals and cannot be used to fill unseen marks
or as independent ground truth.

## Verification and scope

Synthetic tests cover strict decoding, source/image integrity, evidence
membership, bounds/units, rotated offset-MediaBox pages, stable identity,
provider mutation isolation, unknowns, stale results, duplicate boxes, budgets,
and refusal of canonical state injection.

#98 checks supported by this adapter: configured endpoint isolation; narrow
structured interface; inferred proposal provenance; deterministic rejection;
replaceable adapters; no whole-job canonical prompt. The existing bounded ranking
component supplies routing-ladder fake tests and its measured public-safe vision
gate. This new task has no measured Luna/Sol/Astra capability benchmark or live
production adapter, no canonical acceptance path, and no verified full-floor
accuracy. #98 and #241 remain open.
