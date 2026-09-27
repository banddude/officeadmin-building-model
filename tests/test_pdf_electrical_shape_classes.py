"""Per-page symbol shape-class table tests (issue #200).

All PDFs are generated in-test in the electrical extractor's style: raw
content streams through pypdf, no answer keys, no private data.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_electrical import (
    PdfElectricalDocument,
    PdfVectorPathObservation,
    extract_pdf,
)
from oabm.importers.pdf_electrical.shape_classes import (
    ShapeClass,
    symbol_shape_classes,
)

# Symbol positions and sizes for the acceptance page.
CIRCLE_CENTRES = ((60.0, 700.0), (120.0, 700.0), (180.0, 700.0), (60.0, 640.0), (120.0, 640.0), (180.0, 640.0))
CIRCLE_RADIUS_PT = 4.5  # 9 pt diameter
FILLED_TRIANGLE_XY = ((60.0, 520.0), (120.0, 520.0), (180.0, 520.0), (240.0, 520.0))
OPEN_TRIANGLE_XY = ((60.0, 420.0), (120.0, 420.0), (180.0, 420.0))
TRIANGLE_LEG_PT = 7.0
OPEN_TRIANGLE_LEG_PT = 11.0
RECTANGLE_XY = ((300.0, 620.0), (300.0, 560.0))
RECTANGLE_SIDE_PT = 10.0
HEXAGON_CENTRE = (420.0, 380.0)
HEXAGON_RADIUS_PT = 6.0


def _bezier_circle(cx: float, cy: float, radius: float) -> str:
    k = 0.552284749831 * radius
    return "\n".join(
        [
            f"{cx - radius} {cy} m",
            f"{cx - radius} {cy + k} {cx - k} {cy + radius} {cx} {cy + radius} c",
            f"{cx + k} {cy + radius} {cx + radius} {cy + k} {cx + radius} {cy} c",
            f"{cx + radius} {cy - k} {cx + k} {cy - radius} {cx} {cy - radius} c",
            f"{cx - k} {cy - radius} {cx - radius} {cy - k} {cx - radius} {cy} c",
            "s",
        ]
    )


def _triangle(x: float, y: float, leg: float, paint: str) -> str:
    return f"{x} {y} m {x + leg} {y} l {x} {y + leg} l h {paint}"


def _hexagon(cx: float, cy: float, radius: float, paint: str) -> str:
    points = [
        (cx + radius * math.cos(math.radians(60 * index)), cy + radius * math.sin(math.radians(60 * index)))
        for index in range(6)
    ]
    first = f"{points[0][0]:.6f} {points[0][1]:.6f} m"
    rest = " l ".join(f"{x:.6f} {y:.6f}" for x, y in points[1:])
    return f"{first} {rest} l h {paint}"


def _acceptance_content() -> list[str]:
    content = [
        # Long wall lines must never appear in a shape-class table.
        "40 80 m 520 80 l S",
        "40 60 m 520 60 l S",
        "300 40 m 300 560 l S",
    ]
    for cx, cy in CIRCLE_CENTRES:
        content.append(_bezier_circle(cx, cy, CIRCLE_RADIUS_PT))
    for x, y in FILLED_TRIANGLE_XY:
        content.append(_triangle(x, y, TRIANGLE_LEG_PT, "f"))
    for x, y in OPEN_TRIANGLE_XY:
        content.append(_triangle(x, y, OPEN_TRIANGLE_LEG_PT, "S"))
    for x, y in RECTANGLE_XY:
        content.append(f"{x} {y} {RECTANGLE_SIDE_PT} {RECTANGLE_SIDE_PT} re S")
    cx, cy = HEXAGON_CENTRE
    content.append(_hexagon(cx, cy, HEXAGON_RADIUS_PT, "S"))
    return content


def _write_pdf(path: Path, content: list[str]) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612.0, height=792.0)
    stream = DecodedStreamObject()
    stream.set_data("\n".join(content).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject()
    with path.open("wb") as handle:
        writer.write(handle)


def _assert_gray(value: float | None, expected_if_carried: float) -> None:
    """The lane may or may not carry gray metadata; accept either state."""

    assert value is None or value == pytest.approx(expected_if_carried, abs=1e-9)


@pytest.fixture()
def acceptance_document(tmp_path: Path):
    pdf_path = tmp_path / "shape-class-page.pdf"
    _write_pdf(pdf_path, _acceptance_content())
    return extract_pdf(pdf_path, source_id="shape-classes-probe")


def test_acceptance_page_yields_exactly_the_drawn_classes(acceptance_document) -> None:
    classes = symbol_shape_classes(acceptance_document, 1)
    by_kind = {shape_class.kind: shape_class for shape_class in classes}

    # Exactly the five drawn classes; the kind key alone is not unique, so
    # the two triangle classes are counted separately below.
    assert len(classes) == 5
    assert set(by_kind) == {"circle", "rectangle", "triangle", "polygon6"}
    triangles = [shape_class for shape_class in classes if shape_class.kind == "triangle"]
    assert len(triangles) == 2

    circles = by_kind["circle"]
    assert circles.count == len(CIRCLE_CENTRES)
    assert circles.size_pt == 9.0
    assert circles.filled is False
    _assert_gray(circles.stroke_gray, 0.0)
    assert circles.fill_gray is None
    expected_centres = sorted(
        ((cx, cy) for cx, cy in CIRCLE_CENTRES), key=lambda centre: (centre[1], centre[0])
    )
    assert circles.sample_positions == tuple(expected_centres[:5])

    filled, open_tri = sorted(triangles, key=lambda shape_class: shape_class.size_pt)
    assert (filled.count, filled.size_pt, filled.filled) == (4, 7.0, True)
    _assert_gray(filled.fill_gray, 0.0)
    assert (open_tri.count, open_tri.size_pt, open_tri.filled) == (3, 11.0, False)
    assert open_tri.stroke_gray is None or open_tri.stroke_gray == pytest.approx(0.0, abs=1e-9)

    assert by_kind["rectangle"].count == len(RECTANGLE_XY)
    assert by_kind["rectangle"].size_pt == RECTANGLE_SIDE_PT
    assert by_kind["rectangle"].filled is False
    assert by_kind["polygon6"].count == 1
    assert by_kind["polygon6"].size_pt == 2 * HEXAGON_RADIUS_PT
    assert by_kind["polygon6"].filled is False


def test_classes_sort_by_count_descending_then_key(acceptance_document) -> None:
    classes = symbol_shape_classes(acceptance_document, 1)
    counts = [shape_class.count for shape_class in classes]
    assert counts == sorted(counts, reverse=True)
    keys = [
        (
            -shape_class.count,
            shape_class.kind,
            shape_class.size_pt,
            shape_class.filled,
            shape_class.stroke_gray is None,
            shape_class.stroke_gray or 0.0,
            shape_class.fill_gray is None,
            shape_class.fill_gray or 0.0,
        )
        for shape_class in classes
    ]
    assert keys == sorted(keys)


def test_shape_class_table_is_deterministic(acceptance_document) -> None:
    first = symbol_shape_classes(acceptance_document, 1)
    second = symbol_shape_classes(acceptance_document, 1)
    assert first == second
    assert [shape_class.to_dict() for shape_class in first] == [
        shape_class.to_dict() for shape_class in second
    ]


def test_two_circle_sizes_share_one_half_point_bucket(tmp_path: Path) -> None:
    content = [
        _bezier_circle(100.0, 600.0, 4.5),  # 9.0 pt
        _bezier_circle(200.0, 600.0, 4.6),  # 9.2 pt
    ]
    pdf_path = tmp_path / "bucket-pair.pdf"
    _write_pdf(pdf_path, content)
    document = extract_pdf(pdf_path, source_id="shape-classes-probe")
    classes = symbol_shape_classes(document, 1)
    assert len(classes) == 1
    assert classes[0].kind == "circle"
    assert classes[0].size_pt == 9.0
    assert classes[0].count == 2


def test_size_bounds_exclude_tiny_and_huge_shapes(tmp_path: Path) -> None:
    content = [
        _bezier_circle(100.0, 600.0, 0.3),  # 0.6 pt: below min_size_pt
        _bezier_circle(300.0, 600.0, 70.0),  # 140 pt: above max_size_pt
        _bezier_circle(500.0, 600.0, 4.5),  # 9.0 pt: kept
    ]
    pdf_path = tmp_path / "size-bounds.pdf"
    _write_pdf(pdf_path, content)
    document = extract_pdf(pdf_path, source_id="shape-classes-probe")
    classes = symbol_shape_classes(document, 1)
    assert [shape_class.count for shape_class in classes] == [1]
    assert classes[0].size_pt == 9.0


def test_page_filter_only_sees_the_requested_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    writer = PdfWriter()
    for content in (_acceptance_content()[:3], ["100 100 8 8 re f"]):
        page = writer.add_blank_page(width=612.0, height=792.0)
        stream = DecodedStreamObject()
        stream.set_data("\n".join(content).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject()
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    document = extract_pdf(pdf_path, source_id="shape-classes-probe")

    assert symbol_shape_classes(document, 1) == ()
    page_two = symbol_shape_classes(document, 2)
    assert len(page_two) == 1
    assert page_two[0].kind == "rectangle"
    assert page_two[0].filled is True
    assert page_two[0].size_pt == 8.0


def test_page_argument_is_validated(acceptance_document) -> None:
    with pytest.raises(ValueError):
        symbol_shape_classes(acceptance_document, 0)
    with pytest.raises(ValueError):
        symbol_shape_classes(acceptance_document, 2)
    with pytest.raises(ValueError):
        symbol_shape_classes(acceptance_document, True)  # type: ignore[arg-type]


def test_non_square_bezier_path_is_ignored(tmp_path: Path) -> None:
    # A single flattened elliptical arc: bezier-flattened but not square.
    content = [
        "100 400 m "
        "110 430 130 450 160 450 c "
        "190 450 210 430 220 400 c S"
    ]
    pdf_path = tmp_path / "ellipse-arc.pdf"
    _write_pdf(pdf_path, content)
    document = extract_pdf(pdf_path, source_id="shape-classes-probe")
    assert symbol_shape_classes(document, 1) == ()


def test_to_dict_is_json_ready() -> None:
    shape_class = ShapeClass(
        kind="circle",
        size_pt=9.0,
        filled=False,
        stroke_gray=None,
        fill_gray=0.05,
        count=2,
        sample_positions=((10.0, 20.0), (30.0, 40.0)),
    )
    payload = shape_class.to_dict()
    assert payload == {
        "kind": "circle",
        "size_pt": 9.0,
        "filled": False,
        "stroke_gray": None,
        "fill_gray": 0.05,
        "count": 2,
        "sample_positions": [[10.0, 20.0], [30.0, 40.0]],
    }
    assert json.loads(json.dumps(payload)) == payload


def _square(
    page: int,
    x: float,
    y: float,
    side: float,
    *,
    paint_operator: str,
    metadata: dict | None = None,
) -> PdfVectorPathObservation:
    return PdfVectorPathObservation(
        element_id=f"p{page}:vector:{x:.0f}{y:.0f}",
        page=page,
        points_pt=(
            (x, y),
            (x + side, y),
            (x + side, y + side),
            (x, y + side),
        ),
        closed=True,
        metadata={"paint_operator": paint_operator, **(metadata or {})},
    )


def test_metadata_grays_bucket_and_split_classes() -> None:
    document = PdfElectricalDocument(
        source_id="shape-classes-probe",
        page_count=1,
        vectors=(
            _square(1, 10.0, 10.0, 9.0, paint_operator="s", metadata={"stroke_gray": 0.0}),
            _square(1, 40.0, 10.0, 9.0, paint_operator="s", metadata={"stroke_gray": 0.03}),
            _square(1, 70.0, 10.0, 9.0, paint_operator="b"),
        ),
    )
    classes = symbol_shape_classes(document, 1)
    by_gray = {shape_class.stroke_gray: shape_class for shape_class in classes}
    assert set(by_gray) == {0.0, 0.05, None}
    assert by_gray[0.0].count == 1
    assert by_gray[0.05].count == 1  # 0.03 buckets to the 0.05 gray step
    assert by_gray[None].count == 1  # no metadata: unknown gray stays None


def test_closed_path_that_collapses_to_two_vertices_is_ignored() -> None:
    # A closed path that writes its closing vertex explicitly collapses to
    # two distinct corners after the closing point is dropped: not a shape.
    document = PdfElectricalDocument(
        source_id="shape-classes-probe",
        page_count=1,
        vectors=(
            PdfVectorPathObservation(
                element_id="p1:vector:degenerate",
                page=1,
                points_pt=((10.0, 10.0), (30.0, 10.0), (10.0, 10.0)),
                closed=True,
                metadata={"paint_operator": "S"},
            ),
        ),
    )
    assert symbol_shape_classes(document, 1) == ()


def test_fill_gray_buckets_without_dust() -> None:
    document = PdfElectricalDocument(
        source_id="shape-classes-probe",
        page_count=1,
        vectors=(
            _square(
                1,
                10.0,
                10.0,
                9.0,
                paint_operator="f",
                metadata={"fill_gray": 0.2999},
            ),
        ),
    )
    classes = symbol_shape_classes(document, 1)
    assert len(classes) == 1
    assert classes[0].fill_gray == 0.3
    assert classes[0].filled is True
