# Electrical importer branch handoff

The optional `architecture_document` argument on electrical import functions
accepts the actual architecture extraction accompanying `wall_evidence`.
The handoff is checked against PDF byte identity, source observations and the
displayed page frame before recognition. Supplying an extraction without wall
evidence is refused. Existing calls without this argument retain their previous
path; the legacy wall-conflict diagnostic now reads its evidence kind from the
wall-evidence envelope rather than the chain object.

With the handoff, every arrow-seeded component is checked through the shared
branch discriminator before any circuit is created. The complete returned vector
set feeds the existing load, enclosure, annotation, panel and schedule checks.
Rejection retains component, arrow, wall-chain and wall-source IDs in
`attributes.pdf_electrical.branch_geometry_checks`. Rejected vector, load and
annotation identities also block direct-tag, text-link and topology materializers.
The rejection sets are complete before materialization begins, so processing order
does not let a different path rescue a rejected component.

The discriminator's `architecture_evidence_status` is copied verbatim:
`missing_handoff`, `supplied_empty`, or `supplied_positive`. None establishes
complete architectural coverage. Eligibility
is only absence of a supported rejection; it does not establish full architecture
coverage or authorize circuit construction on its own. A single wall handoff covers
its specified page. Branches on other pages retain the helper's unavailable status.

Generated-PDF regressions cover long and densely segmented valid runs, interior
wall crossing with a nearby direct circuit tag, dimension arrows, stale evidence,
missing source IDs, incompatible frames, wrapper forwarding and deterministic
serialization. No private project acceptance, route quantities or compliance
certification follows from these checks. Issues 72 and 76 remain open.
