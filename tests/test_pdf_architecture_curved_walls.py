"""Issue #226: synthetic source PDFs, never pre-extracted answer keys."""

import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture import ImportOptions, import_architectural_pdf
from oabm.model import validate_model


def _pdf(path: Path, *, mode="pair", scale=True):
    # Two cubic quarter circles; at 1:100, a 6 pt gap is 0.2117 m.
    commands = [
        "BT /F1 12 Tf 20 750 Td (A201 FLOOR PLAN) Tj ET",
        "BT /F1 12 Tf 20 720 Td (LEVEL: GROUND) Tj ET",
    ]
    if scale:
        commands.append("BT /F1 12 Tf 20 735 Td (SCALE: 1:100) Tj ET")
    if mode == "legend":
        commands += ["180 180 150 150 re S", "BT /F1 10 Tf 190 310 Td (LEGEND) Tj ET"]
    if mode == "dimension":
        commands += ["[3 2] 0 d", "BT /F1 10 Tf 280 270 Td (RADIUS 10 FT) Tj ET"]
    commands.append("300 200 m 300 255.228475 255.228475 300 200 300 c S")
    if mode != "isolated":
        outer_y = 316 if mode == "inconsistent" else 306
        commands.append(
            f"306 200 m 306 258.542183 258.542183 {outer_y} 200 {outer_y} c S"
        )
    if mode in {"hatched", "mixed"}:
        # Boundary returns close the annular band; radial hatch does not define it.
        commands += ["300 200 m 306 200 l S", "200 300 m 200 306 l S"]
        for angle in range(5, 90, 5):
            a = math.radians(angle)
            commands.append(
                f"{200+100*math.cos(a)} {200+100*math.sin(a)} m {200+106*math.cos(a)} {200+106*math.sin(a)} l S"
            )
    if mode == "mixed":
        commands += ["200 300 m 100 300 l 100 306 l 200 306 l S"]
    if mode == "unbounded_hatch":
        commands = commands[:3] + [
            f"{100+i} 100 m {110+i} 110 l S" for i in range(0, 100, 5)
        ]
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as f:
        writer.write(f)
    assert not path.with_suffix(".expected.json").exists()
    return path


def _import(path):
    return import_architectural_pdf(
        path,
        source_id="synthetic:curved-wall",
        options=ImportOptions(default_wall_height_m=3),
    )


@pytest.mark.parametrize("mode", ["pair", "hatched", "mixed"])
def test_curved_wall_pdf_centerline_and_provenance(tmp_path, mode):
    path = _pdf(tmp_path / "wall.pdf", mode=mode)
    model = _import(path)
    curved = [w for w in model.walls if len(w.centerline.points) > 2]
    assert len(curved) == 1
    wall = curved[0]
    mpp = 100 * 0.0254 / 72
    assert wall.thickness_m == pytest.approx(6 * mpp, abs=0.003)
    points = wall.centerline.points
    length = sum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) for a, b in zip(points, points[1:])
    )
    assert length == pytest.approx(103 * math.pi / 2 * mpp, abs=2 * mpp)
    assert 0 < wall.confidence < 1
    assert wall.provenance and wall.provenance[0].source_element_id
    assert wall.level_id in {l.id for l in model.levels}
    assert not validate_model(model)
    assert model.to_json() == _import(path).to_json()
    if mode == "mixed":
        assert len(model.walls) >= 2


@pytest.mark.parametrize(
    "mode", ["isolated", "dimension", "unbounded_hatch", "legend", "inconsistent"]
)
def test_unsupported_curves_stay_unresolved(tmp_path, mode):
    model = _import(_pdf(tmp_path / "wall.pdf", mode=mode))
    assert not model.walls


def test_curved_wall_requires_scale(tmp_path):
    model = _import(_pdf(tmp_path / "wall.pdf", scale=False))
    assert not model.walls


def test_sampled_source_and_centerline_chord_error(tmp_path):
    from oabm.importers.pdf_architecture.extract import extract_pdf

    path = _pdf(tmp_path / "wall.pdf")
    page = extract_pdf(path).pages[0]
    assert len(page.curves) == 2
    # A cubic quarter circle has ~0.03 pt intrinsic radial approximation error.
    for curve in page.curves:
        assert curve.max_chord_error_pt == 0.1
        radius = math.dist(curve.points_pt[0], (200, 200))
        assert (
            max(abs(math.dist(point, (200, 200)) - radius) for point in curve.points_pt)
            < 0.04
        )
        for a, b in zip(curve.points_pt, curve.points_pt[1:]):
            midpoint = tuple((x + y) / 2 for x, y in zip(a, b))
            assert abs(math.dist(midpoint, (200, 200)) - radius) < 0.11
    wall = _import(path).walls[0]
    mpp = 100 * 0.0254 / 72
    for a, b in zip(wall.centerline.points, wall.centerline.points[1:]):
        for t in (0, 0.25, 0.5, 0.75, 1):
            point = ((a.x * (1 - t) + b.x * t) / mpp, (a.y * (1 - t) + b.y * t) / mpp)
            assert abs(math.dist(point, (200, 200)) - 103) < 0.15
    assert len(wall.attributes["pdf_architecture"]["source_boundaries"]) == 2


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("isolated", "curved_wall_pair_unresolved"),
        ("dimension", "curved_wall_dimension_or_dashed"),
        ("legend", "curved_wall_title_or_legend"),
        ("inconsistent", "curved_wall_non_circular_or_unsupported"),
        ("unbounded_hatch", "architectural_geometry_unrecognized"),
    ],
)
def test_explicit_refusal_reasons(tmp_path, mode, expected):
    model = _import(_pdf(tmp_path / "wall.pdf", mode=mode))
    codes = {
        item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]
    }
    assert expected in codes


def test_missing_scale_reason(tmp_path):
    model = _import(_pdf(tmp_path / "wall.pdf", scale=False))
    assert "scale_unresolved" in {
        item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_mixed_join_is_one_wall_each_without_chord(tmp_path):
    model = _import(_pdf(tmp_path / "wall.pdf", mode="mixed"))
    assert len(model.walls) == 2
    curved = next(w for w in model.walls if len(w.centerline.points) > 2)
    straight = next(w for w in model.walls if len(w.centerline.points) == 2)
    assert (
        min(
            math.dist((a.x, a.y), (b.x, b.y))
            for a in (curved.centerline.points[0], curved.centerline.points[-1])
            for b in straight.centerline.points
        )
        < 0.002
    )


def _rewrite(path, transform):
    from pypdf import PdfReader

    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append(reader)
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(transform(page.get_contents().get_data().decode()).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_competing_concentric_boundaries_fail_closed(tmp_path):
    path = _pdf(tmp_path / "wall.pdf")
    _rewrite(
        path,
        lambda text: text + "\n312 200 m 312 261.855892 261.855892 312 200 312 c S",
    )
    model = _import(path)
    assert not model.walls
    assert "curved_wall_pair_ambiguous" in {
        v["code"] for v in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_boundary_order_does_not_change_wall_ids(tmp_path):
    path = _pdf(tmp_path / "wall.pdf")
    before = _import(path)

    def reorder(text):
        lines = text.splitlines()
        return "\n".join(lines[:3] + list(reversed(lines[3:])))

    _rewrite(path, reorder)
    after = _import(path)
    assert before.walls == after.walls


def test_curved_wall_applies_rotation_translation_and_height(tmp_path):
    from oabm.importers.pdf_architecture import RegistrationHint, LevelOverride

    path = _pdf(tmp_path / "wall.pdf")
    mpp = 100 * 0.0254 / 72
    options = ImportOptions(
        registrations=(
            RegistrationHint(1, (0, 0), (100, 0), (10, 20), (10, 20 + 100 * mpp)),
        ),
        level_overrides=(
            LevelOverride(page_number=1, name="Ground", elevation_m=5, height_m=3),
        ),
    )
    model = import_architectural_pdf(
        path, source_id="synthetic:curved-wall", options=options
    )
    assert len(model.walls) == 1
    for p in model.walls[0].centerline.points:
        assert p.z == 5
        # Invert the 90-degree rotation and translation.
        assert (
            abs(math.dist(((p.y - 20) / mpp, (10 - p.x) / mpp), (200, 200)) - 103)
            < 0.06
        )
    assert not validate_model(model)


def test_opening_is_not_placed_on_invisible_arc_chord(tmp_path):
    path = _pdf(tmp_path / "wall.pdf")
    _rewrite(
        path, lambda text: text + "\nBT /F1 8 Tf 266 266 Td (DOOR D1 3' X 7') Tj ET"
    )
    model = _import(path)
    assert not model.openings
    assert "curved_wall_opening_host_unresolved" in {
        v["code"] for v in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_hidden_wall_source_does_not_promote_unlayered_arcs(tmp_path):
    from dataclasses import replace
    from oabm.importers.pdf_architecture.extract import extract_pdf
    from oabm.importers.pdf_architecture.importer import import_observations

    source = extract_pdf(_pdf(tmp_path / "wall.pdf"))
    page = replace(source.pages[0], hidden_wall_source_present=True)
    model = import_observations(
        replace(source, pages=(page,)), options=ImportOptions(default_wall_height_m=3)
    )
    assert not model.walls
    assert "hidden_wall_layer_unresolved" in {
        item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_competing_level_labels_still_withhold_curves(tmp_path):
    path = _pdf(tmp_path / "wall.pdf")
    _rewrite(path, lambda text: text + "\nBT /F1 12 Tf 20 700 Td (LEVEL: SECOND) Tj ET")
    model = _import(path)
    assert not model.walls
    assert "level_ambiguous" in {
        item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_duplicate_pdf_strokes_do_not_duplicate_walls(tmp_path):
    path = _pdf(tmp_path / "wall.pdf")
    _rewrite(path, lambda text: text + "\n" + "\n".join(text.splitlines()[3:]))
    model = _import(path)
    assert len(model.walls) == 1
    assert len(model.walls[0].attributes["pdf_architecture"]["source_boundaries"]) == 2
