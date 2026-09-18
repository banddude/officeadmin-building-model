"""Deterministic drawing views derived from the canonical semantic model.

The canonical :mod:`oabm.model` document remains the source of truth. Objects in
this package are disposable, traceable view products and schedules.
"""

from .generator import (
    AnnotationProvider,
    DefaultAnnotationProvider,
    DefaultSymbolProvider,
    SymbolProvider,
    generate_package,
    generate_standard_package,
    generate_view,
)
from .projection import ProjectionFrame, elevation_frame, plan_frame, section_frame
from .schedules import SUPPORTED_SCHEDULES, generate_schedule, generate_standard_schedules
from .types import (
    DRAWING_FORMAT_VERSION,
    DimensionElement,
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
    ScheduleRow,
    SectionViewSpec,
    SymbolElement,
    Visibility,
)

__all__ = [
    "DRAWING_FORMAT_VERSION",
    "AnnotationProvider",
    "DefaultAnnotationProvider",
    "DefaultSymbolProvider",
    "DimensionElement",
    "DrawingError",
    "DrawingPackage",
    "DrawingView",
    "ElevationViewSpec",
    "LabelElement",
    "PlanViewSpec",
    "Point2",
    "PolylineElement",
    "ProjectionFrame",
    "Rect2",
    "SUPPORTED_SCHEDULES",
    "Schedule",
    "ScheduleRow",
    "SectionViewSpec",
    "SymbolElement",
    "SymbolProvider",
    "Visibility",
    "elevation_frame",
    "generate_package",
    "generate_schedule",
    "generate_standard_package",
    "generate_standard_schedules",
    "generate_view",
    "plan_frame",
    "section_frame",
]
