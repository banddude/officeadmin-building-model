# Physical-route geometric schedule

This standalone BIM report measures canonical physical routes. It is not a complete takeoff, a stock/cut list, or an app change. `extract_quantities` and the existing confidence/review-bundle limitations are unchanged.

## Use

`generate_physical_route_schedule(model)` in `oabm.reports.physical_routes` returns an immutable `PhysicalRouteSchedule` with `to_dict()`, `to_json()`, `routes_csv()`, `segments_csv()`, `source_model_json` and `validate_against_model(model)`.

The CLI is `oabm-physical-route-schedule MODEL.json FRESH_OUTPUT_DIRECTORY`. It validates and prepares all bytes before creating the new directory, then writes `model.json`, `report.json`, `routes.csv` and `segments.csv`. Existing destinations are never overwritten. A file-write failure is an error, not a successful delivery. The standard-library module form is also available: `python -m oabm.reports.physical_routes` with the same arguments.

Run `python examples/physical_route_schedule.py FRESH_OUTPUT_DIRECTORY` for a connected synthetic example. It generates a real obstacle-detour route, a GLB and a schedule from one normalized canonical model. The GLB's model identity and report's source identity must agree. This example is not an electrical installation design or a production upload.

## Measurements and scope

Route rows contain route/endpoint/requirements IDs, original name/type text, actual supplied outside diameter and bend radius, segment counts, unsigned quarter-bend counts and unsigned total turn. Segment rows retain traversal order, segment/joint identities, actual source records and provenance.

The exact centerline length is `(straight_ticks + pi_coefficient_ticks * pi) / 1e9` metres. The reduced coefficient retains half-nanometre values for odd-nanometre radii. Approximate metres are labelled explicitly. Measurement uses canonical segment facts, never GLB chords or virtual sharp control legs. Opposite turns do not cancel when counting bends or total turn.

All physical routes are measured in deterministic ID order. Every legacy route is explicitly excluded in JSON and receives a labelled CSV row with blank measurement cells, not misleading zero lengths. Empty inputs mean zero measured physical routes, not a complete zero takeoff. Other model entities are not measured.

No purchased elbow/fitting count, conductor slack, stock lengths, cut allowances, couplings, supports, waste, pricing, current clearance or code approval is inferred. A valid measurement report is not evidence that a route clears the current building.

## Binding, safety and limits

Inputs are detached through bounded canonical capture and reconstructed by the canonical reader. Source binding profile `normalized-canonical-model-json-v1` hashes the resulting compact canonical model JSON. This is normalized typed-model identity, not original input-file bytes or source authenticity. The CLI retains those exact normalized bytes as `model.json`. Model metadata and unrelated objects participate in the binding.

`validate_against_model` regenerates the complete report, so changing a measurement while keeping the source hash does not pass. `validate_physical_route_schedule(payload, model)` validates saved report JSON by the same regeneration, then returns the canonical report. Unknown fields, tampered values and stale source snapshots are not accepted. A returned dictionary is detached; editing it does not edit the report.

Source/report byte, node and nesting caps follow the existing bounded physical capture profile. Unsupported raw physical numbers remain subject to canonical original-token admission when loading model JSON. Direct Python values use their canonical representation. Resource limits fail before a CLI output directory is reserved.

CSV is a presentation derivative. Formula-like text, including whitespace-prefixed formulas and leading tab/CR/newline, is apostrophe-prefixed. Unsupported control characters and unpaired surrogates are escaped. Commas, quotes and embedded newlines use normal CSV quoting. Authoritative JSON preserves original source text; use JSON for exact source identity and provenance. Spreadsheet import settings can still affect how ordinary numeric-looking text is displayed.
