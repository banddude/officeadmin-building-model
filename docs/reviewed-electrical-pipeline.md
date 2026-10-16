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
