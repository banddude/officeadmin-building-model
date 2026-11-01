"""Adapter from a resolved layer preset to the existing GLB visibility boundary.

The GLB exporter already owns grouping and default visibility: ``groups``
reparents member nodes under ``group:<name>`` parents and ``hidden_groups``
marks a group hidden through the ratified ``KHR_node_visibility`` extension,
while ``dimmed_ids`` renders a translucent style where the geometry supports
one. This adapter translates a :class:`~oabm.model.PresetResolution` into
exactly those caller-supplied inputs, and nothing else: it never mutates the
model, changes default export bytes, or invents new GLB features.

Ghosting is disclosed honestly. The exporter can render a ghost (dimmed,
translucent) style for architectural walls, slabs and spaces, electrical
devices, electrical equipment and routes, including the conductor wires
drawn inside a dimmed route, so a ghosted route ghosts whole. A glazed wall
takes the dim only as a lower alpha. Physical boxes, support components,
interfaces and assemblies, structural members, and v1.5 route-fitting
occurrences still have no caller-ghost style today, so those ids stay in the
ghosted group but retain their normal/envelope appearance, and
:attr:`LayerGlbPlan.unsupported_ghost_ids` names them explicitly rather than
silently advertising the behaviour as working.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import struct
from typing import Any, Iterable

from oabm.model import (
    BuildingModel, ContractError, LayerSelectionConfig, PresetResolution, reject_physical_routes, resolve_preset,
)
from oabm.model.layers import _universe
from oabm.assembly_compat import require_no_populated_building_assemblies

__all__ = ["LayerGlbPlan", "layer_glb_plan", "LayerGlbDelivery", "build_layer_glb"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LayerGlbPlan:
    """Caller-supplied inputs for :func:`oabm.exports.gltf.to_glb`.

    ``groups`` maps one group name per nonempty resolved state; the hidden
    state's group is the one entry of ``hidden_groups``. ``dimmed_ids``
    carries every ghosted id so supported kinds actually render ghosted.
    """

    groups: dict[str, frozenset[str]]
    hidden_groups: frozenset[str]
    dimmed_ids: frozenset[str]
    #: Ghosted ids whose GLB kind has no ghost style today. They stay in the
    #: ghosted group but draw normally; the caller must surface this report
    #: instead of presenting them as ghosted.
    unsupported_ghost_ids: tuple[str, ...]


def layer_glb_plan(
    model: "BuildingModel",
    resolution: PresetResolution,
    *,
    physical_routes: bool = False,
    include_obstacles: bool = False,
) -> LayerGlbPlan:
    """Build GLB inputs; physical/obstacle opt-ins require complete coverage.

    This lower-level adapter cannot prove that a resolution came from a given
    configuration or request. Prefer build_layer_glb for source-bound delivery.
    """

    if type(physical_routes) is not bool or type(include_obstacles) is not bool:
        raise ValueError("physical_routes and include_obstacles must be booleans")
    if not physical_routes:
        reject_physical_routes(model, "layer_glb_plan")
    if physical_routes or include_obstacles:
        _validate_partition(model, resolution, include_obstacles=include_obstacles)
    ghost_capable = _ghost_capable_ids(model)
    if include_obstacles:
        ghost_capable.update(obstacle.id for obstacle in model.obstacles)
    tag = resolution.preset_id
    groups: dict[str, frozenset[str]] = {}
    if resolution.visible_ids:
        groups[f"layer:{tag}:visible"] = frozenset(resolution.visible_ids)
    if resolution.ghosted_ids:
        groups[f"layer:{tag}:ghosted"] = frozenset(resolution.ghosted_ids)
    if resolution.hidden_ids:
        groups[f"layer:{tag}:hidden"] = frozenset(resolution.hidden_ids)
    hidden_groups = frozenset(
        name for name in groups if name.endswith(":hidden")
    )
    return LayerGlbPlan(
        groups=groups,
        hidden_groups=hidden_groups,
        dimmed_ids=frozenset(resolution.ghosted_ids),
        unsupported_ghost_ids=tuple(
            sorted(set(resolution.ghosted_ids) - ghost_capable)
        ),
    )


def _ghost_capable_ids(model: "BuildingModel") -> set[str]:
    """Ids the current GLB exporter can actually draw in a ghost style."""

    capable: set[str] = set()
    for wall in model.walls:
        # Opaque walls use the common grey BLEND style; glazing keeps its
        # material and gets only the lower caller alpha in the GLB exporter.
        capable.add(wall.id)
    for slab in model.slabs:
        capable.add(slab.id)
    for space in model.spaces:
        capable.add(space.id)
    for device in model.electrical_devices:
        capable.add(device.id)
    for equipment in model.electrical_equipment:
        capable.add(equipment.id)
    for route in model.routes:
        capable.add(route.id)
    return capable


def _validate_partition(
    model: BuildingModel, resolution: PresetResolution, *, include_obstacles: bool,
) -> None:
    require_no_populated_building_assemblies(model, "physical layer GLB")
    if model.support_assemblies or model.support_components or model.support_interfaces:
        raise ValueError("physical layer GLB does not support shared support hierarchy regrouping")
    universe = set(_universe(model, include_obstacles=include_obstacles))
    states = (resolution.visible_ids, resolution.hidden_ids, resolution.ghosted_ids)
    flattened = [item for state in states for item in state]
    if len(flattened) != len(set(flattened)) or set(flattened) != universe:
        raise ValueError("layer resolution must be a complete disjoint partition of the drawable universe")


@dataclass(frozen=True, slots=True, kw_only=True)
class LayerGlbDelivery:
    """Detached source, configuration, selection manifest and exact GLB bytes.

    JSON strings and bytes retain no mutable caller state. Hidden geometry is
    retained in the GLB, and remains part of the source model and its quantities.
    """

    source_model_json: str
    config_json: str
    manifest_json: str
    glb_bytes: bytes

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.manifest_json)


def build_layer_glb(
    model: BuildingModel,
    config: LayerSelectionConfig,
    *,
    preset: str,
    unassigned: str,
    isolate: Iterable[str] = (),
    level_ids: Iterable[str] = (),
    physical_routes: bool = False,
    include_obstacles: bool = False,
) -> LayerGlbDelivery:
    """Resolve a saved preset on a detached snapshot and export immediately.

    Unlike an arbitrary PresetResolution, the retained request proves exactly
    which configuration and model were used. Both inclusion flags are passed
    unchanged through resolution, validation and export. No geometry is removed.
    """
    from .gltf import build_glb_bytes

    from oabm.routing.physical_capture import capture, bounded_source_json, SOURCE_BYTES
    from oabm.routing.physical_verify_types import VerificationStop

    if type(model) is not BuildingModel:
        raise ContractError("layer GLB requires a canonical BuildingModel")
    try:
        # Admit mutable metadata before recursive canonical serialization.
        detached = capture(model, byte_limit=SOURCE_BYTES)
        snapshot = BuildingModel.from_json(detached.to_json(indent=None))
        bounded_source_json(snapshot.to_json(indent=None))
    except VerificationStop as exc:
        raise ContractError(f"layer GLB {exc.status}: {exc.reason}") from exc
    except (OverflowError, TypeError, AttributeError, KeyError) as exc:
        raise ContractError("malformed layer GLB source") from exc
    config = LayerSelectionConfig.from_json(config.to_json(indent=None))
    isolate = tuple(sorted(set(isolate)))
    level_ids = tuple(sorted(set(level_ids)))
    resolution = resolve_preset(config, snapshot, preset=preset, unassigned=unassigned,
                                isolate=isolate, level_ids=level_ids,
                                include_obstacles=include_obstacles)
    _validate_partition(snapshot, resolution, include_obstacles=include_obstacles)
    plan = layer_glb_plan(snapshot, resolution, physical_routes=physical_routes,
                          include_obstacles=include_obstacles)
    options = dict(physical_routes=physical_routes, include_obstacles=include_obstacles,
                   groups=plan.groups, hidden_groups=plan.hidden_groups,
                   dimmed_ids=plan.dimmed_ids)
    generated_names = {"group:" + key for key in plan.groups}
    if generated_names & _universe(snapshot, include_obstacles=include_obstacles).keys():
        raise ValueError("canonical id collides with generated display group name")
    payload = build_glb_bytes(snapshot, **options)
    size = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20:20 + size])
    universe = _universe(snapshot, include_obstacles=include_obstacles)
    nodes = document.get("nodes", [])
    names = [node.get("name") for node in nodes]
    name_counts = Counter(names)
    if any(name_counts[entity_id] != 1 for entity_id in universe):
        raise ValueError("layer export must emit each drawable canonical id exactly once")
    group_nodes = [node for node in nodes
                   if node.get("extras", {}).get("display") == "caller-supplied group"
                   and node.get("extras", {}).get("group") in plan.groups]
    if {node["name"] for node in group_nodes} != {"group:" + key for key in plan.groups}:
        raise ValueError("layer export has missing or unexpected display groups")
    # Conductors are derived child nodes riding a route. Validate canonical
    # membership directly rather than comparing child counts that include wires.
    for node in group_nodes:
        key = node["extras"]["group"]
        found = {names[index] for index in node.get("children", [])} & universe.keys()
        if found != set(plan.groups[key]):
            raise ValueError("layer export has unmatched canonical group ids")
    # Wire nodes are derived geometry outside the canonical drawable universe.
    # Prove they ride their owning route instead of disappearing in an ID-only
    # coverage intersection (canonical route IDs need not start with route:).
    memberships = {(conductor.id, route_id) for conductor in snapshot.conductors
                   for route_id in conductor.route_ids}
    groups_by_key = {node["extras"]["group"]: node for node in group_nodes}
    for index, node in enumerate(nodes):
        extras = node.get("extras", {})
        owner = extras.get("route_id")
        if (extras.get("canonical_id"), owner) in memberships:
            expected_group = next(key for key, ids in plan.groups.items() if owner in ids)
            if index not in groups_by_key[expected_group].get("children", []):
                raise ValueError("derived conductor geometry must follow its canonical route display group")
    source_json = snapshot.to_json(indent=None)
    config_json = config.to_json(indent=None)
    serial_options = dict(physical_routes=physical_routes, include_obstacles=include_obstacles,
                          groups={key: sorted(value) for key, value in sorted(plan.groups.items())},
                          hidden_groups=sorted(plan.hidden_groups), dimmed_ids=sorted(plan.dimmed_ids))
    manifest = {
        "kind": "oabm.layer-glb-delivery", "version": "1.0.0",
        "source": {"model_id": snapshot.model_id,
                   "sha256": hashlib.sha256(source_json.encode("utf-8")).hexdigest()},
        "configuration": json.loads(config_json),
        "request": {"preset": preset, "unassigned": unassigned,
                    "isolate": list(isolate), "level_ids": list(level_ids),
                    "physical_routes": physical_routes, "include_obstacles": include_obstacles},
        "resolution": resolution.to_dict(),
        "export_options": serial_options,
        "glb_sha256": hashlib.sha256(payload).hexdigest(),
        "unsupported_ghost_ids": list(plan.unsupported_ghost_ids),
        "unknown_level_hidden_ids": sorted(i for i, level in universe.items()
                                            if level is None and level_ids),
        "unmatched_group_ids": 0,
        "disclosures": [
            "Hidden nodes retain geometry; display presets do not change quantities or clearance obligations.",
            "Hiding requires KHR_node_visibility support; generic viewers may show hidden nodes.",
            "This display export does not certify route clearance or engineering acceptance.",
        ],
    }
    return LayerGlbDelivery(source_model_json=source_json, config_json=config_json,
                            manifest_json=json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False),
                            glb_bytes=payload)
