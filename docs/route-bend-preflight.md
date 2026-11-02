# Conduit bend preflight

`oabm.qa.route_bends` checks canonical route centerlines before generating plans,
3D previews or a delivery. It reads the same model those consumers use and never
moves ports, edits routes, chooses catalog fittings or connects nearby stubs.
It supplements `validate_model`, which checks endpoint positions and fitting
ownership but does not enforce elbow placement or physical bend-space budgets.
Existing exports remain unchanged. Call this gate explicitly in a production
script before invoking the existing drawing/GLB/IFC exporters.

```python
from oabm.qa.route_bends import check_route_bends

report = check_route_bends(model, radii_m={"route:feed": 0.30})
if not report.ready:
    raise ValueError(report.to_json())
# Continue with existing model-derived exporters after reviewing limitations.
```

The mapping supplies one **centerline bend radius in metres** per route, as
caller design intent. Never derive a radius from nominal diameter or use these
synthetic values as manufacturer data. Omitted radii produce `radius_unknown`
at every turn. A straight route needs no radius. Unknown route IDs, nonpositive,
boolean or nonfinite radii fail rather than silently applying a default.
A single radius per route is deliberately bounded. Mixed-radius routes need a
future explicit per-turn input, not undocumented attributes or a guessed value.

At each 3D turn of angle `a`, a circular bend consumes `radius * tan(a/2)` on
each adjacent straight. Both ends of a straight are charged. Redundant collinear
vertices do not artificially shorten available space. Exact fits within the
explicit distance tolerance (default 1 micrometre) pass; shorter runs report
`short_straight` with available and required lengths. Reversals are unresolved
and never assigned a finite fillet. Angles use radians; the default angular
tolerance is 1 microradian. Reports retain original canonical vertex indices.

Every turn must have exactly one route-owned elbow positioned at its canonical
vertex, matching its turn angle. This is the router's existing sharp-vertex
convention. Missing/duplicate elbows, unknown/wrong angles and elbows away from
turns are diagnostics. Non-elbow fittings, such as consolidation tees, are left
alone. This is not a validator for already sampled curves, manufacturer CAD,
physical mating ports, terminal approach directions or fitting envelopes.

The supported input includes interior sharp vertices and explicit consolidation
elbows joining exactly two same-type route endpoints through one canonical
connection port. Both incident routes must supply the same radius. One physical
elbow is reported on its owner route with that endpoint's vertex index; its
setback is charged to both legs, including a straight between two junctions.
Unknown or mismatched radii, ambiguous topology, incorrect angles/types and
insufficient straight lengths remain diagnostics. Other off-turn elbows retain
`elbow_off_turn`. Coincident disconnected endpoints do not establish connectivity.

Report JSON is versioned, sorted by route ID and diagnostic location, and has no
timestamps or local paths. It includes the supplied radius and computed setback
for each bend, plus the distance and angular tolerances used. `ready` means only that these bounded checks passed. It does not
establish surveyed equipment position, code compliance, internal fit, bendability,
swept-volume clearance, self-intersection freedom or engineering approval.
No smooth geometry is added to exports by this check. Radius-based route
realization and shared curved plan/3D geometry remain future work.

## Executable synthetic demonstration

```sh
PYTHONPATH=src python examples/route_bend_preflight.py
PYTHONPATH=src python -m oabm.qa.route_bends \
  fixtures/routing/bend-preflight/valid.json \
  fixtures/routing/bend-preflight/radii.json
```

The CLI writes JSON to stdout. Exit 0 means the bounded checks passed, 1 means
unresolved geometry/inventory, and 2 means invalid input. Substitute
`short-offset.json` to see a 500 mm offset fail because adjacent 300 mm radius
bends consume 600 mm. Fixtures and expected reports are entirely synthetic.
The example checks both reports against committed expected bytes and confirms
that the canonical model is unchanged. No renderer or external service is used.
