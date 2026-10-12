"""Adapter from a resolved layer preset to the existing GLB visibility boundary.

The GLB exporter already owns grouping and default visibility: ``groups``
reparents member nodes under ``group:<name>`` parents and ``hidden_groups``
marks a group hidden through the ratified ``KHR_node_visibility`` extension,
while ``dimmed_ids`` renders a translucent style where the geometry supports
one. This adapter translates a :class:`~oabm.model.PresetResolution` into
exactly those caller-supplied inputs, and nothing else: it never mutates the
model, changes default export bytes, or invents new GLB features.

Ghosting is disclosed honestly. The exporter can render a ghost (dimmed,
translucent) style for electrical devices, electrical equipment and routes,
including the conductor wires drawn inside a dimmed route, so a ghosted
route ghosts whole; a glazed wall takes the dim only as a lower alpha. For
every other kind (plain walls, slabs, spaces, physical boxes, and the 1.3
support components, interfaces and assemblies) there is no ghost style
today, so those ids still land in the ghosted group node but draw at their
normal appearance, and :attr:`LayerGlbPlan.unsupported_ghost_ids` names
them explicitly rather than silently advertising the behaviour as working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from oabm.model import PresetResolution
from oabm.model.entities import WALL_CONSTRUCTION_GLAZED

if TYPE_CHECKING:  # pragma: no cover - typing only
    from oabm.model import BuildingModel

__all__ = ["LayerGlbPlan", "layer_glb_plan"]


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
) -> LayerGlbPlan:
    """Build the GLB export inputs for one resolved preset."""

    ghost_capable = _ghost_capable_ids(model)
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
    for device in model.electrical_devices:
        capable.add(device.id)
    for equipment in model.electrical_equipment:
        capable.add(equipment.id)
    for route in model.routes:
        capable.add(route.id)
    for wall in model.walls:
        if wall.construction == WALL_CONSTRUCTION_GLAZED:
            # Supported, alpha-only: the glass gets more translucent, never
            # grey (the exporter's dim rule for glazed walls).
            capable.add(wall.id)
    return capable
