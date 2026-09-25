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
per-page recognition to proceed. When explicit `PdfPageTransform` values are
supplied they must still cover every page and target one canonical `frame_id`.
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
wings). Prototype and candidate strokes are normalized to
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
starts within about one inch (72 pt) past where the admitted table's printed
descriptions are estimated to end (per row, the nearest description text's
position plus its character count times its font size times 0.6, falling back
to the label plus the description span when a row has no description), every
one of its labels continues that grid on a distinct row (table purity), and
each of its rows carries description text to the right the way a printed
legend row does and a bare field tag does not (row evidence). The estimate
may legitimately run past the evidence search span, which is what lets a
legend printed with long descriptions resolve the column beside it. A
two-column legend with the heading over column 1 therefore still resolves
column 2, even at the roughly 340 pt column offset measured in issue #108,
while a vertical run of tagged field fixtures on the
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
printed at non-orthogonal angles in skewed wings. The tag remains the only
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

Scope evidence is read only from what the drawing prints: its own text, plus
the SHX text proxies described below. Any other PDF comment (a reviewer's
`/FreeText`, `/Square` or sticky-note markup) is laid over the sheet, so it is
never a status marker, a general-note default, or an `(E)` callout.

A device whose position has no marker of its own still takes a status letter
printed beside it: single `E`/`N`/`R` texts within the field-status radius are
marker candidates, and letters that tie within the ambiguity margin with
different values leave the device `unresolved` with `scope_reason`
`scope_marker_ambiguous` and both candidates recorded. Some CAD exports draw
SHX-font text as strokes and repeat each string as a read-only `/Square`
comment whose title is "AutoCAD SHX Text"; such a comment is the drawing's own
printed text, so its letter is a marker candidate too (recorded with
`text_proxy: autocad_shx_text`, never an annotation author). Markup comments
by any other author are not markers.

A sheet general note can also default the scope of the unmarked devices of one
family on that sheet, for example
`LIGHT FIXTURES SHOWN ON THIS SHEET ARE EXISTING U.O.N.` A default needs a
named family (light fixtures, luminaires, outlets, receptacles), exactly one
scope, and an "unless otherwise noted" clause, with no further scope wording in
the note's own text. The note reaches only unmarked devices whose canonical
type is in that family, and only on the note's own page. A note that names both
scopes (`NEW / EXISTING U.O.N.`), or whose own text qualifies the default
(`... NEW U.O.N. REUSE EXISTING OUTLETS WHERE AVAILABLE.`), stays evidence of
ambiguity: its devices remain `unresolved` with `scope_reason`
`scope_default_note_ambiguous`, and two notes of the same family giving
different scopes give `scope_default_note_conflict`. "Unless otherwise noted"
is honoured literally: scope wording printed beside a device, within the
field-status radius (for example `(N) ...`, `NEW`, `EXISTING`, `RELOCATE`),
notes it otherwise. That device stays `unresolved` with `scope_reason`
`scope_default_note_otherwise_noted` and the wording's element IDs. The note
text and its source element IDs are recorded with the devices it reaches.

For document sets a human has explicitly ruled on, the importer accepts a
`UserScopeAssumption(rule: str, source: str)`, passed as
`ElectricalPdfImporter(user_scope_assumption=...)`. It is off by default, and
the library ships no rule of its own: the caller supplies the rule text and
its source, and passes the assumption only for the documents the rule names.

- **Where it applies.** Only to entities with `scope_reason: no_scope_marker`,
  meaning the sheet gives no scope evidence for them at all. Every other
  unresolved reason is the sheet's own evidence and stays unresolved: tied
  letters, a letter the sheet's legend does not define
  (`scope_marker_undefined`), a legend giving one letter two meanings, and a
  note naming both scopes. Legend-resolved markers and valid sheet-note
  defaults keep their own scope.
- **Default.** The rule sets `scope_status: new` with
  `scope_method: "user scope assumption"`.
- **The `(E)` exception.** A drawing text with an uppercase `(E)` callout marks
  the nearest entity within the importer's `annotation_radius_pt` as
  `existing_to_remain` (`scope_method: "user scope assumption, (E) exception"`,
  with the callout's element IDs). It does so only when that entity is clearly
  the nearest: another entity within 25% (plus the 2 pt field-status tie
  margin) makes the callout's target ambiguous. The rule then sets no scope for
  any of the tied entities, and those it would have classified stay
  `unresolved` with `scope_reason: scope_assumption_exception_ambiguous`. An
  abbreviation definition (`(E) = EXISTING`, `(E) EXISTING`) marks nothing.
  Leader lines are not traced, so a callout whose leader runs to an entity
  outside the radius does not reach it.
- **Provenance.** `derivation` is the authoritative record of the rule's
  effect. Every scope the rule sets adds a canonical `Provenance` record with
  `derivation: user`, `source_kind: caller-scope-assumption`, the
  `scope_method` as its method, and `attributes`: `assumed_attribute:
  scope_status`, the resulting `scope_status`, and the rule and source
  verbatim. The entity's own sheet records stay observed, so `is_observed`
  reports the entity as not purely observed. The lane keys
  `scope_assumption_rule`, `scope_assumption_source` and
  `scope_assumption_derivation: user` are diagnostic copies.

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
