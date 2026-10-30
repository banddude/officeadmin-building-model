"""Bounded same-page source-legend producer for branch stroke ownership.

Only explicit legend-row labels and their unique sample can establish semantics.
Unsupported paint, ambiguous rows and missing source signals remain unresolved.
"""
import re
import math

from ..pdf_branch_evidence import BranchStrokeClaim, SourceBranchEvidence
from .schedule_review import schedule_source_fingerprint

_LABELS = {
    "ELECTRICAL BRANCH WIRING": "electrical",
    "ARCHITECTURAL LINEWORK": "architectural",
}


def _style(vector):
    metadata = vector.metadata
    if (vector.closed or vector.metadata.get("curve_commands")
            or metadata.get("paint_operator") != "S"
            or metadata.get("branch_style_supported") is not True):
        return None
    gray, width = metadata.get("stroke_gray"), metadata.get("line_width_pt")
    if gray != 0. or width is None or width <= 0:
        return None
    return gray, width


def _text_region(text):
    size = text.font_size_pt
    if size is None or not math.isfinite(size) or size <= 0:
        return None
    # Core Helvetica's maximum advance is 1.015 em and its AFM glyph box is
    # [-.166, -.225, 1, .931] em. Enlarge those extents and add a margin.
    # This is a conservative paint bound for the admitted font, not layout.
    return (text.x_pt - .2 * size - 1, text.y_pt - .3 * size - 1,
            text.x_pt + len(text.text) * 1.1 * size + 1, text.y_pt + size + 1)


def _stroke_region(vector):
    width = vector.metadata.get("line_width_pt")
    if width is None or not math.isfinite(width) or width <= 0:
        return None
    points = list(vector.points_pt)
    for command in vector.metadata.get("curve_commands", ()):
        points.extend(command.get("control_points_pt", ()))
    margin = width / 2 + 1
    return (min(p[0] for p in points) - margin, min(p[1] for p in points) - margin,
            max(p[0] for p in points) + margin, max(p[1] for p in points) + margin)


def _overlaps(left, right):
    return not (left[2] < right[0] or right[2] < left[0]
                or left[3] < right[1] or right[3] < left[1])


def source_branch_evidence(document, *, page):
    """Produce explicit claims bound to bytes, observations and displayed page.

    Supported legend row: an exact label to the right of one horizontal sample
    in the same baseline band. No closest sample wins: multiple candidates refuse.
    This is source-defined style semantics, not inferred semantics from topology.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", document.source_id):
        raise ValueError("branch ownership requires byte-bound PDF extraction")
    display = document.page_provenance.get(page, {})
    if display.get("coordinate_space") != "displayed":
        raise ValueError("branch ownership requires actual displayed page frame")
    frame = (0., 0., float(display["displayed_page_width_pt"]),
             float(display["displayed_page_height_pt"]))
    if (type(page) is not int or not 1 <= page <= document.page_count
            or any(not math.isfinite(value) for value in frame)
            or frame[2] <= 0 or frame[3] <= 0):
        raise ValueError("branch ownership requires a valid displayed page")
    def in_frame(points):
        return all(0 <= x <= frame[2] and 0 <= y <= frame[3] for x, y in points)
    vectors = tuple(v for v in document.vectors if v.page == page)
    page_texts = tuple(t for t in document.texts if t.page == page)
    stroke_regions = {v.element_id: _stroke_region(v) for v in vectors}
    text_regions = {t.element_id: _text_region(t) for t in page_texts}
    def clear_region(region, *, text_id=None, vector_id=None):
        if region is None or not in_frame(((region[0], region[1]), (region[2], region[3]))):
            return False
        # Refuse overlap regardless of paint order. Unknown bounds also refuse;
        # extracted strings alone cannot establish that the legend was visible.
        return (all(bounds is not None and not _overlaps(region, bounds)
                    for key, bounds in stroke_regions.items() if key != vector_id)
            and all(bounds is not None and not _overlaps(region, bounds)
                    for key, bounds in text_regions.items() if key != text_id))
    def supported_text(text):
        return (text.branch_legend_paint_supported is True
            and clear_region(text_regions[text.element_id], text_id=text.element_id))
    if len({v.element_id for v in vectors}) != len(vectors):
        raise ValueError("duplicate branch source observations")
    if len({t.element_id for t in document.texts}) != len(document.texts):
        raise ValueError("duplicate branch text observations")
    rows = []
    for text in document.texts:
        classification = _LABELS.get(text.text.upper())
        if text.page != page or classification is None or not supported_text(text):
            continue
        headings = [item for item in document.texts if item.page == page
            and item.text.upper() == "LINE LEGEND"
            and supported_text(item)
            and 0 < item.y_pt - text.y_pt <= 120
            and abs(item.x_pt - text.x_pt) <= 80]
        if len(headings) != 1:
            continue
        candidates = []
        for vector in vectors:
            if (_style(vector) is None or len(vector.points_pt) != 2
                    or not clear_region(stroke_regions[vector.element_id], vector_id=vector.element_id)):
                continue
            (x0, y0), (x1, y1) = sorted(vector.points_pt)
            # A bounded legend row, not a clearance over the page. The line is
            # strictly left of the exact label and aligned with its baseline.
            if (y0 == y1 and 0 < text.x_pt - x1 <= 20
                    and 10 <= x1 - x0 <= 80 and abs(y0 - text.y_pt) <= 3):
                candidates.append(vector)
        if len(candidates) == 1:
            sample = candidates[0]
            rows.append((_style(sample), classification,
                         (headings[0].element_id, text.element_id, sample.element_id)))
    claims = []
    for vector in sorted(vectors, key=lambda v: v.element_id):
        matching = [row for row in rows if row[0] == _style(vector)
                    and in_frame(vector.points_pt)]
        meanings = {row[1] for row in matching}
        classification = (next(iter(meanings)) if len(meanings) == 1 else
                          "conflicting" if meanings else "unresolved")
        claims.append(BranchStrokeClaim(vector.element_id, classification,
            tuple(sorted({key for row in matching for key in row[2]})),
            .9 if meanings else 0.))
    return SourceBranchEvidence(document.source_id[7:],
        schedule_source_fingerprint(document), page, frame,
        "pdf_electrical.source_branch_evidence", "1", tuple(claims))
