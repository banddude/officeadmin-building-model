"""Triangulated poché fills rebuild into wall strips.

Synthetic fixtures only: every filled strip is built in code as filled
triangles whose shared edges are delivered once, the way ``_unique_lines``
delivers them, and one generated source PDF exercises the real
``extract_pdf`` path.  A strip drawn as two triangles sharing a diagonal,
a chain of such rectangles, an L triangulated as a fan, and a strip whose
T-junction corner lands on another strip's face each rebuild into wall
legs; overlapping fills, a ring around a room, and isolated corner squares
fail closed with codes instead of walls.
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
    _is_wall_pattern_layer,
    extract_pdf,
)
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_architecture.types import (
    ImportOptions,
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfTextObservation,
    ScaleOverride,
)

# 1/8 inch = 1 foot.
MPP = 0.033867
PATTERN_LAYER = "A-WALL-PATT"
WALL_LAYER = "A-WALL"


def _pt(meters: float) -> float:
    return meters / MPP


def _text(
    element_id: str,
    text: str,
    x: float,
    y: float,
) -> PdfTextObservation:
    return PdfTextObservation(
        element_id=element_id, text=text, bbox_pt=(x, y, x + 80, y + 10)
    )


def _triangulated_edges(
    prefix: str,
    triangles: tuple[tuple[tuple[float, float], ...], ...],
    *,
    merged: dict[tuple[tuple[float, float], tuple[float, float]], PdfLineObservation]
    | None = None,
) -> tuple[PdfLineObservation, ...]:
    """The delivered edges of filled triangles: each unique edge once.

    ``merged`` optionally replaces one edge's observation, the way the
    extractor delivers a triangle edge that coincides exactly with an
    unfilled line on another layer.  Returns the edges keyed by their
    canonical endpoints so a test can name the diagonal.
    """

    unique: dict[tuple[tuple[float, float], tuple[float, float]], None] = {}
    for corners in triangles:
        for index in range(len(corners)):
            start = corners[index]
            end = corners[(index + 1) % len(corners)]
            key = tuple(sorted((start, end)))
            unique.setdefault(key, None)
    edges = []
    for position, key in enumerate(sorted(unique)):
        replacement = None if merged is None else merged.get(key)
        if replacement is not None:
            edges.append(replacement)
            continue
        edges.append(
            PdfLineObservation(
                element_id=f"{prefix}:e{position}",
                start_pt=key[0],
                end_pt=key[1],
                primitive_family="polyline",
                filled=True,
                source_layers=(PATTERN_LAYER,),
            )
        )
    return tuple(edges)


def _edge_ids(
    prefix: str,
    triangles: tuple[tuple[tuple[float, float], ...], ...],
) -> dict[tuple[tuple[float, float], tuple[float, float]], str]:
    """The element ids ``_triangulated_edges`` assigns, by edge endpoints."""

    unique: dict[tuple[tuple[float, float], tuple[float, float]], None] = {}
    for corners in triangles:
        for index in range(len(corners)):
            start = corners[index]
            end = corners[(index + 1) % len(corners)]
            key = tuple(sorted((start, end)))
            unique.setdefault(key, None)
    return {
        key: f"{prefix}:e{position}"
        for position, key in enumerate(sorted(unique))
    }


def _rectangle_triangles(
    x: float,
    y: float,
    length_pt: float,
    width_pt: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """A strip filled as two triangles sharing one diagonal."""

    return (
        ((x, y), (x + length_pt, y), (x + length_pt, y + width_pt)),
        ((x, y), (x + length_pt, y + width_pt), (x, y + width_pt)),
    )


def _plan_page(
    *lines: PdfLineObservation,
) -> PdfPageObservation:
    return PdfPageObservation(
        page_number=1,
        width_pt=612,
        height_pt=792,
        texts=(
            _text("tp:title", "A201 FLOOR PLAN", 20, 740),
            _text("tp:scale", "SCALE: 1/8\" = 1'-0\"", 20, 726),
            _text("tp:level", "LEVEL: GROUND", 20, 712),
        ),
        lines=tuple(lines),
    )


def _options() -> ImportOptions:
    return ImportOptions(
        scale_overrides=(ScaleOverride(1, MPP),),
        default_wall_height_m=3.0,
    )


def _import(
    page: PdfPageObservation,
    *,
    source_id: str = "fixture:triangulated-poche",
) -> object:
    return import_observations(
        PdfDocumentObservation(
            source_id=source_id,
            content_sha256="c" * 64,
            pages=(page,),
        ),
        options=_options(),
    )


def _ambiguity_codes(model: object) -> set[str]:
    return {
        item["code"]
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def _poche_diagnostics(model: object) -> dict[str, int]:
    for record in model.attributes["pdf_architecture"]["pages"]:
        diagnostics = record.get("geometric_wall_pair_diagnostics")
        if diagnostics is not None:
            return {
                key: diagnostics[key]
                for key in (
                    "poche_triangle_count",
                    "poche_piece_count",
                    "poche_piece_accepted_count",
                    "poche_piece_not_simple_count",
                    "poche_junction_fill_count",
                )
            }
    raise AssertionError("no geometric wall pair diagnostics on the model")


def _wall_length_m(wall: object) -> float:
    points = wall.centerline.points
    return math.hypot(
        points[-1].x - points[0].x,
        points[-1].y - points[0].y,
    )


X0, Y0 = 120.0, 300.0


def test_wall_pattern_layer_helper_matches_the_wall_pattern_names() -> None:
    assert _is_wall_pattern_layer("A-WALL-PATT")
    assert _is_wall_pattern_layer("AE-WALL-PATT")
    assert _is_wall_pattern_layer("XREF|A-WALL-PATT")
    assert not _is_wall_pattern_layer("A-WALL")
    assert not _is_wall_pattern_layer("A-HATCH-PATT")


def test_strip_of_two_triangles_is_one_wall() -> None:
    length = _pt(4.0)
    width = _pt(0.1016)
    triangles = _rectangle_triangles(X0, Y0, length, width)
    edges = _triangulated_edges("strip", triangles)
    ids = _edge_ids("strip", triangles)
    diagonal = tuple(sorted(((X0, Y0), (X0 + length, Y0 + width))))
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    attributes = wall.attributes["pdf_architecture"]
    assert attributes["recognition"] == "poche_strip_wall_faces"
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)
    assert wall.thickness_m == pytest.approx(0.1016, abs=1e-6)
    assert ids[diagonal] in attributes["source_boundaries"]
    assert len(attributes["source_boundaries"]) == 5
    assert attributes["poche_triangle_count"] == 2
    assert all(
        abs(other.thickness_m - 0.0508) > 1e-6 for other in model.walls
    )


def test_strip_of_two_triangles_at_a_wider_width_is_one_wall() -> None:
    length = _pt(4.0)
    width = _pt(0.127)
    edges = _triangulated_edges(
        "wide", _rectangle_triangles(X0, Y0, length, width)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    assert _wall_length_m(model.walls[0]) == pytest.approx(4.0, abs=1e-6)
    assert model.walls[0].thickness_m == pytest.approx(0.127, abs=1e-6)


def test_short_strip_below_the_hatch_length_is_one_wall() -> None:
    length = _pt(1.2)
    width = _pt(0.1016)
    edges = _triangulated_edges(
        "short", _rectangle_triangles(X0, Y0, length, width)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    assert _wall_length_m(model.walls[0]) == pytest.approx(1.2, abs=1e-6)
    assert model.walls[0].thickness_m == pytest.approx(0.1016, abs=1e-6)
    assert model.walls[0].attributes["pdf_architecture"][
        "recognition"
    ] == "poche_strip_wall_faces"


def test_chain_of_rectangles_sharing_seams_is_one_wall() -> None:
    width = _pt(0.1016)
    triangles: list[tuple[tuple[float, float], ...]] = []
    cursor = X0
    for segment in (1.5, 1.5, 1.0):
        span = _pt(segment)
        triangles.extend(_rectangle_triangles(cursor, Y0, span, width))
        cursor += span
    edges = _triangulated_edges("chain", tuple(triangles))
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    assert wall.attributes["pdf_architecture"][
        "recognition"
    ] == "poche_strip_wall_faces"
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)
    assert wall.thickness_m == pytest.approx(0.1016, abs=1e-6)


def test_l_fan_from_one_outer_corner_splits_into_two_legs() -> None:
    # The corner square of the L belongs to the horizontal leg, so the
    # vertical leg runs 2.0 m minus one width, like the solid L strip test.
    leg_h = _pt(3.0)
    leg_v = _pt(2.0)
    width = _pt(0.1016)
    corners = (
        (X0, Y0),
        (X0 + leg_h, Y0),
        (X0 + leg_h, Y0 + width),
        (X0 + width, Y0 + width),
        (X0 + width, Y0 + leg_v),
        (X0, Y0 + leg_v),
    )
    # A four-triangle fan from the outer corner where both arms meet.
    apex = corners[0]
    triangles = tuple(
        (apex, corners[index], corners[index + 1]) for index in range(1, 5)
    )
    edges = _triangulated_edges("fan", triangles)
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 2
    assert {
        wall.attributes["pdf_architecture"]["recognition"]
        for wall in model.walls
    } == {"poche_strip_wall_faces"}
    lengths = sorted(_wall_length_m(wall) for wall in model.walls)
    assert lengths[0] == pytest.approx(1.8984, abs=1e-6)
    assert lengths[1] == pytest.approx(3.0, abs=1e-6)
    assert {round(wall.thickness_m, 6) for wall in model.walls} == {
        round(0.1016, 6)
    }
    # Both legs name every edge of the one piece, diagonals included.
    for wall in model.walls:
        assert len(wall.attributes["pdf_architecture"]["source_boundaries"]) == 9


def test_perpendicular_strip_ending_on_a_face_stays_two_walls() -> None:
    # Strip B's corners are T-vertices on strip A's single face edge, so
    # the two fills touch without sharing an edge and stay two pieces.
    length = _pt(4.0)
    width = _pt(0.1016)
    main_triangles = _rectangle_triangles(X0, Y0, length, width)
    branch_x = X0 + length / 2.0
    branch_length = _pt(2.0)
    branch_triangles = _rectangle_triangles(
        branch_x, Y0 + width, width, branch_length
    )
    edges = _triangulated_edges(
        "tee", main_triangles + branch_triangles
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 2
    lengths = sorted(_wall_length_m(wall) for wall in model.walls)
    assert lengths[0] == pytest.approx(2.0, abs=1e-6)
    assert lengths[1] == pytest.approx(4.0, abs=1e-6)
    assert {
        round(wall.thickness_m, 6) for wall in model.walls
    } == {round(0.1016, 6)}


def test_extractor_merged_edges_still_rebuild_the_strip() -> None:
    # A triangle edge that coincides exactly with an unfilled line on
    # another layer arrives merged: unfilled, family "line", carrying both
    # layers.  The strip must rebuild all the same.
    length = _pt(4.0)
    width = _pt(0.1016)
    triangles = _rectangle_triangles(X0, Y0, length, width)
    bottom = tuple(sorted(((X0, Y0), (X0 + length, Y0))))
    cap = tuple(sorted(((X0, Y0), (X0, Y0 + width))))
    merged = {
        bottom: PdfLineObservation(
            element_id="merged:bottom",
            start_pt=bottom[0],
            end_pt=bottom[1],
            primitive_family="line",
            filled=False,
            source_layers=("A-FLOR", PATTERN_LAYER),
        ),
        cap: PdfLineObservation(
            element_id="merged:cap",
            start_pt=cap[0],
            end_pt=cap[1],
            primitive_family="line",
            filled=False,
            source_layers=("A-DOOR", PATTERN_LAYER),
        ),
    }
    edges = _triangulated_edges("merged", triangles, merged=merged)
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert len(model.walls) == 1
    wall = model.walls[0]
    assert wall.attributes["pdf_architecture"][
        "recognition"
    ] == "poche_strip_wall_faces"
    assert _wall_length_m(wall) == pytest.approx(4.0, abs=1e-6)
    assert wall.thickness_m == pytest.approx(0.1016, abs=1e-6)
    assert wall.attributes["pdf_architecture"]["source_layers"] == [
        "A-DOOR",
        "A-FLOR",
        PATTERN_LAYER,
    ]


def test_overlapping_fills_fail_closed_without_a_truncated_leg() -> None:
    width = _pt(0.127)
    length = _pt(2.0)
    full = _rectangle_triangles(X0, Y0, length, width)
    split_at = _pt(1.1)
    split = (
        *_rectangle_triangles(X0, Y0, split_at, width),
        *_rectangle_triangles(X0 + split_at, Y0, length - split_at, width),
    )
    edges = _triangulated_edges("overlap", full + split)
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert "poche_polygon_not_simple" in _ambiguity_codes(model)
    assert all(_wall_length_m(wall) >= 2.0 - 1e-6 for wall in model.walls)
    assert _poche_diagnostics(model)["poche_piece_not_simple_count"] == 1


def test_ring_around_a_room_fails_closed_but_pairs_its_faces() -> None:
    # Four trapezoids of one fill surround a room: the piece has a hole,
    # so it is not one simple loop.  Only the interior diagonals and corner
    # seams are consumed, and the outline faces still pair into the four
    # room walls.
    width = _pt(0.1016)
    room = _pt(3.0)
    inner = (
        (X0, Y0),
        (X0 + room, Y0),
        (X0 + room, Y0 + room),
        (X0, Y0 + room),
    )
    outer = (
        (X0 - width, Y0 - width),
        (X0 + room + width, Y0 - width),
        (X0 + room + width, Y0 + room + width),
        (X0 - width, Y0 + room + width),
    )
    triangles = []
    for index in range(4):
        first = inner[index]
        second = inner[(index + 1) % 4]
        third = outer[(index + 1) % 4]
        fourth = outer[index]
        triangles.append((first, second, third))
        triangles.append((first, third, fourth))
    edges = _triangulated_edges("ring", tuple(triangles))
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert "poche_polygon_not_simple" in _ambiguity_codes(model)
    assert len(model.walls) == 4
    for wall in model.walls:
        attributes = wall.attributes["pdf_architecture"]
        assert wall.thickness_m == pytest.approx(0.1016, abs=1e-6)
        assert attributes["recognition"] == "geometric_parallel_wall_faces"
        assert attributes["closed_loop"] is True
    assert all(wall.thickness_m >= 0.1 for wall in model.walls)
    assert _poche_diagnostics(model)["poche_piece_not_simple_count"] == 1


def test_small_square_is_counted_as_a_junction_fill() -> None:
    width = _pt(0.1016)
    edges = _triangulated_edges(
        "corner", _rectangle_triangles(X0, Y0, width, width)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert not [code for code in _ambiguity_codes(model) if "poche" in code]
    assert _poche_diagnostics(model)["poche_junction_fill_count"] == 1


def test_square_fill_is_not_a_wall_and_records_ambiguity() -> None:
    side = _pt(0.6)
    edges = _triangulated_edges(
        "column", _rectangle_triangles(X0, Y0, side, side)
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert "poche_strip_not_elongated" in _ambiguity_codes(model)


def test_too_thick_triangulated_strip_records_ambiguity() -> None:
    edges = _triangulated_edges(
        "thick", _rectangle_triangles(X0, Y0, _pt(3.0), _pt(0.5))
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert "poche_strip_thickness_out_of_range" in _ambiguity_codes(model)


def test_triangles_on_a_door_layer_give_no_wall() -> None:
    edges = _triangulated_edges(
        "door",
        _rectangle_triangles(X0, Y0, _pt(4.0), _pt(0.1016)),
    )
    edges = tuple(
        PdfLineObservation(
            element_id=edge.element_id,
            start_pt=edge.start_pt,
            end_pt=edge.end_pt,
            primitive_family=edge.primitive_family,
            filled=edge.filled,
            source_layers=("A-DOOR",),
        )
        for edge in edges
    )
    model = _import(_plan_page(*edges))

    validate_model(model)
    assert not model.walls
    assert not [code for code in _ambiguity_codes(model) if "poche" in code]


def _face_lines(
    prefix: str,
    x: float,
    y: float,
    length_pt: float,
    width_pt: float,
) -> tuple[PdfLineObservation, ...]:
    return (
        PdfLineObservation(
            element_id=f"{prefix}:face-a",
            start_pt=(x, y),
            end_pt=(x + length_pt, y),
            primitive_family="polyline",
            source_layers=(WALL_LAYER,),
        ),
        PdfLineObservation(
            element_id=f"{prefix}:face-b",
            start_pt=(x, y + width_pt),
            end_pt=(x + length_pt, y + width_pt),
            primitive_family="polyline",
            source_layers=(WALL_LAYER,),
        ),
    )


def test_line_pair_ending_on_a_poche_leg_is_junction_supported() -> None:
    length = _pt(4.0)
    width = _pt(0.1016)
    strip = _triangulated_edges(
        "host", _rectangle_triangles(X0, Y0, length, width)
    )
    pair_x = X0 + _pt(1.9)
    standoff = _pt(0.05)
    pair = tuple(
        PdfLineObservation(
            element_id=f"pair:line-{index}",
            start_pt=(pair_x + index * width, Y0 + width + standoff),
            end_pt=(pair_x + index * width, Y0 + width + standoff + _pt(3.0)),
            primitive_family="polyline",
            source_layers=(WALL_LAYER,),
        )
        for index in range(2)
    )
    model = _import(_plan_page(*strip, *pair))

    validate_model(model)
    assert len(model.walls) == 2
    pair_wall = next(
        wall
        for wall in model.walls
        if wall.attributes["pdf_architecture"]["recognition"]
        == "geometric_parallel_wall_face_partial"
    )
    attributes = pair_wall.attributes["pdf_architecture"]
    assert attributes["junction_supported"] is True
    assert attributes["junction_source"] == "poche_leg"


def test_line_pair_moved_off_the_poche_leg_stays_unresolved() -> None:
    length = _pt(4.0)
    width = _pt(0.1016)
    strip = _triangulated_edges(
        "host", _rectangle_triangles(X0, Y0, length, width)
    )
    pair_x = X0 + _pt(1.9)
    standoff = _pt(1.0)
    pair = tuple(
        PdfLineObservation(
            element_id=f"pair:line-{index}",
            start_pt=(pair_x + index * width, Y0 + width + standoff),
            end_pt=(pair_x + index * width, Y0 + width + standoff + _pt(3.0)),
            primitive_family="polyline",
            source_layers=(WALL_LAYER,),
        )
        for index in range(2)
    )
    model = _import(_plan_page(*strip, *pair))

    validate_model(model)
    assert len(model.walls) == 1
    assert model.walls[0].attributes["pdf_architecture"][
        "recognition"
    ] == "poche_strip_wall_faces"
    for record in model.attributes["pdf_architecture"]["pages"]:
        diagnostics = record.get("geometric_wall_pair_diagnostics")
        if diagnostics is not None:
            assert diagnostics["partial_no_junction_rejected_count"] == 1


def test_wall_drawn_as_triangulated_strip_and_line_pair_is_one_wall() -> None:
    length = _pt(4.0)
    width = _pt(0.1016)
    strip = _triangulated_edges(
        "twin", _rectangle_triangles(X0, Y0, length, width)
    )
    faces = _face_lines("twin", X0, Y0, length, width)
    model = _import(_plan_page(*strip, *faces))

    validate_model(model)
    assert len(model.walls) == 1
    assert _wall_length_m(model.walls[0]) == pytest.approx(4.0, abs=1e-6)
    assert model.walls[0].thickness_m == pytest.approx(0.1016, abs=1e-6)


def test_triangulated_poche_import_is_deterministic() -> None:
    width = _pt(0.1016)
    triangles = (
        *_rectangle_triangles(X0, Y0, _pt(4.0), width),
        *_rectangle_triangles(X0 + _pt(4.5), Y0, _pt(1.2), width),
    )
    edges = _triangulated_edges("det", triangles)
    page = _plan_page(*edges)
    first = _import(page, source_id="fixture:triangulated-det")
    second = _import(page, source_id="fixture:triangulated-det")
    reversed_page = _plan_page(*reversed(edges))
    third = _import(reversed_page, source_id="fixture:triangulated-det")

    assert [wall.id for wall in first.walls] == [
        wall.id for wall in second.walls
    ]
    assert [wall.id for wall in first.walls] == [
        wall.id for wall in third.walls
    ]
    assert first.to_json() == second.to_json()
    assert first.to_json() == third.to_json()


def _write_triangulated_source(path: Path) -> None:
    """Generated source PDF: two triangulated strips on a wall-pattern layer.

    Each strip is filled as two ``m l l h f`` triangles sharing one
    diagonal, and one unfilled line on the floor layer lies exactly on the
    long strip's lower face, the way an extractor merges a coincident
    triangle edge.
    """

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    patt_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject(PATTERN_LAYER),
    })
    flor_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-FLOR"),
    })
    patt_ref = writer._add_object(patt_group)
    flor_ref = writer._add_object(flor_group)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Properties"): DictionaryObject({
            NameObject("/PATT"): patt_ref,
            NameObject("/FLOR"): flor_ref,
        }),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([patt_ref, flor_ref]),
        NameObject("/D"): DictionaryObject({
            NameObject("/BaseState"): NameObject("/ON")
        }),
    })

    def triangle_commands(
        prefix: str,
        x: float,
        y: float,
        length_m: float,
        width_m: float,
    ) -> list[str]:
        length = length_m / MPP
        width = width_m / MPP
        corners = (
            (x, y),
            (x + length, y),
            (x + length, y + width),
            (x, y + width),
        )

        def coord(point: tuple[float, float]) -> str:
            return f"{point[0]:.4f} {point[1]:.4f}"

        return [
            f"/OC /PATT BDC {coord(corners[0])} m {coord(corners[1])} l "
            f"{coord(corners[2])} l h f EMC",
            f"/OC /PATT BDC {coord(corners[0])} m {coord(corners[2])} l "
            f"{coord(corners[3])} l h f EMC",
        ]

    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (A201 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 726 Tm (SCALE: 1/8\" = 1'-0\") Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 712 Tm (LEVEL: GROUND) Tj ET",
        # One unfilled floor line exactly on the long strip's lower face.
        f"/OC /FLOR BDC {X0:.4f} {Y0:.4f} m "
        f"{X0 + 4.0 / MPP:.4f} {Y0:.4f} l S EMC",
        *triangle_commands("long", X0, Y0, 4.0, 0.1016),
        *triangle_commands("short", X0, Y0 + 40.0, 1.2, 0.1016),
    ]
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_generated_pdf_triangulated_strips_import_through_the_real_extractor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-triangulated-poche.pdf"
    assert not source.with_suffix(".expected.json").exists()
    _write_triangulated_source(source)

    extracted = extract_pdf(source, source_id="fixture:triangulated-pdf")
    assert extract_pdf(source, source_id="fixture:triangulated-pdf") == extracted
    lines = extracted.pages[0].lines
    assert len(lines) == 10
    merged = [
        line
        for line in lines
        if line.source_layers == ("A-FLOR", PATTERN_LAYER)
    ]
    assert len(merged) == 1
    # Both the unfilled floor stroke and filled wall path survive deduplication.
    assert merged[0].filled
    assert merged[0].primitive_family == "polyline"
    assert merged[0].source_layers == ("A-FLOR", PATTERN_LAYER)

    model = import_observations(extracted, options=_options())
    validate_model(model)
    assert len(model.walls) == 2
    assert {
        wall.attributes["pdf_architecture"]["recognition"]
        for wall in model.walls
    } == {"poche_strip_wall_faces"}
    assert {round(_wall_length_m(wall), 3) for wall in model.walls} == {
        4.0,
        1.2,
    }
    assert {round(wall.thickness_m, 4) for wall in model.walls} == {
        round(0.1016, 4)
    }

    reimported = import_observations(
        extract_pdf(source, source_id="fixture:triangulated-pdf"),
        options=_options(),
    )
    assert model.to_json() == reimported.to_json()
