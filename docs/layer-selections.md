# Layer selections: portable saved selections and explicit membership

Bounded first implementation under issues #237 and #227. This adds portable
backend layer selections and a deterministic resolver over explicit canonical
entity membership. It does not add the OA production control panel, automatic
system recognition, IFC layer presentation, or CAD export integration, and it
does not close the parents.

## What it is

`oabm.model.layers` (with the schema at `contracts/oabm-layers-v1.schema.json`)
defines a versioned JSON configuration of:

- **Layers**: a named `LayerDefinition` with an explicit `member_ids` list of
  canonical entity ids and optional `system`, `trade` and `phase` labels.
  Membership is exactly what the configuration states. Nothing is classified
  from prose, geometry, or nearest-object proximity, and a label is carried
  data, never an inference.
- **Saved presets**: a named `SavedPreset` that assigns every layer of the
  configuration exactly one display state: `visible`, `hidden` or `ghosted`.
  A preset is a total function over the layers; a layer with no state is a
  decode error, so resolution never guesses a default state.

Decoding is strict: unknown fields are rejected at every level, ids must be
canonical ids, duplicate layer or preset ids and names are rejected, a member
listed twice in one layer is rejected, and a layer named in two states of one
preset is rejected. Future schema versions are refused rather than guessed at.

## Resolution

`resolve_preset(config, model, preset=..., isolate=..., level_ids=...,
unassigned=...)` resolves one saved preset against a concrete
`BuildingModel` and returns a `PresetResolution`: sorted, explicit
`visible_ids`, `hidden_ids` and `ghosted_ids`, plus coverage reports. The
semantics, applied in this order:

1. Every configured member id must exist in the model; dangling members,
   unknown levels in the level filter, and unknown isolate references are
   refused with diagnostics naming the offender.
2. An entity in several layers takes the **strictest** state among them:
   `hidden` beats `ghosted` beats `visible`. This is the documented rule for
   overlapping layers; it is independent of layer order.
3. An entity in no layer is **unassigned**. Incomplete coverage is a
   property of the whole configuration, so `unassigned_ids` always reports
   it, independent of `isolate` and of the level filter; isolate can hide
   an unassigned entity but can never make the coverage gap disappear. The
   caller's `unassigned` policy is a required argument, so there is no
   silent default: it decides where unassigned entities land (`visible`
   keeps today's everything-shown behaviour, `hidden` and `ghosted` tuck
   them away) or refuses incomplete coverage outright (`error`, also under
   isolate).
4. A nonempty `isolate` restricts the considered layers to the named ones.
   Their members keep the strictest state among those layers under the
   preset, and every other drawable entity is hidden. Isolating an empty
   layer is legal and renders nothing.
5. A nonempty `level_ids` filter keeps entities whose canonical level is in
   the filter and hides the rest. A route resolves its level from its start
   port's owner, the same documented rule the GLB exporter uses for
   reference planes. An entity with no resolvable level is outside the
   filter and hidden.

The three id sets are always a disjoint, complete partition of the drawable
universe: the entities the current GLB exporter draws as canonical nodes
(walls, slabs, spaces, electrical devices, electrical equipment, routes,
physical electrical boxes, and the 1.3 shared support components,
interfaces and assemblies). Ceilings and openings are not drawn by the GLB
exporter yet, so they are outside the universe; a member that exists in the
model but outside the universe (a port, a raceway selection, a support
selection outcome) is reported in `out_of_scope_member_ids` and never
enters the partition. Conductor wire nodes are counted once, through their
route id, and a support assembly brings its component and interface
children with it exactly once.

## What resolution never claims

- The canonical model and its quantities are unchanged: the configuration is
  not part of `BuildingModel`, and resolving it mutates nothing. Default GLB
  and drawing bytes are unchanged unless a caller explicitly passes the
  derived plan to an exporter.
- A layer named `fire_alarm` with zero drawable members does **not** mean
  fire alarm is modeled. `empty_layer_ids` and `system_coverage` (system
  label to drawable member count) say so numerically; consumers must read
  coverage from the report, never from a layer name's existence.

## GLB handoff

`oabm.exports.layers.layer_glb_plan(model, resolution)` adapts a resolution
to the exporter's existing caller-supplied grouping and visibility boundary:

- one group per nonempty state (`layer:<preset id>:visible|ghosted:hidden`),
  with the hidden state's group in `hidden_groups`, so it starts hidden
  through the ratified `KHR_node_visibility` extension (used, never
  required);
- ghosted ids passed as `dimmed_ids`, so supported kinds actually render
  ghosted (translucent): walls, slabs, spaces, electrical devices,
  electrical equipment and routes. A dimmed route dims the conductor wires
  drawn inside it too, so the conduit ghosts whole; glazed walls keep their
  glass colour and take only the lower caller alpha;
- `unsupported_ghost_ids` names every ghosted id whose kind has no ghost
  style today (physical boxes, support components, interfaces and
  assemblies). They stay in the ghosted group but draw at normal appearance,
  and the caller must surface the report instead of presenting them as
  ghosted.

The adapter adds no new exporter features and does not change the exporter;
with no plan passed, `to_glb` output is byte-identical to its previous
behaviour. Pass the plan straight through:

```python
from oabm.exports import layer_glb_plan, to_glb

resolution = resolve_preset(config, model, preset="preset:phase-1")
plan = layer_glb_plan(model, resolution)
summary = to_glb(model, path, groups=plan.groups,
                 hidden_groups=plan.hidden_groups, dimmed_ids=plan.dimmed_ids)
```

`PresetResolution.to_dict()` is the derived selection/visibility manifest for
other export consumers: sorted id lists plus the coverage reports above.

## Determinism

Resolution is a pure function of the configuration and the model. Permuting
the order of layers, members, or presets in the configuration cannot change
a resolution, and exporting the same plan is byte-identical. Tests cover the
permutation property directly, on the resolver and on actual GLB bytes.

## Remaining gaps

Not delivered here, by scope: the OA production viewer control panel,
automatic system/trade recognition from a plan, IFC presentation of layers,
per-layer CAD export, ghost styles for physical boxes and support records, and
any real panel-recognition gate change. File follow-ups per slice against
#237/#227.
