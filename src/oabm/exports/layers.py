"""Saved-preset GLB delivery over canonical physical and semantic objects.

Every resolved ID is emitted once. Logical ownership is retained in extras;
state groups flatten the display tree so independently selected children do
not acquire a second parent or inherit accidental hidden state.
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
    #: Unsupported ghost geometry, empty for all current drawable kinds.
    #: Meshless semantic records have no surface to dim.
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

    return set(_universe(model))


def _validate_partition(
    model: BuildingModel, resolution: PresetResolution, *, include_obstacles: bool,
) -> None:
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



def _bounded_layer_request(preset, unassigned, isolate, level_ids):
    """Consume finite string iterables with the existing physical resource profile.

    Charge every occurrence before deduplicating so repeated references and
    infinite generators cannot bypass the item or cumulative byte budget.
    """
    from oabm.routing.physical_capture import Capture, SOURCE_BYTES, MAX_NODES
    from oabm.routing.physical_verify_types import VerificationStop

    budget = Capture(SOURCE_BYTES)
    count = 0
    try:
        for label, value in (("preset", preset), ("unassigned", unassigned)):
            if type(value) is not str:
                raise ContractError(f"layer GLB {label} must be a string")
            budget.copy(value)
        admitted = []
        for label, values in (("isolate", isolate), ("level_ids", level_ids)):
            if isinstance(values, (str, bytes)):
                raise ContractError(f"layer GLB {label} must be an iterable of strings")
            members = set()
            for value in values:
                count += 1
                if count > MAX_NODES:
                    raise VerificationStop("resource_limit", "request_item_limit")
                if type(value) is not str:
                    raise ContractError(f"layer GLB {label} entries must be strings")
                members.add(budget.copy(value))
            admitted.append(tuple(sorted(members)))
        return tuple(admitted)
    except VerificationStop as exc:
        raise ContractError(f"layer GLB request {exc.status}: {exc.reason}") from exc
    except TypeError as exc:
        raise ContractError("layer GLB isolate/level_ids must be iterables of strings") from exc

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
    if type(config) is not LayerSelectionConfig:
        raise ContractError("layer GLB requires a canonical LayerSelectionConfig")
    try:
        # Admit mutable metadata before recursive canonical serialization.
        detached = capture(model, byte_limit=SOURCE_BYTES)
        detached_config = capture(config, byte_limit=SOURCE_BYTES)
        snapshot = BuildingModel.from_json(detached.to_json(indent=None))
        bounded_source_json(snapshot.to_json(indent=None))
        config_text = detached_config.to_json(indent=None)
        bounded_source_json(config_text)
        config = LayerSelectionConfig.from_json(config_text)
    except VerificationStop as exc:
        raise ContractError(f"layer GLB {exc.status}: {exc.reason}") from exc
    except (OverflowError, TypeError, AttributeError, KeyError) as exc:
        raise ContractError("malformed layer GLB source/configuration") from exc
    isolate, level_ids = _bounded_layer_request(preset, unassigned, isolate, level_ids)
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
    reached: set[int] = set()
    pending = list(document["scenes"][document.get("scene", 0)]["nodes"])
    while pending:
        index = pending.pop()
        if index in reached:
            raise ValueError("layer display hierarchy must be a tree with one owner per node")
        reached.add(index)
        pending.extend(nodes[index].get("children", ()))
    if reached != set(range(len(nodes))):
        raise ValueError("layer display hierarchy has orphan nodes")
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
