# Explicit project placement assertions

Use materialize_asserted_symbols in
oabm.importers.pdf_electrical.placement_assertions to require field-qualified
evidence before admitting reviewed electrical symbol anchors. It validates a
caller-supplied assertion review, then calls the existing source-verified
materialize_reviewed_symbols API. It does not alter the canonical schema,
provider routing, recognition, source coordinates or older intake entrypoint.
Applications must deliberately adopt this opt-in gate.

This is a trusted application input API, not authentication or independent
verification of a designer, measurement, or semantic interpretation. The
application must authorize the caller; provider output cannot supply review.
Agent review uses reviewer_derivation="inferred"; only an actual authorized
user decision uses "user". Hash checks bind supplied bytes and snapshots;
they do not prove a measurement or interpretation true.

## Required per-device assertions

Each PlacementAssertion binds a stable assertion ID, exact proposal ID and full
fingerprint, field/value/unit, canonical frame, reference ID, basis, role and
evidence. All six fields are required for requested acceptance:

| Field | Value/unit | Basis | Reference ID |
|---|---|---|---|
| registration_xy | six coefficients; m/pt;m | displayed_bottom_pt_to_canonical_m | registration fingerprint |
| level_elevation | scalar m | canonical_datum | envelope canonical_datum_id |
| anchor | two points; pt | displayed_bottom_pt | registration fingerprint |
| elevation | scalar m | above_level or absolute_in_datum | level ID or canonical datum ID |
| yaw | scalar rad | canonical_product_axis | canonical frame ID |
| entity_type | type; token | canonical_type | device or equipment |

Affine order is (m11,m12,m21,m22,tx,ty): the first four coefficients are metres
per displayed point; final two are metres. Registration uses the existing
PdfPageTransform; the gate does not estimate transforms.

Roles: source_observed, field_measured, project_defined, designed,
diagram_convention, unknown. Source-observed means read from the document,
never automatically installed/as-built. Measured facts require evidence with
source_kind="field-measurement" and observed derivation. Source-observed facts
require an actual verified PDF image locator and observed evidence derivation.
Legend examples can support type interpretation, but cannot establish observed
or measured physical placement. They can remain visible on unknown assertions
or as context for a separately documented project design decision. Measured facts require measurement_record evidence; designed and
project-defined facts require an explicit project_decision record.
Review derivation remains separate from evidence derivation.

Project-defined may define registration and level datum; it cannot supply
physical device height, anchor or product orientation. An authorized designer
can supply designed placement, preserved explicitly as designed. Diagram
conventions cannot satisfy required physical fields. Unknown values remain
null. A legend example or unassociated label must not become a physical height.

## Evidence, frames and datums

PlacementEvidence binds immutable document SHA-256, source identity, canonical
Provenance, locator and explicit evidence kind (source_fact, legend_example,
measurement_record or project_decision). Supply original bytes in evidence_documents keyed by
digest; source PDFs can reuse documents. PDF evidence also binds image ID,
original one-based page and displayed bottom-origin point bounds. They must
match source-verified images in the saved run, lie within the supplied crop,
and cover asserted coordinate/control points. Coordinate evidence must belong
to the proposal's source page. Locator prose remains a reviewed assertion,
not automatic semantic verification.

Observed/measured registration needs two fitting controls plus an independent,
non-collinear check, with explicit tolerance in metres. The actual affine
recomputes residuals; controls must lie within the registration region.
Project-defined/designed transforms are explicit authorized definitions,
not measured claims. Any supplied controls are still checked.

Every envelope names one canonical vertical datum for its frame. An explicitly
project-defined local zero is permissible, never silently a surveyed elevation.
Room/suite labels do not define elevations. Above-level means the actual level
plane. Absolute-in-datum must name the envelope datum and equal the level
elevation plus the supplied SymbolPlacement height_above_level_m.

AFF, ceiling-relative and other bases deliberately defer in this minimal gate.
The caller must first establish/review their relation to the level and submit
a justified level-relative or absolute assertion with complete evidence.
No ceiling height, finish offset, mounting height or yaw receives a default.
Even a symmetric representation requires an explicit designed orientation
decision here; a diagram convention does not satisfy physical yaw.

## Legitimate project design when plans omit facts

An authorized project designer can create an immutable design record defining
value, unit, datum, exact device association and rationale. Supply its bytes and
provenance with evidence kind project_decision; mark placement fields designed. A local datum is project_defined
on level_elevation, referencing the envelope datum. Never label these decisions
as field measurements or observed installation.

Construct normal SymbolRegistration, SymbolPlacement and SymbolDecision
records, then matching PlacementAssertions. The complete test setup is an
executable synthetic example. Capture the review as follows:

    assertion_review = PlacementAssertionReview.capture(
        model, review, decisions=decisions, registrations=registrations,
        canonical_datum_id=project_datum_id,
        reviewer_id=authorized_reviewer_id, reviewer_derivation="user",
        rationale=review_rationale, assertions=assertions,
    )
    outcome = materialize_asserted_symbols(
        model, review, runs=runs, documents=documents, images=images,
        journal_paths=journal_paths, decisions=decisions,
        registrations=registrations, assertion_review=assertion_review,
        evidence_documents=immutable_evidence_by_sha256,
    )

The envelope binds the exact canonical model, rebuilt source review, decisions
and registrations. Stale source/model/proposal/evidence/decision bindings fail
atomically. Missing, conflicting, unknown, diagram-only or incompatible required
facts turn requested acceptance into a visible deferral. Acknowledging an
original unknown does not waive these checks.

Original requested decisions, assertions and reason codes are retained in
model.attributes.placement_assertion_reviews. Admitted entities retain their
associated assertions and envelope fingerprint, field-scoped provenance,
evidence roles and original source
identity. Confidence cannot exceed original source/level/registration evidence
or the weakest assertion evidence. Existing entities are never overwritten.

This admits reviewed anchors only: dimensions, hosting, topology, circuits,
scope/quantity policy, complete coverage, split localization and code compliance
remain separate. Unknown split children must first become separately localized
source-bound proposals. This helper never divides a parent box or copies its
anchor. No generic code or engineering defaults are introduced.

Tests use independently authored synthetic PDFs through real dispatch journals,
review rebuild and canonical intake, with injected in-process stub adapters
only. They do not measure provider accuracy or include private plans.
