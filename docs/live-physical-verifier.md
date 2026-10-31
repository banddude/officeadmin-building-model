# Live supplied physical-route verification

The opt-in `oabm.routing.physical_verify.verify_physical_route(source, candidate,
source_json=None)` checks one supplied canonical PhysicalRoute against its current
bound pre-route BuildingModel. It does not generate routes, size raceways, select
fittings, authorize penetrations, or establish installation/code compliance.

## Evidence and outcomes

The immutable result contains status, reason and canonical report JSON. Status is
one of verified_route, rejected_route, invalid_input, unsupported_input,
numerically_unresolved or resource_limit. A well-formed route with proved contact
or insufficient clearance is rejected_route. Uncertain bounds or exhausted work
are never success. There is no planner search-exhaustion claim.

Both inputs undergo bounded deep capture before canonical validation/encoding.
Only exact known canonical dataclasses and ordinary built-in JSON containers are
accepted. Every serialized alias occurrence counts; ancestor cycles refuse.
Concurrent mutation may change the captured snapshot or cause refusal. Capture
does not promise an atomic instantaneous view. All later geometry and hashes use
detached captured inputs, never reread caller mappings. A receipt remains evidence
for those snapshots only; reuse requires another current-context verification.

Original source JSON is optional. When supplied, its consumed exact numerical
lexemes are admitted before its ordinary canonical decode, and its normalized
snapshot must match the captured typed source. Without it, source admission is
canonical_python_snapshot, not an assertion about unavailable original lexemes.
Typed candidate admission is always canonical_python_snapshot.

Source/request/candidate hashes bind scope and evidence but do not prove clearance.
All mandatory blockers are enumerated anew. Explicit permitted scope exclusions
are reported. Unsupported source/envelope adapters refuse. Endpoint ownership is
not an implicit obstacle exemption.

PhysicalRoutingAdmissionError is an additive ContractError subclass with stable
category and reason. Existing ContractError callers continue working. No runtime
classification uses human-readable exception substrings. No model schema or
persisted verification-status field is added.

## Physical checks

Canonical cardinal lines map to finite flat-ended cylinders; quarter arcs map to
normal-disc quarter tori. All geometry is exact Fraction metres derived from
canonical nanometre ticks. No snapping, polyline substitution or spherical caps.

Each obstacle check uses supplied route clearance + canonical blocker clearance
+ 1 micrometre. Structural members have explicit profile extra clearance zero,
while route clearance and margin still apply.

Allowed space is the union of supplied closed boxes, not their bounding box and
not independently eroded boxes. Attained solid extrema prove separation from the
outer bounding-box exterior. A streaming slab decomposition checks closures of
positive-volume uncovered cells inside it. Covered internal seams produce no
forbidden gap. Domain clearance is supplied route clearance + 1 micrometre.

Every adjacent seam has an exact shared-disc proof. Every nonadjacent actual
solid pair must have certified strictly positive separation, with no micrometre
self margin. Exact local certificates are conditional on their checked canonical
prerequisites. Remaining pairs use exact rational parameter-cell coverage;
overlapping outer bounds alone are not a collision witness. Failed refinement
returns unresolved or limited, never safe.

## Deterministic resource policy

The existing total_refinement_cells input is the global charged work budget for
this algorithm profile. Admission is bounded separately. Primitive adaptation,
domain slabs, covered box scans, interval merges, every primitive/cell root pair,
self seams/pairs/children, distance refinements and trace records consume work
before execution. A balanced merge sort reserves n*ceil(log2(n)) comparisons
before sorting, with zero for n <= 1; actual comparisons are informational, do not
control limits and receive no refunds. Equivalent permutations preserve charged
work/verdict semantics, although normalized source hashes and actual comparison
counts may differ. Identical snapshots produce identical receipts.

Kernel refinements are 32/64/128 bits, subject to declared refinement depth.
General self refinement uses bounded depth-first state. Domain cells and primitive
pairs are streamed, never materialized as full Cartesian products.

Capture caps: source 32 MiB/250000 nodes, candidate 16 MiB/250000 nodes,
combined model 48 MiB/500000 nodes, depth 64. Conservative escaped-byte counters
run before copying; canonical encoders only receive detached bounded graphs.
Original source text is byte/depth/node/token checked before ordinary JSON parsing.
Caps are computational limits, not engineering infeasibility.

Reports are capped at 4 MiB, at most 256 diagnostics and 2 MiB retained diagnostic
encodings, with at most 16 exact rational facts per record. Exact values use
numerator/denominator strings, maximum 1024 decimal digits each, never rounded
floats. Counts and a rolling trace hash summarize executed obligations, but the
trace hash is not standalone proof. Fixed bounded terminal serialization is
outside an exhausted proof ledger so resource_limit can always be reported.
