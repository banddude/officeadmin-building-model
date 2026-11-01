"""Atomic, explicit edits of existing finite preset assignments.

Requests are the new complete selection for each edited assignment; omitted
assignments retain their exact canonical records. Source facts are never edited.
"""
from dataclasses import replace

from oabm.model import BuildingModel, ContractError
from .catalog import DEFAULT_PRESET_CATALOG
from .opening_insert_presets import DEFAULT_OPENING_INSERT_CATALOG
from .suspended_ceiling_catalog import DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG
from .wall import WallAssemblyRequest, build_wall_assembly
from .opening_inserts import OpeningInsertAssemblyRequest, build_opening_insert_assembly
from .suspended_ceiling import SuspendedCeilingAssemblyRequest, build_suspended_ceiling_assembly


class AssemblyRegenerationError(ContractError):
    """An explicit edit cannot be applied safely to this dependency graph."""


def regenerate_building_assemblies(
    model: BuildingModel, requests: tuple, *,
    wall_catalog=DEFAULT_PRESET_CATALOG,
    opening_insert_catalog=DEFAULT_OPENING_INSERT_CATALOG,
    suspended_ceiling_catalog=DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
) -> BuildingModel:
    """Replace selected existing assignments atomically using finite presets.

    IDs and hosts must match existing assignments. This operation edits preset
    selections/inputs, not observed wall/opening/ceiling/deck facts. Supply wall
    and dependent insert requests together when editing an inserted wall.
    Routes are refused: retain the pre-routing model, regenerate it, then rerun
    domain derivation and routing. No route or dependent record is discarded.
    """
    if not isinstance(model, BuildingModel):
        raise AssemblyRegenerationError("model must be a canonical BuildingModel")
    if not isinstance(requests, tuple) or not requests:
        raise AssemblyRegenerationError("requests must be a nonempty tuple of typed complete replacement selections")
    if len(requests) > len(model.building_assemblies):
        raise AssemblyRegenerationError("replacement request count exceeds existing assignment count")
    if model.routes:
        raise AssemblyRegenerationError(
            "requires_pre_routing_model: regeneration refuses existing legacy or physical routes; "
            "regenerate the retained pre-routing model, derive fresh allowed domains/obstacles, "
            "then rerun routing and dependent selections; no routes were removed")
    kinds = {
        WallAssemblyRequest: ("wall", "wall_id", build_wall_assembly),
        OpeningInsertAssemblyRequest: ("opening_insert", "opening_id", build_opening_insert_assembly),
        SuspendedCeilingAssemblyRequest: ("suspended_ceiling", "ceiling_id", build_suspended_ceiling_assembly),
    }
    catalogs = {"wall": wall_catalog, "opening_insert": opening_insert_catalog,
                "suspended_ceiling": suspended_ceiling_catalog}
    expected_catalog_types = {"wall": type(DEFAULT_PRESET_CATALOG),
        "opening_insert": type(DEFAULT_OPENING_INSERT_CATALOG),
        "suspended_ceiling": type(DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG)}
    if any(type(catalog) is not expected_catalog_types[kind] for kind, catalog in catalogs.items()):
        raise AssemblyRegenerationError("catalogs must be typed finite producer catalogs")
    assignments = {row.id: row for row in model.building_assemblies}
    selected = {}
    for request in requests:
        if type(request) not in kinds:
            raise AssemblyRegenerationError("unsupported request type; only wall, opening insert and suspended ceiling selections are supported")
        kind, host_field, _ = kinds[type(request)]
        old = assignments.get(request.assembly_id)
        if old is None or old.assembly_kind != kind or old.host_id != getattr(request, host_field):
            raise AssemblyRegenerationError(f"{request.assembly_id}: edit must preserve an existing assignment's kind and host")
        catalog = catalogs[kind]
        if (old.catalog_id, old.catalog_revision) != (catalog.catalog_id, catalog.catalog_revision):
            raise AssemblyRegenerationError(f"{old.id}: catalog identity/revision mismatch; supply its explicit finite catalog; no implicit recipe migration")
        if old.id in selected:
            raise AssemblyRegenerationError(f"{old.id}: duplicate replacement selection")
        selected[old.id] = request
    edited_walls = {assignments[key].host_id for key in selected
                    if assignments[key].assembly_kind == "wall"}
    opening_hosts = {row.id: row.host_id for row in model.openings}
    for assignment in model.building_assemblies:
        if (assignment.assembly_kind == "opening_insert" and
                opening_hosts[assignment.host_id] in edited_walls and assignment.id not in selected):
            raise AssemblyRegenerationError(
                f"{assignment.id}: dependent insert requires its complete replacement request with the edited wall")
    owned = {part.id for part in model.assembly_components if part.assembly_id in selected}
    try:
        candidate = replace(model,
            building_assemblies=tuple(row for row in model.building_assemblies if row.id not in selected),
            assembly_components=tuple(row for row in model.assembly_components if row.id not in owned))
        # Host assemblies must exist before their opening inserts. Sorting by ID
        # within each producer also makes request permutation immaterial.
        order = {WallAssemblyRequest: 0, OpeningInsertAssemblyRequest: 1, SuspendedCeilingAssemblyRequest: 2}
        for request in sorted(requests, key=lambda row: (order[type(row)], row.assembly_id)):
            kind = kinds[type(request)][0]
            candidate = kinds[type(request)][2](candidate, request, catalog=catalogs[kind])
        # Preserve original collection slots for surviving semantic identities;
        # append new components in deterministic ID order.
        new_assignments = {row.id: row for row in candidate.building_assemblies}
        new_parts = {row.id: row for row in candidate.assembly_components}
        old_ids = {row.id for row in model.assembly_components}
        return replace(candidate,
            building_assemblies=tuple(new_assignments[row.id] for row in model.building_assemblies),
            assembly_components=tuple(new_parts[row.id] for row in model.assembly_components if row.id in new_parts)
                + tuple(new_parts[key] for key in sorted(new_parts.keys() - old_ids)))
    except ContractError as error:
        raise AssemblyRegenerationError(
            f"regeneration refused atomically: {error}; retain the original model and resolve the explicit input or dependent reference") from error
