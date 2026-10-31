# Physical route XY review drawing

This explicit drawing adapter renders canonical physical routes as scaled,
editable SVG. It is a bounded drawing slice of #228, not completion of that
issue. Existing `generate_plan`, legacy drawing outputs and consumer refusal
checks remain unchanged. No sharp legacy polyline is substituted for an arc.

Call `oabm.drawings.physical_routes.generate_physical_route_plan(model, scale=25)`.
The immutable result carries `svg`, the source-bound `schedule`, and
`source_model_json`. The canonical snapshot, source hash, full 3D lengths and
bend counts come directly from `oabm.reports.physical_routes`. The SVG displays
approximate metres to nine decimal places; exact straight/pi terms remain in
the schedule JSON. Model/joint/segment/requirements/endpoint identities remain
in SVG source attributes and retained canonical JSON, including provenance.
The supplied model is never mutated.

Use `python -m oabm.drawings.physical_routes MODEL.json FRESH_OUTPUT_DIRECTORY
--scale 25` (or installed `oabm-physical-route-plan`). The fresh directory contains
`plan.svg`, `model.json`, `schedule.json`, `routes.csv`, and `segments.csv`.
Validation and UTF-8 encoding happen before directory creation. Existing output
paths are refused. A write failure is an error and can leave partial output.

## Geometry and interpretation

The drawing is an all-height XY projection in the canonical coordinate frame,
with +Y up, rather than a level cut. Native paper dimensions are millimetres;
1:25 maps one model metre to 40 mm on paper. Print at native size for that
scale; the XY scale bar remains usable after uniform resizing.

* Horizontal cardinal lines and XY quarter circles use the supplied actual OD
  as their scaled stroke width. SVG circular arcs preserve canonical turn and
  radius; no chord or sharp virtual corner replaces them. Stroke end caps are
  flat. SVG coordinates are presentation values rounded to six decimal mm.
* Vertical lines project to actual-OD discs. XZ/YZ quarter circles have a straight
  XY centerline projection, shown dashed orange. Their projected tube envelope
  is **not** represented. `V` tags and the register report each changing segment's
  canonical identity, start/end model Z and signed vertical change.
* Endpoints have route-specific A/B tags, port identities and XYZ coordinates.
  Route rows report their model Z range. Heights are model Z, not inferred
  height above floor. Segment tooltips also report Z endpoints.
* Every supplied canonical obstacle is shown as an identity-rotation box XY
  footprint at all heights with an O tag, canonical identity and Z range.
  Non-box geometry and nonidentity rotations fail explicitly. No obstacle
  clearance envelope is inferred or rendered.
* Legacy routes are explicitly excluded and listed. Other populated entity
  collections are listed as omitted, including endpoint owner geometry.
  Allowed domains, hard constraints, structural/assembly objects and other
  building geometry are not silently treated as visible or checked obstacles.

Crossings do not establish connectivity or clearance. This drawing performs
no live clearance check, does not establish hidden vertical geometry as clear,
and makes no installation, permit or code approval claim. Use the 3D model and
live verifier for the supplied supported scope. Dense projections can overlap;
this adapter is a route-review drawing, not an automatic production sheet set.

## Bounds and example

Presentation is bounded to 32 physical routes, 4096 total physical segments,
512 obstacles, 1200 mm plot extents and 5000 mm annotation height. A finite
scale in [1, 1000] is required. An empty physical-route scope refuses output.
Changing scale can accommodate a larger model; it does not change model facts.

`PYTHONPATH=src python examples/physical_route_plan.py FRESH_OUTPUT_DIRECTORY`
produces a connected synthetic obstacle-detour example using the actual physical
route generator. `route/` has the generator result, model, GLB and schedule;
`drawing/` has the SVG and source-bound drawing delivery. The example uses only
synthetic supplied geometry. The model identity in both schedules and the GLB
agrees. No app code or PDF importing participates.
