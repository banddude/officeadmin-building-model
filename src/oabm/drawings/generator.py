from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Sequence

from oabm.model import (
    Box3D,
    BuildingModel,
    Ceiling,
    ElectricalDevice,
    ElectricalEquipment,
    Entity,
    Obstacle,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Route,
    RouteConstraint,
    RouteFitting,
    Slab,
    Space,
    Wall,
)

from .projection import (
    BOX_EDGE_INDICES,
    ProjectionFrame,
    clip_polygon,
    clip_segment_2d,
    distance_2d,
    elevation_frame,
    midpoint,
    oriented_box_corners,
    plan_frame,
    project_polyline_segments,
    section_frame,
    section_intervals,
)
from .schedules import generate_schedule, generate_standard_schedules
from .types import (
    DimensionElement,
    DrawingElement,
    DrawingError,
    DrawingPackage,
    DrawingView,
    ElevationViewSpec,
    LabelElement,
    PlanViewSpec,
    Point2,
    PolylineElement,
    Rect2,
    Schedule,
    SectionViewSpec,
    SymbolElement,
    ViewSpec,
    element_sort_key,
    stable_element_id,
)

_EPS = 1e-9


class SymbolProvider(Protocol):
    def symbol_for(self, entity: Entity) -> str: ...


class AnnotationProvider(Protocol):
    def label_for(self, entity: Entity) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DefaultSymbolProvider:
    def symbol_for(self, entity: Entity) -> str:
        if isinstance(entity, ElectricalEquipment):
            return f"equipment:{entity.equipment_type}"
        if isinstance(entity, ElectricalDevice):
            return f"device:{entity.device_type}"
        if isinstance(entity, RouteFitting):
            return f"fitting:{entity.fitting_type}"
        if isinstance(entity, Opening):
            return f"opening:{entity.opening_type}"
        return entity.__class__.__name__.lower()


@dataclass(frozen=True, slots=True)
class DefaultAnnotationProvider:
    include_id_fallback: bool = True

    def label_for(self, entity: Entity) -> str | None:
        if entity.name:
            return entity.name
        if self.include_id_fallback and isinstance(
            entity, (Space, Opening, ElectricalEquipment, ElectricalDevice, Route)
        ):
            return entity.id
        return None


def generate_view(
    model: BuildingModel,
    spec: ViewSpec,
    *,
    symbol_provider: SymbolProvider | None = None,
    annotation_provider: AnnotationProvider | None = None,
) -> DrawingView:
    symbols = symbol_provider or DefaultSymbolProvider()
    annotations = annotation_provider or DefaultAnnotationProvider()
    if isinstance(spec, PlanViewSpec):
        return _generate_plan(model, spec, symbols, annotations)
    if isinstance(spec, ElevationViewSpec):
        return _generate_elevation(model, spec, symbols, annotations)
    if isinstance(spec, SectionViewSpec):
        return _generate_section(model, spec, symbols, annotations)
    raise DrawingError(f"unsupported view spec {type(spec).__name__}")


def generate_package(
    model: BuildingModel,
    *,
    view_specs: Sequence[ViewSpec] = (),
    schedule_types: Sequence[str] = (),
    schedule_level_id: str | None = None,
    include_standard_schedules: bool = False,
    symbol_provider: SymbolProvider | None = None,
    annotation_provider: AnnotationProvider | None = None,
) -> DrawingPackage:
    views = tuple(
        generate_view(
            model,
            spec,
            symbol_provider=symbol_provider,
            annotation_provider=annotation_provider,
        )
        for spec in view_specs
    )
    if include_standard_schedules:
        schedules = generate_standard_schedules(model, level_id=schedule_level_id)
    else:
        schedules = tuple(
            generate_schedule(model, schedule_type, level_id=schedule_level_id)
            for schedule_type in schedule_types
        )
    return DrawingPackage(
        source_model_id=model.model_id,
        source_schema_version=model.schema_version,
        views=views,
        schedules=schedules,
    )


def generate_standard_package(
    model: BuildingModel,
    *,
    section_specs: Sequence[SectionViewSpec] = (),
    crop: Rect2 | None = None,
    include_schedules: bool = True,
) -> DrawingPackage:
    """Generate deterministic baseline plans/elevations plus caller-defined sections.

    Sections require an explicit cut location, so this helper never invents one.
    """
    view_specs: list[ViewSpec] = []
    for level in sorted(model.levels, key=lambda item: (item.elevation_m, item.id)):
        view_specs.append(
            PlanViewSpec(
                view_id=f"plan:{level.id}",
                level_id=level.id,
                title=f"{level.name or level.id} Plan",
                crop=crop,
            )
        )
    for direction in ("north", "south", "east", "west"):
        view_specs.append(
            ElevationViewSpec(
                view_id=f"elevation:{direction}",
                direction=direction,
                title=f"{direction.title()} Elevation",
                crop=crop,
            )
        )
    view_specs.extend(section_specs)
    return generate_package(
        model,
        view_specs=view_specs,
        include_standard_schedules=include_schedules,
    )


def _level(model: BuildingModel, level_id: str):
    for level in model.levels:
        if level.id == level_id:
            return level
    raise DrawingError(f"view references unknown level {level_id!r}")


def _validated_level_ids(model: BuildingModel, requested: tuple[str, ...]) -> set[str] | None:
    if not requested:
        return None
    known = {level.id for level in model.levels}
    missing = sorted(set(requested) - known)
    if missing:
        raise DrawingError(f"view references unknown level(s): {', '.join(missing)}")
    return set(requested)


def _plan_depth_range(model: BuildingModel, spec: PlanViewSpec) -> tuple[float, float]:
    level = _level(model, spec.level_id)
    min_depth = -spec.view_range_below_m
    if spec.view_range_above_m is not None:
        return min_depth, spec.view_range_above_m
    if level.height_m is not None:
        return min_depth, level.height_m
    higher = sorted(
        other.elevation_m - level.elevation_m
        for other in model.levels
        if other.elevation_m > level.elevation_m + _EPS
    )
    if higher:
        return min_depth, higher[0]
    # No story height is known. Use a geometry-driven unbounded upper range rather
    # than inventing a story height; callers can provide view_range_above_m to constrain it.
    return min_depth, math.inf


def _source_kwargs(entity: Entity) -> dict[str, object]:
    return {"confidence": entity.confidence, "provenance": entity.provenance}


def _plan_entity_on_level(entity: Entity, level_id: str, min_z: float, max_z: float) -> bool:
    explicit = getattr(entity, "level_id", None)
    if explicit is not None:
        return explicit == level_id
    pose = getattr(entity, "pose", None)
    if pose is None:
        return False
    return min_z - _EPS <= pose.position.z <= max_z + _EPS


def _project_polygon_element(
    *,
    frame: ProjectionFrame,
    points: tuple[Point3, ...],
    crop: Rect2 | None,
    view_id: str,
    entity: Entity,
    layer: str,
    role: str,
    part: int = 0,
    line_width_m: float | None = None,
) -> PolylineElement | None:
    projected = tuple(frame.project(point).point for point in points)
    clipped = clip_polygon(projected, crop)
    if len(clipped) < 3:
        return None
    return PolylineElement(
        element_id=stable_element_id(view_id, role, (entity.id,), part),
        layer=layer,
        role=role,
        source_ids=(entity.id,),
        points=clipped,
        closed=True,
        line_width_m=line_width_m,
        **_source_kwargs(entity),
    )


def _add_projected_segments(
    elements: list[DrawingElement],
    *,
    frame: ProjectionFrame,
    points: tuple[Point3, ...],
    crop: Rect2 | None,
    min_depth: float | None,
    max_depth: float | None,
    view_id: str,
    entity: Entity,
    layer: str,
    role: str,
    line_width_m: float | None = None,
    part_offset: int = 0,
) -> int:
    segments = project_polyline_segments(
        frame,
        points,
        min_depth=min_depth,
        max_depth=max_depth,
        crop=crop,
    )
    for index, (start, end) in enumerate(segments, start=part_offset):
        elements.append(
            PolylineElement(
                element_id=stable_element_id(view_id, role, (entity.id,), index),
                layer=layer,
                role=role,
                source_ids=(entity.id,),
                points=(start, end),
                line_width_m=line_width_m,
                **_source_kwargs(entity),
            )
        )
    return part_offset + len(segments)


def _box_hull(
    frame: ProjectionFrame, pose, size, crop: Rect2 | None
) -> tuple[Point2, ...]:
    from .projection import convex_hull

    hull = convex_hull(frame.project(point).point for point in oriented_box_corners(pose, size))
    return clip_polygon(hull, crop)


def _add_box_edges(
    elements: list[DrawingElement],
    *,
    frame: ProjectionFrame,
    entity: Entity,
    pose,
    size,
    crop: Rect2 | None,
    min_depth: float | None,
    max_depth: float | None,
    view_id: str,
    layer: str,
    role: str,
) -> None:
    corners = oriented_box_corners(pose, size)
    part = 0
    for left, right in BOX_EDGE_INDICES:
        part = _add_projected_segments(
            elements,
            frame=frame,
            points=(corners[left], corners[right]),
            crop=crop,
            min_depth=min_depth,
            max_depth=max_depth,
            view_id=view_id,
            entity=entity,
            layer=layer,
            role=role,
            part_offset=part,
        )


def _label(
    elements: list[DrawingElement],
    *,
    view_id: str,
    entity: Entity,
    point: Point2,
    text: str | None,
    layer: str,
    crop: Rect2 | None,
) -> None:
    if not text or not _point_in_crop(point, crop):
        return
    elements.append(
        LabelElement(
            element_id=stable_element_id(view_id, "annotation", (entity.id,)),
            layer=layer,
            role="annotation",
            source_ids=(entity.id,),
            point=point,
            text=text,
        )
    )


def _point_in_crop(point: Point2, crop: Rect2 | None) -> bool:
    return crop is None or (
        crop.min_x - _EPS <= point.x <= crop.max_x + _EPS
        and crop.min_y - _EPS <= point.y <= crop.max_y + _EPS
    )


def _dimension(
    elements: list[DrawingElement],
    *,
    view_id: str,
    entity: Entity,
    start: Point2,
    end: Point2,
    text: str,
    crop: Rect2 | None,
    role: str,
) -> None:
    clipped = clip_segment_2d(start, end, crop)
    if clipped is None or clipped[0] == clipped[1]:
        return
    elements.append(
        DimensionElement(
            element_id=stable_element_id(view_id, role, (entity.id,)),
            layer="dimensions",
            role=role,
            source_ids=(entity.id,),
            start=clipped[0],
            end=clipped[1],
            text=text,
        )
    )


def _geometry_plan(
    elements: list[DrawingElement], frame: ProjectionFrame, entity: Entity, geometry, spec: PlanViewSpec
) -> None:
    if isinstance(geometry, Polygon3D):
        item = _project_polygon_element(
            frame=frame,
            points=geometry.points,
            crop=spec.crop,
            view_id=spec.view_id,
            entity=entity,
            layer="reference",
            role="reference-polygon",
        )
        if item:
            elements.append(item)
    elif isinstance(geometry, Polyline3D):
        _add_projected_segments(
            elements,
            frame=frame,
            points=geometry.points,
            crop=spec.crop,
            min_depth=None,
            max_depth=None,
            view_id=spec.view_id,
            entity=entity,
            layer="reference",
            role="reference-polyline",
        )
    elif isinstance(geometry, Box3D):
        hull = _box_hull(frame, geometry.pose, geometry.size, spec.crop)
        if len(hull) >= 3:
            elements.append(
                PolylineElement(
                    element_id=stable_element_id(spec.view_id, "reference-box", (entity.id,)),
                    layer="reference",
                    role="reference-box",
                    source_ids=(entity.id,),
                    points=hull,
                    closed=True,
                    **_source_kwargs(entity),
                )
            )


def _generate_plan(
    model: BuildingModel,
    spec: PlanViewSpec,
    symbols: SymbolProvider,
    annotations: AnnotationProvider,
) -> DrawingView:
    level = _level(model, spec.level_id)
    frame = plan_frame(level.elevation_m)
    min_depth, max_depth = _plan_depth_range(model, spec)
    min_z = level.elevation_m + min_depth
    max_z = level.elevation_m + max_depth
    elements: list[DrawingElement] = []

    if spec.visibility.spaces:
        for item in sorted((x for x in model.spaces if x.level_id == level.id), key=lambda x: x.id):
            poly = _project_polygon_element(
                frame=frame, points=item.footprint.points, crop=spec.crop,
                view_id=spec.view_id, entity=item, layer="architecture-space", role="space-boundary",
            )
            if poly:
                elements.append(poly)
                if spec.visibility.annotations:
                    _label(
                        elements, view_id=spec.view_id, entity=item,
                        point=midpoint(poly.points), text=annotations.label_for(item),
                        layer="annotations", crop=spec.crop,
                    )

    if spec.visibility.slabs:
        for item in sorted((x for x in model.slabs if x.level_id == level.id), key=lambda x: x.id):
            poly = _project_polygon_element(
                frame=frame, points=item.footprint.points, crop=spec.crop,
                view_id=spec.view_id, entity=item, layer="architecture-slab", role="slab-boundary",
            )
            if poly:
                elements.append(poly)

    if spec.visibility.ceilings:
        for item in sorted((x for x in model.ceilings if x.level_id == level.id), key=lambda x: x.id):
            poly = _project_polygon_element(
                frame=frame, points=item.footprint.points, crop=spec.crop,
                view_id=spec.view_id, entity=item, layer="architecture-ceiling", role="ceiling-boundary",
            )
            if poly:
                elements.append(poly)

    if spec.visibility.walls:
        for item in sorted((x for x in model.walls if x.level_id == level.id), key=lambda x: x.id):
            _add_projected_segments(
                elements,
                frame=frame,
                points=item.centerline.points,
                crop=spec.crop,
                min_depth=None,
                max_depth=None,
                view_id=spec.view_id,
                entity=item,
                layer="architecture-wall",
                role="wall-centerline",
                line_width_m=item.thickness_m,
            )
            if spec.visibility.dimensions:
                start = frame.project(item.centerline.points[0]).point
                end = frame.project(item.centerline.points[-1]).point
                _dimension(
                    elements,
                    view_id=spec.view_id,
                    entity=item,
                    start=start,
                    end=end,
                    text=f"{distance_2d(start, end):.3f} m",
                    crop=spec.crop,
                    role="wall-dimension",
                )

    host_levels = {
        entity.id: entity.level_id for entity in (*model.walls, *model.slabs, *model.ceilings)
    }
    if spec.visibility.openings:
        for item in sorted(model.openings, key=lambda x: x.id):
            if host_levels.get(item.host_id) != level.id:
                continue
            hull = _box_hull(frame, item.pose, item.size, spec.crop)
            if len(hull) >= 3:
                elements.append(
                    PolylineElement(
                        element_id=stable_element_id(spec.view_id, "opening-outline", (item.id,)),
                        layer="architecture-opening",
                        role="opening-outline",
                        source_ids=(item.id,),
                        points=hull,
                        closed=True,
                        **_source_kwargs(item),
                    )
                )
                if spec.visibility.annotations:
                    _label(
                        elements, view_id=spec.view_id, entity=item, point=midpoint(hull),
                        text=annotations.label_for(item), layer="annotations", crop=spec.crop,
                    )

    for collection, enabled, layer in (
        (model.electrical_equipment, spec.visibility.electrical_equipment, "electrical-equipment"),
        (model.electrical_devices, spec.visibility.electrical_devices, "electrical-device"),
    ):
        if not enabled:
            continue
        for item in sorted(collection, key=lambda x: x.id):
            if not _plan_entity_on_level(item, level.id, min_z, max_z):
                continue
            projected = frame.project(item.pose.position)
            if projected.depth < min_depth - _EPS or projected.depth > max_depth + _EPS:
                continue
            if not _point_in_crop(projected.point, spec.crop):
                continue
            elements.append(
                SymbolElement(
                    element_id=stable_element_id(spec.view_id, "symbol", (item.id,)),
                    layer=layer,
                    role="symbol",
                    source_ids=(item.id,),
                    point=projected.point,
                    symbol=symbols.symbol_for(item),
                    **_source_kwargs(item),
                )
            )
            if spec.visibility.annotations:
                _label(
                    elements, view_id=spec.view_id, entity=item, point=projected.point,
                    text=annotations.label_for(item), layer="annotations", crop=spec.crop,
                )

    if spec.visibility.routes:
        for item in sorted(model.routes, key=lambda x: x.id):
            _add_projected_segments(
                elements,
                frame=frame,
                points=item.centerline.points,
                crop=spec.crop,
                min_depth=min_depth,
                max_depth=max_depth,
                view_id=spec.view_id,
                entity=item,
                layer="electrical-route",
                role="route-centerline",
                line_width_m=item.nominal_diameter_m,
            )
            if spec.visibility.annotations:
                projected_points = [frame.project(point) for point in item.centerline.points]
                visible = [p.point for p in projected_points if min_depth <= p.depth <= max_depth]
                if visible:
                    _label(
                        elements, view_id=spec.view_id, entity=item, point=midpoint(visible),
                        text=annotations.label_for(item), layer="annotations", crop=spec.crop,
                    )

    if spec.visibility.route_fittings:
        for item in sorted(model.route_fittings, key=lambda x: x.id):
            projected = frame.project(item.pose.position)
            if min_depth - _EPS <= projected.depth <= max_depth + _EPS and _point_in_crop(projected.point, spec.crop):
                elements.append(
                    SymbolElement(
                        element_id=stable_element_id(spec.view_id, "fitting-symbol", (item.id,)),
                        layer="electrical-fitting",
                        role="fitting-symbol",
                        source_ids=(item.id,),
                        point=projected.point,
                        symbol=symbols.symbol_for(item),
                        **_source_kwargs(item),
                    )
                )

    if spec.visibility.obstacles:
        for item in sorted(model.obstacles, key=lambda x: x.id):
            if item.level_id is None or item.level_id == level.id:
                _geometry_plan(elements, frame, item, item.geometry, spec)
    if spec.visibility.route_constraints:
        for item in sorted(model.route_constraints, key=lambda x: x.id):
            if item.level_id is None or item.level_id == level.id:
                _geometry_plan(elements, frame, item, item.geometry, spec)

    projection = {
        "type": "orthographic-plan",
        "frame": frame.to_dict(),
        "level_id": level.id,
        "level_elevation_m": level.elevation_m,
        "depth_range_m": {
            "min": min_depth,
            "max": None if math.isinf(max_depth) else max_depth,
        },
        "crop": None if spec.crop is None else spec.crop.to_dict(),
    }
    return DrawingView(
        view_id=spec.view_id,
        kind="plan",
        title=spec.title or f"{level.name or level.id} Plan",
        source_model_id=model.model_id,
        source_schema_version=model.schema_version,
        projection=projection,
        visibility=spec.visibility,
        elements=tuple(sorted(elements, key=element_sort_key)),
        metadata={"canonical_level_id": level.id},
    )


def _closed_edges(points: tuple[Point3, ...]) -> tuple[tuple[Point3, Point3], ...]:
    return tuple(zip(points, points[1:] + points[:1]))


def _extruded_polygon_edges(points: tuple[Point3, ...], height_m: float) -> tuple[tuple[Point3, Point3], ...]:
    top = tuple(Point3(x=p.x, y=p.y, z=p.z + height_m) for p in points)
    edges = list(_closed_edges(points)) + list(_closed_edges(top))
    edges.extend(zip(points, top))
    return tuple(edges)


def _wall_edges(item: Wall) -> tuple[tuple[Point3, Point3], ...]:
    base = item.centerline.points
    top = tuple(Point3(x=p.x, y=p.y, z=p.z + item.height_m) for p in base)
    edges = list(zip(base, base[1:])) + list(zip(top, top[1:]))
    edges.extend((base[index], top[index]) for index in range(len(base)))
    return tuple(edges)


def _footprint_slab_edges(points: tuple[Point3, ...], thickness_m: float | None) -> tuple[tuple[Point3, Point3], ...]:
    edges = list(_closed_edges(points))
    if thickness_m is not None:
        lower = tuple(Point3(x=p.x, y=p.y, z=p.z - thickness_m) for p in points)
        edges.extend(_closed_edges(lower))
        edges.extend(zip(points, lower))
    return tuple(edges)


def _add_edge_set(
    elements: list[DrawingElement],
    *,
    frame: ProjectionFrame,
    edges: tuple[tuple[Point3, Point3], ...],
    crop: Rect2 | None,
    min_depth: float | None,
    max_depth: float | None,
    view_id: str,
    entity: Entity,
    layer: str,
    role: str,
    line_width_m: float | None = None,
) -> None:
    for index, (start, end) in enumerate(edges):
        _add_projected_segments(
            elements,
            frame=frame,
            points=(start, end),
            crop=crop,
            min_depth=min_depth,
            max_depth=max_depth,
            view_id=view_id,
            entity=entity,
            layer=layer,
            role=role,
            line_width_m=line_width_m,
            part_offset=index,
        )


def _entity_levels_allowed(entity: Entity, level_ids: set[str] | None, host_levels: dict[str, str]) -> bool:
    if level_ids is None:
        return True
    level_id = getattr(entity, "level_id", None)
    if level_id is None and isinstance(entity, Opening):
        level_id = host_levels.get(entity.host_id)
    return level_id is None or level_id in level_ids


def _depth_point_visible(frame: ProjectionFrame, point: Point3, min_depth: float | None, max_depth: float | None) -> bool:
    depth = frame.project(point).depth
    return (min_depth is None or depth >= min_depth - _EPS) and (max_depth is None or depth <= max_depth + _EPS)


def _add_elevation_like_geometry(
    elements: list[DrawingElement],
    *,
    model: BuildingModel,
    frame: ProjectionFrame,
    view_id: str,
    crop: Rect2 | None,
    min_depth: float | None,
    max_depth: float | None,
    level_ids: set[str] | None,
    visibility,
    symbols: SymbolProvider,
    annotations: AnnotationProvider,
) -> None:
    host_levels = {item.id: item.level_id for item in (*model.walls, *model.slabs, *model.ceilings)}

    if visibility.spaces:
        for item in sorted(model.spaces, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            edges = _closed_edges(item.footprint.points)
            if item.height_m is not None:
                edges = _extruded_polygon_edges(item.footprint.points, item.height_m)
            _add_edge_set(
                elements, frame=frame, edges=edges, crop=crop, min_depth=min_depth,
                max_depth=max_depth, view_id=view_id, entity=item,
                layer="architecture-space", role="space-edge",
            )

    if visibility.walls:
        for item in sorted(model.walls, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            _add_edge_set(
                elements, frame=frame, edges=_wall_edges(item), crop=crop,
                min_depth=min_depth, max_depth=max_depth, view_id=view_id, entity=item,
                layer="architecture-wall", role="wall-edge", line_width_m=item.thickness_m,
            )

    if visibility.slabs:
        for item in sorted(model.slabs, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            _add_edge_set(
                elements, frame=frame, edges=_footprint_slab_edges(item.footprint.points, item.thickness_m),
                crop=crop, min_depth=min_depth, max_depth=max_depth, view_id=view_id, entity=item,
                layer="architecture-slab", role="slab-edge",
            )

    if visibility.ceilings:
        for item in sorted(model.ceilings, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            _add_edge_set(
                elements, frame=frame, edges=_footprint_slab_edges(item.footprint.points, item.thickness_m),
                crop=crop, min_depth=min_depth, max_depth=max_depth, view_id=view_id, entity=item,
                layer="architecture-ceiling", role="ceiling-edge",
            )

    if visibility.openings:
        for item in sorted(model.openings, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            _add_box_edges(
                elements, frame=frame, entity=item, pose=item.pose, size=item.size, crop=crop,
                min_depth=min_depth, max_depth=max_depth, view_id=view_id,
                layer="architecture-opening", role="opening-edge",
            )
            if visibility.dimensions and _depth_point_visible(frame, item.pose.position, min_depth, max_depth):
                corners = [frame.project(p).point for p in oriented_box_corners(item.pose, item.size)]
                low, high = min(p.y for p in corners), max(p.y for p in corners)
                x = min(p.x for p in corners)
                _dimension(
                    elements, view_id=view_id, entity=item,
                    start=Point2(x=x, y=low), end=Point2(x=x, y=high),
                    text=f"{item.size.z:.3f} m", crop=crop, role="opening-height-dimension",
                )

    for collection, enabled, layer in (
        (model.electrical_equipment, visibility.electrical_equipment, "electrical-equipment"),
        (model.electrical_devices, visibility.electrical_devices, "electrical-device"),
    ):
        if not enabled:
            continue
        for item in sorted(collection, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            if item.size is not None:
                _add_box_edges(
                    elements, frame=frame, entity=item, pose=item.pose, size=item.size, crop=crop,
                    min_depth=min_depth, max_depth=max_depth, view_id=view_id,
                    layer=layer, role="equipment-edge",
                )
            projected = frame.project(item.pose.position)
            if (
                (min_depth is None or projected.depth >= min_depth - _EPS)
                and (max_depth is None or projected.depth <= max_depth + _EPS)
                and _point_in_crop(projected.point, crop)
            ):
                elements.append(
                    SymbolElement(
                        element_id=stable_element_id(view_id, "symbol", (item.id,)),
                        layer=layer, role="symbol", source_ids=(item.id,), point=projected.point,
                        symbol=symbols.symbol_for(item), **_source_kwargs(item),
                    )
                )
                if visibility.annotations:
                    _label(
                        elements, view_id=view_id, entity=item, point=projected.point,
                        text=annotations.label_for(item), layer="annotations", crop=crop,
                    )

    if visibility.routes:
        for item in sorted(model.routes, key=lambda x: x.id):
            _add_projected_segments(
                elements, frame=frame, points=item.centerline.points, crop=crop,
                min_depth=min_depth, max_depth=max_depth, view_id=view_id, entity=item,
                layer="electrical-route", role="route-centerline", line_width_m=item.nominal_diameter_m,
            )

    if visibility.route_fittings:
        for item in sorted(model.route_fittings, key=lambda x: x.id):
            projected = frame.project(item.pose.position)
            if (
                (min_depth is None or projected.depth >= min_depth - _EPS)
                and (max_depth is None or projected.depth <= max_depth + _EPS)
                and _point_in_crop(projected.point, crop)
            ):
                elements.append(
                    SymbolElement(
                        element_id=stable_element_id(view_id, "fitting-symbol", (item.id,)),
                        layer="electrical-fitting", role="fitting-symbol", source_ids=(item.id,),
                        point=projected.point, symbol=symbols.symbol_for(item), **_source_kwargs(item),
                    )
                )


def _generate_elevation(
    model: BuildingModel,
    spec: ElevationViewSpec,
    symbols: SymbolProvider,
    annotations: AnnotationProvider,
) -> DrawingView:
    frame = elevation_frame(spec.direction)
    level_ids = _validated_level_ids(model, spec.level_ids)
    elements: list[DrawingElement] = []
    _add_elevation_like_geometry(
        elements,
        model=model,
        frame=frame,
        view_id=spec.view_id,
        crop=spec.crop,
        min_depth=spec.min_depth_m,
        max_depth=spec.max_depth_m,
        level_ids=level_ids,
        visibility=spec.visibility,
        symbols=symbols,
        annotations=annotations,
    )
    return DrawingView(
        view_id=spec.view_id,
        kind="elevation",
        title=spec.title or f"{spec.direction.title()} Elevation",
        source_model_id=model.model_id,
        source_schema_version=model.schema_version,
        projection={
            "type": "orthographic-elevation",
            "direction": spec.direction,
            "frame": frame.to_dict(),
            "level_ids": list(spec.level_ids),
            "depth_range_m": {"min": spec.min_depth_m, "max": spec.max_depth_m},
            "crop": None if spec.crop is None else spec.crop.to_dict(),
        },
        visibility=spec.visibility,
        elements=tuple(sorted(elements, key=element_sort_key)),
    )


def _segment_plane_hit(a: Point3, b: Point3, axis: str, offset_m: float) -> Point3 | None:
    va = (a.x if axis == "x" else a.y) - offset_m
    vb = (b.x if axis == "x" else b.y) - offset_m
    if abs(va) <= _EPS and abs(vb) <= _EPS:
        return None
    if va * vb > _EPS:
        return None
    denom = va - vb
    if abs(denom) <= _EPS:
        return None
    t = va / denom
    if t < -_EPS or t > 1 + _EPS:
        return None
    t = min(1.0, max(0.0, t))
    return Point3(
        x=a.x + (b.x - a.x) * t,
        y=a.y + (b.y - a.y) * t,
        z=a.z + (b.z - a.z) * t,
    )


def _add_section_interval_rectangle(
    elements: list[DrawingElement],
    *,
    view_id: str,
    entity: Entity,
    start: Point2,
    end: Point2,
    height_m: float | None,
    crop: Rect2 | None,
    layer: str,
    role: str,
    thickness_m: float | None = None,
) -> None:
    if height_m is None:
        clipped = clip_segment_2d(start, end, crop)
        if clipped is not None and clipped[0] != clipped[1]:
            elements.append(
                PolylineElement(
                    element_id=stable_element_id(view_id, role, (entity.id,)),
                    layer=layer, role=role, source_ids=(entity.id,), points=clipped,
                    line_width_m=thickness_m, **_source_kwargs(entity),
                )
            )
        return
    top_start = Point2(x=start.x, y=start.y + height_m)
    top_end = Point2(x=end.x, y=end.y + height_m)
    edges = ((start, end), (top_start, top_end), (start, top_start), (end, top_end))
    for index, (left, right) in enumerate(edges):
        clipped = clip_segment_2d(left, right, crop)
        if clipped is None or clipped[0] == clipped[1]:
            continue
        elements.append(
            PolylineElement(
                element_id=stable_element_id(view_id, role, (entity.id,), index),
                layer=layer, role=role, source_ids=(entity.id,), points=clipped,
                line_width_m=thickness_m, **_source_kwargs(entity),
            )
        )


def _generate_section(
    model: BuildingModel,
    spec: SectionViewSpec,
    symbols: SymbolProvider,
    annotations: AnnotationProvider,
) -> DrawingView:
    frame = section_frame(spec.axis, spec.offset_m, spec.direction)
    level_ids = _validated_level_ids(model, spec.level_ids)
    half_depth = spec.depth_m / 2
    elements: list[DrawingElement] = []
    host_levels = {item.id: item.level_id for item in (*model.walls, *model.slabs, *model.ceilings)}

    if spec.visibility.spaces:
        for item in sorted(model.spaces, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            for part, (left, right) in enumerate(section_intervals(frame, item.footprint.points, spec.axis, spec.offset_m)):
                _add_section_interval_rectangle(
                    elements, view_id=spec.view_id, entity=item,
                    start=left.point, end=right.point, height_m=item.height_m, crop=spec.crop,
                    layer="architecture-space", role=f"space-cut-{part}",
                )

    if spec.visibility.walls:
        for item in sorted(model.walls, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            for part, (a, b) in enumerate(zip(item.centerline.points, item.centerline.points[1:])):
                hit = _segment_plane_hit(a, b, spec.axis, spec.offset_m)
                if hit is None:
                    # A wall segment that lies within the finite section slice is shown in projection.
                    _add_projected_segments(
                        elements, frame=frame, points=(a, b), crop=spec.crop,
                        min_depth=-half_depth, max_depth=half_depth, view_id=spec.view_id,
                        entity=item, layer="architecture-wall", role="wall-in-plane",
                        line_width_m=item.thickness_m, part_offset=part,
                    )
                    continue
                base = frame.project(hit).point
                top = frame.project(Point3(x=hit.x, y=hit.y, z=hit.z + item.height_m)).point
                _dimensionless = clip_segment_2d(base, top, spec.crop)
                if _dimensionless is not None and _dimensionless[0] != _dimensionless[1]:
                    elements.append(
                        PolylineElement(
                            element_id=stable_element_id(spec.view_id, "wall-cut", (item.id,), part),
                            layer="architecture-wall", role="wall-cut", source_ids=(item.id,),
                            points=_dimensionless, line_width_m=item.thickness_m, **_source_kwargs(item),
                        )
                    )

    for collection, enabled, layer, thickness_direction in (
        (model.slabs, spec.visibility.slabs, "architecture-slab", -1),
        (model.ceilings, spec.visibility.ceilings, "architecture-ceiling", -1),
    ):
        if not enabled:
            continue
        for item in sorted(collection, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            for part, (left, right) in enumerate(section_intervals(frame, item.footprint.points, spec.axis, spec.offset_m)):
                thickness = getattr(item, "thickness_m", None)
                if thickness is None:
                    _add_section_interval_rectangle(
                        elements, view_id=spec.view_id, entity=item, start=left.point, end=right.point,
                        height_m=None, crop=spec.crop, layer=layer, role=f"surface-cut-{part}",
                    )
                else:
                    shifted_left = Point2(x=left.point.x, y=left.point.y + thickness_direction * thickness)
                    shifted_right = Point2(x=right.point.x, y=right.point.y + thickness_direction * thickness)
                    edges = (
                        (left.point, right.point),
                        (shifted_left, shifted_right),
                        (left.point, shifted_left),
                        (right.point, shifted_right),
                    )
                    for edge_index, (a, b) in enumerate(edges):
                        clipped = clip_segment_2d(a, b, spec.crop)
                        if clipped is not None and clipped[0] != clipped[1]:
                            elements.append(
                                PolylineElement(
                                    element_id=stable_element_id(
                                        spec.view_id, f"surface-cut-{part}", (item.id,), edge_index
                                    ),
                                    layer=layer, role="surface-cut", source_ids=(item.id,),
                                    points=clipped, **_source_kwargs(item),
                                )
                            )

    # Openings/electrical/routes reuse the same finite-slice orthographic projection.
    section_visibility = spec.visibility
    if section_visibility.openings:
        for item in sorted(model.openings, key=lambda x: x.id):
            if _entity_levels_allowed(item, level_ids, host_levels):
                _add_box_edges(
                    elements, frame=frame, entity=item, pose=item.pose, size=item.size,
                    crop=spec.crop, min_depth=-half_depth, max_depth=half_depth,
                    view_id=spec.view_id, layer="architecture-opening", role="opening-cut-edge",
                )
    for collection, enabled, layer in (
        (model.electrical_equipment, section_visibility.electrical_equipment, "electrical-equipment"),
        (model.electrical_devices, section_visibility.electrical_devices, "electrical-device"),
    ):
        if not enabled:
            continue
        for item in sorted(collection, key=lambda x: x.id):
            if not _entity_levels_allowed(item, level_ids, host_levels):
                continue
            projected = frame.project(item.pose.position)
            if -half_depth - _EPS <= projected.depth <= half_depth + _EPS:
                if item.size is not None:
                    _add_box_edges(
                        elements, frame=frame, entity=item, pose=item.pose, size=item.size,
                        crop=spec.crop, min_depth=-half_depth, max_depth=half_depth,
                        view_id=spec.view_id, layer=layer, role="equipment-cut-edge",
                    )
                if _point_in_crop(projected.point, spec.crop):
                    elements.append(
                        SymbolElement(
                            element_id=stable_element_id(spec.view_id, "symbol", (item.id,)),
                            layer=layer, role="symbol", source_ids=(item.id,), point=projected.point,
                            symbol=symbols.symbol_for(item), **_source_kwargs(item),
                        )
                    )
                    if section_visibility.annotations:
                        _label(
                            elements, view_id=spec.view_id, entity=item, point=projected.point,
                            text=annotations.label_for(item), layer="annotations", crop=spec.crop,
                        )

    if section_visibility.routes:
        for item in sorted(model.routes, key=lambda x: x.id):
            _add_projected_segments(
                elements, frame=frame, points=item.centerline.points, crop=spec.crop,
                min_depth=-half_depth, max_depth=half_depth, view_id=spec.view_id, entity=item,
                layer="electrical-route", role="route-cut", line_width_m=item.nominal_diameter_m,
            )
            # A segment normal to the section plane collapses to a single 2D point.
            # Preserve that real model intersection explicitly rather than dropping the route.
            crossing_points: list[Point2] = []
            for a, b in zip(item.centerline.points, item.centerline.points[1:]):
                hit = _segment_plane_hit(a, b, spec.axis, spec.offset_m)
                if hit is None:
                    continue
                point = frame.project(hit).point
                if point not in crossing_points and _point_in_crop(point, spec.crop):
                    crossing_points.append(point)
            for part, point in enumerate(crossing_points):
                elements.append(
                    SymbolElement(
                        element_id=stable_element_id(
                            spec.view_id, "route-cut-crossing", (item.id,), part
                        ),
                        layer="electrical-route",
                        role="route-cut",
                        source_ids=(item.id,),
                        point=point,
                        symbol=f"route:{item.route_type}",
                        **_source_kwargs(item),
                    )
                )
    if section_visibility.route_fittings:
        for item in sorted(model.route_fittings, key=lambda x: x.id):
            projected = frame.project(item.pose.position)
            if -half_depth - _EPS <= projected.depth <= half_depth + _EPS and _point_in_crop(projected.point, spec.crop):
                elements.append(
                    SymbolElement(
                        element_id=stable_element_id(spec.view_id, "fitting-symbol", (item.id,)),
                        layer="electrical-fitting", role="fitting-symbol", source_ids=(item.id,),
                        point=projected.point, symbol=symbols.symbol_for(item), **_source_kwargs(item),
                    )
                )

    if section_visibility.obstacles or section_visibility.route_constraints:
        for collection, enabled in (
            (model.obstacles, section_visibility.obstacles),
            (model.route_constraints, section_visibility.route_constraints),
        ):
            if not enabled:
                continue
            for item in sorted(collection, key=lambda x: x.id):
                if not _entity_levels_allowed(item, level_ids, host_levels):
                    continue
                geometry = item.geometry
                if isinstance(geometry, Box3D):
                    _add_box_edges(
                        elements, frame=frame, entity=item, pose=geometry.pose, size=geometry.size,
                        crop=spec.crop, min_depth=-half_depth, max_depth=half_depth,
                        view_id=spec.view_id, layer="reference", role="reference-cut",
                    )
                elif isinstance(geometry, Polyline3D):
                    _add_projected_segments(
                        elements, frame=frame, points=geometry.points, crop=spec.crop,
                        min_depth=-half_depth, max_depth=half_depth, view_id=spec.view_id,
                        entity=item, layer="reference", role="reference-cut",
                    )
                elif isinstance(geometry, Polygon3D):
                    for part, (left, right) in enumerate(section_intervals(frame, geometry.points, spec.axis, spec.offset_m)):
                        clipped = clip_segment_2d(left.point, right.point, spec.crop)
                        if clipped is not None and clipped[0] != clipped[1]:
                            elements.append(
                                PolylineElement(
                                    element_id=stable_element_id(spec.view_id, "reference-cut", (item.id,), part),
                                    layer="reference", role="reference-cut", source_ids=(item.id,),
                                    points=clipped, **_source_kwargs(item),
                                )
                            )

    return DrawingView(
        view_id=spec.view_id,
        kind="section",
        title=spec.title or f"Section {spec.axis.upper()}={spec.offset_m:g}",
        source_model_id=model.model_id,
        source_schema_version=model.schema_version,
        projection={
            "type": "orthographic-section",
            "axis": spec.axis,
            "offset_m": spec.offset_m,
            "direction": spec.direction,
            "depth_m": spec.depth_m,
            "frame": frame.to_dict(),
            "level_ids": list(spec.level_ids),
            "crop": None if spec.crop is None else spec.crop.to_dict(),
        },
        visibility=spec.visibility,
        elements=tuple(sorted(elements, key=element_sort_key)),
        metadata={"section_plane_source": "explicit view specification"},
    )
