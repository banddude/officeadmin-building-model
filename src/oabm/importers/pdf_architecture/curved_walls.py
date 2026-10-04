"""Conservative concentric-arc pairing in source points, never hatch inference."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .extract import _is_wall_source_layer
from .types import ImportOptions, PdfCurveObservation, PdfPageObservation


@dataclass(frozen=True)
class ArcPair:
    points_pt: tuple[tuple[float, float], ...]
    thickness_m: float
    boundaries: tuple[PdfCurveObservation, PdfCurveObservation]


# center, radius, counterclockwise start, sweep, all in source points/radians.
Circle = tuple[tuple[float, float], float, float, float]


def _circle(curve: PdfCurveObservation) -> Circle | None:
    points = np.asarray(curve.points_pt, dtype=float)
    origin = points.mean(axis=0)
    local = points - origin
    matrix = np.column_stack((2 * local[:, 0], 2 * local[:, 1], np.ones(len(local))))
    solution, _, rank, _ = np.linalg.lstsq(
        matrix, np.sum(local * local, axis=1), rcond=None
    )
    if rank != 3:
        return None
    center = solution[:2] + origin
    distances = np.linalg.norm(points - center, axis=1)
    radius = float(np.mean(distances))
    if radius <= 0 or float(np.ptp(distances)) > 0.1:
        return None
    angles = np.unwrap(np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0]))
    if angles[-1] < angles[0]:
        angles = angles[::-1]
    sweep = float(angles[-1] - angles[0])
    # Closed bubbles and tiny nearly straight arcs cannot establish a wall.
    if not 0.1 <= sweep <= math.pi * 1.95 or np.any(np.diff(angles) < -1e-6):
        return None
    start = float(angles[0] % (2 * math.pi))
    return (tuple(float(v) for v in center), radius, start, sweep)


def arc_pairs(
    page: PdfPageObservation,
    meters_per_point: float,
    options: ImportOptions,
    excluded_boxes: (
        tuple[tuple[float, float, float, float], ...]
        | list[tuple[float, float, float, float]]
    ) = (),
) -> tuple[tuple[ArcPair, ...], list[dict[str, object]]]:
    """Return unique pairs and explicit refusal diagnostics; no best-guess ties."""
    rejected = []
    candidates = []
    for curve in page.curves:
        reason = None
        if curve.dashed:
            reason = "curved_wall_dimension_or_dashed"
        elif any(
            all(
                box[0] <= x <= box[2] and box[1] <= y <= box[3]
                for x, y in curve.points_pt
            )
            for box in excluded_boxes
        ):
            reason = "curved_wall_title_or_legend"
        elif page.hidden_wall_source_present and not any(
            _is_wall_source_layer(layer) for layer in curve.source_layers
        ):
            reason = "hidden_wall_layer_unresolved"
        circle = _circle(curve) if reason is None else None
        if circle is None and reason is None:
            reason = "curved_wall_non_circular_or_unsupported"
        if reason:
            rejected.append(
                {
                    "page": page.page_number,
                    "code": reason,
                    "source_element_ids": [curve.element_id],
                }
            )
        else:
            candidates.append((curve, circle))
    matches = {i: [] for i in range(len(candidates))}
    for i, (_, a) in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            _, b = candidates[j]
            gap = abs(a[1] - b[1])
            if (
                not options.min_wall_thickness_m
                <= gap * meters_per_point
                <= options.max_wall_thickness_m
            ):
                continue
            # Fitted centers and endpoints must agree well inside the wall gap.
            tolerance = min(0.2, gap * 0.05)
            angle_error = abs((a[2] - b[2] + math.pi) % (2 * math.pi) - math.pi)
            if (
                math.dist(a[0], b[0]) > tolerance
                or angle_error * max(a[1], b[1]) > tolerance
                or abs(a[3] - b[3]) * max(a[1], b[1]) > tolerance
                or min(a[1] * a[3], b[1] * b[3]) * meters_per_point < 0.6096
            ):
                continue
            matches[i].append(j)
            matches[j].append(i)
    pairs = []
    for i, (curve, a) in enumerate(candidates):
        partners = matches[i]
        if len(partners) != 1 or len(matches[partners[0]]) != 1:
            rejected.append(
                {
                    "page": page.page_number,
                    "code": (
                        "curved_wall_pair_ambiguous"
                        if partners
                        else "curved_wall_pair_unresolved"
                    ),
                    "source_element_ids": [curve.element_id],
                }
            )
            continue
        j = partners[0]
        if j < i:
            continue
        other, b = candidates[j]
        # Mean boundary at common angular positions, chord sag <= 0.1 pt.
        center = tuple((x + y) / 2 for x, y in zip(a[0], b[0]))
        radius = (a[1] + b[1]) / 2
        start = a[2]
        sweep = (a[3] + b[3]) / 2
        step = 2 * math.acos(max(-1.0, 1 - 0.1 / radius))
        count = max(2, math.ceil(sweep / step))
        points = tuple(
            (
                center[0] + radius * math.cos(start + sweep * k / count),
                center[1] + radius * math.sin(start + sweep * k / count),
            )
            for k in range(count + 1)
        )
        pairs.append(
            ArcPair(points, abs(a[1] - b[1]) * meters_per_point, (curve, other))
        )
    return tuple(pairs), rejected
