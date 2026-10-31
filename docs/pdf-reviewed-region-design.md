# Source-bound reviewed regions: design contract

This document addresses [issue #381](https://github.com/banddude/officeadmin-building-model/issues/381):
an opt-in, source-bound reviewed-region design for architecture and electrical
PDF import. It records the accepted v2 design and the independently reviewed
foundation clarifications. It does not change canonical schema or enable
production admission.

## Issue completion scope

Issue #381 is a **design task**, not a promise to fix recognition or actual
room/device counts. Its criteria are fulfilled by publishing the accepted
contract and synthetic contract evidence for source/frame binding, disposition,
overlap, mismatch rejection and opt-in compatibility. Production extractor
adapters, a verified renderer, admission/dependency integration and actual-source
validation are future capability gates, **not additional prerequisites to
complete #381**. No new issues or production capability are created by this
document.

The companion [foundation report](pdf-reviewed-region-foundation.md) identifies
what the isolated code implements and what remains unavailable. The existing
109-case focused suite in `tests/test_pdf_region_review.py` exercises the
foundation; the broader acceptance matrix below specifies future integration
tests as well and is not a claim that those future tests have run.

## 1. Problem and non-goals

A source may contain drafting forms, logos, legends, schedules and genuine plan objects with similar vector shapes. An automatic drafting-form hypothesis is not a reviewed region. Unsupported text/rule visibility and extrapolated regions cannot establish negative ownership. This interface permits an explicit reviewer decision on an exact source region; it does not improve PDF visibility inference or retrospectively certify a heuristic.

No producer may manufacture an accepted review from metadata families, page margins, target counts, expected element IDs, or a detector's confidence. A tool may propose geometry for review, but proposal and accepted review are separate states; only an explicit review action creates the latter. This specification does not supply any actual reviewed region.

Non-goals: PDF painting rewrite, automatic logo recognition, full-page exclusion heuristics, canonical schema additions, circuits inferred from regional membership, automatic re-admission of previously refused observations, or public publication of private source material.

## 2. Integration constraints

The existing architecture and electrical importers retain different observation
types and use different materialization paths. A raw PDF digest is distinct from
an extracted-inventory fingerprint; logical source IDs authenticate neither.
Existing candidate anchors do not certify complete geometric support.

Future integration must inventory every materializer and ownership-changing
merge, preserve selected original page numbering, registration and level
semantics, and use existing diagnostic/attribute extension points. A final-output
filter alone is insufficient. These are implementation constraints, not extra
design-issue completion requirements.

## 3. Proposed input types (design notation, not existing API)

Use immutable typed records. Review records and fingerprint payloads have strict JSON equivalents; the runtime snapshot/context capability explicitly does NOT. Reject unknown fields, nonfinite numbers, booleans used as numeric coordinates, and implicit string-to-number coercions. Version 1 accepts only axis-aligned rectangles in one explicitly normalized displayed frame.

```text
PageFrameV1:
  page_number: positive integer, ORIGINAL PDF 1-based index
  coordinate_space: literal "displayed-bottom-origin-pt/v1"
  media_box_pdf_units: four finite numbers
  crop_box_pdf_units: four finite numbers (resolved default recorded explicitly)
  user_unit: positive finite number
  rotation_degrees: 0 | 90 | 180 | 270
  displayed_width_pt, displayed_height_pt: positive finite numbers
  pdf_to_display_affine: six finite numbers, invertible
  visible_page_bbox_pt: ordered finite rectangle
  normalization_version: nonempty string

ExtractionBindingV1:
  adapter: "pdf_architecture" | "pdf_electrical"
  extractor_version: nonempty version identifier
  normalization_version: nonempty string
  observation_fingerprint_sha256: 64 lowercase hexadecimal characters

ExtractionSnapshotV1 (runtime-only, not accepted from JSON or caller construction):
  source_bytes: one immutable bytes object, owned for the import lifetime
  document: immutable adapter extraction from exactly source_bytes
  context: VerifiedSourceContextV1, internally issued
  issuance: internal runtime capability checked by identity, never a JSON token

VerifiedSourceContextV1 (read-only contents of an issued snapshot; not a separately accepted input):
  content_sha256: exact SHA-256 of source PDF bytes
  source_id: existing logical source ID, not a replacement for hash
  pages: immutable mapping original page number -> PageFrameV1
  extraction: ExtractionBindingV1
  context_producer_version: nonempty string

RenderContextV1:
  profile: literal "pdf-default-review-view/v1"
  content_sha256: exact source bytes
  page_number: original 1-based index
  frame_digest_sha256: canonical PageFrameV1 digest
  renderer_id, renderer_version: exact approved preview producer identity
  view_event: literal "View"
  optional_content_policy: literal "document-default-configuration"
  annotation_policy: literal "default-view-visible-annotations"
  interactive_overrides: literal false
  external_content_policy: literal "disabled"
  output_raster_sha256: digest of the actual private preview raster
  output_width_px, output_height_px: positive integers
  display_to_raster_affine: six finite numbers
  producer_receipt_id: inert ID of private renderer-issued preview receipt

ReviewedRegionV1:
  review_id: nonempty stable ID
  region_id: nonempty ID unique in review set
  page_number: original 1-based page index
  bbox_pt: [left, bottom, right, top] in declared displayed frame
  disposition: "plan" | "non_plan" | "unresolved"
  scope: literal "automatic_plan_object_admission"
  reviewer_id: nonempty attributable actor identifier
  reviewed_at: explicit timezone-bearing timestamp
  review_basis: literal "issued_default_view_preview"
  render_context: RenderContextV1 matching an issued preview receipt
  basis_reference: opaque private review/session/artifact reference
  note: optional bounded text, not parsed for semantics

ReviewedRegionSetV1:
  version: literal "1.0"
  content_sha256, source_id: exact source bindings
  page_frames: exact frame records for every referenced page
  extraction_bindings: one or both adapter bindings
  regions: immutable sequence of ReviewedRegionV1

```

Review records are assertions, not cryptographic authentication. No reviewer_id or timestamp supplied by a detector proves a review occurred. Existing authorized caller/review workflow must explicitly supply the accepted record; no new access or authority is introduced. Basis references are audit references, never automatically fetched URLs or commands. Review basis is deliberately narrowed to an internally issued default-view preview. Its receipt binds the exact immutable PDF snapshot, page frame, producer/version, explicit view policies and actual raster digest. A review supplied with a different layer/annotation policy, custom overrides, unsupported renderer, missing receipt or mismatching receipt is rejected. The preview renderer must either implement this profile faithfully or refuse issuance; writing the fields alone is insufficient. Optional-content defaults and annotation visibility come from the bound PDF under the recorded View profile, not caller toggles. No default-view renderer support is presumed to exist today. Issuing production receipts requires separate renderer verification; the initial design slice below uses only synthetic fixtures. The receipt proves which preview was issued, not that a human actually looked at it.

A shared region set can carry both adapter bindings when independently prepared for both. A single adapter binding is NOT portable to the other importer by comparing logical IDs. Each adapter validates its own extracted inventory against its own binding while sharing exact byte/page/frame/region intent.

Proposed eventual API shape (not enabled by the first implementation slice):

```text
  extract_review_snapshot(path, adapter) -> ExtractionSnapshotV1
    reads once into immutable bytes, then hashes and extracts from that same
    bytes object via adapter readers; never reopens the path for extraction.
  issue_default_review_preview(snapshot, page, approved_renderer)
    -> runtime preview receipt and private raster
    renders that same snapshot bytes object; output receipt is not caller-issued.
  validate_region_reviews(snapshot, runtime_preview_receipts, reviewed_regions)
    -> immutable validated plan, no model mutation.
  evaluate_region_candidate(validated_plan, CompleteCandidateSupportV1)
    -> pure decision/audit data, no canonical model.
  import_pdf(..., reviewed_regions=None)
    eventual wrapper must obtain/validate an issued snapshot and matching
    preview receipt through the authorized review workflow.
  import_document/import_observations(...)
    legacy API remains unchanged. Arbitrary document + deserialized context
    is NOT an accepted route for reviewed admission.
  import_reviewed_snapshot(snapshot, receipts, reviewed_regions, ...)
    eventual separate opt-in adapter route; requires the exact internally
    issued snapshot document and receipts, not look-alike dataclass values.

```

No review-triggered path rereads mutable files after snapshot creation. Later
file mutation does not change the retained snapshot, hash or extracted evidence.
No network fetch follows a receipt ID. Cross-process/persisted snapshot and
receipt hydration is unsupported in v1; adding it requires a separately reviewed
trusted verification mechanism, not accepting a serialized "issued=true" flag.
A previously saved review may be submitted only against a freshly issued local
snapshot and faithfully regenerated matching preview context; producer or output
changes invalidate it. These interfaces are proposals, not existing APIs.

Final public naming and options-versus-keyword placement require independent API review. Existing positional arguments and defaults must remain unchanged. No-review calls must not require context, emit new keys, or execute new rejection policies.

The runtime boundary trusts the installed extractor/preview producer and the authorized caller supplying review assertions. It does not defend against hostile Python code with arbitrary process access; runtime identity checks prevent accidental or JSON-level substitution, not a compromised host. Within that boundary, reject caller-created contexts, copied dataclasses and arbitrary observation-only documents even if their fields match. The snapshot owns the exact extracted document; a recomputed inventory fingerprint detects unexpected mutation. Designing snapshot/context/receipt issuance is an importer/extraction-layer change, not a canonical BuildingModel change.

## 4. Fingerprint and identity rules

Define versioned per-adapter canonical serialization over the complete actual observation object and frame context. Sort semantic collections by page, observation kind and stable source ID; reject duplicate composite IDs before hashing. Preserve text, geometry, paint/visibility metadata and provenance included in the extraction. Never omit unknown fields to make a stale review pass. Freeze canonicalization rules; an extractor version change requires fresh binding.

No region input includes an expected canonical-ID whitelist or target count. It references geography, not desired output. Candidate IDs and canonical identity keys follow existing algorithms and must not be renumbered because another candidate was withheld.

Retain every supporting observation reference, separating geometric identity support from contextual evidence such as legend prototypes and distant schedule text. Retain both categories for audit; only the former defines candidate containment. Document this distinction per recognition path to prevent either distant legend text inflating the bbox or omitted glyph strokes shrinking it.

## 5. Ordered validation and transaction semantics

Before effects:
1. Parse strict versioned types, enforce bounded input size, enum and timestamp syntax.
2. Require internally issued snapshot and preview receipt objects by runtime identity; reject caller-created/deserialized substitutes. Validate exact raw hash/logical identity against the immutable snapshot, adapter version and recomputed complete observation fingerprint. Validate every RenderContextV1 field against its issued receipt and the supported profile; no omitted default or prose-only compatibility.
3. Validate original page existence, selected-page membership and full frame equality. Do not reindex after selected_pages filtering. Missing page or frame is an error.
4. Validate finite ordered rectangles wholly within the verified visible page extent. Reject off-page geometry, unsupported frames, singular transforms and implicit cropping.
5. Deduplicate byte-equivalent review records idempotently. A repeated region_id with different record content is a conflict.
   A review_id may group several distinct region IDs in one review event; it is
   not a unique region key. Identical region records deduplicate idempotently. No "latest timestamp wins".
6. Validate region overlaps and explicit caller evidence compatibility.
7. Build candidates/ownership using untouched observations; establish complete identity support and hint association.
8. Evaluate decisions, validate dependency closure and all diagnostics; publish a model only after the entire import succeeds.

Invalid binding or contradictory supplied input raises a typed importer validation error carrying stable reason code, page and review IDs. No partially filtered model is returned and no silent legacy fallback occurs. Existing importer error types may wrap the shared error without changing canonical schema. Offline diagnostics may be returned by a separately named validation operation, not disguised as a successful model import.

Valid review with ambiguous candidate membership is different: emit an unresolved candidate diagnostic and withhold automatic admission; other unaffected candidates may proceed.

Stable error families: review_schema_invalid, review_snapshot_not_issued, review_render_context_mismatch, review_render_profile_unsupported, review_source_mismatch, review_context_missing, review_extraction_mismatch, review_frame_mismatch, review_page_not_selected, review_geometry_invalid, review_id_conflict, review_disposition_conflict, review_hint_conflict, review_adapter_support_incomplete. Detailed messages must avoid exposing source text unnecessarily.

## 6. Geometry and overlap semantics

All comparisons occur before model registration in displayed source points. Frame adaptation must explicitly account for original page boxes, offset origins, UserUnit and rotation. Region transforms into model space are recorded for traceability but do not redefine source membership.

Version 1 uses a fixed, versioned comparison epsilon of 1e-6 displayed points solely for numerical uncertainty. Do not grow regions or snap a candidate into ownership. A candidate is definitely contained only when its complete finite support bbox is strictly inside every side by more than epsilon. A point/line support may have zero area; it still must be strictly interior. Contact or epsilon-near contact is boundary ambiguity.

For candidate support, conservative bbox over-approximation is acceptable; under-approximation is not. Include complete vector geometry, curve extrema and applicable painted extent where needed; account for nested transforms and all merged identity-support paths. Existing anchors or font size alone do not certify text or Form extents. When exact support is not available, conservative verified bounds may be used; otherwise the reviewed adapter is incomplete. Do not guess bounds or silently exempt that candidate class. If bounded localization proves possible intersection but not containment, emit unresolved. If even safe localization is unavailable for an in-scope candidate, reject the opt-in import with review_adapter_support_incomplete rather than claim full coverage.

Different dispositions whose interiors overlap are invalid at input validation, irrespective of whether a candidate currently exists there. Same-disposition overlap is allowed. Deterministic candidate precedence:
1. If support intersects/contact-touches regions of DIFFERENT dispositions (including contact within epsilon), return unresolved; input-level interior-overlap conflicts have already been rejected.
2. Otherwise, if any one region of the intersected disposition definitely contains the complete support, apply that disposition. Boundary crossing of a smaller/overlapping SAME-disposition region does not veto complete containment in a larger one. Retain all intersected review references and separately identify the containing authorizations.
3. Otherwise, any intersection/contact is unresolved.
4. If there is no intersection/contact, retain original behavior.
Version 1 does not merge adjacent rectangles into a larger authorization: containment by the union is insufficient. Disjoint regions with different dispositions can still be crossed by a candidate; that candidate is unresolved, not split.

Wholly outside all reviewed geometry: original behavior.
Wholly inside a plan rectangle: original eligibility checks still apply.
Wholly inside a non_plan rectangle: reviewed exclusion from automatic plan-object materialization.
Wholly inside an unresolved rectangle: explicit ownership uncertainty withholding.
Touches/crosses reviewed geometry without the single-region same-disposition authorization above, or mixes dispositions: unresolved. A nested same-disposition boundary is explicitly non-vetoing when a larger single region definitely contains all support.
No trimming source paths, deleting unknown segments or fragmenting a candidate to make it fit. Existing geometry segmentation may be used only if independently justified by the importer before review, with original-source lineage retained.

## 7. Meaning, hint precedence and dependencies

Plan means permission to attempt the existing inference, not proof of type, level, visibility, scale, connectivity, circuit membership or confidence. Non_plan refers only to automatic plan-object admission: a legend/schedule may remain contextual evidence if its own independent producer validates it. Review does not make hidden legend text valid.

Unresolved and reviewed non_plan must remain distinct audit outcomes. Neither erases the observation. Review attribution is appended separately; do not change observed provenance into human-confirmed geometry or boost entity confidence.

Existing explicit instance hints are not silently suppressed by negative/uncertain regions. A hinted candidate potentially intersecting such a region raises review_hint_conflict; no model commits until caller removes or explicitly resolves conflicting input. The hint is not silently discarded, nor does it silently overrule the review. Plan regions do not validate hints. Complete explicit instance hints contacting
only plan regions retain their existing behavior, including at plan boundaries;
they must not be silently withheld by automatic boundary rules. Incomplete hint
support contacting plan review raises review_adapter_support_incomplete rather
than returning an automatic unresolved admission decision. Architecture scale/registration/level overrides are not automatically object-admission hints: preserve their established semantics, and reject any incompatible attempt to force an otherwise withheld object.

Perform ownership decisions before dependent canonical creation. No ports, equipment references, hosted openings, room boundaries or circuits may refer to withheld objects. A room/space whose essential boundary support is withheld must become unresolved, not be repaired by invented geometry. A circuit that loses a member cannot retain a completeness claim or be silently certified from its remaining members. Retain association evidence and report the unresolved dependency using existing mechanisms. Never delete evidence before symbol/prototype recognition merely to hide it from candidate generation.

## 8. Audit output without canonical schema change

Propose namespaced optional attributes only when review input is supplied:

```text
  model.attributes["pdf_region_review"]:
    version, source/context binding, normalized accepted review records,
    per-candidate decision records, validation/adaptation version.

Decision record:
  adapter, page, candidate_key, existing_identity_key_if_any,
  entity_kind, source_element_refs (complete typed IDs),
  identity_support_refs, contextual_support_refs,
  support_bbox_pt or explicit bounds_unknown,
  review_ids, region_ids, relationship,
  action ("unchanged" | "reviewed_exclusion" | "unresolved"),
  stable reason codes, existing confidence/provenance snapshot,
  dependent candidate/entity references where applicable.

```

Architecture additionally uses existing pdf_architecture.ambiguities for actionable unresolved region/dependency summaries; electrical uses existing unresolved_observations/unresolved_circuits for unresolved outcomes. Confirmed reviewed exclusion is not falsely labeled an unresolved device merely to reuse a list. Store authoritative detail once and reference it from summaries.

Preserve the complete extracted observation inventory through an immutable in-process reference and a caller-retainable source-bound inventory artifact or equivalent audit bundle; do not assume a PDF filename alone preserves observations. If output-only consumers require full snapshots, design the bundle explicitly rather than silently dropping supporting observations. No images/PDF copies or customer text must be uploaded by this feature; private audit retention follows existing storage controls.

## 9. Synthetic acceptance matrix

This matrix defines contract and future production-integration acceptance.
The current foundation suite tests issuance, strict validation and pure evaluation,
not complete importer materialization. In particular, end-to-end registration,
production support bounds, dependencies and audit persistence remain future
integration tests; listing them here is not a completion claim.

All generated fixtures use generic invented labels/geometry, not copied customer layouts.
A=architecture, E=electrical.

ID | Scope | Input/control | Expected
--- | --- | --- | ---
C01 | A/E | No input vs None vs empty regions | Legacy model serialization, IDs, confidence and errors unchanged; no new attrs.
C02 | A/E | Same source/candidates under plan, non_plan, unresolved | Eligible unchanged / reviewed exclusion / unresolved, respectively; complete source lineage retained.
C03 | A/E | Plan region around unrecognizable/otherwise refused object | No forced entity and no confidence increase.
B01 | A/E | Byte changed with same source ID/name/frame | Atomic review_source_mismatch.
B02 | A/E | Observation text/vector/metadata changed, same PDF claim | Atomic review_extraction_mismatch.
B03 | A/E | Missing snapshot, JSON/caller-created context, cloned snapshot or binding for wrong adapter | Reject; no inferred hash, value-only issuance or cross-adapter fingerprint reuse.
B04 | A/E | Same dimensions but CropBox/origin/rotation/UserUnit differs | Exact binding rejection.
B05 | A/E | Unknown/unselected original page; reordered selected pages | Reject unknown/unselected; original numbering preserved.
B06 | A/E | Duplicate source ID within kind; duplicated region ID different decision | Reject; identical review duplicates idempotent.
G01 | A/E | Equivalent layouts at rotations 0/90/180/270 and translated origins | Same semantic decisions with independently correct frame bindings.
G02 | A/E | Different model registrations/scales for same bound source | Same source membership; correct recorded transform chains.
G03 | A/E | NaN/Inf, zero/reversed rectangle, out-of-CropBox region | Atomic geometry/schema failure.
G04 | A/E | Candidate center inside but support crosses edge; edge/tolerance contact | Unresolved, retained full support, no clipping.
G05 | A/E | Large region fully contains support that crosses nested same-disposition region boundary | Containing region authorizes its disposition; both references retained. Partial coverage only by union remains unresolved.
G06 | A/E | Different-disposition interior overlap | Atomic disposition conflict even with no candidates.
G07 | A/E | Candidate spans two disjoint/adjacent reviewed regions | Unresolved, no split or union-based approval.
G08 | A/E | Curves, thick/mitered paths, nested transformed Forms, text-only candidates | Safe full bounds or explicit incomplete-adapter refusal; no centroid bypass.
H01 | E | Explicit instance hint inside/intersecting negative/uncertain region | Atomic hint conflict, hint preserved; plan/outside hints retain behavior.
H02 | A | Scale/level/registration overrides with reviewed regions | Existing override semantics retained; no forced withheld geometry.
D01 | E | Vector, named Form, text, merged marker and alternate materializer cases | One consistent admission decision; all identity source IDs retained.
D02 | A | Wall, space boundary, opening host and repeated drawing-region support | No dangling dependencies, invented closure or lost repeat provenance.
D03 | E | Withheld device belongs to inferred/confirmed circuit | No dangling member or silently complete remainder; explicit dependency outcome.
D04 | A/E | Legend/schedule within non_plan region | No plan object there, contextual evidence still independently validated.
D05 | A/E | Hidden/clipped metadata yields automatic form hypothesis, no review | No new veto or manufactured review.
D06 | A/E | Reordered observations/reviews/candidate traversal | Stable IDs, decisions and sorted audit serialization.
D07 | A/E | Candidate omitted vs withheld | Retained candidate record distinguishes both; no count-only certification.
P01 | A/E | Shared region set with two independent adapter bindings | Both validate shared frame intent, not interchangeable inventories.
P02 | A/E | Source revision with old bindings | Reject stale bindings. If trusted caller regenerates every binding/receipt but reuses an old human decision, importer cannot detect that; test/document this trust limitation, never claim enforced re-review.
P03 | A/E | Arbitrary basis_reference URL/command | Treated as inert audit data, never fetched/executed.
P04 | A/E | Custom OCG/annotation override, alternate renderer/version, raster/frame mismatch or missing issued preview receipt | Reject explicit render binding/profile; no prose-only acceptance.
P05 | A/E | File changes after snapshot bytes read | Hash/extraction/preview all refer to retained snapshot; no hash/extract TOCTOU.
P06 | A/E | Recreated default preview changes output digest or renderer | Reject stale render binding, require newly supplied explicit decision record.

Tests must assert complete reference sets and legacy identities, not just counts. Actual-source evaluation, if separately authorized, reports reviewed exclusions/unresolved changes distinctly and is not substituted for these controls.

## 10. Capability gates, separate from design completion

The independently reviewed foundation implements the first bounded slice below.
These implementation boundaries do not extend issue #381's design scope:
- Same-immutable-bytes snapshot/context mechanism and versioned strict review validator.
- Explicit render-context schema and receipt matching contract; only verified production producers can issue real receipts. Synthetic fixture producers are test-only and cannot be selected through production caller input.
- A pure evaluator over complete synthetic candidate support, including nested same-disposition precedence and conflict cases.
- No production admission filtering, no new CLI workflow that claims actual reviewed exclusion, no change to existing architecture/electrical import results.
- Pure evaluator support contract: stable candidate key, original page, all typed identity/contextual source references, conservative verified full support bbox, and a completeness flag. Incomplete support cannot receive a definite inclusion/exclusion; unknown safe localization rejects adapter readiness. Synthetic test constructors explicitly supply complete finite bounds.

Later, separately accepted adapter work must establish per-adapter fingerprint canonicalization, trusted one-snapshot extraction and preview support, complete production support bounds, all-materializer/dependency audit, output audit retention and review workflow attribution. Only after independent verification may production admission be connected. Existing callers remain unchanged; no canonical schema modifications.

Enforceable freshness is equality of supplied binding to currently issued source/extraction/render evidence. There is no review ledger, signed human attestation, or authenticated UI action in this proposal. The importer cannot know whether a trusted caller actually performed a new human review when that caller refreshes all fields. Re-review is a caller/workflow responsibility, explicitly not a machine-enforced guarantee. A stronger guarantee would require a separately accepted trusted review-action receipt workflow.

No automatic source form detector feeds accepted reviews. No private source artifact publication.

This document specifies the design. The companion foundation has synthetic test
evidence; this documentation-only revision changes no source or tests and
reprocesses no production drawing. No actual-source recognition fix is claimed.

## 11. Authority and immutability clarifications

Independent acceptance of the bounded foundation covers snapshot issuance,
strict validation and the pure complete-support evaluator. It is not acceptance
of production admission filtering or a claim that a real review workflow exists.
Issue completion and production readiness are different decisions.

Receipt identity has two separate meanings:
- producer_receipt_id is a stable content-bound identifier derived from the canonical RenderContext payload excluding that ID; it can match a faithfully regenerated preview. Its hash includes exact source/frame/profile/renderer/output evidence. It is inert serializable data, not authority.
- Runtime issuance identity is a separate internal capability belonging to the currently issued snapshot/preview receipt. It is never serialized, reconstructed from producer_receipt_id or accepted from JSON. Matching a stable receipt ID is insufficient without an issued runtime receipt whose actual content verifies it.

Snapshot immutability must be deep: freeze or defensively copy nested observation mappings, metadata, sequences, frame records and receipt payloads, not merely top-level dataclass fields. Fingerprints are recomputed/checked against the retained immutable inventory at validation. Mutation attempts and equal-valued forged contexts are explicit negative tests. Hostile arbitrary code within the trusted Python process remains outside the stated authority boundary.
