from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable

from oabm.model import (
    Box3D,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Entity,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    RouteFitting,
    Size3,
)

from .model import (
    Annotation,
    Bounds2,
    Dimension,
    DrawingPrimitive,
    DrawingSet,
    DrawingView,
    Point2,
    Schedule,
    ScheduleColumn,
    ScheduleRow,
    SourceReference,
)
from .projection import ProjectionFrame, clip_polygon, clip_polyline, clip_segment_to_depth


@dataclass(frozen=True, slots=True)
class VisibilityFilter:
    include_categories: tuple[str, ...] | None = None
    exclude_entity_ids: tuple[str, ...] = ()
    min_confidence: float = 0.0
    systems: tuple[str, ...] | None = None

    def allows(self, category: str, entity: Entity) -> bool:
        if self.include_categories is not None and category not in self.include_categories:
            return False
        if entity.id in self.exclude_entity_ids or entity.confidence < self.min_confidence:
            return False
        if self.systems is not None:
            system = getattr(entity, "system", None)
            if system not in self.systems:
                return False
        return True


@dataclass(frozen=True, slots=True)
class SymbolContext:
    view_id: str
    view_kind: str
    anchor: Point2
    source: SourceReference


@dataclass(frozen=True, slots=True)
class AnnotationContext:
    view_id: str
    view_kind: str
    anchor: Point2
    source: SourceReference


SymbolHook = Callable[[Entity, SymbolContext], Iterable[DrawingPrimitive]]
AnnotationHook = Callable[[Entity, AnnotationContext], Iterable[Annotation]]


def _source(entity: Entity) -> SourceReference:
    provenance = tuple(
        sorted(
            f"{record.source_kind}:{record.source_id}:"
            f"{record.source_element_id or ''}"
            for record in entity.provenance
        )
    )
    return SourceReference(entity_id=entity.id, provenance=provenance)


def _label(entity: Entity) -> str:
    return entity.name.strip() if entity.name and entity.name.strip() else entity.id


def _metadata(**items: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in items.items() if value is not None))


def _qrotate(pose: Pose, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    q = pose.rotation
    qv = (q.x, q.y, q.z)
    vx, vy, vz = vector
    uv = (
        qv[1] * vz - qv[2] * vy,
        qv[2] * vx - qv[0] * vz,
        qv[0] * vy - qv[1] * vx,
    )
    uuv = (
        qv[1] * uv[2] - qv[2] * uv[1],
        qv[2] * uv[0] - qv[0] * uv[2],
        qv[0] * uv[1] - qv[1] * uv[0],
    )
    scale_uv = 2.0 * q.w
    return (
        vx + scale_uv * uv[0] + 2.0 * uuv[0],
        vy + scale_uv * uv[1] + 2.0 * uuv[1],
        vz + scale_uv * uv[2] + 2.0 * uuv[2],
    )


def _pose_point(pose: Pose, local: tuple[float, float, float]) -> Point3:
    rotated = _qrotate(pose, local)
    return Point3(
        x=pose.position.x + rotated[0],
        y=pose.position.y + rotated[1],
        z=pose.position.z + rotated[2],
    )


def _box_corners(pose: Pose, size: Size3) -> tuple[Point3, ...]:
    hx, hy, hz = size.x / 2.0, size.y / 2.0, size.z / 2.0
    return tuple(
        _pose_point(pose, (sx * hx, sy * hy, sz * hz))
        for sx, sy, sz in (
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        )
    )


_BOX_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)


def _entity_points(model: BuildingModel) -> tuple[Point3, ...]:
    points: list[Point3] = []
    for collection in (model.spaces, model.slabs, model.ceilings):
        for entity in collection:
            points.extend(entity.footprint.points)
    for wall in model.walls:
        for point in wall.centerline.points:
            points.append(point)
            points.append(Point3(x=point.x, y=point.y, z=point.z + wall.height_m))
    for opening in model.openings:
        points.extend(_box_corners(opening.pose, opening.size))
    for entity in (*model.electrical_equipment, *model.electrical_devices):
        if entity.size is None:
            points.append(entity.pose.position)
        else:
            points.extend(_box_corners(entity.pose, entity.size))
    for route in model.routes:
        points.extend(route.centerline.points)
    for fitting in model.route_fittings:
        points.append(fitting.pose.position)
    for item in (*model.obstacles, *model.route_constraints):
        geometry = item.geometry
        if isinstance(geometry, Box3D):
            points.extend(_box_corners(geometry.pose, geometry.size))
        elif isinstance(geometry, (Polyline3D, Polygon3D)):
            points.extend(geometry.points)
    return tuple(points)


def _projected_bounds(model: BuildingModel, frame: ProjectionFrame, margin: float = 0.5) -> Bounds2:
    projected = [frame.project(point)[0] for point in _entity_points(model)]
    if not projected:
        return Bounds2(-1.0, -1.0, 1.0, 1.0)
    bounds = Bounds2(
        min(point.x for point in projected),
        min(point.y for point in projected),
        max(point.x for point in projected),
        max(point.y for point in projected),
    )
    if bounds.width < 1e-9:
        bounds = Bounds2(bounds.min_x - 0.5, bounds.min_y, bounds.max_x + 0.5, bounds.max_y)
    if bounds.height < 1e-9:
        bounds = Bounds2(bounds.min_x, bounds.min_y - 0.5, bounds.max_x, bounds.max_y + 0.5)
    return bounds.expanded(margin)


def _default_symbol(entity: Entity, context: SymbolContext) -> Iterable[DrawingPrimitive]:
    size = 0.12
    layer = "electrical-symbol"
    kind = getattr(entity, "equipment_type", getattr(entity, "device_type", getattr(entity, "fitting_type", "symbol")))
    yield DrawingPrimitive(
        id=f"{context.view_id}:{entity.id}:symbol-a",
        kind="line",
        layer=layer,
        points=(
            Point2(context.anchor.x - size, context.anchor.y - size),
            Point2(context.anchor.x + size, context.anchor.y + size),
        ),
        source=context.source,
        metadata=_metadata(symbol=kind),
    )
    yield DrawingPrimitive(
        id=f"{context.view_id}:{entity.id}:symbol-b",
        kind="line",
        layer=layer,
        points=(
            Point2(context.anchor.x - size, context.anchor.y + size),
            Point2(context.anchor.x + size, context.anchor.y - size),
        ),
        source=context.source,
        metadata=_metadata(symbol=kind),
    )


def _default_annotation(entity: Entity, context: AnnotationContext) -> Iterable[Annotation]:
    yield Annotation(
        id=f"{context.view_id}:{entity.id}:label",
        text=_label(entity),
        position=Point2(context.anchor.x + 0.16, context.anchor.y + 0.16),
        source=context.source,
    )


class DrawingGenerator:
    """Deterministically derives 2D views and schedules from a canonical model.

    The returned objects contain projected geometry plus references back to canonical
    entity IDs. They intentionally contain no editable building/electrical semantic
    model of their own.
    """

    def __init__(
        self,
        model: BuildingModel,
        *,
        symbol_hook: SymbolHook | None = None,
        annotation_hook: AnnotationHook | None = None,
    ) -> None:
        self.model = model
        self.symbol_hook = symbol_hook or _default_symbol
        self.annotation_hook = annotation_hook or _default_annotation
        self._levels = {level.id: level for level in model.levels}
        self._walls = {wall.id: wall for wall in model.walls}

    def plan(
        self,
        level_id: str,
        *,
        clip_bounds: Bounds2 | None = None,
        visibility: VisibilityFilter = VisibilityFilter(),
    ) -> DrawingView:
        level = self._levels.get(level_id)
        if level is None:
            raise ValueError(f"unknown level_id {level_id!r}")
        frame = ProjectionFrame.plan(level.elevation_m)
        depth_min = -0.05
        depth_max = (level.height_m if level.height_m is not None else 3.0) + 0.05
        view_id = f"plan:{level.id}"
        return self._generate_view(
            view_id=view_id,
            name=f"Plan - {level.name or level.id}",
            kind="plan",
            frame=frame,
            clip_bounds=clip_bounds or _projected_bounds(self.model, frame),
            visibility=visibility,
            level_id=level.id,
            depth_min=depth_min,
            depth_max=depth_max,
            metadata=_metadata(level_id=level.id, elevation_m=level.elevation_m),
        )

    def elevation(
        self,
        direction: str,
        *,
        clip_bounds: Bounds2 | None = None,
        visibility: VisibilityFilter = VisibilityFilter(),
    ) -> DrawingView:
        direction = direction.lower()
        frame = ProjectionFrame.elevation(direction)
        view_id = f"elevation:{direction}"
        return self._generate_view(
            view_id=view_id,
            name=f"{direction.title()} Elevation",
            kind="elevation",
            frame=frame,
            clip_bounds=clip_bounds or _projected_bounds(self.model, frame),
            visibility=visibility,
            metadata=_metadata(direction=direction),
        )

    def section(
        self,
        section_id: str,
        start: Point3,
        end: Point3,
        *,
        depth_m: float = 0.2,
        clip_bounds: Bounds2 | None = None,
        visibility: VisibilityFilter = VisibilityFilter(),
    ) -> DrawingView:
        if depth_m <= 0:
            raise ValueError("depth_m must be > 0")
        frame = ProjectionFrame.section(start, end)
        depth_min, depth_max = -depth_m / 2.0, depth_m / 2.0
        view_id = f"section:{section_id}"
        return self._generate_view(
            view_id=view_id,
            name=f"Section {section_id}",
            kind="section",
            frame=frame,
            clip_bounds=clip_bounds or _projected_bounds(self.model, frame),
            visibility=visibility,
            depth_min=depth_min,
            depth_max=depth_max,
            metadata=_metadata(
                section_id=section_id,
                start=f"{start.x},{start.y},{start.z}",
                end=f"{end.x},{end.y},{end.z}",
                depth_m=depth_m,
            ),
        )

    def schedules(self) -> tuple[Schedule, ...]:
        return (
            self._equipment_schedule(),
            self._device_schedule(),
            self._opening_schedule(),
            self._circuit_schedule(),
        )

    def default_set(self) -> DrawingSet:
        views: list[DrawingView] = []
        for level in sorted(self.model.levels, key=lambda item: (item.elevation_m, item.id)):
            views.append(self.plan(level.id))
        for direction in ("north", "east", "south", "west"):
            views.append(self.elevation(direction))

        points = _entity_points(self.model)
        if points:
            min_x, max_x = min(p.x for p in points), max(p.x for p in points)
            min_y, max_y = min(p.y for p in points), max(p.y for p in points)
            min_z = min(p.z for p in points)
            if math.isclose(min_x, max_x):
                max_x = min_x + 1.0
            if math.isclose(min_y, max_y):
                max_y = min_y + 1.0
            mid_x, mid_y = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
            views.append(
                self.section(
                    "A-A",
                    Point3(x=min_x, y=mid_y, z=min_z),
                    Point3(x=max_x, y=mid_y, z=min_z),
                )
            )
            views.append(
                self.section(
                    "B-B",
                    Point3(x=mid_x, y=min_y, z=min_z),
                    Point3(x=mid_x, y=max_y, z=min_z),
                )
            )

        return DrawingSet(
            model_id=self.model.model_id,
            model_schema_version=self.model.schema_version,
            views=tuple(views),
            schedules=self.schedules(),
        )

    def _generate_view(
        self,
        *,
        view_id: str,
        name: str,
        kind: str,
        frame: ProjectionFrame,
        clip_bounds: Bounds2,
        visibility: VisibilityFilter,
        level_id: str | None = None,
        depth_min: float | None = None,
        depth_max: float | None = None,
        metadata: tuple[tuple[str, str], ...] = (),
    ) -> DrawingView:
        primitives: list[DrawingPrimitive] = []
        annotations: list[Annotation] = []

        def level_matches(entity: Entity) -> bool:
            if level_id is None:
                return True
            entity_level = getattr(entity, "level_id", None)
            if entity_level is not None:
                return entity_level == level_id
            if isinstance(entity, Opening):
                wall = self._walls.get(entity.host_id)
                return wall is not None and wall.level_id == level_id
            return True

        def add_polyline(
            entity: Entity,
            category: str,
            role: str,
            points: tuple[Point3, ...],
            *,
            layer: str,
            closed: bool = False,
            extra_metadata: tuple[tuple[str, str], ...] = (),
        ) -> None:
            if not visibility.allows(category, entity) or not level_matches(entity):
                return
            source = _source(entity)
            if closed and kind != "section":
                depths = [frame.project(point)[1] for point in points]
                if depth_min is not None and max(depths) < depth_min - 1e-9:
                    return
                if depth_max is not None and min(depths) > depth_max + 1e-9:
                    return
                projected = tuple(frame.project(point)[0] for point in points)
                clipped = clip_polygon(projected, clip_bounds)
                if clipped:
                    primitives.append(
                        DrawingPrimitive(
                            id=f"{view_id}:{entity.id}:{role}",
                            kind="polygon",
                            layer=layer,
                            points=clipped,
                            source=source,
                            closed=True,
                            label=_label(entity),
                            metadata=extra_metadata,
                        )
                    )
                return

            runs: list[list[Point2]] = []
            current: list[Point2] = []
            segment_pairs = list(zip(points, points[1:]))
            if closed:
                segment_pairs.append((points[-1], points[0]))
            for a, b in segment_pairs:
                clipped_3d = clip_segment_to_depth(a, b, frame, depth_min, depth_max)
                if clipped_3d is None:
                    if current:
                        runs.append(current)
                        current = []
                    continue
                p0, p1 = (frame.project(point)[0] for point in clipped_3d)
                clipped_runs = clip_polyline((p0, p1), clip_bounds)
                if not clipped_runs:
                    if current:
                        runs.append(current)
                        current = []
                    continue
                c0, c1 = clipped_runs[0]
                if current and math.hypot(current[-1].x - c0.x, current[-1].y - c0.y) <= 1e-9:
                    current.append(c1)
                else:
                    if current:
                        runs.append(current)
                    current = [c0, c1]
            if current:
                runs.append(current)
            for index, run in enumerate(runs):
                if len(run) < 2:
                    continue
                primitives.append(
                    DrawingPrimitive(
                        id=f"{view_id}:{entity.id}:{role}:{index}",
                        kind="polyline",
                        layer=layer,
                        points=tuple(run),
                        source=source,
                        closed=False,
                        label=_label(entity),
                        metadata=extra_metadata,
                    )
                )

        def add_box(entity: Entity, category: str, role: str, pose: Pose, size: Size3, layer: str) -> None:
            corners = _box_corners(pose, size)
            for index, (a_index, b_index) in enumerate(_BOX_EDGES):
                add_polyline(
                    entity,
                    category,
                    f"{role}-edge-{index}",
                    (corners[a_index], corners[b_index]),
                    layer=layer,
                    extra_metadata=_metadata(size_x_m=size.x, size_y_m=size.y, size_z_m=size.z),
                )

        for space in sorted(self.model.spaces, key=lambda item: item.id):
            if kind == "plan":
                add_polyline(space, "spaces", "footprint", space.footprint.points, layer="space", closed=True)
        for slab in sorted(self.model.slabs, key=lambda item: item.id):
            add_polyline(slab, "slabs", "footprint", slab.footprint.points, layer="slab", closed=(kind != "section"))
        for ceiling in sorted(self.model.ceilings, key=lambda item: item.id):
            add_polyline(ceiling, "ceilings", "footprint", ceiling.footprint.points, layer="ceiling", closed=(kind != "section"))
        for wall in sorted(self.model.walls, key=lambda item: item.id):
            if kind == "plan":
                add_polyline(
                    wall,
                    "walls",
                    "centerline",
                    wall.centerline.points,
                    layer="wall",
                    extra_metadata=_metadata(thickness_m=wall.thickness_m, height_m=wall.height_m),
                )
            else:
                for index, (a, b) in enumerate(zip(wall.centerline.points, wall.centerline.points[1:])):
                    clipped_base = clip_segment_to_depth(a, b, frame, depth_min, depth_max)
                    if clipped_base is None:
                        continue
                    ca, cb = clipped_base
                    top_b = Point3(x=cb.x, y=cb.y, z=cb.z + wall.height_m)
                    top_a = Point3(x=ca.x, y=ca.y, z=ca.z + wall.height_m)
                    add_polyline(
                        wall,
                        "walls",
                        f"face-{index}",
                        (ca, cb, top_b, top_a),
                        layer="wall",
                        closed=True,
                        extra_metadata=_metadata(thickness_m=wall.thickness_m, height_m=wall.height_m),
                    )

        for opening in sorted(self.model.openings, key=lambda item: item.id):
            add_box(opening, "openings", "box", opening.pose, opening.size, "opening")

        for obstacle in sorted(self.model.obstacles, key=lambda item: item.id):
            geometry = obstacle.geometry
            if isinstance(geometry, Box3D):
                add_box(obstacle, "obstacles", "box", geometry.pose, geometry.size, "obstacle")
            elif isinstance(geometry, Polyline3D):
                add_polyline(obstacle, "obstacles", "geometry", geometry.points, layer="obstacle")
            elif isinstance(geometry, Polygon3D):
                add_polyline(obstacle, "obstacles", "geometry", geometry.points, layer="obstacle", closed=True)

        for constraint in sorted(self.model.route_constraints, key=lambda item: item.id):
            geometry = constraint.geometry
            if isinstance(geometry, Box3D):
                add_box(constraint, "route_constraints", "box", geometry.pose, geometry.size, "route-constraint")
            elif isinstance(geometry, Polyline3D):
                add_polyline(constraint, "route_constraints", "geometry", geometry.points, layer="route-constraint")
            elif isinstance(geometry, Polygon3D):
                add_polyline(constraint, "route_constraints", "geometry", geometry.points, layer="route-constraint", closed=True)

        for route in sorted(self.model.routes, key=lambda item: item.id):
            add_polyline(
                route,
                "routes",
                "centerline",
                route.centerline.points,
                layer="route",
                extra_metadata=_metadata(route_type=route.route_type, nominal_diameter_m=route.nominal_diameter_m),
            )

        symbol_entities: tuple[tuple[str, Entity], ...] = tuple(
            [("electrical_equipment", entity) for entity in self.model.electrical_equipment]
            + [("electrical_devices", entity) for entity in self.model.electrical_devices]
            + [("route_fittings", entity) for entity in self.model.route_fittings]
        )
        for category, entity in sorted(symbol_entities, key=lambda pair: pair[1].id):
            if not visibility.allows(category, entity) or not level_matches(entity):
                continue
            pose = entity.pose
            anchor, depth = frame.project(pose.position)
            if depth_min is not None and depth < depth_min - 1e-9:
                continue
            if depth_max is not None and depth > depth_max + 1e-9:
                continue
            if not (clip_bounds.min_x <= anchor.x <= clip_bounds.max_x and clip_bounds.min_y <= anchor.y <= clip_bounds.max_y):
                continue
            source = _source(entity)
            context = SymbolContext(view_id=view_id, view_kind=kind, anchor=anchor, source=source)
            primitives.extend(self.symbol_hook(entity, context))
            annotations.extend(
                self.annotation_hook(
                    entity,
                    AnnotationContext(view_id=view_id, view_kind=kind, anchor=anchor, source=source),
                )
            )

        # Space labels are annotations rather than editable drawing semantics.
        if kind == "plan":
            for space in sorted(self.model.spaces, key=lambda item: item.id):
                if not visibility.allows("spaces", space) or not level_matches(space):
                    continue
                projected = [frame.project(point)[0] for point in space.footprint.points]
                anchor = Point2(
                    sum(point.x for point in projected) / len(projected),
                    sum(point.y for point in projected) / len(projected),
                )
                if clip_bounds.min_x <= anchor.x <= clip_bounds.max_x and clip_bounds.min_y <= anchor.y <= clip_bounds.max_y:
                    annotations.append(
                        Annotation(
                            id=f"{view_id}:{space.id}:label",
                            text=_label(space),
                            position=anchor,
                            source=_source(space),
                        )
                    )

        primitives = sorted(primitives, key=lambda item: (item.layer, item.source.entity_id, item.id))
        annotations = sorted(annotations, key=lambda item: item.id)
        dimensions = self._overall_dimensions(view_id, primitives, clip_bounds)
        return DrawingView(
            id=view_id,
            name=name,
            kind=kind,
            model_id=self.model.model_id,
            projection=frame.metadata(depth_min_m=depth_min, depth_max_m=depth_max),
            clip_bounds=clip_bounds,
            primitives=tuple(primitives),
            annotations=tuple(annotations),
            dimensions=dimensions,
            metadata=metadata,
        )

    @staticmethod
    def _overall_dimensions(
        view_id: str,
        primitives: list[DrawingPrimitive],
        clip_bounds: Bounds2,
    ) -> tuple[Dimension, ...]:
        points = [point for primitive in primitives for point in primitive.points]
        if not points:
            return ()
        min_x, max_x = min(p.x for p in points), max(p.x for p in points)
        min_y, max_y = min(p.y for p in points), max(p.y for p in points)
        source_ids = tuple(sorted({primitive.source.entity_id for primitive in primitives}))
        dimensions: list[Dimension] = []
        if max_x - min_x > 1e-9:
            y = min(max_y + 0.25, clip_bounds.max_y)
            dimensions.append(
                Dimension(
                    id=f"{view_id}:dimension:overall-horizontal",
                    start=Point2(min_x, y),
                    end=Point2(max_x, y),
                    value_m=max_x - min_x,
                    label=f"{max_x - min_x:.3f} m",
                    source_entity_ids=source_ids,
                )
            )
        if max_y - min_y > 1e-9:
            x = min(max_x + 0.25, clip_bounds.max_x)
            dimensions.append(
                Dimension(
                    id=f"{view_id}:dimension:overall-vertical",
                    start=Point2(x, min_y),
                    end=Point2(x, max_y),
                    value_m=max_y - min_y,
                    label=f"{max_y - min_y:.3f} m",
                    source_entity_ids=source_ids,
                )
            )
        return tuple(dimensions)

    def _equipment_schedule(self) -> Schedule:
        columns = (
            ScheduleColumn("id", "ID"), ScheduleColumn("name", "Name"),
            ScheduleColumn("type", "Type"), ScheduleColumn("level", "Level"),
            ScheduleColumn("space", "Space"), ScheduleColumn("system", "System"),
            ScheduleColumn("voltage", "Voltage", "V"), ScheduleColumn("confidence", "Confidence"),
        )
        rows = tuple(
            ScheduleRow(
                source=_source(item),
                values=(
                    item.id, item.name or "", item.equipment_type, item.level_id or "",
                    item.space_id or "", item.system or "", _fmt(item.rated_voltage_v), _fmt(item.confidence),
                ),
            )
            for item in sorted(self.model.electrical_equipment, key=lambda entity: entity.id)
        )
        return Schedule("schedule:electrical-equipment", "Electrical Equipment", self.model.model_id, columns, rows)

    def _device_schedule(self) -> Schedule:
        columns = (
            ScheduleColumn("id", "ID"), ScheduleColumn("name", "Name"),
            ScheduleColumn("type", "Type"), ScheduleColumn("level", "Level"),
            ScheduleColumn("space", "Space"), ScheduleColumn("system", "System"),
            ScheduleColumn("voltage", "Voltage", "V"), ScheduleColumn("confidence", "Confidence"),
        )
        rows = tuple(
            ScheduleRow(
                source=_source(item),
                values=(
                    item.id, item.name or "", item.device_type, item.level_id or "",
                    item.space_id or "", item.system or "", _fmt(item.rated_voltage_v), _fmt(item.confidence),
                ),
            )
            for item in sorted(self.model.electrical_devices, key=lambda entity: entity.id)
        )
        return Schedule("schedule:electrical-devices", "Electrical Devices", self.model.model_id, columns, rows)

    def _opening_schedule(self) -> Schedule:
        columns = (
            ScheduleColumn("id", "ID"), ScheduleColumn("name", "Name"),
            ScheduleColumn("type", "Type"), ScheduleColumn("host", "Host"),
            ScheduleColumn("width", "Width", "m"), ScheduleColumn("height", "Height", "m"),
            ScheduleColumn("confidence", "Confidence"),
        )
        rows = tuple(
            ScheduleRow(
                source=_source(item),
                values=(
                    item.id, item.name or "", item.opening_type, item.host_id,
                    _fmt(item.size.x), _fmt(item.size.z), _fmt(item.confidence),
                ),
            )
            for item in sorted(self.model.openings, key=lambda entity: entity.id)
        )
        return Schedule("schedule:openings", "Openings", self.model.model_id, columns, rows)

    def _circuit_schedule(self) -> Schedule:
        columns = (
            ScheduleColumn("id", "ID"), ScheduleColumn("number", "Circuit"),
            ScheduleColumn("source", "Source Port"), ScheduleColumn("loads", "Load Ports"),
            ScheduleColumn("voltage", "Voltage", "V"), ScheduleColumn("poles", "Poles"),
            ScheduleColumn("phase", "Phase"), ScheduleColumn("load", "Load", "VA"),
            ScheduleColumn("routes", "Routes"),
        )
        rows = tuple(
            ScheduleRow(
                source=_source(item),
                values=(
                    item.id, item.circuit_number or "", item.source_port_id,
                    ",".join(sorted(item.load_port_ids)), _fmt(item.voltage_v), _fmt(item.poles),
                    item.phase or "", _fmt(item.load_va), ",".join(sorted(item.route_ids)),
                ),
            )
            for item in sorted(self.model.circuits, key=lambda entity: entity.id)
        )
        return Schedule("schedule:circuits", "Circuits", self.model.model_id, columns, rows)


def _fmt(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".9g")
    return str(value)
