from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Iterable

OUTPUT_VERSION = "1.0.0"


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _q(value: float) -> float:
    """Quantize derived coordinates so equal canonical input serializes identically."""
    value = _finite(value, "coordinate")
    result = round(value, 9)
    return 0.0 if result == -0.0 else result


@dataclass(frozen=True, slots=True, order=True)
class Point2:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _q(self.x))
        object.__setattr__(self, "y", _q(self.y))


@dataclass(frozen=True, slots=True)
class Bounds2:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        values = tuple(_q(v) for v in (self.min_x, self.min_y, self.max_x, self.max_y))
        object.__setattr__(self, "min_x", values[0])
        object.__setattr__(self, "min_y", values[1])
        object.__setattr__(self, "max_x", values[2])
        object.__setattr__(self, "max_y", values[3])
        if self.min_x >= self.max_x or self.min_y >= self.max_y:
            raise ValueError("bounds must have positive width and height")

    @property
    def width(self) -> float:
        return _q(self.max_x - self.min_x)

    @property
    def height(self) -> float:
        return _q(self.max_y - self.min_y)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    source_kind: str
    source_id: str
    source_element_id: str | None
    page: int | None
    method: str | None
    confidence: float
    derivation: str | None = None


@dataclass(frozen=True, slots=True)
class SourceReference:
    canonical_id: str
    name: str | None
    confidence: float
    provenance: tuple[SourceProvenance, ...] = ()


@dataclass(frozen=True, slots=True)
class LineStyle:
    stroke: str = "object"
    weight: str = "normal"
    pattern: str = "solid"


@dataclass(frozen=True, slots=True)
class DrawingPrimitive:
    """A derived vector primitive. ``source_ids`` are canonical-model identities."""

    id: str
    kind: str
    layer: str
    source_ids: tuple[str, ...]
    points: tuple[Point2, ...]
    closed: bool = False
    style: LineStyle = field(default_factory=LineStyle)
    text: str | None = None
    symbol: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.kind or not self.layer:
            raise ValueError("primitive id, kind, and layer are required")
        if not self.source_ids:
            raise ValueError("derived primitives must reference canonical source IDs")
        if self.kind in {"line", "polyline", "polygon"} and len(self.points) < 2:
            raise ValueError(f"{self.kind} requires at least two points")
        if self.kind in {"symbol", "text"} and len(self.points) != 1:
            raise ValueError(f"{self.kind} requires exactly one anchor point")
        if self.kind == "text" and self.text is None:
            raise ValueError("text primitive requires text")
        if self.kind == "symbol" and self.symbol is None:
            raise ValueError("symbol primitive requires a symbol token")


@dataclass(frozen=True, slots=True)
class DrawingDimension:
    id: str
    source_ids: tuple[str, ...]
    start: Point2
    end: Point2
    offset_m: float
    value_m: float
    text: str

    def __post_init__(self) -> None:
        if not self.source_ids:
            raise ValueError("dimensions must reference canonical source IDs")
        object.__setattr__(self, "offset_m", _q(self.offset_m))
        object.__setattr__(self, "value_m", _q(self.value_m))
        if self.value_m < 0:
            raise ValueError("dimension value cannot be negative")


@dataclass(frozen=True, slots=True)
class DrawingView:
    id: str
    view_type: str
    title: str
    bounds: Bounds2
    scale: float
    primitives: tuple[DrawingPrimitive, ...]
    dimensions: tuple[DrawingDimension, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.view_type not in {"plan", "elevation", "section"}:
            raise ValueError(f"unsupported view_type {self.view_type!r}")
        scale = _finite(self.scale, "scale")
        if scale <= 0:
            raise ValueError("scale must be > 0")
        object.__setattr__(self, "scale", _q(scale))
        ids = [item.id for item in self.primitives]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate primitive id in view {self.id}")


@dataclass(frozen=True, slots=True)
class ScheduleColumn:
    key: str
    title: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    source_id: str
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DrawingSchedule:
    id: str
    title: str
    columns: tuple[ScheduleColumn, ...]
    rows: tuple[ScheduleRow, ...]

    def __post_init__(self) -> None:
        width = len(self.columns)
        if len({column.key for column in self.columns}) != width:
            raise ValueError(f"duplicate column key in schedule {self.id}")
        for row in self.rows:
            if len(row.cells) != width:
                raise ValueError(f"row {row.source_id} has {len(row.cells)} cells; expected {width}")


@dataclass(frozen=True, slots=True)
class DrawingSet:
    model_id: str
    views: tuple[DrawingView, ...]
    schedules: tuple[DrawingSchedule, ...]
    source_index: tuple[SourceReference, ...]
    output_version: str = OUTPUT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


def union_bounds(points: Iterable[Point2], *, padding: float = 0.5) -> Bounds2:
    points = tuple(points)
    if not points:
        return Bounds2(min_x=-0.5, min_y=-0.5, max_x=0.5, max_y=0.5)
    min_x = min(p.x for p in points)
    max_x = max(p.x for p in points)
    min_y = min(p.y for p in points)
    max_y = max(p.y for p in points)
    if math.isclose(min_x, max_x, abs_tol=1e-9):
        min_x -= 0.5
        max_x += 0.5
    if math.isclose(min_y, max_y, abs_tol=1e-9):
        min_y -= 0.5
        max_y += 0.5
    return Bounds2(
        min_x=min_x - padding,
        min_y=min_y - padding,
        max_x=max_x + padding,
        max_y=max_y + padding,
    )


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
