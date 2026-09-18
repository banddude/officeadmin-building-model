"""Deterministic derived drawing views and schedules."""

from .generator import (
    AnnotationContext,
    AnnotationHook,
    DrawingGenerator,
    SymbolContext,
    SymbolHook,
    VisibilityFilter,
)
from .model import (
    Annotation,
    Bounds2,
    Dimension,
    DrawingPrimitive,
    DrawingSet,
    DrawingView,
    Point2,
    ProjectionMetadata,
    Schedule,
    ScheduleColumn,
    ScheduleRow,
    SourceReference,
)
from .projection import ProjectionFrame, clip_polygon, clip_polyline, clip_segment_to_depth
from .svg import render_svg, schedule_to_csv

__all__ = [
    "Annotation", "AnnotationContext", "AnnotationHook", "Bounds2", "Dimension",
    "DrawingGenerator", "DrawingPrimitive", "DrawingSet", "DrawingView", "Point2",
    "ProjectionFrame", "ProjectionMetadata", "Schedule", "ScheduleColumn", "ScheduleRow",
    "SourceReference", "SymbolContext", "SymbolHook", "VisibilityFilter", "clip_polygon",
    "clip_polyline", "clip_segment_to_depth", "render_svg", "schedule_to_csv",
]
