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
