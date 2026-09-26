"""Shared PDF page display-space coordinate normalization helpers.

PDF coordinates are reported in raw user space, which ignores the page
/Rotate entry; recognition in every lane assumes the orientation a viewer
displays. Both the electrical and the architecture extractor map source
positions through these helpers before creating observations, so both lanes
agree on displayed, bottom-origin page space.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# AutoCAD SHX-font strings arrive in PDF exports as /Square annotations whose
# /T entry carries this author string. Lanes match on it to accept CAD text
# while ignoring reviewer markups, and must not copy the author string itself
# into observations or model attributes.
SHX_TEXT_ANNOTATION_AUTHOR = "AutoCAD SHX Text"

_SUPPORTED_PAGE_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass(frozen=True, slots=True)
class PdfPageDisplayTransform:
    """Map one PDF page from raw user space into displayed page space."""

    page_rotation: int
    source_width_pt: float
    source_height_pt: float
    displayed_width_pt: float
    displayed_height_pt: float

    def apply(self, x_pt: float, y_pt: float) -> tuple[float, float]:
        """Return bottom-origin displayed coordinates for one raw PDF point."""

        x = float(x_pt)
        y = float(y_pt)
        if self.page_rotation == 0:
            return x, y
        if self.page_rotation == 90:
            result = (y, self.source_width_pt - x)
        elif self.page_rotation == 180:
            result = (self.source_width_pt - x, self.source_height_pt - y)
        elif self.page_rotation == 270:
            result = (self.source_height_pt - y, x)
        else:
            raise ValueError(f"unsupported PDF page rotation {self.page_rotation}")
        # Quarter-turn transforms can introduce binary floating-point noise from
        # subtraction even when the displayed coordinate is exactly representable
        # in the source PDF. Keep deterministic point-space identity stable.
        return round(result[0], 9), round(result[1], 9)

    def provenance_attributes(self) -> dict[str, float | int | str]:
        return {
            "page_rotation": self.page_rotation,
            "displayed_page_width_pt": self.displayed_width_pt,
            "displayed_page_height_pt": self.displayed_height_pt,
            "coordinate_space": "displayed",
        }


def _resolved_pdf_value(value: Any) -> Any:
    try:
        return value.get_object()
    except AttributeError:
        return value


def page_display_transform(page: Any) -> PdfPageDisplayTransform:
    """Read /Rotate and page size, then return the displayed-space transform."""

    raw_rotation = _resolved_pdf_value(page.get("/Rotate", 0))
    try:
        numeric_rotation = int(raw_rotation or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid PDF page /Rotate value {raw_rotation!r}") from exc

    rotation = numeric_rotation % 360
    if rotation not in _SUPPORTED_PAGE_ROTATIONS:
        raise ValueError(
            "PDF page /Rotate must resolve to one of 0, 90, 180, or 270 degrees"
        )

    source_width = float(page.mediabox.width)
    source_height = float(page.mediabox.height)
    if source_width <= 0.0 or source_height <= 0.0:
        raise ValueError("PDF page dimensions must be positive")

    if rotation in {90, 270}:
        displayed_width = source_height
        displayed_height = source_width
    else:
        displayed_width = source_width
        displayed_height = source_height

    return PdfPageDisplayTransform(
        page_rotation=rotation,
        source_width_pt=source_width,
        source_height_pt=source_height,
        displayed_width_pt=displayed_width,
        displayed_height_pt=displayed_height,
    )
