"""Parameterized new/existing scope from symbol stroke gray (#208).

Many drafting sets encode scope of work by stroking colour: new devices in
black, existing devices in a set-specific gray. This module carries the
deterministic, parameterized version of that rule as a read-only helper: it
consumes an already-extracted :class:`PdfElectricalDocument` and feeds nothing
back into the importer, so default extraction output stays byte-identical.

Each queried symbol position is classified by the stroke grays of the stroked
vector paths around it, weighted by path length, so a long stroke outweighs a
short tick. The rule complements the legend-letter scope reading recorded as
``scope_status``: where a set publishes no status legend (or leaves a device
unmarked), the drafting gray around the symbol can still resolve its scope,
and anything still undecided stays ambiguous or unknown instead of guessing.

All lengths are in PDF points, matching the vector observations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from oabm.importers.pdf_electrical.importer import PdfElectricalDocument

__all__ = [
    "SCOPE_FRACTION_THRESHOLD",
    "StrokeScopeRule",
    "StrokeScopeVerdict",
    "scope_by_stroke",
]

#: Share of nearby stroked path length a gray range must hold for its scope.
SCOPE_FRACTION_THRESHOLD = 0.8

# The stroking paint operators, as the str values recorded in the
# ``paint_operator`` metadata key. Mirrors the importer's
# ``_STROKING_PAINT_OPERATORS`` (which holds the raw bytes operators); the
# PDF stroking operators are fixed by the PDF specification.
_STROKED_PAINT_OPERATORS = frozenset({"S", "s", "B", "B*", "b", "b*"})


@dataclass(frozen=True, slots=True)
class StrokeScopeRule:
    """Gray ranges that separate new work from existing work on a set.

    ``new`` covers ``[0, new_gray_max]`` and ``existing`` covers
    ``[existing_gray_min, existing_gray_max]``, both inclusive. The ranges
    must not overlap so a stroke gray can never satisfy both; grays between
    the ranges (or outside them) count toward neither scope.
    """

    new_gray_max: float = 0.1
    existing_gray_min: float = 0.2
    existing_gray_max: float = 0.5

    def __post_init__(self) -> None:
        values = (self.new_gray_max, self.existing_gray_min, self.existing_gray_max)
        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError("stroke scope rule bounds must be finite numbers")
        if not (
            0.0
            <= self.new_gray_max
            < self.existing_gray_min
            <= self.existing_gray_max
            <= 1.0
        ):
            raise ValueError(
                "stroke scope rule requires 0 <= new_gray_max < existing_gray_min"
                " <= existing_gray_max <= 1 so the ranges cannot overlap"
            )


@dataclass(frozen=True, slots=True)
class StrokeScopeVerdict:
    """Scope reading for one queried symbol position.

    ``scope`` is ``new``, ``existing``, ``ambiguous``, or ``unknown``. The
    fractions are each range's share of the total length of the nearby
    stroked paths; paths without a usable stroke gray stay in the total and
    so dilute both fractions rather than being silently dropped. ``unknown``
    reports zero fractions; ``path_count`` is the number of nearby stroked
    paths the verdict considered.
    """

    scope: str
    fraction_new: float
    fraction_existing: float
    path_count: int


def _usable_gray(value: object) -> float | None:
    """The stroke gray a metadata value carries, or ``None`` when unusable."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    gray = float(value)
    return gray if math.isfinite(gray) else None


def _closed_segments(
    points_pt: tuple[tuple[float, float], ...],
    *,
    closed: bool,
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    """The path's segments, including the closing segment when closed."""

    segments = tuple(zip(points_pt, points_pt[1:]))
    if closed and len(points_pt) >= 3:
        segments += ((points_pt[-1], points_pt[0]),)
    return segments


def _path_length_pt(segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...]) -> float:
    """Polyline length of a path's segments."""

    return sum(
        math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in segments
    )


def _within_radius_pt(
    segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...],
    x_pt: float,
    y_pt: float,
    radius_pt: float,
) -> bool:
    """Whether any point of the path lies within ``radius_pt`` of the query.

    Distance is measured to the path's segments, so a query near the middle
    of a long straight stroke still finds it, not only near its vertices.
    """

    if not math.isfinite(radius_pt) or radius_pt < 0.0:
        return False
    radius_sq = radius_pt * radius_pt
    for (x1, y1), (x2, y2) in segments:
        dx = x2 - x1
        dy = y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq <= 0.0:
            distance_sq = (x_pt - x1) ** 2 + (y_pt - y1) ** 2
        else:
            t = ((x_pt - x1) * dx + (y_pt - y1) * dy) / length_sq
            t = min(1.0, max(0.0, t))
            distance_sq = (x_pt - (x1 + t * dx)) ** 2 + (y_pt - (y1 + t * dy)) ** 2
        if distance_sq <= radius_sq:
            return True
    return False


def scope_by_stroke(
    document: PdfElectricalDocument,
    points_pt: Sequence[tuple[int, float, float]],
    rule: StrokeScopeRule,
    *,
    radius_pt: float = 6.0,
) -> tuple[StrokeScopeVerdict, ...]:
    """Classify each ``(page, x, y)`` symbol position by nearby stroke gray.

    For every query point, the stroked vector paths on that page with any
    point within ``radius_pt`` are collected and their stroke grays are
    weighted by path length:

    - ``new`` when at least ``SCOPE_FRACTION_THRESHOLD`` of the length falls
      in ``[0, rule.new_gray_max]``;
    - ``existing`` when at least ``SCOPE_FRACTION_THRESHOLD`` of the length
      falls in ``[rule.existing_gray_min, rule.existing_gray_max]``;
    - ``ambiguous`` when neither range reaches the threshold, including when
      nearby strokes carry grays between or outside the rule's ranges;
    - ``unknown`` when no nearby stroked path carries a usable stroke gray
      (including when there are no nearby stroked paths at all, and when the
      nearby paths carry no length to weigh).

    Only stroked paths count: a path painted with a filling operator carries
    no scope evidence here, and a stroked path whose ``paint_operator``
    metadata is missing cannot be proven stroked and is ignored. Verdicts are
    returned in query order. The function never raises on document data; a
    page with no match simply reads ``unknown``.
    """

    by_page: dict[int, list[tuple[float, float | None, tuple]]] = {}
    for path in document.vectors:
        if not isinstance(path.metadata, Mapping):
            continue
        if path.metadata.get("paint_operator") not in _STROKED_PAINT_OPERATORS:
            continue
        segments = _closed_segments(path.points_pt, closed=path.closed)
        by_page.setdefault(path.page, []).append(
            (
                _path_length_pt(segments),
                _usable_gray(path.metadata.get("stroke_gray")),
                segments,
            )
        )

    verdicts: list[StrokeScopeVerdict] = []
    for page, x_pt, y_pt in points_pt:
        nearby = [
            (length, gray)
            for length, gray, segments in by_page.get(page, ())
            if _within_radius_pt(segments, x_pt, y_pt, radius_pt)
        ]
        total_length = sum(length for length, _ in nearby)
        new_length = 0.0
        existing_length = 0.0
        carries_gray = False
        for length, gray in nearby:
            if gray is None:
                continue
            carries_gray = True
            if 0.0 <= gray <= rule.new_gray_max:
                new_length += length
            elif rule.existing_gray_min <= gray <= rule.existing_gray_max:
                existing_length += length
        if not nearby or not carries_gray or total_length <= 0.0:
            verdicts.append(StrokeScopeVerdict("unknown", 0.0, 0.0, len(nearby)))
            continue
        fraction_new = new_length / total_length
        fraction_existing = existing_length / total_length
        if fraction_new >= SCOPE_FRACTION_THRESHOLD:
            scope = "new"
        elif fraction_existing >= SCOPE_FRACTION_THRESHOLD:
            scope = "existing"
        else:
            scope = "ambiguous"
        verdicts.append(
            StrokeScopeVerdict(scope, fraction_new, fraction_existing, len(nearby))
        )
    return tuple(verdicts)
