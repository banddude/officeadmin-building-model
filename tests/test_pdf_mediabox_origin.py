"""Offset-MediaBox acceptance tests (issue #191).

The same synthetic drawing is generated twice: once on a MediaBox with a
zero lower-left origin and once on a centred MediaBox whose content is
translated by the origin. Both extractors must return identical displayed
coordinates across each pair, for /Rotate 0 and /Rotate 90. All fixtures
are generated in-test; no answer keys, no private data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_architecture.extract import extract_pdf as extract_architecture
from oabm.importers.pdf_display import (
    SHX_TEXT_ANNOTATION_AUTHOR,
    page_display_transform,
)
from oabm.importers.pdf_electrical import extract_pdf as extract_electrical
from oabm.importers.pdf_electrical import ElectricalPdfImporter

PAGE_WIDTH_PT = 960.0
PAGE_HEIGHT_PT = 640.0
ORIGIN_X_PT = -PAGE_WIDTH_PT / 2
ORIGIN_Y_PT = -PAGE_HEIGHT_PT / 2

LINE_START = (140.0, 180.0)
LINE_END = (420.0, 460.0)
TEXT_ANCHOR = (160.0, 200.0)
TEXT_STRING = "M1 PANEL"
CIRCLE_CENTER = (300.0, 360.0)
CIRCLE_RADIUS = 50.0
RECT_XYWH = (100.0, 140.0, 40.0, 30.0)
SHX_LABEL = "PANEL SCHEDULE"
SHX_RECT = (150.0, 190.0, 260.0, 216.0)

GRID = (140.0, 180.0, 420.0, 460.0)
GRID_CELL_TEXTS = (("RA1", "RB1"), ("RA2", "RB2"))


def _drawing_body() -> str:
    cx, cy = CIRCLE_CENTER
    r = CIRCLE_RADIUS
    rx, ry, rw, rh = RECT_XYWH
    return "\n".join(
        [
            f"{rx} {ry} {rw} {rh} re S",
            f"{LINE_START[0]} {LINE_START[1]} m {LINE_END[0]} {LINE_END[1]} l S",
            f"BT /F1 12 Tf 1 0 0 1 {TEXT_ANCHOR[0]} {TEXT_ANCHOR[1]} Tm ({TEXT_STRING}) Tj ET",
            f"{cx - r} {cy} m {cx + r} {cy} l S",
            f"{cx} {cy - r} m {cx} {cy + r} l S",
        ]
    )


def _table_body() -> str:
    x0, y0, x1, y1 = GRID
    mid_x = (x0 + x1) / 2
    mid_y = (y0 + y1) / 2
    lines = [
        f"{x0} {y0} m {x1} {y0} l S",
        f"{x0} {mid_y} m {x1} {mid_y} l S",
        f"{x0} {y1} m {x1} {y1} l S",
        f"{x0} {y0} m {x0} {y1} l S",
        f"{mid_x} {y0} m {mid_x} {y1} l S",
        f"{x1} {y0} m {x1} {y1} l S",
    ]
    cell_ys = (y1 - 30.0, mid_y - 30.0)
    for row, y in zip(GRID_CELL_TEXTS, cell_ys):
        for text, x in zip(row, (x0 + 20.0, mid_x + 20.0)):
            lines.append(f"BT /F1 11 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET")
    return "\n".join(lines)


def _write_twin(
    path: Path,
    *,
    body: str,
    offset: bool,
    rotate: int,
    annotation: bool = False,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=PAGE_WIDTH_PT, height=PAGE_HEIGHT_PT)
    if offset:
        page[NameObject("/MediaBox")] = ArrayObject(
            [
                FloatObject(ORIGIN_X_PT),
                FloatObject(ORIGIN_Y_PT),
                FloatObject(-ORIGIN_X_PT),
                FloatObject(-ORIGIN_Y_PT),
            ]
        )
        translate = f"1 0 0 1 {ORIGIN_X_PT} {ORIGIN_Y_PT} cm\n"
    else:
        translate = ""
    page[NameObject("/Rotate")] = NumberObject(rotate)
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
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data((translate + body).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if annotation:
        shift_x = ORIGIN_X_PT if offset else 0.0
        shift_y = ORIGIN_Y_PT if offset else 0.0
        annot = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Square"),
                NameObject("/T"): TextStringObject(SHX_TEXT_ANNOTATION_AUTHOR),
                NameObject("/Contents"): TextStringObject(SHX_LABEL),
                NameObject("/Rect"): ArrayObject(
                    [
                        FloatObject(SHX_RECT[0] + shift_x),
                        FloatObject(SHX_RECT[1] + shift_y),
                        FloatObject(SHX_RECT[2] + shift_x),
                        FloatObject(SHX_RECT[3] + shift_y),
                    ]
                ),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annot)])
    with path.open("wb") as handle:
        writer.write(handle)


def _write_pair(
    directory: Path,
    stem: str,
    *,
    body: str,
    rotate: int,
    annotation: bool = False,
) -> tuple[Path, Path]:
    zero = directory / f"{stem}-zero-rot{rotate}.pdf"
    offset = directory / f"{stem}-offset-rot{rotate}.pdf"
    _write_twin(zero, body=body, offset=False, rotate=rotate, annotation=annotation)
    _write_twin(offset, body=body, offset=True, rotate=rotate, annotation=annotation)
    return zero, offset


def _displayed_point(x: float, y: float, page_rotation: int) -> tuple[float, float]:
    """Page-relative drawing point in displayed, bottom-origin space."""

    if page_rotation == 0:
        return x, y
    return (y, PAGE_WIDTH_PT - x)


def _expected_architecture_segment(page_rotation: int) -> tuple[tuple[float, float], tuple[float, float]]:
    """Displayed segment as the architecture extractor reports it.

    The architecture lane rebuilds each drawn segment from the pdfplumber
    line bbox corners, so on /Rotate 90 a diagonal segment is reported as
    the bbox anti-diagonal (the electrical lane reports the drawn diagonal).
    That pre-existing difference is independent of the MediaBox origin; the
    origin fix only requires both twins to agree, and they do.
    """

    if page_rotation == 0:
        return tuple(sorted((LINE_START, LINE_END)))
    xs = sorted((LINE_START[1], LINE_END[1]))
    ys = sorted((PAGE_WIDTH_PT - LINE_START[0], PAGE_WIDTH_PT - LINE_END[0]))
    return ((xs[0], ys[0]), (xs[1], ys[1]))


@pytest.mark.parametrize("page_rotation", [0, 90])
def test_electrical_extractor_identical_across_mediabox_twins(
    tmp_path: Path, page_rotation: int
) -> None:
    zero_path, offset_path = _write_pair(
        tmp_path, "drawing", body=_drawing_body(), rotate=page_rotation, annotation=True
    )
    zero = extract_electrical(zero_path, source_id="mediabox-origin-probe")
    offset = extract_electrical(offset_path, source_id="mediabox-origin-probe")

    key_text = lambda doc: [(t.text, t.x_pt, t.y_pt, t.font_size_pt) for t in doc.texts]
    key_vector = lambda doc: [(v.points_pt, v.closed) for v in doc.vectors]
    key_symbol = lambda doc: [(s.name, s.x_pt, s.y_pt) for s in doc.symbols]
    assert key_text(zero) == key_text(offset)
    assert key_vector(zero) == key_vector(offset)
    assert key_symbol(zero) == key_symbol(offset)

    # Exact displayed values from the page-relative drawing.
    expected_line = tuple(
        sorted(
            (
                _displayed_point(*LINE_START, page_rotation),
                _displayed_point(*LINE_END, page_rotation),
            )
        )
    )
    two_point_lines = {
        tuple(sorted(points))
        for vector in zero.vectors
        if len(points := vector.points_pt) == 2
    }
    assert expected_line in two_point_lines
    expected_anchor = _displayed_point(*TEXT_ANCHOR, page_rotation)
    anchors = [(t.x_pt, t.y_pt) for t in zero.texts if t.text == TEXT_STRING]
    assert anchors == [expected_anchor]

    # Two runs on the offset page stay identical (determinism).
    assert extract_electrical(offset_path, source_id="mediabox-origin-probe") == offset


@pytest.mark.parametrize("page_rotation", [0, 90])
def test_electrical_import_pose_identity_across_twins(
    tmp_path: Path, page_rotation: int
) -> None:
    zero_path, offset_path = _write_pair(
        tmp_path, "drawing", body=_drawing_body(), rotate=page_rotation, annotation=True
    )
    zero_model = ElectricalPdfImporter().import_document(
        extract_electrical(zero_path, source_id="mediabox-origin-probe")
    )
    offset_model = ElectricalPdfImporter().import_document(
        extract_electrical(offset_path, source_id="mediabox-origin-probe")
    )
    zero_poses = sorted(
        (device.name, round(device.pose.position.x, 6), round(device.pose.position.y, 6))
        for device in zero_model.electrical_devices
    )
    offset_poses = sorted(
        (device.name, round(device.pose.position.x, 6), round(device.pose.position.y, 6))
        for device in offset_model.electrical_devices
    )
    assert zero_poses == offset_poses


@pytest.mark.parametrize("page_rotation", [0, 90])
def test_architecture_extractor_identical_across_mediabox_twins(
    tmp_path: Path, page_rotation: int
) -> None:
    zero_path, offset_path = _write_pair(
        tmp_path, "drawing", body=_drawing_body(), rotate=page_rotation, annotation=True
    )
    zero = extract_architecture(zero_path, source_id="mediabox-origin-probe")
    offset = extract_architecture(offset_path, source_id="mediabox-origin-probe")
    zero_page, offset_page = zero.pages[0], offset.pages[0]

    assert zero_page.lines == offset_page.lines
    assert zero_page.rects == offset_page.rects
    assert zero_page.texts == offset_page.texts
    assert zero_page.width_pt == offset_page.width_pt
    assert zero_page.height_pt == offset_page.height_pt

    expected_line = _expected_architecture_segment(page_rotation)
    segments = {
        tuple(sorted((line.start_pt, line.end_pt))) for line in zero_page.lines
    }
    assert expected_line in segments
    assert zero_page.texts, "expected word and SHX text observations"

    # The SHX annotation lands on the same displayed box from both files.
    shx = [text for text in zero_page.texts if text.text == SHX_LABEL]
    assert len(shx) == 1
    assert shx[0].native_id is not None and "shx" in shx[0].native_id
    corners = (
        _displayed_point(SHX_RECT[0], SHX_RECT[1], page_rotation),
        _displayed_point(SHX_RECT[2], SHX_RECT[3], page_rotation),
    )
    assert shx[0].bbox_pt[0] == pytest.approx(min(c[0] for c in corners), abs=1e-6)
    assert shx[0].bbox_pt[1] == pytest.approx(min(c[1] for c in corners), abs=1e-6)


@pytest.mark.parametrize("page_rotation", [0, 90])
def test_table_extractor_identical_across_mediabox_twins(
    tmp_path: Path, page_rotation: int
) -> None:
    pytest.importorskip("camelot")
    from oabm.importers.tables import extract_tables

    zero_path, offset_path = _write_pair(
        tmp_path, "table", body=_table_body(), rotate=page_rotation
    )
    zero_tables = extract_tables(zero_path, 1, flavor="lattice")
    offset_tables = extract_tables(offset_path, 1, flavor="lattice")
    assert zero_tables, "lattice found no table on the zero-origin page"
    assert offset_tables, "lattice found no table on the offset-origin page"
    assert [table.bbox_pt for table in zero_tables] == [
        table.bbox_pt for table in offset_tables
    ]
    assert [[cell.text for cell in row] for row in zero_tables[0].rows] == [
        [cell.text for cell in row] for row in offset_tables[0].rows
    ]


class _StaticBox:
    left: float
    bottom: float
    width: float
    height: float

    def __init__(self, left: float, bottom: float) -> None:
        self.left = left
        self.bottom = bottom
        self.width = PAGE_WIDTH_PT
        self.height = PAGE_HEIGHT_PT


class _StaticPage:
    def __init__(self, left: float, bottom: float) -> None:
        self.mediabox = _StaticBox(left, bottom)

    def get(self, key: str, default: object = None) -> object:
        return default


def test_display_transform_zero_origin_is_exact_noop() -> None:
    transform = page_display_transform(_StaticPage(0.0, 0.0))
    assert transform.origin_x_pt == 0.0
    assert transform.origin_y_pt == 0.0
    assert transform.apply(-123.5, 987.25) == (-123.5, 987.25)
    assert transform.apply_relative(-123.5, 987.25) == (-123.5, 987.25)


def test_display_transform_subtracts_origin_before_rotating() -> None:
    transform = page_display_transform(_StaticPage(ORIGIN_X_PT, ORIGIN_Y_PT))
    assert (transform.origin_x_pt, transform.origin_y_pt) == (
        ORIGIN_X_PT,
        ORIGIN_Y_PT,
    )
    # The MediaBox lower-left corner maps to the displayed origin.
    assert transform.apply(ORIGIN_X_PT, ORIGIN_Y_PT) == (0.0, 0.0)
    assert transform.apply_relative(0.0, 0.0) == (0.0, 0.0)
    # apply() is apply_relative() of the origin-shifted point.
    for point in ((-480.0, -320.0), (140.0, 180.0), (0.0, 0.0), (33.5, -17.25)):
        assert transform.apply(*point) == transform.apply_relative(
            point[0] - ORIGIN_X_PT, point[1] - ORIGIN_Y_PT
        )
