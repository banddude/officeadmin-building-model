# GLB export (deterministic Blender preview)

`oabm.exports.gltf.to_glb(model, path)` writes the canonical model as a binary
glTF 2.0 file that imports into Blender natively. The export is a derived view:
walls, slabs, space floor plates, devices, electrical equipment and conduit
routes become meshes, and every node carries its canonical identity in its name
and provenance in `extras`.

Geometry whose provenance derivation (or scope status) reads
`inferred`/`proposed` gets a semi-transparent material, so generated geometry
cannot masquerade as observed geometry. The same model always produces
byte-identical output.

## Conductor wires

Each conductor is drawn as a thin wire tube following the centerline of every
route it rides, so the conduit routing and the wires inside it are both
visible in Blender:

- `count` on a conductor is real: a conductor with `count: 2` produces two
  wire nodes.
- The wires of one route are bundled deterministically: they are ordered by
  conductor id, then by the index within `count`, and are offset
  perpendicular to each segment onto a ring at half the remaining radius, so
  every wire vertex stays inside the conduit radius and no two wires coincide.
  A route without a `nominal_diameter_m` still draws its wires, bundled in a
  fixed 5 mm radius.
- Every drawn wire shares one visual diameter (3 mm). This is a display
  constant, not an electrical size; the canonical `size` string travels in
  `extras`, and no wire-size table lives in this repository.
- Colours follow the common convention: phase conductors cycle black, red and
  blue by their order within the circuit; neutral is white/grey; ground is
  green. An unknown role gets a neutral purple, and its canonical role string
  stays in `extras`.
- Wires inherit their conductor's provenance translucency: an inferred
  conductor draws semi-transparent, an observed one opaque.

Wire node names are `conductor:<id>#route:<route_id>#<n>` (scheme prefixes are
never doubled). Extras carry `circuit_id`, `role`, `size` when present,
`derivation` when present, `route_id`, and `canonical_id`. The route node
keeps its `extras["conductors"]` list.

The `to_glb` summary dict reports the drawn wire count as
`conductor_wires`.

## Display options

Every display option is a keyword-only `to_glb` parameter that defaults off,
and with all options at their defaults the output is byte-identical to the
plain export. Display choices are labeled rendering parameters, never silent
constants, and none of them flow back into the canonical model, IFC or
quantities (the #88 ruling). The summary dict reports `reference_planes`,
`dimmed` and `emphasized` counts, and — when grouping is requested — the
`groups` member counts and `unmatched_group_ids`.

### Reference planes

`reference_planes=True` adds, per level:

- a **floor plane** at the level's `elevation_m`, when the level has no
  canonical slab (a canonical slab is already drawn);
- a **ceiling plane** at `elevation_m + height_m`, when the level has no
  canonical ceiling and a known `height_m` — never a hard-coded height. A
  level without `height_m` gets no ceiling plane, and its floor plane's
  extras carry `"ceiling": "no level height"` as the reason.

Both span the plan bounding box of everything drawn on the level (wall
centerlines, slab and space footprints, device and equipment positions, and
route points of routes assigned via their start port's owner) plus
`reference_margin_m` (default 0.5 m). Both are double-sided two-triangle
quads in a light neutral grey, translucent BLEND materials whose alphas are
the `reference_floor_alpha` (default 0.25) and `reference_ceiling_alpha`
(default 0.08) parameters. A level with no drawn content gets no plane.

Node names are `reference:floor#<level_id>` and
`reference:ceiling#<level_id>`. Extras carry `reference_plane`, `canonical:
false`, `source`, `extent`, `margin_m`, `alpha`, `level_id` and
`level_confidence`, so every plane discloses its own parameters in Blender's
custom properties.

### Caller-dimmed entities

`dimmed_ids` lists canonical device, equipment or route ids to draw in a grey
translucent style; `dimmed_color` (default grey) and `dimmed_alpha` (default
0.3) shape that style. The caller decides which ids to dim and why; the
exporter attaches no meaning to the choice and only draws the style.
Dimmed entities get a `"<class>-dimmed"` BLEND material and
`extras["display"] = "dimmed (caller-supplied)"`; the dimmed style also wins
over the low-voltage colour below. Ids that match nothing are ignored, and
the summary reports how many entities were actually dimmed as `dimmed`.

### Caller-emphasized entities

`emphasized_ids` lists canonical device, equipment or route ids to draw in
their normal class colour at full opacity, so new work reads in full colour
while `dimmed_ids` ids stay faded. The caller decides which ids to emphasize
and why; the exporter attaches no meaning to the choice and only draws the
style. Emphasized entities get an opaque `"<class>-emphasized"` material with
no BLEND and `extras["display"] = "emphasized (caller-supplied)"`; their
`extras["derivation"]` is unchanged, so a plan-derived device still discloses
that its mounting height is a rule even though it draws at full colour. When
an id is in both sets, dimmed wins and the report counts it only as dimmed.
Ids that match nothing are ignored, and the summary reports how many entities
were actually emphasized as `emphasized`.

### Caller-supplied node groups

`groups` maps a caller-chosen group name (for example `"ALTERNATES"`) to the
canonical entity ids in it — devices, equipment, routes, walls, any exported
entity. Each group becomes one parent node named `group:<name>`, placed at
the scene root after all the other root nodes, with the groups sorted by
name; the members' nodes are reparented under it. A route member brings its
drawn wire nodes (`conductor:<id>#route:<route id>#<n>`) with it, so hiding
one group hides the alternate devices *and their conduit with the wires
inside*. The exporter never decides what belongs together — it only draws
the grouping the caller names, and the group node says so in its `extras`:
`group` (the name), `display` = `"caller-supplied group"`, and `members`
(the child count).

An id may belong to at most one group; a duplicate raises `ValueError`. Ids
that match nothing are ignored and counted in the summary as
`unmatched_group_ids`, and the summary reports each group as
`groups: {name: member_count}`.

`hidden_groups` names the groups that start hidden: the group node gets
`extras["hidden_by_default"] = true` and the ratified `KHR_node_visibility`
extension with `"visible": false`, which hides the node and its whole
subtree in supporting viewers (Blender, three.js). The extension is listed
in `extensionsUsed` and never in `extensionsRequired`, so a viewer without
it still loads the file and simply shows the group — one click to hide.

### Low-voltage device colour

The device types `data_outlet`, `catv_outlet`, `telephone_outlet`,
`junction_box_data`, `speaker` and `access_control_device` draw in teal
instead of their geometry class colour. This is a colour class only: geometry
comes from the unchanged shape classification, and `combination_outlet`
stays an outlet (red). The colour applies whenever the device is not dimmed.

### Names in extras

When a wall, device, electrical equipment or route has a non-empty canonical
`name`, it travels as `extras["name"]` and shows up among Blender's custom
properties — so a labeled route such as a low-voltage stub-up can be read
directly in the viewer.
