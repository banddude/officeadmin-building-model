from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

DRAWING_FORMAT_VERSION = "1.0.0"
_PRECISION = 9


class DrawingError(ValueError):
    """Raised when a drawing request is invalid or cannot be derived safely."""


def canonical_number(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DrawingError(f"drawing coordinate must be a finite number, got {value!r}")
    rounded = round(float(value), _PRECISION)
    return 0.0 if rounded == 0.0 else rounded


@dataclass(frozen=True, slots=True, order=True)
class Point2:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", canonical_number(self.x))
        object.__setattr__(self, "y", canonical_number(self.y))

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class Rect2:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        values = tuple(canonical_number(v) for v in (self.min_x, self.min_y, self.max_x, self.max_y))
        object.__setattr__(self, "min_x", values[0])
        object.__setattr__(self, "min_y", values[1])
        object.__setattr__(self, "max_x", values[2])
        object.__setattr__(self, "max_y", values[3])
        if self.max_x <= self.min_x or self.max_y <= self.min_y:
            raise DrawingError("crop rectangle must have positive width and height")

    def to_dict(self) -> dict[str, float]:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
        }


@dataclass(frozen=True, slots=True)
class Visibility:
    spaces: bool = True
    walls: bool = True
    slabs: bool = True
    ceilings: bool = True
    openings: bool = True
    electrical_equipment: bool = True
    electrical_devices: bool = True
    routes: bool = True
    route_fittings: bool = True
    obstacles: bool = False
    route_constraints: bool = False
    annotations: bool = True
    dimensions: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "spaces": self.spaces,
            "walls": self.walls,
            "slabs": self.slabs,
            "ceilings": self.ceilings,
            "openings": self.openings,
            "electrical_equipment": self.electrical_equipment,
            "electrical_devices": self.electrical_devices,
            "routes": self.routes,
            "route_fittings": self.route_fittings,
            "obstacles": self.obstacles,
            "route_constraints": self.route_constraints,
            "annotations": self.annotations,
            "dimensions": self.dimensions,
        }


@dataclass(frozen=True, slots=True)
class PlanViewSpec:
    view_id: str
    level_id: str
    title: str | None = None
    crop: Rect2 | None = None
    view_range_below_m: float = 0.05
    view_range_above_m: float | None = None
    visibility: Visibility = field(default_factory=Visibility)

    def __post_init__(self) -> None:
        if not self.view_id or not self.level_id:
            raise DrawingError("plan view_id and level_id are required")
        below = canonical_number(self.view_range_below_m)
        object.__setattr__(self, "view_range_below_m", below)
        if below < 0:
            raise DrawingError("plan view_range_below_m must be >= 0")
        if self.view_range_above_m is not None:
            above = canonical_number(self.view_range_above_m)
            object.__setattr__(self, "view_range_above_m", above)
            if above <= 0:
                raise DrawingError("plan view_range_above_m must be > 0 when supplied")


ElevationDirection = Literal["north", "south", "east", "west"]


@dataclass(frozen=True, slots=True)
class ElevationViewSpec:
    view_id: str
    direction: ElevationDirection
    title: str | None = None
    crop: Rect2 | None = None
    level_ids: tuple[str, ...] = ()
    min_depth_m: float | None = None
    max_depth_m: float | None = None
    visibility: Visibility = field(default_factory=Visibility)

    def __post_init__(self) -> None:
        if not self.view_id:
            raise DrawingError("elevation view_id is required")
        if self.direction not in ("north", "south", "east", "west"):
            raise DrawingError(f"unsupported elevation direction {self.direction!r}")
        if self.min_depth_m is not None:
            object.__setattr__(self, "min_depth_m", canonical_number(self.min_depth_m))
        if self.max_depth_m is not None:
            object.__setattr__(self, "max_depth_m", canonical_number(self.max_depth_m))
        if self.min_depth_m is not None and self.max_depth_m is not None:
            if self.max_depth_m <= self.min_depth_m:
                raise DrawingError("elevation max_depth_m must be greater than min_depth_m")


SectionAxis = Literal["x", "y"]
SectionDirection = Literal["positive", "negative"]


@dataclass(frozen=True, slots=True)
class SectionViewSpec:
    view_id: str
    axis: SectionAxis
    offset_m: float
    direction: SectionDirection = "positive"
    depth_m: float = 0.1
    title: str | None = None
    crop: Rect2 | None = None
    level_ids: tuple[str, ...] = ()
    visibility: Visibility = field(default_factory=Visibility)

    def __post_init__(self) -> None:
        if not self.view_id:
            raise DrawingError("section view_id is required")
        if self.axis not in ("x", "y"):
            raise DrawingError(f"unsupported section axis {self.axis!r}")
        if self.direction not in ("positive", "negative"):
            raise DrawingError(f"unsupported section direction {self.direction!r}")
        object.__setattr__(self, "offset_m", canonical_number(self.offset_m))
        depth = canonical_number(self.depth_m)
        object.__setattr__(self, "depth_m", depth)
        if depth <= 0:
            raise DrawingError("section depth_m must be > 0")


ViewSpec = PlanViewSpec | ElevationViewSpec | SectionViewSpec


def stable_element_id(view_id: str, role: str, source_ids: tuple[str, ...], part: int = 0) -> str:
    key = "\x1f".join((view_id, role, *source_ids, str(part)))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return f"draw:{digest}"


def _provenance_to_dict(record: Any) -> dict[str, Any]:
    return {
        "source_kind": record.source_kind,
        "source_id": record.source_id,
        "source_element_id": record.source_element_id,
        "page": record.page,
        "method": record.method,
        "confidence": canonical_number(record.confidence),
        "attributes": record.attributes,
    }


@dataclass(frozen=True, slots=True)
class PolylineElement:
    element_id: str
    layer: str
    role: str
    source_ids: tuple[str, ...]
    points: tuple[Point2, ...]
    closed: bool = False
    line_width_m: float | None = None
    confidence: float | None = None
    provenance: tuple[Any, ...] = ()

    kind: str = field(default="polyline", init=False)

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise DrawingError("polyline drawing elements need at least two points")
        if self.line_width_m is not None and self.line_width_m <= 0:
            raise DrawingError("line_width_m must be > 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_id": self.element_id,
            "layer": self.layer,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "points": [point.to_dict() for point in self.points],
            "closed": self.closed,
            "line_width_m": None if self.line_width_m is None else canonical_number(self.line_width_m),
            "confidence": None if self.confidence is None else canonical_number(self.confidence),
            "provenance": [_provenance_to_dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class SymbolElement:
    element_id: str
    layer: str
    role: str
    source_ids: tuple[str, ...]
    point: Point2
    symbol: str
    rotation_radians: float = 0.0
    confidence: float | None = None
    provenance: tuple[Any, ...] = ()

    kind: str = field(default="symbol", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_id": self.element_id,
            "layer": self.layer,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "point": self.point.to_dict(),
            "symbol": self.symbol,
            "rotation_radians": canonical_number(self.rotation_radians),
            "confidence": None if self.confidence is None else canonical_number(self.confidence),
            "provenance": [_provenance_to_dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class LabelElement:
    element_id: str
    layer: str
    role: str
    source_ids: tuple[str, ...]
    point: Point2
    text: str

    kind: str = field(default="label", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_id": self.element_id,
            "layer": self.layer,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "point": self.point.to_dict(),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class DimensionElement:
    element_id: str
    layer: str
    role: str
    source_ids: tuple[str, ...]
    start: Point2
    end: Point2
    text: str

    kind: str = field(default="dimension", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_id": self.element_id,
            "layer": self.layer,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "text": self.text,
        }


DrawingElement = PolylineElement | SymbolElement | LabelElement | DimensionElement


def element_sort_key(element: DrawingElement) -> tuple[Any, ...]:
    return (
        element.layer,
        element.role,
        element.source_ids,
        element.kind,
        element.element_id,
    )


@dataclass(frozen=True, slots=True)
class DrawingView:
    view_id: str
    kind: Literal["plan", "elevation", "section"]
    title: str
    source_model_id: str
    source_schema_version: str
    projection: dict[str, Any]
    visibility: Visibility
    elements: tuple[DrawingElement, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "kind": self.kind,
            "title": self.title,
            "source_model_id": self.source_model_id,
            "source_schema_version": self.source_schema_version,
            "projection": self.projection,
            "visibility": self.visibility.to_dict(),
            "elements": [item.to_dict() for item in sorted(self.elements, key=element_sort_key)],
            "metadata": self.metadata,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    source_id: str
    values: tuple[Any, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class Schedule:
    schedule_id: str
    schedule_type: str
    title: str
    source_model_id: str
    columns: tuple[str, ...]
    rows: tuple[ScheduleRow, ...]
    level_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "schedule_type": self.schedule_type,
            "title": self.title,
            "source_model_id": self.source_model_id,
            "level_id": self.level_id,
            "columns": list(self.columns),
            "rows": [row.to_dict() for row in sorted(self.rows, key=lambda item: item.source_id)],
        }


@dataclass(frozen=True, slots=True)
class DrawingPackage:
    source_model_id: str
    source_schema_version: str
    views: tuple[DrawingView, ...] = ()
    schedules: tuple[Schedule, ...] = ()
    format_version: str = DRAWING_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "source_model_id": self.source_model_id,
            "source_schema_version": self.source_schema_version,
            "views": [view.to_dict() for view in sorted(self.views, key=lambda item: item.view_id)],
            "schedules": [item.to_dict() for item in sorted(self.schedules, key=lambda row: row.schedule_id)],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)
