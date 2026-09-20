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

A multi-page PDF no longer fails solely because registration transforms are
absent. Without caller-supplied transforms, each page is placed in the same
document-local working frame with a deterministic 100 m X offset between page
tiles. This is explicitly marked `multi-page-local-best-effort-unregistered`;
model provenance records the synthetic transform for every page, and no
cross-page or building registration is asserted. This prevents unrelated
page-local coordinates from collapsing onto one another while still allowing
per-page recognition to proceed. When explicit `PdfPageTransform` values are
supplied they must still cover every page and target one canonical `frame_id`.
Architectural convergence can replace best-effort placement once sheet
registration is known. `level_id`, `space_id`, and `host_id` remain null until
real canonical hosts are identified.

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

A source glyph or named symbol is materialized only when recognition yields one
clear classification and a stable identity anchor. For drawn vector glyphs, that
classification can come from a unique match against the sheet's own legend and
the source-geometry fingerprint supplies the fallback identity. Unknown, tied,
or unmatched classifications are retained under `unresolved_observations` and
are never guessed. Bare device-class labels such as an unnumbered generic
receptacle or light abbreviation are class evidence, not instance identity, and
therefore remain unresolved unless they reinforce an independently recognized
glyph.

For real plan families that repeat a legitimate symbol but expose no stable PDF
native identifier, callers may supply `ElectricalInstanceHint` entries. Each hint
must provide a caller-owned stable semantic `identity_key`, the source page and
position, and the canonical device/equipment type. A hint may also claim one
existing source element for traceable provenance. A claimed source must identify
one unique observation, agree with the hint position within the configured
source radius, classify compatibly with the hinted device/equipment type, still
lack a stable source identity, and be owned by exactly one hint. Semantic,
position, appropriateness, and duplicate-ownership mismatches reject the hint
without suppressing normal recognition of the original source observation.
Hints never weaken the default fail-closed behavior: they are explicit
human/project inputs analogous to scale or registration overrides, and unknown
claimed source elements are rejected.

Host words such as `WALL MTD` are hints only. They never become a canonical
`host_id` without a real canonical host object.

## Stable identity

`import_pdf(..., source_id=...)` should receive a stable document identity when
one is available. Canonical electrical entity IDs are derived from stable
semantic tags, stable source-native identifiers, or, for a geometry-only
legend-matched instance, a deterministic fingerprint of that instance's
absolute source geometry and page. They never depend on extraction order, text
counters, or graphics-operator counters. Counter-based source element IDs remain
provenance only. A semantic tag, when present, remains the stronger identity
anchor. Moving or redrawing a geometry-only source glyph intentionally changes
its source-geometry fingerprint, while unrelated extraction-ID changes do not.

A recognized form/annotation symbol that has neither a stable semantic tag nor
a stable native identifier remains unresolved instead of receiving an unstable
canonical ID. A drawn vector glyph may establish identity only through the
sheet-legend shape path described below. When a stable native annotation
identifier is present, it can be used as the identity key.

If no source ID is supplied, the extractor uses a SHA-256 identity for the exact
PDF bytes. That guarantees repeatability for an unchanged file but intentionally
treats a revised PDF as a new source version. Callers that need identity to
survive unrelated PDF revisions must provide the same stable `source_id`.

## Symbol recognition

The primary path for drawn power-plan symbols is geometry matching against a
detected legend block. The importer clusters small nearby vector paths into
candidate glyphs, including the filled and Bézier geometry retained by Slice 1.
A legend block is accepted when either (1) a nearby title ends in `LEGEND`,
`SYMBOL`, or `SYMBOLS`, case-insensitively, including a title attached to a
leader, or (2) the page contains the densest table-like run of at least three
aligned small glyphs, each followed by a short text label within the fixed
horizontal legend-label distance. The density fallback also requires repeated
glyph signatures elsewhere on the same page so ordinary title-block and drafting
geometry do not become legend prototypes merely because text is nearby.

Within the selected block, the importer associates each unambiguous type label
with its paired glyph, builds a translation/scale/quarter-turn invariant shape
signature, and matches field glyph clusters against those prototypes. Legend
prototypes remain page-local by default. A page without a recognized local legend
does not inherit prototypes from another sheet and retains explicit same-page
unresolved provenance. Cross-sheet matching is allowed only when source text on
the field sheet explicitly references one uniquely identified legend sheet, for
example `SEE E-001 FOR LEGEND` or `SEE GENERAL NOTES AND LEGEND`. The field
device then carries `pdf-explicit-legend-sheet-reference` provenance on the field
page plus the legend-label provenance on the referenced page. Ambiguous or
missing references fail closed, so there is never silent shared-legend
inheritance. A unique match emits the legend's canonical device/equipment type
with `pdf-legend-shape-match` provenance from both field geometry and the legend
label. Conflicting legend definitions fail closed, and unmatched glyphs remain
unresolved rather than guessed. Detection details are exposed in
`attributes.pdf_electrical.legend_recognition`.

The built-in text catalog remains available as additional evidence and as a
fallback for sources with explicit stable semantic labels. It also recognizes
common semantic names in form XObject names and stamp metadata. It intentionally
treats a bare `SW` as ambiguous. Projects with different CAD export names can
pass their own `SymbolRule` sequence without changing the canonical model
contract. Nearby text may reinforce a legend-matched glyph and provide a stable
tag, but it is not required to classify or locate that glyph.

The extractor also records ordinary stroked straight-line vector paths. A simple
closed rectangular outline can still reinforce one already-recognized tagged
electrical entity and add source provenance. Small glyph clusters are excluded
from circuit-topology interpretation so symbol strokes cannot silently become
wiring.

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

Curved and fill-only source paths are retained as vector geometry observations
instead of being discarded during extraction. Cubic `c`, `v`, and `y` segments
are deterministically flattened with a fixed subdivision count while their source
operator, transformed control points, and endpoint remain in observation metadata.
Observations containing Bézier commands are excluded from the existing rectangular
symbol-outline and straight-line topology families, so curve retention cannot
silently reinterpret a curve as straight circuit topology. Curved or filled paths
may participate only as members of a small glyph cluster that uniquely matches a
sheet-legend prototype. Otherwise they remain unresolved or unassociated drafting
geometry rather than being guessed as electrical objects.

## API

`extract_pdf(path)` produces deterministic source observations from text,
form XObjects, and supported PDF annotations.

`ElectricalPdfImporter.import_document(document, page_transforms=...)`
recognizes an extracted document and returns a canonical `BuildingModel`.
Multi-page documents may run without transforms using explicit best-effort
per-page placement and provenance; supplied transforms must cover every page.
Construct the importer with `instance_hints=(...)` when explicit stable identity
must be supplied for source instances that otherwise have only repeated generic
class labels.

`ElectricalPdfImporter.import_pdf(path, page_transforms=...)` performs both
steps.

The checked-in fixtures under `fixtures/pdf_electrical/` are synthetic and
contain no customer plan data. `vector-topology-sheet-e1.json` exercises the
public-safe rectangular-marker and straight-line topology family.
`geometry-only-power-sheet-with-legend.pdf` is a source-only Slice 4 acceptance
fixture. Its plan field contains drawn glyph geometry only; semantic type text
appears only inside the sheet's drawn legend. It has no paired expected-output
artifact.
