"""Optional Docling-backed table extraction (``docling`` extra).

Docling is heavier than Camelot (layout models) and is never installed by
CI, so this adapter stays defensive: it follows the same output shape and
confidence contract as the Camelot path but cannot reuse Camelot's
accuracy/whitespace parsing report, so its confidence carries the
non-empty fraction term only and a warning says so. Region boxes are
applied as a bbox-intersection filter because Docling has no region
parameter. Cell boxes use a cell's provenance box when present and fall
back to the table box with a warning when it is not.
"""

from __future__ import annotations

from typing import Any

from oabm.importers.pdf_display import PdfPageDisplayTransform

from ._types import ExtractedCell, ExtractedTable, normalize_cell_text, table_confidence

_NO_REPORT_WARNING = (
    "docling provides no accuracy/whitespace report; "
    "confidence reflects non-empty cells only"
)
_CELL_BOX_FALLBACK_WARNING = (
    "docling cell has no provenance box; cell falls back to the table box"
)


def extract_tables_docling(
    pdf_path: Any,
    page_number: int,
    *,
    transform: PdfPageDisplayTransform,
    regions_pt: list[tuple[float, float, float, float]] | None,
) -> list[ExtractedTable]:
    """Extract tables with Docling; same output shape as the Camelot path."""

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise ImportError(
            "Docling table extraction needs the optional 'docling' extra. "
            "Install it with: pip install 'officeadmin-building-model[docling]'"
        ) from exc

    converted = DocumentConverter().convert(str(pdf_path))
    document = getattr(converted, "document", None)
    if document is None:
        raise RuntimeError(f"docling produced no document for {pdf_path}")

    tables: list[ExtractedTable] = []
    for item in getattr(document, "tables", None) or []:
        prov = getattr(item, "prov", None) or []
        first = prov[0] if prov else None
        provenance_page = int(getattr(first, "page_no", 0) or 0)
        if provenance_page != page_number:
            continue

        data = getattr(item, "data", None)
        grid = getattr(data, "grid", None) or []
        rows_text = [
            [normalize_cell_text(getattr(cell, "text", "")) for cell in row]
            for row in grid
        ]
        n_rows = len(rows_text)
        n_cols = len(rows_text[0]) if rows_text else 0
        if n_rows < 2 or n_cols < 2:
            continue

        table_bbox = _displayed_bbox(first, document, page_number)
        if table_bbox is not None and regions_pt is not None and not _intersects_any(
            table_bbox, regions_pt
        ):
            continue

        warnings_out: list[str] = [_NO_REPORT_WARNING]
        if table_bbox is None:
            warnings_out.append("docling gave no usable table provenance box")
            table_bbox = (0.0, 0.0, 0.0, 0.0)

        nonempty = 0
        rows_out: list[tuple[ExtractedCell, ...]] = []
        for r, row in enumerate(rows_text):
            row_cells: list[ExtractedCell] = []
            for c, text in enumerate(row):
                if text:
                    nonempty += 1
                cell_bbox = table_bbox
                cell_prov = (getattr(grid[r][c], "prov", None) or [None])[0]
                cell_box = _displayed_bbox(cell_prov, document, page_number)
                if cell_box is not None:
                    cell_bbox = cell_box
                elif text:
                    if _CELL_BOX_FALLBACK_WARNING not in warnings_out:
                        warnings_out.append(_CELL_BOX_FALLBACK_WARNING)
                row_cells.append(
                    ExtractedCell(text=text, bbox_pt=cell_bbox, row=r, col=c)
                )
            rows_out.append(tuple(row_cells))

        tables.append(
            ExtractedTable(
                bbox_pt=table_bbox,
                rows=tuple(rows_out),
                backend="docling",
                flavor="layout",
                confidence=table_confidence(
                    accuracy_pct=None,
                    whitespace_pct=None,
                    n_cells=n_rows * n_cols,
                    n_nonempty=nonempty,
                ),
                warnings=tuple(warnings_out),
            )
        )
    return tables


def _displayed_bbox(
    provenance: Any,
    document: Any,
    page_number: int,
) -> tuple[float, float, float, float] | None:
    """Map one Docling provenance bbox to displayed, bottom-origin points.

    Returns ``None`` when the provenance or its coordinate origin is
    missing, so callers can fall back instead of guessing.
    """

    if provenance is None:
        return None
    bbox = getattr(provenance, "bbox", None)
    if bbox is None:
        return None
    try:
        left = float(getattr(bbox, "l"))
        right = float(getattr(bbox, "r"))
        top = float(getattr(bbox, "t"))
        bottom = float(getattr(bbox, "b"))
    except (AttributeError, TypeError, ValueError):
        return None

    origin = str(getattr(bbox, "coord_origin", ""))
    if "BOTTOMLEFT" in origin:
        return left, bottom, right, top
    if "TOPLEFT" in origin:
        page = (getattr(document, "pages", None) or {}).get(page_number)
        height = float(getattr(getattr(page, "size", None), "height", 0.0) or 0.0)
        if height <= 0.0:
            return None
        return left, height - bottom, right, height - top
    return None


def _intersects_any(
    bbox: tuple[float, float, float, float],
    regions: list[tuple[float, float, float, float]],
) -> bool:
    x0, y0, x1, y1 = bbox
    for rx0, ry0, rx1, ry1 in regions:
        if x0 < rx1 and rx0 < x1 and y0 < ry1 and ry0 < y1:
            return True
    return False
