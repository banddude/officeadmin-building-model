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
from .wall_evidence import (
    CHAIN_SNAP_TOLERANCE_PT,
    WALL_EVIDENCE_CLASSIFIER,
    WALL_EVIDENCE_HANDOFF_VERSION,
    source_wall_evidence,
)

__all__ = [
    "CHAIN_SNAP_TOLERANCE_PT",
    "ImportOptions",
    "LevelOverride",
    "RegionEvidence",
    "RegistrationHint",
    "ScaleOverride",
    "SheetWallEvidence",
    "StrokeStyleBin",
    "WALL_EVIDENCE_CLASSIFIER",
    "WALL_EVIDENCE_HANDOFF_VERSION",
    "WallStrokeStyle",
    "drawing_level_names",
    "import_architectural_pdf",
    "printed_sheet_scale",
    "region_wall_evidence",
    "select_lines_by_style",
    "sheet_wall_evidence",
    "source_wall_evidence",
    "stroke_style_histogram",
]
