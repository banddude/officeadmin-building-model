from __future__ import annotations

import json
import types
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar, Union, get_args, get_origin, get_type_hints

from .common import (
    SCHEMA_VERSION, ContractError, CoordinateSystem, Entity, Point3, Provenance,
    UnsupportedSchemaVersion, _distance, _finite, _validate_id, _validate_json_value,
)
from .entities import (
    Ceiling, Circuit, Conductor, ElectricalDevice, ElectricalEquipment, Level,
    Obstacle, Opening, Port, Route, RouteConstraint, RouteFitting, Slab, Space, Wall,
)

@dataclass(frozen=True, slots=True, kw_only=True)
class BuildingModel:
    model_id: str
    name: str | None = None
    schema_version: str = SCHEMA_VERSION
    coordinate_system: CoordinateSystem = field(default_factory=CoordinateSystem)
    levels: tuple[Level, ...] = ()
    spaces: tuple[Space, ...] = ()
    walls: tuple[Wall, ...] = ()
    slabs: tuple[Slab, ...] = ()
    ceilings: tuple[Ceiling, ...] = ()
    openings: tuple[Opening, ...] = ()
    electrical_equipment: tuple[ElectricalEquipment, ...] = ()
    electrical_devices: tuple[ElectricalDevice, ...] = ()
    ports: tuple[Port, ...] = ()
    obstacles: tuple[Obstacle, ...] = ()
    route_constraints: tuple[RouteConstraint, ...] = ()
    routes: tuple[Route, ...] = ()
    route_fittings: tuple[RouteFitting, ...] = ()
    circuits: tuple[Circuit, ...] = ()
    conductors: tuple[Conductor, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id(self.model_id, "model_id")
        if self.schema_version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"expected schema_version {SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        _validate_json_value(self.attributes)
        validate_model(self)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> BuildingModel:
        version = document.get("schema_version") if isinstance(document, Mapping) else None
        if version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"expected schema_version {SCHEMA_VERSION!r}, got {version!r}"
            )
        return _decode_dataclass(cls, document)

    @classmethod
    def from_json(cls, payload: str | bytes) -> BuildingModel:
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ContractError("model document root must be an object")
        return cls.from_dict(document)

    @classmethod
    def load(cls, path: str | Path) -> BuildingModel:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def dump(self, path: str | Path, *, indent: int = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")


def _entity_collections(model: BuildingModel) -> tuple[tuple[Entity, ...], ...]:
    return (
        model.levels,
        model.spaces,
        model.walls,
        model.slabs,
        model.ceilings,
        model.openings,
        model.electrical_equipment,
        model.electrical_devices,
        model.ports,
        model.obstacles,
        model.route_constraints,
        model.routes,
        model.route_fittings,
        model.circuits,
        model.conductors,
    )


def validate_model(model: BuildingModel) -> None:
    all_entities = [entity for collection in _entity_collections(model) for entity in collection]
    by_id: dict[str, Entity] = {}
    for entity in all_entities:
        if entity.id == model.model_id:
            raise ContractError(f"entity id {entity.id!r} collides with model_id")
        if entity.id in by_id:
            raise ContractError(f"duplicate entity id {entity.id!r}")
        by_id[entity.id] = entity

    levels = {item.id: item for item in model.levels}
    spaces = {item.id: item for item in model.spaces}
    ports = {item.id: item for item in model.ports}
    routes = {item.id: item for item in model.routes}
    fittings = {item.id: item for item in model.route_fittings}
    circuits = {item.id: item for item in model.circuits}

    def require(ref: str | None, mapping: Mapping[str, Any], label: str) -> None:
        if ref is not None and ref not in mapping:
            raise ContractError(f"{label} references missing id {ref!r}")

    for entity in (*model.spaces, *model.walls, *model.slabs, *model.ceilings):
        require(entity.level_id, levels, f"{entity.id}.level_id")

    hostable = {item.id: item for item in (*model.walls, *model.slabs, *model.ceilings)}
    for opening in model.openings:
        require(opening.host_id, hostable, f"{opening.id}.host_id")

    for item in (*model.electrical_equipment, *model.electrical_devices):
        require(item.level_id, levels, f"{item.id}.level_id")
        require(item.space_id, spaces, f"{item.id}.space_id")
        require(item.host_id, by_id, f"{item.id}.host_id")

    for item in (*model.obstacles, *model.route_constraints):
        require(item.level_id, levels, f"{item.id}.level_id")

    for port in model.ports:
        require(port.owner_id, by_id, f"{port.id}.owner_id")
        if port.owner_id == port.id:
            raise ContractError(f"{port.id} cannot own itself")
        for connected_id in port.connected_port_ids:
            require(connected_id, ports, f"{port.id}.connected_port_ids")
            if connected_id == port.id:
                raise ContractError(f"{port.id} cannot connect to itself")

    for port in model.ports:
        for connected_id in port.connected_port_ids:
            other = ports[connected_id]
            if port.id not in other.connected_port_ids:
                raise ContractError(
                    f"port connectivity must be symmetric: {port.id!r} -> {connected_id!r}"
                )

    for route in model.routes:
        require(route.start_port_id, ports, f"{route.id}.start_port_id")
        require(route.end_port_id, ports, f"{route.id}.end_port_id")
        start = ports[route.start_port_id].pose.position
        end = ports[route.end_port_id].pose.position
        if _distance(route.centerline.points[0], start) > 1e-6:
            raise ContractError(f"{route.id}.centerline must start at the start port position")
        if _distance(route.centerline.points[-1], end) > 1e-6:
            raise ContractError(f"{route.id}.centerline must end at the end port position")
        for fitting_id in route.fitting_ids:
            require(fitting_id, fittings, f"{route.id}.fitting_ids")
            if fittings[fitting_id].route_id != route.id:
                raise ContractError(f"{fitting_id}.route_id does not match route {route.id}")

    for fitting in model.route_fittings:
        require(fitting.route_id, routes, f"{fitting.id}.route_id")
        if fitting.id not in routes[fitting.route_id].fitting_ids:
            raise ContractError(
                f"{fitting.id} must appear in {fitting.route_id}.fitting_ids to preserve fitting order"
            )

    for circuit in model.circuits:
        require(circuit.source_port_id, ports, f"{circuit.id}.source_port_id")
        for port_id in circuit.load_port_ids:
            require(port_id, ports, f"{circuit.id}.load_port_ids")
        for route_id in circuit.route_ids:
            require(route_id, routes, f"{circuit.id}.route_ids")

    for conductor in model.conductors:
        require(conductor.circuit_id, circuits, f"{conductor.id}.circuit_id")
        for route_id in conductor.route_ids:
            require(route_id, routes, f"{conductor.id}.route_ids")


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _encode(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


T = TypeVar("T")


def _decode_dataclass(cls: type[T], data: Mapping[str, Any]) -> T:
    if not isinstance(data, Mapping):
        raise ContractError(f"{cls.__name__} must be an object")
    field_map = {item.name: item for item in fields(cls)}
    unknown = set(data) - set(field_map)
    if unknown:
        raise ContractError(f"{cls.__name__} has unknown field(s): {', '.join(sorted(unknown))}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for item in fields(cls):
        if not item.init:
            if item.name == "kind" and item.name not in data:
                raise ContractError(f"{cls.__name__} is missing required discriminator 'kind'")
            if item.name in data and data[item.name] != item.default:
                raise ContractError(
                    f"{cls.__name__}.{item.name} must equal {item.default!r}"
                )
            continue
        if item.name in data:
            kwargs[item.name] = _decode_value(hints.get(item.name, item.type), data[item.name])
        elif item.default is MISSING and item.default_factory is MISSING:
            raise ContractError(f"{cls.__name__} is missing required field {item.name!r}")
    try:
        return cls(**kwargs)
    except ContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid {cls.__name__}: {exc}") from exc


def _decode_value(tp: Any, value: Any) -> Any:
    if tp is Any:
        _validate_json_value(value)
        return value

    origin = get_origin(tp)
    args = get_args(tp)

    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        errors: list[str] = []
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _decode_value(candidate, value)
            except (ContractError, TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise ContractError("value does not match any allowed type: " + " | ".join(errors))

    if origin is tuple:
        if not isinstance(value, list):
            raise ContractError("tuple fields serialize as JSON arrays")
        item_type = args[0] if args else Any
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(item_type, item) for item in value)
        if len(value) != len(args):
            raise ContractError("tuple length does not match contract")
        return tuple(_decode_value(item_type, item) for item_type, item in zip(args, value))

    if origin is dict:
        if not isinstance(value, dict):
            raise ContractError("mapping field must be a JSON object")
        key_type, value_type = args if args else (str, Any)
        return {
            _decode_value(key_type, key): _decode_value(value_type, item)
            for key, item in value.items()
        }

    if isinstance(tp, type) and is_dataclass(tp):
        return _decode_dataclass(tp, value)

    if tp is str:
        if not isinstance(value, str):
            raise ContractError("expected string")
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise ContractError("expected boolean")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("expected integer")
        return value
    if tp is float:
        return _finite(value, "number")
    if tp is type(None):
        if value is not None:
            raise ContractError("expected null")
        return None

    return value
