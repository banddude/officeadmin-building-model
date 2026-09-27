# Derived drawings

`oabm.drawings` turns a canonical `BuildingModel` into deterministic presentation artifacts. The canonical model remains the only semantic source of truth: drawing views, annotations, dimensions, SVG, and schedules are outputs only and are never read back as model geometry.

## Output model

`DrawingSet` contains orthographic `DrawingView` objects, semantic schedules, and a stable source index. Every derived primitive, dimension, and schedule row references one or more canonical entity IDs. The source index retains canonical confidence and provenance so a rendered item can be traced back to the model without copying or redefining the canonical entity.

Drawing output has its own presentation version (`1.0.0`). This is deliberately separate from the canonical model schema version because changing presentation metadata or rendering details must not mutate the model contract.

## Views

- Plans use the canonical XY plane and a level-relative cut plane. Level hosting plus explicit 3D geometry controls visibility.
- Elevations and sections use an orthonormal frame derived from an explicit origin and view direction.
- Bounds clip projected polylines and polygons deterministically. Geometry outside a view is omitted; geometry crossing a boundary is clipped rather than discarded.
- Sections distinguish geometry crossing the section plane from geometry merely visible within the configured depth.
- Wall thickness and height are derived from canonical wall centerlines and dimensions. Equipment, devices, openings, obstacles, and fittings use their canonical poses and sizes where available.
- A wall whose canonical `construction` is `glazed` projects onto the `architecture:glazing` layer with the lightest existing line weight (`normal`, never the heavy cut weight) and, in plan cuts, one dashed centre line per wall segment drawn on the centerline between the two wall faces. Every other construction token keeps the `architecture:walls` layer. Tokened walls carry `("construction", token)` in each primitive's `metadata`; tokenless walls produce byte-identical output to before the token existed.
- Routes use their canonical centerlines. Each 3D route segment is clipped against the active plan/elevation/section depth interval before 2D projection, so out-of-depth portions cannot leak into a view; crossing segments terminate exactly at the depth boundary. The drawings lane never reroutes them or recomputes takeoff lengths.

## Visibility and annotation hooks

`VisibilityPolicy` controls architecture, electrical, routes, spaces, obstacles, constraints, labels, and dimensions. Obstacles and route constraints are hidden by default and can be enabled for coordination views.

`SymbolProvider` and `LabelProvider` are presentation hooks. They receive canonical entities and the view type and return display-only symbols or labels. Hooks cannot change canonical model objects.

## Opt-in readability options

`generate_drawing_set`, `generate_plan`, `generate_elevation`, and `generate_section` accept two keyword-only presentation options. Both default to off, and defaults leave output byte-identical to the previous generator.

- `dimension_decimals` (`None`, or an integer `0`–`4`). `None` keeps the historic full-precision dimension text (`_fmt_number`). An integer formats each dimension's display text with exactly that many decimals, rounded once with `decimal` `ROUND_HALF_EVEN` applied to the value's shortest repr, so results are deterministic; `0` prints no decimal point (`3`, not `3.0`), and higher precisions keep trailing zeros (`3.10` at 2). `value_m` always keeps full precision — only `text` changes. Values outside `0`–`4` raise `ValueError`.
- `label_overlap` (`"keep"`, `"skip"`, or `"skip_with_dimensions"`). `"skip"` omits text-label primitives (`annotations:labels`) whose estimated box intersects an already kept label's box. Labels are evaluated in sorted source-id order, so a cluster keeps the label with the lowest source id. A label box is estimated at the view scale `1:s` as `len(text) × char_w` wide by `char_h` tall in model units, where `char_h = 0.0025 m × s` (the 2.5 mm annotated text height on paper) and `char_w = 0.6 × char_h`; the anchor is the SVG start anchor and baseline, so the box spans rightward from it and one `char_h` above it, and boxes that merely touch do not count as overlapping. `"skip_with_dimensions"` behaves identically except each dimension's text box also counts as an already kept obstacle before labels are placed: that box uses the same constants and size as a label box, centred on the dimension's text anchor (the midpoint of the offset dimension line, rendered with `text-anchor="middle"`), and dimensions themselves are never dropped. Symbols, dimensions, and geometry are never dropped in any mode. The view's existing `metadata` channel gains a `skipped_labels` entry with the dropped count; `"skip_with_dimensions"` additionally gains `skipped_labels_by_dimension` with the count of labels skipped by a dimension box; no drawing-model field is added. Any other value raises `ValueError`.

`view_to_svg` accepts a matching keyword-only `text_height_m` so rendered text matches the `label_overlap` estimate: when set, every label and dimension `<text>` gets `font-size="<_fmt(text_height_m × pixels_per_model_unit)>"`, values of `0` or less raise `ValueError`, and the default `None` writes no `font-size` and leaves the SVG byte-identical to the previous serializer. `paper_text_height_m(scale, paper_height_m=0.0025)` converts an annotated paper text height to model units at view scale `1:s`; its default is the same constant the label-overlap box estimate uses, imported rather than restated so the two cannot drift. Pass its result straight through to keep the rendering and the overlap estimate consistent: `view_to_svg(view, pixels_per_model_unit=ppu, text_height_m=paper_text_height_m(spec.scale))`.

## Schedules

Schedules are stable-ID-sorted views of canonical semantics for rooms, openings, electrical equipment, electrical devices, and circuits. They intentionally do not calculate conduit, cable, or conductor quantities; that belongs to the quantities workstream.

## Determinism

Generation sorts canonical inputs by stable IDs, quantizes output coordinates, uses content-derived primitive IDs, and serializes JSON with sorted keys. Reordering canonical collections therefore does not change output. `fixtures/drawings/v1/expected-drawing-set.sha256` is a golden fingerprint of the serialization generated from the synthetic drawing fixture and fixed view specifications.
