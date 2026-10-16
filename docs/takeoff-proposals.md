# Source-bound takeoff proposal experiment

The v2 optional `oabm.proposals.takeoff` adapter extends the proposal boundary of
#98 with a narrow task: locate lighting marks within one supplied source region
and propose a type from explicit allowed choices. It is a sidecar, not a new
building model. It does not use the equipment-ranking request for detections.

## Boundary and routing

The existing `Endpoint`, `Portable` strict JSON transport, bounded validation
helpers, canonical `stable_id` and `Provenance` are reused. Shared model,
contracts, importer recognition and `dispatch_ranking` remain untouched.

`run_takeoff_experiment(request, endpoint, adapters=..., images=...,
documents=..., experiment_reason=...)` requires explicit caller authorization for an experiment
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
- `render_source_image` reads immutable original PDF bytes, derives source facts
  from their actual 1-based page, renders the MediaBox with the source quarter
  turn, then crops integer raster edges without resizing. The fixed policy uses
  PDFium RGB pixels, white background, no additional rotation, and printable
  annotation appearances (`pdfium-mediabox-rgb-print-annots-v2`). The supported
  static appearance subtypes are Stamp, Square, Circle, FreeText, Line, Polygon,
  PolyLine and Ink. Other printable subtypes refuse explicitly. Printable
  widgets, missing/state-dependent normal appearances, and annotation optional
  content refuse rather than silently vanish; interactive forms remain unsupported.
  Historical `pdfium-mediabox-rgb-no-annots-v1` references remain verifiable only
  under their recorded policy; their pixels/identities are never upgraded. `SourceImage` includes the exact PNG SHA-256 plus a `RenderManifest` with
  policy/version, installed pypdfium2/PDFium versions, scale and a six-value
  affine from local crop pixels to displayed bottom-origin points. The affine
  must agree exactly with the source raster and crop metadata.
- Hashes and manifests are claims until verified. Dispatch, saved-result
  `validate_against`, and `unresolved_observations` all require `documents`
  (exact PDF SHA-256 to immutable bytes) and `images` (image ID to bytes). They
  recompute the document hash, parse actual page/MediaBox/rotation, rerender
  through the fixed implementation, and compare every decoded RGB crop pixel.
  Wrong source bytes, page, crop, raster dimensions, rotation, transform,
  renderer version or changed/resized pixels refuse before source coordinates
  can be exported. There is no caller-attested or portable verified-state flag.
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
  against the current request, endpoint and exact source/image bytes. Portable
  decoding checks structure and content hashes only; it does not grant source
  verification. Output hashes do not authenticate remote model execution.

Boxes are `(x0,y0,x1,y1)` with positive area. Provider boxes use [0,1]
normalized coordinates within the target crop, origin top-left, +Y down.
`unresolved_observations(documents=..., images=...)` verifies the render binding
and maps them deterministically through the manifest affine into full-page pixels
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

## Trusted render boundary and compatibility

The trusted boundary is this Python implementation, its installed PDFium runtime,
and the caller's selection of original PDF bytes/source identity. The model
adapter cannot supply a renderer, transform implementation, source-validation
receipt or PDF bytes. It receives only verified image bytes and a disposable
request. A self-consistent fabricated portable result still must pass a fresh
render comparison at coordinate export. The check proves pixel equivalence to
that exact PDF page/crop under the recorded render policy. It does not prove the
historical process that created an equivalent PNG, authenticate document ownership
or model execution, or verify the semantic classification.

`pypdfium2` is already installed by the repository's `tables` extra used in CI;
render verification fails closed when it is unavailable. PDFium calls in this
component are serialized. Other PDFium use must run in a separate process rather
than concurrently with this component. See the [renderer API and thread-safety
contract](https://pypdfium2.readthedocs.io/en/stable/python_api.html).

PDFs are limited to 128 MB each and 256 MB total. Full-page render work, including
unique source/page/scale contexts, is limited to 64 million pixels per operation;
individual crops retain the 16-million-pixel limit. Source pages rendered at the
same scale are reused within one validation operation. Render caches never
survive that operation or replace future source verification. Non-unit
`/UserUnit`, encrypted/malformed PDFs, absent pages, unsupported policies, and
renderer/source dimension disagreements fail closed. Different renderer versions
must produce a new request; no stale receipt is silently accepted.

The proposal envelope and validator are v2. V1 records have no enforceable render
binding and are rejected by the active API. The previously frozen private
46-proposal experiment remains byte-for-byte historical evidence, including its
original renderer/context limitations. It is neither rewritten nor retroactively
certified by this code fix. Any future verified experiment needs newly rendered
inputs and a separately recorded request/result. Independent review decisions
must continue to remain separate from those raw proposals.

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

Synthetic tests generate real independent two-page PDFs. They cover strict decoding,
rerendered PDF/page/crop/rotation/resize/affine rejection (including self-consistent
forged hashes and saved-result exports), actual offset-page pixel locations,
source/image integrity, evidence
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


## Annotation completeness and historical evidence

New source captures use PDFium annotation rendering in printing mode. The PDF
Print flag governs inclusion; hidden and nonprinting annotations are not painted.
`source_annotation_inventory(document, page=...)` in `pdf_annotations` returns
source PDF digest, annotation indices/object numbers, flags, raw rectangles and
normal-appearance digests. It does not extract appearance text or interpret work
status. Keep this inventory with independently reviewed annotation evidence.

The recorded render policy participates in rerender cache identity and pixel
comparison. A legacy no-annotation crop cannot be relabeled as a print-policy
crop. Existing frozen requests/results retain their original serialization and
remain historical observations over their original view. Reconciliation is a
separate source-bound review layer; new captures have new policies and hashes.
No annotation contents are promoted to canonical facts by rendering alone.
