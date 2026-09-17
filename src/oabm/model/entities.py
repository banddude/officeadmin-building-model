from __future__ import annotations

from dataclasses import dataclass

from .common import (
    ContractError, Entity, Geometry3D, Point3, Polygon3D, Polyline3D, Pose, Provenance,
    Size3, Vector3, _EPS, _finite, _nonnegative, _positive, _validate_id,
)

@dataclass(frozen=True, slots=True, kw_only=True)
class Level(Entity):
    elevation_m: float
    height_m: float | None = None

    def __post_init__(self) -> None:
        super(Level, self).__post_init__()
        _finite(self.elevation_m, f"{self.id}.elevation_m")
        if self.height_m is not None:
            _positive(self.height_m, f"{self.id}.height_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class Space(Entity):
    level_id: str
    footprint: Polygon3D
    height_m: float | None = None
    usage: str | None = None

    def __post_init__(self) -> None:
        super(Space, self).__post_init__()
        _validate_id(self.level_id, f"{self.id}.level_id")
        if self.height_m is not None:
            _positive(self.height_m, f"{self.id}.height_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class Wall(Entity):
    level_id: str
    centerline: Polyline3D
    thickness_m: float
    height_m: float

    def __post_init__(self) -> None:
        super(Wall, self).__post_init__()
        _validate_id(self.level_id, f"{self.id}.level_id")
        _positive(self.thickness_m, f"{self.id}.thickness_m")
        _positive(self.height_m, f"{self.id}.height_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class Slab(Entity):
    level_id: str
    footprint: Polygon3D
    thickness_m: float

    def __post_init__(self) -> None:
        super(Slab, self).__post_init__()
        _validate_id(self.level_id, f"{self.id}.level_id")
        _positive(self.thickness_m, f"{self.id}.thickness_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class Ceiling(Entity):
    level_id: str
    footprint: Polygon3D
    thickness_m: float | None = None

    def __post_init__(self) -> None:
        super(Ceiling, self).__post_init__()
        _validate_id(self.level_id, f"{self.id}.level_id")
        if self.thickness_m is not None:
            _positive(self.thickness_m, f"{self.id}.thickness_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class Opening(Entity):
    host_id: str
    opening_type: str
    pose: Pose
    size: Size3

    def __post_init__(self) -> None:
        super(Opening, self).__post_init__()
        _validate_id(self.host_id, f"{self.id}.host_id")
        if not self.opening_type:
            raise ContractError(f"{self.id}.opening_type is required")


@dataclass(frozen=True, slots=True, kw_only=True)
class ElectricalEquipment(Entity):
    equipment_type: str
    pose: Pose
    level_id: str | None = None
    space_id: str | None = None
    host_id: str | None = None
    size: Size3 | None = None
    system: str | None = None
    rated_voltage_v: float | None = None

    def __post_init__(self) -> None:
        super(ElectricalEquipment, self).__post_init__()
        if not self.equipment_type:
            raise ContractError(f"{self.id}.equipment_type is required")
        for label, value in (("level_id", self.level_id), ("space_id", self.space_id), ("host_id", self.host_id)):
            if value is not None:
                _validate_id(value, f"{self.id}.{label}")
        if self.rated_voltage_v is not None:
            _positive(self.rated_voltage_v, f"{self.id}.rated_voltage_v")


@dataclass(frozen=True, slots=True, kw_only=True)
class ElectricalDevice(Entity):
    device_type: str
    pose: Pose
    level_id: str | None = None
    space_id: str | None = None
    host_id: str | None = None
    size: Size3 | None = None
    system: str | None = None
    rated_voltage_v: float | None = None

    def __post_init__(self) -> None:
        super(ElectricalDevice, self).__post_init__()
        if not self.device_type:
            raise ContractError(f"{self.id}.device_type is required")
        for label, value in (("level_id", self.level_id), ("space_id", self.space_id), ("host_id", self.host_id)):
            if value is not None:
                _validate_id(value, f"{self.id}.{label}")
        if self.rated_voltage_v is not None:
            _positive(self.rated_voltage_v, f"{self.id}.rated_voltage_v")


@dataclass(frozen=True, slots=True, kw_only=True)
class Port(Entity):
    owner_id: str
    domain: str
    role: str
    pose: Pose
    direction: Vector3
    nominal_diameter_m: float | None = None
    connected_port_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(Port, self).__post_init__()
        _validate_id(self.owner_id, f"{self.id}.owner_id")
        if not self.domain or not self.role:
            raise ContractError(f"{self.id}.domain and role are required")
        if self.direction.magnitude <= _EPS:
            raise ContractError(f"{self.id}.direction must be non-zero")
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m, f"{self.id}.nominal_diameter_m")
        for target in self.connected_port_ids:
            _validate_id(target, f"{self.id}.connected_port_ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class Obstacle(Entity):
    geometry: Geometry3D
    obstacle_type: str = "hard"
    level_id: str | None = None
    clearance_m: float = 0.0

    def __post_init__(self) -> None:
        super(Obstacle, self).__post_init__()
        if self.level_id is not None:
            _validate_id(self.level_id, f"{self.id}.level_id")
        _nonnegative(self.clearance_m, f"{self.id}.clearance_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteConstraint(Entity):
    constraint_type: str
    geometry: Geometry3D
    hard: bool = True
    applies_to: tuple[str, ...] = ()
    level_id: str | None = None
    clearance_m: float = 0.0

    def __post_init__(self) -> None:
        super(RouteConstraint, self).__post_init__()
        if not self.constraint_type:
            raise ContractError(f"{self.id}.constraint_type is required")
        if self.level_id is not None:
            _validate_id(self.level_id, f"{self.id}.level_id")
        _nonnegative(self.clearance_m, f"{self.id}.clearance_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteFitting(Entity):
    route_id: str
    fitting_type: str
    pose: Pose
    nominal_diameter_m: float | None = None
    angle_radians: float | None = None

    def __post_init__(self) -> None:
        super(RouteFitting, self).__post_init__()
        _validate_id(self.route_id, f"{self.id}.route_id")
        if not self.fitting_type:
            raise ContractError(f"{self.id}.fitting_type is required")
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m, f"{self.id}.nominal_diameter_m")
        if self.angle_radians is not None:
            _finite(self.angle_radians, f"{self.id}.angle_radians")


@dataclass(frozen=True, slots=True, kw_only=True)
class Route(Entity):
    route_type: str
    start_port_id: str
    end_port_id: str
    centerline: Polyline3D
    nominal_diameter_m: float | None = None
    fitting_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(Route, self).__post_init__()
        if not self.route_type:
            raise ContractError(f"{self.id}.route_type is required")
        _validate_id(self.start_port_id, f"{self.id}.start_port_id")
        _validate_id(self.end_port_id, f"{self.id}.end_port_id")
        if self.start_port_id == self.end_port_id:
            raise ContractError(f"{self.id} must connect two different ports")
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m, f"{self.id}.nominal_diameter_m")
        for fitting_id in self.fitting_ids:
            _validate_id(fitting_id, f"{self.id}.fitting_ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class Circuit(Entity):
    source_port_id: str
    load_port_ids: tuple[str, ...]
    circuit_number: str | None = None
    voltage_v: float | None = None
    poles: int | None = None
    phase: str | None = None
    load_va: float | None = None
    route_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(Circuit, self).__post_init__()
        _validate_id(self.source_port_id, f"{self.id}.source_port_id")
        if not self.load_port_ids:
            raise ContractError(f"{self.id}.load_port_ids cannot be empty")
        for port_id in self.load_port_ids:
            _validate_id(port_id, f"{self.id}.load_port_ids")
        for route_id in self.route_ids:
            _validate_id(route_id, f"{self.id}.route_ids")
        if self.voltage_v is not None:
            _positive(self.voltage_v, f"{self.id}.voltage_v")
        if self.poles is not None:
            if isinstance(self.poles, bool) or not isinstance(self.poles, int) or self.poles < 1:
                raise ContractError(f"{self.id}.poles must be an integer >= 1")
        if self.load_va is not None:
            _nonnegative(self.load_va, f"{self.id}.load_va")


@dataclass(frozen=True, slots=True, kw_only=True)
class Conductor(Entity):
    circuit_id: str
    role: str
    material: str | None = None
    size: str | None = None
    count: int = 1
    insulation: str | None = None
    route_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(Conductor, self).__post_init__()
        _validate_id(self.circuit_id, f"{self.id}.circuit_id")
        if not self.role:
            raise ContractError(f"{self.id}.role is required")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ContractError(f"{self.id}.count must be an integer >= 1")
        for route_id in self.route_ids:
            _validate_id(route_id, f"{self.id}.route_ids")
