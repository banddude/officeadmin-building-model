"""Synthetic partial arc evidence; never extend beyond both drawn faces."""

import math
from dataclasses import replace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture import import_architectural_pdf
from oabm.importers.pdf_architecture.curved_walls import arc_pairs
from oabm.importers.pdf_architecture.types import (
    ImportOptions,
    PdfCurveObservation,
    PdfPageObservation,
)
from oabm.model import validate_model


def arc(name, radius, start, end, *, center=(200, 200)):
    return PdfCurveObservation(
        name,
        tuple(
            (
                center[0]
                + radius * math.cos(math.radians(start + (end - start) * i / 100)),
                center[1]
                + radius * math.sin(math.radians(start + (end - start) * i / 100)),
            )
            for i in range(101)
        ),
    )


def pairs(*curves):
    return arc_pairs(
        PdfPageObservation(1, 600, 800, curves=tuple(curves)), 0.01, ImportOptions()
    )


@pytest.mark.parametrize(
    "extents,expected",
    [
        ((0, 90, 10, 80), (10, 80)),
        ((350, 450, 355, 435), (355, 435)),
        ((0, 90, 30, 120), (30, 90)),
    ],
)
def test_only_common_arc_extent_is_emitted(extents, expected):
    a, b, c, d = extents
    result, diagnostics = pairs(arc("inner", 80, a, b), arc("outer", 90, c, d))
    assert len(result) == 1
    wall = result[0]
    assert any(item["code"] == "curved_wall_partial_overlap" for item in diagnostics)
    assert wall.thickness_m == pytest.approx(0.1)
    for index, point in [(0, wall.points_pt[0]), (-1, wall.points_pt[-1])]:
        angle = math.radians(expected[index])
        assert point == pytest.approx(
            (200 + 85 * math.cos(angle), 200 + 85 * math.sin(angle)), abs=1e-7
        )
    assert {x.element_id for x in wall.boundaries} == {"inner", "outer"}
    assert all(
        math.hypot(x - 200, y - 200) == pytest.approx(85) for x, y in wall.points_pt
    )


def test_partial_pair_is_order_and_direction_stable():
    a, b = arc("inner", 80, 350, 450), arc("outer", 90, 355, 435)
    normal, _ = pairs(a, b)
    reordered, _ = pairs(
        replace(b, points_pt=tuple(reversed(b.points_pt))),
        replace(a, points_pt=tuple(reversed(a.points_pt))),
    )
    assert len(normal) == len(reordered) == 1
    assert normal[0].points_pt == reordered[0].points_pt
    assert sorted(x.element_id for x in normal[0].boundaries) == sorted(
        x.element_id for x in reordered[0].boundaries
    )


@pytest.mark.parametrize("other", [(100, 150), (85, 130), (150, 450)])
def test_disjoint_short_and_split_overlap_fail_closed(other):
    first = (0, 300) if other == (150, 450) else (0, 90)
    result, _ = pairs(arc("inner", 80, *first), arc("outer", 90, *other))
    assert result == ()


def test_partial_overlap_does_not_resolve_real_partner_ambiguity():
    result, diagnostics = pairs(
        arc("inner", 80, 0, 90), arc("middle", 90, 10, 80), arc("outer", 100, 10, 80)
    )
    assert result == ()
    assert any(d["code"] == "curved_wall_pair_ambiguous" for d in diagnostics)


def test_dashed_partial_face_is_not_a_wall():
    result, _ = pairs(
        arc("inner", 80, 0, 90), replace(arc("outer", 90, 10, 80), dashed=True)
    )
    assert result == ()


def test_equal_spans_still_pair():
    result, _ = pairs(arc("inner", 80, 0, 90), arc("outer", 90, 0, 90))
    assert len(result) == 1
    assert result[0].points_pt[0] == pytest.approx((285, 200))
    assert result[0].points_pt[-1] == pytest.approx((200, 285))


def test_synthetic_source_pdf_imports_only_common_extent(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    commands = [
        "BT /F1 12 Tf 20 750 Td (A201 FLOOR PLAN) Tj ET",
        "BT /F1 12 Tf 20 735 Td (SCALE: 1:100) Tj ET",
        "BT /F1 12 Tf 20 720 Td (LEVEL: GROUND) Tj ET",
    ]
    for observation in (arc("inner", 80, 0, 90), arc("outer", 90, 10, 80)):
        commands.append(
            " ".join(
                f'{x:.8f} {y:.8f} {"m" if i==0 else "l"}'
                for i, (x, y) in enumerate(observation.points_pt)
            )
            + " S"
        )
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "partial-arc.pdf"
    with path.open("wb") as f:
        writer.write(f)
    model = import_architectural_pdf(
        path,
        source_id="synthetic:partial-arc",
        options=ImportOptions(default_wall_height_m=3),
    )
    curved = [w for w in model.walls if len(w.centerline.points) > 2]
    assert len(curved) == 1
    assert not validate_model(model)
    wall = curved[0]
    mpp = 100 * 0.0254 / 72
    assert wall.thickness_m == pytest.approx(10 * mpp, abs=1e-6)
    endpoints = sorted(
        (point.x, point.y)
        for point in (wall.centerline.points[0], wall.centerline.points[-1])
    )
    expected = sorted(
        (
            (200 + 85 * math.cos(math.radians(angle))) * mpp,
            (200 + 85 * math.sin(math.radians(angle))) * mpp,
        )
        for angle in (10, 80)
    )
    for actual, wanted in zip(endpoints, expected):
        assert actual == pytest.approx(wanted, abs=1e-5)
    assert len(wall.provenance) >= 2
    assert (
        model.to_json()
        == import_architectural_pdf(
            path,
            source_id="synthetic:partial-arc",
            options=ImportOptions(default_wall_height_m=3),
        ).to_json()
    )
