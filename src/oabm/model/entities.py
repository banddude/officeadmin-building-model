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
class ElectricalBox(Entity):
    box_type: str
    resolution_status: str
    occupant_ids: tuple[str, ...] = ()
    gang_count: int | None = None
    level_id: str | None = None
    space_id: str | None = None
    host_id: str | None = None
    pose: Pose | None = None
    size: Size3 | None = None
    listed_volume_m3: float | None = None
    mounting: str | None = None
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        super(ElectricalBox, self).__post_init__()
        if not self.box_type:
            raise ContractError(f"{self.id}.box_type is required")
        if self.resolution_status not in {"resolved", "unresolved"}:
            raise ContractError(
                f"{self.id}.resolution_status must be 'resolved' or 'unresolved'"
            )
        if len(self.occupant_ids) != len(set(self.occupant_ids)):
            raise ContractError(f"{self.id}.occupant_ids cannot contain duplicates")
        for occupant_id in self.occupant_ids:
            _validate_id(occupant_id, f"{self.id}.occupant_ids")
        if self.gang_count is not None:
            if isinstance(self.gang_count, bool) or not isinstance(self.gang_count, int) or self.gang_count < 1:
                raise ContractError(f"{self.id}.gang_count must be an integer >= 1")
            if len(self.occupant_ids) > self.gang_count:
                raise ContractError(
                    f"{self.id}.gang_count cannot be smaller than occupant count"
                )
        for label, value in (
            ("level_id", self.level_id),
            ("space_id", self.space_id),
            ("host_id", self.host_id),
        ):
            if value is not None:
                _validate_id(value, f"{self.id}.{label}")
        if self.listed_volume_m3 is not None:
            _positive(self.listed_volume_m3, f"{self.id}.listed_volume_m3")
        if self.mounting is not None and not self.mounting.strip():
            raise ContractError(f"{self.id}.mounting cannot be empty")
        if self.resolution_status == "resolved":
            if self.pose is None or self.size is None:
                raise ContractError(f"{self.id} resolved box requires pose and size")
            if self.unresolved_reason is not None:
                raise ContractError(
                    f"{self.id}.unresolved_reason must be null when resolution_status is resolved"
                )
        elif self.unresolved_reason is None or not self.unresolved_reason.strip():
            raise ContractError(
                f"{self.id}.unresolved_reason is required when resolution_status is unresolved"
            )


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
class RacewaySelection(Entity):
    route_id: str
    start_segment_index: int
    end_segment_index_exclusive: int
    basis: str
    resolution_status: str
    product_kind: str | None = None
    product_type: str | None = None
    catalog_id: str | None = None
    catalog_item_id: str | None = None
    trade_size: str | None = None
    nominal_diameter_m: float | None = None
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        super(RacewaySelection, self).__post_init__()
        _validate_id(self.route_id, f"{self.id}.route_id")
        for label, value in (
            ("start_segment_index", self.start_segment_index),
            ("end_segment_index_exclusive", self.end_segment_index_exclusive),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"{self.id}.{label} must be an integer")
        if self.start_segment_index < 0:
            raise ContractError(f"{self.id}.start_segment_index must be >= 0")
        if self.end_segment_index_exclusive <= self.start_segment_index:
            raise ContractError(
                f"{self.id}.end_segment_index_exclusive must be greater than start_segment_index"
            )
        if not self.basis.strip():
            raise ContractError(f"{self.id}.basis is required")
        if self.resolution_status not in {"resolved", "unresolved"}:
            raise ContractError(
                f"{self.id}.resolution_status must be 'resolved' or 'unresolved'"
            )
        if self.product_kind is not None and self.product_kind not in {"raceway", "cable_assembly"}:
            raise ContractError(
                f"{self.id}.product_kind must be 'raceway' or 'cable_assembly'"
            )
        for label, value in (
            ("product_type", self.product_type),
            ("catalog_id", self.catalog_id),
            ("catalog_item_id", self.catalog_item_id),
            ("trade_size", self.trade_size),
        ):
            if value is not None and not value.strip():
                raise ContractError(f"{self.id}.{label} cannot be empty")
        if (self.catalog_id is None) != (self.catalog_item_id is None):
            raise ContractError(
                f"{self.id}.catalog_id and catalog_item_id must be provided together"
            )
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m, f"{self.id}.nominal_diameter_m")
        if self.resolution_status == "resolved":
            if self.unresolved_reason is not None:
                raise ContractError(
                    f"{self.id}.unresolved_reason must be null when resolution_status is resolved"
                )
            if self.product_kind is None or self.product_type is None:
                raise ContractError(
                    f"{self.id} resolved selection requires product_kind and product_type"
                )
            if self.catalog_id is None or self.catalog_item_id is None:
                raise ContractError(
                    f"{self.id} resolved selection requires catalog_id and catalog_item_id"
                )
            if self.product_kind == "raceway":
                if self.trade_size is None or self.nominal_diameter_m is None:
                    raise ContractError(
                        f"{self.id} resolved raceway requires trade_size and nominal_diameter_m"
                    )
        elif self.unresolved_reason is None or not self.unresolved_reason.strip():
            raise ContractError(
                f"{self.id}.unresolved_reason is required when resolution_status is unresolved"
            )


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
