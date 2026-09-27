"""Detect floor/ceiling line bands in building-section drawings.

This module works only in sheet-local PDF points.  It reads the horizontal
line segments of one page's :class:`PdfPageObservation` and groups them into
the slab bands a building section draws at each level: two strong horizontal
lines a slab thickness apart.  The upper line of a band is the finished floor
(walking surface); the lower line is the ceiling/soffit below the slab, so
floor-to-floor spacing is ceiling height plus slab thickness.

The companion :func:`level_elevations_from_bands` turns bands into finished
floor elevations in metres relative to a datum floor, optionally validated
against dimensioned ceiling heights: the lower line of each band must sit one
ceiling height above the floor below.

Everything here is observation-level evidence.  The importer owns level,
scale, identity, and canonical promotion; nothing in this module is wired
into the importer yet.  Results are deterministic and sorted, and unclear
input fails closed to empty results with reason-coded warnings instead of
raising on data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .types import PdfLineObservation, PdfPageObservation

#: Maximum |dy| in points for a segment to count as horizontal.
MAX_HORIZONTAL_SLOPE_PT = 0.5

#: Default minimum line length as a fraction of the region width.  Floor and
#: ceiling lines span the drawn section, so a fraction well above the length
#: of text underlines, hatch strokes, and dimension ticks rejects clutter
#: without a tuned absolute value.  Pass ``min_length_pt`` explicitly for
#: narrow sections.
DEFAULT_MIN_LENGTH_FRACTION = 0.35

#: Default y clustering tolerance in points.
DEFAULT_CLUSTER_TOL_PT = 1.0

#: Slab thickness in points: two strong lines at most this far apart form one
#: band.  Storey spacing is an order of magnitude larger, so this separates
#: slab pairs from floor-to-floor gaps.
DEFAULT_MAX_SLAB_GAP_PT = 30.0

#: A cluster must carry at least this fraction of the strongest cluster's
#: total length to take part in a band.
DEFAULT_MIN_STRENGTH_FRACTION = 0.25

#: Tolerance in metres when checking a band's lower line against a dimensioned
#: ceiling height.  Covers dimension rounding plus small line-placement noise.
DEFAULT_CEILING_TOLERANCE_M = 0.05


@dataclass(frozen=True, slots=True)
class SectionLevels:
    """Horizontal level evidence for one section region.

    ``bands`` holds ``(y_top_pt, y_bottom_pt, total_len_pt)`` tuples sorted by
    y; ``y_top_pt`` is the finished-floor line and ``y_bottom_pt`` the ceiling
    line below the slab.  ``lines`` holds the horizontal candidate segments
    that passed the length filter, sorted.  ``warnings`` carries reason-coded
    strings; fewer than two bands cannot evidence level elevations.
    """

    bands: tuple[tuple[float, float, float], ...]
    lines: tuple[PdfLineObservation, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LevelElevation:
    """One finished-floor elevation relative to the datum floor.

    ``warnings`` carries reason-coded validation results, including ceiling
    height mismatches for the storey below this level.
    """

    level_index: int
    elevation_m: float
    band: tuple[float, float, float]
    warnings: tuple[str, ...] = ()


Candidate = tuple[float, float, PdfLineObservation]


def _cluster_mean_y(segments: list[Candidate]) -> float:
    weight = sum(item[0] for item in segments)
    if weight <= 0:
        return segments[0][1]
    return sum(item[0] * item[1] for item in segments) / weight


def _horizontal_candidates(
    page: PdfPageObservation,
    region_pt: tuple[float, float, float, float],
    min_length_pt: float,
) -> list[Candidate]:
    """Horizontal segments of usable length inside the region, sorted.

    Each entry is ``(length_pt, center_y_pt, line)``; sorting keeps every
    downstream step deterministic regardless of observation order.
    """

    x0, y0, x1, y1 = region_pt
    found: list[Candidate] = []
    for line in page.lines:
        (ax, ay), (bx, by) = line.start_pt, line.end_pt
        if not (x0 <= ax <= x1 and x0 <= bx <= x1 and y0 <= ay <= y1 and y0 <= by <= y1):
            continue
        if abs(by - ay) > MAX_HORIZONTAL_SLOPE_PT:
            continue
        length = math.dist(line.start_pt, line.end_pt)
        if length < min_length_pt:
            continue
        found.append((length, (ay + by) / 2.0, line))
    found.sort(
        key=lambda item: (
            item[1],
            min(item[2].start_pt[0], item[2].end_pt[0]),
            max(item[2].start_pt[0], item[2].end_pt[0]),
            item[2].element_id,
        )
    )
    return found


def _cluster_by_y(
    candidates: list[Candidate],
    cluster_tol_pt: float,
) -> list[tuple[float, float]]:
    """Chain segments into y clusters; returns ``(mean_y_pt, total_len_pt)``.

    Clusters come back sorted by y because the input is sorted.
    """

    clusters: list[list[Candidate]] = []
    for item in candidates:
        if clusters and abs(item[1] - _cluster_mean_y(clusters[-1])) <= cluster_tol_pt:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return [(_cluster_mean_y(cluster), sum(item[0] for item in cluster)) for cluster in clusters]


def find_section_level_lines(
    page: PdfPageObservation,
    region_pt: tuple[float, float, float, float] | None = None,
    *,
    min_length_pt: float | None = None,
    cluster_tol_pt: float = DEFAULT_CLUSTER_TOL_PT,
    max_slab_gap_pt: float = DEFAULT_MAX_SLAB_GAP_PT,
    min_strength_fraction: float = DEFAULT_MIN_STRENGTH_FRACTION,
) -> SectionLevels:
    """Find floor/ceiling bands in one building-section region.

    The default ``min_length_pt`` is :data:`DEFAULT_MIN_LENGTH_FRACTION` of
    the region width.  Horizontal candidate segments are clustered by y
    within ``cluster_tol_pt``, clusters are weighted by total length, and
    strong clusters at most ``max_slab_gap_pt`` apart are paired into bands.
    Never raises on data: unusable regions yield empty results with warnings.
    """

    warnings: list[str] = []
    box = region_pt if region_pt is not None else (0.0, 0.0, float(page.width_pt), float(page.height_pt))
    if (
        len(box) != 4
        or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in box)
        or box[2] <= box[0]
        or box[3] <= box[1]
    ):
        return SectionLevels(
            bands=(),
            lines=(),
            warnings=("invalid_region: region_pt must be finite with positive width and height",),
        )

    resolved_min_length = (
        DEFAULT_MIN_LENGTH_FRACTION * (box[2] - box[0]) if min_length_pt is None else min_length_pt
    )
    if not math.isfinite(resolved_min_length) or resolved_min_length < 0:
        return SectionLevels(
            bands=(),
            lines=(),
            warnings=("invalid_min_length: min_length_pt must be finite and >= 0",),
        )

    candidates = _horizontal_candidates(page, box, resolved_min_length)
    if not candidates:
        return SectionLevels(
            bands=(),
            lines=(),
            warnings=(
                "no_horizontal_lines: no horizontal segment of length >= "
                f"{resolved_min_length:.2f} pt in the region",
            ),
        )

    clusters = _cluster_by_y(candidates, cluster_tol_pt)
    strongest = max(total for _, total in clusters)
    strong_indexes = [
        index
        for index, (_, total) in enumerate(clusters)
        if total >= min_strength_fraction * strongest
    ]

    bands: list[tuple[float, float, float]] = []
    position = 0
    while position < len(strong_indexes):
        following = position + 1
        if following < len(strong_indexes):
            gap = clusters[strong_indexes[following]][0] - clusters[strong_indexes[position]][0]
            if 0 < gap <= max_slab_gap_pt:
                bands.append((
                    clusters[strong_indexes[following]][0],
                    clusters[strong_indexes[position]][0],
                    clusters[strong_indexes[position]][1] + clusters[strong_indexes[following]][1],
                ))
                position += 2
                continue
        position += 1

    if len(bands) < 2:
        warnings.append(
            "fewer_than_two_bands: "
            f"{len(bands)} band(s) detected from {len(clusters)} cluster(s); "
            "at least two are needed to evidence level elevations"
        )
    return SectionLevels(
        bands=tuple(bands),
        lines=tuple(item[2] for item in candidates),
        warnings=tuple(warnings),
    )


def level_elevations_from_bands(
    bands: Sequence[tuple[float, float, float]],
    *,
    scale_m_per_pt: float,
    datum: str | int = "lowest",
    ceiling_heights_m: Sequence[float] | None = None,
    tolerance_m: float = DEFAULT_CEILING_TOLERANCE_M,
) -> list[LevelElevation]:
    """Turn slab bands into finished-floor elevations in metres.

    Elevations are relative to the datum floor: ``datum="lowest"`` (default)
    or an integer band index.  When ``ceiling_heights_m`` is given (one
    height per storey, lowest first), the lower line of each band must sit
    one ceiling height above the floor below within ``tolerance_m``;
    mismatches are flagged in that level's warnings.  Fewer than two usable
    bands, or an unusable scale, fail closed to an empty list.  Never raises
    on data; unusable band entries are dropped.
    """

    if not isinstance(scale_m_per_pt, (int, float)) or not math.isfinite(scale_m_per_pt) or scale_m_per_pt <= 0:
        return []
    usable: list[tuple[float, float, float]] = []
    for band in bands or ():
        if not isinstance(band, tuple) or len(band) != 3:
            continue
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in band):
            continue
        y_top, y_bottom, total_len = band
        if y_top <= y_bottom:
            continue
        usable.append((float(y_top), float(y_bottom), float(total_len)))
    usable.sort()
    if len(usable) < 2:
        return []

    if datum == "lowest":
        base_y = usable[0][0]
    elif isinstance(datum, bool) or not isinstance(datum, int) or not 0 <= datum < len(usable):
        raise ValueError('datum must be "lowest" or a band index within range')
    else:
        base_y = usable[datum][0]

    notes: dict[int, list[str]] = {index: [] for index in range(len(usable))}
    if ceiling_heights_m is not None:
        heights = list(ceiling_heights_m)
        if len(heights) != len(usable) - 1:
            note = (
                "ceiling_height_count_mismatch: "
                f"{len(heights)} ceiling height(s) for {len(usable) - 1} storey(s); "
                "checked the overlapping pairs only"
            )
            for index in range(1, len(usable)):
                notes[index].append(note)
        for position in range(1, len(usable)):
            if position - 1 >= len(heights):
                continue
            expected = heights[position - 1]
            if not isinstance(expected, (int, float)) or not math.isfinite(expected) or expected <= 0:
                notes[position].append(
                    f"ceiling_height_not_usable: storey {position - 1} height {expected!r} "
                    "is not a finite positive number"
                )
                continue
            measured = (usable[position][1] - usable[position - 1][0]) * scale_m_per_pt
            if abs(measured - float(expected)) > tolerance_m:
                notes[position].append(
                    f"ceiling_height_mismatch: storey {position - 1} dimensioned "
                    f"{float(expected):.3f} m but the band lower line measures "
                    f"{measured:.3f} m (tolerance {tolerance_m:.3f} m)"
                )

    return [
        LevelElevation(
            level_index=index,
            elevation_m=(band[0] - base_y) * scale_m_per_pt,
            band=band,
            warnings=tuple(notes[index]),
        )
        for index, band in enumerate(usable)
    ]
