"""Synthetic source rings, independent of font metric centering."""

import math
from dataclasses import replace
import pytest
from oabm.importers.pdf_architecture.types import (
    PdfLineObservation,
    PdfPageObservation,
    PdfTextObservation,
)
from oabm.importers.pdf_convergence.sheet_registration import _grid_bubbles


def ring(cx=100, cy=100, r=18, n=24, sy=1):
    points = [
        (cx + r * math.cos(i * math.tau / n), cy + sy * r * math.sin(i * math.tau / n))
        for i in range(n)
    ]
    return tuple(
        PdfLineObservation(f"edge-{cx}-{cy}-{i}", a, b, primitive_family="polyline")
        for i, (a, b) in enumerate(zip(points, points[1:] + points[:1]))
    )


def label(value="A", cx=100, cy=100, offset=-4):
    return PdfTextObservation(
        f"text-{value}-{cx}-{cy}",
        value,
        (cx - 6, cy - 10 + offset, cx + 6, cy + 10 + offset),
        font_size_pt=20,
    )


def page(lines=None, texts=None):
    return PdfPageObservation(
        1, 600, 600, texts=texts or (label(),), lines=ring() if lines is None else lines
    )


def test_offset_font_box_returns_actual_ring_center():
    assert _grid_bubbles(page())["A"] == pytest.approx((100, 100), abs=0.001)


def test_attached_leader_and_nearby_noise_do_not_corrupt_ring():
    noise = (
        PdfLineObservation("leader", (118, 100), (150, 100)),
        PdfLineObservation("noise", (80, 85), (90, 95)),
    )
    assert _grid_bubbles(page(lines=ring() + noise))["A"] == pytest.approx(
        (100, 100), abs=0.001
    )


@pytest.mark.parametrize("lines", [ring()[:-1], ring(sy=1.5), ring(n=4)])
def test_open_ellipse_and_square_are_not_rings(lines):
    assert _grid_bubbles(page(lines=lines)) == {}


def test_external_label_not_accepted():
    assert _grid_bubbles(page(texts=(label(offset=-22),))) == {}


def test_nested_competing_rings_are_ambiguous():
    assert _grid_bubbles(page(lines=ring() + ring(r=25))) == {}


def test_duplicate_labels_refused_but_region_scope_is_respected():
    p = page(lines=ring() + ring(300, 100), texts=(label(), label(cx=300)))
    assert _grid_bubbles(p) == {}
    assert _grid_bubbles(p, (50, 50, 150, 150))["A"] == pytest.approx(
        (100, 100), abs=0.001
    )


def test_order_does_not_change_result():
    p = page()
    assert _grid_bubbles(p) == _grid_bubbles(replace(p, lines=tuple(reversed(p.lines))))


def test_native_cubic_ring_is_not_mistaken_for_its_four_chord_square(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from oabm.importers.pdf_architecture.extract import extract_pdf

    writer = PdfWriter()
    p = writer.add_blank_page(300, 300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    p[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"118 100 m 118 109.941 109.941 118 100 118 c 90.059 118 82 109.941 82 100 c 82 90.059 90.059 82 100 82 c 109.941 82 118 90.059 118 100 c S\nBT /F1 20 Tf 1 0 0 1 93 88 Tm (A) Tj ET"
    )
    p[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "synthetic-grid.pdf"
    writer.write(path)
    assert _grid_bubbles(extract_pdf(path).pages[0])["A"] == pytest.approx(
        (100, 100), abs=0.001
    )


def test_two_exactly_concentric_cubic_rings_are_ambiguous():
    lines = tuple(
        replace(line, primitive_family="curve") for line in ring(n=4) + ring(r=25, n=4)
    )
    assert _grid_bubbles(page(lines=lines)) == {}


@pytest.mark.parametrize("field", ["dashed", "filled"])
def test_non_outline_geometry_is_not_a_ring(field):
    assert (
        _grid_bubbles(
            page(lines=tuple(replace(line, **{field: True}) for line in ring()))
        )
        == {}
    )


def test_ambiguous_letters_stay_excluded():
    assert _grid_bubbles(page(texts=(label("I"),))) == {}
    assert _grid_bubbles(page(texts=(label("O"),))) == {}


@pytest.mark.parametrize("sheet_ref", ["Q-27", "A204", "S3.2", "Q7"])
def test_detail_reference_inside_same_ring_is_not_a_grid_label(sheet_ref):
    # The divider is just short of the ring endpoints, as can happen after
    # source flattening. Ring topology alone must not erase the sheet reference.
    divider = PdfLineObservation("detail-divider",(82.1,100),(117.9,100))
    number = PdfTextObservation("detail-number","2",(97,102,103,111),font_size_pt=8)
    reference = PdfTextObservation("detail-sheet",sheet_ref,(89,88,111,97),font_size_pt=8)
    assert _grid_bubbles(page(lines=ring()+(divider,),texts=(number,reference))) == {}


def test_sheet_reference_outside_grid_ring_does_not_remove_grid_label():
    reference = PdfTextObservation("nearby-sheet","Q-27",(122,92,150,101),font_size_pt=8)
    assert _grid_bubbles(page(texts=(label("2"),reference)))["2"] == pytest.approx((100,100),abs=.001)


def test_short_letter_number_grid_label_alone_remains_valid():
    assert _grid_bubbles(page(texts=(label("Q7"),)))["Q7"] == pytest.approx((100,100),abs=.001)


def test_detail_reference_font_bounds_can_overhang_its_source_ring():
    number = PdfTextObservation("detail-n","4",(97,102,103,111),font_size_pt=8)
    reference = PdfTextObservation("detail-ref","Q-27",(80,85,120,97),font_size_pt=10)
    assert _grid_bubbles(page(texts=(number,reference))) == {}


def test_adjacent_plan_caption_makes_number_a_view_label_not_a_grid():
    caption = PdfTextObservation("view-title","FLOOR PLAN EAST",(135,91,290,109),font_size_pt=12)
    assert _grid_bubbles(page(texts=(label("4"),caption))) == {}


def test_plan_caption_on_another_row_does_not_veto_a_grid():
    caption = PdfTextObservation("view-title","FLOOR PLAN EAST",(135,35,290,53),font_size_pt=12)
    assert _grid_bubbles(page(texts=(label("4"),caption)))["4"] == pytest.approx((100,100),abs=.001)


def test_plan_caption_font_baseline_offset_still_identifies_the_view_number():
    caption = PdfTextObservation("view-title","FLOOR PLAN EAST",(135,102,290,120),font_size_pt=12)
    assert _grid_bubbles(page(texts=(label("4"),caption))) == {}
