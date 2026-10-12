"""Source-only alignment proposals for explicitly adjacent construction drawings.

This is not a wall matcher or a new building representation. A proposal requires
named-level agreement, reciprocal continuation notes on opposite drawing edges,
and supported orthogonal grid axes at the two printed scales. No transform is
inferred from a bubble center's arbitrary position along its grid line.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from statistics import median
from typing import Any

from .grid_rings import _enclosing_rings, grid_ring_labels
from .types import PdfPageObservation
from .wall_registration import DEFAULT_WALL_MATCH_OPTIONS

Box = tuple[float, float, float, float]
_MARK = re.compile(r"^([A-Z]{1,3})[-.]?(\d+(?:[.-]\d+)*)(?:\s*([A-Z]))?$")
_CONTINUATION = re.compile(r"CONTINUED\s+ON\s+(?:SHEET\s+)?(.+)")
_NORMAL_TOLERANCE_PT = .25


@dataclass(frozen=True)
class GridAxis:
    label: str
    dimension: int  # 0 is a constant-X vertical axis; 1 is constant-Y.
    coordinate_pt: float
    source_element_ids: tuple[str, ...]
    support_extent_pt: tuple[float, float]


def _mark(text: str) -> str | None:
    match = _MARK.fullmatch(text.strip().upper())
    return None if match is None else ":".join((match[1], match[2], match[3] or ""))


def _sheet_identity(page: PdfPageObservation) -> tuple[str, tuple[str, ...]] | None:
    by_mark: dict[str, list[str]] = {}
    for text in page.texts:
        value = _mark(text.text)
        if value and text.center_pt[0] >= .75 * page.width_pt and text.center_pt[1] <= .25 * page.height_pt:
            by_mark.setdefault(value, []).append(text.element_id)
    if len(by_mark) != 1:
        return None
    value = next(iter(by_mark))
    return value, tuple(sorted(by_mark[value]))


def _scope(page: PdfPageObservation, box: Box, mpp: float) -> Box:
    reach = max(72.0, DEFAULT_WALL_MATCH_OPTIONS.min_span_m / mpp)
    return (max(0, box[0]-reach), max(0, box[1]-reach),
            min(page.width_pt, box[2]+reach), min(page.height_pt, box[3]+reach))


def _ink_length(parts: list[tuple[float, float, str, float]]) -> float:
    """Actual interval union, so duplicate source strokes cannot inflate proof."""
    total = 0.0
    end = -math.inf
    for lo, hi, _, _ in sorted(parts):
        total += max(0.0, hi-max(lo, end))
        end = max(end, hi)
    return total


def grid_axes(page: PdfPageObservation, box: Box, mpp: float) -> dict[str, GridAxis]:
    scope = _scope(page, box, mpp)
    result: dict[str, GridAxis] = {}
    for label, center in grid_ring_labels(page, scope).items():
        local = [line for line in page.lines
                 if all(math.dist(center, p) <= 85 for p in (line.start_pt, line.end_pt))]
        rings = [(ring, text.element_id) for text in page.texts
                 if text.text.strip().upper() == label and math.dist(text.center_pt, center) <= 40
                 for ring in _enclosing_rings(text, local) if ring.center == center]
        if len(rings) != 1:
            continue
        ring, text_id = rings[0]
        found = []
        for dimension in (0, 1):
            segments = []
            for line in page.lines:
                if line.filled or line.primitive_family == "curve" or line.stroke_present is False:
                    continue
                a, b = line.start_pt, line.end_pt
                if max(abs(a[dimension]-center[dimension]), abs(b[dimension]-center[dimension])) > _NORMAL_TOLERANCE_PT:
                    continue
                lo, hi = sorted((a[1-dimension], b[1-dimension]))
                lo, hi = max(lo, scope[1-dimension]), min(hi, scope[3-dimension])
                if hi-lo >= 2:
                    segments.append((lo, hi, line.element_id, (a[dimension]+b[dimension])/2))
            groups: list[list[Any]] = []
            for lo, hi, key, normal in sorted(segments):
                if groups and lo-groups[-1][1] <= 2*ring.radius_pt:
                    groups[-1][1] = max(groups[-1][1], hi)
                    groups[-1][2].append((lo, hi, key, normal))
                else:
                    groups.append([lo, hi, [(lo, hi, key, normal)]])
            nearby = [group for group in groups
                      if max(group[0]-center[1-dimension], 0, center[1-dimension]-group[1]) <= 2*ring.radius_pt]
            if len(nearby) != 1:
                continue
            lo, hi, parts = nearby[0]
            if (hi-lo)*mpp < DEFAULT_WALL_MATCH_OPTIONS.min_span_m:
                continue
            if _ink_length(parts) < max(4*ring.radius_pt, .25*(hi-lo)):
                continue
            found.append(GridAxis(label, dimension, median([normal for _, _, _, normal in parts]),
                                  tuple(sorted({text_id, *[key for _, _, key, _ in parts]})), (lo, hi)))
        if len(found) == 1:
            result[label] = found[0]
    return result


def _notes(page: PdfPageObservation, box: Box, mpp: float, target: str) -> list[tuple[str | None, str]]:
    found = []
    scope = _scope(page, box, mpp)
    for text in page.texts:
        match = _CONTINUATION.fullmatch(text.text.strip().upper())
        if match is None or _mark(match[1]) != target:
            continue
        if text.font_size_pt is not None and text.font_size_pt < .1:
            continue
        x, y = text.center_pt
        if not (scope[0] <= x <= scope[2] and scope[1] <= y <= scope[3]):
            continue
        x0, y0, x1, y1 = text.bbox_pt
        width, height = x1-x0, y1-y0
        side = None
        if height > 2*width and box[1] <= y <= box[3]:
            side = "left" if x <= box[0] else "right" if x >= box[2] else None
        elif width > 2*height and box[0] <= x <= box[2]:
            side = "bottom" if y <= box[1] else "top" if y >= box[3] else None
        found.append((side, text.element_id))
    return found


def adjacent_grid_proposal(
    source: PdfPageObservation, source_box: Box, source_mpp: float, source_level: str | None,
    target: PdfPageObservation, target_box: Box, target_mpp: float, target_level: str | None,
) -> dict[str, Any]:
    """Propose positive-scale translation only; caller owns canonical placement.

    Boxes must describe separately bounded drawings. Level keys must already be
    resolved named-level identities, not an unlabeled/default level. A missing or
    inconsistent source constraint always leaves the proposal unaccepted.
    """
    record: dict[str, Any] = {"method": "reciprocal_continuation_grid_axes", "applicable": False,
                              "accepted": False, "reason_codes": []}
    def fail(reason: str) -> dict[str, Any]:
        record["reason_codes"] = [reason]
        return record
    if not all(math.isfinite(v) for v in (*source_box, *target_box, source_mpp, target_mpp)) or min(source_mpp, target_mpp) <= 0:
        return fail("invalid_registration_inputs")
    if any(box[2] <= box[0] or box[3] <= box[1] for box in (source_box, target_box)):
        return fail("unbounded_drawing_region")
    if not source_level or source_level != target_level:
        return fail("named_level_mismatch")
    sid, tid = _sheet_identity(source), _sheet_identity(target)
    if not sid or not tid or sid[0] == tid[0]:
        return fail("sheet_identity_unresolved")
    sn, tn = _notes(source, source_box, source_mpp, tid[0]), _notes(target, target_box, target_mpp, sid[0])
    record["applicable"] = bool(sn or tn)
    source_sides, target_sides = {side for side, _ in sn}, {side for side, _ in tn}
    if len(source_sides) != 1 or len(target_sides) != 1 or None in source_sides or None in target_sides:
        return fail("reciprocal_continuation_unresolved")
    source_side, target_side = next(iter(source_sides)), next(iter(target_sides))
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    if opposite[source_side] != target_side:
        return fail("continuation_sides_conflict")
    sa, ta = grid_axes(source, source_box, source_mpp), grid_axes(target, target_box, target_mpp)
    shared = sorted(sa.keys() & ta.keys())
    if any(sa[key].dimension != ta[key].dimension for key in shared):
        return fail("grid_axis_orientation_conflict")
    dimension = 0 if source_side in ("left", "right") else 1
    def edge_ok(axis: GridAxis, box: Box, side: str, mpp: float) -> bool:
        edge = box[dimension if side in ("left", "bottom") else dimension+2]
        span = box[dimension+2]-box[dimension]
        return (abs(axis.coordinate_pt-edge)*mpp <= DEFAULT_WALL_MATCH_OPTIONS.min_span_m
                and abs(axis.coordinate_pt-edge) <= .25*span)
    boundary = [key for key in shared if sa[key].dimension == dimension
                and edge_ok(sa[key], source_box, source_side, source_mpp)
                and edge_ok(ta[key], target_box, target_side, target_mpp)]
    across = [key for key in shared if sa[key].dimension != dimension]
    if len(boundary) != 1 or len(across) < 2:
        return fail("independent_grid_axes_insufficient")
    if min((max(axes[key].coordinate_pt for key in across)-min(axes[key].coordinate_pt for key in across))*mpp
           for axes, mpp in ((sa, source_mpp), (ta, target_mpp))) < DEFAULT_WALL_MATCH_OPTIONS.min_span_m:
        return fail("grid_control_span_insufficient")
    ratio = source_mpp/target_mpp
    offsets = {dim: [ta[key].coordinate_pt-ratio*sa[key].coordinate_pt for key in shared if sa[key].dimension == dim]
               for dim in (0, 1)}
    delta = tuple(median(offsets[dim]) for dim in (0, 1))
    residual = max(abs(ta[key].coordinate_pt-ratio*sa[key].coordinate_pt-delta[sa[key].dimension])*target_mpp for key in shared)
    if residual > DEFAULT_WALL_MATCH_OPTIONS.max_residual_m:
        return fail("grid_axes_inconsistent_with_printed_scale")
    def controls(axes: dict[str, GridAxis]) -> list[dict[str, Any]]:
        return [{"label": key, "dimension": axes[key].dimension, "coordinate_pt": axes[key].coordinate_pt,
                 "support_extent_pt": list(axes[key].support_extent_pt), "source_element_ids": list(axes[key].source_element_ids)}
                for key in shared]
    record.update(accepted=True, translation_pt=list(delta), scale_ratio=ratio, max_residual_m=residual,
                  boundary_axis=boundary[0], shared_axes=shared, source_side=source_side, target_side=target_side,
                  source_sheet=sid[0], target_sheet=tid[0], source_sheet_ids=list(sid[1]), target_sheet_ids=list(tid[1]),
                  source_reference_ids=sorted(key for _, key in sn), target_reference_ids=sorted(key for _, key in tn),
                  source_controls=controls(sa), target_controls=controls(ta))
    return record
