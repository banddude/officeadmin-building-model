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
        and not wall_fill_paint(item.fill_grays, require_known=not (
            item.source_layers if isinstance(item, PdfLineObservation) else item.source_layer
        ))
    )


def wall_geometry_page(page: PdfPageObservation, *, expand_native_fills: bool = False) -> PdfPageObservation:
    """Normalize visible material primitives without changing source observations.

    The caller enables native fill expansion only for recognized construction
    plans. Native filled rectangles and equivalent closed paths need the same strip
    checks. Expand known nonwhite unlayered rectangle boundaries, preserving source IDs
    and paint; geometry/scale/role gates still decide whether they are walls.
    """
    from .extract import _rect_edge_segments
    # Keep coincident paint conflicts before filtering masks out. Otherwise
    # native-rectangle expansion could recreate the very boundary just removed.
    def geometry_key(line: PdfLineObservation):
        return tuple(sorted((line.start_pt, line.end_pt)))
    ambiguous_fill_edges: dict[tuple, tuple[float | None, ...]] = {}
    for line in page.lines:
        if line.filled and not wall_fill_paint(line.fill_grays, require_known=True):
            key = geometry_key(line)
            ambiguous_fill_edges[key] = tuple(dict.fromkeys(
                (*ambiguous_fill_edges.get(key, ()), *(line.fill_grays or (None,)))))
    lines = tuple(line for line in page.lines if not is_white_mask(line))
    def guarded_rect(rect: PdfRectObservation) -> PdfRectObservation:
        paints = list(rect.fill_grays)
        if rect.filled:
            for edge in _rect_edge_segments((rect,), page.page_number):
                paints.extend(ambiguous_fill_edges.get(geometry_key(edge), ()))
        if tuple(paints) != rect.fill_grays:
            # Coincident source paints disagree; draw order is not recorded.
            # Preserve actual paint evidence and any independent stroke.
            return replace(rect, fill_grays=tuple(dict.fromkeys(paints)))
        return rect
    rects = tuple(candidate for rect in page.rects
                  if not is_white_mask(candidate := guarded_rect(rect)))
    known_ids = {line.element_id for line in lines}
    known_filled_edges = {geometry_key(line) for line in lines
                          if line.filled and wall_fill_paint(line.fill_grays, require_known=True)}
    expanded = _rect_edge_segments(
        (rect for rect in rects if expand_native_fills and rect.filled
         and wall_fill_paint(rect.fill_grays, require_known=True)
         and rect.source_layer is None),
        page.page_number,
    )
    additions = []
    for line in expanded:
        key = geometry_key(line)
        if (line.element_id not in known_ids and key not in known_filled_edges
                and key not in ambiguous_fill_edges):
            additions.append(line)
            known_ids.add(line.element_id)
            known_filled_edges.add(key)
    lines = (*lines, *additions)
    if lines == page.lines and rects == page.rects:
        return page
    return replace(page, lines=lines, rects=rects)
