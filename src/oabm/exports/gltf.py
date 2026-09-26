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
- Optional display features are opt-in, labeled rendering parameters (the
  issue #88 ruling on display choices): every option is keyword-only and
  defaults off, with all options at their defaults the output is
  byte-identical, and none of them flow back into the canonical model, IFC
  or quantities.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any, Iterable, NamedTuple

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
    # Low-voltage device work (data, TV, telephone, speakers, access control)
    # reads apart from power devices at a glance. A display colour class only:
    # it changes no geometry, and a caller-dimmed device keeps the dimmed
    # style instead.
    "low_voltage": (0.10, 0.65, 0.70),
    "wall": (0.72, 0.72, 0.70),
    "slab": (0.55, 0.55, 0.55),
    "space": (0.45, 0.50, 0.55),
    "route": (0.60, 0.60, 0.62),
    # Conductor wire colours follow the common convention: phases cycle
    # black/red/blue by order within the circuit, neutral is white/grey,
    # ground is green, and an unknown role gets a neutral purple (and keeps
    # its role in extras). These are display colours, not an electrical
    # classification.
    "wire-phase-1": (0.05, 0.05, 0.05),
    "wire-phase-2": (0.80, 0.10, 0.10),
    "wire-phase-3": (0.15, 0.25, 0.85),
    "wire-neutral": (0.85, 0.85, 0.87),
    "wire-ground": (0.10, 0.65, 0.15),
    "wire-unknown": (0.50, 0.35, 0.70),
}
_TRANSLUCENT_ALPHA = 0.35

#: Light neutral grey for the opt-in reference planes. Display colour only;
#: each plane's translucency is a caller parameter (floor and ceiling alphas),
#: disclosed per plane in its ``extras``.
_REFERENCE_PLANE_COLOUR = (0.82, 0.82, 0.80)

#: The ``extras["display"]`` value on caller-dimmed entities. It states who
#: chose the dimming: the caller, never the exporter.
_DIMMED_DISPLAY = "dimmed (caller-supplied)"

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
# Colour class only: these types draw teal instead of their geometry class
# colour. ``_device_class`` stays the sole shaper of geometry, and a dimmed
# device keeps the dimmed style instead of this colour.
_LOW_VOLTAGE_TYPES = frozenset({
    "data_outlet", "catv_outlet", "telephone_outlet", "junction_box_data",
    "speaker", "access_control_device",
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

#: Every drawn wire shares one visual diameter. It is a display constant so
#: wires stay visible inside a drawn conduit; it deliberately carries no AWG
#: size table — canonical conductor ``size`` strings travel in ``extras``.
_WIRE_DIAMETER_M = 0.003
_WIRE_RADIUS_M = _WIRE_DIAMETER_M / 2.0

#: Bundle radius the wires of one route are laid out in when the route
#: carries no ``nominal_diameter_m`` to fit inside.
_WIRE_BUNDLE_RADIUS_M = 0.005


class _DisplayOptions(NamedTuple):
    """Labeled rendering parameters for the optional display-only features.

    Every field is a viewer concern disclosed by an explicit keyword-only
    ``to_glb`` option, never a silent constant (the issue #88 ruling on
    display choices). None of them flow back into the canonical model, IFC or
    quantities, and with every option at its default the exported bytes are
    unchanged.
    """

    reference_planes: bool = False
    reference_floor_alpha: float = 0.25
    reference_ceiling_alpha: float = 0.08
    reference_margin_m: float = 0.5
    dimmed_ids: frozenset[str] = frozenset()
    dimmed_alpha: float = 0.3
    dimmed_color: tuple[float, float, float] = (0.62, 0.62, 0.62)


def to_glb(
    model: BuildingModel,
    path: str | Path,
    *,
    reference_planes: bool = False,
    reference_floor_alpha: float = 0.25,
    reference_ceiling_alpha: float = 0.08,
    reference_margin_m: float = 0.5,
    dimmed_ids: Iterable[str] = (),
    dimmed_alpha: float = 0.3,
    dimmed_color: tuple[float, float, float] = (0.62, 0.62, 0.62),
) -> dict[str, Any]:
    """Write ``model`` as a binary glTF 2.0 file and return a summary dict.

    The export is a deterministic derived view: walls, slabs, space floor
    plates, devices, electrical equipment and conduit routes become meshes;
    canonical identity and provenance travel in node names and ``extras``.

    The keyword-only options are display-only rendering parameters, each
    defaulting off:

    - ``reference_planes`` adds a translucent floor plane per level that has
      no canonical slab, and — where the level height is known — a fainter
      ceiling plane per level that has no canonical ceiling, so heights have
      something to be read against in the viewer. The planes span the level's
      plan bounding box plus ``reference_margin_m``; the two alphas set their
      translucency. Levels with no drawn content get no plane.
    - ``dimmed_ids`` lists canonical device, equipment or route ids the
      caller wants drawn in a grey translucent style shaped by
      ``dimmed_color`` and ``dimmed_alpha``. What dimming means (existing-
      to-remain, for example) is the caller's decision; ids that match
      nothing are ignored.
    """

    options = _DisplayOptions(
        reference_planes=reference_planes,
        reference_floor_alpha=reference_floor_alpha,
        reference_ceiling_alpha=reference_ceiling_alpha,
        reference_margin_m=reference_margin_m,
        dimmed_ids=frozenset(dimmed_ids),
        dimmed_alpha=dimmed_alpha,
        dimmed_color=dimmed_color,
    )
    document, binary, summary_counts = _build_document(model, options)
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
    options: _DisplayOptions,
) -> tuple[dict[str, Any], bytes, dict[str, int]]:
    """Return (glTF JSON dict, BIN chunk bytes, per-kind summary counts)."""

    level_ids = {level.id for level in model.levels}
    level_heights: dict[str, float | None] = {
        level.id: level.height_m for level in model.levels
    }
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
        height_m, height_source = _wall_height(entity, level_heights)
        extras = _extras(entity, "wall", entity.level_id)
        extras["height_source"] = height_source
        _add_name(extras, entity)
        add({
            "name": entity.id,
            "vertices": _wall_vertices(entity, height_m),
            "material_key": ("wall", _is_derived(entity.provenance, entity.attributes)),
            "extras": extras,
        })
    for entity in model.slabs:
        add({
            "name": entity.id,
            "vertices": _prism_vertices(
                entity.footprint.points,
                min(point.z for point in entity.footprint.points),
                min(point.z for point in entity.footprint.points) + entity.thickness_m,
            ),
            "material_key": ("slab", _is_derived(entity.provenance, entity.attributes)),
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
            "material_key": ("space", _is_derived(entity.provenance, entity.attributes)),
            "extras": _extras(entity, "space", entity.level_id),
        })
    dimmed_count = 0
    for entity in (*model.electrical_devices, *model.electrical_equipment):
        device_type = getattr(entity, "device_type", None) or getattr(
            entity, "equipment_type", ""
        )
        extras = _extras(
            entity,
            device_type,
            entity.level_id if entity.level_id in level_ids else None,
        )
        _add_name(extras, entity)
        if entity.id in options.dimmed_ids:
            dimmed_count += 1
            extras["display"] = _DIMMED_DISPLAY
            material_key: tuple[Any, ...] = ("dimmed", _device_colour_class(device_type))
        else:
            material_key = (
                _device_colour_class(device_type),
                _is_derived(entity.provenance, entity.attributes),
            )
        add({
            "name": entity.id,
            "vertices": _device_vertices(entity, device_type),
            "material_key": material_key,
            "extras": extras,
            "pose": entity.pose,
        })
    for entity in model.routes:
        extras = _route_extras(model, entity)
        _add_name(extras, entity)
        if entity.id in options.dimmed_ids:
            dimmed_count += 1
            extras["display"] = _DIMMED_DISPLAY
            material_key = ("dimmed", "route")
        else:
            material_key = ("route", _is_derived(entity.provenance, entity.attributes))
        add({
            "name": entity.id,
            "vertices": _route_vertices(entity),
            "material_key": material_key,
            "extras": extras,
        })
    wire_entries = _wire_entries(model)
    for entry in wire_entries:
        add(entry)
    reference_entries: list[dict[str, Any]] = []
    if options.reference_planes:
        reference_entries = _reference_plane_entries(model, options)
        for entry in reference_entries:
            add(entry)

    # Second pass: assemble glTF structures deterministically.
    entries.sort(key=lambda entry: entry["name"])
    material_keys = sorted({entry["material_key"] for entry in entries})
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
                "material": material_index[entry["material_key"]],
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
            _material_for_key(key, options) for key in material_keys
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
        "conductor_wires": len(wire_entries),
        "reference_planes": len(reference_entries),
        "dimmed": dimmed_count,
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


def _material_for_key(key: tuple[Any, ...], options: _DisplayOptions) -> dict[str, Any]:
    """Material for a sorted material key: plain, caller-dimmed or reference."""

    if key[0] == "dimmed":
        return _dimmed_material(key[1], options)
    if key[0] == "reference":
        return _reference_material(key[1], options)
    return _material(key[0], key[1])


def _dimmed_material(material_class: str, options: _DisplayOptions) -> dict[str, Any]:
    """Caller-supplied dimmed style: grey, translucent (BLEND), per class.

    The colour and alpha are the caller's ``dimmed_color``/``dimmed_alpha``
    parameters, so both the choice of what to dim and its look stay outside
    the exporter.
    """

    red, green, blue = options.dimmed_color
    return {
        "name": f"{material_class}-dimmed",
        "pbrMetallicRoughness": {
            "baseColorFactor": [red, green, blue, options.dimmed_alpha],
            "metallicFactor": 0.6 if material_class == "route" else 0.0,
            "roughnessFactor": 0.4 if material_class == "route" else 0.9,
        },
        "alphaMode": "BLEND",
        "doubleSided": True,
    }


def _reference_material(kind: str, options: _DisplayOptions) -> dict[str, Any]:
    """Translucent BLEND material for a labeled reference plane."""

    alpha = (
        options.reference_floor_alpha if kind == "floor"
        else options.reference_ceiling_alpha
    )
    return {
        "name": f"reference-{kind}",
        "pbrMetallicRoughness": {
            "baseColorFactor": [*_REFERENCE_PLANE_COLOUR, alpha],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.9,
        },
        "alphaMode": "BLEND",
        "doubleSided": True,
    }


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


def _add_name(extras: dict[str, Any], entity: Any) -> None:
    """Carry a canonical entity's non-empty ``name`` into ``extras``.

    The name stays exactly as the model holds it — for walls, devices,
    equipment and routes — so a labeled route such as a low-voltage stub-up
    shows its label among Blender's custom properties.
    """

    if entity.name:
        extras["name"] = entity.name


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


def _horizontal_quad(
    min_x: float, min_y: float, max_x: float, max_y: float, z: float,
) -> list[tuple[float, float, float]]:
    """A horizontal rectangle at height ``z`` as two triangles (+Z up)."""

    corners = [
        (min_x, min_y, z),
        (max_x, min_y, z),
        (max_x, max_y, z),
        (min_x, max_y, z),
    ]
    return [corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]]


def _level_plan_bbox(
    model: BuildingModel, level_id: str
) -> tuple[float, float, float, float] | None:
    """Plan bounding box ``(min_x, min_y, max_x, max_y)`` of a level's contents.

    Covers everything the export draws on the level: wall centerlines, slab
    and space footprints, device and equipment positions, and the centerline
    points of routes assigned to the level by the existing route-level helper.
    ``None`` when nothing is drawn on the level.
    """

    xs: list[float] = []
    ys: list[float] = []

    def take(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    for wall in model.walls:
        if wall.level_id == level_id:
            for point in wall.centerline.points:
                take(point.x, point.y)
    for slab in model.slabs:
        if slab.level_id == level_id:
            for point in slab.footprint.points:
                take(point.x, point.y)
    for space in model.spaces:
        if space.level_id == level_id:
            for point in space.footprint.points:
                take(point.x, point.y)
    for entity in (*model.electrical_devices, *model.electrical_equipment):
        if entity.level_id == level_id:
            take(entity.pose.position.x, entity.pose.position.y)
    for route in model.routes:
        if _route_level_id(model, route) == level_id:
            for point in route.centerline.points:
                take(point.x, point.y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _reference_plane_entries(
    model: BuildingModel, options: _DisplayOptions
) -> list[dict[str, Any]]:
    """Labeled floor and ceiling reference planes per level, never canonical.

    A level with no canonical slab gets a floor plane at its elevation; one
    with no canonical ceiling and a known height gets a ceiling plane at
    elevation + height — never a hard-coded height. Both span the level's
    plan bounding box plus the caller's margin, both carry their parameters
    in ``extras`` with ``canonical: false``, and nothing here flows back into
    the canonical model.
    """

    slab_levels = {slab.level_id for slab in model.slabs}
    ceiling_levels = {ceiling.level_id for ceiling in model.ceilings}
    entries: list[dict[str, Any]] = []
    for level in model.levels:
        bbox = _level_plan_bbox(model, level.id)
        if bbox is None:
            # Nothing is drawn on the level, so there is no extent to anchor
            # a plane to and no heights to read against it.
            continue
        min_x, min_y, max_x, max_y = bbox
        min_x -= options.reference_margin_m
        min_y -= options.reference_margin_m
        max_x += options.reference_margin_m
        max_y += options.reference_margin_m
        wants_floor = level.id not in slab_levels
        wants_ceiling = level.id not in ceiling_levels and level.height_m is not None
        if wants_floor:
            extras: dict[str, Any] = {
                "reference_plane": "floor",
                "canonical": False,
                "source": "level elevation_m",
                "extent": "plan bbox of the level's contents + margin",
                "margin_m": options.reference_margin_m,
                "alpha": options.reference_floor_alpha,
                "level_id": level.id,
                "level_confidence": level.confidence,
            }
            if not wants_ceiling and level.height_m is None:
                # The level height is unknown, so no ceiling plane can be
                # placed; the floor plane records why instead of inventing one.
                extras["ceiling"] = "no level height"
            entries.append({
                "name": f"reference:floor#{level.id}",
                "vertices": _horizontal_quad(
                    min_x, min_y, max_x, max_y, level.elevation_m
                ),
                "material_key": ("reference", "floor"),
                "extras": extras,
            })
        if wants_ceiling:
            entries.append({
                "name": f"reference:ceiling#{level.id}",
                "vertices": _horizontal_quad(
                    min_x, min_y, max_x, max_y,
                    level.elevation_m + level.height_m,
                ),
                "material_key": ("reference", "ceiling"),
                "extras": {
                    "reference_plane": "ceiling",
                    "canonical": False,
                    "source": "level elevation_m + height_m",
                    "extent": "plan bbox of the level's contents + margin",
                    "margin_m": options.reference_margin_m,
                    "alpha": options.reference_ceiling_alpha,
                    "level_id": level.id,
                    "level_confidence": level.confidence,
                },
            })
    return entries


_WIRE_GROUND_ROLES = frozenset({
    "ground", "equipment-ground", "equipment_ground", "grounding", "earth", "egc",
})
_WIRE_NEUTRAL_ROLES = frozenset({"neutral", "grounded", "grounded-conductor", "grounded_conductor"})
_WIRE_PHASE_ROLES = frozenset({"phase", "line", "hot", "ungrounded", "live"})


def _is_phase_role(token: str) -> bool:
    return token in _WIRE_PHASE_ROLES or (token.startswith("l") and token[1:].isdigit())


def _wire_material_class(role: str, phase_index: int | None) -> str:
    """Visual material class for a conductor role.

    A phase conductor cycles black/red/blue by its order among the phase
    conductors of its circuit; neutral, ground and everything else map to
    their convention colours. Role classification stays a display concern:
    the canonical role string travels unchanged in ``extras``.
    """

    token = role.strip().lower()
    if token in _WIRE_GROUND_ROLES:
        return "wire-ground"
    if token in _WIRE_NEUTRAL_ROLES:
        return "wire-neutral"
    if _is_phase_role(token) and phase_index is not None:
        return f"wire-phase-{phase_index % 3 + 1}"
    return "wire-unknown"


def _wire_node_name(conductor_id: str, route_id: str, index: int) -> str:
    """``conductor:<id>#route:<route_id>#<n>`` with scheme prefixes never doubled."""

    prefix = "" if conductor_id.startswith("conductor:") else "conductor:"
    return f"{prefix}{conductor_id}#route:{route_id.removeprefix('route:')}#{index}"


def _wire_entries(model: BuildingModel) -> list[dict[str, Any]]:
    """One wire mesh per conductor per route it rides, bundled in the conduit.

    Layout is deterministic: the wires of one route are ordered by conductor
    id, then by the index within ``count``, and sit on a ring at half the
    remaining radius, perpendicular to each segment, so the bundle fits the
    conduit bore and no two wires land on the same spot. A route without a
    ``nominal_diameter_m`` still carries its wires, laid out in a fixed 5 mm
    bundle.
    """

    route_by_id = {route.id: route for route in model.routes}
    phase_seen: dict[str, int] = {}
    material_by_conductor: dict[str, str] = {}
    bundle: dict[str, list[tuple[str, int, Any]]] = {}
    for conductor in sorted(model.conductors, key=lambda item: item.id):
        token = conductor.role.strip().lower()
        phase_index = None
        if _is_phase_role(token):
            phase_index = phase_seen.get(conductor.circuit_id, 0)
            phase_seen[conductor.circuit_id] = phase_index + 1
        material_by_conductor[conductor.id] = _wire_material_class(conductor.role, phase_index)
        for route_id in dict.fromkeys(conductor.route_ids):
            route = route_by_id.get(route_id)
            if route is None:
                continue
            for index in range(conductor.count):
                bundle.setdefault(route_id, []).append((conductor.id, index, conductor))

    entries: list[dict[str, Any]] = []
    for route_id in sorted(bundle):
        wires = bundle[route_id]
        route = route_by_id[route_id]
        conduit_radius = (
            route.nominal_diameter_m / 2.0 if route.nominal_diameter_m is not None else None
        )
        max_offset = (
            conduit_radius - _WIRE_RADIUS_M if conduit_radius is not None
            else _WIRE_BUNDLE_RADIUS_M
        )
        max_offset = max(max_offset, 0.0)
        total = len(wires)
        for position, (conductor_id, index, conductor) in enumerate(wires):
            # One wire sits on the axis; several share a ring at half the
            # remaining radius, so every vertex stays inside the conduit.
            offset = 0.0 if total == 1 else max_offset / 2.0
            derivation = _derivation(conductor.provenance)
            extras: dict[str, Any] = {
                "circuit_id": conductor.circuit_id,
                "role": conductor.role,
                "route_id": route_id,
                "canonical_id": conductor.id,
            }
            if conductor.size:
                extras["size"] = conductor.size
            if derivation is not None:
                extras["derivation"] = derivation
            entries.append({
                "name": _wire_node_name(conductor.id, route_id, index),
                "vertices": _wire_vertices(route, offset, 2.0 * math.pi * position / total),
                "material_key": (
                    material_by_conductor[conductor.id],
                    _is_derived(conductor.provenance, conductor.attributes),
                ),
                "extras": extras,
            })
    return entries


def _wire_vertices(
    route: Route, offset_radius: float, angle: float
) -> list[tuple[float, float, float]]:
    """Wire tube vertices following a route's centerline at a fixed offset."""

    cos_angle, sin_angle = math.cos(angle), math.sin(angle)
    vertices: list[tuple[float, float, float]] = []
    for start, end in zip(route.centerline.points, route.centerline.points[1:]):
        frame = _segment_frame(start, end)
        if frame is None:
            continue
        _dx, _dy, _dz, ux, uy, uz, vx, vy, vz = frame
        offset = (
            offset_radius * (cos_angle * ux + sin_angle * vx),
            offset_radius * (cos_angle * uy + sin_angle * vy),
            offset_radius * (cos_angle * uz + sin_angle * vz),
        )
        center_a = Point3(x=start.x + offset[0], y=start.y + offset[1], z=start.z + offset[2])
        center_b = Point3(x=end.x + offset[0], y=end.y + offset[1], z=end.z + offset[2])
        vertices.extend(_cylinder_vertices(center_a, center_b, _WIRE_RADIUS_M, _CYLINDER_SIDES))
    return vertices


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


def _device_colour_class(device_type: str) -> str:
    """Display material class of a device: geometry class, low-voltage teal.

    A colour choice only: geometry keeps coming from ``_device_class``, so a
    low-voltage device draws exactly the primitive it always drew, and the
    dimmed style (applied by the caller) wins over this colour.
    """

    token = device_type.lower()
    if token in _LOW_VOLTAGE_TYPES:
        return "low_voltage"
    return _device_class(token)


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


def _wall_height(
    wall: Wall, level_heights: dict[str, float | None]
) -> tuple[float | None, str]:
    """Canonical wall height in metres, plus where it came from.

    The wall's own ``height_m`` wins (source ``"wall"``). Without one, the
    wall's canonical ``Level`` height applies (source ``"level"``). With
    neither, the export keeps the historical flat strip (source ``"none"``)
    rather than inventing a default height.
    """

    if wall.height_m is not None:
        return wall.height_m, "wall"
    if level_heights.get(wall.level_id) is not None:
        return level_heights[wall.level_id], "level"
    return None, "none"


def _wall_vertices(
    wall: Wall, height_m: float | None
) -> list[tuple[float, float, float]]:
    """Vertical prisms, one per centerline segment, of the canonical height.

    Each segment extrudes up from its own base — the lower of the segment's
    two endpoint heights — by the wall's canonical height (its own, else its
    level's). ``height_m`` of ``None`` keeps the historical flat strip.
    """

    vertices: list[tuple[float, float, float]] = []
    half = wall.thickness_m / 2.0
    for start, end in zip(wall.centerline.points, wall.centerline.points[1:]):
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            continue
        nx, ny = -dy / length * half, dx / length * half
        z_bottom = min(start.z, end.z)
        z_top = (z_bottom + height_m) if height_m is not None else z_bottom
        vertices.extend(
            _oriented_box_vertices(
                (start.x, start.y),
                (end.x, end.y),
                (nx, ny),
                z_bottom,
                z_top,
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


def _segment_frame(
    start: Point3, end: Point3
) -> tuple[float, float, float, float, float, float, float, float, float] | None:
    """Deterministic orthonormal segment frame ``(d, u, v)``, or None.

    Zero-length segments have no direction, so they cannot carry a tube. The
    basis picks the reference cardinal axis least aligned with the segment
    direction, the same rule conduit tubes and wire offsets share.
    """

    axis = (end.x - start.x, end.y - start.y, end.z - start.z)
    length = math.sqrt(sum(component * component for component in axis))
    if length <= 1e-12:
        return None
    dx, dy, dz = (component / length for component in axis)
    ref = (1.0, 0.0, 0.0) if abs(dx) < 0.9 else (0.0, 1.0, 0.0)
    ux = ref[1] * dz - ref[2] * dy
    uy = ref[2] * dx - ref[0] * dz
    uz = ref[0] * dy - ref[1] * dx
    u_norm = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux, uy, uz = ux / u_norm, uy / u_norm, uz / u_norm
    vx, vy, vz = dy * uz - dz * uy, dz * ux - dx * uz, dx * uy - dy * ux
    return (dx, dy, dz, ux, uy, uz, vx, vy, vz)


def _cylinder_vertices(
    start: Point3, end: Point3, radius: float, sides: int
) -> list[tuple[float, float, float]]:
    """Cylinder between two world-space points (canonical, +Z up)."""

    frame = _segment_frame(start, end)
    if frame is None:
        return []
    dx, dy, dz, ux, uy, uz, vx, vy, vz = frame

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
