"""Deterministic table extraction from ruled and unruled PDF pages (#72).

Every PDF here is generated in the test from synthetic content through raw
pypdf content streams, then run through the real ``extract_tables``. No
pre-extracted JSON, no expected-output files, no private data. The grid
text is invented for this file and carries no customer wording.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.tables import ExtractedCell, ExtractedTable, extract_tables

PAGE_W, PAGE_H = 612.0, 792.0
FONT_PT = 10.0
BASELINE_INSET = 7.0
TEXT_X_INSET = 6.0


def _write_pdf(
    path: Path,
    commands: list[str],
    *,
    width: float = PAGE_W,
    height: float = PAGE_H,
    rotate: int = 0,
) -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            ),
        }
    )
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    if rotate:
        page[NameObject("/Rotate")] = NumberObject(rotate)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _ruled_grid_commands(
    x0: float,
    y0: float,
    col_widths: list[float],
    row_h: float,
    texts: list[list[str]],
) -> tuple[list[str], dict[str, tuple[float, float]]]:
    """Draw a ruled grid and return its commands plus drawn text origins.

    ``texts[0]`` is the top row. Origins are user-space baseline points,
    kept so tests can check displayed boxes against drawn positions.
    """

    n_rows, n_cols = len(texts), len(col_widths)
    total_w = sum(col_widths)
    total_h = row_h * n_rows
    commands = [f"{0.8} w", f"{x0} {y0} {total_w} {total_h} re S"]
    for c in range(1, n_cols):
        x = x0 + sum(col_widths[:c])
        commands.append(f"{x} {y0} m {x} {y0 + total_h} l S")
    for r in range(1, n_rows):
        y = y0 + r * row_h
        commands.append(f"{x0} {y} m {x0 + total_w} {y} l S")

    origins: dict[str, tuple[float, float]] = {}
    for r, row in enumerate(texts):
        baseline_y = y0 + (n_rows - 1 - r) * row_h + BASELINE_INSET
        for c, text in enumerate(row):
            base_x = x0 + sum(col_widths[:c]) + TEXT_X_INSET
            commands.append(
                f"BT /F1 {FONT_PT} Tf {base_x} {baseline_y} Td ({text}) Tj ET"
            )
            origins[text] = (base_x, baseline_y)
    return commands, origins


def _kv_text_commands(
    key_x: float,
    val_x: float,
    top_y: float,
    dy: float,
    pairs: list[tuple[str, str]],
) -> tuple[list[str], dict[str, tuple[float, float]]]:
    """Draw unruled key/value rows and return commands plus origins."""

    commands: list[str] = []
    origins: dict[str, tuple[float, float]] = {}
    for i, (key, value) in enumerate(pairs):
        y = top_y - i * dy
        for x, text in ((key_x, key), (val_x, value)):
            commands.append(f"BT /F1 {FONT_PT} Tf {x} {y} Td ({text}) Tj ET")
            origins[text] = (x, y)
    return commands, origins


def _display_point(
    x: float, y: float, rotation: int, w: float, h: float
) -> tuple[float, float]:
    """Displayed, bottom-origin point for a user-space point.

    Derived from the PDF display rule (the page is shown turned clockwise
    by ``rotation`` degrees), independent of the module's implementation.
    """

    if rotation == 90:
        return (y, w - x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        return (h - y, x)
    return (x, y)


def _matrix(table: ExtractedTable) -> list[list[str]]:
    return [[cell.text for cell in row] for row in table.rows]


def _in_box(
    point: tuple[float, float],
    box: tuple[float, float, float, float],
    tol: float = 1.0,
) -> bool:
    x, y = point
    x0, y0, x1, y1 = box
    return x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol


LEGEND_TEXTS = [
    ["SY-1", "BLUE LAMP"],
    ["SY-2", "PULL STATION"],
    ["SY-3", "DOOR CONTACT"],
    ["SY-4", "ALARM BELL"],
    ["SY-5", "EXHAUST FAN"],
]

PANEL_TEXTS = [
    ["CKT", "LOAD", "VA", "BRK", "PL", "PH"],
    ["C-01", "LTS-GR-A", "180", "15", "1", "1"],
    ["C-02", "LTS-GR-B", "240", "15", "1", "1"],
    ["C-03", "REC-GR-A", "600", "20", "1", "1"],
    ["C-04", "REC-GR-B", "450", "20", "1", "1"],
    ["C-05", "HT-UNIT", "3000", "30", "2", "1"],
    ["C-06", "AHU-TRIM", "900", "20", "2", "3"],
    ["C-07", "SPARE", "0", "15", "1", "1"],
]

TITLE_PAIRS = [
    ("JOB", "N-118"),
    ("SHEET", "E-03"),
    ("REV", "B"),
    ("SCALE", "1/4"),
]


def test_ruled_legend_exact_cells_and_confidence(tmp_path: Path) -> None:
    commands, _ = _ruled_grid_commands(72.0, 520.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    pdf = _write_pdf(tmp_path / "legend.pdf", commands)

    tables = extract_tables(pdf, 1)

    assert len(tables) == 1
    table = tables[0]
    assert isinstance(table, ExtractedTable)
    assert table.n_rows == 5
    assert table.n_cols == 2
    assert _matrix(table) == LEGEND_TEXTS
    assert table.backend == "camelot"
    assert table.flavor == "lattice"
    assert table.confidence >= 0.8
    # Every cell keeps its own box, and row/col indexes match placement.
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row):
            assert isinstance(cell, ExtractedCell)
            assert (cell.row, cell.col) == (r, c)
            assert cell.bbox_pt[2] > cell.bbox_pt[0]
            assert cell.bbox_pt[3] > cell.bbox_pt[1]


def test_ruled_panel_schedule_exact_cells(tmp_path: Path) -> None:
    commands, _ = _ruled_grid_commands(
        60.0, 430.0, [44.0, 96.0, 56.0, 44.0, 32.0, 32.0], 20.0, PANEL_TEXTS
    )
    pdf = _write_pdf(tmp_path / "panel.pdf", commands)

    tables = extract_tables(pdf, 1)

    assert len(tables) == 1
    table = tables[0]
    assert (table.n_rows, table.n_cols) == (8, 6)
    assert _matrix(table) == PANEL_TEXTS
    assert table.flavor == "lattice"


def test_unruled_title_block_uses_stream(tmp_path: Path) -> None:
    commands, _ = _kv_text_commands(72.0, 216.0, 560.0, 18.0, TITLE_PAIRS)
    pdf = _write_pdf(tmp_path / "title.pdf", commands)

    tables = extract_tables(pdf, 1)

    assert len(tables) == 1
    table = tables[0]
    assert (table.n_rows, table.n_cols) == (4, 2)
    assert _matrix(table) == [list(pair) for pair in TITLE_PAIRS]
    assert table.flavor == "stream"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_pages_cells_contain_drawn_text(
    tmp_path: Path, rotation: int
) -> None:
    commands, origins = _ruled_grid_commands(72.0, 520.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    pdf = _write_pdf(tmp_path / f"legend-rot{rotation}.pdf", commands, rotate=rotation)

    tables = extract_tables(pdf, 1)

    assert len(tables) == 1
    table = tables[0]
    # Rows follow the reading order of the page as displayed: for /Rotate
    # 180 the whole sheet is upside down, so both rows and columns reverse.
    expected = (
        LEGEND_TEXTS
        if rotation != 180
        else [list(reversed(row)) for row in reversed(LEGEND_TEXTS)]
    )
    assert _matrix(table) == expected
    # Displayed page size swaps for quarter turns.
    disp_w, disp_h = (
        (PAGE_H, PAGE_W) if rotation in (90, 270) else (PAGE_W, PAGE_H)
    )
    x0, y0, x1, y1 = table.bbox_pt
    assert 0.0 <= x0 and x1 <= disp_w
    assert 0.0 <= y0 and y1 <= disp_h
    for row in table.rows:
        for cell in row:
            assert cell.text in origins
            drawn = _display_point(*origins[cell.text], rotation, PAGE_W, PAGE_H)
            assert _in_box(drawn, cell.bbox_pt), (
                f"{cell.text}: drawn {drawn} outside {cell.bbox_pt}"
            )


def test_regions_pt_limits_extraction(tmp_path: Path) -> None:
    upper, _ = _ruled_grid_commands(72.0, 560.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    lower_texts = [["P-1", "ONE"], ["P-2", "TWO"], ["P-3", "THREE"]]
    lower, _ = _ruled_grid_commands(72.0, 200.0, [60.0, 140.0], 22.0, lower_texts)
    pdf = _write_pdf(tmp_path / "two-tables.pdf", upper + lower)

    whole = extract_tables(pdf, 1)
    assert sorted(_matrix(t) for t in whole) == sorted(
        [LEGEND_TEXTS, lower_texts]
    )

    region = [(40.0, 170.0, 420.0, 300.0)]
    tables = extract_tables(pdf, 1, regions_pt=region)
    assert len(tables) == 1
    table = tables[0]
    assert _matrix(table) == lower_texts
    rx0, ry0, rx1, ry1 = region[0]
    for row in table.rows:
        for cell in row:
            cx0, cy0, cx1, cy1 = cell.bbox_pt
            assert cx0 >= rx0 and cx1 <= rx1
            assert cy0 >= ry0 and cy1 <= ry1


def test_page_without_tables_returns_empty(tmp_path: Path) -> None:
    pdf = _write_pdf(
        tmp_path / "notes.pdf",
        ["BT /F1 10 Tf 100 300 Td (RANDOM NOTE LINE ONLY) Tj ET"],
    )
    for flavor in ("auto", "lattice", "stream"):
        assert extract_tables(pdf, 1, flavor=flavor) == []


def test_missing_camelot_extra_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _write_pdf(
        tmp_path / "probe.pdf",
        ["BT /F1 10 Tf 100 300 Td (PROBE LINE) Tj ET"],
    )
    monkeypatch.setitem(sys.modules, "camelot", None)
    with pytest.raises(ImportError, match="tables"):
        extract_tables(pdf, 1)


def test_missing_docling_extra_names_the_extra(tmp_path: Path) -> None:
    if importlib.util.find_spec("docling") is not None:
        pytest.skip("docling is installed; the missing-extra path cannot run")
    pdf = _write_pdf(
        tmp_path / "probe.pdf",
        ["BT /F1 10 Tf 100 300 Td (PROBE LINE) Tj ET"],
    )
    with pytest.raises(ImportError, match="docling"):
        extract_tables(pdf, 1, backend="docling")


def test_docling_backend_same_shape(tmp_path: Path) -> None:
    pytest.importorskip("docling")
    commands, _ = _ruled_grid_commands(72.0, 520.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    pdf = _write_pdf(tmp_path / "legend.pdf", commands)

    tables = extract_tables(pdf, 1, backend="docling")

    assert len(tables) == 1
    table = tables[0]
    assert table.backend == "docling"
    assert (table.n_rows, table.n_cols) == (5, 2)
    assert _matrix(table) == LEGEND_TEXTS


def test_determinism_and_serialization(tmp_path: Path) -> None:
    commands, _ = _ruled_grid_commands(72.0, 520.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    pdf = _write_pdf(tmp_path / "legend.pdf", commands)

    first = extract_tables(pdf, 1)
    second = extract_tables(pdf, 1)

    assert [t.to_dict() for t in first] == [t.to_dict() for t in second]
    payload = json.dumps(first[0].to_dict())
    assert "SY-3" in payload
    # Frozen value types: no in-place mutation of extracted evidence.
    with pytest.raises(dataclasses.FrozenInstanceError):
        first[0].flavor = "stream"
    with pytest.raises(dataclasses.FrozenInstanceError):
        first[0].rows[0][0].text = "TAMPERED"


def test_argument_validation(tmp_path: Path) -> None:
    commands, _ = _ruled_grid_commands(72.0, 520.0, [72.0, 200.0], 24.0, LEGEND_TEXTS)
    pdf = _write_pdf(tmp_path / "legend.pdf", commands)

    with pytest.raises(ValueError, match="flavor"):
        extract_tables(pdf, 1, flavor="neural")
    with pytest.raises(ValueError, match="backend"):
        extract_tables(pdf, 1, backend="ai")
    with pytest.raises(ValueError, match="regions_pt"):
        extract_tables(pdf, 1, regions_pt=[(40.0, 40.0, 20.0, 100.0)])
    with pytest.raises(ValueError, match="1-based"):
        extract_tables(pdf, 0)
    with pytest.raises(ValueError, match="out of range"):
        extract_tables(pdf, 99)

    reader = PdfReader(str(pdf))
    assert len(reader.pages) == 1
