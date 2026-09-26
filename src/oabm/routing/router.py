from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass
from typing import Iterable

from oabm.model import (
    DERIVATION_INFERRED,
    Box3D,
    BuildingModel,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    RouteConstraint,
    RouteFitting,
    Vector3,
    stable_id,
)

_EPS = 1e-9
_ALGORITHM = "deterministic-rectilinear-v1"
_KEEP_OUT_TYPES = {"keep-out", "keepout", "no-go", "nogo", "forbidden"}
_REQUIRED_TYPES = {"required-corridor", "required", "must-pass", "must-use"}
_PREFERRED_TYPES = {"preferred-corridor", "preferred", "corridor"}
_AVOID_TYPES = {"avoid", "soft-avoid", "discouraged"}


class RoutingError(ValueError):
    """Base error for deterministic routing failures."""


class NoRouteError(RoutingError):
    """Raised when the canonical geometry admits no route under the limits."""


@dataclass(frozen=True, slots=True)
class RoutingOptions:
    """Algorithm controls that do not alter the canonical model contract."""

    bend_penalty_m: float = 0.30
    max_bends: int | None = 12
    port_stub_m: float = 0.15
    clearance_m: float = 0.0
    search_margin_m: float = 0.25
    corridor_tolerance_m: float = 0.05
    preferred_corridor_discount: float = 0.20
    surface_path_discount: float = 0.05
    soft_obstacle_penalty_factor: float = 3.0
    wall_penetration_cost_m: float = 3.0
    vertical_cost_factor: float = 1.0
    coordinate_precision: int = 9

    def __post_init__(self) -> None:
        for name in (
            "bend_penalty_m",
            "port_stub_m",
            "clearance_m",
            "search_margin_m",
            "corridor_tolerance_m",
            "soft_obstacle_penalty_factor",
            "wall_penetration_cost_m",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise RoutingError(f"{name} must be finite and >= 0")
        if self.max_bends is not None:
            if isinstance(self.max_bends, bool) or not isinstance(self.max_bends, int) or self.max_bends < 0:
                raise RoutingError("max_bends must be an integer >= 0 or None")
        for name in ("preferred_corridor_discount", "surface_path_discount"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value < 1:
                raise RoutingError(f"{name} must be in [0, 1)")
        if not math.isfinite(self.vertical_cost_factor) or self.vertical_cost_factor <= 0:
            raise RoutingError("vertical_cost_factor must be finite and > 0")
        if not 3 <= self.coordinate_precision <= 12:
            raise RoutingError("coordinate_precision must be between 3 and 12")


@dataclass(frozen=True, slots=True)
class _Bounds:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def expanded(self, value: float) -> "_Bounds":
        return _Bounds(
            self.min_x - value,
            self.min_y - value,
            self.min_z - value,
            self.max_x + value,
            self.max_y + value,
            self.max_z + value,
        )

    @property
    def center(self) -> Point3:
        return Point3(
            x=(self.min_x + self.max_x) / 2,
            y=(self.min_y + self.max_y) / 2,
            z=(self.min_z + self.max_z) / 2,
        )


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    bounds: _Bounds
    geometry: Box3D | Polyline3D | Polygon3D | None = None
    tolerance_m: float = 0.0
    include_center_in_grid: bool = True


@dataclass(frozen=True, slots=True)
class _WallSegment:
    """One centerline span of a wall in its horizontal local frame."""

    origin_x: float
    origin_y: float
    unit_x: float
    unit_y: float
    normal_x: float
    normal_y: float
    length: float


@dataclass(frozen=True, slots=True)
class _OpeningRule:
    id: str
    box: Box3D
    bounds: _Bounds


@dataclass(frozen=True, slots=True)
class _WallRule:
    """Canonical wall solid: centerline swept by thickness over its height."""

    id: str
    bounds: _Bounds
    segments: tuple[_WallSegment, ...]
    half_thickness: float
    thickness_m: float
    base_z: float
    top_z: float
    openings: tuple[_OpeningRule, ...]


@dataclass(frozen=True, slots=True)
class _WallIndex:
    """Bisect broad phase over wall footprint x-intervals.

    Walls are ordered by their footprint minimum x with a running maximum of
    the maximum x, so a segment range query scans only walls that can overlap
    it and always yields them in the same deterministic order.
    """

    min_x: tuple[float, ...]
    prefix_max_x: tuple[float, ...]
    walls: tuple[_WallRule, ...]

    @classmethod
    def build(cls, walls: tuple[_WallRule, ...]) -> "_WallIndex":
        ordered = tuple(sorted(walls, key=lambda item: (item.bounds.min_x, item.id)))
        running = -math.inf
        prefix: list[float] = []
        for item in ordered:
            running = max(running, item.bounds.max_x)
            prefix.append(running)
        return cls(
            tuple(item.bounds.min_x for item in ordered),
            tuple(prefix),
            ordered,
        )

    def candidates(self, low_x: float, high_x: float) -> tuple[_WallRule, ...]:
        boundary = bisect.bisect_right(self.min_x, high_x + _EPS)
        found: list[_WallRule] = []
        index = boundary - 1
        while index >= 0:
            if self.prefix_max_x[index] < low_x - _EPS:
                break
            found.append(self.walls[index])
            index -= 1
        found.reverse()
        return tuple(found)


@dataclass(frozen=True, slots=True)
class _RoutingGeometry:
    hard_blockers: tuple[_Rule, ...]
    soft_blockers: tuple[_Rule, ...]
    required: tuple[_Rule, ...]
    preferred: tuple[_Rule, ...]
    surfaces: tuple[_Rule, ...]
    walls: tuple[_WallRule, ...]
    wall_index: _WallIndex


def route_between_ports(
    model: BuildingModel,
    start_port_id: str,
    end_port_id: str,
    route_type: str,
    *,
    nominal_diameter_m: float | None = None,
    options: RoutingOptions | None = None,
) -> tuple[Route, tuple[RouteFitting, ...]]:
    """Route one canonical port-to-port connection deterministically.

    The input is the canonical ``BuildingModel``. The outputs are canonical
    ``Route`` and ``RouteFitting`` objects only; callers decide whether and how
    to attach them to a model document.

    Canonical walls are traversable obstacles: the route may cross a wall
    through one of its openings without penalty, or anywhere else as a
    recorded penetration priced by ``RoutingOptions.wall_penetration_cost_m``.
    Every crossed wall id is listed on the route under ``penetrated_wall_ids``.
    """

    if not route_type:
        raise RoutingError("route_type is required")
    options = options or RoutingOptions()
    ports = {port.id: port for port in model.ports}
    try:
        start_port = ports[start_port_id]
        end_port = ports[end_port_id]
    except KeyError as exc:
        raise RoutingError(f"unknown port id {exc.args[0]!r}") from exc
    if start_port_id == end_port_id:
        raise RoutingError("start and end ports must be different")
    if start_port.domain != end_port.domain:
        raise RoutingError(
            f"port domains differ: {start_port.domain!r} != {end_port.domain!r}"
        )

    diameter = _resolve_diameter(start_port, end_port, nominal_diameter_m)
    route_radius = (diameter or 0.0) / 2.0
    geometry = _collect_routing_geometry(model, route_type, route_radius, options)

    start = start_port.pose.position
    end = end_port.pose.position
    start_dir = _unit(start_port.direction)
    end_dir = _unit(end_port.direction)
    start_anchor = _offset(start, start_dir, options.port_stub_m)
    end_anchor = _offset(end, end_dir, options.port_stub_m)

    if _point_blocked(start, geometry.hard_blockers) or _point_blocked(end, geometry.hard_blockers):
        raise NoRouteError("a route endpoint lies inside hard obstacle/no-go geometry")
    if not _segment_clear(start, start_anchor, geometry.hard_blockers):
        raise NoRouteError("start port direction stub intersects hard obstacle/no-go geometry")
    if not _segment_clear(end_anchor, end, geometry.hard_blockers):
        raise NoRouteError("end port direction stub intersects hard obstacle/no-go geometry")

    xs, ys, zs = _candidate_coordinates(
        model,
        start_anchor,
        end_anchor,
        geometry,
        options,
    )
    start_node = _node_for_point(start_anchor, xs, ys, zs, options.coordinate_precision)
    end_node = _node_for_point(end_anchor, xs, ys, zs, options.coordinate_precision)

    points = _search(
        start,
        start_anchor,
        start_dir,
        end,
        end_anchor,
        end_dir,
        start_node,
        end_node,
        xs,
        ys,
        zs,
        geometry,
        options,
    )
    points = _simplify(points)
    bend_count = _count_bends(points)
    if options.max_bends is not None and bend_count > options.max_bends:
        raise NoRouteError(
            f"best route requires {bend_count} bends, exceeding max_bends={options.max_bends}"
        )
    penetrated_wall_ids = _penetrated_wall_ids(points, geometry)

    route_id = stable_id(
        "route",
        f"{model.model_id}:{route_type}:{start_port_id}:{end_port_id}",
    )
    provenance = (
        Provenance(
            source_kind="router",
            source_id=model.model_id,
            source_element_id=f"{start_port_id}->{end_port_id}",
            method=_ALGORITHM,
            confidence=1.0,
            # A routed path is a proposal, never an observation. No source
            # shows this centerline; we computed it. It stays inferred until
            # something in the source actually shows the run.
            derivation=DERIVATION_INFERRED,
            attributes={"route_type": route_type},
        ),
    )
    fittings = _build_fittings(route_id, points, diameter, provenance)
    length_m = sum(_distance(a, b) for a, b in zip(points, points[1:]))
    route = Route(
        id=route_id,
        route_type=route_type,
        start_port_id=start_port_id,
        end_port_id=end_port_id,
        centerline=Polyline3D(points=tuple(points)),
        nominal_diameter_m=diameter,
        fitting_ids=tuple(fitting.id for fitting in fittings),
        provenance=provenance,
        attributes={
            "routing_engine": _ALGORITHM,
            "bend_count": bend_count,
            "length_m": round(length_m, options.coordinate_precision),
            "required_constraint_ids": [item.id for item in geometry.required],
            "penetrated_wall_ids": penetrated_wall_ids,
        },
    )
    return route, fittings


def _resolve_diameter(start: Port, end: Port, explicit: float | None) -> float | None:
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0:
            raise RoutingError("nominal_diameter_m must be finite and > 0")
        return float(explicit)
    values = [value for value in (start.nominal_diameter_m, end.nominal_diameter_m) if value is not None]
    return max(values) if values else None


def _normalize_constraint_type(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _applies(constraint: RouteConstraint, route_type: str) -> bool:
    return not constraint.applies_to or route_type in constraint.applies_to or "*" in constraint.applies_to


def _constraint_rule(constraint: RouteConstraint, tolerance_m: float) -> _Rule:
    if isinstance(constraint.geometry, Polygon3D):
        _polygon_plane(constraint.geometry, constraint.id)
    return _Rule(
        constraint.id,
        _geometry_bounds(constraint.geometry).expanded(tolerance_m),
        constraint.geometry,
        tolerance_m,
    )


def _collect_routing_geometry(
    model: BuildingModel,
    route_type: str,
    route_radius: float,
    options: RoutingOptions,
) -> _RoutingGeometry:
    hard: list[_Rule] = []
    soft: list[_Rule] = []
    required: list[_Rule] = []
    preferred: list[_Rule] = []

    for obstacle in sorted(model.obstacles, key=lambda item: item.id):
        padding = obstacle.clearance_m + options.clearance_m + route_radius
        rule = _Rule(obstacle.id, _geometry_bounds(obstacle.geometry).expanded(padding))
        if obstacle.obstacle_type.strip().lower() in {"soft", "advisory"}:
            soft.append(rule)
        else:
            hard.append(rule)

    for constraint in sorted(model.route_constraints, key=lambda item: item.id):
        if not _applies(constraint, route_type):
            continue
        kind = _normalize_constraint_type(constraint.constraint_type)
        padding = constraint.clearance_m + options.clearance_m + route_radius
        if kind in _KEEP_OUT_TYPES:
            rule = _constraint_rule(constraint, padding)
            if constraint.hard:
                hard.append(rule)
            else:
                soft.append(rule)
        elif kind in _REQUIRED_TYPES:
            rule = _constraint_rule(
                constraint,
                padding + options.corridor_tolerance_m,
            )
            if constraint.hard:
                required.append(rule)
            else:
                preferred.append(rule)
        elif kind in _PREFERRED_TYPES:
            preferred.append(
                _constraint_rule(
                    constraint,
                    padding + options.corridor_tolerance_m,
                )
            )
        elif kind in _AVOID_TYPES:
            rule = _constraint_rule(constraint, padding)
            if constraint.hard:
                hard.append(rule)
            else:
                soft.append(rule)
        else:
            raise RoutingError(
                f"unsupported active route constraint type {constraint.constraint_type!r} "
                f"on {constraint.id}"
            )

    surfaces: list[_Rule] = []
    level_by_id = {level.id: level for level in model.levels}
    surface_pad = options.corridor_tolerance_m + route_radius
    openings_by_host: dict[str, list[_OpeningRule]] = {}
    for opening in sorted(model.openings, key=lambda item: item.id):
        box = Box3D(pose=opening.pose, size=opening.size)
        openings_by_host.setdefault(opening.host_id, []).append(
            _OpeningRule(opening.id, box, _geometry_bounds(box))
        )
    walls: list[_WallRule] = []
    for wall in sorted(model.walls, key=lambda item: item.id):
        base = _geometry_bounds(wall.centerline)
        level = level_by_id.get(wall.level_id)
        base_z = min(point.z for point in wall.centerline.points)
        if level is not None:
            base_z = min(base_z, level.elevation_m)
        top_z = base_z + wall.height_m
        half_thickness = wall.thickness_m / 2
        solid_bounds = _Bounds(
            base.min_x - half_thickness,
            base.min_y - half_thickness,
            base_z,
            base.max_x + half_thickness,
            base.max_y + half_thickness,
            top_z,
        )
        walls.append(_WallRule(
            id=wall.id,
            bounds=solid_bounds,
            segments=tuple(_wall_segments(wall.centerline)),
            half_thickness=half_thickness,
            thickness_m=wall.thickness_m,
            base_z=base_z,
            top_z=top_z,
            openings=tuple(openings_by_host.get(wall.id, ())),
        ))
        wall_bounds = solid_bounds.expanded(half_thickness + surface_pad)
        surfaces.append(_Rule(
            f"wall:{wall.id}", wall_bounds, include_center_in_grid=False,
        ))
    for ceiling in sorted(model.ceilings, key=lambda item: item.id):
        bounds = _geometry_bounds(ceiling.footprint)
        z = sum(point.z for point in ceiling.footprint.points) / len(ceiling.footprint.points)
        ceiling_bounds = _Bounds(
            bounds.min_x,
            bounds.min_y,
            z - surface_pad,
            bounds.max_x,
            bounds.max_y,
            z + surface_pad,
        )
        surfaces.append(_Rule(f"ceiling:{ceiling.id}", ceiling_bounds))

    return _RoutingGeometry(
        hard_blockers=tuple(hard),
        soft_blockers=tuple(soft),
        required=tuple(required),
        preferred=tuple(preferred),
        surfaces=tuple(surfaces),
        walls=tuple(walls),
        wall_index=_WallIndex.build(tuple(walls)),
    )


def _geometry_bounds(geometry: Box3D | Polyline3D | Polygon3D) -> _Bounds:
    if isinstance(geometry, Box3D):
        hx, hy, hz = geometry.size.x / 2, geometry.size.y / 2, geometry.size.z / 2
        corners = []
        for x in (-hx, hx):
            for y in (-hy, hy):
                for z in (-hz, hz):
                    rx, ry, rz = _rotate_vector((x, y, z), geometry.pose.rotation)
                    center = geometry.pose.position
                    corners.append(Point3(x=center.x + rx, y=center.y + ry, z=center.z + rz))
        return _bounds_from_points(corners)
    return _bounds_from_points(geometry.points)


def _rotate_vector(vector: tuple[float, float, float], quaternion) -> tuple[float, float, float]:
    x, y, z = vector
    qx, qy, qz, qw = quaternion.x, quaternion.y, quaternion.z, quaternion.w
    # q * v * q^-1, expanded without temporary quaternion allocation.
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def _bounds_from_points(points: Iterable[Point3]) -> _Bounds:
    values = tuple(points)
    if not values:
        raise RoutingError("geometry has no points")
    return _Bounds(
        min(point.x for point in values),
        min(point.y for point in values),
        min(point.z for point in values),
        max(point.x for point in values),
        max(point.y for point in values),
        max(point.z for point in values),
    )


def _candidate_coordinates(
    model: BuildingModel,
    start: Point3,
    end: Point3,
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    p = options.coordinate_precision
    xs = {_canon(start.x, p), _canon(end.x, p)}
    ys = {_canon(start.y, p), _canon(end.y, p)}
    zs = {_canon(start.z, p), _canon(end.z, p)}
    escape = max(10 ** (-p + 2), 1e-6)

    all_rules = (
        *geometry.hard_blockers,
        *geometry.soft_blockers,
        *geometry.required,
        *geometry.preferred,
        *geometry.surfaces,
    )
    for rule in all_rules:
        bounds = rule.bounds
        xs.update({_canon(bounds.min_x - escape, p), _canon(bounds.max_x + escape, p)})
        ys.update({_canon(bounds.min_y - escape, p), _canon(bounds.max_y + escape, p)})
        zs.update({_canon(bounds.min_z - escape, p), _canon(bounds.max_z + escape, p)})
        if rule.include_center_in_grid:
            xs.add(_canon(bounds.center.x, p))
            ys.add(_canon(bounds.center.y, p))
            zs.add(_canon(bounds.center.z, p))
        if isinstance(rule.geometry, (Polyline3D, Polygon3D)):
            for point in rule.geometry.points:
                xs.add(_canon(point.x, p))
                ys.add(_canon(point.y, p))
                zs.add(_canon(point.z, p))

    # Wall faces, end caps, and top plane give crossings one clean grid edge
    # through the solid; the centerline itself is deliberately not a grid
    # plane so a crossing cannot ride it for free.
    for wall in model.walls:
        half_thickness = wall.thickness_m / 2
        base_z = min(point.z for point in wall.centerline.points)
        zs.update({_canon(base_z, p), _canon(base_z + wall.height_m, p)})
        for point in wall.centerline.points:
            xs.update({_canon(point.x - half_thickness, p), _canon(point.x + half_thickness, p)})
            ys.update({_canon(point.y - half_thickness, p), _canon(point.y + half_thickness, p)})
    walls_by_id = {wall.id: wall for wall in model.walls}
    for opening in sorted(model.openings, key=lambda item: item.id):
        if opening.host_id not in walls_by_id:
            continue
        bounds = _geometry_bounds(Box3D(pose=opening.pose, size=opening.size))
        xs.update({_canon(bounds.min_x - escape, p), _canon(bounds.max_x + escape, p)})
        ys.update({_canon(bounds.min_y - escape, p), _canon(bounds.max_y + escape, p)})
        zs.update({
            _canon(bounds.min_z - escape, p),
            _canon(bounds.center.z, p),
            _canon(bounds.max_z + escape, p),
        })
        # One interior plane along the wall gives crossings a free column
        # through the opening. The center across the wall stays out of the
        # grid: it would sit on the centerline and let crossings ride it.
        host = walls_by_id[opening.host_id]
        span_x = math.fsum(
            abs(second.x - first.x)
            for first, second in zip(host.centerline.points, host.centerline.points[1:])
        )
        span_y = math.fsum(
            abs(second.y - first.y)
            for first, second in zip(host.centerline.points, host.centerline.points[1:])
        )
        if span_y <= _EPS:
            xs.add(_canon(bounds.center.x, p))
        elif span_x <= _EPS:
            ys.add(_canon(bounds.center.y, p))
        else:
            xs.add(_canon(bounds.center.x, p))
            ys.add(_canon(bounds.center.y, p))
    for ceiling in model.ceilings:
        for point in ceiling.footprint.points:
            xs.add(_canon(point.x, p))
            ys.add(_canon(point.y, p))
            zs.add(_canon(point.z, p))
    for level in model.levels:
        zs.add(_canon(level.elevation_m, p))
        if level.height_m is not None:
            zs.add(_canon(level.elevation_m + level.height_m, p))

    xs.update({_canon(min(xs) - options.search_margin_m, p), _canon(max(xs) + options.search_margin_m, p)})
    ys.update({_canon(min(ys) - options.search_margin_m, p), _canon(max(ys) + options.search_margin_m, p)})
    zs.update({_canon(min(zs) - options.search_margin_m, p), _canon(max(zs) + options.search_margin_m, p)})
    return tuple(sorted(xs)), tuple(sorted(ys)), tuple(sorted(zs))


def _node_for_point(
    point: Point3,
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
    precision: int,
) -> tuple[int, int, int]:
    maps = ({value: index for index, value in enumerate(xs)}, {value: index for index, value in enumerate(ys)}, {value: index for index, value in enumerate(zs)})
    try:
        return (
            maps[0][_canon(point.x, precision)],
            maps[1][_canon(point.y, precision)],
            maps[2][_canon(point.z, precision)],
        )
    except KeyError as exc:
        raise RoutingError("endpoint anchor was not represented in routing grid") from exc


def _search(
    start: Point3,
    start_anchor: Point3,
    start_direction: tuple[float, float, float],
    end: Point3,
    end_anchor: Point3,
    end_direction: tuple[float, float, float],
    start_node: tuple[int, int, int],
    end_node: tuple[int, int, int],
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> list[Point3]:
    start_stub_exists = _distance(start, start_anchor) > _EPS
    end_stub_exists = _distance(end_anchor, end) > _EPS
    start_vector = _vector(start, start_anchor) if start_stub_exists else None
    terminal_vector = _vector(end_anchor, end) if end_stub_exists else None

    initial_mask = _required_mask(0, start, start_anchor, geometry.required)
    if start_stub_exists:
        initial_cost, start_memory = _edge_cost_with_memory(
            start, start_anchor, geometry, options, 0,
        )
    else:
        initial_cost, start_memory = 0.0, 0
    start_prev = -2 if start_stub_exists else -1
    start_state = (start_node, start_prev, 0, initial_mask, start_memory)
    best: dict[tuple[tuple[int, int, int], int, int, int, int], float] = {start_state: initial_cost}
    predecessor: dict[
        tuple[tuple[int, int, int], int, int, int, int],
        tuple[tuple[int, int, int], int, int, int, int],
    ] = {}
    heap: list[tuple[float, int, int, int, int, int, int, int, tuple[tuple[int, int, int], int, int, int, int]]] = []
    _push(heap, initial_cost, start_state)

    full_required_mask = (1 << len(geometry.required)) - 1
    best_goal_cost = math.inf
    best_goal_state = None

    while heap:
        cost, _, _, _, _, _, _, _, state = heapq.heappop(heap)
        if cost > best.get(state, math.inf) + 1e-12:
            continue
        if cost >= best_goal_cost - 1e-12:
            break
        node, prev_code, bends, mask, memory = state
        point = _point_for(node, xs, ys, zs)

        if node == end_node:
            final_mask = _required_mask(mask, end_anchor, end, geometry.required)
            terminal_bends = bends
            terminal_cost = cost
            if end_stub_exists:
                if prev_code == -2:
                    prior_vector = start_vector
                elif prev_code == -1:
                    prior_vector = None
                else:
                    prior_vector = _direction_vector(prev_code)
                turn = _is_turn(prior_vector, terminal_vector)
                terminal_bends += 1 if turn else 0
                if options.max_bends is not None and terminal_bends > options.max_bends:
                    pass
                else:
                    stub_cost, _ = _edge_cost_with_memory(
                        end_anchor, end, geometry, options, memory,
                    )
                    terminal_cost += stub_cost
                    if turn:
                        terminal_cost += options.bend_penalty_m
            if final_mask == full_required_mask and (
                options.max_bends is None or terminal_bends <= options.max_bends
            ):
                if terminal_cost < best_goal_cost - 1e-12:
                    best_goal_cost = terminal_cost
                    best_goal_state = state

        for next_node, direction_code in _neighbors(node, xs, ys, zs):
            next_point = _point_for(next_node, xs, ys, zs)
            if _point_blocked(next_point, geometry.hard_blockers):
                continue
            if not _segment_clear(point, next_point, geometry.hard_blockers):
                continue
            if prev_code == -2:
                prior_vector = start_vector
            elif prev_code == -1:
                prior_vector = None
            else:
                prior_vector = _direction_vector(prev_code)
            next_vector = _direction_vector(direction_code)
            turn = _is_turn(prior_vector, next_vector)
            next_bends = bends + (1 if turn else 0)
            if options.max_bends is not None and next_bends > options.max_bends:
                continue
            next_mask = _required_mask(mask, point, next_point, geometry.required)
            edge_cost, next_memory = _edge_cost_with_memory(
                point, next_point, geometry, options, memory,
            )
            next_cost = cost + edge_cost
            if turn:
                next_cost += options.bend_penalty_m
            next_state = (next_node, direction_code, next_bends, next_mask, next_memory)
            old_cost = best.get(next_state)
            if old_cost is None or next_cost < old_cost - 1e-12:
                best[next_state] = next_cost
                predecessor[next_state] = state
                _push(heap, next_cost, next_state)

    if best_goal_state is None:
        required_text = ""
        if geometry.required:
            required_text = "; required corridors=" + ",".join(item.id for item in geometry.required)
        bend_text = "unlimited" if options.max_bends is None else str(options.max_bends)
        raise NoRouteError(
            f"no route from {start_node} to {end_node} under hard geometry and max_bends={bend_text}{required_text}"
        )

    states = [best_goal_state]
    while states[-1] != start_state:
        states.append(predecessor[states[-1]])
    states.reverse()
    path = [_point_for(state[0], xs, ys, zs) for state in states]
    points = [start]
    points.extend(path)
    points.append(end)
    return _dedupe(points)


def _push(heap, cost: float, state) -> None:
    node, direction, bends, mask, memory = state
    heapq.heappush(
        heap,
        (cost, bends, node[0], node[1], node[2], direction, mask, memory, state),
    )


def _neighbors(
    node: tuple[int, int, int],
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
):
    ix, iy, iz = node
    candidates = []
    if ix > 0:
        candidates.append(((ix - 1, iy, iz), 1))  # -X
    if ix + 1 < len(xs):
        candidates.append(((ix + 1, iy, iz), 0))  # +X
    if iy > 0:
        candidates.append(((ix, iy - 1, iz), 3))  # -Y
    if iy + 1 < len(ys):
        candidates.append(((ix, iy + 1, iz), 2))  # +Y
    if iz > 0:
        candidates.append(((ix, iy, iz - 1), 5))  # -Z
    if iz + 1 < len(zs):
        candidates.append(((ix, iy, iz + 1), 4))  # +Z
    # Coordinate-first ordering makes equal-cost ties independent of input list order.
    yield from sorted(candidates, key=lambda item: item[0])


def _point_for(node, xs, ys, zs) -> Point3:
    return Point3(x=xs[node[0]], y=ys[node[1]], z=zs[node[2]])


def _edge_base_cost(
    a: Point3,
    b: Point3,
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> float:
    length = _distance(a, b)
    if length <= _EPS:
        return 0.0
    vertical = abs(a.z - b.z) > _EPS and abs(a.x - b.x) <= _EPS and abs(a.y - b.y) <= _EPS
    cost = length * (options.vertical_cost_factor if vertical else 1.0)
    if geometry.soft_blockers:
        hits = sum(_rule_intersects_segment(a, b, item) for item in geometry.soft_blockers)
        cost += length * options.soft_obstacle_penalty_factor * hits
    if geometry.preferred and any(_rule_intersects_segment(a, b, item) for item in geometry.preferred):
        cost *= 1.0 - options.preferred_corridor_discount
    if geometry.surfaces and any(_segment_midpoint_in_bounds(a, b, item.bounds) for item in geometry.surfaces):
        cost *= 1.0 - options.surface_path_discount
    return cost


def _wall_segments(centerline: Polyline3D) -> Iterable[_WallSegment]:
    """Local horizontal frames for the wall baseline spans, in polyline order."""
    for start, end in zip(centerline.points, centerline.points[1:]):
        delta_x, delta_y = end.x - start.x, end.y - start.y
        length = math.hypot(delta_x, delta_y)
        if length <= _EPS:
            continue
        unit_x, unit_y = delta_x / length, delta_y / length
        yield _WallSegment(
            origin_x=start.x,
            origin_y=start.y,
            unit_x=unit_x,
            unit_y=unit_y,
            normal_x=-unit_y,
            normal_y=unit_x,
            length=length,
        )


def _strict_side(value: float) -> int:
    if value > _EPS:
        return 1
    if value < -_EPS:
        return -1
    return 0


def _wall_edge_event(
    a: Point3,
    b: Point3,
    wall: _WallRule,
    memory: int,
) -> tuple[float, int | None]:
    """Penetration chord of a-b against one wall plus the next side memory.

    chord > 0 means the edge crosses the wall solid outside every opening and
    owes the penetration cost. The memory carries the last strict side of the
    centerline seen inside the solid, so a crossing split across grid nodes
    that sit exactly on the centerline still charges on the way out. None
    means the edge never entered this wall solid.
    """
    if not _segment_intersects_bounds(a, b, wall.bounds):
        return 0.0, None
    span = _distance(a, b)
    if span <= _EPS:
        return 0.0, memory
    chord_total = 0.0
    current = memory
    touched = False
    for segment in wall.segments:
        inside = _wall_segment_solid_interval(a, b, wall, segment)
        if inside is None:
            continue
        touched = True
        from_start_x, from_start_y = a.x - segment.origin_x, a.y - segment.origin_y
        delta_x, delta_y = b.x - a.x, b.y - a.y
        side_a = from_start_x * segment.normal_x + from_start_y * segment.normal_y
        side_b = side_a + delta_x * segment.normal_x + delta_y * segment.normal_y
        strict_a = _strict_side(side_a)
        strict_b = _strict_side(side_b)
        if (
            (strict_a and strict_b and strict_a == -strict_b)
            or (strict_a and not strict_b)
            or (not strict_a and strict_b and current == -strict_b)
        ):
            covered = tuple(_opening_intervals(a, b, wall.openings, inside))
            chord_total += _uncovered_length(inside, covered) * span
        if strict_b:
            current = strict_b
        elif strict_a:
            current = strict_a
    if not touched:
        return 0.0, None
    return chord_total, current


def _wall_edge_charges(
    a: Point3,
    b: Point3,
    geometry: _RoutingGeometry,
    options: "RoutingOptions",
    memory: int,
) -> tuple[float, int]:
    if not geometry.walls:
        return 0.0, 0
    total = 0.0
    current = memory
    touched = False
    for wall in geometry.wall_index.candidates(min(a.x, b.x), max(a.x, b.x)):
        chord, nxt = _wall_edge_event(a, b, wall, current)
        if chord > _EPS:
            total += options.wall_penetration_cost_m * chord / wall.thickness_m
        if nxt is not None:
            current = nxt
            touched = True
    return total, (current if touched else 0)


def _edge_cost_with_memory(
    a: Point3,
    b: Point3,
    geometry: _RoutingGeometry,
    options: "RoutingOptions",
    memory: int,
) -> tuple[float, int]:
    charge, next_memory = _wall_edge_charges(a, b, geometry, options, memory)
    return _edge_base_cost(a, b, geometry, options) + charge, next_memory


def _wall_segment_solid_interval(
    a: Point3,
    b: Point3,
    wall: _WallRule,
    segment: _WallSegment,
) -> tuple[float, float] | None:
    from_start_x, from_start_y = a.x - segment.origin_x, a.y - segment.origin_y
    delta_x, delta_y = b.x - a.x, b.y - a.y
    along_a = from_start_x * segment.unit_x + from_start_y * segment.unit_y
    along_b = along_a + delta_x * segment.unit_x + delta_y * segment.unit_y
    side_a = from_start_x * segment.normal_x + from_start_y * segment.normal_y
    side_b = side_a + delta_x * segment.normal_x + delta_y * segment.normal_y
    height_a, height_b = a.z - wall.base_z, b.z - wall.base_z
    intervals = (
        _affine_interval(
            along_a, along_b,
            -wall.half_thickness, segment.length + wall.half_thickness,
        ),
        _affine_interval(side_a, side_b, -wall.half_thickness, wall.half_thickness),
        _affine_interval(height_a, height_b, 0.0, wall.top_z - wall.base_z),
    )
    if any(item is None for item in intervals):
        return None
    low = max(item[0] for item in intervals)
    high = min(item[1] for item in intervals)
    if high <= low:
        return None
    return low, high


def _affine_interval(
    start_value: float,
    end_value: float,
    low: float,
    high: float,
) -> tuple[float, float] | None:
    """Parameter range in [0, 1] where an affine value stays inside [low, high]."""
    delta = end_value - start_value
    if abs(delta) <= _EPS:
        if start_value < low - _EPS or start_value > high + _EPS:
            return None
        return (0.0, 1.0)
    first = (low - start_value) / delta
    second = (high - start_value) / delta
    if first > second:
        first, second = second, first
    return (max(0.0, first), min(1.0, second))


def _opening_intervals(
    a: Point3,
    b: Point3,
    openings: tuple[_OpeningRule, ...],
    inside: tuple[float, float],
) -> Iterable[tuple[float, float]]:
    for opening in openings:
        local_a = _box_local_coordinates(a, opening.box)
        local_b = _box_local_coordinates(b, opening.box)
        clips = []
        for start_value, end_value, half_size in (
            (local_a[0], local_b[0], opening.box.size.x / 2),
            (local_a[1], local_b[1], opening.box.size.y / 2),
            (local_a[2], local_b[2], opening.box.size.z / 2),
        ):
            clipped = _affine_interval(start_value, end_value, -half_size, half_size)
            if clipped is None:
                break
            clips.append(clipped)
        else:
            low = max(inside[0], *(item[0] for item in clips))
            high = min(inside[1], *(item[1] for item in clips))
            if high > low:
                yield low, high


def _uncovered_length(
    inside: tuple[float, float],
    covered: tuple[tuple[float, float], ...],
) -> float:
    """Fraction of the in-solid span not punched through by any opening."""
    low, high = inside
    cursor = low
    uncovered = 0.0
    for start, end in sorted(covered):
        if end <= cursor:
            continue
        if start > cursor:
            uncovered += min(start, high) - cursor
        cursor = max(cursor, end)
        if cursor >= high:
            break
    if cursor < high:
        uncovered += high - cursor
    return uncovered


def _penetrated_wall_ids(points: list[Point3], geometry: _RoutingGeometry) -> list[str]:
    found: set[str] = set()
    memory = 0
    for a, b in zip(points, points[1:]):
        touched = False
        for wall in geometry.wall_index.candidates(min(a.x, b.x), max(a.x, b.x)):
            chord, nxt = _wall_edge_event(a, b, wall, memory)
            if chord > _EPS:
                found.add(wall.id)
            if nxt is not None:
                memory = nxt
                touched = True
        if not touched:
            memory = 0
    return sorted(found)


def _required_mask(mask: int, a: Point3, b: Point3, required: tuple[_Rule, ...]) -> int:
    if _distance(a, b) <= _EPS:
        for index, item in enumerate(required):
            if _rule_contains_point(a, item):
                mask |= 1 << index
        return mask
    for index, item in enumerate(required):
        if _rule_intersects_segment(a, b, item):
            mask |= 1 << index
    return mask


def _point_blocked(point: Point3, blockers: tuple[_Rule, ...]) -> bool:
    return any(_rule_contains_point(point, item) for item in blockers)


def _segment_clear(a: Point3, b: Point3, blockers: tuple[_Rule, ...]) -> bool:
    if _distance(a, b) <= _EPS:
        return not _point_blocked(a, blockers)
    return not any(_rule_intersects_segment(a, b, item) for item in blockers)


def _point_in_bounds(point: Point3, bounds: _Bounds) -> bool:
    return (
        bounds.min_x - _EPS <= point.x <= bounds.max_x + _EPS
        and bounds.min_y - _EPS <= point.y <= bounds.max_y + _EPS
        and bounds.min_z - _EPS <= point.z <= bounds.max_z + _EPS
    )


def _segment_midpoint_in_bounds(a: Point3, b: Point3, bounds: _Bounds) -> bool:
    return _point_in_bounds(
        Point3(x=(a.x + b.x) / 2, y=(a.y + b.y) / 2, z=(a.z + b.z) / 2),
        bounds,
    )


def _segment_intersects_bounds(a: Point3, b: Point3, bounds: _Bounds) -> bool:
    t_min = 0.0
    t_max = 1.0
    for start, end, minimum, maximum in (
        (a.x, b.x, bounds.min_x, bounds.max_x),
        (a.y, b.y, bounds.min_y, bounds.max_y),
        (a.z, b.z, bounds.min_z, bounds.max_z),
    ):
        delta = end - start
        if abs(delta) <= _EPS:
            if start < minimum - _EPS or start > maximum + _EPS:
                return False
            continue
        first = (minimum - start) / delta
        second = (maximum - start) / delta
        if first > second:
            first, second = second, first
        t_min = max(t_min, first)
        t_max = min(t_max, second)
        if t_min > t_max + _EPS:
            return False
    return t_max >= -_EPS and t_min <= 1.0 + _EPS


def _inverse_rotate_vector(vector: tuple[float, float, float], quaternion) -> tuple[float, float, float]:
    x, y, z = vector
    qx, qy, qz, qw = -quaternion.x, -quaternion.y, -quaternion.z, quaternion.w
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def _box_local_coordinates(point: Point3, box: Box3D) -> tuple[float, float, float]:
    center = box.pose.position
    return _inverse_rotate_vector(
        (point.x - center.x, point.y - center.y, point.z - center.z),
        box.pose.rotation,
    )


def _point_box_distance(point: Point3, box: Box3D) -> float:
    local = _box_local_coordinates(point, box)
    half_sizes = (box.size.x / 2, box.size.y / 2, box.size.z / 2)
    outside = tuple(
        max(abs(value) - half_size, 0.0)
        for value, half_size in zip(local, half_sizes)
    )
    return math.sqrt(_dot(outside, outside))


def _segment_aabb_distance(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    half_sizes: tuple[float, float, float],
) -> float:
    delta = tuple(right - left for left, right in zip(start, end))

    t_min = 0.0
    t_max = 1.0
    intersects = True
    for value, change, half_size in zip(start, delta, half_sizes):
        if abs(change) <= _EPS:
            if value < -half_size - _EPS or value > half_size + _EPS:
                intersects = False
                break
            continue
        first = (-half_size - value) / change
        second = (half_size - value) / change
        if first > second:
            first, second = second, first
        t_min = max(t_min, first)
        t_max = min(t_max, second)
        if t_min > t_max + _EPS:
            intersects = False
            break
    if intersects and t_max >= -_EPS and t_min <= 1.0 + _EPS:
        return 0.0

    breakpoints = {0.0, 1.0}
    for value, change, half_size in zip(start, delta, half_sizes):
        if abs(change) <= _EPS:
            continue
        for boundary in (-half_size, half_size):
            parameter = (boundary - value) / change
            if _EPS < parameter < 1.0 - _EPS:
                breakpoints.add(parameter)
    ordered = sorted(breakpoints)

    def squared_distance(parameter: float) -> float:
        total = 0.0
        for value, change, half_size in zip(start, delta, half_sizes):
            coordinate = value + change * parameter
            outside = max(abs(coordinate) - half_size, 0.0)
            total += outside * outside
        return total

    best = min(squared_distance(parameter) for parameter in ordered)
    for left, right in zip(ordered, ordered[1:]):
        if right - left <= _EPS:
            continue
        midpoint = (left + right) / 2
        numerator = 0.0
        denominator = 0.0
        for value, change, half_size in zip(start, delta, half_sizes):
            coordinate = value + change * midpoint
            if coordinate < -half_size:
                boundary = -half_size
            elif coordinate > half_size:
                boundary = half_size
            else:
                continue
            numerator += change * (value - boundary)
            denominator += change * change
        if denominator <= _EPS:
            continue
        parameter = -numerator / denominator
        if left - _EPS <= parameter <= right + _EPS:
            parameter = max(left, min(right, parameter))
            best = min(best, squared_distance(parameter))
    return math.sqrt(max(best, 0.0))


def _segment_box_distance(a: Point3, b: Point3, box: Box3D) -> float:
    return _segment_aabb_distance(
        _box_local_coordinates(a, box),
        _box_local_coordinates(b, box),
        (box.size.x / 2, box.size.y / 2, box.size.z / 2),
    )


def _rule_contains_point(point: Point3, rule: _Rule) -> bool:
    if not _point_in_bounds(point, rule.bounds):
        return False
    if isinstance(rule.geometry, Box3D):
        return _point_box_distance(point, rule.geometry) <= rule.tolerance_m + _EPS
    if isinstance(rule.geometry, Polyline3D):
        return any(
            _point_segment_distance(point, start, end) <= rule.tolerance_m + _EPS
            for start, end in zip(rule.geometry.points, rule.geometry.points[1:])
        )
    if isinstance(rule.geometry, Polygon3D):
        return _point_polygon_distance(point, rule.geometry, rule.id) <= rule.tolerance_m + _EPS
    return True


def _rule_intersects_segment(a: Point3, b: Point3, rule: _Rule) -> bool:
    if not _segment_intersects_bounds(a, b, rule.bounds):
        return False
    if isinstance(rule.geometry, Box3D):
        return _segment_box_distance(a, b, rule.geometry) <= rule.tolerance_m + _EPS
    if isinstance(rule.geometry, Polyline3D):
        return any(
            _segment_segment_distance(a, b, start, end) <= rule.tolerance_m + _EPS
            for start, end in zip(rule.geometry.points, rule.geometry.points[1:])
        )
    if isinstance(rule.geometry, Polygon3D):
        return _segment_polygon_distance(a, b, rule.geometry, rule.id) <= rule.tolerance_m + _EPS
    return True


def _delta(a: Point3, b: Point3) -> tuple[float, float, float]:
    return (b.x - a.x, b.y - a.y, b.z - a.z)


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _point_segment_distance(point: Point3, start: Point3, end: Point3) -> float:
    segment = _delta(start, end)
    length_sq = _dot(segment, segment)
    if length_sq <= _EPS:
        return _distance(point, start)
    from_start = _delta(start, point)
    t = max(0.0, min(1.0, _dot(from_start, segment) / length_sq))
    closest = Point3(
        x=start.x + segment[0] * t,
        y=start.y + segment[1] * t,
        z=start.z + segment[2] * t,
    )
    return _distance(point, closest)


def _segment_segment_distance(a: Point3, b: Point3, c: Point3, d: Point3) -> float:
    u = _delta(a, b)
    v = _delta(c, d)
    w = _delta(c, a)
    aa = _dot(u, u)
    cc = _dot(v, v)
    if aa <= _EPS:
        return _point_segment_distance(a, c, d)
    if cc <= _EPS:
        return _point_segment_distance(c, a, b)

    bb = _dot(u, v)
    dd = _dot(u, w)
    ee = _dot(v, w)
    denominator = aa * cc - bb * bb
    if denominator <= _EPS:
        s = 0.0
    else:
        s = max(0.0, min(1.0, (bb * ee - cc * dd) / denominator))
    t = (bb * s + ee) / cc
    if t < 0.0:
        t = 0.0
        s = max(0.0, min(1.0, -dd / aa))
    elif t > 1.0:
        t = 1.0
        s = max(0.0, min(1.0, (bb - dd) / aa))

    left = Point3(x=a.x + u[0] * s, y=a.y + u[1] * s, z=a.z + u[2] * s)
    right = Point3(x=c.x + v[0] * t, y=c.y + v[1] * t, z=c.z + v[2] * t)
    return _distance(left, right)


def _polygon_plane(
    polygon: Polygon3D,
    rule_id: str,
) -> tuple[Point3, tuple[float, float, float]]:
    origin = polygon.points[0]
    normal = None
    for first, second in zip(polygon.points[1:], polygon.points[2:]):
        candidate = _cross(_delta(origin, first), _delta(origin, second))
        magnitude = math.sqrt(_dot(candidate, candidate))
        if magnitude > _EPS:
            normal = tuple(value / magnitude for value in candidate)
            break
    if normal is None:
        raise RoutingError(f"route constraint {rule_id!r} has degenerate Polygon3D geometry")
    for point in polygon.points:
        if abs(_dot(_delta(origin, point), normal)) > 1e-7:
            raise RoutingError(f"route constraint {rule_id!r} has non-planar Polygon3D geometry")
    return origin, normal


def _project_2d(point: Point3, normal: tuple[float, float, float]) -> tuple[float, float]:
    drop = max(range(3), key=lambda index: abs(normal[index]))
    if drop == 0:
        return point.y, point.z
    if drop == 1:
        return point.x, point.z
    return point.x, point.y


def _point_on_segment_2d(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    cross = (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (end[0] - start[0])
    if abs(cross) > 1e-9:
        return False
    dot = (point[0] - start[0]) * (point[0] - end[0]) + (point[1] - start[1]) * (point[1] - end[1])
    return dot <= 1e-9


def _point_in_polygon_projection(
    point: Point3,
    polygon: Polygon3D,
    normal: tuple[float, float, float],
) -> bool:
    target = _project_2d(point, normal)
    vertices = [_project_2d(vertex, normal) for vertex in polygon.points]
    for start, end in zip(vertices, (*vertices[1:], vertices[0])):
        if _point_on_segment_2d(target, start, end):
            return True

    inside = False
    x, y = target
    previous = vertices[-1]
    for current in vertices:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _point_polygon_distance(point: Point3, polygon: Polygon3D, rule_id: str) -> float:
    origin, normal = _polygon_plane(polygon, rule_id)
    signed_distance = _dot(_delta(origin, point), normal)
    projected = Point3(
        x=point.x - normal[0] * signed_distance,
        y=point.y - normal[1] * signed_distance,
        z=point.z - normal[2] * signed_distance,
    )
    if _point_in_polygon_projection(projected, polygon, normal):
        return abs(signed_distance)
    edges = zip(polygon.points, (*polygon.points[1:], polygon.points[0]))
    return min(_point_segment_distance(point, start, end) for start, end in edges)


def _segment_polygon_distance(a: Point3, b: Point3, polygon: Polygon3D, rule_id: str) -> float:
    origin, normal = _polygon_plane(polygon, rule_id)
    distance_a = _dot(_delta(origin, a), normal)
    distance_b = _dot(_delta(origin, b), normal)

    if abs(distance_a) <= _EPS and _point_in_polygon_projection(a, polygon, normal):
        return 0.0
    if abs(distance_b) <= _EPS and _point_in_polygon_projection(b, polygon, normal):
        return 0.0
    denominator = distance_a - distance_b
    if abs(denominator) > _EPS:
        t = distance_a / denominator
        if -_EPS <= t <= 1.0 + _EPS:
            intersection = Point3(
                x=a.x + (b.x - a.x) * t,
                y=a.y + (b.y - a.y) * t,
                z=a.z + (b.z - a.z) * t,
            )
            if _point_in_polygon_projection(intersection, polygon, normal):
                return 0.0

    distances = [
        _point_polygon_distance(a, polygon, rule_id),
        _point_polygon_distance(b, polygon, rule_id),
    ]
    for start, end in zip(polygon.points, (*polygon.points[1:], polygon.points[0])):
        distances.append(_segment_segment_distance(a, b, start, end))
    return min(distances)


def _unit(vector: Vector3) -> tuple[float, float, float]:
    magnitude = vector.magnitude
    return (vector.x / magnitude, vector.y / magnitude, vector.z / magnitude)


def _offset(point: Point3, direction: tuple[float, float, float], distance: float) -> Point3:
    return Point3(
        x=point.x + direction[0] * distance,
        y=point.y + direction[1] * distance,
        z=point.z + direction[2] * distance,
    )


def _vector(a: Point3, b: Point3) -> tuple[float, float, float]:
    dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length <= _EPS:
        raise RoutingError("zero-length vector has no direction")
    return (dx / length, dy / length, dz / length)


def _direction_vector(code: int) -> tuple[float, float, float]:
    return {
        0: (1.0, 0.0, 0.0),
        1: (-1.0, 0.0, 0.0),
        2: (0.0, 1.0, 0.0),
        3: (0.0, -1.0, 0.0),
        4: (0.0, 0.0, 1.0),
        5: (0.0, 0.0, -1.0),
    }[code]


def _is_turn(
    previous: tuple[float, float, float] | None,
    current: tuple[float, float, float] | None,
) -> bool:
    if previous is None or current is None:
        return False
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(previous, current))))
    return abs(dot - 1.0) > 1e-9


def _distance(a: Point3, b: Point3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _canon(value: float, precision: int) -> float:
    rounded = round(float(value), precision)
    return 0.0 if rounded == -0.0 else rounded


def _dedupe(points: list[Point3]) -> list[Point3]:
    result: list[Point3] = []
    for point in points:
        if not result or _distance(result[-1], point) > _EPS:
            result.append(point)
    return result


def _simplify(points: list[Point3]) -> list[Point3]:
    points = _dedupe(points)
    if len(points) <= 2:
        return points
    result = [points[0]]
    for index in range(1, len(points) - 1):
        a = result[-1]
        b = points[index]
        c = points[index + 1]
        ab = _vector(a, b)
        bc = _vector(b, c)
        dot = sum(x * y for x, y in zip(ab, bc))
        if abs(dot - 1.0) <= 1e-9:
            continue
        result.append(b)
    result.append(points[-1])
    return result


def _count_bends(points: list[Point3]) -> int:
    bends = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        if _is_turn(_vector(a, b), _vector(b, c)):
            bends += 1
    return bends


def _direction_token(a: Point3, b: Point3) -> str:
    vector = _vector(a, b)
    axes = {
        (1.0, 0.0, 0.0): "+x",
        (-1.0, 0.0, 0.0): "-x",
        (0.0, 1.0, 0.0): "+y",
        (0.0, -1.0, 0.0): "-y",
        (0.0, 0.0, 1.0): "+z",
        (0.0, 0.0, -1.0): "-z",
    }
    for known, token in axes.items():
        if all(abs(left - right) <= 1e-9 for left, right in zip(vector, known)):
            return token
    return "v({:.6f},{:.6f},{:.6f})".format(*vector)


def _build_fittings(
    route_id: str,
    points: list[Point3],
    diameter: float | None,
    provenance: tuple[Provenance, ...],
) -> tuple[RouteFitting, ...]:
    if len(points) < 3:
        return ()
    directions = [_direction_token(a, b) for a, b in zip(points, points[1:])]
    fittings: list[RouteFitting] = []
    for index, (a, vertex, c) in enumerate(zip(points, points[1:], points[2:]), start=1):
        incoming = _vector(a, vertex)
        outgoing = _vector(vertex, c)
        dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(incoming, outgoing))))
        angle = math.acos(dot)
        if angle <= 1e-9:
            continue
        signature = ">".join(directions[: index + 1])
        fitting_id = stable_id("fitting", f"{route_id}:turn:{signature}")
        if abs(angle - math.pi / 2) <= 1e-6:
            fitting_type = "elbow-90"
        elif abs(angle - math.pi / 4) <= 1e-6:
            fitting_type = "elbow-45"
        else:
            fitting_type = "elbow"
        fittings.append(
            RouteFitting(
                id=fitting_id,
                route_id=route_id,
                fitting_type=fitting_type,
                pose=Pose(position=vertex),
                nominal_diameter_m=diameter,
                angle_radians=angle,
                provenance=provenance,
                attributes={"routing_turn_signature": signature},
            )
        )
    return tuple(fittings)
