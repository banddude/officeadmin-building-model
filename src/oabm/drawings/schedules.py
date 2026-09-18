from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from oabm.model import BuildingModel

from .types import DrawingError, Schedule, ScheduleRow

SUPPORTED_SCHEDULES = (
    "levels",
    "spaces",
    "openings",
    "electrical_equipment",
    "electrical_devices",
    "routes",
    "circuits",
    "conductors",
)


def _name(item: Any) -> str:
    return item.name or item.id


def _level_ids_for_routes(model: BuildingModel) -> dict[str, set[str]]:
    owners = {
        item.id: item
        for item in (*model.electrical_equipment, *model.electrical_devices)
    }
    ports = {item.id: item for item in model.ports}
    result: dict[str, set[str]] = {}
    for route in model.routes:
        levels: set[str] = set()
        for port_id in (route.start_port_id, route.end_port_id):
            owner = owners.get(ports[port_id].owner_id)
            level_id = getattr(owner, "level_id", None) if owner is not None else None
            if level_id is not None:
                levels.add(level_id)
        result[route.id] = levels
    return result


def _opening_levels(model: BuildingModel) -> dict[str, str | None]:
    hosts = {item.id: item for item in (*model.walls, *model.slabs, *model.ceilings)}
    return {
        opening.id: getattr(hosts.get(opening.host_id), "level_id", None)
        for opening in model.openings
    }


def _filtered(items: Iterable[Any], level_id: str | None, get_level) -> list[Any]:
    result = list(items)
    if level_id is not None:
        result = [item for item in result if get_level(item) == level_id]
    return sorted(result, key=lambda item: item.id)


def generate_schedule(
    model: BuildingModel,
    schedule_type: str,
    *,
    schedule_id: str | None = None,
    title: str | None = None,
    level_id: str | None = None,
) -> Schedule:
    if schedule_type not in SUPPORTED_SCHEDULES:
        raise DrawingError(
            f"unsupported schedule type {schedule_type!r}; expected one of {', '.join(SUPPORTED_SCHEDULES)}"
        )
    if level_id is not None and level_id not in {level.id for level in model.levels}:
        raise DrawingError(f"schedule references unknown level {level_id!r}")

    columns: tuple[str, ...]
    rows: list[ScheduleRow]

    if schedule_type == "levels":
        if level_id is not None:
            items = [item for item in model.levels if item.id == level_id]
        else:
            items = sorted(model.levels, key=lambda item: item.id)
        columns = ("name", "elevation_m", "height_m")
        rows = [
            ScheduleRow(item.id, (_name(item), item.elevation_m, item.height_m))
            for item in items
        ]
    elif schedule_type == "spaces":
        items = _filtered(model.spaces, level_id, lambda item: item.level_id)
        columns = ("name", "level_id", "usage", "height_m")
        rows = [
            ScheduleRow(item.id, (_name(item), item.level_id, item.usage, item.height_m))
            for item in items
        ]
    elif schedule_type == "openings":
        opening_levels = _opening_levels(model)
        items = sorted(model.openings, key=lambda item: item.id)
        if level_id is not None:
            items = [item for item in items if opening_levels[item.id] == level_id]
        columns = ("name", "opening_type", "host_id", "level_id", "width_m", "height_m")
        rows = [
            ScheduleRow(
                item.id,
                (_name(item), item.opening_type, item.host_id, opening_levels[item.id], item.size.x, item.size.z),
            )
            for item in items
        ]
    elif schedule_type == "electrical_equipment":
        items = _filtered(model.electrical_equipment, level_id, lambda item: item.level_id)
        columns = ("name", "equipment_type", "level_id", "space_id", "system", "rated_voltage_v")
        rows = [
            ScheduleRow(
                item.id,
                (_name(item), item.equipment_type, item.level_id, item.space_id, item.system, item.rated_voltage_v),
            )
            for item in items
        ]
    elif schedule_type == "electrical_devices":
        items = _filtered(model.electrical_devices, level_id, lambda item: item.level_id)
        columns = ("name", "device_type", "level_id", "space_id", "system", "rated_voltage_v")
        rows = [
            ScheduleRow(
                item.id,
                (_name(item), item.device_type, item.level_id, item.space_id, item.system, item.rated_voltage_v),
            )
            for item in items
        ]
    elif schedule_type == "routes":
        route_levels = _level_ids_for_routes(model)
        items = sorted(model.routes, key=lambda item: item.id)
        if level_id is not None:
            items = [item for item in items if level_id in route_levels[item.id]]
        columns = (
            "name",
            "route_type",
            "start_port_id",
            "end_port_id",
            "nominal_diameter_m",
            "fitting_ids",
        )
        rows = [
            ScheduleRow(
                item.id,
                (
                    _name(item),
                    item.route_type,
                    item.start_port_id,
                    item.end_port_id,
                    item.nominal_diameter_m,
                    list(item.fitting_ids),
                ),
            )
            for item in items
        ]
    elif schedule_type == "circuits":
        items = sorted(model.circuits, key=lambda item: item.id)
        if level_id is not None:
            route_levels = _level_ids_for_routes(model)
            items = [
                item
                for item in items
                if any(level_id in route_levels.get(route_id, set()) for route_id in item.route_ids)
            ]
        columns = (
            "name",
            "circuit_number",
            "source_port_id",
            "load_port_ids",
            "voltage_v",
            "poles",
            "phase",
            "load_va",
            "route_ids",
        )
        rows = [
            ScheduleRow(
                item.id,
                (
                    _name(item),
                    item.circuit_number,
                    item.source_port_id,
                    list(item.load_port_ids),
                    item.voltage_v,
                    item.poles,
                    item.phase,
                    item.load_va,
                    list(item.route_ids),
                ),
            )
            for item in items
        ]
    else:  # conductors
        circuits = {item.id: item for item in model.circuits}
        route_levels = _level_ids_for_routes(model)
        items = sorted(model.conductors, key=lambda item: item.id)
        if level_id is not None:
            def on_level(item: Any) -> bool:
                circuit = circuits[item.circuit_id]
                route_ids = item.route_ids or circuit.route_ids
                return any(level_id in route_levels.get(route_id, set()) for route_id in route_ids)
            items = [item for item in items if on_level(item)]
        columns = (
            "name",
            "circuit_id",
            "role",
            "material",
            "size",
            "count",
            "insulation",
            "route_ids",
        )
        rows = [
            ScheduleRow(
                item.id,
                (
                    _name(item),
                    item.circuit_id,
                    item.role,
                    item.material,
                    item.size,
                    item.count,
                    item.insulation,
                    list(item.route_ids),
                ),
            )
            for item in items
        ]

    suffix = f":{level_id}" if level_id is not None else ""
    return Schedule(
        schedule_id=schedule_id or f"schedule:{schedule_type}{suffix}",
        schedule_type=schedule_type,
        title=title or schedule_type.replace("_", " ").title(),
        source_model_id=model.model_id,
        level_id=level_id,
        columns=columns,
        rows=tuple(rows),
    )


def generate_standard_schedules(
    model: BuildingModel,
    *,
    level_id: str | None = None,
    include_empty: bool = False,
) -> tuple[Schedule, ...]:
    schedules = tuple(
        generate_schedule(model, schedule_type, level_id=level_id)
        for schedule_type in SUPPORTED_SCHEDULES
    )
    if include_empty:
        return schedules
    return tuple(item for item in schedules if item.rows)
