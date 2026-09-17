# Electrical PDF importer

The electrical PDF lane lives in `oabm.importers.pdf_electrical`. It recognizes
electrical meaning from a PDF and emits only canonical `oabm.model` v1 objects.

## Scope

This lane owns recognition of:

- distribution equipment such as panelboards, switchboards, and transformers
- field devices such as EVSE, receptacles, junction boxes, luminaires, switches, and disconnects
- form-XObject and annotation symbol observations
- electrical text, notes, circuit callouts, voltage, poles, phase, mounting heights, and host hints
- source provenance, confidence, stable source-derived IDs, and explicit unresolved evidence

It does not create architectural walls, rooms, levels, routes, quantities, IFC,
or drawing views. Final level, space, and physical host attachment belongs to the
PDF convergence gate.

## Spatial semantics before convergence

The canonical v1 electrical entities require a pose. Before an architectural
registration exists, the importer converts PDF points to metres and records the
symbol or text position in a source-page-local frame. The entity attributes mark
that pose as `source-page-local-unregistered`; `level_id`, `space_id`, and
`host_id` remain null.

The source-page `z=0` plane is not a claimed mounting elevation. Mounting
height, when stated by the plan, is preserved separately in
`attributes.pdf_electrical.mounting_height_m`. If multiple heights are
plausible, all candidates are retained and the status is marked ambiguous.

For multi-page files, page provenance remains authoritative and the model states
that the pages have no asserted building registration. The later architecture
plus electrical convergence step is responsible for transforming and hosting
these recognized objects.

## Ambiguity rules

The importer does not create a circuit unless the same text evidence identifies
a source panel, circuit number, and at least one recognized load tag. Incomplete
circuit evidence is retained under
`model.attributes.pdf_electrical.unresolved_circuits`.

A symbol is materialized only when the symbol catalog yields one clear
classification. Unknown or tied classifications are retained under
`unresolved_observations`, including the candidate types and confidences.

Host words such as `WALL MTD` are hints only. They never become a canonical
`host_id` without a real canonical host object.

## Stable identity

`import_pdf(..., source_id=...)` should receive a stable document identity when
one is available. Canonical IDs are then derived from that source identity plus
stable extraction element IDs. If no source ID is supplied, the extractor uses a
SHA-256 identity for the exact PDF bytes, which guarantees repeatability for an
unchanged file but intentionally treats a revised PDF as a new source version.

## Symbol recognition

The built-in catalog recognizes common semantic names in form XObject names and
stamp metadata. It intentionally treats a bare `SW` as ambiguous. Projects with
different CAD export names can pass their own `SymbolRule` sequence without
changing the canonical model contract.

## API

`extract_pdf(path)` produces deterministic source observations from text,
form XObjects, and supported PDF annotations.

`ElectricalPdfImporter.import_document(document)` recognizes an extracted
document and returns a canonical `BuildingModel`.

`ElectricalPdfImporter.import_pdf(path)` performs both steps.

The checked-in fixtures under `fixtures/pdf_electrical/` are synthetic and
contain no customer plan data.
