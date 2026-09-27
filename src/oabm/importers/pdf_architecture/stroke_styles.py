"""Stroke-style histogram and style-filtered line selection for one page.

Proven drafting cues are set-specific: walls are drawn with one heavy stroke
weight and existing work at one gray.  This module summarizes the observed
stroke styles of a page's line observations into a compact, deterministic
histogram, and applies a chosen style to the page as a deterministic filter,
so style-based wall selection is reproducible from public code.

Everything here is read-only observation-level evidence.  The importer does
not call any of it, imported models are unchanged, and identical pages give
identical results.  A line whose width or gray the source did not carry is
summarized in a ``None`` bucket of the histogram and never matches a
:class:`WallStrokeStyle` range: unknown style fails closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import PdfLineObservation, PdfPageObservation


def _style_bucket(value: float, step: float) -> float:
    """The nearest multiple of ``step`` (deterministic half-even rounding).

    The final re-round snaps the product back onto the decimal grid, so a
    bucket value never carries binary-float noise and identical inputs always
    compare equal.
    """

    return round(round(value / step) * step, 10)


def _line_length_pt(line: PdfLineObservation) -> float:
    x0, y0 = line.start_pt
    x1, y1 = line.end_pt
    return math.hypot(x1 - x0, y1 - y0)


def _axis_key(value: float | None) -> tuple[int, float]:
    """Ascending sort key for one bucket axis, ``None`` before any number."""

    return (0, 0.0) if value is None else (1, value)


@dataclass(frozen=True, slots=True)
class StrokeStyleBin:
    """One ``(width bucket, gray bucket, dashed)`` cell of a page histogram.

    The bucket values are the style actually carried by the bin's lines:
    multiples of the histogram step, or ``None`` when the source carried no
    width or no gray on that axis.
    """

    line_width_pt: float | None
    stroke_gray: float | None
    dashed: bool
    line_count: int
    total_length_pt: float
    longest_pt: float

    def to_dict(self) -> dict[str, float | int | bool | None]:
        """A JSON-ready mapping for a compact style summary."""

        return {
            "line_width_pt": self.line_width_pt,
            "stroke_gray": self.stroke_gray,
            "dashed": self.dashed,
            "line_count": self.line_count,
            "total_length_pt": self.total_length_pt,
            "longest_pt": self.longest_pt,
        }


def _bin_sort_key(summary: StrokeStyleBin) -> tuple[float, tuple[int, float], tuple[int, float], bool]:
    return (
        -summary.total_length_pt,
        _axis_key(summary.line_width_pt),
        _axis_key(summary.stroke_gray),
        summary.dashed,
    )


def stroke_style_histogram(
    page: PdfPageObservation,
    *,
    width_step_pt: float = 0.01,
    gray_step: float = 0.05,
) -> tuple[StrokeStyleBin, ...]:
    """Histogram a page's line observations by observed stroke style.

    One bin per ``(width bucket, gray bucket, dashed)`` holding its
    ``line_count``, ``total_length_pt``, and ``longest_pt``.  Width and gray
    buckets round half-even to the step; a line with no carried width or gray
    falls in a bin whose value is ``None`` on that axis.  Bins sort by
    ``total_length_pt`` descending, then by the bin key ascending with
    ``None`` before any number, so the heaviest drawn style comes first and
    identical pages give an identical tuple.
    """

    if not math.isfinite(width_step_pt) or width_step_pt <= 0:
        raise ValueError("width_step_pt must be > 0")
    if not math.isfinite(gray_step) or gray_step <= 0:
        raise ValueError("gray_step must be > 0")

    buckets: dict[tuple[float | None, float | None, bool], list[float]] = {}
    for line in page.lines:
        width = (
            None
            if line.line_width_pt is None
            else _style_bucket(line.line_width_pt, width_step_pt)
        )
        gray = None if line.stroke_gray is None else _style_bucket(line.stroke_gray, gray_step)
        buckets.setdefault((width, gray, line.dashed), []).append(_line_length_pt(line))

    return tuple(
        sorted(
            (
                StrokeStyleBin(
                    line_width_pt=key[0],
                    stroke_gray=key[1],
                    dashed=key[2],
                    line_count=len(lengths),
                    total_length_pt=sum(lengths),
                    longest_pt=max(lengths),
                )
                for key, lengths in buckets.items()
            ),
            key=_bin_sort_key,
        )
    )


@dataclass(frozen=True, slots=True)
class WallStrokeStyle:
    """The stroke style one drawn family (walls, existing work) uses.

    Every bound is closed.  A line matches only when its observed width and
    gray both fall inside the ranges, it is dashed exactly when ``dashed`` is
    true, and its length reaches ``min_length_pt``.  A line with unknown width
    or unknown gray matches nothing: unknown style fails closed.
    """

    width_min_pt: float
    width_max_pt: float
    gray_min: float = 0.0
    gray_max: float = 1.0
    dashed: bool = False
    min_length_pt: float = 0.0

    def __post_init__(self) -> None:
        for label, value in (
            ("width_min_pt", self.width_min_pt),
            ("width_max_pt", self.width_max_pt),
            ("gray_min", self.gray_min),
            ("gray_max", self.gray_max),
            ("min_length_pt", self.min_length_pt),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
        if not 0 <= self.width_min_pt <= self.width_max_pt:
            raise ValueError("width bounds must satisfy 0 <= width_min_pt <= width_max_pt")
        if not 0 <= self.gray_min <= 1 or not 0 <= self.gray_max <= 1:
            raise ValueError("gray bounds must be between 0 and 1")
        if self.gray_min > self.gray_max:
            raise ValueError("gray_min must be <= gray_max")
        if self.min_length_pt < 0:
            raise ValueError("min_length_pt must be >= 0")

    def matches(self, line: PdfLineObservation) -> bool:
        """Whether one line observation is drawn in exactly this style."""

        if line.line_width_pt is None or line.stroke_gray is None:
            return False
        if not self.width_min_pt <= line.line_width_pt <= self.width_max_pt:
            return False
        if not self.gray_min <= line.stroke_gray <= self.gray_max:
            return False
        if line.dashed is not self.dashed:
            return False
        return _line_length_pt(line) >= self.min_length_pt


def select_lines_by_style(
    page: PdfPageObservation,
    style: WallStrokeStyle,
) -> tuple[PdfLineObservation, ...]:
    """The page's lines drawn in ``style``, in the page's existing line order."""

    return tuple(line for line in page.lines if style.matches(line))
