"""Stroke width and stroke gray recorded on PDF line observations.

``PdfLineObservation`` carries the observed stroke style next to the
geometry: the displayed width in points and the stroking colour as a
0-1 luminance. These tests pin the known answers on synthetic generated
PDFs through the real ``extract_pdf`` path, the duplicate-geometry merge
rule, the observation validation, and extraction determinism. The stroke
style is observation-level groundwork only: the importer does not read
it, so no imported model changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture.extract import _rect_edge_segments, extract_pdf
from oabm.importers.pdf_architecture.types import (
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
)


def _write_pdf(path: Path, commands: tuple[str, ...]) -> None:
    """A synthetic 300x160 pt sheet whose content stream draws the lines."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=160)
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _line_at(page: PdfPageObservation, x0: float, y0: float, x1: float, y1: float) -> PdfLineObservation:
    return next(
        line
        for line in page.lines
        if line.start_pt == (x0, y0) and line.end_pt == (x1, y1)
    )


def test_pdf_lines_carry_exact_width_and_gray(tmp_path: Path) -> None:
    source = tmp_path / "stroke-styles.pdf"
    _write_pdf(
        source,
        (
            # 1.5 pt pure black
            "1.5 w 0 G 50 20 m 250 20 l S",
            # 0.25 pt 30% gray
            "0.25 w 0.3 G 50 40 m 250 40 l S",
            # RGB red: luminance 0.299
            "1 w 1 0 0 RG 50 60 m 250 60 l S",
            # CMYK converted to RGB first: (0.16, 0.12, 0.08) -> 0.1274
            "1 w 0.2 0.4 0.6 0.8 K 50 80 m 250 80 l S",
            # 1 w drawn under a scale-2 CTM displays as 2 pt, and pdfplumber
            # already reports the displayed width: recorded as 2.0.
            "q 2 0 0 2 0 0 cm 1 w 0 G 25 60 m 125 60 l S Q",
        ),
    )

    page = extract_pdf(source).pages[0]
    assert len(page.lines) == 5

    black = _line_at(page, 50.0, 20.0, 250.0, 20.0)
    assert (black.line_width_pt, black.stroke_gray) == (1.5, 0.0)
    thin = _line_at(page, 50.0, 40.0, 250.0, 40.0)
    assert (thin.line_width_pt, thin.stroke_gray) == (0.25, 0.3)
    red = _line_at(page, 50.0, 60.0, 250.0, 60.0)
    assert (red.line_width_pt, red.stroke_gray) == (1.0, 0.299)
    cmyk = _line_at(page, 50.0, 80.0, 250.0, 80.0)
    assert (cmyk.line_width_pt, cmyk.stroke_gray) == (1.0, 0.1274)
    scaled = _line_at(page, 50.0, 120.0, 250.0, 120.0)
    assert (scaled.line_width_pt, scaled.stroke_gray) == (2.0, 0.0)


def test_duplicate_geometry_keeps_max_width_and_min_gray(tmp_path: Path) -> None:
    source = tmp_path / "duplicate-styles.pdf"
    # The same geometry twice: the first stroke is the thinner, darker one.
    _write_pdf(
        source,
        (
            "0.5 w 0.3 G 50 100 m 250 100 l S",
            "1.5 w 0.8 G 50 100 m 250 100 l S",
        ),
    )

    page = extract_pdf(source).pages[0]
    assert len(page.lines) == 1, "duplicate geometry still dedupes to one line"
    merged = page.lines[0]
    assert merged.line_width_pt == 1.5, "the heaviest duplicate width wins"
    assert merged.stroke_gray == 0.3, "the darkest duplicate gray wins"


def test_rect_edge_segments_carry_no_stroke_style() -> None:
    rect = PdfRectObservation(element_id="rect:one", bbox_pt=(0.0, 0.0, 40.0, 20.0))
    segments = _rect_edge_segments((rect,), 1)
    assert len(segments) == 4
    assert all(segment.line_width_pt is None for segment in segments)
    assert all(segment.stroke_gray is None for segment in segments)


def test_observation_rejects_out_of_range_stroke_style() -> None:
    base: dict[str, object] = {
        "element_id": "line:one",
        "start_pt": (0.0, 0.0),
        "end_pt": (10.0, 0.0),
    }
    with pytest.raises(ValueError):
        PdfLineObservation(**base, line_width_pt=-0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PdfLineObservation(**base, stroke_gray=1.2)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PdfLineObservation(**base, stroke_gray=-0.1)  # type: ignore[arg-type]
    # In-range values construct.
    PdfLineObservation(**base, line_width_pt=0.0, stroke_gray=1.0)  # type: ignore[arg-type]


def test_extraction_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "stroke-determinism.pdf"
    _write_pdf(
        source,
        (
            "1.5 w 0 G 50 20 m 250 20 l S",
            "0.25 w 0.3 G 50 40 m 250 40 l S",
            "1 w 1 0 0 RG 50 60 m 250 60 l S",
        ),
    )
    assert extract_pdf(source) == extract_pdf(source)
