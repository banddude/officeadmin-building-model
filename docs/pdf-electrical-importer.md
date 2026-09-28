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

Text-led panelboard identity requires a compact `PANEL <tag>` label, optionally
followed by explicit voltage, phase, or ampere ratings. A prose mention of a
panel does not materialize equipment, and common sentence words after `PANEL`
cannot become panel IDs. A bare alphabetic tag must be a recognized panel
designation (such as `LP` or `MDP`); numbered tags or short tags with explicit
ratings are also accepted. An annotation alone cannot establish a panelboard
from its free-form subject or contents. A source without a reliable panel label remains
unresolved; circuit text or proximity does not supply the missing identity.

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
per-page recognition to proceed. When explicit transforms are supplied they
must still cover every page and target one canonical `frame_id`.

A page's value is either one `PdfPageTransform` or a non-empty sequence of
`DrawingRegionTransform(source_bbox_pt, transform)`, one per drawing on a page
that holds several drawings (#72). A recognized point on such a page uses the
transform of the unique region whose bbox (displayed page points) contains it.
If any point lies outside every region, or inside two, nothing is placed by
region: the document keeps the best-effort placement, stays
`registration_pending`, and `drawing_region_assignment` lists the unplaced
points. Placed devices record their `drawing_region_bbox_pt`, and each region's
registration is its own `inferred` provenance entry.
Architectural convergence can replace best-effort placement once sheet
registration is known. Transforms proposed by sheet registration (#104, see
`pdf-convergence.md`) carry a `registration` record; the importer keeps it in
every device's `page_transform` attributes and adds one `inferred` model
provenance entry per page, so the placement is visibly a proposal while the
source positions stay observed. `level_id`, `space_id`, and `host_id` remain null until
real canonical hosts are identified.

PDF extraction normalizes page `/Rotate` values of 0, 90, 180, and 270 degrees
into the orientation shown by a PDF viewer before any electrical recognition
runs. For quarter turns, page width and height are swapped and every text,
form-symbol, annotation, and vector-path coordinate is rotated into the displayed
bottom-origin page space. Per-page extraction provenance records `page_rotation`
and the displayed page dimensions. `PdfPageTransform` always consumes these
displayed coordinates, so caller-supplied registration does not need to account
for the PDF storage rotation separately.

## Symbol shape classes

`oabm.importers.pdf_electrical.shape_classes.symbol_shape_classes(document, page)`
groups a page's vector paths into a deterministic table of small closed-shape
classes: circles (closed bezier-flattened paths with a square bounding box),
triangles and rectangles (closed straight paths with 3 and 4 distinct
vertices; only axis-aligned 4-gons are rectangles), and other polygons
(`polygon<n>`). Each `ShapeClass` row carries `kind`, `size_pt` (the bbox's
larger side, rounded half-even to `size_step_pt`), `filled` (from the paint
operator), bucketed `stroke_gray`/`fill_gray` when the observations carry
them and `None` when they do not, a `count`, and up to five `sample_positions`
(bbox centres, the first five by `(y, x)`). Open paths, non-square bezier
paths, and shapes outside `[min_size_pt, max_size_pt]` are ignored. Classes
sort by `count` descending, then by the class key, so identical pages give an
identical table. The module is read-only observation evidence: the importer
does not call it, imported models are unchanged, and a per-set parameter look
can bind a class to a device meaning later.

Two closure and exclusion rules keep the table usable on CAD exports:

- **Endpoint closure.** CAD exports draw circles as open Bézier paths whose
  end meets their start, with no closepaint operator. A path whose first and
  last written points coincide within 0.3 pt therefore counts as closed
  alongside the `closed` flag, and circles are classified from that. A
  Bézier arc that ends a visible gap away stays open and is ignored.
- **Tessellated-fill exclusion.** CAD solid fills are exported as triangle
  meshes. A closed straight triangle that shares an edge with another closed
  triangle on the page — both edge corners matching the other triangle's
  corners within 0.05 pt — is a mesh cell, not a symbol, and is excluded
  from the table; isolated symbol triangles share no edge and stay. The rule
  is size-independent and only pairs triangles with triangles, so a triangle
  touching a rectangle or a single-corner neighbour is untouched.
  `triangle_mesh_diagnostic(document, page)` documents it per page with
  `closed_triangles_on_page`, `mesh_triangles_excluded`, and
  `symbol_triangles_on_page` counts.

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

For explicit homeruns, the arrowhead is recognized by its open, nondegenerate
acute V shape, independent of its absolute point size. Any nonzero straight
branch stroke may participate. The arrowhead and strokes use the configured
topology snap radius for contact, so an extra fixed point allowance cannot
change the circuit verdict at a separate boundary. A branch touching an
unclaimed closed path fails as `branch_run_not_isolated`; a recognized device's
own outline remains a lawful endpoint. A panel schedule heading makes that
panel's schedule present even if no row parses. A schedule validates circuits
only when the source draws a bounded two-column table: a title cell containing
the heading, separate circuit-number and load-description header cells, a
continuous vertical divider, and paired number/description row cells sharing
the header's column edges. Circuit numbers must be alone in their left cells.
The shortest unambiguous frame owns the rows. A single-column heading or notes
box remains present but has status `structure_not_confirmed`, validates no
circuits, and emits `schedule found, structure not confirmed` in diagnostics.
This conservative rule reduces automatic recall for minimally ruled or
single-column schedules; a later human-confirmed-region workflow can recover
those sources without making unsupported circuit claims. A direct device
circuit tag has no leader to follow, so it uses the lane's
configured annotation association radius and requires one unambiguous recognized
device in range.

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
A legend block is accepted when either (1) the nearest section heading above
or beside the candidate block ends in `LEGEND`, `SYMBOL`, or `SYMBOLS`,
case-insensitively, including a heading attached by a leader, or (2) the page
contains the densest table-like run of at least three aligned small glyphs, each
paired with a short non-numeric text label within the fixed horizontal
legend-label distance. Title matching fails closed when the heading contains
`KEYNOTE`, `KEYNOTES`, `SCHEDULE`, `PANEL`, `NOTES`,
`ABBREVIATIONS`, or `DETAIL`. The density fallback applies the same
exclusion to the nearest section heading above the cluster and rejects candidate
tables that are mostly numeric or text-only. It also requires repeated glyph
signatures elsewhere on the same page so ordinary title-block and drafting
geometry do not become legend prototypes merely because text is nearby.

Text size means the rendered size, not the raw `Tf` operand. CAD exports often
set a large `Tf` size and shrink it with the text matrix, for example `Tf 60`
with a `Tm` scale of 0.12, which draws 7.2 pt glyphs. The extractor records
`font_size_pt` as the `Tf` size times the length of the text-space y axis after
the text matrix and the current transformation matrix, so a quarter-turn
rotation keeps the size and a `cm` scale applies to it. Without this, such
labels would read as oversized text, fail the 18 pt short-label filter, and the
sheet would yield no legend. The reverse also holds: a small `Tf` blown up by
the text matrix draws large, so it records its large drawn size and is not a
legend label. A negative `Tf` counts at its magnitude.

For ruled notes-column legends, page-frame detection is deliberately sheet-level: it uses the largest axis-aligned closed rectangle or a rectangle formed by four long rules only when that rectangle covers at least 85% of the displayed media box, and otherwise falls back to the displayed media box. Interior plan/view borders therefore cannot become the sheet frame. The notes-column title-block exclusion is limited to a ruled bottom band and is capped at 12% of the sheet-frame height. If a candidate legend header nevertheless falls outside a detected frame, the importer re-derives the frame from the displayed media box and records that recovery in legend/model provenance.

Within the selected block, the importer associates each unambiguous type label
with its paired glyph and records that source label with the canonical device
type. The built-in legend vocabulary includes duplex and quad receptacles, data
and combination outlets, power and data junction boxes, access-control devices,
and CATV outlets. Field matching is translation-, uniform-scale-, quarter-turn-,
and axis-mirror-invariant, with principal-axis rotation normalization covering
arbitrary printed angles (the same machinery the lighting path uses for skewed
wings; isotropic glyphs with no principal axis de-rotate by the same
fixed-order coarse sweep the lighting path uses). Prototype and candidate strokes are normalized to
their glyph bounds, resampled at fixed geometric density, and compared with a
symmetric chamfer plus Hausdorff distance so CAD block segmentation does not
need to match the legend's graphics operators. A match is accepted when its
nearest score is at least 0.55 with at least 0.12 margin over the runner-up, or
when the nearest score is at least 0.75 regardless of margin. Scores below 0.45
are an absolute rejection floor. Inside the 0.12 margin only, differentiating
stroke count can resolve one unique canonical type; otherwise matching fails
closed. The nearest score and margin are recorded together as match confidence.

Before scoring, field clusters can be cleaned without changing legend
ownership. Leader strokes are removed from oversized clusters, clusters larger
than 2.5 times the largest applicable legend glyph are split by connected
components while interior strokes remain attached to their enclosing glyph,
and undersized fragments can merge with the nearest neighbor within one legend
glyph width. Short field text made only from E/N/R, digits, plus signs, quotes,
and punctuation is stripped from geometric comparison; its source text is
retained as device tags and in recognition diagnostics. E/N/R status text, bare
mounting-height tags, and circuit-count numbers remain field modifiers rather
than legend labels. Every match and every unresolved field glyph records the
nearest prototype score, second-best candidate, margin, thresholds, cleanup
actions, and any non-unique reason in recognition provenance. Legend prototypes remain page-local by default. A page without a recognized local legend
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

### Drafting style on vector observations

Each vector path also records the drafting style it was painted with, read from
the content-stream graphics state at its paint operator: `stroke_gray` and
`line_width_pt` on stroking operators, `fill_gray` on filling operators. Colour
luminance uses the same rule as the architecture extractor's line styles:
DeviceGray directly, DeviceRGB mixed 0.299/0.587/0.114, DeviceCMYK converted to
RGB first, rounded to 4 decimals. The width is the user-space line width under
the CTM at paint time.

The tracked operators are `g`/`G`, `rg`/`RG`, and `k`/`K` (device colour), plus
`cs`/`CS` colour-space switches (the colour resets to that space's initial
value) and `sc`/`SC`/`scn`/`SCN` component sets; `w` sets the line width, and
`q`/`Q` save and restore the whole style state around form XObjects, which
paint with their own resources. A `gs` is honoured for its `/CA` and `/ca`
constant alpha only; a pattern colour (a name or indirect `scn`/`SCN` operand)
or any other uninterpretable colour omits its metadata key instead of guessing.
These keys are recognition input only: the provenance attribute mirrors of
source metadata drop them, so imported model output stays byte-identical.

### Scope of work from stroke gray

Sets that publish no status legend often encode scope in the drafting colour
itself: new work stroked in black, existing work in a set-specific gray. The
read-only helper `scope_by_stroke` in
`oabm.importers.pdf_electrical.scope_by_stroke` applies that rule as a
parameterized, deterministic classification over an already-extracted
document. It changes nothing in extraction or recognition; callers use it
beside the legend-letter `scope_status` reading, not instead of it.

`StrokeScopeRule(new_gray_max=0.1, existing_gray_min=0.2,
existing_gray_max=0.5)` is a validated frozen dataclass: `new` covers
`[0, new_gray_max]`, `existing` covers `[existing_gray_min,
existing_gray_max]`, and the ranges must not overlap, so constructing a rule
with overlapping or out-of-range bounds raises `ValueError`. Grays between or
outside the ranges carry no scope.

`scope_by_stroke(document, points_pt, rule, radius_pt=6.0)` takes one
`(page, x, y)` query per symbol position and returns one verdict per query,
in query order. A verdict weighs the stroke grays of the stroked vector paths
on that page within the query radius — measured to the path segments, so a
mid-stroke symbol still matches — by path length, with the closing segment of
a closed path included:

- `new` or `existing` when at least 80% of the weighed length falls in the
  matching range;
- `ambiguous` when neither range reaches 80%, including when nearby strokes
  sit between the rule's ranges or carry no usable gray;
- `unknown` when no nearby stroked path carries a usable `stroke_gray`,
  including when there are no nearby stroked paths at all.

Only strokes count: filling-only paths are not scope evidence. Every verdict
reports both length fractions and the number of nearby stroked paths, so an
ambiguous or unknown reading always shows the evidence it did (or did not)
have. The function never raises on document data, and identical inputs give
identical verdicts.

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


### Lighting legends, fixture schedules, and letter tags

Lighting recognition is a separate path from the power-device symbol legend. An
explicit heading such as `LIGHTING FIXTURE LEGEND` establishes a lighting
legend only when at least two readable letter-tag rows can be associated with
small glyph prototypes. Tags such as `A`, `A1`, `B`, and `F2` are the
fixture-type evidence. Geometry is supporting evidence only: the same symbol may
legitimately appear under multiple fixture tags, so the importer never chooses a
fixture type from shape alone.

Legend rows are read as table structure rather than as distance from the
heading. The heading anchors the table over its own column, whose labels
define the row grid; a further printed column belongs to the same legend only
when all three of these hold, so no one signal admits a column alone: it
starts right of the table's own tags (the admitted labels' max x plus one
label width -- printed geometry, deliberately not font-sensitive) and within
about one inch (72 pt) past where the admitted table's printed descriptions
are estimated to end (per row, the nearest description text's position plus
its character count times its font size times 0.6, falling back to the label
plus the description span when a row has no description), every one of its
labels continues that grid on a distinct row (table purity), and each of its
rows carries description text to the right the way a printed legend row does
and a bare field tag does not (row evidence). The estimate may legitimately
run past the evidence search span, which is what lets a legend printed with
long descriptions resolve the column beside it, and it may also run long of
the printed text: narrow CAD fonts (RomanS and condensed faces, about 0.45
em) print about a third shorter than the 0.6 em estimate, which is why the
estimated edge bounds only how far out a column may sit and the tag-position
bound decides where a column must start. A two-column legend with the
heading over column 1 therefore still resolves column 2, even at the roughly
340 pt column offset measured in issue #108 or when column 2 starts just
past the true narrow-font description edge measured in issue #111, while a
vertical run of tagged field fixtures on the
grid rows -- even far right of the legend, inside the heading's band -- stays
field evidence and never becomes legend prototypes. A lone tag-shaped label
on no legend row is not table structure either. Every resolved legend label
and row description is claimed as legend evidence, so legend rows no longer
leak into the field-code pass or inflate `unresolved_switch_count`.

An explicit lighting/fixture schedule heading plus a tag/type column and at
least one descriptive column establishes a fixture schedule. Readable rows are
associated by tag and can carry description, lamp, wattage, mounting,
manufacturer, and model/catalog text. Parsed wattage also exposes
`wattage_w` when numeric. Conflicting duplicate schedule rows fail closed.
Recognized luminaires carry the schedule row under
`attributes.pdf_electrical.fixture_schedule` and preserve independent
provenance for field geometry, the printed field tag, the lighting legend tag,
and schedule cells.

Field association is one-to-one and local. A schedule/legend-known tag must be
uniquely adjacent to a small glyph, and a luminaire additionally requires that
tag's own lighting-legend prototype on the same sheet with the adjacent glyph
clearing the glyph match minimum (`_GLYPH_MATCH_SCORE_MIN`) against it. The tag
narrows the candidates to one fixture type, so this is the power-device margin
rule for a single candidate type; the lower absolute floor only marks shapes
that are certainly not the prototype and never confirms one. Scale, mirroring,
arbitrary in-plane rotation, and CAD stroke variation therefore use the
same normalized geometry machinery as power-device matching without making
shape the semantic classifier: beyond mirroring and quarter turns, both clouds
of a comparison are re-expressed in a principal-axis frame (centroid and
root-mean-square radius, then axis alignment), which recognizes fixtures
printed at non-orthogonal angles in skewed wings. Isotropic shapes -- squares,
hexagons, equilateral triangles, plus and X glyphs, circles -- have no
principal axis: their covariance eigenvalue ratio sits near 1.0 and the axis
formula returns resampling noise. Comparisons involving such a cloud
de-rotate by a coarse fixed-order rotation sweep instead (every 5 degrees
over the full turn, ties keeping the smallest angle), so a square troffer or
hexagon printed at an arbitrary angle in a skewed wing resolves against its
axis-aligned legend prototype. The tag remains the only
type evidence. Repeated instances may share a fixture tag but
retain separate stable source-geometry identities.

Chamfer scoring alone leaves round and polygonal shapes dangerously close: a
12x12 square beside an `A` tag scores about 0.567 against a circle legend row,
just over the match minimum. A shape-class guard therefore compares the
boundary's extreme centroid-distance ratio (round glyphs such as circles and
octagons stay near 1.0-1.08; squares and triangles reach 1.41 and beyond) and
lets a round-versus-polygonal disagreement confirm only at the strong-score
bar. Thinner cross-class scores fail closed as
`lighting_fixture_symbol_mismatch` with the guard recorded beside the raw
score. Same-class pairs keep the ordinary match minimum, so an octagon next to
a circle legend row still confirms.

A fixture schedule enriches a confirmed fixture; it never proves one. A
schedule-known letter beside small geometry is exactly what a room name or
grid label looks like, so a tag with no tag-specific legend prototype on the
sheet stays unresolved with `lighting_fixture_symbol_unconfirmed`, whatever
the adjacent geometry looks like. A tag whose adjacent glyph falls below the
match minimum for that tag's prototype stays unresolved with
`lighting_fixture_symbol_mismatch` and its shape score. Lighting claims field
geometry only when the sheet's lighting legend confirms it is lighting-shaped;
geometry it rejects stays available to the power-device path.

Fixture-like geometry with no readable tag remains unresolved with
`lighting_fixture_tag_missing` (or
`lighting_fixture_tag_unreadable_or_unknown` when short unreadable text is
adjacent). Competing tags or non-unique tag-to-glyph association also remain
unresolved with explicit reason codes. A bare schedule-known letter elsewhere
on the sheet, including a room-name lookalike, does not create a luminaire
because it has no qualifying adjacent glyph.

On a confirmed lighting page, exact legible codes `S`, `S3`, `SD`, and
`OS` may classify switching as single-pole, three-way, dimmer, or occupancy
sensor respectively. Like a fixture tag, the code alone is not evidence: `SD`
inside a circle is a smoke detector and `S` in a bubble is a column grid label.
A switch therefore needs one unambiguous adjacent glyph that clears the match
minimum against that code's own lighting-legend prototype on the sheet. A
lighting-legend row labelled with a switching code is that code's switch
prototype only when the row's own description names a switching device
(`SWITCH`, `DIMMER`, `OCCUPANCY`, or `VACANCY`), because `S1`/`S2`/`S3` are
also common strip-fixture type tags. A row's description ends at the next
legend entry in its row band (the first other glyph or tag-shaped label to
the right), so a multi-column legend never lends one row its neighbouring
column's description. Fixture tags always take precedence if a
schedule actually defines one of those strings as a fixture type. A
switch-code legend row that neither a schedule row nor its description settles
stays out of both roles as `lighting_legend_code_role_ambiguous`. A code with
no prototype on the sheet
stays unresolved as `lighting_switch_symbol_unconfirmed`, and a glyph below the
match minimum stays `lighting_switch_symbol_mismatch`. Ambiguous switching
stays unresolved.

This costs recall on sheets that print switch codes without a switch legend,
and the cost stays visible. `lighting_recognition` reports
`unresolved_fixture_count`, `unresolved_switch_count`, and
`unresolved_by_reason` beside the recognized counts, so every legible code
that did not become a switch is counted and explained rather than dropped.

The Slice 16 acceptance tests generate public-safe PDFs at runtime and pass the
generated source through `extract_pdf()`; there is no pre-extracted JSON and
no paired `.expected.json` answer key. The generated sheet covers same-shape
different-tag fixtures, scale/rotation/mirroring, adjacent distractor text,
fixture schedules, switching, a room-name lookalike, a tagless fixture, and
ambiguous/unreadable tags. A separate generated probe puts a schedule-known
room label beside unrelated small closed geometry inside the association
radius: without a lighting legend it yields zero luminaires, and with one the
genuine legend-confirmed fixture beside it is still recognized. A switch probe
puts a smoke-detector `SD` inside a circle and a grid-bubble `S` on a lighting
sheet: both yield zero switches with or without a switch legend, and a genuine
legend-confirmed `S` beside them is still recognized. A legend row `S3` that
describes an LED strip never becomes a switch prototype: a schedule row makes
it a fixture, and without one it stays ambiguous, even when a neighbouring
legend column's `DIMMER SWITCH` shares its baseline.

### Notes-column legend tables and field status

A right-hand notes column is not treated as one monolithic title block. Only the
bottom title-band portion of that column is excluded from the strongest ruled
legend-table path; framed content above it remains eligible. A ruled header row
whose columns are exactly `SYMBOL` and `FUNCTION` is accepted as a strong
legend signal even when a separate `LEGEND` title is absent. Glyphs are paired
with description text from the same ruled row rather than relying on a fixed
horizontal label radius.

Single-letter field modifiers `E`, `N`, and `R` and bare mounting-height
tags such as `+44"` are excluded from legend-label and section-heading
detection. The letters do not alter glyph clustering or shape signatures. When
one unambiguous status letter is adjacent to a legend-matched field glyph, the
device records `attributes.pdf_electrical.status` as the source letter plus a
normalized `status_meaning`, with dedicated source provenance. Existing
page-local legend ownership and the Slice 7 keynote/schedule false-positive
guards remain unchanged.

`status_meaning` is the conventional reading of the letter. The scope of work
(#105) is read from the sheet's own status legend instead, because sets differ:
one uses `R` for "existing to be removed", another for "existing to be removed
and salvaged for relocation". A legend row is a marker letter immediately left
of its meaning on the same baseline (`R  EXISTING TO BE RELOCATED`) or a single
string (`R = EXISTING TO BE RELOCATED`). Every device records `scope_status`:

- `new`, `relocated`, `existing_to_remain` or `removed` when its marker letter
  has exactly one meaning in its own page's legend. The marker and legend
  source element IDs are recorded with it;
- `unresolved` otherwise, with `scope_reason`:
  - `no_scope_marker`: an unmarked device is never assumed new;
  - `scope_marker_undefined`: the page's legend does not define the letter;
  - `scope_legend_conflict`: the page's legend gives the letter more than one
    meaning.

Legends are page-local, like symbol legends; a legend on another sheet is not
inherited.

Unresolved vector glyph clusters retain tuning evidence instead of only a
generic failure string. Each unresolved cluster exposes the nearest and
second-nearest canonical types and scores, the active threshold, a normalized
failure reason, its point-space bounding-box size, and stroke count. The
earlier detailed prototype diagnostics remain present for compatibility.

Short `/Square` annotation contents are also treated as sheet-legend code
evidence. Known abbreviations such as `CR`, `TV`, `J`/`JB`, and `D`/`DATA` resolve only
through classified rows on that sheet, and a short code appearing verbatim in
one legend row can resolve the same way. Successful evidence uses the
`annotation-code` provenance method and preserves an adjacent `E`/`N`/`R` status;
unknown or non-unique codes remain unresolved with the source code recorded.
Long legend descriptions remain semantic labels in full, so an explanatory
trailing sentence does not prevent a leading tele/data J-box phrase from
classifying as `junction_box_data`.

## Device-sheet selection

`oabm.importers.pdf_electrical.sheet_selection.select_device_pages(document,
*, importer=None)` answers one question for public callers (#180): which
sheets of a set carry electrical devices? Per page, in page order, it returns
a frozen `SheetChoice` (`page`, `sheet_id`, `discipline`, `device_count`,
`recognized_device_count`, `symbol_like_count`, `included`, `reason`) wrapped
in a `DeviceSheetSelection` (`pages`, `import_mode`,
`import_fallback_reason`) whose `to_dict()` is deterministic.

The document is imported **once**, with the default importer or the
caller-supplied `importer`, and every canonical device is counted on the page
its recognition recorded (`attributes.pdf_electrical.source_page`, else the
page of its first provenance record). The per-page counts therefore add up to
exactly the devices the caller's own whole-document import produces, and
callers with project-specific `SymbolRule` sets or instance hints get
matching counts. On pages whose symbols are resolved only through a legend on
another sheet, this counts the devices that a page imported in isolation
would miss; everywhere else the counts equal per-page isolation (the tests
assert both).

Selection never raises because the importer refuses the document. The
whole-document import can legitimately raise `ElectricalPdfError`, most often
because the same tagged equipment (for example one panel) is drawn on two
sheets and the importer will not canonicalize one identity from two source
locations. Selection then falls back to importing each page on its own (every
other page's observations removed, page numbering and page provenance kept)
and uses that import's device count. `import_mode` records which path ran:
`"document"` for the single import, `"per_page_fallback"` otherwise.
`import_fallback_reason` is `None` for `"document"`; for the fallback it is the
error class and a fixed short summary, such as `"ElectricalPdfError: the same
stable semantic identity was recognized at multiple source locations"`, and
never the importer's message, which quotes source tags. Unrecognized refusals
get the generic summary `the whole-document import refused the document`. If a
page's own import also raises, its device count is 0 and an electrical or
unknown-discipline page is excluded as `import_failed`; other disciplines keep
`<discipline>_sheet`. Only `ElectricalPdfError` triggers the fallback; any
other exception is a bug and propagates.

`device_count` and `recognized_device_count` are the same number: devices the
importer *recognized* on the page. The count is only as good as the importer's
recall on the set, so `symbol_like_count` sits next to it: the page's symbol
observations plus its tag-like text observations. A text is tag-like when it
is a single token of at most ten characters that is either a one-to-three
character code (`a`, `WP`, `GFI`, `$3`) or a letter-led tag with a digit
(`RECEPT-1`, `D1`), and is not a printed sheet id. Common short words and
drafting abbreviations that fit the code shape are never tag-like, compared
case-insensitively: `AND`, `THE`, `ALL`, `FOR`, `OF`, `SEE`, `NOT`, `TO`,
`AT`, `IN`, `ON`, `BY`, `OR`, `NO`, `AS`, `IS`, `BE`, `IF`, `UP`, `SET`,
`PER`, `VIA`, `TYP`, `EQ`. Without them, an extractor that emits general
notes word by word would inflate the count on notes-only sheets. A lone `A` is
deliberately not a stop word, because `a` is a common switch-leg code. An
electrical sheet with
zero recognized devices and zero symbol-like observations is empty; one with
zero recognized devices and many symbol-like observations most likely uses
symbols the importer does not recognize. `symbol_like_count` is a disclosure
only and never changes a verdict.

The discipline comes from the printed sheet-number prefix. A printed sheet id
is a discipline prefix and a sheet number (`E-110`, `FP-2`, `E-2.1`). Dotted
numbers also accept no separator, a space, or an en/em dash (`E2.1`,
`E 2.1`, `E–2.1`); all normalize to the hyphen form. Undotted unseparated
tokens (`E110`) remain excluded because grid bubbles and device tags use
that form. A sheet id must also stand alone
as a token: it may not be preceded by a word character or a hyphen, and it
may not be followed by a word character, a hyphen, or `.digit`. So a
panel/circuit callout such as `P-1-12` holds no plumbing sheet `P-1`, and
`HP-E-3`, `LF-1`, `E-201-4`, `E-2.1.3` and `E-12345` hold no sheet id either;
two such callouts can never outvote a single title-block sheet number.
Sentence punctuation after an id is accepted (`SEE E-201.`, `(FP-2)`,
`SHEET M-101, NOTE 3`). Comma/slash number continuations such as `A-1,3`
and `P-1/12` are rejected. The prefixes are `E`/`EL`/`ELEC` →
`electrical`, `M` → `mechanical`, `P` → `plumbing`, `FP`/`FA` →
`fire_protection`, `A`/`ID` → `architectural`, `S` → `structural`, `C` →
`civil`, matched case-insensitively with the longest prefix first. A different
prefix such as `PP-1.0` supplies no discipline. When a
page prints no sheet number, discipline words **inside the title-block band**
decide (`ELECTRIC`/`ELECTRICAL`, `MECHANICAL`/`HVAC`, `PLUMBING`,
`FIRE PROTECTION`/`FIRE ALARM`, `ARCHITECTURAL`, `STRUCTURAL`, `CIVIL`; most
frequent wins, ties lexicographic). The band is the strip along the displayed
right edge plus the strip along the displayed bottom edge, each
`TITLE_BLOCK_BAND_FRACTION` = 0.15 of the displayed page width or height (the
page's text extent when extraction provenance has no displayed page size).
General notes elsewhere on the sheet that name another trade never decide the
discipline. If the band has neither a sheet number nor a discipline word, the
largest drawing title outside it can supply a discipline when the same text
names both a trade and a drawing kind, for example `MECHANICAL PLAN` or
`HVAC FLOOR PLAN`. Otherwise the discipline is `unknown` and the sheet id is
`None`.

One sheet number usually prints more than once (title block, border
callouts). A candidate in the title-block band whose text height is at least
1.5 times the next candidate's height wins even when smaller cross-references
repeat more often. Without a clear height winner, the most frequent candidate
wins. Ties are
broken by the candidate occurrence closest to the displayed bottom-right
title-block corner (using the extracted displayed page size, falling back to
the page's text extent when provenance is absent), then lexicographically,
so the choice is always deterministic. This rule is exposed as
`sheet_identity(document, page) -> (sheet_id, discipline)`, the electrical
lane's shared sheet-identity function.

Selection rules, in precedence order: an electrical sheet with at least one
recognized device is included (`electrical_with_devices`); an electrical
sheet with no recognized devices is excluded
(`electrical_no_recognized_devices`); an unknown-discipline page with devices
is excluded as `unknown_discipline_with_devices` so it is surfaced rather than
silently used; any other discipline is excluded as `<discipline>_sheet`. The
module reads only extracted observations and the importer's output and never
changes importer behaviour; default import output stays byte-identical.

### Cross-discipline sheets and page type filters

An item the electrical side owns is sometimes printed only within another
trade's package; duct-mounted smoke detection shown with the mechanical work
is the standard example. It may be counted from that sheet only when the
electrical legend claims the type. A symbol on a foreign sheet alone is never
scope.

Duct smoke detectors have their own canonical type `duct_smoke_detector`
(aliases `DSD`, `DUCT DETECTOR`, and `DUCT SMOKE DETECTOR`). The rule sits
before the generic smoke rules, so a duct detector is never typed as a
`smoke_alarm` or `smoke_co_alarm`.

The printed sheet number carries the discipline, and the lane has exactly one
definition of it: `sheet_selection.sheet_identity(document, page)` (see
Device-sheet selection above). A page is an electrical sheet when
`sheet_identity(document, page)[1] == "electrical"`: an `E-`, `EL-` or
`ELEC-` sheet number such as `E-1` or `E-2.1`, or, when no sheet number is
printed, an electrical discipline word inside the title-block band. Fixture,
panel and keynote tags (`LF-1`, `HP-E-3`) and project numbers never qualify.
`printed_sheet_ids(document)` is a thin wrapper that maps each page with a
printed sheet id to the id `sheet_identity` chose.

`electrical_scope_types(document)` reads the electrical legend's scope from
those electrical sheets. Every text row on an electrical sheet is classified
with the default symbol rules used for legend rows. An unambiguous row adds
its canonical type to `defined`. A defined type also joins
`cross_discipline` when the row's own text names the electrical side as
responsible. The documented phrase list:

- `WIRED BY E` (also with `E.C.` or `EC` after it)
- `BY ELECTRICAL`
- `BY E.C.` / `BY EC` (a bare `BY E` matches too)
- the contractor-name phrase, matched as `ELECTRICAL\s+CONTRACTOR`
- `FURNISHED BY M`, `MECH`, or `MECHANICAL`, paired with
  `INSTALLED BY E`; the furnished half alone never claims electrical scope

`cross_discipline` also always includes `CROSS_DISCIPLINE_DEFAULT_TYPES`,
which starts as `{"duct_smoke_detector"}`. A row that ties between two types
defines neither. The function returns an `ElectricalScopeTypes(defined,
cross_discipline)` pair and never changes import output by itself.

`ElectricalPdfImporter.import_document(..., page_type_filters=...)` is the
opt-in consumer. The mapping keys are 1-based page indices and the values are
sets of canonical types. On a listed page, only entities whose canonical type
is in that page's set are imported; each kept entity gains
`attributes.pdf_electrical.cross_discipline_sheet` (the sheet id
`sheet_identity` chose for the page, or the page number when none is printed), its confidence is capped at
`CROSS_DISCIPLINE_MAX_CONFIDENCE` (0.6), and it gains one `inferred`
provenance record (`cross-discipline-page-filter`, source element
`p<page>:page-type-filter`, because the sheet id is a page-level vote rather
than one text element) recording the filter. An
entity dropped by the filter is retained as explicit evidence under
`unresolved_observations` with status `excluded_by_page_type_filter`, so a
filtered sheet never silently loses recognized devices. Unlisted pages behave
exactly as before, and `None` or `{}` produces byte-identical output.

Composition is a caller-side step; there is no automatic wiring. Identify the
non-electrical pages with `sheet_identity`, read the electrical legend's
claim with `electrical_scope_types`, and pass the cross-discipline types for
each page of another known discipline (pages of `unknown` discipline are left
unfiltered):

```python
scope = electrical_scope_types(document)
filters = {
    page: scope.cross_discipline
    for page in range(1, document.page_count + 1)
    if sheet_identity(document, page)[1] not in {"electrical", "unknown"}
}
model = ElectricalPdfImporter().import_document(
    document, page_type_filters=filters
)
```
