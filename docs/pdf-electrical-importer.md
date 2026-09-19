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

The canonical v1 model permits only one coordinate frame per document, so the
importer never places unrelated page-local coordinate systems into the same
canonical model.

A single-page electrical PDF can be imported before architectural registration.
Its PDF points are converted to metres in a dedicated page-local canonical
frame, and entity attributes mark that geometry as
`single-page-local-unregistered`. The source-page `z=0` plane is not a
claimed mounting elevation. Mounting height, when stated by the plan, is
preserved separately in `attributes.pdf_electrical.mounting_height_m`.

A multi-page PDF requires an explicit `PdfPageTransform` for every page before
canonical objects are emitted. Every transform must target the same canonical
`frame_id`. If those transforms are missing, incomplete, or target different
frames, import fails rather than creating falsely coincident geometry.
Architectural convergence can supply those transforms once sheet registration
is known. `level_id`, `space_id`, and `host_id` remain null until real
canonical hosts are identified.

## Ambiguity rules

The importer does not create a circuit unless the same text evidence identifies
a source panel, circuit number, and at least one independently recognized load
tag. A circuit callout that merely mentions a panel or device does not
materialize that object at the callout position. Incomplete circuit evidence is
retained under `model.attributes.pdf_electrical.unresolved_circuits`. Repeated
callouts for the same semantic source panel and circuit number are merged into
one canonical circuit. Their load ports and provenance are unioned
deterministically, and conflicting scalar electrical evidence is preserved as
explicit ambiguity instead of being selected by extraction order.

A symbol is materialized only when the symbol catalog yields one clear
classification and the importer also has stable semantic or native identity.
Unknown or tied classifications are retained under `unresolved_observations`,
including the candidate types and confidences. Recognized symbols without stable
identity are also retained there rather than receiving counter-derived
canonical IDs. Bare device-class labels such as an unnumbered generic receptacle
or light abbreviation are class evidence, not instance identity, and therefore
remain unresolved by default.

For real plan families that repeat a legitimate symbol but expose no stable PDF
native identifier, callers may supply `ElectricalInstanceHint` entries. Each hint
must provide a caller-owned stable semantic `identity_key`, the source page and
position, and the canonical device/equipment type. A hint may also claim one
existing source element for traceable provenance. Hints never weaken the default
fail-closed behavior: they are explicit human/project inputs analogous to scale
or registration overrides, and stale claimed source elements are rejected.

Host words such as `WALL MTD` are hints only. They never become a canonical
`host_id` without a real canonical host object.

## Stable identity

`import_pdf(..., source_id=...)` should receive a stable document identity when
one is available. Canonical electrical entity IDs are derived from stable
semantic tags or stable source-native identifiers, never extraction order,
text counters, graphics-operator counters, or mutable coordinates. Counter-based
source element IDs remain useful provenance only.

A recognized graphical symbol that has neither a stable semantic tag nor a
stable native identifier remains unresolved instead of receiving an unstable
canonical ID. When a stable native annotation identifier is present, it can be
used as the identity key.

If no source ID is supplied, the extractor uses a SHA-256 identity for the exact
PDF bytes. That guarantees repeatability for an unchanged file but intentionally
treats a revised PDF as a new source version. Callers that need identity to
survive unrelated PDF revisions must provide the same stable `source_id`.

## Symbol recognition

The built-in catalog recognizes common semantic names in form XObject names and
stamp metadata. It intentionally treats a bare `SW` as ambiguous. Projects with
different CAD export names can pass their own `SymbolRule` sequence without
changing the canonical model contract.

The extractor also records a deliberately small family of ordinary stroked
straight-line vector paths. A simple closed rectangular outline can reinforce a
single already-recognized tagged electrical entity and adds source provenance,
but vector geometry by itself never creates or classifies an electrical object.

## Vector topology

Open stroked straight-line paths can contribute circuit intent only when one
connected component has unambiguous endpoint attachment to exactly one
recognized panelboard/switchboard and at least one independently recognized
device. Endpoint snapping is bounded and deterministic; T-junctions are joined
only when a path endpoint actually reaches another path. Crossing lines without
an endpoint junction are not treated as connected.

A nearby circuit callout may contribute a circuit number, voltage, poles, or
phase to that topology. Repeated text and vector evidence for the same source
panel/circuit is merged into one canonical circuit with unioned load ports and
provenance. A topology-only component may emit a circuit with no circuit number
when the source/load relationship itself is unambiguous.

The importer does not turn these source lines into canonical routes and does not
populate explicit port-to-port physical connectivity. It emits canonical owner
ports plus circuit source/load intent for the routing lane to consume. Multiple
possible source equipment, ambiguous endpoint attachment, conflicting nearby
circuit numbers/panel tags/load tags, or a dangling recognized endpoint are
retained under `model.attributes.pdf_electrical.unresolved_topology` and do not
materialize a circuit. When vector topology contradicts nearby text-derived
circuit intent, every implicated text/topology circuit bucket is quarantined
before canonical circuits and ports are finalized. Ports that would exist only
because of the suppressed connectivity are omitted, while the unresolved row
retains the source vector and circuit-callout IDs.

Curved vector paths, fill-only shapes, and unassociated drafting geometry are
outside this conservative recognition family.

## API

`extract_pdf(path)` produces deterministic source observations from text,
form XObjects, and supported PDF annotations.

`ElectricalPdfImporter.import_document(document, page_transforms=...)`
recognizes an extracted document and returns a canonical `BuildingModel`.
Multi-page documents require transforms for every page. Construct the importer
with `instance_hints=(...)` when explicit stable identity must be supplied for
source instances that otherwise have only repeated generic class labels.

`ElectricalPdfImporter.import_pdf(path, page_transforms=...)` performs both
steps.

The checked-in fixtures under `fixtures/pdf_electrical/` are synthetic and
contain no customer plan data. `vector-topology-sheet-e1.json` exercises the
public-safe rectangular-marker and straight-line topology family.
