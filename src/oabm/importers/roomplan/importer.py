from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from oabm.model import (
    Box3D,
    BuildingModel,
    CoordinateSystem,
    Level,
    Obstacle,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Provenance,
    Quaternion,
    Size3,
    Slab,
    Space,
    Wall,
    stable_id,
)

_EPS = 1e-9
_CONFIDENCE = {"low": 0.33, "medium": 0.66, "high": 1.0}
_SURFACE_COLLECTIONS = ("walls", "floors", "doors", "windows", "openings")
_ELEMENT_COLLECTIONS = (*_SURFACE_COLLECTIONS, "objects")


class RoomPlanImportError(ValueError):
    """Raised when CapturedRoom data cannot be represented faithfully in v1."""


@dataclass(frozen=True, slots=True)
class _OpeningHostInference:
    wall_id: str
    distance_m: float
    candidates_within_tolerance: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class RoomPlanImportOptions:
    """Importer choices needed where RoomPlan exposes a surface, not a solid."""

    wall_surface_thickness_m: float = 0.001
    floor_surface_thickness_m: float = 0.001
    opening_surface_depth_m: float = 0.001
    object_min_dimension_m: float = 0.001
    orphan_opening_host_tolerance_m: float = 0.75
    orphan_opening_host_ambiguity_m: float = 0.01

    def __post_init__(self) -> None:
        for label, value in (
            ("wall_surface_thickness_m", self.wall_surface_thickness_m),
            ("floor_surface_thickness_m", self.floor_surface_thickness_m),
            ("opening_surface_depth_m", self.opening_surface_depth_m),
            ("object_min_dimension_m", self.object_min_dimension_m),
        ):
            if not _is_finite_number(value) or float(value) <= 0:
                raise RoomPlanImportError(f"{label} must be a finite number > 0")
        for label, value in (
            ("orphan_opening_host_tolerance_m", self.orphan_opening_host_tolerance_m),
            ("orphan_opening_host_ambiguity_m", self.orphan_opening_host_ambiguity_m),
        ):
            if not _is_finite_number(value) or float(value) < 0:
                raise RoomPlanImportError(f"{label} must be a finite number >= 0")


def load_captured_room(
    path: str | Path,
    *,
    source_id: str | None = None,
    name: str | None = None,
    options: RoomPlanImportOptions | None = None,
) -> BuildingModel:
    """Load a JSONEncoder-produced CapturedRoom document from disk."""

    source_path = Path(path)
    try:
        document = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoomPlanImportError(f"could not read CapturedRoom JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise RoomPlanImportError("CapturedRoom document root must be a JSON object")
    return import_captured_room(
        document,
        source_id=source_id or str(source_path),
        name=name,
        options=options,
    )


def import_captured_room(
    document: Mapping[str, Any],
    *,
    source_id: str | None = None,
    name: str | None = None,
    options: RoomPlanImportOptions | None = None,
) -> BuildingModel:
    """Convert Apple RoomPlan CapturedRoom JSON into the canonical v1 model.

    RoomPlan uses a right-handed scene with +Y up. The canonical model is also
    right-handed but requires +Z up, so source coordinates are rotated by +90
    degrees about X: (x, y, z) -> (x, -z, y). No plan-view flattening occurs.
    """

    if not isinstance(document, Mapping):
        raise RoomPlanImportError("CapturedRoom document must be a mapping")

    options = options or RoomPlanImportOptions()
    room_identifier = _required_identifier(document, "CapturedRoom")
    provenance_source_id = source_id or room_identifier
    room_story = _story(document.get("story", 0), "CapturedRoom.story")
    room_version = document.get("version")

    source_items: dict[str, list[Mapping[str, Any]]] = {}
    for collection in _ELEMENT_COLLECTIONS:
        raw = document.get(collection, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise RoomPlanImportError(f"CapturedRoom.{collection} must be an array")
        items: list[Mapping[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise RoomPlanImportError(
                    f"CapturedRoom.{collection}[{index}] must be an object"
                )
            _required_identifier(item, f"CapturedRoom.{collection}[{index}]")
            items.append(item)
        source_items[collection] = sorted(items, key=lambda item: str(item["identifier"]))

    stories = {room_story}
    for collection in _ELEMENT_COLLECTIONS:
        for item in source_items[collection]:
            stories.add(_element_story(item, room_story, collection))

    level_ids = {
        story: stable_id("level", f"roomplan:{room_identifier}:story:{story}")
        for story in sorted(stories)
    }

    walls: list[Wall] = []
    wall_id_by_source: dict[str, str] = {}
    wall_story_by_id: dict[str, int] = {}
    wall_geometry_by_id: dict[str, Polyline3D] = {}
    wall_base_by_story: dict[int, list[tuple[float, float, str]]] = {
        story: [] for story in stories
    }
    wall_top_by_story: dict[int, list[tuple[float, float, str]]] = {
        story: [] for story in stories
    }

    for item in source_items["walls"]:
        source_element_id = str(item["identifier"])
        story = _element_story(item, room_story, "walls")
        transform = _Transform.from_json(item.get("transform"), f"wall {source_element_id}")
        dimensions = _dimensions(item.get("dimensions"), f"wall {source_element_id}")
        polygon = _surface_polygon_points(item, transform, f"wall {source_element_id}")
        centerline, height_m, base_z, top_z = _wall_geometry(
            transform,
            dimensions,
            polygon,
            item.get("curve"),
            f"wall {source_element_id}",
        )
        source_thickness = abs(dimensions[2])
        thickness_m = (
            source_thickness
            if source_thickness > _EPS
            else options.wall_surface_thickness_m
        )
        entity_id = stable_id(
            "wall", f"roomplan:{room_identifier}:surface:{source_element_id}"
        )
        confidence, confidence_label = _confidence(item.get("confidence"))
        attributes = _element_attributes(
            item,
            collection="walls",
            category=_category(item.get("category"), default="wall"),
            story=story,
            dimensions=dimensions,
            transform=transform,
            canonical_polygon=polygon,
        )
        attributes["roomplan"]["surface_thickness_m"] = thickness_m
        attributes["roomplan"]["surface_thickness_inferred"] = source_thickness <= _EPS
        wall = Wall(
            id=entity_id,
            level_id=level_ids[story],
            centerline=centerline,
            thickness_m=thickness_m,
            height_m=height_m,
            confidence=confidence,
            provenance=(
                _provenance(
                    provenance_source_id,
                    source_element_id,
                    "walls",
                    story,
                    confidence,
                    confidence_label,
                ),
            ),
            attributes=attributes,
        )
        walls.append(wall)
        wall_id_by_source[source_element_id] = entity_id
        wall_story_by_id[entity_id] = story
        wall_geometry_by_id[entity_id] = centerline
        wall_base_by_story[story].append((base_z, confidence, source_element_id))
        wall_top_by_story[story].append((top_z, confidence, source_element_id))

    slabs: list[Slab] = []
    floor_polygons_by_story: dict[
        int, list[tuple[str, Polygon3D, float, float]]
    ] = {story: [] for story in stories}
    floor_elevations_by_story: dict[int, list[tuple[float, float, str]]] = {
        story: [] for story in stories
    }

    for item in source_items["floors"]:
        source_element_id = str(item["identifier"])
        story = _element_story(item, room_story, "floors")
        transform = _Transform.from_json(item.get("transform"), f"floor {source_element_id}")
        dimensions = _dimensions(item.get("dimensions"), f"floor {source_element_id}")
        polygon_points = _surface_polygon_points(item, transform, f"floor {source_element_id}")
        if len(polygon_points) < 3:
            polygon_points = _rect_surface_polygon(
                transform, dimensions, f"floor {source_element_id}"
            )
        footprint = _polygon(polygon_points, f"floor {source_element_id}")
        elevation = median(point.z for point in footprint.points)
        source_thickness = abs(dimensions[2])
        thickness_m = (
            source_thickness
            if source_thickness > _EPS
            else options.floor_surface_thickness_m
        )
        entity_id = stable_id(
            "slab", f"roomplan:{room_identifier}:surface:{source_element_id}"
        )
        confidence, confidence_label = _confidence(item.get("confidence"))
        attributes = _element_attributes(
            item,
            collection="floors",
            category=_category(item.get("category"), default="floor"),
            story=story,
            dimensions=dimensions,
            transform=transform,
            canonical_polygon=tuple(footprint.points),
        )
        attributes["roomplan"]["surface_thickness_m"] = thickness_m
        attributes["roomplan"]["surface_thickness_inferred"] = source_thickness <= _EPS
        slab = Slab(
            id=entity_id,
            level_id=level_ids[story],
            footprint=footprint,
            thickness_m=thickness_m,
            confidence=confidence,
            provenance=(
                _provenance(
                    provenance_source_id,
                    source_element_id,
                    "floors",
                    story,
                    confidence,
                    confidence_label,
                ),
            ),
            attributes=attributes,
        )
        slabs.append(slab)
        floor_polygons_by_story[story].append(
            (source_element_id, footprint, _polygon_area_xy(footprint), confidence)
        )
        floor_elevations_by_story[story].append(
            (elevation, confidence, source_element_id)
        )

    object_bottom_by_story: dict[int, list[tuple[float, float, str]]] = {
        story: [] for story in stories
    }
    obstacles: list[Obstacle] = []
    for item in source_items["objects"]:
        source_element_id = str(item["identifier"])
        story = _element_story(item, room_story, "objects")
        transform = _Transform.from_json(item.get("transform"), f"object {source_element_id}")
        dimensions = _dimensions(item.get("dimensions"), f"object {source_element_id}")
        canonical_size, inferred_axes = _canonical_size(
            dimensions,
            min_value=options.object_min_dimension_m,
        )
        pose = transform.canonical_pose()
        category = _category(item.get("category"), default="object")
        confidence, confidence_label = _confidence(item.get("confidence"))
        object_bottom_by_story[story].append(
            (pose.position.z - canonical_size.z / 2.0, confidence, source_element_id)
        )
        attributes = _element_attributes(
            item,
            collection="objects",
            category=category,
            story=story,
            dimensions=dimensions,
            transform=transform,
        )
        if inferred_axes:
            attributes["roomplan"]["inferred_size_axes"] = inferred_axes
        obstacles.append(
            Obstacle(
                id=stable_id(
                    "obstacle",
                    f"roomplan:{room_identifier}:object:{source_element_id}",
                ),
                name=category,
                geometry=Box3D(pose=pose, size=canonical_size),
                obstacle_type=f"roomplan-object:{category}",
                level_id=level_ids[story],
                clearance_m=0.0,
                confidence=confidence,
                provenance=(
                    _provenance(
                        provenance_source_id,
                        source_element_id,
                        "objects",
                        story,
                        confidence,
                        confidence_label,
                    ),
                ),
                attributes=attributes,
            )
        )

    levels: list[Level] = []
    for story in sorted(stories):
        elevation_method: str
        elevation_source_ids: list[str]
        if floor_elevations_by_story[story]:
            elevation_evidence = floor_elevations_by_story[story]
            elevation = median(entry[0] for entry in elevation_evidence)
            elevation_method = "floor-surface"
            elevation_confidence = min(entry[1] for entry in elevation_evidence)
            elevation_source_ids = sorted(entry[2] for entry in elevation_evidence)
        elif wall_base_by_story[story]:
            elevation_evidence = wall_base_by_story[story]
            elevation = median(entry[0] for entry in elevation_evidence)
            elevation_method = "wall-base"
            elevation_confidence = min(entry[1] for entry in elevation_evidence)
            elevation_source_ids = sorted(entry[2] for entry in elevation_evidence)
        elif object_bottom_by_story[story]:
            minimum_bottom = min(entry[0] for entry in object_bottom_by_story[story])
            elevation_evidence = [
                entry
                for entry in object_bottom_by_story[story]
                if abs(entry[0] - minimum_bottom) <= _EPS
            ]
            elevation = minimum_bottom
            elevation_method = "object-bottom"
            elevation_confidence = min(entry[1] for entry in elevation_evidence)
            elevation_source_ids = sorted(entry[2] for entry in elevation_evidence)
        else:
            elevation = 0.0
            elevation_method = "default-zero"
            elevation_confidence = 0.0
            elevation_source_ids = []

        height_confidence: float | None = None
        height_source_ids: list[str] = []
        height_method: str | None = None
        if wall_top_by_story[story]:
            maximum_top = max(entry[0] for entry in wall_top_by_story[story])
            top_evidence = [
                entry
                for entry in wall_top_by_story[story]
                if abs(entry[0] - maximum_top) <= _EPS
            ]
            height = maximum_top - elevation
            height_m = height if height > _EPS else None
            if height_m is not None:
                height_confidence = min(entry[1] for entry in top_evidence)
                height_source_ids = sorted(entry[2] for entry in top_evidence)
                height_method = "wall-top-minus-elevation"
        else:
            height_m = None

        level_confidence = elevation_confidence
        if height_confidence is not None:
            level_confidence = min(level_confidence, height_confidence)

        derivation = {
            "story": story,
            "roomplan_story": story,
            "elevation_method": elevation_method,
            "elevation_source_identifiers": elevation_source_ids,
            "elevation_confidence": elevation_confidence,
            "height_method": height_method,
            "height_source_identifiers": height_source_ids,
            "height_confidence": height_confidence,
            "confidence_rule": "minimum-confidence-of-contributing-source-geometry",
        }
        levels.append(
            Level(
                id=level_ids[story],
                name=f"Story {story}",
                elevation_m=elevation,
                height_m=height_m,
                confidence=level_confidence,
                provenance=(
                    Provenance(
                        source_kind="roomplan",
                        source_id=provenance_source_id,
                        source_element_id=f"story:{story}",
                        method="CapturedRoom story grouping",
                        confidence=level_confidence,
                        attributes=derivation,
                    ),
                ),
                attributes={"roomplan": dict(derivation)},
            )
        )

    spaces: list[Space] = []
    room_floor_candidates = floor_polygons_by_story.get(room_story, [])
    if room_floor_candidates:
        source_floor_id, footprint, _, floor_confidence = sorted(
            room_floor_candidates,
            key=lambda entry: (-entry[2], entry[0]),
        )[0]
        level = next(level for level in levels if level.id == level_ids[room_story])
        sections = _sections(document.get("sections"), room_story)
        usage = _single_section_usage(sections)
        space_confidence = min(floor_confidence, level.confidence)
        spaces.append(
            Space(
                id=stable_id("space", f"roomplan:{room_identifier}:room"),
                name=name or f"RoomPlan room {room_identifier}",
                level_id=level_ids[room_story],
                footprint=footprint,
                height_m=level.height_m,
                usage=usage,
                confidence=space_confidence,
                provenance=(
                    Provenance(
                        source_kind="roomplan",
                        source_id=provenance_source_id,
                        source_element_id=room_identifier,
                        method="CapturedRoom floor footprint",
                        confidence=space_confidence,
                        attributes={
                            "roomplan_story": room_story,
                            "source_floor_identifier": source_floor_id,
                            "source_floor_confidence": floor_confidence,
                            "level_confidence": level.confidence,
                            "confidence_rule": (
                                "minimum-of-source-floor-and-derived-level-confidence"
                            ),
                        },
                    ),
                ),
                attributes={
                    "roomplan": {
                        "identifier": room_identifier,
                        "story": room_story,
                        "version": room_version,
                        "sections": sections,
                        "source_floor_identifier": source_floor_id,
                        "source_floor_confidence": floor_confidence,
                        "level_confidence": level.confidence,
                        "confidence_rule": (
                            "minimum-of-source-floor-and-derived-level-confidence"
                        ),
                    }
                },
            )
        )

    openings: list[Opening] = []
    for collection, opening_type in (
        ("doors", "door"),
        ("windows", "window"),
        ("openings", "opening"),
    ):
        for item in source_items[collection]:
            source_element_id = str(item["identifier"])
            story = _element_story(item, room_story, collection)
            transform = _Transform.from_json(
                item.get("transform"), f"{opening_type} {source_element_id}"
            )
            dimensions = _dimensions(
                item.get("dimensions"), f"{opening_type} {source_element_id}"
            )
            pose = transform.canonical_pose()
            parent_identifier = _optional_identifier(item.get("parentIdentifier"))
            host_id: str | None = None
            host_inference: _OpeningHostInference | None = None
            host_inferred = False
            if parent_identifier is not None:
                host_id = wall_id_by_source.get(parent_identifier)
                if host_id is None:
                    raise RoomPlanImportError(
                        f"{opening_type} {source_element_id} references unknown "
                        f"parentIdentifier {parent_identifier!r}"
                    )
            else:
                host_inference = _infer_opening_host(
                    pose.position,
                    story,
                    wall_story_by_id,
                    wall_geometry_by_id,
                    options.orphan_opening_host_tolerance_m,
                    options.orphan_opening_host_ambiguity_m,
                    f"{opening_type} {source_element_id}",
                )
                host_id = None if host_inference is None else host_inference.wall_id
                host_inferred = host_inference is not None

            if host_id is None:
                raise RoomPlanImportError(
                    f"{opening_type} {source_element_id} has no resolvable host wall"
                )

            host_wall = next(wall for wall in walls if wall.id == host_id)
            width = abs(dimensions[0])
            height = abs(dimensions[1])
            if width <= _EPS or height <= _EPS:
                raise RoomPlanImportError(
                    f"{opening_type} {source_element_id} requires positive width and height"
                )
            source_depth = abs(dimensions[2])
            depth = (
                source_depth
                if source_depth > _EPS
                else max(host_wall.thickness_m, options.opening_surface_depth_m)
            )
            source_confidence, confidence_label = _confidence(item.get("confidence"))
            confidence = source_confidence
            if host_inference is not None:
                confidence = min(source_confidence, host_wall.confidence)
            polygon = _surface_polygon_points(
                item, transform, f"{opening_type} {source_element_id}"
            )
            attributes = _element_attributes(
                item,
                collection=collection,
                category=_category(item.get("category"), default=opening_type),
                story=story,
                dimensions=dimensions,
                transform=transform,
                canonical_polygon=polygon,
            )
            attributes["roomplan"]["host_inferred"] = host_inferred
            attributes["roomplan"]["source_confidence_value"] = source_confidence
            if host_inference is not None:
                attributes["roomplan"]["host_inference"] = {
                    "distance_m": host_inference.distance_m,
                    "ambiguity_m": options.orphan_opening_host_ambiguity_m,
                    "candidate_distances_m": [
                        {"wall_id": wall_id, "distance_m": distance}
                        for wall_id, distance in host_inference.candidates_within_tolerance
                    ],
                    "confidence_rule": (
                        "minimum-of-opening-and-inferred-host-wall-confidence"
                    ),
                }
            attributes["roomplan"]["surface_depth_m"] = depth
            attributes["roomplan"]["surface_depth_inferred"] = source_depth <= _EPS
            openings.append(
                Opening(
                    id=stable_id(
                        "opening",
                        f"roomplan:{room_identifier}:surface:{source_element_id}",
                    ),
                    host_id=host_id,
                    opening_type=opening_type,
                    pose=pose,
                    size=Size3(x=width, y=depth, z=height),
                    confidence=confidence,
                    provenance=(
                        _provenance(
                            provenance_source_id,
                            source_element_id,
                            collection,
                            story,
                            confidence,
                            confidence_label,
                        ),
                    ),
                    attributes=attributes,
                )
            )

    model_attributes = {
        "roomplan": {
            "identifier": room_identifier,
            "version": room_version,
            "story": room_story,
            "sections": _sections(document.get("sections"), room_story),
            "source_coordinate_system": {
                "handedness": "right",
                "up_axis": "+Y",
                "length_unit": "m",
            },
            "source_to_canonical": {
                "description": "(x, y, z) -> (x, -z, y)",
                "matrix_row_major": [
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    -1.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
            },
        }
    }
    known_room_fields = {
        "identifier",
        "version",
        "story",
        "walls",
        "floors",
        "doors",
        "windows",
        "openings",
        "objects",
        "sections",
    }
    room_extras = {
        key: _json_copy(value)
        for key, value in document.items()
        if key not in known_room_fields
    }
    if room_extras:
        model_attributes["roomplan"]["extra_fields"] = room_extras

    model = BuildingModel(
        model_id=stable_id("model", f"roomplan:{room_identifier}"),
        name=name or f"RoomPlan capture {room_identifier}",
        coordinate_system=CoordinateSystem(
            frame_id=stable_id("frame", f"roomplan:{room_identifier}")
        ),
        levels=tuple(sorted(levels, key=lambda item: item.id)),
        spaces=tuple(sorted(spaces, key=lambda item: item.id)),
        walls=tuple(sorted(walls, key=lambda item: item.id)),
        slabs=tuple(sorted(slabs, key=lambda item: item.id)),
        openings=tuple(sorted(openings, key=lambda item: item.id)),
        obstacles=tuple(sorted(obstacles, key=lambda item: item.id)),
        provenance=(
            Provenance(
                source_kind="roomplan",
                source_id=provenance_source_id,
                source_element_id=room_identifier,
                method="CapturedRoom JSON import",
                attributes={"roomplan_version": room_version, "roomplan_story": room_story},
            ),
        ),
        attributes=model_attributes,
    )
    return model


@dataclass(frozen=True, slots=True)
class _Transform:
    values: tuple[float, ...]

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "_Transform":
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise RoomPlanImportError(f"{label}.transform must be a 16-number array")
        values = tuple(_number(value, f"{label}.transform") for value in raw)
        if len(values) != 16:
            raise RoomPlanImportError(
                f"{label}.transform must contain 16 numbers, got {len(values)}"
            )
        return cls(values=values)

    def source_point(self, point: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = point
        m = self.values
        return (
            m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14],
        )

    def canonical_point(self, point: tuple[float, float, float]) -> Point3:
        return _canonical_point(self.source_point(point))

    def canonical_pose(self) -> Pose:
        source_rotation = _orthonormal_rotation(self.values)
        c = (
            (1.0, 0.0, 0.0),
            (0.0, 0.0, -1.0),
            (0.0, 1.0, 0.0),
        )
        canonical_rotation = _matrix_multiply(
            _matrix_multiply(c, source_rotation),
            _matrix_transpose(c),
        )
        return Pose(
            position=_canonical_point((self.values[12], self.values[13], self.values[14])),
            rotation=_quaternion(canonical_rotation),
        )


def _wall_geometry(
    transform: _Transform,
    dimensions: tuple[float, float, float],
    polygon: tuple[Point3, ...],
    curve: Any,
    label: str,
) -> tuple[Polyline3D, float, float, float]:
    width = abs(dimensions[0])
    source_height = abs(dimensions[1])
    if width <= _EPS or source_height <= _EPS:
        raise RoomPlanImportError(f"{label} requires positive width and height")

    curved_points = _curve_wall_points(curve, transform, source_height, label)
    if curved_points is not None:
        centerline_points, curved_top_points = curved_points
    else:
        centerline_points = ()
        curved_top_points = ()

    if polygon:
        min_z = min(point.z for point in polygon)
        max_z = max(point.z for point in polygon)
        tolerance = max(0.005, (max_z - min_z) * 0.01)
        if not centerline_points:
            base_points = [point for point in polygon if abs(point.z - min_z) <= tolerance]
            if len(base_points) >= 2:
                centerline_points = _ordered_distinct_points(base_points)
                if len(centerline_points) < 2:
                    raise RoomPlanImportError(
                        f"{label} polygon base collapses to one point"
                    )
            else:
                centerline_points = (
                    transform.canonical_point(
                        (-width / 2.0, -source_height / 2.0, 0.0)
                    ),
                    transform.canonical_point(
                        (width / 2.0, -source_height / 2.0, 0.0)
                    ),
                )
        height_m = max_z - min_z
        if height_m <= _EPS:
            height_m = source_height
        base_z = min_z
        top_z = max_z if max_z - min_z > _EPS else min_z + height_m
    elif centerline_points:
        base_z = min(point.z for point in centerline_points)
        top_z = max(point.z for point in curved_top_points)
        height_m = top_z - base_z
        if height_m <= _EPS:
            height_m = source_height
            top_z = base_z + height_m
    else:
        centerline_points = (
            transform.canonical_point(
                (-width / 2.0, -source_height / 2.0, 0.0)
            ),
            transform.canonical_point(
                (width / 2.0, -source_height / 2.0, 0.0)
            ),
        )
        top_a = transform.canonical_point(
            (-width / 2.0, source_height / 2.0, 0.0)
        )
        top_b = transform.canonical_point(
            (width / 2.0, source_height / 2.0, 0.0)
        )
        base_z = min(point.z for point in centerline_points)
        top_z = max(top_a.z, top_b.z)
        height_m = top_z - base_z
        if height_m <= _EPS:
            height_m = source_height
            top_z = base_z + height_m

    return Polyline3D(points=tuple(centerline_points)), height_m, base_z, top_z


def _curve_wall_points(
    raw: Any,
    transform: _Transform,
    source_height: float,
    label: str,
) -> tuple[tuple[Point3, ...], tuple[Point3, ...]] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RoomPlanImportError(f"{label}.curve must be an object or null")

    radius = _number(raw.get("radius"), f"{label}.curve.radius")
    if radius <= _EPS:
        raise RoomPlanImportError(f"{label}.curve.radius must be > 0")

    center_raw = raw.get("center")
    if isinstance(center_raw, Mapping):
        center_values = (center_raw.get("x"), center_raw.get("y"))
    elif isinstance(center_raw, Sequence) and not isinstance(center_raw, (str, bytes)):
        center_values = tuple(center_raw)
    else:
        raise RoomPlanImportError(
            f"{label}.curve.center must contain local x/z coordinates"
        )
    if len(center_values) != 2:
        raise RoomPlanImportError(
            f"{label}.curve.center must contain exactly 2 numbers"
        )
    center_x = _number(center_values[0], f"{label}.curve.center[0]")
    center_z = _number(center_values[1], f"{label}.curve.center[1]")

    start_angle = _angle_radians(raw.get("startAngle"), f"{label}.curve.startAngle")
    end_angle = _angle_radians(raw.get("endAngle"), f"{label}.curve.endAngle")
    sweep = end_angle - start_angle
    if abs(sweep) <= _EPS:
        raise RoomPlanImportError(f"{label}.curve must have a nonzero angular sweep")
    if abs(sweep) > 2.0 * math.pi + _EPS:
        raise RoomPlanImportError(f"{label}.curve sweep cannot exceed one full turn")

    max_step = math.radians(5.0)
    segment_count = max(1, math.ceil(abs(sweep) / max_step))
    base_points: list[Point3] = []
    top_points: list[Point3] = []
    for index in range(segment_count + 1):
        angle = start_angle + sweep * index / segment_count
        local_x = center_x + radius * math.cos(angle)
        local_z = center_z + radius * math.sin(angle)
        base_points.append(
            transform.canonical_point(
                (local_x, -source_height / 2.0, local_z)
            )
        )
        top_points.append(
            transform.canonical_point(
                (local_x, source_height / 2.0, local_z)
            )
        )
    return tuple(base_points), tuple(top_points)


def _angle_radians(raw: Any, label: str) -> float:
    if isinstance(raw, Mapping):
        if "value" not in raw:
            raise RoomPlanImportError(f"{label} measurement must include value")
        value = _number(raw.get("value"), f"{label}.value")
        unit = raw.get("unit")
        symbol: str | None = None
        if isinstance(unit, str):
            symbol = unit
        elif isinstance(unit, Mapping):
            candidate = unit.get("symbol")
            if candidate is not None:
                symbol = str(candidate)
        if symbol is None or symbol.lower() in {"rad", "radian", "radians"}:
            return value
        if symbol.lower() in {"deg", "degree", "degrees"} or symbol == "°":
            return math.radians(value)
        raise RoomPlanImportError(f"{label} uses unsupported angle unit {symbol!r}")
    return _number(raw, label)


def _surface_polygon_points(
    item: Mapping[str, Any],
    transform: _Transform,
    label: str,
) -> tuple[Point3, ...]:
    raw = item.get("polygonCorners", [])
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise RoomPlanImportError(f"{label}.polygonCorners must be an array")
    points: list[Point3] = []
    for index, corner in enumerate(raw):
        if not isinstance(corner, Sequence) or isinstance(corner, (str, bytes)):
            raise RoomPlanImportError(
                f"{label}.polygonCorners[{index}] must be a 3-number array"
            )
        values = tuple(_number(value, f"{label}.polygonCorners[{index}]") for value in corner)
        if len(values) != 3:
            raise RoomPlanImportError(
                f"{label}.polygonCorners[{index}] must contain 3 numbers"
            )
        points.append(transform.canonical_point(values))
    return _strip_polygon_closure(tuple(points))


def _rect_surface_polygon(
    transform: _Transform,
    dimensions: tuple[float, float, float],
    label: str,
) -> tuple[Point3, ...]:
    width = abs(dimensions[0])
    height = abs(dimensions[1])
    if width <= _EPS or height <= _EPS:
        raise RoomPlanImportError(
            f"{label} needs polygonCorners or positive first two dimensions"
        )
    return (
        transform.canonical_point((-width / 2.0, -height / 2.0, 0.0)),
        transform.canonical_point((width / 2.0, -height / 2.0, 0.0)),
        transform.canonical_point((width / 2.0, height / 2.0, 0.0)),
        transform.canonical_point((-width / 2.0, height / 2.0, 0.0)),
    )


def _polygon(points: Sequence[Point3], label: str) -> Polygon3D:
    stripped = _strip_polygon_closure(tuple(points))
    if len(stripped) < 3:
        raise RoomPlanImportError(f"{label} requires at least three polygon points")
    return Polygon3D(points=stripped)


def _strip_polygon_closure(points: tuple[Point3, ...]) -> tuple[Point3, ...]:
    if len(points) >= 2 and _distance_3d(points[0], points[-1]) <= _EPS:
        return points[:-1]
    return points


def _canonical_size(
    dimensions: tuple[float, float, float],
    *,
    min_value: float,
) -> tuple[Size3, list[str]]:
    source = (abs(dimensions[0]), abs(dimensions[2]), abs(dimensions[1]))
    names = ("x", "y", "z")
    values: list[float] = []
    inferred: list[str] = []
    for name, value in zip(names, source):
        if value <= _EPS:
            values.append(min_value)
            inferred.append(name)
        else:
            values.append(value)
    return Size3(x=values[0], y=values[1], z=values[2]), inferred


def _infer_opening_host(
    position: Point3,
    story: int,
    wall_story_by_id: Mapping[str, int],
    wall_geometry_by_id: Mapping[str, Polyline3D],
    tolerance_m: float,
    ambiguity_m: float,
    label: str,
) -> _OpeningHostInference | None:
    candidates: list[tuple[float, str]] = []
    for wall_id, centerline in wall_geometry_by_id.items():
        if wall_story_by_id[wall_id] != story:
            continue
        distance = _point_to_polyline_distance_xy(position, centerline)
        candidates.append((distance, wall_id))

    within_tolerance = sorted(
        (
            (distance, wall_id)
            for distance, wall_id in candidates
            if distance <= tolerance_m
        ),
        key=lambda item: (item[0], item[1]),
    )
    if not within_tolerance:
        return None

    nearest_distance = within_tolerance[0][0]
    ambiguous = [
        (distance, wall_id)
        for distance, wall_id in within_tolerance
        if abs(distance - nearest_distance) <= ambiguity_m
    ]
    if len(ambiguous) > 1:
        details = ", ".join(
            f"{wall_id} ({distance:.6f} m)"
            for distance, wall_id in ambiguous
        )
        raise RoomPlanImportError(
            f"{label} host wall is ambiguous within {ambiguity_m:.6f} m: {details}"
        )

    distance, wall_id = within_tolerance[0]
    return _OpeningHostInference(
        wall_id=wall_id,
        distance_m=distance,
        candidates_within_tolerance=tuple(
            (candidate_wall_id, candidate_distance)
            for candidate_distance, candidate_wall_id in within_tolerance
        ),
    )


def _point_to_polyline_distance_xy(point: Point3, polyline: Polyline3D) -> float:
    return min(
        _point_to_segment_distance_xy(point, a, b)
        for a, b in zip(polyline.points, polyline.points[1:])
    )


def _point_to_segment_distance_xy(point: Point3, a: Point3, b: Point3) -> float:
    dx = b.x - a.x
    dy = b.y - a.y
    length_sq = dx * dx + dy * dy
    if length_sq <= _EPS:
        return math.hypot(point.x - a.x, point.y - a.y)
    t = ((point.x - a.x) * dx + (point.y - a.y) * dy) / length_sq
    t = min(1.0, max(0.0, t))
    qx = a.x + t * dx
    qy = a.y + t * dy
    return math.hypot(point.x - qx, point.y - qy)


def _element_attributes(
    item: Mapping[str, Any],
    *,
    collection: str,
    category: str,
    story: int,
    dimensions: tuple[float, float, float],
    transform: _Transform,
    canonical_polygon: Sequence[Point3] = (),
) -> dict[str, Any]:
    roomplan: dict[str, Any] = {
        "collection": collection,
        "identifier": str(item["identifier"]),
        "category": _json_copy(item.get("category")),
        "category_token": category,
        "confidence": _json_copy(item.get("confidence")),
        "story": story,
        "parent_identifier": _optional_identifier(item.get("parentIdentifier")),
        "dimensions_m": list(dimensions),
        "transform_column_major": list(transform.values),
    }
    for key in ("completedEdges", "curve", "attributes"):
        if key in item:
            roomplan[key] = _json_copy(item.get(key))
    if "polygonCorners" in item:
        roomplan["polygon_corners_local"] = _json_copy(item.get("polygonCorners"))
    if canonical_polygon:
        roomplan["polygon_corners_canonical"] = [
            {"x": point.x, "y": point.y, "z": point.z} for point in canonical_polygon
        ]

    known = {
        "identifier",
        "category",
        "confidence",
        "story",
        "parentIdentifier",
        "dimensions",
        "transform",
        "polygonCorners",
        "completedEdges",
        "curve",
        "attributes",
    }
    extras = {key: _json_copy(value) for key, value in item.items() if key not in known}
    if extras:
        roomplan["extra_fields"] = extras
    return {"roomplan": roomplan}


def _provenance(
    source_id: str,
    source_element_id: str,
    collection: str,
    story: int,
    confidence: float,
    confidence_label: str | None,
) -> Provenance:
    return Provenance(
        source_kind="roomplan",
        source_id=source_id,
        source_element_id=source_element_id,
        method="CapturedRoom JSON import",
        confidence=confidence,
        attributes={
            "roomplan_collection": collection,
            "roomplan_story": story,
            "roomplan_confidence": confidence_label,
        },
    )


def _sections(raw: Any, room_story: int) -> list[dict[str, Any]]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise RoomPlanImportError("CapturedRoom.sections must be an array")
    sections: list[dict[str, Any]] = []
    for index, section in enumerate(raw):
        if not isinstance(section, Mapping):
            raise RoomPlanImportError(f"CapturedRoom.sections[{index}] must be an object")
        story = _story(section.get("story", room_story), f"sections[{index}].story")
        center_raw = section.get("center")
        center = None
        if center_raw is not None:
            if not isinstance(center_raw, Sequence) or isinstance(center_raw, (str, bytes)):
                raise RoomPlanImportError(
                    f"CapturedRoom.sections[{index}].center must be a 3-number array"
                )
            values = tuple(_number(value, f"sections[{index}].center") for value in center_raw)
            if len(values) != 3:
                raise RoomPlanImportError(
                    f"CapturedRoom.sections[{index}].center must contain 3 numbers"
                )
            point = _canonical_point(values)
            center = {"x": point.x, "y": point.y, "z": point.z}
        label = section.get("label")
        if isinstance(label, Mapping):
            label = _category(label, default="unidentified")
        elif label is not None:
            label = str(label)
        sections.append(
            {
                "label": label,
                "story": story,
                "center": center,
                "source": _json_copy(section),
            }
        )
    return sorted(
        sections,
        key=lambda item: (
            item["story"],
            "" if item["label"] is None else item["label"],
            json.dumps(item["source"], sort_keys=True, separators=(",", ":")),
        ),
    )


def _single_section_usage(sections: Sequence[Mapping[str, Any]]) -> str | None:
    labels = sorted(
        {
            str(section["label"])
            for section in sections
            if section.get("label") not in (None, "unidentified")
        }
    )
    return labels[0] if len(labels) == 1 else None


def _required_identifier(item: Mapping[str, Any], label: str) -> str:
    value = item.get("identifier")
    if not isinstance(value, str) or not value.strip():
        raise RoomPlanImportError(f"{label}.identifier must be a non-empty string")
    return value


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RoomPlanImportError("parentIdentifier must be null or a non-empty string")
    return value


def _element_story(item: Mapping[str, Any], room_story: int, collection: str) -> int:
    return _story(item.get("story", room_story), f"{collection} story")


def _story(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomPlanImportError(f"{label} must be an integer")
    return value


def _dimensions(raw: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise RoomPlanImportError(f"{label}.dimensions must be a 3-number array")
    values = tuple(_number(value, f"{label}.dimensions") for value in raw)
    if len(values) != 3:
        raise RoomPlanImportError(
            f"{label}.dimensions must contain 3 numbers, got {len(values)}"
        )
    return values


def _confidence(raw: Any) -> tuple[float, str | None]:
    label: str | None = None
    if isinstance(raw, str):
        label = raw.lower()
    elif isinstance(raw, Mapping):
        for candidate in ("high", "medium", "low"):
            if candidate in raw:
                label = candidate
                break
        if label is None and len(raw) == 1:
            label = str(next(iter(raw))).lower()
    if label in _CONFIDENCE:
        return _CONFIDENCE[label], label
    return 0.5, label


def _category(raw: Any, *, default: str) -> str:
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, Mapping) and raw:
        return str(sorted(raw.keys(), key=str)[0])
    return default


def _number(value: Any, label: str) -> float:
    if not _is_finite_number(value):
        raise RoomPlanImportError(f"{label} must contain only finite numbers")
    return float(value)


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _canonical_point(source: tuple[float, float, float]) -> Point3:
    x, y, z = source
    return Point3(x=_clean(x), y=_clean(-z), z=_clean(y))


def _clean(value: float) -> float:
    return 0.0 if abs(value) < 1e-12 else float(value)


def _orthonormal_rotation(values: tuple[float, ...]) -> tuple[tuple[float, ...], ...]:
    c0 = _normalize((values[0], values[1], values[2]), "transform X basis")
    raw_c1 = (values[4], values[5], values[6])
    dot01 = _dot(c0, raw_c1)
    c1 = _normalize(
        (
            raw_c1[0] - dot01 * c0[0],
            raw_c1[1] - dot01 * c0[1],
            raw_c1[2] - dot01 * c0[2],
        ),
        "transform Y basis",
    )
    c2 = _cross(c0, c1)
    raw_c2 = (values[8], values[9], values[10])
    if _dot(c2, raw_c2) < 0:
        raise RoomPlanImportError("transform rotation must be right-handed")
    return (
        (c0[0], c1[0], c2[0]),
        (c0[1], c1[1], c2[1]),
        (c0[2], c1[2], c2[2]),
    )


def _normalize(vector: tuple[float, float, float], label: str) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length <= _EPS:
        raise RoomPlanImportError(f"{label} is degenerate")
    return tuple(component / length for component in vector)  # type: ignore[return-value]


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _matrix_multiply(
    a: tuple[tuple[float, ...], ...],
    b: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(sum(a[row][k] * b[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )


def _matrix_transpose(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(matrix[col][row] for col in range(3)) for row in range(3))


def _quaternion(matrix: tuple[tuple[float, ...], ...]) -> Quaternion:
    m00, m01, m02 = matrix[0]
    m10, m11, m12 = matrix[1]
    m20, m21, m22 = matrix[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m21 - m12) / s
        y = (m02 - m20) / s
        z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(max(0.0, 1.0 + m00 - m11 - m22)) * 2.0
        w = (m21 - m12) / s
        x = 0.25 * s
        y = (m01 + m10) / s
        z = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(max(0.0, 1.0 + m11 - m00 - m22)) * 2.0
        w = (m02 - m20) / s
        x = (m01 + m10) / s
        y = 0.25 * s
        z = (m12 + m21) / s
    else:
        s = math.sqrt(max(0.0, 1.0 + m22 - m00 - m11)) * 2.0
        w = (m10 - m01) / s
        x = (m02 + m20) / s
        y = (m12 + m21) / s
        z = 0.25 * s
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= _EPS:
        raise RoomPlanImportError("transform rotation cannot be converted to a quaternion")
    x, y, z, w = (component / norm for component in (x, y, z, w))
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    return Quaternion(x=_clean(x), y=_clean(y), z=_clean(z), w=_clean(w))


def _ordered_distinct_points(points: Sequence[Point3]) -> tuple[Point3, ...]:
    ordered: list[Point3] = []
    for point in points:
        if not ordered or _distance_3d(ordered[-1], point) > _EPS:
            ordered.append(point)
    if len(ordered) > 2 and _distance_3d(ordered[0], ordered[-1]) <= _EPS:
        ordered.pop()
    return tuple(ordered)


def _distance_3d(a: Point3, b: Point3) -> float:
    return math.sqrt(
        (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
    )


def _polygon_area_xy(polygon: Polygon3D) -> float:
    points = polygon.points
    area = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        area += point.x * next_point.y - next_point.x * point.y
    return abs(area) / 2.0


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise RoomPlanImportError(
            "RoomPlan source metadata must be JSON-compatible"
        ) from exc
