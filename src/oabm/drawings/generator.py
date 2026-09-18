from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, Protocol

from oabm.model import (
    Box3D,
    BuildingModel,
    Ceiling,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Entity,
    Level,
    Obstacle,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Route,
    RouteConstraint,
    RouteFitting,
    Size3,
    Slab,
    Space,
    Vector3,
    Wall,
)

from .model import (
    Bounds2,
    DrawingDimension,
    DrawingPrimitive,
    DrawingSchedule,
    DrawingSet,
    DrawingView,
    LineStyle,
    Point2,
    ScheduleColumn,
    ScheduleRow,
    SourceProvenance,
    SourceReference,
    union_bounds,
)
from .projection import (
    ProjectionFrame,
    clip_polygon,
    clip_polyline,
    clip_polyline_depth,
    depth_range,
    frame_from_view_direction,
    geometry_points,
    oriented_box_corners,
    project_points,
    section_intersection_edges,
)

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class VisibilityPolicy:
    architecture: bool = True
    electrical: bool = True
    routes: bool = True
    obstacles: bool = False
    constraints: bool = False
    spaces: bool = True
    labels: bool = True
    dimensions: bool = True


@dataclass(frozen=True, slots=True)
class PlanSpec:
    id: str
    level_id: str
    cut_height_m: float = 1.2
    view_depth_below_m: float = 0.25
    view_depth_above_m: float = 0.25
    bounds: Bounds2 | None = None
    scale: float = 50.0
    visibility: VisibilityPolicy = VisibilityPolicy()

    def __post_init__(self) -> None:
        if not self.id or not self.level_id:
            raise ValueError("plan id and level_id are required")
        if self.cut_height_m < 0 or self.view_depth_below_m < 0 or self.view_depth_above_m < 0:
            raise ValueError("plan cut/depth distances must be >= 0")
        if self.scale <= 0:
            raise ValueError("plan scale must be > 0")


@dataclass(frozen=True, slots=True)
class ElevationSpec:
    id: str
    origin: Point3
    direction: Vector3
    near_m: float = -1000.0
    far_m: float = 1000.0
    bounds: Bounds2 | None = None
    scale: float = 50.0
    level_ids: tuple[str, ...] = ()
    visibility: VisibilityPolicy = VisibilityPolicy()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("elevation id is required")
        if self.near_m > self.far_m:
            raise ValueError("elevation near_m must be <= far_m")
        if self.scale <= 0:
            raise ValueError("elevation scale must be > 0")


@dataclass(frozen=True, slots=True)
class SectionSpec:
    id: str
    origin: Point3
    direction: Vector3
    depth_m: float = 3.0
    back_depth_m: float = 0.05
    bounds: Bounds2 | None = None
    scale: float = 50.0
    level_ids: tuple[str, ...] = ()
    visibility: VisibilityPolicy = VisibilityPolicy()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("section id is required")
        if self.depth_m <= 0 or self.back_depth_m < 0:
            raise ValueError("section depth must be > 0 and back depth >= 0")
        if self.scale <= 0:
            raise ValueError("section scale must be > 0")


@dataclass(frozen=True, slots=True)
class Symbol:
    token: str
    label: str | None = None


class SymbolProvider(Protocol):
    def __call__(self, entity: Entity, view_type: str) -> Symbol | None: ...


class LabelProvider(Protocol):
    def __call__(self, entity: Entity, view_type: str) -> str | None: ...


def default_symbol_provider(entity: Entity, view_type: str) -> Symbol | None:
    if isinstance(entity, ElectricalEquipment):
        return Symbol(token=f"equipment:{entity.equipment_type}")
    if isinstance(entity, ElectricalDevice):
        return Symbol(token=f"device:{entity.device_type}")
    if isinstance(entity, Opening):
        return Symbol(token=f"opening:{entity.opening_type}")
    if isinstance(entity, RouteFitting):
        return Symbol(token=f"fitting:{entity.fitting_type}")
    return None


def default_label_provider(entity: Entity, view_type: str) -> str | None:
    if entity.name:
        return entity.name
    if isinstance(entity, ElectricalEquipment):
        return entity.equipment_type
    if isinstance(entity, ElectricalDevice):
        return entity.device_type
    if isinstance(entity, Space):
        return entity.id
    return None


def generate_drawing_set(
    model: BuildingModel,
    *,
    plans: Iterable[PlanSpec] = (),
    elevations: Iterable[ElevationSpec] = (),
    sections: Iterable[SectionSpec] = (),
    include_schedules: bool = True,
    symbol_provider: SymbolProvider = default_symbol_provider,
    label_provider: LabelProvider = default_label_provider,
) -> DrawingSet:
    """Generate immutable drawing output from canonical model semantics.

    Nothing in the returned structure is accepted as model input. Every geometric
    primitive and schedule row points back to canonical IDs, making the drawing a
    deterministic projection rather than an alternate source of truth.
    """
    views: list[DrawingView] = []
    for spec in sorted(tuple(plans), key=lambda item: item.id):
        views.append(generate_plan(model, spec, symbol_provider=symbol_provider, label_provider=label_provider))
    for spec in sorted(tuple(elevations), key=lambda item: item.id):
        views.append(generate_elevation(model, spec, symbol_provider=symbol_provider, label_provider=label_provider))
    for spec in sorted(tuple(sections), key=lambda item: item.id):
        views.append(generate_section(model, spec, symbol_provider=symbol_provider, label_provider=label_provider))

    schedules = generate_schedules(model) if include_schedules else ()
    # Keep a deterministic identity/provenance index for the canonical model.
    # Derived geometry only stores source IDs; this index lets downstream renderers
    # trace any drawing reference back to provenance without copying model semantics.
    source_index = tuple(_source_reference(entity) for entity in sorted(_entities(model), key=lambda item: item.id))
    return DrawingSet(
        model_id=model.model_id,
        views=tuple(sorted(views, key=lambda item: (item.view_type, item.id))),
        schedules=tuple(sorted(schedules, key=lambda item: item.id)),
        source_index=source_index,
    )


def generate_plan(
    model: BuildingModel,
    spec: PlanSpec,
    *,
    symbol_provider: SymbolProvider = default_symbol_provider,
    label_provider: LabelProvider = default_label_provider,
) -> DrawingView:
    level = _level(model, spec.level_id)
    cut_z = level.elevation_m + spec.cut_height_m
    min_z = level.elevation_m - spec.view_depth_below_m
    max_z = cut_z + spec.view_depth_above_m
    frame = ProjectionFrame(
        origin=Point3(x=0, y=0, z=level.elevation_m),
        right=Vector3(x=1, y=0, z=0),
        up=Vector3(x=0, y=1, z=0),
        normal=Vector3(x=0, y=0, z=1),
    )
    entities = _plan_entities(model, spec.level_id, min_z=min_z, max_z=max_z, visibility=spec.visibility)
    depth_interval = (min_z - level.elevation_m, max_z - level.elevation_m)
    bounds = spec.bounds or _auto_bounds(frame, entities, depth_interval=depth_interval)
    primitives: list[DrawingPrimitive] = []
    dimensions: list[DrawingDimension] = []

    for entity in entities:
        primitives.extend(
            _entity_primitives(
                entity,
                frame,
                bounds,
                "plan",
                symbol_provider,
                label_provider if spec.visibility.labels else None,
                cut_depth=cut_z - level.elevation_m,
                depth_interval=depth_interval,
            )
        )
        if spec.visibility.dimensions and isinstance(entity, Wall):
            dimensions.extend(_wall_dimensions(entity, frame, bounds))

    return DrawingView(
        id=spec.id,
        view_type="plan",
        title=f"{level.name or level.id} Plan",
        bounds=bounds,
        scale=spec.scale,
        primitives=_sort_primitives(primitives),
        dimensions=tuple(sorted(dimensions, key=lambda item: item.id)),
        metadata=(
            ("level_id", level.id),
            ("cut_elevation_m", _fmt_number(cut_z)),
            ("source_frame_id", model.coordinate_system.frame_id),
        ),
    )


def generate_elevation(
    model: BuildingModel,
    spec: ElevationSpec,
    *,
    symbol_provider: SymbolProvider = default_symbol_provider,
    label_provider: LabelProvider = default_label_provider,
) -> DrawingView:
    frame = frame_from_view_direction(origin=spec.origin, direction=spec.direction)
    entities = _spatial_entities(model, spec.level_ids, spec.visibility)
    entities = tuple(entity for entity in entities if _entity_depth_overlaps(entity, frame, spec.near_m, spec.far_m))
    depth_interval = (spec.near_m, spec.far_m)
    bounds = spec.bounds or _auto_bounds(frame, entities, depth_interval=depth_interval)
    primitives: list[DrawingPrimitive] = []
    dimensions: list[DrawingDimension] = []
    for entity in entities:
        primitives.extend(
            _entity_primitives(
                entity,
                frame,
                bounds,
                "elevation",
                symbol_provider,
                label_provider if spec.visibility.labels else None,
                depth_interval=depth_interval,
            )
        )
        if spec.visibility.dimensions and isinstance(entity, Opening):
            dimensions.extend(_opening_dimensions(entity, frame, bounds))
    return DrawingView(
        id=spec.id,
        view_type="elevation",
        title=f"Elevation {spec.id}",
        bounds=bounds,
        scale=spec.scale,
        primitives=_sort_primitives(primitives),
        dimensions=tuple(sorted(dimensions, key=lambda item: item.id)),
        metadata=(
            ("view_direction", f"{_fmt_number(spec.direction.x)},{_fmt_number(spec.direction.y)},{_fmt_number(spec.direction.z)}"),
            ("source_frame_id", model.coordinate_system.frame_id),
        ),
    )


def generate_section(
    model: BuildingModel,
    spec: SectionSpec,
    *,
    symbol_provider: SymbolProvider = default_symbol_provider,
    label_provider: LabelProvider = default_label_provider,
) -> DrawingView:
    frame = frame_from_view_direction(origin=spec.origin, direction=spec.direction)
    entities = _spatial_entities(model, spec.level_ids, spec.visibility)
    entities = tuple(entity for entity in entities if _entity_depth_overlaps(entity, frame, -spec.back_depth_m, spec.depth_m))
    depth_interval = (-spec.back_depth_m, spec.depth_m)
    bounds = spec.bounds or _auto_bounds(frame, entities, depth_interval=depth_interval)
    primitives: list[DrawingPrimitive] = []
    for entity in entities:
        primitives.extend(
            _entity_primitives(
                entity,
                frame,
                bounds,
                "section",
                symbol_provider,
                label_provider if spec.visibility.labels else None,
                depth_interval=depth_interval,
            )
        )
    return DrawingView(
        id=spec.id,
        view_type="section",
        title=f"Section {spec.id}",
        bounds=bounds,
        scale=spec.scale,
        primitives=_sort_primitives(primitives),
        dimensions=(),
        metadata=(
            ("section_depth_m", _fmt_number(spec.depth_m)),
            ("back_depth_m", _fmt_number(spec.back_depth_m)),
            ("source_frame_id", model.coordinate_system.frame_id),
        ),
    )


def generate_schedules(model: BuildingModel) -> tuple[DrawingSchedule, ...]:
    schedules = (
        _room_schedule(model),
        _opening_schedule(model),
        _equipment_schedule(model),
        _device_schedule(model),
        _circuit_schedule(model),
    )
    return tuple(schedule for schedule in schedules if schedule.rows)


def _room_schedule(model: BuildingModel) -> DrawingSchedule:
    columns = (
        ScheduleColumn("id", "ID"),
        ScheduleColumn("name", "Name"),
        ScheduleColumn("level", "Level"),
        ScheduleColumn("usage", "Usage"),
        ScheduleColumn("height", "Height", "m"),
        ScheduleColumn("confidence", "Confidence"),
    )
    rows = tuple(
        ScheduleRow(
            source_id=item.id,
            cells=(item.id, item.name or "", item.level_id, item.usage or "", _fmt_optional(item.height_m), _fmt_number(item.confidence)),
        )
        for item in sorted(model.spaces, key=lambda item: item.id)
    )
    return DrawingSchedule(id="schedule:rooms", title="Room Schedule", columns=columns, rows=rows)


def _opening_schedule(model: BuildingModel) -> DrawingSchedule:
    columns = (
        ScheduleColumn("id", "ID"),
        ScheduleColumn("name", "Name"),
        ScheduleColumn("type", "Type"),
        ScheduleColumn("host", "Host"),
        ScheduleColumn("width", "Width", "m"),
        ScheduleColumn("height", "Height", "m"),
        ScheduleColumn("confidence", "Confidence"),
    )
    rows = tuple(
        ScheduleRow(
            source_id=item.id,
            cells=(item.id, item.name or "", item.opening_type, item.host_id, _fmt_number(item.size.x), _fmt_number(item.size.z), _fmt_number(item.confidence)),
        )
        for item in sorted(model.openings, key=lambda item: item.id)
    )
    return DrawingSchedule(id="schedule:openings", title="Opening Schedule", columns=columns, rows=rows)


def _equipment_schedule(model: BuildingModel) -> DrawingSchedule:
    columns = _electrical_columns()
    rows = tuple(_electrical_row(item, item.equipment_type) for item in sorted(model.electrical_equipment, key=lambda item: item.id))
    return DrawingSchedule(id="schedule:equipment", title="Electrical Equipment Schedule", columns=columns, rows=rows)


def _device_schedule(model: BuildingModel) -> DrawingSchedule:
    columns = _electrical_columns()
    rows = tuple(_electrical_row(item, item.device_type) for item in sorted(model.electrical_devices, key=lambda item: item.id))
    return DrawingSchedule(id="schedule:devices", title="Electrical Device Schedule", columns=columns, rows=rows)


def _circuit_schedule(model: BuildingModel) -> DrawingSchedule:
    port_owner = {item.id: item.owner_id for item in model.ports}
    columns = (
        ScheduleColumn("id", "ID"),
        ScheduleColumn("number", "Circuit"),
        ScheduleColumn("source", "Source"),
        ScheduleColumn("loads", "Loads"),
        ScheduleColumn("voltage", "Voltage", "V"),
        ScheduleColumn("poles", "Poles"),
        ScheduleColumn("phase", "Phase"),
        ScheduleColumn("load", "Load", "VA"),
        ScheduleColumn("routes", "Routes"),
    )
    rows = tuple(
        ScheduleRow(
            source_id=item.id,
            cells=(
                item.id,
                item.circuit_number or "",
                port_owner.get(item.source_port_id, item.source_port_id),
                ", ".join(sorted(port_owner.get(port_id, port_id) for port_id in item.load_port_ids)),
                _fmt_optional(item.voltage_v),
                "" if item.poles is None else str(item.poles),
                item.phase or "",
                _fmt_optional(item.load_va),
                ", ".join(sorted(item.route_ids)),
            ),
        )
        for item in sorted(model.circuits, key=lambda item: item.id)
    )
    return DrawingSchedule(id="schedule:circuits", title="Circuit Schedule", columns=columns, rows=rows)


def _electrical_columns() -> tuple[ScheduleColumn, ...]:
    return (
        ScheduleColumn("id", "ID"),
        ScheduleColumn("name", "Name"),
        ScheduleColumn("type", "Type"),
        ScheduleColumn("level", "Level"),
        ScheduleColumn("space", "Space"),
        ScheduleColumn("system", "System"),
        ScheduleColumn("voltage", "Voltage", "V"),
        ScheduleColumn("size", "Size (X×Y×Z)", "m"),
        ScheduleColumn("confidence", "Confidence"),
    )


def _electrical_row(item: ElectricalEquipment | ElectricalDevice, type_token: str) -> ScheduleRow:
    size = "" if item.size is None else "×".join(_fmt_number(value) for value in (item.size.x, item.size.y, item.size.z))
    return ScheduleRow(
        source_id=item.id,
        cells=(item.id, item.name or "", type_token, item.level_id or "", item.space_id or "", item.system or "", _fmt_optional(item.rated_voltage_v), size, _fmt_number(item.confidence)),
    )


def _entity_primitives(
    entity: Entity,
    frame: ProjectionFrame,
    bounds: Bounds2,
    view_type: str,
    symbol_provider: SymbolProvider,
    label_provider: LabelProvider | None,
    *,
    cut_depth: float | None = None,
    depth_interval: tuple[float, float] | None = None,
) -> list[DrawingPrimitive]:
    result: list[DrawingPrimitive] = []
    if isinstance(entity, Wall):
        for index, corners in enumerate(_wall_segment_corners(entity)):
            _, depths = project_points(frame, corners)
            plan_cut = cut_depth is not None and _depth_spans(depths, cut_depth)
            result.extend(
                _project_solid(
                    entity.id,
                    corners,
                    frame,
                    bounds,
                    layer="architecture:walls",
                    style=LineStyle(stroke="cut" if plan_cut else "object", weight="heavy" if plan_cut else "normal"),
                    suffix=f"segment:{index}:projection",
                )
            )
            if view_type == "section" and _depth_spans(depths, 0.0):
                result.extend(
                    _section_cut_profile(
                        entity.id,
                        corners,
                        _WALL_PRISM_EDGES,
                        frame,
                        bounds,
                        layer="architecture:walls",
                        suffix=f"segment:{index}:cut",
                    )
                )
    elif isinstance(entity, Space):
        if view_type == "plan" or entity.height_m is None:
            result.extend(
                _project_polygon_surface(
                    entity.id,
                    entity.footprint.points,
                    frame,
                    bounds,
                    layer="architecture:spaces",
                    style=LineStyle(stroke="space", pattern="dash"),
                )
            )
        else:
            corners = _vertical_prism_points(entity.footprint.points, entity.height_m)
            result.extend(_project_solid(entity.id, corners, frame, bounds, layer="architecture:spaces", style=LineStyle(stroke="space", pattern="dash")))
    elif isinstance(entity, (Slab, Ceiling)):
        layer = "architecture:slabs" if isinstance(entity, Slab) else "architecture:ceilings"
        # v1 gives slabs/ceilings a 3D footprint plus thickness but does not define
        # an extrusion normal. Project the authoritative footprint without
        # inventing a cut volume. A future contract can make that solid explicit.
        result.extend(
            _project_polygon_surface(
                entity.id,
                entity.footprint.points,
                frame,
                bounds,
                layer=layer,
                style=LineStyle(stroke="object"),
            )
        )
    elif isinstance(entity, Opening):
        result.extend(_box_entity_primitives(entity.id, entity.pose, entity.size, frame, bounds, layer="architecture:openings", style=LineStyle(stroke="opening")))
        if view_type == "section" and _box_intersects_section(entity.pose, entity.size, frame):
            result.extend(_box_section_cut(entity.id, entity.pose, entity.size, frame, bounds, layer="architecture:openings"))
        symbol = symbol_provider(entity, view_type)
        if symbol is not None:
            result.extend(_symbol_primitive(entity, frame, bounds, symbol, "symbols:openings"))
    elif isinstance(entity, (ElectricalEquipment, ElectricalDevice)):
        if entity.size is not None:
            layer = "electrical:equipment" if isinstance(entity, ElectricalEquipment) else "electrical:devices"
            result.extend(_box_entity_primitives(entity.id, entity.pose, entity.size, frame, bounds, layer=layer, style=LineStyle(stroke="electrical")))
            if view_type == "section" and _box_intersects_section(entity.pose, entity.size, frame):
                result.extend(_box_section_cut(entity.id, entity.pose, entity.size, frame, bounds, layer=layer))
        symbol = symbol_provider(entity, view_type)
        if symbol is not None:
            result.extend(_symbol_primitive(entity, frame, bounds, symbol, "symbols:electrical"))
    elif isinstance(entity, Route):
        fragments3 = (
            clip_polyline_depth(
                entity.centerline.points,
                frame,
                depth_interval[0],
                depth_interval[1],
            )
            if depth_interval is not None
            else (entity.centerline.points,)
        )
        for index, fragment3 in enumerate(fragments3):
            points, depths = project_points(frame, fragment3)
            is_cut = view_type == "section" and _depth_spans(depths, 0.0)
            suffix_prefix = "frag" if len(fragments3) == 1 else f"depth:{index}"
            result.extend(
                _polyline_primitives(
                    entity.id,
                    points,
                    bounds,
                    layer="electrical:routes",
                    style=LineStyle(
                        stroke="route",
                        weight="heavy" if is_cut else "normal",
                        pattern="dash",
                    ),
                    suffix_prefix=suffix_prefix,
                )
            )
    elif isinstance(entity, RouteFitting):
        symbol = symbol_provider(entity, view_type)
        if symbol is not None:
            result.extend(_symbol_primitive(entity, frame, bounds, symbol, "symbols:fittings"))
    elif isinstance(entity, Obstacle):
        result.extend(_generic_geometry_primitives(entity.id, entity.geometry, frame, bounds, "coordination:obstacles", LineStyle(stroke="obstacle", pattern="dash")))
    elif isinstance(entity, RouteConstraint):
        result.extend(_generic_geometry_primitives(entity.id, entity.geometry, frame, bounds, "coordination:constraints", LineStyle(stroke="constraint", pattern="dot")))

    if label_provider is not None:
        label = label_provider(entity, view_type)
        if label:
            anchor = _label_anchor(entity, frame, depth_interval=depth_interval)
            if anchor is not None and _inside(anchor, bounds):
                result.append(_primitive(entity.id, "text", "annotations:labels", (anchor,), text=label, style=LineStyle(stroke="annotation"), suffix="label"))
    return result



_WALL_PRISM_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)
_BOX_EDGES = (
    (0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3),
    (2, 6), (3, 7), (4, 5), (4, 6), (5, 7), (6, 7),
)

def _section_cut_profile(source_id, points3, edges, frame, bounds, *, layer, suffix):
    hits = section_intersection_edges(tuple(points3), edges, frame)
    if len(hits) < 2:
        return []
    hull = _convex_hull(hits)
    style = LineStyle(stroke="cut", weight="heavy")
    if len(hull) >= 3 and abs(_polygon_area(hull)) > _EPS:
        clipped = clip_polygon(hull, bounds)
        return [] if not clipped else [_primitive(source_id, "polygon", layer, clipped, closed=True, style=style, suffix=suffix)]
    return _polyline_primitives(source_id, hull, bounds, layer=layer, style=style, suffix_prefix=suffix)

def _box_section_cut(source_id, pose, size, frame, bounds, *, layer):
    corners = oriented_box_corners(pose, size.x, size.y, size.z)
    return _section_cut_profile(source_id, corners, _BOX_EDGES, frame, bounds, layer=layer, suffix="section-cut")

def _project_solid(source_id, points3, frame, bounds, *, layer, style, suffix="solid"):
    projected, _ = project_points(frame, points3)
    hull = _convex_hull(projected)
    if len(hull) >= 3 and abs(_polygon_area(hull)) > _EPS:
        clipped = clip_polygon(hull, bounds)
        return [] if not clipped else [_primitive(source_id, "polygon", layer, clipped, closed=True, style=style, suffix=suffix)]
    if len(hull) == 2:
        return _polyline_primitives(source_id, hull, bounds, layer=layer, style=style)
    return []


def _project_polygon_surface(source_id, points3, frame, bounds, *, layer, style):
    projected, _ = project_points(frame, points3)
    hull = _convex_hull(projected)
    if len(hull) >= 3 and abs(_polygon_area(hull)) > _EPS:
        clipped = clip_polygon(hull, bounds)
        return [] if not clipped else [_primitive(source_id, "polygon", layer, clipped, closed=True, style=style)]
    if len(hull) == 2:
        return _polyline_primitives(source_id, hull, bounds, layer=layer, style=style)
    return []


def _wall_segment_corners(wall: Wall) -> tuple[tuple[Point3, ...], ...]:
    result = []
    half = wall.thickness_m / 2.0
    for a, b in zip(wall.centerline.points, wall.centerline.points[1:]):
        dx = b.x - a.x
        dy = b.y - a.y
        planar = math.hypot(dx, dy)
        if planar <= _EPS:
            # A vertical/sloped centerline with no XY extent has no defined wall
            # normal in v1. Keep a deterministic square footprint rather than
            # inventing an orientation.
            offsets = ((-half, -half), (half, -half), (half, half), (-half, half))
            base = tuple(Point3(x=a.x + ox, y=a.y + oy, z=a.z) for ox, oy in offsets)
            top = tuple(Point3(x=p.x, y=p.y, z=p.z + wall.height_m) for p in base)
            result.append(base + top)
            continue
        nx = -dy / planar * half
        ny = dx / planar * half
        base = (
            Point3(x=a.x + nx, y=a.y + ny, z=a.z),
            Point3(x=b.x + nx, y=b.y + ny, z=b.z),
            Point3(x=b.x - nx, y=b.y - ny, z=b.z),
            Point3(x=a.x - nx, y=a.y - ny, z=a.z),
        )
        top = tuple(Point3(x=p.x, y=p.y, z=p.z + wall.height_m) for p in base)
        result.append(base + top)
    return tuple(result)


def _vertical_prism_points(points: tuple[Point3, ...], height: float) -> tuple[Point3, ...]:
    return tuple(points) + tuple(Point3(x=p.x, y=p.y, z=p.z + height) for p in points)


def _generic_geometry_primitives(source_id, geometry, frame, bounds, layer, style):
    if isinstance(geometry, Polyline3D):
        points, _ = project_points(frame, geometry.points)
        return _polyline_primitives(source_id, points, bounds, layer=layer, style=style)
    if isinstance(geometry, Polygon3D):
        points, _ = project_points(frame, geometry.points)
        clipped = clip_polygon(points, bounds)
        return [] if not clipped else [_primitive(source_id, "polygon", layer, clipped, closed=True, style=style)]
    if isinstance(geometry, Box3D):
        return _box_entity_primitives(source_id, geometry.pose, geometry.size, frame, bounds, layer=layer, style=style)
    return []


def _box_entity_primitives(source_id, pose, size, frame, bounds, *, layer, style):
    return _project_solid(source_id, oriented_box_corners(pose, size.x, size.y, size.z), frame, bounds, layer=layer, style=style)


def _symbol_primitive(entity: Entity, frame: ProjectionFrame, bounds: Bounds2, symbol: Symbol, layer: str) -> list[DrawingPrimitive]:
    point3 = getattr(getattr(entity, "pose", None), "position", None)
    if point3 is None:
        return []
    anchor, _ = frame.project(point3)
    if not _inside(anchor, bounds):
        return []
    return [_primitive(entity.id, "symbol", layer, (anchor,), symbol=symbol.token, text=symbol.label, style=LineStyle(stroke="symbol"), suffix="symbol")]


def _polyline_primitives(
    source_id: str,
    points: tuple[Point2, ...],
    bounds: Bounds2,
    *,
    layer: str,
    style: LineStyle,
    suffix_prefix: str = "frag",
) -> list[DrawingPrimitive]:
    if len(set(points)) < 2:
        return []
    fragments = clip_polyline(points, bounds)
    return [
        _primitive(source_id, "polyline", layer, fragment, style=style, suffix=f"{suffix_prefix}:{index}")
        for index, fragment in enumerate(fragments)
    ]


def _primitive(source_id, kind, layer, points, *, closed=False, style=LineStyle(), text=None, symbol=None, suffix="0"):
    return DrawingPrimitive(
        id=_derived_id("primitive", source_id, layer, suffix),
        kind=kind,
        layer=layer,
        source_ids=(source_id,),
        points=tuple(points),
        closed=closed,
        style=style,
        text=text,
        symbol=symbol,
    )


def _wall_dimensions(wall: Wall, frame: ProjectionFrame, bounds: Bounds2) -> list[DrawingDimension]:
    dimensions: list[DrawingDimension] = []
    for index, (a3, b3) in enumerate(zip(wall.centerline.points, wall.centerline.points[1:])):
        a, _ = frame.project(a3)
        b, _ = frame.project(b3)
        if a == b:
            continue
        if not (_inside(a, bounds) or _inside(b, bounds)):
            continue
        value = math.dist((a3.x, a3.y, a3.z), (b3.x, b3.y, b3.z))
        dimensions.append(
            DrawingDimension(
                id=_derived_id("dimension", wall.id, str(index)),
                source_ids=(wall.id,),
                start=a,
                end=b,
                offset_m=0.2,
                value_m=value,
                text=f"{_fmt_number(value)} m",
            )
        )
    return dimensions


def _opening_dimensions(opening: Opening, frame: ProjectionFrame, bounds: Bounds2) -> list[DrawingDimension]:
    corners = oriented_box_corners(opening.pose, opening.size.x, opening.size.y, opening.size.z)
    projected, _ = project_points(frame, corners)
    if not projected:
        return []
    min_x, max_x = min(p.x for p in projected), max(p.x for p in projected)
    min_y, max_y = min(p.y for p in projected), max(p.y for p in projected)
    result: list[DrawingDimension] = []
    width_a, width_b = Point2(x=min_x, y=min_y), Point2(x=max_x, y=min_y)
    height_a, height_b = Point2(x=max_x, y=min_y), Point2(x=max_x, y=max_y)
    if width_a != width_b and (_inside(width_a, bounds) or _inside(width_b, bounds)):
        value = max_x - min_x
        result.append(DrawingDimension(id=_derived_id("dimension", opening.id, "width"), source_ids=(opening.id,), start=width_a, end=width_b, offset_m=0.15, value_m=value, text=f"{_fmt_number(value)} m"))
    if height_a != height_b and (_inside(height_a, bounds) or _inside(height_b, bounds)):
        value = max_y - min_y
        result.append(DrawingDimension(id=_derived_id("dimension", opening.id, "height"), source_ids=(opening.id,), start=height_a, end=height_b, offset_m=0.15, value_m=value, text=f"{_fmt_number(value)} m"))
    return result


def _plan_entities(model: BuildingModel, level_id: str, *, min_z: float, max_z: float, visibility: VisibilityPolicy) -> tuple[Entity, ...]:
    result: list[Entity] = []
    if visibility.architecture:
        result.extend(
            item
            for item in (*model.walls, *model.slabs, *model.ceilings, *model.openings)
            if _entity_on_level(item, level_id, model) and _entity_z_overlap(item, min_z, max_z)
        )
    if visibility.spaces:
        result.extend(item for item in model.spaces if item.level_id == level_id)
    if visibility.electrical:
        result.extend(
            item
            for item in (*model.electrical_equipment, *model.electrical_devices)
            if (item.level_id in {None, level_id}) and _entity_z_overlap(item, min_z, max_z)
        )
    if visibility.routes:
        result.extend(item for item in model.routes if _polyline_z_overlap(item.centerline, min_z, max_z))
        route_ids = {item.id for item in result if isinstance(item, Route)}
        result.extend(item for item in model.route_fittings if item.route_id in route_ids and min_z <= item.pose.position.z <= max_z)
    if visibility.obstacles:
        result.extend(item for item in model.obstacles if item.level_id in {None, level_id})
    if visibility.constraints:
        result.extend(item for item in model.route_constraints if item.level_id in {None, level_id})
    return tuple(sorted(_dedupe_entities(result), key=lambda item: item.id))


def _spatial_entities(model: BuildingModel, level_ids: tuple[str, ...], visibility: VisibilityPolicy) -> tuple[Entity, ...]:
    allowed = set(level_ids)
    result: list[Entity] = []
    def level_ok(entity: Entity) -> bool:
        level_id = getattr(entity, "level_id", None)
        return not allowed or level_id is None or level_id in allowed or (isinstance(entity, Opening) and _opening_level(entity, model) in allowed)
    if visibility.architecture:
        result.extend(item for item in (*model.walls, *model.slabs, *model.ceilings, *model.openings) if level_ok(item))
    if visibility.spaces:
        result.extend(item for item in model.spaces if level_ok(item))
    if visibility.electrical:
        result.extend(item for item in (*model.electrical_equipment, *model.electrical_devices) if level_ok(item))
    if visibility.routes:
        result.extend(model.routes)
        result.extend(model.route_fittings)
    if visibility.obstacles:
        result.extend(item for item in model.obstacles if level_ok(item))
    if visibility.constraints:
        result.extend(item for item in model.route_constraints if level_ok(item))
    return tuple(sorted(_dedupe_entities(result), key=lambda item: item.id))


def _entity_on_level(entity: Entity, level_id: str, model: BuildingModel) -> bool:
    direct = getattr(entity, "level_id", None)
    if direct is not None:
        return direct == level_id
    if isinstance(entity, Opening):
        return _opening_level(entity, model) == level_id
    return False


def _opening_level(opening: Opening, model: BuildingModel) -> str | None:
    hosts = {item.id: item for item in (*model.walls, *model.slabs, *model.ceilings)}
    host = hosts.get(opening.host_id)
    return getattr(host, "level_id", None)



def _entity_z_overlap(entity: Entity, min_z: float, max_z: float) -> bool:
    points = _entity_points(entity)
    if not points:
        return False
    low = min(point.z for point in points)
    high = max(point.z for point in points)
    return high >= min_z - _EPS and low <= max_z + _EPS

def _entity_depth_overlaps(entity: Entity, frame: ProjectionFrame, near_m: float, far_m: float) -> bool:
    points = _entity_points(entity)
    if not points:
        return False
    low, high = depth_range(frame, points)
    return high >= near_m - _EPS and low <= far_m + _EPS


def _entity_points(entity: Entity) -> tuple[Point3, ...]:
    if isinstance(entity, Wall): return tuple(point for segment in _wall_segment_corners(entity) for point in segment)
    if isinstance(entity, Space):
        return _vertical_prism_points(entity.footprint.points, entity.height_m) if entity.height_m is not None else entity.footprint.points
    if isinstance(entity, (Slab, Ceiling)): return entity.footprint.points
    if isinstance(entity, Opening): return oriented_box_corners(entity.pose, entity.size.x, entity.size.y, entity.size.z)
    if isinstance(entity, (ElectricalEquipment, ElectricalDevice)):
        if entity.size is None: return (entity.pose.position,)
        return oriented_box_corners(entity.pose, entity.size.x, entity.size.y, entity.size.z)
    if isinstance(entity, Route): return entity.centerline.points
    if isinstance(entity, RouteFitting): return (entity.pose.position,)
    if isinstance(entity, (Obstacle, RouteConstraint)): return geometry_points(entity.geometry)
    return ()


def _auto_bounds(
    frame: ProjectionFrame,
    entities: tuple[Entity, ...],
    *,
    depth_interval: tuple[float, float] | None = None,
) -> Bounds2:
    points: list[Point2] = []
    for entity in entities:
        if isinstance(entity, Route) and depth_interval is not None:
            fragments = clip_polyline_depth(
                entity.centerline.points,
                frame,
                depth_interval[0],
                depth_interval[1],
            )
            for fragment in fragments:
                projected, _ = project_points(frame, fragment)
                points.extend(projected)
            continue
        projected, _ = project_points(frame, _entity_points(entity))
        points.extend(projected)
    return union_bounds(points, padding=0.5)


def _label_anchor(
    entity: Entity,
    frame: ProjectionFrame,
    *,
    depth_interval: tuple[float, float] | None = None,
) -> Point2 | None:
    if isinstance(entity, Wall):
        points, _ = project_points(frame, entity.centerline.points)
    elif isinstance(entity, (Slab, Ceiling, Space)):
        points, _ = project_points(frame, entity.footprint.points)
    elif isinstance(entity, (Opening, ElectricalEquipment, ElectricalDevice, RouteFitting)):
        return frame.project(entity.pose.position)[0]
    elif isinstance(entity, Route):
        route_points = entity.centerline.points
        if depth_interval is not None:
            fragments = clip_polyline_depth(
                route_points,
                frame,
                depth_interval[0],
                depth_interval[1],
            )
            route_points = tuple(point for fragment in fragments for point in fragment)
        points, _ = project_points(frame, route_points)
    elif isinstance(entity, (Obstacle, RouteConstraint)):
        points, _ = project_points(frame, geometry_points(entity.geometry))
    else:
        return None
    if not points:
        return None
    return Point2(x=sum(p.x for p in points) / len(points), y=sum(p.y for p in points) / len(points))


def _convex_hull(points: tuple[Point2, ...]) -> tuple[Point2, ...]:
    unique = sorted(set(points))
    if len(unique) <= 2:
        return tuple(unique)
    def turn(o, a, b): return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)
    lower: list[Point2] = []
    for point in unique:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[Point2] = []
    for point in reversed(unique):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _box_intersects_section(pose, size, frame):
    _, depths = project_points(frame, oriented_box_corners(pose, size.x, size.y, size.z))
    return min(depths) <= 0 <= max(depths)


def _polygon_area(points: tuple[Point2, ...]) -> float:
    if len(points) < 3:
        return 0.0
    return 0.5 * sum(a.x * b.y - b.x * a.y for a, b in zip(points, points[1:] + points[:1]))


def _depth_spans(depths: tuple[float, ...], target: float) -> bool:
    return bool(depths) and min(depths) <= target + _EPS and max(depths) >= target - _EPS


def _polyline_z_overlap(polyline: Polyline3D, min_z: float, max_z: float) -> bool:
    low = min(point.z for point in polyline.points)
    high = max(point.z for point in polyline.points)
    return high >= min_z - _EPS and low <= max_z + _EPS


def _inside(point: Point2, bounds: Bounds2) -> bool:
    return bounds.min_x - _EPS <= point.x <= bounds.max_x + _EPS and bounds.min_y - _EPS <= point.y <= bounds.max_y + _EPS


def _level(model: BuildingModel, level_id: str) -> Level:
    for level in model.levels:
        if level.id == level_id:
            return level
    raise ValueError(f"unknown level_id {level_id!r}")


def _source_reference(entity: Entity) -> SourceReference:
    provenance = tuple(
        SourceProvenance(
            source_kind=item.source_kind,
            source_id=item.source_id,
            source_element_id=item.source_element_id,
            page=item.page,
            method=item.method,
            confidence=item.confidence,
        )
        for item in entity.provenance
    )
    return SourceReference(canonical_id=entity.id, name=entity.name, confidence=entity.confidence, provenance=provenance)


def _entities(model: BuildingModel) -> tuple[Entity, ...]:
    return (
        *model.levels, *model.spaces, *model.walls, *model.slabs, *model.ceilings,
        *model.openings, *model.electrical_equipment, *model.electrical_devices,
        *model.ports, *model.obstacles, *model.route_constraints, *model.routes,
        *model.route_fittings, *model.circuits, *model.conductors,
    )


def _dedupe_entities(items: Iterable[Entity]) -> tuple[Entity, ...]:
    by_id: dict[str, Entity] = {}
    for item in items:
        by_id[item.id] = item
    return tuple(by_id.values())


def _sort_primitives(primitives: Iterable[DrawingPrimitive]) -> tuple[DrawingPrimitive, ...]:
    return tuple(sorted(primitives, key=lambda item: (item.layer, item.source_ids, item.kind, item.id)))


def _derived_id(kind: str, *parts: str) -> str:
    payload = "\x1f".join((kind, *parts)).encode("utf-8")
    return f"{kind}:{hashlib.sha256(payload).hexdigest()[:20]}"


def _fmt_number(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _fmt_optional(value: float | None) -> str:
    return "" if value is None else _fmt_number(value)
