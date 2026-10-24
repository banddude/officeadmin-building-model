# Physical sheet SVG renderer

`oabm.drawings.sheet_svg.render_sheet_svg` is an opt-in renderer over the existing
physical sheet composer. It returns editable vector/text SVG plus a detached
source-mapping manifest; it does not fetch images, fonts or evidence, write
files, upload artifacts or change a model. Existing drawing/SVG default APIs
and OfficeAdmin's strict review-bundle contract are unchanged.

```python
from oabm.drawings.sheet_svg import render_sheet_svg

# Use the same DrawingSet, SheetSpec and ViewPlacement inputs as compose_sheet.
rendered = render_sheet_svg(drawing_set, sheet_spec, placements)
svg = rendered.svg
manifest = rendered.manifest()
```

## Physical presentation

The root has explicit physical `width`/`height` in millimetres and an equal-sized
viewBox. Geometry is flattened to paper-space millimetres with top-left origin,
X right, Y down. Existing view scale is never overridden or fitted to the page.

Supported primitives are lines, polylines, outline polygons, text and explicitly
registered line-segment symbols. No arbitrary caller SVG/XML, remote images,
scripts, hrefs, CSS, transforms or resource loads are generated. Text and metadata
are escaped; XML-invalid characters and unlaid-out multiline/tabbed text fail.

Semantic lineweight mapping is normal=0.25 mm, heavy=0.5 mm, thin=0.13 mm.
Dash patterns are solid, dash=3/1.5 mm and dot=0.25/1 mm. Object, cut, opening,
electrical, route, annotation and symbol strokes are black; spaces/obstacles are
#666666, constraints #990000. Unknown style tokens fail. These are presentation
choices, not engineering classifications or new source observations.

Dimensions render their supplied reference endpoints, offset line, inward
arrows and unchanged text. They remain nonassociative graphics; no measurement
is invented or recomputed from the paper. Too-short lines for the requested
arrows or geometry/stroke/symbol extents outside the placed view fail rather
than clipping or shrinking. A reserved titleblock is required; its ruled frame
prints sheet ID, title, revision and supplied fields in deterministic rows.
Insufficient row height or conservative character capacity fails. Missing
revision is explicitly printed as `unspecified`.

`SheetSvgOptions` records font family, label/dimension/title em sizes, arrow
length and white text halo width. Defaults: DejaVu Sans; 2.5/2.5/4 mm text;
1.5 mm arrows; 0.7 mm white halo. Halos deliberately mask underlying strokes
locally to make text readable, without changing canonical or drawing geometry.
All positioned labels paint after geometry, independent of source ID order.
Dimension labels have a separately mapped `svg_label_id` linked to their
dimension geometry group, so their halos are not crossed by later linework.
Set `text_halo_mm=0` to disable. No fonts are embedded or redistributed. Physical
text size is an SVG em size, not a DXF cap-height assertion. Actual font presence,
fallback, glyph bounds and collisions must be checked by the final renderer.
Deterministic SVG bytes do not promise pixel identity across fonts/platforms.

## Explicit symbol registry

There is no generic fallback that could resemble a recognized electrical symbol.
Pass `symbols={token: SheetSymbol(segments=...)}` with nondegenerate `Point2`
endpoint pairs, measured in paper mm relative to the symbol anchor, +Y down.
Every used token must exist. Caller-supplied symbol segments are ordinary editable
lines; their exact geometry is recorded in the manifest. A symbol with unplaced
label text is refused: provide a separate positioned text primitive instead.
This bounded registry does not claim support for a standard symbol library.

## Traceability and explicit omissions

Each source primitive and dimension has a collision-safe SVG ID scoped to its
placement, view, item kind and original ID. Source-ID arrays are JSON-encoded
in `data-source-ids` and exactly listed in the manifest. Primitive metadata,
view metadata (including existing skipped-label/dimension counts), source
confidence and provenance remain available. The manifest binds both exact SVG
bytes and composed-sheet serialization by SHA-256 and retains the drawing-set
digest. Hashes identify bytes; they do not authenticate an author or regenerate
a canonical model.

View titles and schedules are not automatically laid out. The manifest lists
unrendered schedule IDs and unplaced view IDs; placement is explicit. No silent
schedule drawing, new layer inference, canonical writeback or live OfficeAdmin
integration is claimed. This is a reviewable drawing, not a permit-ready set.

## Synthetic visual proof

```sh
PYTHONPATH=src python examples/physical_sheet_svg.py /new/synthetic-output
```

The example reads only the checked-in public synthetic drawing model. It creates
an explicitly architecture-only plan and elevation at 1:50 on A3 landscape.
Electrical/routes are disabled in the view policy; only spaces receive labels;
opening geometry is retained without separate marker symbols. Existing generator
readability options are explicit and their omission counts remain in the manifest.
It writes SVG, manifest and drawing-set JSON into a new directory and refuses to
overwrite an existing output. The titleblock says SYNTHETIC DEMONSTRATION - NOT
FOR CONSTRUCTION. No project/customer content is used.

Tests reopen the generated XML, independently check physical coordinates and
source joins, assert repeatability and unchanged canonical inputs, and exercise
unsupported styles/symbols, escaping, malformed text and layout refusals. Native
Inkscape rasterization and visual inspection were additionally performed on the
synthetic example; these checks do not establish arbitrary drawing readiness.
