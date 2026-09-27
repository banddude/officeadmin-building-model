"""Architectural PDF ingestion into the canonical building model."""

from .drawing_regions import RegionEvidence
from .importer import (
    SheetWallEvidence,
    drawing_level_names,
    import_architectural_pdf,
    printed_sheet_scale,
    region_wall_evidence,
    sheet_wall_evidence,
)
from .stroke_styles import (
    StrokeStyleBin,
    WallStrokeStyle,
    select_lines_by_style,
    stroke_style_histogram,
)
from .types import ImportOptions, LevelOverride, RegistrationHint, ScaleOverride

__all__ = [
    "ImportOptions",
    "LevelOverride",
    "RegionEvidence",
    "RegistrationHint",
    "ScaleOverride",
    "SheetWallEvidence",
    "StrokeStyleBin",
    "WallStrokeStyle",
    "drawing_level_names",
    "import_architectural_pdf",
    "printed_sheet_scale",
    "region_wall_evidence",
    "select_lines_by_style",
    "sheet_wall_evidence",
    "stroke_style_histogram",
]
