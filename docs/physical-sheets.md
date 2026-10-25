# Opt-in physical sheet composition

`oabm.drawings.compose_sheet` places existing derived drawing views onto an
explicit paper size. It returns a deterministic presentation document, not a
new semantic model, rendered image, SVG, PDF, DXF or completed permit sheet.
The existing drawing generators and default outputs are unchanged.

```python
from oabm.drawings import SheetSpec, ViewPlacement, compose_sheet

# drawing_set comes from the existing generate_drawing_set(model, ...) API.
spec = SheetSpec(
    id="sheet-1", width_mm=420, height_mm=297, margin_mm=10,
    titleblock_height_mm=25, title="Example plan", revision="A",
    titleblock_fields=(("prepared_by", "Example"),),
)
sheet = compose_sheet(
    drawing_set, spec,
    (ViewPlacement(id="main", view_id="plan-ground", x_mm=10, y_mm=10),),
)
text = sheet.to_json()
```

## Physical coordinates and source authority

Paper coordinates are millimetres from the top-left, with X right and Y down.
A view's existing `scale` is authoritative: at 1:50, one model metre occupies
20 paper millimetres. There is no implicit fit-to-page or scale override.
Placement coordinates refer to the upper-left of the view's existing bounds.
The adapter neither changes those bounds nor silently clips additional content.

`points_mm`, dimension reference/offset-line endpoints and text anchors are
explicit paper coordinates. Dimension `value_m` and text retain the original
measurement and label. Source primitive IDs remain scoped by `placement_id` and
`view_id`; repeating one view in different placements does not invent new
canonical identities. Style, symbol, metadata, confidence and provenance values
are retained, not reclassified or promoted to source-observed facts.

`drawing_set_sha256` binds the exact UTF-8 `DrawingSet.to_json(indent=None)`
bytes. It is not a canonical-model digest or an authenticity assertion. The
complete source index is copied losslessly and sorted by canonical ID. No source
reference is resolved, fetched or uploaded. The document's `model_id` is the
existing drawing-set identity, not independent verification of a model snapshot.

The bottom titleblock reserves a full-width rectangle inside the margin, with
caller-supplied sorted key/value fields. Title, revision and fields are metadata;
this API does not lay out their text or fabricate a logo, stamp or signature.
Zero height means no titleblock and requires empty fields.

## Refusals and limits

Missing and duplicate IDs, unknown source IDs, nonfinite coordinates, invalid
paper sizes, view rectangle overlap and overflow are rejected. Touching view
rectangles are permitted. Geometry and dimension endpoints must fit their
source view bounds; enlarge the source bounds explicitly if annotation space
is needed. Text and symbols are checked by anchor only: their glyph/symbol
extents, text collisions, font metrics, lineweights and actual print readability
require a subsequent renderer and independent visual QA. No automatically
rendered dimension extension lines, arrows, schedules or titleblock graphics
are claimed.

Symbol tokens and semantic line styles deliberately remain unresolved. This
increment does not fix compatibility between the existing view SVG serializer
and any external SVG-to-DXF converter. A later bounded renderer must resolve
physical styles and supported symbol geometry and reject unsupported tokens.
It must not interpret these paper coordinates as 1:1 BIM modelspace geometry.

Serialization has its own `oabm.physical-sheet` version `1.0.0`, separate from
canonical and drawing-model versions. The strict existing review-bundle and
OfficeAdmin review-file contracts are unchanged; do not insert this artifact
into their fixed v1 artifact sets. No production integration is claimed.

## Verification

`tests/test_physical_sheets.py` uses synthetic geometry and the existing public
synthetic drawing fixture. It checks exact scale/axis conversion, dimension
placement, provenance retention, deterministic placement order, immutable output,
source/model non-mutation, duplicate/missing references, text/symbol retention,
bounds and titleblock refusal cases. Existing golden drawings remain unchanged.

No dependency, GPL code, project-specific source or private asset is added.
