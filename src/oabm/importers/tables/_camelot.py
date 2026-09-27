"""Camelot-backed table extraction.

Coordinate-space note (probed against camelot-py 2.0.0): for pages whose
``/Rotate`` entry is 0 or 180, camelot presents the rotated page, so its
output coordinates already are displayed page points. For ``/Rotate`` 90 and
270 camelot ignores the page rotation and reports page-relative, unrotated
points (probed on synthetic offset-MediaBox pages: its output does not shift
with the MediaBox origin). This adapter maps the rotated cases into
displayed, bottom-origin space with :meth:`PdfPageDisplayTransform.apply_relative`,
and converts ``regions_pt`` boxes back the same way before calling camelot.

Lattice extraction runs camelot's ``vector`` engine, which reads ruled lines
from the PDF layout instead of rasterizing the page: it is exact and fast on
vector CAD output, avoids image-conversion backends entirely, and sidesteps
a camelot 2.0.0 defect where the combined raster engine drops every
``table_areas`` request (a whole-page area yields zero tables there).

Failures fail closed per flavor and per region: camelot can raise inside a
flavor (for example the stream parser's ``TypeError`` when a ``table_areas``
region on a ``/Rotate`` 270 page holds only sparse short text, seen with
camelot-py 2.0.0). Each camelot call is isolated, the failure becomes a
warning string naming the flavor and the exception, the other flavor's
result is kept, and a page or region where no flavor yields a table
returns ``[]`` instead of raising.
"""

from __future__ import annotations

import warnings
from typing import Any

from oabm.importers.pdf_display import PdfPageDisplayTransform

from ._types import ExtractedCell, ExtractedTable, table_confidence

# Rounding applied when formatting region boxes for camelot.
_AREA_DECIMALS = 3

_FLAVORS = ("lattice", "stream")


def import_camelot() -> Any:
    """Import camelot lazily, mapping a missing package to a clear error."""

    try:
        import camelot  # optional dependency, imported on first use
    except ImportError as exc:
        raise ImportError(
            "Table extraction needs the optional 'tables' extra "
            "(camelot-py). Install it with: "
            "pip install 'officeadmin-building-model[tables]'"
        ) from exc
    return camelot


def _camelot_space_is_displayed(rotation: int) -> bool:
    """True when camelot already reports displayed points for this page."""

    return rotation % 180 == 0


def _to_displayed_bbox(
    bbox: tuple[float, float, float, float],
    transform: PdfPageDisplayTransform,
) -> tuple[float, float, float, float]:
    """Map one camelot-space box to displayed, bottom-origin points."""

    x0, y0, x1, y1 = (float(v) for v in bbox)
    if _camelot_space_is_displayed(transform.page_rotation):
        return x0, y0, x1, y1
    # camelot output is page-relative, so no MediaBox-origin term applies.
    corners = [transform.apply_relative(x, y) for x, y in ((x0, y0), (x1, y1))]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _region_to_camelot_area(
    region: tuple[float, float, float, float],
    transform: PdfPageDisplayTransform,
) -> str:
    """Map one displayed ``regions_pt`` box into camelot's layout space."""

    x0, y0, x1, y1 = (float(v) for v in region)
    if _camelot_space_is_displayed(transform.page_rotation):
        corners = [(x0, y0), (x1, y1)]
    else:
        # Inverse of the quarter-turn display transform; camelot's space is
        # page-relative, so there is no MediaBox-origin term to undo. Both
        # corners are then re-normalized because the turn swaps and flips
        # the axes.
        w, h = transform.source_width_pt, transform.source_height_pt
        if transform.page_rotation == 90:
            corners = [(w - y0, x0), (w - y1, x1)]
        else:  # 270
            corners = [(y0, h - x0), (y1, h - x1)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (
        f"{round(min(xs), _AREA_DECIMALS)},{round(min(ys), _AREA_DECIMALS)},"
        f"{round(max(xs), _AREA_DECIMALS)},{round(max(ys), _AREA_DECIMALS)}"
    )


def _cell_text(value: Any) -> str:
    """Render one pandas cell value as normalized text."""

    if value is None:
        return ""
    # pandas marks empty cells as NaN (a float); those become "".
    if isinstance(value, float) and value != value:
        return ""
    return str(value)


def _build_table(
    camelot_table: Any,
    *,
    backend: str,
    flavor: str,
    transform: PdfPageDisplayTransform,
) -> ExtractedTable | None:
    """Convert one camelot Table into an :class:`ExtractedTable`.

    Returns ``None`` (with the reason carried by the caller) when the
    candidate is not a table grid, e.g. a single stray text run that the
    stream flavor wrapped into a 1x1 frame.
    """

    frame = camelot_table.df
    n_rows, n_cols = int(frame.shape[0]), int(frame.shape[1])
    if n_rows < 2 or n_cols < 2:
        return None

    texts = [[_cell_text(v) for v in row] for row in frame.to_numpy().tolist()]
    camelot_cells = getattr(camelot_table, "cells", None) or []

    table_bbox = _to_displayed_bbox(
        tuple(float(v) for v in camelot_table._bbox), transform
    )
    warnings_out: list[str] = []
    rows_out: list[tuple[ExtractedCell, ...]] = []
    nonempty = 0
    for r in range(n_rows):
        row_cells: list[ExtractedCell] = []
        for c in range(n_cols):
            text = texts[r][c]
            if text:
                nonempty += 1
            bbox = table_bbox
            try:
                cell = camelot_cells[r][c]
                bbox = _to_displayed_bbox(
                    (float(cell.x1), float(cell.y1), float(cell.x2), float(cell.y2)),
                    transform,
                )
            except (IndexError, AttributeError, TypeError):
                # Fail closed: keep the text, fall back to the table-level
                # box, and say so instead of dropping the cell.
                if "cell geometry incomplete; some cells fall back to the table box" not in warnings_out:
                    warnings_out.append(
                        "cell geometry incomplete; some cells fall back to the table box"
                    )
            row_cells.append(ExtractedCell(text=text, bbox_pt=bbox, row=r, col=c))
        rows_out.append(tuple(row_cells))

    report = camelot_table.parsing_report
    bbox = table_bbox
    confidence = table_confidence(
        accuracy_pct=report.get("accuracy"),
        whitespace_pct=report.get("whitespace"),
        n_cells=n_rows * n_cols,
        n_nonempty=nonempty,
    )
    return ExtractedTable(
        bbox_pt=bbox,
        rows=tuple(rows_out),
        backend=backend,
        flavor=flavor,
        confidence=confidence,
        warnings=tuple(warnings_out),
    )


def _run_flavor(
    camelot: Any,
    pdf_path: Any,
    page_number: int,
    flavor: str,
    regions: list[tuple[float, float, float, float]] | None,
    transform: PdfPageDisplayTransform,
) -> tuple[list[ExtractedTable], list[str]]:
    """Run one camelot flavor; never raises for 'no table found'.

    Returns the flavor's tables plus one failure string per camelot call
    that raised. Each call (the whole page, or one region per call) is
    isolated in its own try, so a raising call is recorded instead of
    taking down the other calls of the same flavor, and its failure string
    names the flavor, the region (when any) and the exception.
    """

    kwargs: dict[str, Any] = {"pages": str(page_number), "flavor": flavor}
    if flavor == "lattice":
        kwargs["engine"] = "vector"
    calls: list[tuple[str, dict[str, Any] | None]] = [("", None)]
    if regions is not None:
        # One call per region: camelot merges multiple areas of one call into
        # a single junk table, and per-region calls keep region -> table
        # mapping unambiguous. The vector engine costs well under 0.1 s.
        calls = [
            (
                f" region {index + 1}",
                {"table_areas": [_region_to_camelot_area(region, transform)]},
            )
            for index, region in enumerate(regions)
        ]

    tables: list[ExtractedTable] = []
    failures: list[str] = []
    with warnings.catch_warnings():
        # 'No tables found' arrives as a UserWarning; it is an expected,
        # structured outcome here, not something to spam onto stderr.
        warnings.simplefilter("ignore", UserWarning)
        for label, area_kwargs in calls:
            try:
                found = camelot.read_pdf(
                    pdf_path, **{**kwargs, **(area_kwargs or {})}
                )
                for camelot_table in found:
                    table = _build_table(
                        camelot_table,
                        backend="camelot",
                        flavor=flavor,
                        transform=transform,
                    )
                    if table is not None:
                        tables.append(table)
            except Exception as exc:
                # Fail closed per call: record and move on; the caller
                # keeps the surviving flavor's result.
                failures.append(f"{flavor}{label}: {type(exc).__name__}: {exc}")
    return tables, failures


def extract_tables_camelot(
    pdf_path: Any,
    page_number: int,
    *,
    transform: PdfPageDisplayTransform,
    regions_pt: list[tuple[float, float, float, float]] | None,
    flavor: str,
    diagnostics: list[str] | None = None,
) -> list[ExtractedTable]:
    """Extract tables with camelot, honoring the ``auto`` flavor contract.

    Never raises for content-driven failures: a flavor (or one region of a
    flavor) whose extraction raises is recorded as a warning string naming
    the flavor and the exception, appended to ``diagnostics`` when the
    caller passes a list, while the surviving flavor's result is kept.
    """

    camelot = import_camelot()
    requested: tuple[str, ...] = _FLAVORS if flavor == "auto" else (flavor,)
    results: dict[str, list[ExtractedTable]] = {}
    failures: dict[str, list[str]] = {}
    for run in requested:
        results[run], failures[run] = _run_flavor(
            camelot, pdf_path, page_number, run, regions_pt, transform
        )
    run_failures = [message for run in requested for message in failures[run]]
    if diagnostics is not None:
        diagnostics.extend(run_failures)

    if flavor != "auto":
        return results[flavor]

    if not results["lattice"] and not results["stream"]:
        return []

    def mean_confidence(tables: list[ExtractedTable]) -> float:
        if not tables:
            return -1.0
        return sum(t.confidence for t in tables) / len(tables)

    lattice_mean = mean_confidence(results["lattice"])
    stream_mean = mean_confidence(results["stream"])
    # Ties go to lattice, matching the flavor contract.
    chosen = "lattice" if lattice_mean >= stream_mean else "stream"
    other = "stream" if chosen == "lattice" else "lattice"
    tables = results[chosen]
    note_bits = [f"flavor {chosen} kept"]
    if failures[other]:
        note_bits.append(f"flavor {other} failed: {'; '.join(failures[other])}")
    elif not results[other]:
        note_bits.append(f"flavor {other} found no table grid")
    else:
        note_bits.append(
            f"flavor {other} lower mean confidence "
            f"({mean_confidence(results[other]):.4f})"
        )
    note = "; ".join(note_bits)
    return [
        ExtractedTable(
            bbox_pt=table.bbox_pt,
            rows=table.rows,
            backend="camelot",
            flavor=table.flavor,
            confidence=table.confidence,
            warnings=table.warnings + (note,),
        )
        for table in tables
    ]
