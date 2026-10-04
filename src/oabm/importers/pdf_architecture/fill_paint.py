"""Source-paint guards, independent of shape and canonical model semantics."""

from dataclasses import replace
from .types import PdfLineObservation, PdfRectObservation, PdfPageObservation


def wall_fill_paint(
    grays: tuple[float | None, ...], *, require_known: bool = False
) -> bool:
    """Unknown paint needs existing wall-layer authority; white is not material."""
    if not grays or all(value is None for value in grays):
        return not require_known
    if any(value is None or value >= 0.99 for value in grays):
        return False
    return True


def is_white_mask(item: PdfLineObservation | PdfRectObservation) -> bool:
    # A real stroke is independent evidence for the outline. Otherwise a
    # known mask or conflicting fill cannot prove a visible material edge.
    return (
        item.stroke_present is not True
        and bool(item.fill_grays)
        and not wall_fill_paint(item.fill_grays)
    )


def wall_geometry_page(page: PdfPageObservation) -> PdfPageObservation:
    """Leave original observations intact; exclude known white fill-only outlines."""
    lines = tuple(line for line in page.lines if not is_white_mask(line))
    rects = tuple(rect for rect in page.rects if not is_white_mask(rect))
    if len(lines) == len(page.lines) and len(rects) == len(page.rects):
        return page
    return replace(page, lines=lines, rects=rects)
