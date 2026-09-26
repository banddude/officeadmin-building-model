"""Deterministic binary glTF 2.0 (.glb) export of the canonical model.

The output is a derived view of the canonical model, exactly like a drawing:
it never becomes a geometry source. The file imports into Blender natively via
``File > Import > glTF 2.0`` with no add-on.

Design notes:

- Only the standard library is used. The GLB container (JSON chunk + BIN
  chunk, each 4-byte aligned) and the triangle meshes are hand-written.
- Canonical coordinates are metres, right-handed, +Z up (enforced by
  ``CoordinateSystem``). glTF 2.0 is metres, right-handed, +Y up, so points
  map as ``(x, y, z) -> (x, z, -y)`` and pose rotations are conjugated into
  the glTF frame.
- Determinism: the same model always produces byte-identical output. Nodes and
  materials are emitted in sorted order, all JSON is serialized with sorted
  keys, and no timestamps or random identifiers are written.
- Provenance is visible: geometry whose provenance derivation (or scope
  status) reads ``inferred``/``proposed`` gets a semi-transparent material so
  generated geometry cannot masquerade as observed geometry. Every node also
  carries canonical identity in its name and provenance data in ``extras``,
  which Blender shows as custom properties.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

from oabm.model import (
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Route,
    Slab,
    Space,
    Wall,
)

_GLB_MAGIC = 0x46546C67  # "glTF"
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A  # "JSON"
_CHUNK_BIN = 0x004E4942  # "BIN\0"

_COMPONENT_FLOAT = 5126
_TARGET_ARRAY_BUFFER = 34962

#: Sides used for the cylinder tubes along routes and for round device
#: primitives. The brief allows 8-12; 10 keeps tubes readable at 3/4" conduit.
_CYLINDER_SIDES = 10

#: Thin floor plate thickness for space footprints that have no slab of their own.
_SPACE_PLATE_THICKNESS_M = 0.02

# Base colours per geometry class, as linear-ish sRGB triples in [0, 1].
_COLOURS = {
    "outlet": (0.80, 0.15, 0.15),
    "luminaire": (0.90, 0.78, 0.20),
    "switch": (0.20, 0.35, 0.85),
    "sensor": (0.20, 0.65, 0.30),
    "panel": (0.55, 0.55, 0.55),
    "other": (0.55, 0.55, 0.55),
    "wall": (0.72, 0.72, 0.70),
    "slab": (0.55, 0.55, 0.55),
    "space": (0.45, 0.50, 0.55),
    "route": (0.60, 0.60, 0.62),
}
_TRANSLUCENT_ALPHA = 0.35

# Device/equipment classification mirrors the canonical tokens understood by
# the IFC adapter, so both derived views agree on what a type token means.
_OUTLET_TYPES = frozenset({
    "receptacle", "outlet", "power-outlet", "power_outlet", "receptacle_duplex",
    "receptacle_quad", "combination_outlet", "special_purpose_outlet", "evse",
    "data_outlet", "catv_outlet", "tv_outlet", "telephone_outlet", "telephone",
})
_LUMINAIRE_TYPES = frozenset({"luminaire", "light", "light-fixture", "light_fixture", "exit_sign"})
_SWITCH_TYPES = frozenset({"switch", "disconnect"})
_SENSOR_TYPES = frozenset({
    "occupancy_sensor", "smoke_alarm", "smoke_co_alarm", "heat_detector",
    "access_control_device",
})
_PANEL_TYPES = frozenset({
    "panel", "panelboard", "distribution-board", "distribution_board",
    "switchboard", "switchgear",
})

# Fallback primitive dimensions (metres) in the entity's local frame
# (width_x, depth_y, height_z), used when the entity carries no canonical size.
_DEVICE_DEFAULTS = {
    "outlet": (0.08, 0.05, 0.12),
    "luminaire": None,  # flat disc, see _device_vertices
    "switch": (0.08, 0.03, 0.12),
    "sensor": None,  # small cylinder, see _device_vertices
    "panel": (0.5, 0.15, 0.9),
    "other": (0.1, 0.1, 0.1),
}
_LUMINAIRE_RADIUS_M = 0.1
_LUMINAIRE_HEIGHT_M = 0.02
_SENSOR_RADIUS_M = 0.05
_SENSOR_HEIGHT_M = 0.04
_DEFAULT_ROUTE_DIAMETER_M = 0.021  # 3/4"


def to_glb(model: BuildingModel, path: str | Path) -> dict[str, Any]:
    """Write ``model`` as a binary glTF 2.0 file and return a summary dict.

    The export is a deterministic derived view: walls, slabs, space floor
    plates, devices, electrical equipment and conduit routes become meshes;
    canonical identity and provenance travel in node names and ``extras``.
    """

    document, binary, summary_counts = _build_document(model)
    json_bytes = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    binary += b"\x00" * (-len(binary) % 4)

    chunks = [struct.pack("<III", _GLB_MAGIC, _GLB_VERSION, 0)]
    chunk_bytes = 0
    chunks.append(struct.pack("<II", len(json_bytes), _CHUNK_JSON))
    chunks.append(json_bytes)
    chunk_bytes += 8 + len(json_bytes)
    if binary:
        chunks.append(struct.pack("<II", len(binary), _CHUNK_BIN))
        chunks.append(binary)
        chunk_bytes += 8 + len(binary)
    total = 12 + chunk_bytes
    chunks[0] = struct.pack("<III", _GLB_MAGIC, _GLB_VERSION, total)

    payload = b"".join(chunks)
    target = Path(path)
    target.write_bytes(payload)
    return {
        "path": str(target),
        "bytes": len(payload),
        "nodes": len(document.get("nodes", ())),
        "meshes": len(document.get("meshes", ())),
        "materials": len(document.get("materials", ())),
        "buffer_bytes": len(binary),
        **summary_counts,
    }


def _build_document(
    model: BuildingModel,
) -> tuple[dict[str, Any], bytes, dict[str, int]]:
    """Return (glTF JSON dict, BIN chunk bytes, per-kind summary counts)."""

    level_ids = {level.id for level in model.levels}
    # Space floor plates sit on the level's highest slab top so they never
    # z-fight inside a slab; footprint z is the fallback.
    slab_top: dict[str, float] = {}
    for slab in model.slabs:
        top = max(point.z for point in slab.footprint.points) + slab.thickness_m
        slab_top[slab.level_id] = max(slab_top.get(slab.level_id, 0.0), top)

    # First pass: plain Python geometry records, no glTF indices yet.
    entries: list[dict[str, Any]] = []

    def add(entry: dict[str, Any]) -> None:
        entries.append(entry)

    for entity in model.walls:
        add({
            "name": entity.id,
            "vertices": _wall_vertices(entity),
            "material_class": "wall",
            "derived": _is_derived(entity.provenance, entity.attributes),
            "extras": _extras(entity, "wall", entity.level_id),
        })
    for entity in model.slabs:
        add({
            "name": entity.id,
            "vertices": _prism_vertices(
                entity.footprint.points,
                min(point.z for point in entity.footprint.points),
                min(point.z for point in entity.footprint.points) + entity.thickness_m,
            ),
            "material_class": "slab",
            "derived": _is_derived(entity.provenance, entity.attributes),
            "extras": _extras(entity, "slab", entity.level_id),
        })
    for entity in model.spaces:
        base = max(
            (point.z for point in entity.footprint.points),
            default=0.0,
        )
        base = max(base, slab_top.get(entity.level_id, base))
        add({
            "name": entity.id,
            "vertices": _prism_vertices(
                entity.footprint.points,
                base,
                base + _SPACE_PLATE_THICKNESS_M,
            ),
            "material_class": "space",
            "derived": _is_derived(entity.provenance, entity.attributes),
            "extras": _extras(entity, "space", entity.level_id),
        })
    for entity in (*model.electrical_devices, *model.electrical_equipment):
        device_type = getattr(entity, "device_type", None) or getattr(
            entity, "equipment_type", ""
        )
        add({
            "name": entity.id,
            "vertices": _device_vertices(entity, device_type),
            "material_class": _device_class(device_type),
            "derived": _is_derived(entity.provenance, entity.attributes),
            "extras": _extras(
                entity,
                device_type,
                entity.level_id if entity.level_id in level_ids else None,
            ),
            "pose": entity.pose,
        })
    for entity in model.routes:
        add({
            "name": entity.id,
            "vertices": _route_vertices(entity),
            "material_class": "route",
            "derived": _is_derived(entity.provenance, entity.attributes),
            "extras": _route_extras(model, entity),
        })

    # Second pass: assemble glTF structures deterministically.
    entries.sort(key=lambda entry: entry["name"])
    material_keys = sorted({
        (entry["material_class"], entry["derived"]) for entry in entries
    })
    material_index = {key: index for index, key in enumerate(material_keys)}

    nodes: list[dict[str, Any]] = []
    meshes: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    buffer_views: list[dict[str, Any]] = []
    buffer = bytearray()

    for entry in entries:
        byte_offset = len(buffer)
        packed, minimum, maximum = _pack_positions(entry["vertices"])
        buffer += packed
        buffer_views.append({
            "buffer": 0,
            "byteOffset": byte_offset,
            "byteLength": len(packed),
            "target": _TARGET_ARRAY_BUFFER,
        })
        accessors.append({
            "bufferView": len(buffer_views) - 1,
            "componentType": _COMPONENT_FLOAT,
            "count": len(entry["vertices"]),
            "type": "VEC3",
            "min": minimum,
            "max": maximum,
        })
        mesh_index = len(meshes)
        meshes.append({
            "primitives": [{
                "attributes": {"POSITION": len(accessors) - 1},
                "material": material_index[(entry["material_class"], entry["derived"])],
                "mode": 4,
            }],
        })
        node: dict[str, Any] = {
            "name": entry["name"],
            "mesh": mesh_index,
            "extras": entry["extras"],
        }
        pose = entry.get("pose")
        if pose is not None:
            node["translation"] = _yup_point(pose.position)
            rotation = _gltf_rotation(pose.rotation)
            if rotation is not None:
                node["rotation"] = rotation
        nodes.append(node)

    document: dict[str, Any] = {
        "asset": {
            "version": "2.0",
            "generator": "oabm exports.gltf",
            "extras": {
                "canonical_model_id": model.model_id,
                "canonical_up_axis": model.coordinate_system.up_axis,
                "glTF_up_axis": "+Y",
            },
        },
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            _material(material_class, derived)
            for material_class, derived in material_keys
        ],
    }
    if buffer:
        document["bufferViews"] = buffer_views
        document["accessors"] = accessors
        document["buffers"] = [{"byteLength": len(buffer)}]

    counts = {
        "walls": len(model.walls),
        "slabs": len(model.slabs),
        "spaces": len(model.spaces),
        "devices": len(model.electrical_devices),
        "equipment": len(model.electrical_equipment),
        "routes": len(model.routes),
    }
    return document, bytes(buffer), counts


def _material(material_class: str, derived: bool) -> dict[str, Any]:
    red, green, blue = _COLOURS[material_class]
    alpha = _TRANSLUCENT_ALPHA if derived else 1.0
    material: dict[str, Any] = {
        "name": f"{material_class}{'-inferred' if derived else ''}",
        "pbrMetallicRoughness": {
            "baseColorFactor": [red, green, blue, alpha],
            "metallicFactor": 0.6 if material_class == "route" else 0.0,
            "roughnessFactor": 0.4 if material_class == "route" else 0.9,
        },
        "doubleSided": True,
    }
    if derived:
        material["alphaMode"] = "BLEND"
    return material


def _is_derived(provenance: tuple, attributes: dict[str, Any]) -> bool:
    """True when the geometry must not present itself as observed.

    An explicit ``inferred`` provenance derivation, or a ``proposed`` scope
    status, marks generated geometry. Absent values read as observed, matching
    the contract's rule that an unset derivation states nothing.
    """

    if any(record.derivation == "inferred" for record in provenance):
        return True
    return str(attributes.get("scope_status", "")) == "proposed"


def _derivation(provenance: tuple) -> str | list[str] | None:
    values = sorted({record.derivation for record in provenance if record.derivation})
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _extras(entity: Any, canonical_type: str, level_id: str | None) -> dict[str, Any]:
    extras: dict[str, Any] = {"canonical_type": canonical_type}
    if level_id is not None:
        extras["level"] = level_id
    derivation = _derivation(entity.provenance)
    if derivation is not None:
        extras["derivation"] = derivation
    scope_status = entity.attributes.get("scope_status")
    if scope_status is not None:
        extras["scope_status"] = scope_status
    return extras


def _route_extras(model: BuildingModel, route: Route) -> dict[str, Any]:
    extras = _extras(route, route.route_type, _route_level_id(model, route))
    extras["raceway"] = route.route_type
    extras["length_m"] = round(_polyline_length(route.centerline.points), 6)
    if route.nominal_diameter_m is not None:
        extras["nominal_diameter_m"] = route.nominal_diameter_m
    conductors = [
        {
            "id": conductor.id,
            "role": conductor.role,
            **({"size": conductor.size} if conductor.size else {}),
            "count": conductor.count,
        }
        for conductor in sorted(model.conductors, key=lambda item: item.id)
        if route.id in conductor.route_ids
    ]
    if conductors:
        extras["conductors"] = conductors
    return extras


def _route_level_id(model: BuildingModel, route: Route) -> str | None:
    port = next((p for p in model.ports if p.id == route.start_port_id), None)
    if port is None:
        return None
    owner = next(
        (
            item
            for item in (*model.electrical_equipment, *model.electrical_devices)
            if item.id == port.owner_id
        ),
        None,
    )
    return getattr(owner, "level_id", None)


def _device_class(device_type: str) -> str:
    token = device_type.lower()
    if token in _OUTLET_TYPES:
        return "outlet"
    if token in _LUMINAIRE_TYPES:
        return "luminaire"
    if token in _SWITCH_TYPES:
        return "switch"
    if token in _SENSOR_TYPES:
        return "sensor"
    if token in _PANEL_TYPES:
        return "panel"
    return "other"


def _device_vertices(entity: Any, device_type: str) -> list[tuple[float, float, float]]:
    device_class = _device_class(device_type)
    size = getattr(entity, "size", None)
    if size is not None:
        # Canonical size wins over the class default, as an axis-aligned box
        # in the entity's local frame.
        return _local_box_vertices(size.x, size.y, size.z)
    if device_class == "luminaire":
        return _local_cylinder_vertices(
            _LUMINAIRE_RADIUS_M, _LUMINAIRE_HEIGHT_M, _CYLINDER_SIDES
        )
    if device_class == "sensor":
        return _local_cylinder_vertices(
            _SENSOR_RADIUS_M, _SENSOR_HEIGHT_M, _CYLINDER_SIDES
        )
    width, depth, height = _DEVICE_DEFAULTS[device_class]
    return _local_box_vertices(width, depth, height)


def _wall_vertices(wall: Wall) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    half = wall.thickness_m / 2.0
    for start, end in zip(wall.centerline.points, wall.centerline.points[1:]):
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            continue
        nx, ny = -dy / length * half, dx / length * half
        vertices.extend(
            _oriented_box_vertices(
                (start.x, start.y),
                (end.x, end.y),
                (nx, ny),
                start.z,
                end.z,
            )
        )
    return vertices


def _oriented_box_vertices(
    a: tuple[float, float],
    b: tuple[float, float],
    normal: tuple[float, float],
    z_bottom: float,
    z_top: float,
) -> list[tuple[float, float, float]]:
    """Vertices of the box swept along plan segment ``a``-``b``."""

    ax, ay = a
    bx, by = b
    nx, ny = normal
    corners = [
        (ax + nx, ay + ny, z_bottom),
        (bx + nx, by + ny, z_bottom),
        (bx - nx, by - ny, z_bottom),
        (ax - nx, ay - ny, z_bottom),
    ]
    top = [(x, y, z_top) for x, y, _ in corners]
    return [
        # bottom (facing down) and top (facing up)
        corners[0], corners[2], corners[1],
        corners[0], corners[3], corners[2],
        top[0], top[1], top[2],
        top[0], top[2], top[3],
        # sides
        corners[0], corners[1], top[1],
        corners[0], top[1], top[0],
        corners[1], corners[2], top[2],
        corners[1], top[2], top[1],
        corners[2], corners[3], top[3],
        corners[2], top[3], top[2],
        corners[3], corners[0], top[0],
        corners[3], top[0], top[3],
    ]


def _prism_vertices(
    points: tuple[Point3, ...],
    z_bottom: float,
    z_top: float,
) -> list[tuple[float, float, float]]:
    """Fan-triangulated prism over a plan footprint (convex footprints)."""

    plan = [(point.x, point.y) for point in points]
    bottom = [(x, y, z_bottom) for x, y in plan]
    top = [(x, y, z_top) for x, y in plan]
    vertices: list[tuple[float, float, float]] = []
    for index in range(1, len(plan) - 1):
        vertices += [bottom[0], bottom[index + 1], bottom[index]]
        vertices += [top[0], top[index], top[index + 1]]
    for index in range(len(plan)):
        nxt = (index + 1) % len(plan)
        vertices += [bottom[index], bottom[nxt], top[nxt]]
        vertices += [bottom[index], top[nxt], top[index]]
    return vertices


def _local_box_vertices(
    width: float, depth: float, height: float
) -> list[tuple[float, float, float]]:
    """Axis-aligned box centred on the local origin (base at -height/2)."""

    hx, hy, hz = width / 2.0, depth / 2.0, height / 2.0
    corners = [
        (-hx, -hy, -hz),
        (hx, -hy, -hz),
        (hx, hy, -hz),
        (-hx, hy, -hz),
    ]
    top = [(x, y, hz) for x, y, _ in corners]
    return [
        corners[0], corners[2], corners[1],
        corners[0], corners[3], corners[2],
        top[0], top[1], top[2],
        top[0], top[2], top[3],
        corners[0], corners[1], top[1],
        corners[0], top[1], top[0],
        corners[1], corners[2], top[2],
        corners[1], top[2], top[1],
        corners[2], corners[3], top[3],
        corners[2], top[3], top[2],
        corners[3], corners[0], top[0],
        corners[3], top[0], top[3],
    ]


def _local_cylinder_vertices(
    radius: float, height: float, sides: int
) -> list[tuple[float, float, float]]:
    """Z-axis cylinder centred on the local origin."""

    ring = [
        (radius * math.cos(2.0 * math.pi * i / sides),
         radius * math.sin(2.0 * math.pi * i / sides))
        for i in range(sides)
    ]
    bottom = [(x, y, -height / 2.0) for x, y in ring]
    top = [(x, y, height / 2.0) for x, y in ring]
    vertices: list[tuple[float, float, float]] = []
    for index in range(sides):
        nxt = (index + 1) % sides
        vertices += [
            bottom[index], bottom[nxt], top[nxt],
            bottom[index], top[nxt], top[index],
        ]
        # Cap fans from the first rim vertex keep every vertex on the rim,
        # so tube tests can measure axis distance exactly.
        vertices += [bottom[index], bottom[nxt], bottom[0]]
        vertices += [top[index], top[0], top[nxt]]
    return vertices


def _route_vertices(route: Route) -> list[tuple[float, float, float]]:
    radius = (
        route.nominal_diameter_m / 2.0
        if route.nominal_diameter_m is not None
        else _DEFAULT_ROUTE_DIAMETER_M / 2.0
    )
    vertices: list[tuple[float, float, float]] = []
    for start, end in zip(route.centerline.points, route.centerline.points[1:]):
        vertices.extend(_cylinder_vertices(start, end, radius, _CYLINDER_SIDES))
    return vertices


def _cylinder_vertices(
    start: Point3, end: Point3, radius: float, sides: int
) -> list[tuple[float, float, float]]:
    """Cylinder between two world-space points (canonical, +Z up)."""

    axis = (end.x - start.x, end.y - start.y, end.z - start.z)
    length = math.sqrt(sum(component * component for component in axis))
    if length <= 1e-12:
        return []
    dx, dy, dz = (component / length for component in axis)
    # Deterministic orthonormal basis: pick the reference cardinal axis that
    # is least aligned with the segment direction.
    ref = (1.0, 0.0, 0.0) if abs(dx) < 0.9 else (0.0, 1.0, 0.0)
    ux = ref[1] * dz - ref[2] * dy
    uy = ref[2] * dx - ref[0] * dz
    uz = ref[0] * dy - ref[1] * dx
    u_norm = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux, uy, uz = ux / u_norm, uy / u_norm, uz / u_norm
    vx, vy, vz = dy * uz - dz * uy, dz * ux - dx * uz, dx * uy - dy * ux

    def ring(point: Point3) -> list[tuple[float, float, float]]:
        return [
            (
                point.x + radius * (math.cos(2.0 * math.pi * i / sides) * ux
                                    + math.sin(2.0 * math.pi * i / sides) * vx),
                point.y + radius * (math.cos(2.0 * math.pi * i / sides) * uy
                                    + math.sin(2.0 * math.pi * i / sides) * vy),
                point.z + radius * (math.cos(2.0 * math.pi * i / sides) * uz
                                    + math.sin(2.0 * math.pi * i / sides) * vz),
            )
            for i in range(sides)
        ]

    bottom = ring(start)
    top = ring(end)
    vertices: list[tuple[float, float, float]] = []
    for index in range(sides):
        nxt = (index + 1) % sides
        vertices += [
            bottom[index], bottom[nxt], top[nxt],
            bottom[index], top[nxt], top[index],
        ]
    for index in range(sides):
        nxt = (index + 1) % sides
        vertices += [bottom[index], bottom[nxt], bottom[0]]
        vertices += [top[index], top[0], top[nxt]]
    return vertices


def _polyline_length(points: tuple[Point3, ...]) -> float:
    return math.fsum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(points, points[1:])
    )


def _yup_point(point: Point3) -> list[float]:
    """Canonical +Z-up point to glTF +Y-up coordinates."""

    return [point.x, point.z, -point.y]


def _yup_vertex(
    vertex: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Canonical +Z-up vertex to glTF +Y-up coordinates."""

    return (vertex[0], vertex[2], -vertex[1])


def _gltf_rotation(quaternion: Any) -> list[float] | None:
    """Canonical +Z-up rotation quaternion to glTF frame, or None if identity.

    The rotation matrix is conjugated into the glTF frame with the axis map
    ``(x, y, z) -> (x, z, -y)`` and converted back to a sign-canonicalized
    quaternion, following the same convention as the IFC adapter.
    """

    x, y, z, w = (float(quaternion.x), float(quaternion.y),
                  float(quaternion.z), float(quaternion.w))
    matrix = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    # (x, y, z) -> (x, z, -y) conjugation: rows (r0, r2, -r1), then columns
    # in the same pattern.
    gltf = [
        [matrix[0][0], matrix[0][2], -matrix[0][1]],
        [matrix[2][0], matrix[2][2], -matrix[2][1]],
        [-matrix[1][0], -matrix[1][2], matrix[1][1]],
    ]
    trace = gltf[0][0] + gltf[1][1] + gltf[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (gltf[2][1] - gltf[1][2]) / s
        qy = (gltf[0][2] - gltf[2][0]) / s
        qz = (gltf[1][0] - gltf[0][1]) / s
    elif gltf[0][0] > gltf[1][1] and gltf[0][0] > gltf[2][2]:
        s = math.sqrt(1.0 + gltf[0][0] - gltf[1][1] - gltf[2][2]) * 2.0
        qw = (gltf[2][1] - gltf[1][2]) / s
        qx = 0.25 * s
        qy = (gltf[0][1] + gltf[1][0]) / s
        qz = (gltf[0][2] + gltf[2][0]) / s
    elif gltf[1][1] > gltf[2][2]:
        s = math.sqrt(1.0 + gltf[1][1] - gltf[0][0] - gltf[2][2]) * 2.0
        qw = (gltf[0][2] - gltf[2][0]) / s
        qx = (gltf[0][1] + gltf[1][0]) / s
        qy = 0.25 * s
        qz = (gltf[1][2] + gltf[2][1]) / s
    else:
        s = math.sqrt(1.0 + gltf[2][2] - gltf[0][0] - gltf[1][1]) * 2.0
        qw = (gltf[1][0] - gltf[0][1]) / s
        qx = (gltf[0][2] + gltf[2][0]) / s
        qy = (gltf[1][2] + gltf[2][1]) / s
        qz = 0.25 * s
    values = [qx, qy, qz, qw]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        return None
    values = [value / norm for value in values]
    if values[3] < 0.0:
        values = [-value for value in values]
    if all(abs(value) <= 1e-12 for value in values[:3]) and abs(values[3] - 1.0) <= 1e-9:
        return None
    return values


def _pack_positions(
    vertices: list[tuple[float, float, float]],
) -> tuple[bytes, list[float], list[float]]:
    """Pack triangle-soup vertices as float32 VEC3 data with min/max.

    Vertices arrive in canonical +Z-up coordinates and are converted to the
    glTF +Y-up frame here, so every geometry builder can stay canonical.
    """

    packed = struct.pack(f"<{len(vertices) * 3}f", *(
        component for vertex in vertices for component in _yup_vertex(vertex)
    ))
    quantized = [
        struct.unpack("<f", struct.pack("<f", component))[0]
        for vertex in vertices
        for component in _yup_vertex(vertex)
    ]
    minimum = [
        min(quantized[index::3]) for index in range(3)
    ]
    maximum = [
        max(quantized[index::3]) for index in range(3)
    ]
    return packed, minimum, maximum
