"""Walls drawn as filled poché strips become canonical walls.

Synthetic fixtures only: the strip polygons are built in code as closed
filled polylines on a wall-pattern layer, and one generated source PDF
exercises the real ``extract_pdf`` path.  A filled closed strip whose width
lies in the wall-thickness range and whose length is much greater than its
width is wall evidence; hatch fields, columns, and oversized fills stay out.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from oabm.model import validate_model
from oabm.importers.pdf_architecture.extract import (
    _is_wall_source_layer,
    extract_pdf,
)
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_architecture.types import (
    ImportOptions,
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
    ScaleOverride,
)

# 1/8 inch = 1 foot.
MPP = 0.033867
PATTERN_LAYER = "A-WALL-PATT"


def _text(
    element_id: str,
    text: str,
    x: float,
    y: float,
) -> PdfTextObservation:
    return PdfTextObservation(
        element_id=element_id, text=text, bbox_pt=(x, y, x + 80, y + 10)
    )


def _line(
    element_id: str,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    layer: str | None = None,
) -> PdfLineObservation:
    return PdfLineObservation(
        element_id=element_id,
        start_pt=start,
        end_pt=end,
        primitive_family="polyline",
        source_layers=(layer,) if layer else (),
    )


def _strip_edges(
    prefix: str,
    corners: tuple[tuple[float, float], ...],
    *,
    layer: str | None = PATTERN_LAYER,
) -> tuple[PdfLineObservation, ...]:
    """The delivered edges of one filled closed strip polygon."""

    names = ("first", "second", "third", "fourth", "fifth", "sixth")
    return tuple(
        PdfLineObservation(
            element_id=f"{prefix}:{names[index]}",
            start_pt=corners[index],
            end_pt=corners[(index + 1) % len(corners)],
            primitive_family="polyline",
            filled=True,
            source_layers=(layer,) if layer else (),
        )
        for index in range(len(corners))
    )


def _rect_strip_corners(
    x: float,
    y: float,
    length_m: float,
    width_m: float,
) -> tuple[tuple[float, float], ...]:
    return (
        (x, y),
        (x + length_m / MPP, y),
        (x + length_m / MPP, y + width_m / MPP),
        (x, y + width_m / MPP),
    )


def _plan_page(
    *lines: PdfLineObservation,
    rects: tuple[PdfRectObservation, ...] = (),
) -> PdfPageObservation:
    return PdfPageObservation(
        page_number=1,
        width_pt=612,
        height_pt=792,
        texts=(
            _text("poche:title", "A201 FLOOR PLAN", 20, 740),
            _text("poche:scale", "SCALE: 1/8\" = 1'-0\"", 20, 726),
            _text("poche:level", "LEVEL: GROUND", 20, 712),
        ),
        lines=tuple(lines),
        rects=rects,
    )


def _options() -> ImportOptions:
    return ImportOptions(
        scale_overrides=(ScaleOverride(1, MPP),),
        default_wall_height_m=3.0,
    )


def _import(
    page: PdfPageObservation,
    *,
    source_id: str = "fixture:poche-walls",
) -> object:
    return import_observations(
        PdfDocumentObservation(
            source_id=source_id,
            content_sha256="b" * 64,
            pages=(page,),
        ),
        options=_options(),
    )


def _ambiguity_codes(model: object) -> set[str]:
    return {
        item["code"]
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def _wall_length_m(wall: object) -> float:
    points = wall.centerline.points
    return math.hypot(
        points[-1].x - points[0].x,
        points[-1].y - points[0].y,
    )


def test_wall_pattern_layer_is_a_wall_source_layer() -> None:
    assert _is_wall_source_layer("A-WALL")
    assert _is_wall_source_layer("A-WALL-PATT")
    assert _is_wall_source_layer("AE-WALL-PATT")
    assert not _is_wall_source_layer("A-HATCH-PATT")
    assert not _is_wall_source_layer("A-TAG-NOTE")


def test_straight_strip_is_one_wall() -> None:
    edges = _strip_edges(
        "strip", _rect_strip_corners(120.0, 300.0, 4.0, 0.1)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    attributes = wall.attributes["pdf_architecture"]
    assert attributes["recognition"] == "poche_strip_wall_faces"
    assert attributes["source_layers"] == [PATTERN_LAYER]
    assert attributes["primitive_families"] == ["polyline"]
    assert len(attributes["source_boundaries"]) == 4
    assert all(
        record.derivation == "observed" for record in wall.provenance[:1]
    )
    assert wall.thickness_m == pytest.approx(0.1, abs=1e-6)
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)


def test_l_shaped_strip_splits_into_two_legs_meeting_at_the_corner() -> None:
    # Two 2.5 m legs of one filled L; the shared corner square belongs to the
    # first (horizontal) leg, so the vertical leg runs 2.4 m.
    x, y = 120.0, 250.0
    leg = 2.5 / MPP
    width = 0.1 / MPP
    edges = _strip_edges(
        "el",
        (
            (x, y),
            (x + leg, y),
            (x + leg, y + width),
            (x + width, y + width),
            (x + width, y + leg),
            (x, y + leg),
        ),
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 2
    assert {
        wall.attributes["pdf_architecture"]["recognition"]
        for wall in model.walls
    } == {"poche_strip_wall_faces"}
    lengths = sorted(_wall_length_m(wall) for wall in model.walls)
    assert lengths[0] == pytest.approx(2.4, abs=1e-6)
    assert lengths[1] == pytest.approx(2.5, abs=1e-6)
    assert {round(wall.thickness_m, 6) for wall in model.walls} == {
        round(0.1, 6)
    }
    # One wall per leg of the same strip polygon.
    boundaries = [
        tuple(wall.attributes["pdf_architecture"]["source_boundaries"])
        for wall in model.walls
    ]
    assert boundaries[0] == boundaries[1]
    assert len(boundaries[0]) == 6


def test_collinear_strips_with_a_gap_stay_two_walls() -> None:
    first = _strip_edges("gap-a", _rect_strip_corners(100.0, 300.0, 2.0, 0.1))
    second = _strip_edges(
        "gap-b",
        # A 0.9 m gap along the same centerline.
        _rect_strip_corners(
            100.0 + 2.9 / MPP, 300.0, 2.0, 0.1
        ),
    )
    model = _import(_plan_page(*first, *second))

    validate_model(model)
    assert len(model.walls) == 2
    assert {
        wall.attributes["pdf_architecture"]["recognition"]
        for wall in model.walls
    } == {"poche_strip_wall_faces"}
    for wall in model.walls:
        assert _wall_length_m(wall) == pytest.approx(2.0, abs=1e-6)
        assert wall.thickness_m == pytest.approx(0.1, abs=1e-6)
    span = sorted(
        (
            min(
                wall.centerline.points[0].x,
                wall.centerline.points[-1].x,
            ),
            max(
                wall.centerline.points[0].x,
                wall.centerline.points[-1].x,
            ),
        )
        for wall in model.walls
    )
    # Each strip's centerline runs its full 2.0 m in model metres and the
    # strips start 2.9 m apart, so the centerline spans keep the 0.9 m gap.
    assert span[1][0] - span[0][1] == pytest.approx(0.9, abs=1e-3)


def test_collinear_strips_touching_end_to_end_join_into_one_wall() -> None:
    # Two 2.0 m strips sharing their boundary edge: the wall is filled as two
    # rectangles in a row and must join into one 4.0 m wall.
    first = _strip_edges("join-a", _rect_strip_corners(100.0, 300.0, 2.0, 0.1))
    second = _strip_edges(
        "join-b", _rect_strip_corners(100.0 + 2.0 / MPP, 300.0, 2.0, 0.1)
    )
    model = _import(_plan_page(*first, *second))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    assert wall.attributes["pdf_architecture"]["recognition"] == (
        "poche_strip_wall_faces"
    )
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)
    assert wall.thickness_m == pytest.approx(0.1, abs=1e-6)


def test_collinear_strips_within_the_join_gap_become_one_wall() -> None:
    # A 2 inch drafting gap sits inside the six-inch collinear join tolerance
    # the face pipeline already applies to collinear CAD segments.
    first = _strip_edges("gap-join-a", _rect_strip_corners(100.0, 300.0, 2.0, 0.1))
    second = _strip_edges(
        "gap-join-b",
        _rect_strip_corners(
            100.0 + (2.0 + 2.0 * 0.0254) / MPP, 300.0, 2.0, 0.1
        ),
    )
    model = _import(_plan_page(*first, *second))

    validate_model(model)
    assert len(model.walls) == 1
    assert _wall_length_m(model.walls[0]) == pytest.approx(
        4.0 + 2.0 * 0.0254, abs=1e-6
    )


def test_wall_drawn_as_strip_and_line_pair_is_emitted_once() -> None:
    corners = _rect_strip_corners(120.0, 300.0, 3.0, 0.1)
    strip = _strip_edges("twin", corners)
    # The same wall also drawn as two face lines on the wall layer, plus one
    # perpendicular junction stub so the pair is junction supported.
    (x0, y0), _, (x1, y1), _ = corners
    faces = (
        _line("twin:face-south", (x0, y0), (x1, y0), layer="A-WALL"),
        _line("twin:face-north", (x0, y1), (x1, y1), layer="A-WALL"),
        _line(
            "twin:junction",
            (x0, (y0 + y1) / 2.0),
            (x0, (y0 + y1) / 2.0 + 30.0),
        ),
    )
    model = _import(_plan_page(*strip, *faces))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    # The line-pair wall is emitted first and keeps its identity; a lone pair
    # that closes no loop carries the partial recognition on main.
    assert wall.attributes["pdf_architecture"]["recognition"] == (
        "geometric_parallel_wall_face_partial"
    )
    assert _wall_length_m(wall) == pytest.approx(3.0, rel=1e-6)


def test_hatch_lines_inside_a_strip_add_no_walls() -> None:
    corners = _rect_strip_corners(120.0, 300.0, 4.0, 0.1)
    strip = _strip_edges("hatch-case", corners)
    x0, y0 = corners[0]
    x1, y1 = corners[2]
    pitch = (x1 - x0) / 6.0
    hatch = tuple(
        _line(
            f"hatch-case:diag-{index}",
            (x0 + index * pitch, y0 - 4.0),
            (x0 + index * pitch + (y1 - y0 + 8.0), y1 + 4.0),
            layer=PATTERN_LAYER,
        )
        for index in range(1, 6)
    )
    model = _import(_plan_page(*strip, *hatch))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    assert wall.attributes["pdf_architecture"]["recognition"] == (
        "poche_strip_wall_faces"
    )
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)


def test_filled_square_is_not_a_wall_and_records_ambiguity() -> None:
    side = 0.6 / MPP
    edges = _strip_edges("column", ((120.0, 300.0), (120.0 + side, 300.0),
                                     (120.0 + side, 300.0 + side), (120.0, 300.0 + side)))
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert "poche_strip_not_elongated" in _ambiguity_codes(model)


def test_too_thick_strip_is_not_a_wall_and_records_ambiguity() -> None:
    edges = _strip_edges(
        "wide", _rect_strip_corners(120.0, 300.0, 3.0, 0.5)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert "poche_strip_thickness_out_of_range" in _ambiguity_codes(model)


def test_solid_triangle_is_not_rectilinear_and_records_ambiguity() -> None:
    edges = _strip_edges(
        "tri",
        ((120.0, 300.0), (220.0, 300.0), (170.0, 340.0)),
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert "poche_polygon_not_rectilinear" in _ambiguity_codes(model)


def test_poche_wall_import_is_deterministic() -> None:
    edges = _strip_edges("det", _rect_strip_corners(120.0, 300.0, 4.0, 0.1))
    page = _plan_page(*edges)
    first = _import(page, source_id="fixture:poche-determinism")
    second = _import(page, source_id="fixture:poche-determinism")

    assert [wall.id for wall in first.walls] == [
        wall.id for wall in second.walls
    ]
    assert first.to_json() == second.to_json()


def _write_strip_source(path: Path) -> None:
    """Generated source PDF: filled strips on a wall-pattern layer.

    One straight strip arrives as a filled rectangle primitive and one L
    strip as a closed polyline path, the two ways an extractor delivers a
    filled poché strip.
    """

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    patt_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject(PATTERN_LAYER),
    })
    patt_ref = writer._add_object(patt_group)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({
            NameObject("/PATT"): patt_ref,
        }),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([patt_ref]),
        NameObject("/D"): DictionaryObject({
            NameObject("/BaseState"): NameObject("/ON")
        }),
    })
    length = 3.0 / MPP
    width = 0.1 / MPP
    straight = (
        f"120 300 {length:.4f} {width:.4f} re f"
    )
    leg = 2.0 / MPP
    l_path = (
        f"120 340 m {120 + leg:.4f} 340 l {120 + leg:.4f} {340 + width:.4f} l "
        f"{120 + width:.4f} {340 + width:.4f} l {120 + width:.4f} {340 + leg:.4f} l "
        f"120 {340 + leg:.4f} l 120 340 l h f"
    )
    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (A201 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 726 Tm (SCALE: 1/8\" = 1'-0\") Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 712 Tm (LEVEL: GROUND) Tj ET",
        f"/OC /PATT BDC {straight} EMC",
        f"/OC /PATT BDC {l_path} EMC",
    ]
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_generated_pdf_strips_import_through_the_real_extractor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-poche-strips.pdf"
    assert not source.with_suffix(".expected.json").exists()
    _write_strip_source(source)

    extracted = extract_pdf(source, source_id="fixture:poche-pdf")
    filled = [
        line
        for line in extracted.pages[0].lines
        if line.filled and line.primitive_family == "polyline"
    ]
    assert len(filled) == 6
    assert all(
        line.source_layers == (PATTERN_LAYER,) for line in filled
    )
    assert extract_pdf(source, source_id="fixture:poche-pdf") == extracted

    model = import_observations(extracted, options=_options())
    validate_model(model)
    assert len(model.walls) == 3
    assert {
        wall.attributes["pdf_architecture"]["recognition"]
        for wall in model.walls
    } == {"poche_strip_wall_faces"}
    assert {round(wall.thickness_m, 4) for wall in model.walls} == {
        round(0.1, 4)
    }
    lengths = sorted(round(_wall_length_m(wall), 3) for wall in model.walls)
    assert lengths == [1.9, 2.0, 3.0]

    reimported = import_observations(
        extract_pdf(source, source_id="fixture:poche-pdf"),
        options=_options(),
    )
    assert model.to_json() == reimported.to_json()
