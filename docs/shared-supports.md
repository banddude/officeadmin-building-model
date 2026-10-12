# Explicit shared supports (contract 1.3)

This completes the bounded physical-inventory slice of #267/#84. It does not
implement automatic spacing, structural loading, code tables, pricing, or
fastener-suitability certification (#228/#229). Catalog envelopes describe
exterior extents, not measured screw/thread profiles or material volume.

## Version and canonical objects

The default remains `1.0.0`. `upgrade_to_v1_3(model)` explicitly preserves all
existing IDs, geometry, boxes, and raceway decisions and adds four required
arrays: `support_assemblies`, `support_components`, `support_interfaces`, and
`support_selection_outcomes`. Shipped 1.0/1.1/1.2 schemas and serialized semantics
remain unchanged; those versions reject the new fields even when empty.
Unknown versions/fields and implicit downgrades fail closed.

A `SupportAssembly` identifies one installed shared assembly, explicit level,
supported route/device/equipment/box references, component/interface membership,
and caller rule reference. It is a grouping, not additional material.
A `SupportComponent` identifies each physical part once, with independent pose,
external `Size3`, catalog product, declared component type, and category
`support` or `fastener`. Component ownership is unique. Coincident same-product
world envelopes, including quaternion-sign and box-rotation symmetries, are
rejected at nanometre comparison precision; distinct poses/envelopes remain
separate inventory.
A `SupportInterface` identifies a posed attachment to an explicit canonical
wall/slab/ceiling host, declared actual substrate, and named components. Its
point must lie within each named component's envelope. Several interfaces may
belong to one assembly; several supported objects may share that assembly.
A `SupportSelectionOutcome` retains one selected or unresolved decision per
intent, requested level/rule, supported references, and typed reason tokens.

## Producer and uncertainty

`oabm.electrical.select_supports(model, intents=..., catalog=..., rules=...)`
requires 1.3. Inputs are typed `SupportIntent`, `SupportPartIntent`,
`SupportInterfaceIntent`, `SupportCatalogEntry`, and `SupportRule`. The caller
supplies actor identity, explicit assembly level, supported references, each
part's requested product/pose, attachment hosts/poses/actual substrates, and a
versioned rule reference with permitted products/substrates. These caller rules
are decision inputs, never evidence of verified jurisdictional approval.

The bounded known-substrate vocabulary is wood, steel, concrete, and masonry.
Unknown, absent, or unsupported substrates produce an unresolved outcome and
no physical assembly/components/interfaces. In particular a wall construction
of `framed` supplies no actual substrate. Missing catalog dimensions/category/
type, pose, rule, or permission also prevents installed inventory. Existing
reference IDs must resolve; malformed or dangling references fail validation.
No missing anchor/screw, host, dimension, or permission is guessed.

All host levels and supported-object levels must be known and agree with the
explicit assembly level. A route's two endpoint-owner levels must both be
known; fitting-owned or level-unknown endpoints are unresolved. Multi-level
assemblies are unsupported and explicitly unresolved, never assigned the
first host's level. An explicit empty intent list is valid but does not prove
support coverage: quantities warn `support_design_unrequested` for routed
models with no support outcomes.

Complete producer recomputation replaces its owned inventory with stable IDs,
not unresolved history. Input and output order is deterministic. Catalog
field-qualified evidence stays qualified; intent evidence covers only actual
user inputs. Derived membership/level associations are inferred. Interface
host-level confidence is bounded by matching host/level evidence, and interface
confidence by attached component confidence. Caller/catalog records retain
independent scopes rather than claiming each other's fields.

## Consumers and edits

IFC exports support parts as `IfcDiscreteAccessory USERDEFINED` and declared
fasteners as `IfcMechanicalFastener USERDEFINED`, with declared `ObjectType`.
Fastener nominal diameter/length stay absent: the Body is only the declared
catalog exterior envelope. Assemblies are native `IfcGroup`s; posed interfaces
and decision outcomes are annotations. Native group membership, host assignment,
storey containment, readable managed projections, and Body envelopes must agree
with canonical records. Managed optional property presence is checked as well
as values. Fabricated managed properties on unregistered objects or empty
inventories are rejected; unrelated custom notes remain allowed.

Native component/interface placement edits are read back as scoped user pose
evidence and still must satisfy attachment-envelope constraints. Changing a
catalog Body envelope, declared category/type/product, containment, grouping,
substrate, or decision status without corresponding canonical data is refused.
All inherited 1.2 raceway native absent-state checks apply to 1.3 as well.

GLB contains each component envelope once and groups it with interface nodes;
metadata preserves supported references, product/rule inputs, uncertainty, and
geometry basis. Per-level drawings include posed components and attachments,
and schedules expose all four record types. Quantities count only physical
components once, independent of how many routes share the assembly; assembly,
interface, and outcome records add no material count. No envelope volume is
reported as material volume. QA registers all records and unresolved coverage.
Support components are not router hard obstacles, and cannot own ports.
PDF convergence preserves empty 1.3 version boundaries and refuses downstream
support inventory rather than discarding or reconstructing it.

`tests/test_shared_supports.py` generates public synthetic router-to-producer
proof with two routes, two hosts/substrates, seven components, and two attachment
interfaces. It verifies consumer counts/readback, malformed references, missing
levels/substrates, scoped evidence/confidence, permutations/recomputation,
managed native contradictions, and shipped consumer fingerprints.
