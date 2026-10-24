# Saved-review electrical delivery runner

The function deliver_reviewed_electrical in oabm.pipeline composes existing APIs:

1. Rebuild saved symbol review from immutable PDF/image context and trusted journal paths.
2. Apply the explicit per-field placement assertion gate.
3. Generate the existing scope ledger and model-derived quantity inventory.
4. Generate confidence review, GLB and its verified review bundle.
5. Optionally export IFC using the existing adapter.

It performs no provider dispatch, routing, engineering calculations, uploads,
delivery-file writes or production integration. Existing source validation reads
the caller-selected saved journal files. References are labels, never fetched.
The returned model is the existing canonical BuildingModel, not a second model.

## Explicit caller inputs

Supply the original base model, saved SymbolReview/SymbolRuns, actual source
PDF/image bytes, trusted journal paths, SymbolRegistration/Placement/Decision
records, PlacementAssertionReview and immutable evidence documents. The
assertion review must bind this exact base snapshot; passing the previous
output model with the old review is stale and fails.

The caller also supplies:
- stable delivery_id, explicit ScopePolicy and ReviewConfig;
- proposal_groups (group -> reviewed proposal IDs);
- canonical_groups (group -> existing base-model entity IDs);
- source_references ((source_kind, source_id) -> caller-authorized reference);
- artifact_references, with distinct labels for exactly canonical_model,
  quantities, scope_ledger, confidence_review, source_map, glb, review_bundle,
  and ifc only when requested;
- explicit glb_options (an empty mapping deliberately selects exporter defaults)
  and include_ifc boolean;
- coverage_report=None.

A canonical model name is required when IFC is requested, as required by the
existing IFC adapter. Its optional runtime dependency must be installed for an
IFC request; absence is a failed export, not an empty successful output.

There is no accepted coverage integration in this slice. Passing any asserted
coverage object fails rather than trusting self-attested completeness. None is
required explicitly, and the report retains unavailable source coverage.
A later source-bound coverage API requires a separately coordinated integration.
This runner neither imports nor publishes an unmerged scope/coverage contract.

## Quantity grouping without guessing canonical IDs

Proposal group membership resolves through the actual intake dispositions.
Duplicate proposals resolving to one canonical ID count that object once in the
same group. Conflicting groups for one surviving object fail. Deferred/rejected
requests without a canonical object remain listed with their original IDs and
status. Canonical groups refer only to entities already in the base model.

Every resulting quantity line requires an explicit inventory group; the runner
does not allow omitted membership to become an implicit ungrouped base bid.
Existing quantity computation and its warnings remain unchanged. Group names
are caller labels, not proof of bid eligibility. Scope classification and all
field-qualified design evidence remain separate from those labels.

The source-reference input is a registry. The runner records unused keys and
selects exactly the current canonical review's provenance keys for the existing
strict bundle validator. Missing needed keys fail. Source map bytes and hashes
match the bundle's actual mapping, and all artifact references/content hashes
are recorded.

## Results and honest completion

ReviewedElectricalDelivery contains immutable artifact bytes, model_json, and
completion_json. Its model and completion properties decode fresh copies; edits
to those copies do not change the saved delivery. The runner writes nothing.
The authorized application owns private persistence and access control.
Same saved inputs, options, references and delivery ID produce identical output
bytes and report. No fresh timestamps or generated run IDs are introduced.

The completion report distinguishes:
- which stages actually passed, failed, were not run or were not requested;
- bytes produced in memory and exact SHA-256 identities/reference labels;
- source proposal dispositions, deferred assertion reasons and source coverage;
- total canonical inventory versus newly admitted electrical IDs;
- scope partitions, explicit group bindings, warnings and unmaterialized requests;
- per-field designed, project-defined, observed or unknown roles;
- missing physical dimensions/hosts and unevaluated connectivity/routing/engineering.

delivery_ready means the requested artifact payloads were computed successfully.
It never means source recognition is complete, quantities are base-bid eligible,
the model is buildable, code compliance is verified, or OfficeAdmin integration
is live. These broader claims remain false/not established in this slice.
An empty model after deferral is not a confirmed empty drawing.
Saved transport/request results remain in saved_run_results, with failed runs
explicitly unresolved. Source-review stage success means saved source bindings
were verified, not that a failed recognition attempt succeeded. Such a saved
run may still produce review-only artifacts without any source-completion claim.

Legacy quantity design_status summaries are retained; the runner separately
exposes per-field placement roles from canonical provenance. It never relabels
designed inputs as measurements or turns a diagram convention into physical Z.
It also does not rerun convergence after intake and silently move an approved
pose. The base frame/level/architecture and registration must already be supplied.

On failure, ReviewedElectricalDeliveryError retains the failing stage, a
machine-readable completion_json and the original exception as __cause__.
Previously computed in-memory artifact identities can appear in that failure
report, but no result/payload set is returned and delivery_ready stays false.
This is a failed delivery, not a successful partial job or a suppressed source
validation error.

## Minimal call

    from oabm.pipeline import deliver_reviewed_electrical

    result = deliver_reviewed_electrical(
        base_model, saved_review, delivery_id=stable_delivery_id,
        runs=saved_runs, documents=pdf_bytes_by_digest, images=image_bytes_by_id,
        journal_paths=trusted_paths_by_request_fingerprint,
        decisions=reviewed_decisions, registrations=reviewed_registrations,
        assertion_review=placement_assertion_review,
        evidence_documents=immutable_project_records_by_digest,
        scope_policy=explicit_scope_policy,
        proposal_groups=explicit_proposal_groups,
        canonical_groups=explicit_existing_inventory_groups,
        review_config=explicit_confidence_config,
        source_references=authorized_source_registry,
        artifact_references=caller_artifact_labels,
        glb_options={}, include_ifc=True, coverage_report=None,
    )

All acceptance facts still come from the existing trusted review APIs. An
application must authorize the reviewer; the runner adds no authentication,
source-of-truth override, physical default, code profile or bid policy.

Tests generate synthetic PDFs and use saved in-process stub dispatch receipts,
not live inference, private drawings or answer-derived geometry. They exercise
the real intake, quantities, review bundle and IFC roundtrip.

## Opt-in electrical review plans and panel facts

The optional `review_drawings` argument takes a typed
`oabm.pipeline.review_drawings.ReviewDrawingConfig`. Omission or explicit `None`
keeps the previous delivery bytes, report shape and requested artifact set.
This is a bounded #233 review-output connection, not permit documentation or
completion of the broader engineering/document suite.

The configuration has version `review-electrical-drawings-v1`, an explicit tuple
of `ReviewPlan` records and finite positive `pixels_per_model_unit`. Each plan
requires `id`, `level_id`, `bounds_m=(min_x,min_y,max_x,max_y)`, `cut_height_m`,
`view_depth_below_m`, `view_depth_above_m`, `scale`, and explicit boolean
`show_architecture`, `show_spaces`, `show_routes`, `show_dimensions`. No physical
or engineering parameters are inferred from the view choice. Unknown levels,
nonfinite values, booleans masquerading as numbers and duplicate view IDs fail.

The existing generator selects from level elevation minus below depth through
level elevation plus cut height plus above depth. A default narrow plan could
hide ceiling fixtures: this opt-in boundary therefore supplies no implicit cut
or depth. Caller bounds and display scale are presentation decisions, not
source measurements. Routes, when shown, are only existing canonical routes;
this connection performs no routing or circuit assignment.

Generation consumes the exact post-admission canonical snapshot used by
quantities, GLB and IFC. It invokes `generate_drawing_set` with the existing
compact labels and default semantic schedules disabled, then explicitly adds
`panel_report_to_drawing_schedules(generate_panel_schedule_report(model))`.
The original canonical model is not changed. Only exact `panelboard` ownership
and existing canonical circuit facts feed the panel report. Missing load values
stay null/unknown; an existing known-load arithmetic subtotal of zero is not a
claim of zero building load. No slots, phases, breaker sizes, demand factors,
missing circuits, code profiles or load calculations are introduced.

Additional artifact keys are `drawing_set`, `panel_facts`, and
`review_plan:<view-id>` for each requested view. Caller artifact references must
cover these as well as the existing outputs. Drawing JSON wraps the existing
`DrawingSet` in a `drawing_set` field with a `review` status envelope. Panel JSON
similarly wraps the existing strict `PanelScheduleReport` under `panel_facts`;
its review envelope binds the canonical snapshot and explicitly says incomplete
and not permit ready. The original report's semantic fingerprint algorithm
remains separate from the exact canonical JSON digest.

Each SVG has a visible paper banner, **INCOMPLETE REVIEW - NOT FOR PERMIT OR
CONSTRUCTION**, outside the generated model geometry. Existing canonical source
IDs, projection coordinates and symbols are retained. SVG is an inspection
artifact; no PDF issuance, required discipline sheet set or paper-scale printing
workflow is established. Panel drawing-table titles carry the same review label.

The completion report and drawing JSON list each view's actual non-label
`rendered_electrical_ids`, `omitted_electrical_ids`, and
`clipped_electrical_ids`. The last field conservatively identifies rendered
entities whose source extents cross the requested XY/depth window. Existing
solid depth selection does not imply every solid is physically clipped at that
plane; the report records that limitation. Omissions can reflect another level,
depth, bounds, disabled route visibility or unsupported presentation and never
establish source absence. The candidate inventory covers devices, equipment,
boxes, support components/interfaces, routes and fittings; circuits/conductors
are separate facts and are not claimed as drawn physical objects. A text label
alone does not count as rendered geometry.

Deferred/unadmitted proposal IDs remain in the completion report's
`review_drawings.source_proposals_not_admitted`; they do not become plan symbols.
An explicitly empty plan tuple emits no SVGs, retaining a no-views status and
panel facts without inventing a level or a blank placed floor. Source coverage
stays unavailable and physical completeness unverified for every review package.
A requested drawing failure fails the delivery rather than returning partial
success. Atomic local persistence uses the same whole-delivery transaction.

### Saved-input entrypoint

The existing `python -m oabm.pipeline.saved_electrical` invocation accepts the
unchanged `saved-electrical-input-v1` format and the explicit
`saved-electrical-input-v2` format. V2 has the same required keys plus
`review_drawings`: either null (disabled), or an ordinary `{path, sha256}` file
reference to `ReviewDrawingConfig.to_dict()` JSON. Unknown versioned fields fail;
v1 cannot silently enable a drawing extension. The configuration bytes are
hash-checked and included in the input receipt like the other supplied files.

Enabled outputs add `drawing-set.json`, `panel-facts.json` and
`review-plan-001.svg`, `review-plan-002.svg`, etc., ordered by explicit view ID.
View IDs are data, never output filenames or path components. The completion
report maps the `review_plan:<view-id>` artifact keys to those final paths and
exact hashes; the persistence marker covers all additional files. A requested
generation error or even a late SVG write failure prevents the full directory
from being published. Existing version reservations, failure receipts, ancestor
syncs and no-overwrite/no-delete behavior remain unchanged.

SVG view identifiers must satisfy the XML 1.0 character ranges. Forbidden control
characters, surrogates and U+FFFE/U+FFFF fail saved-input decoding. Valid tab,
newline and carriage-return characters are encoded as attribute character
references, preserving exact identities instead of letting XML normalize them
to spaces. The final generated SVG is parsed before being returned, and its
`data-view-id` must roundtrip exactly; invalid canonical label text also fails
the requested delivery before publication.

The opt-in drawing JSON enriches each `source_index` entry with the complete
canonical owner's provenance records, preserving optional `scope_paths`, absent
legacy derivation fields, nested provenance attributes and designed/observed
placement roles. `provenance_owner_collection` identifies where those qualifiers
apply; they describe the canonical owner, not the whole view or an arithmetic
subtotal. Geometry and entity attributes remain authoritative in the paired
`canonical_model` artifact. `review.complete_model_evidence` names that artifact
kind and its exact SHA-256, also retaining access to model-wide header evidence.
This enrichment is confined to the opt-in package; the shared drawing generator
and its existing summary representation are unchanged.

### Circuit, revision and persisted-job integration

The existing circuit-only `saved-electrical-design-input-v1` contract remains
unchanged. To request both circuit replay and optional review drawings, use
`saved-electrical-design-input-v2` with both `circuit_design` and
`review_drawings` keys in addition to the base fields. Each option is explicitly
null or its existing path/SHA-256 reference. Unknown fields still fail. The
drawing-only V2 and both previous V1 contracts retain their original required
key sets; omitted/disabled drawing options do not change legacy outputs.

The shared saved-input reader now loads drawing configuration for these opt-in
versions. A drawing-enabled published delivery retains its exact configuration
bytes at `drawing-inputs/config.json`, covered by the completion marker and input
receipt. Saved-revision verification reconstructs all drawing/panel/SVG bytes
from the committed canonical model and retained configuration, checks the view
status and unadmitted source list, and retains the existing circuit replay,
quantity, source-lineage and export checks. Rehashing altered drawings is not a
substitute for this replay. No original configuration path is consulted.

The persisted job runner preserves a supplied drawing option when compiling its
circuit-enabled saved manifest. A view-only change reuses the completed provider
proposal and reviewed circuit choice, while creating a fresh export attempt.
The older uncertain-call, ownership, explicit-retry and directory-durability
barriers remain unchanged.

### Optional physical-sheet artifact package

See [Saved physical-sheet delivery](saved-sheet-delivery.md) for the explicit
versioned paper/glyph configuration, SVG/DXF maps, deterministic generated-source
ZIP and replay checks. This does not change legacy outputs or upload to OfficeAdmin.
