"""Stroke-style histogram and style-filtered line selection.

``stroke_style_histogram`` summarizes one page's observed stroke styles into
deterministic bins; ``WallStrokeStyle`` and ``select_lines_by_style`` apply a
chosen style as a fail-closed filter.  The known answers run on a synthetic
generated PDF through the real ``extract_pdf`` path with literal counts and
lengths; validation rejections and determinism are pinned too.  Neither
helper is wired into the importer, so imported models are unchanged.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from oabm.importers.pdf_architecture import (
    StrokeStyleBin,
    WallStrokeStyle,
    select_lines_by_style,
    stroke_style_histogram,
)
from oabm.importers.pdf_architecture.extract import _rect_edge_segments, extract_pdf
from oabm.importers.pdf_architecture.types import PdfPageObservation

#: One 320x200 pt synthetic sheet: a heavy black closed wall loop, six thin
#: 30%-gray strokes, three dashed strokes, and one stroked rectangle whose
#: edge segments are the page's no-style evidence.
COMMANDS: tuple[str, ...] = (
    "1.5 w 0 G 30 30 m 290 30 l S",  # 260 pt
    "1.5 w 0 G 30 30 m 30 170 l S",  # 140 pt
    "1.5 w 0 G 290 170 m 30 170 l S",  # 260 pt
    "1.5 w 0 G 290 30 m 290 170 l S",  # 140 pt
    "0.25 w 0.3 G 60 60 m 60 100 l S",  # 40 pt
    "0.25 w 0.3 G 80 60 m 80 100 l S",
    "0.25 w 0.3 G 100 60 m 100 100 l S",
    "0.25 w 0.3 G 120 60 m 120 100 l S",
    "0.25 w 0.3 G 140 60 m 140 100 l S",
    "0.25 w 0.3 G 160 60 m 160 100 l S",
    "0.5 w 0 G [4 4] 0 d 40 190 m 90 190 l S",  # 50 pt
    "0.5 w 0 G [4 4] 0 d 110 190 m 170 190 l S",  # 60 pt
    "0.5 w 0 G [4 4] 0 d 190 190 m 260 190 l S",  # 70 pt
    "200 120 60 40 re S",
)

EXPECTED_BINS: tuple[StrokeStyleBin, ...] = (
    StrokeStyleBin(
        line_width_pt=1.5,
        stroke_gray=0.0,
        dashed=False,
        line_count=4,
        total_length_pt=800.0,
        longest_pt=260.0,
    ),
    StrokeStyleBin(
        line_width_pt=0.25,
        stroke_gray=0.3,
        dashed=False,
        line_count=6,
        total_length_pt=240.0,
        longest_pt=40.0,
    ),
    StrokeStyleBin(
        line_width_pt=0.5,
        stroke_gray=0.0,
        dashed=True,
        line_count=3,
        total_length_pt=180.0,
        longest_pt=70.0,
    ),
    StrokeStyleBin(
        line_width_pt=None,
        stroke_gray=None,
        dashed=False,
        line_count=1,
        total_length_pt=40.0,
        longest_pt=40.0,
    ),
)

WALL_LOOP: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = (
    ((30.0, 170.0), (290.0, 170.0)),
    ((30.0, 30.0), (30.0, 170.0)),
    ((30.0, 30.0), (290.0, 30.0)),
    ((290.0, 30.0), (290.0, 170.0)),
)


def _write_pdf(path: Path, commands: tuple[str, ...]) -> None:
    """A synthetic 320x200 pt sheet whose content stream draws the lines."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=320, height=200)
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _histogram_page(source: Path) -> PdfPageObservation:
    """The extracted page with ONE no-style rect edge among its lines.

    Rectangle edge segments carry no stroke style, so a single edge from the
    real rect-edge path is the page's no-style line evidence.
    """

    page = extract_pdf(source).pages[0]
    assert len(page.lines) == 13
    assert len(page.rects) == 1
    edge = _rect_edge_segments(page.rects, page.page_number)[0]
    assert (edge.line_width_pt, edge.stroke_gray) == (None, None)
    return replace(page, lines=(*page.lines, edge))


def test_histogram_bins_match_page_styles(tmp_path: Path) -> None:
    source = tmp_path / "stroke-histogram.pdf"
    _write_pdf(source, COMMANDS)

    bins = stroke_style_histogram(_histogram_page(source))

    assert bins == EXPECTED_BINS
    assert bins[0].total_length_pt == max(item.total_length_pt for item in bins)
    # The no-style bin is present, and the summary is JSON-ready.
    assert bins[-1] == EXPECTED_BINS[-1]
    payload = json.dumps([item.to_dict() for item in bins])
    assert json.loads(payload) == [item.to_dict() for item in bins]


def test_wall_style_selects_exactly_the_wall_lines(tmp_path: Path) -> None:
    source = tmp_path / "stroke-wall-selection.pdf"
    _write_pdf(source, COMMANDS)
    page = _histogram_page(source)

    selected = select_lines_by_style(page, WallStrokeStyle(1.4, 1.6, gray_max=0.1))

    assert len(selected) == 4
    assert {(line.start_pt, line.end_pt) for line in selected} == set(WALL_LOOP)
    # Matches keep the page's existing line order.
    assert selected == tuple(line for line in page.lines if line.line_width_pt == 1.5)


def test_wall_style_validation_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError):
        WallStrokeStyle(width_min_pt=1.6, width_max_pt=1.4)
    with pytest.raises(ValueError):
        WallStrokeStyle(1.4, 1.6, gray_min=-0.1)
    with pytest.raises(ValueError):
        WallStrokeStyle(1.4, 1.6, gray_max=1.2)
    with pytest.raises(ValueError):
        WallStrokeStyle(1.4, 1.6, gray_min=0.8, gray_max=0.2)
    # In-range bounds construct.
    WallStrokeStyle(1.4, 1.6)


def test_histogram_and_selection_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "stroke-histogram-determinism.pdf"
    _write_pdf(source, COMMANDS)

    first = _histogram_page(source)
    second = _histogram_page(source)

    assert stroke_style_histogram(first) == stroke_style_histogram(second)
    assert stroke_style_histogram(first) == stroke_style_histogram(first)
    style = WallStrokeStyle(1.4, 1.6, gray_max=0.1)
    assert select_lines_by_style(first, style) == select_lines_by_style(second, style)
    assert [item.to_dict() for item in stroke_style_histogram(first)] == [
        item.to_dict() for item in stroke_style_histogram(second)
    ]
