# Physical routes in ordinary drawing views

`generate_plan` and `generate_drawing_set` accept explicit
`physical_geometry="xy-centerline"` for canonical `line_arc_v1`
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
clearance proofs. Finite-story cuts retain analytic XY quarter arcs. Cardinal
vertical lines become explicitly labelled location markers. XZ/YZ quarter arcs
are intersected analytically with the finite Z slab: their projected XY locus
is a line, visibly labelled as a projection rather than a 3D chord. Primitive
metadata retains source endpoints, exact center/radius ticks, plane/turn,
clipping planes and derived clipped 3D endpoints. Clipped lateral coordinates
are square-root evaluations for presentation; the retained analytic source is
authoritative. No sampled polyline replaces the curve. XY bounds cropping a
physical path still refuse. Arbitrary oblique geometry remains outside the
canonical profile. No installed-product semantics are added by this consumer.
Plan Z limits and route visibility still apply. Canonical level ownership is
shared with the layer resolver; unknown route levels remain explicitly disclosed
in view metadata rather than being inferred from Z. `apply_layer_selection`
consumes the same saved resolver partition before any sheet/PDF/CAD conversion.
Changing presentation does not alter source geometry, quantities or identities.
