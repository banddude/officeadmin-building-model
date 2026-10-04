"""Synthetic numeric-noise regressions, not private source coordinates."""

import math
import pytest
from oabm.importers.pdf_architecture.importer import _poche_strip_polygons, _Transform2D
from oabm.importers.pdf_architecture.types import PdfLineObservation, ImportOptions


def detect(jitter=0, angle=0, scale=0.01, reverse=False):
    pts = ((0, 0), (500, 0), (500 + jitter, 12), (jitter, 12))
    c, s = math.cos(angle), math.sin(angle)
    pts = tuple((x * c - y * s + 100, y * c + x * s + 100) for x, y in pts)
    if reverse:
        pts = tuple(reversed(pts))
    lines = tuple(
        PdfLineObservation(
            str(i),
            a,
            b,
            primitive_family="polyline",
            filled=True,
            source_layers=("A-WALL-PATT",),
        )
        for i, (a, b) in enumerate(zip(pts, pts[1:] + pts[:1]))
    )
    return _poche_strip_polygons(
        lines, _Transform2D(scale, 0, 0, 0, "synthetic", 1), ImportOptions(), 1
    )


def test_exact_strip_is_unchanged():
    legs, rejections, _, stats = detect()
    assert len(legs) == 1 and not rejections and stats.junction_fill_count == 0
    assert legs[0].length_m == pytest.approx(5)
    assert legs[0].thickness_m == pytest.approx(0.12)


@pytest.mark.parametrize("angle", [0, 0.3, 1.2])
@pytest.mark.parametrize("scale", [0.01, 0.025])
def test_tiny_coordinate_noise_does_not_disappear_as_junction(angle, scale):
    legs, rejections, _, stats = detect(0.01, angle, scale)
    assert len(legs) == 1 and not rejections
    assert stats.junction_fill_count == 0
    assert legs[0].length_m == pytest.approx(500 * scale, abs=0.001)
    assert legs[0].thickness_m == pytest.approx(12 * scale, abs=0.001)
    assert 0 < legs[0].orthogonal_fit_error_pt <= min(0.1, 0.001 / scale)


@pytest.mark.parametrize("jitter,scale", [(0.3, 0.01), (0.15, 0.025), (2, 0.01)])
def test_meaningful_skew_is_refused_instead_of_flattened(jitter, scale):
    legs, rejections, _, stats = detect(jitter, scale=scale)
    assert not legs
    assert rejections
    assert stats.junction_fill_count == 0


def test_input_winding_preserves_fitted_geometry():
    first = detect(0.01, 0.3)[0]
    second = detect(0.01, 0.3, reverse=True)[0]
    assert len(first) == len(second) == 1
    assert sorted((first[0].start_pt, first[0].end_pt)) == pytest.approx(
        sorted((second[0].start_pt, second[0].end_pt))
    )


def test_model_records_fit_as_inferred_and_keeps_original_source_ids():
    from test_pdf_architecture_poche_walls import _strip_edges, _plan_page, _import
    from oabm.model import validate_model

    lines = _strip_edges(
        "synthetic-noisy", ((40, 200), (540, 200), (540.01, 212), (40.01, 212))
    )
    model = _import(_plan_page(*lines))
    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    fit = [
        p
        for p in wall.provenance
        if p.method == "bounded orthogonal fit of source wall-strip coordinates"
    ]
    assert len(fit) == 1 and fit[0].derivation == "inferred"
    assert fit[0].scope_paths == ("centerline", "thickness_m")
    assert 0 < fit[0].attributes["orthogonal_fit_max_error_m"] <= 0.001
    assert all(line.element_id in fit[0].source_element_id for line in lines)
    assert wall.attributes["pdf_architecture"]["orthogonal_fit_max_error_pt"] > 0


def test_fitting_does_not_repair_a_self_crossing_polygon():
    from oabm.importers.pdf_architecture.importer import _orthogonal_poche_frame

    assert (
        _orthogonal_poche_frame([(0, 0), (100, 0.001), (0.001, 10), (100, 10)], 0.01)
        is None
    )


def test_collinear_preprocessing_cannot_hide_a_large_boundary_deviation():
    pts = ((0, 0), (250, 0.5), (500, 0), (500.01, 12), (0.01, 12))
    lines = tuple(
        PdfLineObservation(
            str(i),
            a,
            b,
            primitive_family="polyline",
            filled=True,
            source_layers=("A-WALL-PATT",),
        )
        for i, (a, b) in enumerate(zip(pts, pts[1:] + pts[:1]))
    )
    legs, rejections, _, stats = _poche_strip_polygons(
        lines, _Transform2D(0.01, 0, 0, 0, "synthetic", 1), ImportOptions(), 1
    )
    assert not legs
    assert "poche_orthogonal_fit_unresolved" in {r["code"] for r in rejections}
    assert stats.junction_fill_count == 0


def test_exact_axis_alignment_does_not_allow_self_intersection():
    from oabm.importers.pdf_architecture.importer import _orthogonal_poche_frame

    points = [
        (0, 0),
        (20, 0),
        (20, 20),
        (10, 20),
        (10, -10),
        (30, -10),
        (30, 30),
        (0, 30),
    ]
    assert _orthogonal_poche_frame(points, 0.01) is None


def test_numerical_fit_does_not_promote_unlayered_fills_to_poche_walls():
    from dataclasses import replace
    from test_pdf_architecture_poche_walls import _strip_edges, _plan_page, _import

    lines = _strip_edges(
        "unlayered-noise", ((40, 200), (540, 200), (540.01, 212), (40.01, 212))
    )
    model = _import(_plan_page(*(replace(line, source_layers=()) for line in lines)))
    assert not any(
        wall.attributes.get("pdf_architecture", {}).get("recognition")
        == "poche_strip_wall_faces"
        for wall in model.walls
    )
