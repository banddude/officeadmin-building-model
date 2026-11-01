# Physical route revision comparison

This opt-in report under #235 compares two complete normalized canonical
snapshots by stable route ID. It does not call or change `build_revision_diff`,
whole-model quantities, saved-review comparisons, or the OfficeAdmin app.

```sh
PYTHONPATH=src python -m oabm.reports.physical_route_revisions before.json after.json fresh-output
PYTHONPATH=src python examples/physical_route_revisions.py fresh-example
```

The installed entrypoint is `oabm-physical-route-compare`. No install is needed
for the module command. Both inputs must have identical model ID, schema
version and **complete coordinate-system declaration**: frame ID, handedness,
axes, units, CRS, origin and true north. Missing georeferencing on one side is
not assumed equivalent to a supplied value. No alignment, migration, nearest
match or renamed-ID inference is attempted.

`generate_physical_route_comparison(before, after)` returns an immutable result
with `to_dict()`, `to_json()`, `routes_csv()` and
`validate_against_models(before, after)`. `validate_physical_route_comparison`
regenerates the full report from its retained snapshots. It verifies the actual
records and analytic measurements, not just hashes. When comparing to trusted
external sources use `validate_against_models` as well; internal consistency is
not source authenticity. Original physical numeric tokens in saved reports are
checked by the canonical JSON reader before float rounding.

Every route has added/removed/changed/unchanged status, before/after kind and
physical measurements where available. Legacy routes remain visible and
excluded. A same-ID physical-to-legacy replacement is changed, with the physical
side retained, but has a null route delta rather than pretending the legacy
route has zero measured length. Absent physical routes contribute additions or
removals only to the explicitly bounded physical inventory; empty scope is not
a completed zero takeoff.

Physical path equality uses ordered segment kinds, endpoint positions, arc
normal and turn; child IDs, provenance and requirement identity do not define
the path. OD is compared separately. Equal length does not imply equal path.
Field changes carry presence flags, full before/after values and explicit
categories: path geometry, outside diameter, requirements, child identity,
connectivity, provenance, retained context and metadata. A changed request or
obstacle does not establish a changed route or valid current clearance.
Non-route entities are retained in the snapshots but not diffed or measured.

Exact signed deltas reuse the geometric schedule:
`(straight_ticks + numerator / denominator * pi) / 1e9` metres,
plus approximate metres and signed quarter-bend counts. Gross additions and
removals replace whole routes whose path or OD changed; net is gross added
minus gross removed. Unchanged runs, ID-only changes, metadata and evidence
changes add no replacement quantity. Gross measures do not infer salvage,
reusable subsegments, stock, fittings, wire, waste, pricing or change orders.

The CLI prepares bounded deterministic report bytes before reserving a fresh
output directory, then writes `comparison.json`, `routes.csv`, `before.json`
and `after.json`. Existing output paths are refused. CSV uses the schedule's
spreadsheet-safe text policy; JSON retains exact text. Write failure returns an
error and may leave a partial new directory. No input is changed.

The connected public-safe synthetic example moves the supplied end port from
X=4 to X=4.5 metres, regenerates both actual routes, retains each pre-route
source/request hash and generation result, and emits actual adapter SVG plans
labelled BEFORE/AFTER at their native 1:20 scale. Its expected net difference is
+0.5 m straight length, zero pi coefficient and zero bends. The unchanged-run
and equal-length/different-path cases are separate focused synthetic fixtures.
This is a bounded geometry comparison, not full #235 completion or engineering
acceptance.
