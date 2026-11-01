# Opt-in panel schedule facts

Issue #282 adds a read-only, model-derived report and an explicit drawing-table
adapter. It does not change canonical model versions, default schedules, IFC,
GLB or quantity outputs. #233/#227 remain open for engineered load/demand
calculations, panel sizing, permit documents, production OfficeAdmin renderers
and delivery. This report is not an OA-issued customer document.

## Generation and ownership

`oabm.reports.generate_panel_schedule_report(model)` validates a `BuildingModel`
and its portable canonical representation. Each circuit's actual `source_port_id`
is resolved through the port's actual `owner_id`. The only supported panel
classification is the existing exact equipment token `panelboard`; there is no
name matching, nearest-panel assignment, case/alias guessing or interpretation
of switchboards/transformers as panels. Other owners retain identified rows in
`unassigned_circuits` with `unsupported_source_owner` diagnostics.

All explicitly typed panelboards appear, including those with no listed circuits.
Rows preserve canonical circuit/source-port/owner identities, load-port and route
identities, printed `circuit_number`, poles, phase, voltage and connected VA.
Missing supplied facts remain null/blank source facts with explicit
`missing_fields`, not inferred slots, breakers, phases, capacities or spares.
Printed labels are opaque text. Exact duplicate nonblank labels within a panel
produce a conflict listing the involved circuit IDs. `1`, `01`, `1/3` and ` 1 `
remain distinct labels; poles do not allocate imaginary slot numbers.

## Connected-load arithmetic and completeness

`known_connected_va` is `math.fsum` of supplied non-null circuit VA in sorted
circuit-ID order. Each canonical circuit contributes once, irrespective of the
number of load ports or route references. Explicit zero contributes zero.
`subtotal_source_circuit_ids` identifies the contributing circuits, and
`subtotal_derivation` is inferred arithmetic. `unknown_load_circuit_ids` and
`all_listed_loads_known` are separate; unknown values never become source zeroes.
An empty known subset has arithmetic subtotal zero, not a claim of zero unknown
load. An empty panel has `no_listed_circuits`; completeness describes only the
listed canonical circuits, never the actual building or equipment capacity.
Finite individual values whose sum overflows finite output range are refused.
No demand factor, code table, engineering calculation or sizing approval exists;
`code_verification_performed` must be false.

## Source evidence and binding

Each row nests canonical `SourceSnapshot`s for its circuit, source port and
actual source owner. A panel nests its canonical equipment snapshot. These use
canonical owner encoding, including legacy omissions for absent derivation,
absent scope qualifiers and unknown wall construction. Confidence and provenance
keep their original owner-relative meaning; qualifiers are not renamed to the
computed subtotal. Model-wide header provenance and attributes remain visible in
`model_source_context`, including parser/design assumptions scoped to `circuits`.
Original observed/user/inferred records remain original source facts.

Snapshot/context content fingerprints bind the normalized recorded facts and
reject stale nested mutation. Source dictionaries are detached copies, so later
caller/source edits do not silently revise a prior report. `to_dict`/the adapter
revalidate the report before exporting a view. Direct canonical provenance with
non-object attributes is refused at this portable report boundary rather than
changing the canonical model's constructors or hiding it in a summary.

The full source-model binding algorithm is
`canonical-entity-order-and-scope-sets-v1`: use the strict canonical typed reader,
canonical owner encoder, sort top-level entity collections by canonical ID and
sort typed provenance scope qualifier sets. Dict keys are sorted for the digest.
Meaningful geometry, route/load reference arrays, arbitrary attributes arrays and
provenance record/history ordering remain unchanged. Thus entity/scope-set
permutations have identical report bytes while changed facts/context generate a
new fingerprint. The original model and its serialization are never modified.
Values that cannot roundtrip faithfully through the canonical typed reader are
refused. Hashes prove internal binding, not authenticity of an external document.
`report.validate_against_model(model)` regenerates and compares the whole report
when the exact source model is available; a revision cannot retain stale rows or
subtotals behind a matching ID alone.

## Portable shape and drawing adapter

`contracts/panel-schedule-report-v1.schema.json` defines report version `1.0.0`
and references the existing canonical entity definitions for nested snapshots.
`PanelScheduleReport.from_dict/from_json` reject unknown fields/versions,
nonfinite/bool numbers, scalar strings pretending to be ID arrays, malformed
owner bindings, duplicate circuit/panel identities, stale snapshot fingerprints,
contradictory arithmetic/completeness and incorrect label-conflict diagnostics.
Schema validation handles shape; the strict reader additionally validates scopes,
references and recomputed facts. Constructors use the same invariants.

`oabm.drawings.panel_schedules.panel_report_to_drawing_schedules(report)` explicitly
returns existing `DrawingSchedule` objects: one circuit table and one supplied-load
summary per panel, plus an unsupported-owner table where needed. Source IDs link
the view back to the retained report/model facts. Default `generate_schedules` is
untouched; callers must opt in and retain the report alongside its table view.
The adapter issues no PDF, OA write or customer delivery.

Public synthetic tests exercise an actual RoomPlan import with two explicit
panels/circuit facts, shared loads and multiple routes; all existing model
versions and actual legacy garage omissions; opaque conflicts/missing/zero loads;
source/context confidence and scopes; revisions/permutations; strict malformed
inputs; mutation/overflow refusal; and unchanged default model/drawing/IFC/GLB/
quantity bytes after generating the opt-in report.


## Physical routes as source context

Validated 1.7 models containing canonical `line_arc_v1` routes now generate the
same supplied panel/circuit facts and connected-load arithmetic as legacy route
models. Route variants use the canonical discriminator and typed physical reader;
source snapshots retain actual joints, arcs, requirements, provenance and request
context rather than flattening them into polylines. Whole-model fingerprints
include those source changes, so equal panel rows do not authorize stale report
reuse across changed routes. Portable physical owner snapshots enforce the 1.7
source-version floor and original-JSON nanometre admission. This does not measure
routes, establish complete takeoff, calculate demand or verify electrical code.
