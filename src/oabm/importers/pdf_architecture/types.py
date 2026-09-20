"""Internal source observations and explicit import hints for architectural PDFs.

These types describe the PDF source, not a competing building model.  The only
semantic output of this lane is :class:`oabm.model.BuildingModel`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _point2(value: tuple[float, float], label: str) -> None:
    if len(value) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value):
        raise ValueError(f"{label} must contain two finite numbers")


@dataclass(frozen=True, slots=True)
class PdfTextObservation:
    element_id: str
    text: str
    bbox_pt: tuple[float, float, float, float]
    native_id: str | None = None
    font_size_pt: float | None = None

    def __post_init__(self) -> None:
        if self.font_size_pt is not None and (
            not math.isfinite(self.font_size_pt) or self.font_size_pt <= 0
        ):
            raise ValueError("font_size_pt must be > 0 when supplied")

    @property
    def center_pt(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox_pt
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


@dataclass(frozen=True, slots=True)
class PdfLineObservation:
    element_id: str
    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    native_id: str | None = None
    primitive_family: str = "line"
    dashed: bool = False

    def __post_init__(self) -> None:
        _point2(self.start_pt, "start_pt")
        _point2(self.end_pt, "end_pt")
        if self.start_pt == self.end_pt:
            raise ValueError("PDF line endpoints must differ")


@dataclass(frozen=True, slots=True)
class PdfRectObservation:
    element_id: str
    bbox_pt: tuple[float, float, float, float]
    native_id: str | None = None

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.bbox_pt
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in self.bbox_pt):
            raise ValueError("rectangle coordinates must be finite")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("rectangle must have positive width and height")

    @property
    def center_pt(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox_pt
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    @property
    def width_pt(self) -> float:
        return self.bbox_pt[2] - self.bbox_pt[0]

    @property
    def height_pt(self) -> float:
        return self.bbox_pt[3] - self.bbox_pt[1]


@dataclass(frozen=True, slots=True)
class PdfPageObservation:
    page_number: int
    width_pt: float
    height_pt: float
    texts: tuple[PdfTextObservation, ...] = ()
    lines: tuple[PdfLineObservation, ...] = ()
    rects: tuple[PdfRectObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfDocumentObservation:
    source_id: str
    content_sha256: str
    pages: tuple[PdfPageObservation, ...]


@dataclass(frozen=True, slots=True)
class ScaleOverride:
    """Explicit real-world metres per PDF point for one page."""

    page_number: int
    meters_per_point: float
    confidence: float = 1.0
    note: str = "explicit scale override"

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if not math.isfinite(self.meters_per_point) or self.meters_per_point <= 0:
            raise ValueError("meters_per_point must be > 0")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RegistrationHint:
    """Two corresponding control points establishing scale, rotation, and translation."""

    page_number: int
    source_a_pt: tuple[float, float]
    source_b_pt: tuple[float, float]
    model_a_m: tuple[float, float]
    model_b_m: tuple[float, float]
    confidence: float = 1.0
    note: str = "two-point control registration"

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        for label, value in (
            ("source_a_pt", self.source_a_pt),
            ("source_b_pt", self.source_b_pt),
            ("model_a_m", self.model_a_m),
            ("model_b_m", self.model_b_m),
        ):
            _point2(value, label)
        if self.source_a_pt == self.source_b_pt or self.model_a_m == self.model_b_m:
            raise ValueError("registration control points must be distinct")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class LevelOverride:
    page_number: int
    elevation_m: float
    name: str | None = None
    height_m: float | None = None
    confidence: float = 1.0
    note: str = "explicit level override"

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if not math.isfinite(self.elevation_m):
            raise ValueError("elevation_m must be finite")
        if self.height_m is not None and (not math.isfinite(self.height_m) or self.height_m <= 0):
            raise ValueError("height_m must be > 0 when supplied")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ImportOptions:
    scale_overrides: tuple[ScaleOverride, ...] = ()
    registrations: tuple[RegistrationHint, ...] = ()
    level_overrides: tuple[LevelOverride, ...] = ()
    default_wall_height_m: float | None = None
    assumed_value_confidence: float = 0.45
    min_wall_thickness_m: float = 0.0508
    max_wall_thickness_m: float = 0.4572
    min_space_span_m: float = 1.0
    max_opening_host_distance_m: float = 1.25
    scale_registration_tolerance: float = 0.02

    def __post_init__(self) -> None:
        if self.default_wall_height_m is not None and self.default_wall_height_m <= 0:
            raise ValueError("default_wall_height_m must be > 0")
        if not 0 <= self.assumed_value_confidence <= 1:
            raise ValueError("assumed_value_confidence must be between 0 and 1")
        if not (0 < self.min_wall_thickness_m < self.max_wall_thickness_m):
            raise ValueError("wall thickness limits are invalid")
        if self.min_space_span_m <= 0 or self.max_opening_host_distance_m <= 0:
            raise ValueError("distance limits must be > 0")
        if not 0 <= self.scale_registration_tolerance <= 1:
            raise ValueError("scale_registration_tolerance must be between 0 and 1")
