# Proposal: exact-product route fittings and evidenced interfaces

Status: proposed for independent contract review; no implementation authorization
is implied. Baseline: `48c57b7a119cfa418e013a86c8caccc8d348b7ec`.

## Problem and boundary

`RouteFitting` currently has route ownership, type, pose, nominal diameter and
bend angle. `Port` supplies connection position/direction and nominal diameter,
not a physical mating interface. Quantities group fittings by generic type,
diameter and angle. IFC fitting export carries placement without a catalog Body.
`CatalogProduct` already supplies catalog/revision/product identity. Boxes,
whole-route raceway/cable selections and support components have typed selectors.

The missing boundary is an explicit exact-product decision about an existing
route fitting, with supported connection facts and honest geometry classification.
This proposal does not change routing or approved plans. It does not implement
automatic coupling placement, segment ownership, arbitrary native part import,
code design, or closure of #228/#229. #314 code profiles and #318 source scope
remain outside this work. Existing native IFC intake remains wall/opening only.

## Proposed versioned shape

Reserve the next available explicit minor version after independent acceptance
(currently 1.4 is shipped; do not assume 1.5 remains available). Keep defaults and
all shipped version schemas/bytes unchanged. Add three required collections only
to that new version:

* `fitting_selections`: one nonphysical exact-product decision per existing
  `RouteFitting`. Fields: entity identity/evidence, intent ID, fitting ID,
  `CatalogProduct`, explicit `RuleReference`, geometry basis, optional exterior
  `Size3`, interface IDs, and optional local asset evidence reference.
* `fitting_interfaces`: evidenced interface facts attached to an existing
  fitting-owned canonical `Port`. Fields: entity identity/evidence, selection ID,
  port ID, interface role, connection method, permitted wiring methods,
  optional nominal designation, optional actual accepted OD range in metres,
  optional mating-port ID, and sourced environmental/listing facts.
* `fitting_selection_outcomes`: one active selected/unresolved outcome per intent
  and fitting, requested product/reference when supplied, selected selection ID
  only on success, and closed reason tokens on unresolved results.

These are the proposed typed facts, not an `attributes` interoperability bag.
Use a bounded initial vocabulary: roles `raceway`, `cable`, `enclosure`;
methods `set_screw`, `compression`, `threaded`, `clamp`, `snap_in`;
geometry `manufacturer_native_cad`, `dimension_derived_envelope`,
`metadata_only`. Extend vocabularies only through review. Unsupported methods
remain unresolved. Two catalog interfaces may share the same role; use stable
interface keys, never list position, to distinguish coupling ends.

Environmental facts are independently optional booleans (`dry_location`,
`wet_location`, `raintight`) and listing references are optional explicit strings.
Null means unknown, false means an explicitly evidenced negative claim. No
method implies a rating. Exact cable constructions remain distinct product IDs;
MC, AC and NM-B permissions are separate explicit wiring-method facts. The
implementation proposal must bind these tokens to the existing method vocabulary.
Trade designation is a string and must never populate actual OD. OD range must
have finite positive endpoints with minimum <= maximum. Nonround cable acceptance
is unsupported initially; do not collapse a width/height range to a diameter.

An exterior envelope is centered at the existing fitting pose in its canonical
local frame. Geometry basis qualifies the representation actually emitted:
native CAD evidence may coexist with an emitted dimension-derived envelope, but
must not cause that envelope to be labelled manufacturer-native. A native asset
without supported verified intake is metadata only in this slice. Native CAD
import is a separate contract/consumer implementation. Selection with unknown
dimensions is allowed as metadata-only, with no Body or mesh.

## Ownership and validation

Preserve every existing route/fitting/port ID, pose, direction, centerline,
nominal diameter, connectivity and fitting order. The selector cannot add or
move a port, reroute, split a route or fabricate an enclosure knockout. Missing
required ports/interface facts yield unresolved outcomes. Callers may separately
provide explicit fitting/port modeling through the existing validated boundary.

Each selection references exactly one material fitting owned by an existing
route; consolidation `nonmaterial` fitting nodes cannot receive product material.
The route must list the fitting exactly once. Selection, outcome and requested
product/reference agree; duplicate intents and competing choices fail validation.
Each interface belongs to exactly one selection, uses a port owned by that
selection's fitting, and has a stable unique key. Decisions/outcomes/interfaces
cannot themselves own ports. All IDs are document-global and references resolve.

An explicit mate must be an existing different-owner port with symmetric
canonical connectivity. Compatibility cannot be inferred from nominal diameter
or mere proximity. For an initial bounded selector, cable/raceway eligibility
requires that route's existing exact whole-route selection and matching catalog
facts; enclosure eligibility requires explicit counterpart interface evidence
from the pinned catalog input and existing box/product/port identity. That input
and its hash are retained in the saved receipt. A missing counterpart fact gives
`counterpart_interface_unknown`; it never means a compatible knockout.

Supported mating pairs and geometry containment tolerances must be enumerated in
the implementation issue before coding. Do not interpret `connected_port_ids`
alone as manufacturer compatibility or introduce hidden route-end connectivity.
The first slice supports explicit connector/coupling placements only where all
required facts can be expressed; missing split-route/joint semantics stays
`joint_topology_unsupported`. Shared joints cannot be counted twice. Expanding to
one part owned by several routes requires a separate contract decision.

Compatibility uses three-valued facts: supported, contradicted, unknown. Unknown
or rejected choices remain unresolved, with no selected SKU or added solid.
Initial reasons include missing product/rule/permission/interface, unknown
counterpart, incompatible wiring method/OD/environment, unsupported construction,
conflicting nominal claims and unsupported joint topology. A metadata-only chosen
SKU is distinct from an unresolved product choice; geometry absence stays visible.

## Evidence, immutable catalogs and private assets

Reuse field-qualified `Provenance` semantics. Catalog evidence qualifies consumed
product, size, interface and rating fields independently; user evidence qualifies
request/pose inputs, inferred evidence qualifies decision links/status only.
Do not promote whole-entry confidence to unsupported individual fields.

The loader's versioned public-safe manifest records manufacturer, exact SKU,
document title/URL, source revision (explicit unknown allowed), one-based page or
section locator, UTC retrieval timestamp, retrieved-byte SHA-256 and evidence
field paths. Identity remains `CatalogProduct`'s full three-part key. Revision
content is immutable; duplicates, conflicting same-key payloads, duplicate JSON
keys, nonfinite values and unknown fields fail. Search returns deterministic
exact entries without substituting products or downloading assets. It feeds
existing box/raceway/support entries where their contracts suffice. Missing box
or support dimensions stay unresolved under existing selectors.

Public repository fixtures are synthetic; verified public facts may be curated
later with source citations. Manufacturer binaries stay in caller-selected private
local storage unless redistribution rights are established. Public manifests may
retain source URL/hash/media type/rights status without bytes or private paths.
No binary download occurs during replay, CI, search or model loading. Saved input
receipts pin exact manifest bytes and include any permitted local asset digest;
missing optional assets cannot silently become invented native geometry.

## Consumers and replay

Quantities enrich the existing fitting count with full catalog/revision/product
variants; selected fittings count once, never once again as selections or
interfaces. Generic unresolved fitting counts remain generic with warnings, not
SKU totals. Metadata-only selected products can count once with an explicit
geometry warning. Envelopes supply no material-volume claims.

IFC preserves fitting placement/identity, readable SKU/interface/outcome/basis
properties and native ports. Only supported dimension-derived envelopes create
Body geometry initially. GLB follows the same rule; drawings derive those
envelopes and expose product/outcome schedules. Native managed-property/body
contradictions fail closed, and pose edits preserve scoped user evidence.
QA, schema/serialization, derived schedules and convergence purity gates register
all new collections. Defaults and all legacy consumer fingerprints stay unchanged.

Add an opt-in saved-pipeline stage after routing/convergence with separately
hashed catalog, intents and rule files. Preserve manifest-v1 behavior; introduce
an explicit new saved-manifest version if new keys are needed. Do not rerun
selectors on already selected inventory without the producer ownership checks.
Replay from identical saved bytes must produce deterministic canonical output,
quantities, IFC/GLB and drawings without network calls. The pipeline's completion
report must expose unresolved outcomes, metadata-only geometry and lack of code
verification. Catalog existence alone is not integration completion.

## Migration, review slices and acceptance

Explicit upgrade preserves all old data and adds empty arrays; old schemas reject
new fields even when empty, unknown versions fail, populated downgrade fails.
No migration guesses products/interfaces for legacy fittings.

1. Independently accept this issue/spec and settle version, interface vocabulary,
   exact supported mating pairs and topology bounds before canonical code.
2. Separate small PR: immutable manifest loader/search and existing-selector
   adapters using synthetic entries; no canonical extension required.
3. Reviewed contract/producer PR: new schema/version, validation, migration,
   deterministic explicit fitting decisions and negative cases.
4. Consumer/replay PR: saved handoff and actual quantity/IFC/GLB/drawing evidence.

Required targeted proofs: legacy byte fingerprints; schema and strict loader
failures; idempotence and permutation equality; scoped evidence/unknown fields;
dangling/duplicate/cross-owner links; wrong MC/AC/NM-B construction; nominal-vs-OD
conflicts; compression with unknown raintight rating; explicit supported EMT
connector/coupling cases; rejected and metadata-only products with no guessed
solid; two SKUs of one fitting type producing distinct counts; native edit
contradictions; saved replay of actual routing -> selection -> canonical output
-> exports and counts. No browser tests or large suite run until resources are
coordinated. Full required acceptance checks follow before merge.

Shared needs for the separate batch-preflight/support-coverage lane: expose
outcome counts and geometry availability through existing completion/QA reports,
but do not implement their bundle compatibility, coverage policy or preflight.
Independent acceptance and merge belong to the parent reviewer.
