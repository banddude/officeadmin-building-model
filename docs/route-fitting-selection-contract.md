# Bounded route-fitting selection contract (parent-reviewed)

This is a bounded follow-on to issue #342, prepared against `main` at
`a38a6713e3885a66f12d06ecf5323d1228ee1544` (merged #347). It records the
parent-reviewed bounded implementation direction; it does not close #342 or
establish user engineering approval, installation/code approval, or product
certification.

## Version and boundary

Add an opt-in canonical contract **1.5.0**. Keep the default at `1.0.0`; all
shipped 1.0–1.4 schemas and their serialized bytes remain unchanged. Version
1.5 adds three required collections: `fitting_selections`,
`fitting_interfaces`, and `fitting_selection_outcomes`. A 1.5 document also
contains every required collection from 1.1–1.4.

`upgrade_to_v1_5(model)` preserves existing IDs, routes, fittings, ports,
connectivity, product selections, evidence, and geometry, and adds only empty
1.5 collections. It guesses no products, ports, interfaces, or mates. Older
versions reject the new keys even when empty; unknown versions/fields fail
closed. A populated 1.5 model cannot be implicitly downgraded. The default
serializer omits 1.5 keys for all earlier versions.

The selector is opt-in and operates only on a caller-authored existing
**material** `RouteFitting` on one existing `Route`. It cannot route, split or
move a route; add or move a fitting/port; change a fitting type or pose; infer a
knockout; or import a CAD part. `RouteFitting.attributes.consolidation.nonmaterial`
and `logical-junction` occurrences are never selectable or counted as products.

## Supported product pairs and topology

There are exactly two supported topologies, each backed by exact product and
interface keys in the pinned catalog-v2 snapshot:

| Case | Fitting | Existing counterpart ports | Required exact catalog pair edges |
| --- | --- | --- | --- |
| Inline EMT coupling | `fitting_type="coupling"`, route type `emt` | Two distinct ports owned by that same route | Fitting `end_a` ↔ exact route product `run_end`; fitting `end_b` ↔ the same exact route product `run_end` |
| Terminal EMT connector | `fitting_type="connector"`, route type `emt` | One port owned by that same route and one port owned by an existing `ElectricalBox` | Fitting `raceway_end` ↔ exact route product `run_end`; fitting `enclosure_end` ↔ exact box product `entry` |

For both cases the route must have one selected, whole-route 1.2+ raceway
selection. The connector's box must already have an exact `CatalogProduct` and
the referenced canonical `Port`; the catalog-v2 pair must name that exact box
product. The fitting must already be listed exactly once by its route. These
are not generalized role/method rules: each product/interface pair is explicit
and independently evidenced. `CatalogProduct` comparison always uses the full
`catalog_id`, `catalog_revision`, and `product_id` key.

Each selected fitting has exactly two distinct stable interface keys. Each key
maps to one existing port owned by the fitting and one distinct counterpart
port. The caller explicitly supplies both the counterpart interface key and
the two canonical port IDs for every binding; the selector never derives one
from an array index or generated port. Each pair is reciprocal in canonical
`Port.connected_port_ids`. A
route-side counterpart is owned by exactly `fitting.route_id`; a box-side
counterpart is owned by the exact selected `ElectricalBox` whose product is
named in the pair. The same counterpart port cannot be reused by both fitting
interfaces or by another selected fitting. No implicit peer, port, or route end
is synthesized by the selector or IFC adapter.

Tee, reducer, multi-way, branching, shared/multi-route, cross-route, conduit-to-
cable, non-round cable, and unspecified fitting types are unsupported and
remain unresolved. Elbows are not in the initial slice: their bend/interface
geometry needs a separate reviewed topology contract. A product that merely
shares a type, nominal/trade size, method token, or nearby pose is not a mate.

## Interface and catalog vocabulary

Canonical `FittingInterface` is a typed entity, not an `attributes` bag. It
records its `selection_id`, stable `interface_key`, source-stated `role` and
`connection_method`, permitted wiring methods, fitting-owned `port_id`, exact
`mating_port_id`, full `mating_product`, `mating_interface_key`, and the
`mating_pair_id` that supports the edge. Interface keys are stable strings,
never array positions. Initial roles are `raceway` and `enclosure` only; cable
is deferred. Initial method tokens are `set_screw`, `compression`, `threaded`,
`clamp`, and `snap_in`. They are descriptive facts, not compatibility rules;
only a sourced exact pair edge establishes a supported pair.

Extend the existing #343 offline catalog loader; do not add a second loader.
Keep `electrical-catalog-v1` parsing, hashes, search, and selector behavior
unchanged (including refusal to turn a v1 `reference` into a fitting). Add an
explicit `electrical-catalog-v2` shape for fitting entries, stable product
interfaces, and source-evidenced `mating_pairs`. Each pair names the complete
fitting product/interface and complete counterpart product/interface keys.
The counterpart may belong to another catalog; the exact counterpart product
must still be present on the canonical route or box owner. Pair references,
interfaces, field evidence, and source IDs are validated before a row is
returned. Pair lookup is exact and deterministic; it never substitutes a
product or fetches a source or asset. The loader records declarations and hashes
but does not authenticate the source or independently verify its claim.

The v2 entry shape is explicit: it retains the v1 product identity/source/evidence
fields and adds `interfaces[]` plus `mating_pairs[]`. An interface has a stable
`key`, `role`, `connection_method`, `permitted_wiring_methods`, optional
`nominal_designation`, optional `actual_accepted_od_m: {min_m, max_m}`, optional
independent environmental/listing facts, and field-path evidence. A mating pair
has a stable `id`, the entry's `interface_key`, full `mating_product` identity,
`mating_interface_key`, and its own source evidence. For example, the two
coupling ends each require a separate pair row to the selected route product's
`run_end`; the exact key is never inferred from list position. The pair record
may reference an external product key, but that does not imply that the other
product's dimensions or ratings were independently loaded or verified.

Canonical `FittingInterface` stores the same product/interface pair plus the
two existing port IDs. Catalog provenance qualifies its role/method/permissions,
optional ratings and pair fields; caller provenance qualifies the binding from
catalog interface keys to those canonical port IDs. The saved selection receipt
pins the exact catalog-v2 bytes/hash and consumed pair row. Do not persist only a
display name or a hash without enough pair keys to replay the decision.

Use the existing finite `Provenance.derivation` values and field-scoped evidence:
each non-null type, dimension, interface, environment/listing, and mating-pair
claim has its own source/locator/hash evidence. Unknown remains null; `false`
means an explicitly sourced negative. `actual_accepted_od_m`, when supplied,
is a finite positive closed range, but 1.5 does not compare it: the current
route/port contract has nominal diameter, not actual OD. `FittingRule` requires
explicit `check_actual_od`, `required_environment` (a possibly empty set of
fact tokens that must be explicitly true), and `required_listing_refs` (possibly
empty exact references). Omission is invalid; empty values mean no caller
requirement, not a guessed default. If `check_actual_od=true`, 1.5 cannot
satisfy it and the outcome is unresolved (`actual_od_unknown`). Otherwise exact
catalog-pair support may be reported with `actual_od_check_performed: false`;
this is never reported as an OD match. A trade-size string is never actual OD.
Environmental/listing facts are checked only if named by the caller's rule; no
connection method implies dry/wet/raintight/listing status. A required fact
must be source-backed true (unknown stays unresolved; sourced false conflicts).
For this initial interface-scoped schema, each caller-required environment
fact and each required listing reference must be explicit on both fitting
interfaces; otherwise selection stays unresolved. This conservative rule does
not infer a product-wide listing from one interface's claim.

The emitted geometry basis is exactly `dimension_derived_envelope` or
`metadata_only`. A dimension-derived envelope requires an evidenced positive
exterior `Size3` in metres, centered and oriented at the existing fitting pose;
it is a display envelope, not manufacturer CAD, bore, insertion depth, thread,
material volume, or dimensional fit proof. Missing size permits metadata-only
selection with no Body/mesh and visible geometry warning. Native asset references
may be retained with rights/hash metadata but never become native geometry in
this slice; no downloader is introduced.

## Geometry and validation

Retain global entity-ID and provenance-scope validation for all three
collections. A selection references exactly one existing route-listed material
fitting, one intent, exact product, and typed caller `RuleReference`. The
outcome matches that intent/fitting/product/rule; there is at most one active
outcome per intent and per fitting. Every selection has exactly one selected
outcome, and unresolved outcomes have no selected selection. Competing intents,
duplicate choices, unreferenced selections/interfaces, and cross-owner links
fail validation. Other producer inventory is refused; repeated identical input
is idempotent, and IDs derive from stable model/intent/fitting keys rather than
list order or mutable product data.

Fitting and counterpart port positions must coincide within **1e-6 m**. Their
nonzero direction vectors are normalized for comparison and must be opposing
with dot product `<= -1 + 1e-6`. A route-owned counterpart must lie within
**1e-6 m** of its route centerline and be parallel or antiparallel to the local
centerline tangent with absolute dot product `>= 1 - 1e-6`. The fitting pose
must lie within **1e-6 m** of that same route centerline. These tolerances check
consistency of explicit canonical geometry only; they are not product clearance,
fit, OD, dimensional compatibility, or installation tolerances. No coordinate
or direction is repaired.

Catalog input and caller input have separate evidence ownership. User/caller
provenance qualifies the requested fitting/product/rule and the explicit
interface-to-port bindings. Catalog provenance qualifies only consumed product,
size, interface, and exact pair fields, preserving per-field scopes and manifest
SHA-256. Inferred provenance is limited to selection/status/reason links and
product-to-port decision bookkeeping. Do not widen evidence scope or copy
catalog confidence onto caller geometry.

## Producer and outcomes

Expose a deterministic explicit producer in `src/oabm/electrical`, gated on
1.5. An intent names one fitting, exact requested `CatalogProduct`, caller
rule, and both caller-supplied interface/port bindings. The catalog must contain
that exact fitting product and both exact mating edges. The rule must explicitly
permit the product for `route_type="emt"` and the exact fitting type. Missing
or conflicting inputs yield a typed unresolved outcome, no selected product
solid, and stable reason tokens; malformed references/duplicate IDs are
contract errors. No first match, default size, or substitute is allowed.

`FittingRule` carries an exact `RuleReference`, allowed product identities,
allowed route/fitting type pairs, required `check_actual_od`,
`required_environment`, and `required_listing_refs` fields. These last three
fields are mandatory inputs even when empty. They are caller policy, not code
tables: a product is not rejected for missing unrequested facts, and the
selector never invents an environmental/listing/OD requirement. No new rule
value implies approval or code compliance.

Selected means only: exact catalog SKU, caller permission, existing topology,
and exact source-cited pair edges were recorded. It does not mean actual OD was
checked, code/listing compliance was verified, geometry was imported, or the
part is installation-ready. Each outcome states
`actual_od_check_performed: false`. A metadata-only selected product remains
distinct from an unresolved choice.

## Required negative cases

The initial closed unresolved reason vocabulary is `nonmaterial_fitting`,
`unsupported_fitting_type`,
`wrong_route_type`, `missing_requested_product`, `missing_catalog_product`,
`missing_rule_reference`, `product_not_permitted`, `missing_route_product`,
`fitting_port_missing`, `counterpart_port_missing`,
`port_connectivity_missing`, `counterpart_interface_unknown`,
`mating_pair_unknown`, `mating_pair_conflict`,
`interface_port_owner_mismatch`, `interface_geometry_mismatch`,
`unsupported_joint_topology`, `incompatible_wiring_method`,
`actual_od_unknown`, `environment_unknown`,
`environment_conflict`, and `listing_reference_missing`. Geometry absence for
a selected metadata-only SKU is a warning, not an unresolved product choice.
A missing target fitting or malformed/dangling canonical reference is a
structural contract error, not a representable selection outcome; unresolved
outcomes are only for an existing caller-authored fitting with an explicit
intent whose decision cannot be completed.

At minimum, deterministic tests reject or leave unresolved: legacy-schema use of
new keys; unsupported catalog/manifest versions and fields; duplicate JSON keys,
product/interface/pair IDs or provenance; dangling/full-key-mismatched products;
missing or duplicate mates; symmetric-connectivity failure; duplicate/reused
counterpart ports; wrong owner, wrong route, wrong exact box/route SKU, or
route with no selected whole-route product; absent fitting ports, route geometry
or caller rule; no catalog pair despite matching nominal diameter or method;
unknown actual OD when the caller explicitly requires it; incompatible
method/wiring facts; geometric position/direction/tangent mismatch;
nonmaterial fitting; elbow/tee/reducer, shared/multi-route, or cable topology;
unknown/contradictory environment or missing listing only when required by the
explicit rule; and absent size for a requested envelope. Positive synthetic fixtures
must cover exactly the two supported topologies, metadata-only behavior,
field-scoped provenance, idempotence, and input permutation equality.

## Migration, consumers, and replay acceptance

Add a standalone strict `oabm-model-v1.5.schema.json` plus typed dataclasses,
version constants, codec/version gates, migration, global-reference/provenance
validation, QA iteration, and docs. Register each new collection in every
existing entity iterator, serialization/decode gate, provenance-scope walk, QA
and purity/convergence check, quantities path, IFC/GLB/drawing adapter, and saved
replay path. Golden fingerprints for 1.0–1.4 and their default consumer outputs
remain unchanged.

Required vertical-slice proof starts from a saved explicit route and its
existing whole-route EMT product selection, applies each supported fitting
intent, then validates canonical JSON and derives quantities, GLB, IFC, and
drawings. Selected physical fittings count once, with full catalog/revision/
product variants; selections/interfaces do not count. Unresolved fittings keep
generic counts and warnings. `logical-junction` remains excluded. Metadata-only
selections count once as selected product records but warn that no geometry is
available; no envelope volume is reported.

GLB emits only an evidenced dimension-derived fitting envelope, with exact
product/interface/outcome extras; metadata-only has no mesh. IFC adds readable
product, pair, outcome, geometry-basis, and `actual_od_check_performed=false`
properties; a Body is emitted only for the dimension-derived envelope. Existing
adapter-generated route-chain ports remain distinct from canonical fitting /
counterpart ports. IFC round-trip preserves canonical IDs, pair references,
connected-port topology, and evidence; managed property/Body contradictions
fail closed. Drawings retain schematic fitting symbols and add stable product/
outcome/interface schedules, with an envelope only when evidenced. View and
schedule outputs are generated for review; they do not claim installation
suitability.

Replay is opt-in and versioned separately from existing saved electrical-job
manifests. Pin canonical input bytes, catalog-v2 bytes and SHA-256, intents,
rules, and emitted artifact hashes. Replay performs no network calls, downloads,
automatic routing, or geometry placement. Identical pinned bytes and semantic inputs must reproduce
identical selected/unresolved canonical output, quantities, GLB, IFC, and drawing
bytes. Its saved replay manifest is `saved-route-fitting-replay-v1`; every input
and expected output is a distinct canonical relative path with exact byte-count
and SHA-256 receipts. Symlinks, path aliases, hard-linked duplicate artifacts,
and input/output file reuse are refused. The base model may have no fitting
decision inventory (reported as `first_application`) or producer-owned prior
fitting decisions. A prior inventory is re-evaluated against the pinned current
intent/rule/catalog bytes and the receipt reports its base row counts plus
`idempotent_reapplication` only when the regenerated decision rows are equal;
changed rows report `reapplication_with_changes`. Expected artifacts are always
freshly regenerated and compared; none are consumed as cached outputs. No
private catalog assets or user drawings enter public fixtures.
Permuting intent, binding, rule, or typed catalog-entry collection order while
keeping the pinned raw catalog bytes and values unchanged must not change those
outputs; re-encoding the catalog itself changes its raw SHA-256 pin by design.

This contract intentionally does not claim a public manufacturer SKU fixture:
fixtures are synthetic until source documents, exact interface/pair claims, and
any redistributable dimensions are independently curated and cited.

### Replay completion visibility

The replay receipt's `fitting_completion` is derived from the freshly
regenerated canonical model, not the base inventory or saved expected artifacts.
It reports selected/unresolved outcome counts, sorted reason counts and
per-outcome reasons/identities, plus selected dimension-derived-envelope versus
metadata-only counts. Unresolved choices have no selected geometry basis.
Metadata-only is a successful product decision without a fitting Body or mesh;
an envelope is not manufacturer CAD, actual-OD verification or fit proof.
Existing source-authenticity, fit, code/listing, native-CAD and engineering
verification disclaimers remain false. These read-only summary fields do not
change the saved manifest format, canonical schemas or generated artifact bytes.
