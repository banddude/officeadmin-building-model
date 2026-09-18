# Golden QA fixtures

These fixtures are synthetic, public-safe known-answer documents for canonical
model v1. They exercise cross-workstream handoff behavior without implementing
an importer, router, IFC adapter, quantity engine, or drawing generator.

`v1/manifest.json` is the stable fixture index. Consumers can discover cases
by name or tags with `oabm.qa.discover_cases()` and validate a checked-in case
with `oabm.qa.validate_case()`.

The v1 set covers:

- rectangular building geometry
- hosted door opening plus obstacle/keep-out geometry
- panel-to-EVSE topology, symmetric ports, route, circuit, and conductors
- a known-answer route with a vertical elevation change and ordered fittings
- routing input with multiple valid corridors
- routing input declaring an impossible outcome inside its fixture domain
- an explicit two-level building
- a synthetic garage spanning the shared semantic contract
- a schema-valid negative case with an invalid cross-reference

All model documents use deterministic IDs, canonical metres/radians/+Z-up
coordinates, and synthetic provenance. Routing-input fixtures state the
expected outcome category in fixture metadata; QA does not contain a routing
algorithm.
