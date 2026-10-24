# Bounded editable physical-sheet DXF

`oabm.drawings.sheet_dxf.sheet_to_dxf` is an opt-in derived-output adapter over
`compose_sheet`. It returns UTF-8 DXF bytes and a JSON companion. It does not
modify a `BuildingModel`, accept CAD edits back into the model, alter default
outputs, or change the fixed v1 OfficeAdmin review-bundle artifact contracts.

Install `pip install -e '.[cad]'`. The optional dependency is ezdxf 1.4.x
(MIT); no upstream source is copied. The bounded version range protects the
serialization/readback contract. The adapter itself is independently authored.
No customer plan, logo, titleblock, source code, measurement, or private asset
is included. Runtime import of the ordinary drawings API does not load ezdxf.

```python
from pathlib import Path
from oabm.drawings import SheetSpec, ViewPlacement
from oabm.drawings.sheet_dxf import sheet_to_dxf

# drawing_set comes from generate_drawing_set(canonical_model, ...).
artifact = sheet_to_dxf(
    drawing_set,
    SheetSpec('S-1', 297, 210, titleblock_height_mm=35,
              title='Synthetic plan', revision='A'),
    (ViewPlacement('main', 'plan', 20, 20),),
)
Path('sheet.dxf').write_bytes(artifact.dxf)
Path('sheet.manifest.json').write_text(artifact.manifest_json, encoding='utf-8')
```

## Physical scale and CAD editability

The R2010 DXF has an actual `Layout1` paperspace with exact custom paper width
and height, millimetre paper units, 1:1 plot ratio, zero plot margins/offset,
no rotation, and **Layout** plot type (not fit-to-page). There is no configured
printer driver. A CAD client/printer may still impose hardware margins.
Modelspace is intentionally empty; this is a physical drawing sheet, not 1:1
BIM geometry. The automatically generated paperspace viewport is infrastructure,
not model content; its `VIEWPORTS` layer is explicitly declared non-plot. Model
view scale is applied only by the existing composer:
one model metre at 1:50 is exactly 20 paper millimetres. The adapter maps
`(x, y)` to `(x, paper_height - y)` for bottom-left CAD coordinates.

Supported content:

- Native editable `LINE` with exactly two distinct endpoints.
- Native editable `LWPOLYLINE`, open or closed; polygons are unfilled closed
  outlines. Closure is implicit, not a repeated last vertex.
- Plain single-line printable ASCII `TEXT`, with 2.5 mm nominal cap height and
  explicit `DejaVuSansMono.ttf` style. Fonts are referenced, **not embedded**.
- Dimensions as **nonassociative** dimension/extension `LINE` entities and the
  existing measured label as centered, readable, line-parallel `TEXT`, with an
  explicit 1.5 mm perpendicular baseline gap. No CAD `DIMENSION` calculation
  substitutes paper distances for canonical metres. Zero offset creates no
  zero-length extension. No arrowhead, tick, or guessed measurement is added.
- A generic ruled titleblock in the exact reserved rectangle: Sheet, Title,
  Revision, followed by sorted supplied fields. Its geometry is newly authored,
  with no professional stamp, signature, logo, or approval claim.

Normal/heavy/thin weights are 0.25/0.50/0.13 mm; solid/dash/dot patterns are
continuous, 3 mm dash / 1.5 mm gap, and a point / 1 mm gap. Known object, cut,
opening, electrical, route, and annotation strokes are black; space/obstacle
are gray `#666666`, constraint is `#990000`. Patterns and weights are explicit
per entity. Source layers are mapped injectively to `OABM_` plus UTF-8 hex,
retaining the exact original name in the companion. No lossy sanitization or
reserved CAD layer-name interpretation is used.

## Traceability and determinism

Every graphic entity's handle/type is mapped to its placement/view/primitive or
dimension ID, canonical source IDs, and annotation role. Titleblock items are
explicitly sheet-owned with no invented canonical source IDs. The entire
composition, including source confidence/provenance, original styles,
metadata, dimension measurements, paper settings and source drawing digest,
is preserved in the companion. The companion SHA-256 binds the exact returned
DXF bytes, not a canonical model snapshot or a claim of authenticity. Export
handles are only stable for identical input/library versions; external CAD
editing can invalidate the companion.

The ordinary ezdxf writer is used. Only known timestamp/GUID metadata and the
semantically unordered CLASSES section are normalized afterward; caller text
is never searched/replaced. GUIDs are content-derived. No process-global ezdxf
testing flag or random seed is changed. Identical input and ezdxf version give
identical bytes across runs/process hash seeds. A library upgrade requires
reverification and may legitimately change bytes.

Every returned DXF is parsed and audited after serialization; any audit repair
or error fails the export. Tests independently inspect the layout, native
entities, styles, coordinates, all source mappings, and empty modelspace.
The raw serialized tag-stream regression also checks that entity layer
references resolve to the LAYER table and that `VIEWPORTS` has plot flag zero;
ezdxf's audit alone does not detect an undeclared entity layer.

The companion exporter version is now `1.0.1`. This bookkeeping correction
changes DXF bytes and handles, not canonical geometry, composition or source
identity. Public exports always use the corrected profile. A private `1.0.0`
profile exists only for exact validation of earlier saved deliveries, which
remain immutable; it preserves their historical undeclared viewport layer
rather than silently repairing or relabeling their bytes. Saved replay requires
the retained, hash-bound companion's supported exporter and matching ezdxf
versions, then compares every regenerated artifact byte-for-byte. Unknown or
mismatched profiles and rehashed stale/mixed exports fail closed.

## Fail-closed scope and remaining QA

The complete export fails for any symbol primitive/token, unknown line style,
geometry with hidden text, nonprintable/non-ASCII text, CAD text escapes,
unsupported line/closure semantics, zero-length segments, missing sources,
composition overflow, or insufficient titleblock space. The adapter does not
filter unsupported features out of a sheet. Callers must explicitly generate
a supported scope; disabling electrical visibility is not a substitute for
exporting a requested electrical drawing.

Titles/revisions require a reserved titleblock. Empty title/revision values
remain visible as empty ruled fields. Text has conservative rotated bounds
including descenders/bearings; oversized text/titleblock rows fail rather than
wrap, truncate, or silently change font size. Font substitution, exact glyph
metrics, overlapping annotations/view content, lineweight extents, and physical
printer margins still require visual/print QA. This is not permit readiness.

View titles and arbitrary metadata are companion metadata, not automatically
placed annotations. Schedules are not part of the physical composition and
are not rendered. No symbol library, hatch fills, raster imagery, external
references, native DWG, associative dimensions, or complete production CAD
pipeline is claimed.

## Reproducible synthetic preview

From the repository root:

```sh
python spikes/physical_sheet_dxf/demo.py /tmp/cad-demo --preview
```

The independently authored four-wall `BuildingModel` produces a 297 × 210 mm
sheet at 1:50. The demo explicitly adjusts only derived dimension offsets to
0.7 model metres (14 paper millimetres) for clear annotations; source endpoints,
values and canonical model remain unchanged. It writes a DXF, companion, and an
SVG rendered **from DXF readback** using ezdxf's SVG backend. The preview is not
a separately drawn substitute for the CAD file. Physical render bounds and
scale are explicit. A local SVG viewer can inspect the preview; no browser,
network, font upload, or CAD application is required by the generator.

The QA run also rasterizes this SVG with an installed SVG renderer and visually
checks paper framing, separate wall outlines, horizontal/vertical dimension
labels, and the complete ruled titleblock. This is a synthetic adapter proof,
not a native AutoCAD/BricsCAD compatibility certification.
