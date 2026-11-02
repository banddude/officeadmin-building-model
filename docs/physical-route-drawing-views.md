# Physical routes in ordinary drawing views

`generate_plan` and `generate_drawing_set` accept explicit
`physical_geometry="xy-centerline"` for horizontal canonical `line_arc_v1`
routes. The complete original model is validated and projected alongside normal
building/electrical geometry. Assembly cuts retain their separate explicit
`assembly_geometry="cut"` admission. The normal schedules describe supplied
facts; they do not derive purchased bend or stock quantities from curves.

Lines remain lines and quarter-circle bends remain analytic arcs, with source
route/segment/joint IDs. Curved drawing sets use additive output version `1.1.0`;
legacy sets retain `1.0.0`. Ordinary view SVG, physical sheet SVG, native DXF ARC
and `render_drawing_sheet_pdf` preserve curves. PDF conversion accepts only the
existing bounded internal sheet renderer, with page/text/vector readback.

The PDF converter removes the SVG white text halo because CairoSVG does not
implement SVG `paint-order` and otherwise can erase visible glyphs while leaving
extractable text. PDF text remains black editable fill; the manifest discloses
this rendering limit. Poppler raster acceptance checks actual titleblock pixels
as well as text extraction.

This bounded mode draws centerlines, not actual OD envelopes, hidden lines or
clearance proofs. Vertical/oblique route projections and XY bounds cropping a
physical path refuse; they never become chords or silently truncated geometry.
Plan Z limits and route visibility still apply. Canonical level ownership is
shared with the layer resolver; unknown route levels remain explicitly disclosed
in view metadata rather than being inferred from Z. `apply_layer_selection`
consumes the same saved resolver partition before any sheet/PDF/CAD conversion.
Changing presentation does not alter source geometry, quantities or identities.
