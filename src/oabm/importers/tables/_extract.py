"""Public deterministic table extraction entry point.

``extract_tables`` reads ruled legends, panel schedules and title-block
grids out of vector PDF pages without any AI step. It is a building block
for the PDF lanes; wiring it into the legend and schedule code paths is a
separate, later task, and this module changes no existing behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader

from oabm.importers.pdf_display import page_display_transform

from ._camelot import extract_tables_camelot
from ._docling import extract_tables_docling
from ._types import ExtractedTable

_FLAVORS = ("auto", "lattice", "stream")
_BACKENDS = ("camelot", "docling")

RegionBox = tuple[float, float, float, float]


def _validate_regions(regions_pt: Any) -> list[RegionBox] | None:
    if regions_pt is None:
        return None
    regions: list[RegionBox] = []
    for region in regions_pt:
        x0, y0, x1, y1 = (float(v) for v in region)
        if not x1 > x0 or not y1 > y0:
            raise ValueError(
                f"regions_pt box {(x0, y0, x1, y1)!r} must be "
                "(x0, y0, x1, y1) with x1 > x0 and y1 > y0"
            )
        regions.append((x0, y0, x1, y1))
    return regions


def extract_tables(
    pdf_path: str | Path,
    page_number: int,
    *,
    regions_pt: list[RegionBox] | None = None,
    flavor: str = "auto",
    backend: str = "camelot",
) -> list[ExtractedTable]:
    """Extract tables from one PDF page into deterministic value types.

    Args:
        pdf_path: path to the source PDF.
        page_number: 1-based page index in the file.
        regions_pt: optional ``(x0, y0, x1, y1)`` boxes in displayed PDF
            points, bottom-left origin (the repo's convention). ``None``
            means the whole page. With the ``docling`` backend the boxes
            filter whole tables by bbox intersection instead of clipping.
        flavor: ``lattice`` (ruled tables only), ``stream`` (text alignment
            only), or ``auto``: run lattice first, then stream, and keep the
            higher-confidence result, ties going to lattice. ``auto`` picks
            per call by mean table confidence; every kept table records why
            in its ``warnings``.
        backend: ``camelot`` (default, MIT-licensed, lightweight) or
            ``docling`` (optional, heavier layout models). Both backends
            import lazily and raise an :class:`ImportError` naming their
            extra (``tables`` / ``docling``) when it is missing.

    Returns:
        Tables with ``bbox_pt`` and cell boxes in displayed, bottom-origin
        PDF points, rows in reading order, cell text whitespace-normalized
        and otherwise untouched. Pages with no table return ``[]``; a
        missing or degenerate grid is dropped rather than guessed into a
        table. Output is identical across runs on the same input.

    Raises:
        ValueError: bad ``flavor``/``backend``, malformed region box, or an
            out-of-range ``page_number``.
        ImportError: the requested backend's extra is not installed.
        RuntimeError: the chosen backend failed in a way that is not
            simply "no table here".
    """

    if flavor not in _FLAVORS:
        raise ValueError(
            f"flavor must be one of {_FLAVORS}, got {flavor!r}"
        )
    if backend not in _BACKENDS:
        raise ValueError(
            f"backend must be one of {_BACKENDS}, got {backend!r}"
        )
    regions = _validate_regions(regions_pt)
    if not isinstance(page_number, int) or isinstance(page_number, bool):
        raise ValueError("page_number must be an int")
    if page_number < 1:
        raise ValueError(f"page_number is 1-based, got {page_number}")

    pdf_path = Path(pdf_path)
    with PdfReader(str(pdf_path)) as reader:
        if page_number > len(reader.pages):
            raise ValueError(
                f"page {page_number} out of range; PDF holds {len(reader.pages)} pages"
            )
        transform = page_display_transform(reader.pages[page_number - 1])

    if backend == "docling":
        return extract_tables_docling(
            pdf_path, page_number, transform=transform, regions_pt=regions
        )
    return extract_tables_camelot(
        pdf_path,
        page_number,
        transform=transform,
        regions_pt=regions,
        flavor=flavor,
    )


__all__ = ["ExtractedTable", "extract_tables"]
