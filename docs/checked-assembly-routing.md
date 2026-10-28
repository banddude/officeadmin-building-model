# Checked assembly routing

Input: a canonical model with generated assemblies and connection ports, selected
cavity/plenum assembly, explicit diameter and centerline bend radius, and one to
eight ordered routing-option candidates. Output: the first new canonical route
passing assembly collision checks and bend tangent-space preflight, with all
attempt reasons retained. No geometry, radius or clearance is guessed.

Use `oabm-route-checked MODEL.json REQUEST.json FRESH_OUTPUT` or
`python -m oabm.routing.checked_delivery MODEL.json REQUEST.json FRESH_OUTPUT`.
The request has exact keys:

```json
{
  "version": "checked-assembly-route-v1",
  "start_port_id": "port:source",
  "end_port_id": "port:load",
  "route_type": "emt",
  "assembly_id": "assembly:ceiling",
  "domain_kind": "ceiling-plenum",
  "nominal_diameter_m": 0.02,
  "centerline_radius_m": 0.08,
  "candidates": [
    {"id": "first", "options": {"clearance_m": 0.005, "port_stub_m": 0.05}},
    {"id": "second", "options": {"clearance_m": 0.17, "port_stub_m": 0.10}}
  ]
}
```

These numbers are synthetic examples, not engineering defaults. The caller
chooses every candidate and radius. Each domain is re-derived for its actual
clearance. `wall-cavity` is also supported within the existing wall adapter's
bounded geometry. Unsupported facts produce an explicit failure.

Successful attempts contain `delivery/model.json`, retained inputs, selection,
bend report and hash manifest. That model is the next input to existing plan/GLB
delivery. Only the chosen route and fittings are appended. Existing routes and
all source assemblies remain unchanged; existing route IDs require an explicit
revision workflow. Circuit reassignment is still the caller's step.

If every candidate fails, no delivery is published. The fresh attempt retains
failure reasons and inputs. Existing output directories are not overwritten.
The API `route_checked_assembly` returns `CheckedAssemblyRoute` or raises
`NoCheckedAssemblyRoute` with its `.attempts`, without changing input objects.

Admission applies to the new route only. Other routes require their own radii and
whole-job review. Passing proves necessary straight-length budgets and matching
sharp-vertex elbow inventory, not curved swept clearance, manufacturer fit,
penetrations, supports or code approval. No curved fitting or CAD is fabricated.

`examples/routed_assembly_delivery.py FRESH_ROOT` connects synthetic wall framing,
opening inserts, T-bar, tiles, hangers and deck to routing and model delivery with
`bend_policy: require`. It retains quantities, source geometry, cut plans and GLB.
This is a reviewable building region, not whole-building/permit acceptance.
