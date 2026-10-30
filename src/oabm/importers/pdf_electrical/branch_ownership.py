"""Bounded same-page source-legend producer for branch stroke ownership.

Only explicit legend-row labels and their unique sample can establish semantics.
Unsupported paint, ambiguous rows and missing source signals remain unresolved.
"""
import re

from ..pdf_branch_evidence import BranchStrokeClaim, SourceBranchEvidence
from .schedule_review import schedule_source_fingerprint

_LABELS = {
    "ELECTRICAL BRANCH WIRING": "electrical",
    "ARCHITECTURAL LINEWORK": "architectural",
}


def _style(vector):
    metadata = vector.metadata
    if (vector.closed or len(vector.points_pt) != 2
            or metadata.get("paint_operator") != "S"
            or metadata.get("branch_style_supported") is not True):
        return None
    gray, width = metadata.get("stroke_gray"), metadata.get("line_width_pt")
    if gray is None or width is None or width <= 0:
        return None
    return gray, width


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
    if not 1 <= page <= document.page_count or frame[2] <= 0 or frame[3] <= 0:
        raise ValueError("branch ownership requires a valid displayed page")
    vectors = tuple(v for v in document.vectors if v.page == page)
    if len({v.element_id for v in vectors}) != len(vectors):
        raise ValueError("duplicate branch source observations")
    if len({t.element_id for t in document.texts}) != len(document.texts):
        raise ValueError("duplicate branch text observations")
    rows = []
    for text in document.texts:
        classification = _LABELS.get(text.text.upper())
        if text.page != page or classification is None:
            continue
        headings = [item for item in document.texts if item.page == page
            and item.text.upper() == "LINE LEGEND"
            and 0 < item.y_pt - text.y_pt <= 120
            and abs(item.x_pt - text.x_pt) <= 80]
        if len(headings) != 1:
            continue
        candidates = []
        for vector in vectors:
            if _style(vector) is None:
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
        matching = [row for row in rows if row[0] == _style(vector)]
        meanings = {row[1] for row in matching}
        classification = (next(iter(meanings)) if len(meanings) == 1 else
                          "conflicting" if meanings else "unresolved")
        claims.append(BranchStrokeClaim(vector.element_id, classification,
            tuple(sorted({key for row in matching for key in row[2]})),
            .9 if meanings else 0.))
    return SourceBranchEvidence(document.source_id[7:],
        schedule_source_fingerprint(document), page, frame,
        "pdf_electrical.source_branch_evidence", "1", tuple(claims))
