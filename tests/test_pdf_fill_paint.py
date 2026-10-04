"""Synthetic paint observations and white annotation-mask refusals."""

from dataclasses import replace
import pytest
from oabm.importers.pdf_architecture.extract import (
    _unique_lines,
    _unique_rects,
    _curve_polyline_segments,
    _rect_edge_segments,
)
from oabm.importers.pdf_architecture.types import PdfLineObservation


def raw(gray, stroke=False):
    return {
        "x0": 10,
        "y0": 20,
        "x1": 110,
        "y1": 20,
        "_oabm_filled": True,
        "fill": True,
        "stroke": stroke,
        "non_stroking_color": gray,
        "stroking_color": (0, 0, 0),
    }


@pytest.mark.parametrize(
    "color,expected",
    [(0, 0.0), (0.4, 0.4), (1, 1.0), ((1, 1, 1), 1.0), ((1, 0, 0), 0.299)],
)
def test_fill_color_is_independent_of_black_stroke_state(color, expected):
    [line] = _unique_lines([raw(color)], 1)
    assert line.fill_grays == (expected,)
    assert line.stroke_present is False
    assert line.stroke_gray == 0


def test_coincident_white_and_black_fills_are_preserved_in_both_orders():
    first = _unique_lines([raw(0), raw(1)], 1)[0]
    second = _unique_lines([raw(1), raw(0)], 1)[0]
    assert first.fill_grays == second.fill_grays == (0.0, 1.0)
    assert first.element_id == second.element_id


def test_curve_fill_and_stroke_survive_flattening():
    obj = {
        "pts": [(10, 80), (110, 80)],
        "path": [("m", (10, 80)), ("l", (110, 80))],
        "fill": True,
        "stroke": False,
        "non_stroking_color": (1, 1, 1),
        "stroking_color": (0, 0, 0),
    }
    [line] = _unique_lines(_curve_polyline_segments([obj], 100), 1)
    assert line.fill_grays == (1.0,) and line.stroke_present is False


def test_rect_edges_keep_deduplicated_fill_paint():
    a = {**raw(1), "y1": 30}
    b = {**raw(0), "y1": 30}
    [rect] = _unique_rects([a, b], 1)
    assert rect.fill_grays == (0.0, 1.0)
    assert all(edge.fill_grays == (0.0, 1.0) for edge in _rect_edge_segments([rect], 1))


def test_white_mask_does_not_become_a_wall_even_on_a_wall_layer():
    from test_pdf_architecture_poche_walls import _strip_edges, _plan_page, _import

    lines = _strip_edges("mask", ((40, 200), (540, 200), (540.01, 212), (40.01, 212)))
    source = _plan_page(
        *(replace(line, fill_grays=(1.0,), stroke_present=False) for line in lines)
    )
    before = source
    model = _import(source)
    assert not model.walls
    assert source == before and len(source.lines) == 4


def test_dark_wall_fill_still_materializes():
    from test_pdf_architecture_poche_walls import _strip_edges, _plan_page, _import

    lines = _strip_edges("wall", ((40, 200), (540, 200), (540, 212), (40, 212)))
    model = _import(
        _plan_page(
            *(replace(line, fill_grays=(0.4,), stroke_present=False) for line in lines)
        )
    )
    assert len(model.walls) == 1


@pytest.mark.parametrize("gray", [0.0, 0.4, 1.0])
@pytest.mark.parametrize("rotated", [False, True])
def test_native_pdf_paint_is_preserved(tmp_path, gray, rotated):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from oabm.importers.pdf_architecture.extract import extract_pdf

    w = PdfWriter()
    p = w.add_blank_page(600, 800)
    transform = "0.8660254 0.5 -0.5 0.8660254 100 100 cm" if rotated else ""
    stream = DecodedStreamObject()
    stream.set_data(f"q {transform} 0 G {gray} g 40 100 400 12 re f Q".encode())
    p[NameObject("/Contents")] = w._add_object(stream)
    path = tmp_path / "synthetic-fill.pdf"
    w.write(path)
    page = extract_pdf(path).pages[0]
    objects = [*page.lines, *page.rects]
    filled = [x for x in objects if x.filled]
    assert filled
    assert all(x.fill_grays == (gray,) and x.stroke_present is False for x in filled)


@pytest.mark.parametrize(
    "paints,legacy,strict",
    [
        ((), True, False),
        ((None,), True, False),
        ((None, 0.0), False, False),
        ((0.0, 1.0), False, False),
        ((0.4,), True, True),
    ],
)
def test_unknown_and_conflicting_fill_policy_is_explicit(paints, legacy, strict):
    from oabm.importers.pdf_architecture.fill_paint import wall_fill_paint

    assert wall_fill_paint(paints) is legacy
    assert wall_fill_paint(paints, require_known=True) is strict


def test_strict_unlayered_strip_call_refuses_unknown_fill():
    from test_pdf_architecture_poche_walls import _strip_edges
    from oabm.importers.pdf_architecture.importer import (
        _poche_strip_polygons,
        _Transform2D,
    )
    from oabm.importers.pdf_architecture.types import ImportOptions

    lines = _strip_edges("unknown", ((0, 0), (500, 0), (500, 12), (0, 12)), layer=None)
    transform = _Transform2D(0.01, 0, 0, 0, "synthetic", 1)
    assert not _poche_strip_polygons(
        lines, transform, ImportOptions(), 1, require_known_paint=True
    )[0]
    known = tuple(
        replace(line, fill_grays=(0.4,), stroke_present=False) for line in lines
    )
    assert (
        len(
            _poche_strip_polygons(
                known, transform, ImportOptions(), 1, require_known_paint=True
            )[0]
        )
        == 1
    )


def test_known_black_stroke_is_not_erased_by_a_white_fill():
    from oabm.importers.pdf_architecture.fill_paint import is_white_mask

    obj = raw(1, stroke=True)
    [line] = _unique_lines([obj], 1)
    assert line.stroke_present is True
    assert not is_white_mask(line)


def test_unknown_fill_color_does_not_crash_or_become_black():
    [line] = _unique_lines([raw(("Pattern",))], 1)
    assert line.fill_grays == (None,)


@pytest.mark.parametrize("paints", [(0.0, 1.0), (1.0, 0.0), (None, 0.0)])
def test_conflicting_fill_only_paint_cannot_supply_wall_faces(paints):
    from test_pdf_architecture_poche_walls import _strip_edges, _plan_page, _import

    lines = _strip_edges(
        "conflicting-mask", ((40, 200), (540, 200), (540, 212), (40, 212))
    )
    model = _import(
        _plan_page(
            *(replace(line, fill_grays=paints, stroke_present=False) for line in lines)
        )
    )
    assert not model.walls


def test_any_actual_stroke_survives_coincident_fill_paths():
    a = raw(1, stroke=False)
    b = raw(0, stroke=True)
    assert _unique_lines([a, b], 1)[0].stroke_present is True
    assert _unique_lines([b, a], 1)[0].stroke_present is True
