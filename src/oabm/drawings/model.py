from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from typing import Any


def _clean_float(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in sorted(value.items())}
    if isinstance(value, float):
        return _clean_float(value)
    return value


@dataclass(frozen=True, slots=True, order=True)
class Point2:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Bounds2:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError("invalid 2D bounds")

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def expanded(self, margin: float) -> Bounds2:
        if margin < 0:
            raise ValueError("margin must be non-negative")
        return Bounds2(
            min_x=self.min_x - margin,
            min_y=self.min_y - margin,
            max_x=self.max_x + margin,
            max_y=self.max_y + margin,
        )


@dataclass(frozen=True, slots=True)
class SourceReference:
    entity_id: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DrawingPrimitive:
    id: str
    kind: str
    layer: str
    points: tuple[Point2, ...]
    source: SourceReference
    closed: bool = False
    label: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Annotation:
    id: str
    text: str
    position: Point2
    source: SourceReference | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Dimension:
    id: str
    start: Point2
    end: Point2
    value_m: float
    label: str
    source_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectionMetadata:
    origin: tuple[float, float, float]
    horizontal_axis: tuple[float, float, float]
    vertical_axis: tuple[float, float, float]
    depth_axis: tuple[float, float, float]
    depth_min_m: float | None = None
    depth_max_m: float | None = None


@dataclass(frozen=True, slots=True)
class DrawingView:
    id: str
    name: str
    kind: str
    model_id: str
    projection: ProjectionMetadata
    clip_bounds: Bounds2
    primitives: tuple[DrawingPrimitive, ...] = ()
    annotations: tuple[Annotation, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ScheduleColumn:
    key: str
    title: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    source: SourceReference
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    name: str
    model_id: str
    columns: tuple[ScheduleColumn, ...]
    rows: tuple[ScheduleRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)


@dataclass(frozen=True, slots=True)
class DrawingSet:
    model_id: str
    model_schema_version: str
    views: tuple[DrawingView, ...]
    schedules: tuple[Schedule, ...]

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)
