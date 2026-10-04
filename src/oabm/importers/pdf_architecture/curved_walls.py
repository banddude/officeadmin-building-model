"""Conservative concentric-arc pairing in source points, never hatch inference."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace


from .curve_geometry import Circle, fit_circle as _circle
from .extract import _is_wall_source_layer
from .types import ImportOptions, PdfCurveObservation, PdfPageObservation


@dataclass(frozen=True)
class ArcPair:
    points_pt: tuple[tuple[float, float], ...]
    thickness_m: float
    boundaries: tuple[PdfCurveObservation, ...]


def _common_span(a: Circle, b: Circle, tolerance: float) -> tuple[float, float] | None:
    """One supported angular interval, never a bridge across missing evidence."""
    angle_error = abs((a[2] - b[2] + math.pi) % (2 * math.pi) - math.pi)
    if (
        angle_error * max(a[1], b[1]) <= tolerance
        and abs(a[3] - b[3]) * max(a[1], b[1]) <= tolerance
    ):
        # Keep established equal-span sampling byte-for-byte.
        return a[2], (a[3] + b[3]) / 2
    intervals = []
    for shift in (-2 * math.pi, 0.0, 2 * math.pi):
        start = max(a[2], b[2] + shift)
        end = min(a[2] + a[3], b[2] + b[3] + shift)
        if end - start > 1e-10:
            intervals.append((start, end))
    if len(intervals) != 1:
        # Long wrapped arcs can have two disjoint common portions. A single
        # centerline must not join them through an unsupported gap.
        return None
    start, end = intervals[0]
    # Canonicalize wrap arithmetic independent of the boundary iteration order;
    # this precision is far below the existing source chord-error bound.
    return round(start % (2 * math.pi), 12), round(end - start, 12)


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
        canonical = replace(
            curve, points_pt=min(curve.points_pt, tuple(reversed(curve.points_pt)))
        )
        circle = _circle(canonical) if reason is None else None
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
    # A contained overprint of the same geometric face is extra evidence,
    # not another competing wall face. Collapse it before partner counting;
    # otherwise a long pair plus its short redraw falsely becomes ambiguous.
    face_tolerance = min(0.1, options.min_wall_thickness_m / meters_per_point * 0.2)
    groups = []
    for original_index, (curve, circle) in sorted(
        enumerate(candidates), key=lambda item: (-item[1][1][3], item[1][0].element_id)
    ):
        for _, kept, kept_circle, evidence in groups:
            relative = (circle[2] - kept_circle[2]) % (2 * math.pi)
            angle_tolerance = face_tolerance / max(circle[1], kept_circle[1])
            if relative > 2 * math.pi - angle_tolerance:
                relative -= 2 * math.pi
            if (
                math.dist(circle[0], kept_circle[0]) <= face_tolerance
                and abs(circle[1] - kept_circle[1]) <= face_tolerance
                and relative >= -angle_tolerance
                and relative + circle[3] <= kept_circle[3] + angle_tolerance
            ):
                evidence.append(curve)
                break
        else:
            groups.append((original_index, curve, circle, [curve]))
    groups.sort(key=lambda item: item[0])
    candidates = [(curve, circle) for _, curve, circle, _ in groups]
    evidence_groups = [evidence for _, _, _, evidence in groups]
    matches = {i: [] for i in range(len(candidates))}
    spans = {}
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
            if math.dist(a[0], b[0]) > tolerance:
                continue
            span = _common_span(a, b, tolerance)
            if span is None or min(a[1], b[1]) * span[1] * meters_per_point < 0.6096:
                continue
            spans[(i, j)] = span
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
        start, sweep = spans[(i, j)]
        if max((a[3] - sweep) * a[1], (b[3] - sweep) * b[1]) > min(
            0.2, abs(a[1] - b[1]) * 0.05
        ):
            rejected.append(
                {
                    "page": page.page_number,
                    "code": "curved_wall_partial_overlap",
                    "source_element_ids": sorted(
                        c.element_id for c in (*evidence_groups[i], *evidence_groups[j])
                    ),
                    "message": "Only the common boundary extent was emitted; unmatched portions remain unresolved.",
                }
            )
        step = 2 * math.acos(max(-1.0, 1 - 0.1 / radius))
        count = max(2, math.ceil(sweep / step))
        points = tuple(
            (
                center[0] + radius * math.cos(start + sweep * k / count),
                center[1] + radius * math.sin(start + sweep * k / count),
            )
            for k in range(count + 1)
        )
        boundaries = (*evidence_groups[i], *evidence_groups[j])
        if len(boundaries) > 2:
            boundaries = tuple(sorted(boundaries, key=lambda item: item.element_id))
        pairs.append(ArcPair(points, abs(a[1] - b[1]) * meters_per_point, boundaries))
    # CAD can overprint a shorter copy of an existing boundary. Its matched
    # band is additional source evidence, not a second wall occupying it.
    circles = [
        (_circle(PdfCurveObservation("pair", pair.points_pt)), pair) for pair in pairs
    ]
    circles.sort(
        key=lambda item: (
            -item[0][3],
            item[0][:3],
            tuple(c.element_id for c in item[1].boundaries),
        )
    )
    unique: list[tuple[Circle, ArcPair]] = []
    for circle, pair in circles:
        for index, (kept_circle, kept) in enumerate(unique):
            relative_start = (circle[2] - kept_circle[2]) % (2 * math.pi)
            if relative_start > 2 * math.pi - 0.001:
                relative_start -= 2 * math.pi
            if (
                math.dist(circle[0], kept_circle[0]) <= 0.1
                and abs(circle[1] - kept_circle[1]) <= 0.1
                and abs(pair.thickness_m - kept.thickness_m) <= 0.1 * meters_per_point
                and relative_start >= -0.001
                and relative_start + circle[3] <= kept_circle[3] + 0.001
            ):
                boundaries = {
                    c.element_id: c for c in (*kept.boundaries, *pair.boundaries)
                }
                unique[index] = (
                    kept_circle,
                    replace(
                        kept,
                        boundaries=tuple(boundaries[k] for k in sorted(boundaries)),
                    ),
                )
                break
        else:
            unique.append((circle, pair))
    return tuple(pair for _, pair in unique), rejected
