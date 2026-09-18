# Golden fixture and regression harness

Issue #10 owns public-safe known-answer canonical documents plus reusable QA
assertions. The harness is deliberately downstream of the canonical v1 model:
it validates lane outputs, but it does not define a second semantic model.

## What it checks

For positive golden cases, `oabm.qa.validate_case()` checks the JSON Schema,
strict canonical Python reader, declared invariant counts/IDs, synthetic
provenance and confidence, exact canonical serialization, round-trip stability,
and stable entity identity.

The suite covers levels, spaces, walls, slabs, ceilings, openings, electrical
equipment/devices, ports/connectivity, obstacles, route constraints, routes,
ordered fittings, circuits, conductors, provenance/confidence, schema
validation, invalid references, and deterministic repeatability.

The negative fixture intentionally passes JSON Schema and then fails canonical
reference-integrity validation. This keeps structural schema checks distinct
from semantic cross-reference checks.

## Consuming it from another lane

Use named cases instead of copying fixture JSON:

```python
from oabm.qa import (
    assert_deterministic,
    get_case,
    load_case_document,
    validate_case,
)

input_model = validate_case("multiple-valid-routes")
assert input_model is not None

case = get_case("multiple-valid-routes")
source_document = load_case_document(case)

# A lane can wrap its own deterministic transformation:
# output = assert_deterministic(lambda: lane_transform(source_document))
```

Tags allow focused discovery, for example
`discover_cases(tags=("routing-input",), include_invalid=False)`.

`assert_identity_preserved(before, after)` is available to lanes that promise
not to replace existing canonical entities. `assert_deterministic(factory)`
compares canonical fingerprints and stable ID inventories across repeated runs.

## Ownership boundary

The harness does not make IFC mapping decisions, find routes, recognize PDF
content, interpret RoomPlan scans, calculate quantities, or generate drawings.
Those lanes own their algorithms and can plug their canonical inputs/outputs
into these shared assertions.
