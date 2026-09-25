"""Register later architectural sheets to the resolved frame by shared walls.

Every case writes a synthetic multi-sheet architectural source PDF and runs it
through the real extractor and importer. No pre-extracted observations or
expected-output files are used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_architecture import ImportOptions
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.model import validate_model

MPP = 48 * 0.0254 / 72  # 1/4" = 1'-0"
PAGE_W, PAGE_H = 1728, 1152
WALL_T = 9.0
QUARTER = "SCALE: 1/4\" = 1'-0\""
OFFSET = (60.0, -45.0)
ORIGIN = (300.0, 300.0)


@dataclass(frozen=True)
class Plan:
    """One synthetic plan drawing: a double-line room plus an off-center stub."""

    origin: tuple[float, float]
    label: str
    room: tuple[float, float] = (354.0, 236.0)
    stub: tuple[float, float] = (120.0, 60.0)  # length, height above room bottom
    mirrored: bool = False
    repeat_offset: tuple[float, float] | None = None
    repeat_label: str | None = None


def _local(plan: Plan, point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    if plan.mirrored:
        x = plan.room[0] - x  # flipped about the room's vertical centre line
    return (plan.origin[0] + x, plan.origin[1] + y)


def _line(plan: Plan, a: tuple[float, float], b: tuple[float, float]) -> str:
    (ax, ay), (bx, by) = _local(plan, a), _local(plan, b)
    return f"{ax:.4f} {ay:.4f} m {bx:.4f} {by:.4f} l S"


def _text(x: float, y: float, value: str, size: int = 10) -> str:
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET"


def _plan_commands(plan: Plan) -> list[str]:
    w, h = plan.room
    stub_len, stub_y = plan.stub
    commands: list[str] = []
    copies = [plan]
    if plan.repeat_offset is not None:
        copies.append(
            Plan(
                origin=(plan.origin[0] + plan.repeat_offset[0], plan.origin[1] + plan.repeat_offset[1]),
                label=plan.repeat_label or plan.label,
                room=plan.room,
                stub=plan.stub,
                mirrored=plan.mirrored,
            )
        )
    for copy in copies:
        commands.append("/OC /WALL BDC")
        for inset in (0.0, WALL_T):
            a, b = inset, inset
            c, d = copy.room[0] - inset, copy.room[1] - inset
            commands.extend((
                _line(copy, (a, b), (c, b)),
                _line(copy, (c, b), (c, d)),
                _line(copy, (c, d), (a, d)),
                _line(copy, (a, d), (a, b)),
            ))
        # A wall stub off the east wall breaks the room's mirror symmetry.
        for offset in (0.0, WALL_T):
            commands.append(_line(copy, (copy.room[0], stub_y + offset), (copy.room[0] + stub_len, stub_y + offset)))
        commands.append("EMC")
        lx, ly = _local(copy, (60.0, copy.room[1] / 2.0))
        commands.append(_text(lx, ly, copy.label, size=12))
    return commands


def _add_sheet(writer: PdfWriter, sheet_number: str, plans: tuple[Plan, ...]) -> None:
    page = writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))
    wall = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-WALL"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({NameObject("/WALL"): wall}),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([wall]),
        NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
    })
    commands = [
        "1400 40 300 360 re S",
        _text(1412, 370, "PROJECT: SYNTHETIC BUILDING"),
        _text(1412, 340, "DRAWING TITLE: FLOOR PLAN"),
        _text(1412, 310, f"SHEET NO: {sheet_number}"),
        _text(1412, 250, QUARTER),
        *(command for plan in plans for command in _plan_commands(plan)),
    ]
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)


def _write(path: Path, *sheets: tuple[str, tuple[Plan, ...]]) -> Path:
    writer = PdfWriter()
    for sheet_number, plans in sheets:
        _add_sheet(writer, sheet_number, plans)
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


def _import(path: Path):
    model = import_observations(
        extract_pdf(path, source_id="fixture:architecture"),
        options=ImportOptions(),
    )
    validate_model(model)
    return model


def _frames(model):
    return model.attributes["pdf_architecture"]["drawing_regions"]


FIRST = Plan(ORIGIN, "ROOM: OFFICE")
SECOND_COPY = Plan((ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]), "ROOM: STUDY")


def test_offset_copy_of_the_plan_registers_to_the_first_region(tmp_path: Path) -> None:
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,))))

    regions = _frames(model)
    assert [region["status"] for region in regions] == ["resolved", "resolved"]
    first, second = regions
    assert first["frame"]["basis"] == "project_origin"
    assert second["frame"]["basis"] == "registered_to_region"
    evidence = second["frame"]["registered_to_region"]
    assert evidence["target_region_id"] == first["region_id"]
    assert evidence["target_page"] == 1
    assert evidence["evidence_kind"] == "visible_wall_layer"
    assert evidence["wall_inlier_count"] >= 8
    assert evidence["scale_ratio"] == pytest.approx(1.0)

    # The registered frame maps sheet 2's drawing onto sheet 1's drawing.
    second_frame, first_frame = second["frame"], first["frame"]
    assert second_frame["rotation_radians"] == pytest.approx(first_frame["rotation_radians"])
    assert second_frame["meters_per_point"] == pytest.approx(first_frame["meters_per_point"])
    assert second_frame["translation_m"][0] - first_frame["translation_m"][0] == pytest.approx(-OFFSET[0] * MPP, abs=1e-6)
    assert second_frame["translation_m"][1] - first_frame["translation_m"][1] == pytest.approx(-OFFSET[1] * MPP, abs=1e-6)
    assert evidence["confidence"] == pytest.approx(min(first_frame["confidence"], 0.85), abs=1e-6)

    # Canonical geometry of the two sheets coincides: one building, one frame.
    def wall_geometry(model) -> set[tuple[float, float, float, float]]:
        return {
            (
                round(point.x, 3), round(point.y, 3),
            )
            for wall in model.walls
            for point in (wall.centerline.points[0], wall.centerline.points[-1])
        }

    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (FIRST,))))
    assert wall_geometry(first_only) == wall_geometry(model)

    # The registration is recorded as inferred provenance, not as an observed
    # sheet fact.
    assert any(
        item.derivation == "inferred" and item.method == "inter-sheet registration by shared wall vectors"
        for item in model.provenance
    )


def test_a_different_plan_keeps_the_sheet_geometry_fallback(tmp_path: Path) -> None:
    different = Plan((ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]), "ROOM: SHOP", room=(200.0, 160.0), stub=(60.0, 50.0))
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (different,))))

    regions = _frames(model)
    assert regions[0]["frame"]["basis"] == "project_origin"
    assert regions[1]["frame"]["basis"] == "sheet_geometry_fallback"
    assert "registered_to_region" not in regions[1]["frame"]
    # The fallback anchors the drawing's lower left at the project origin.
    assert regions[1]["frame"]["confidence"] <= 0.40


def test_mirrored_plan_does_not_register(tmp_path: Path) -> None:
    mirrored = Plan(
        (ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]),
        "ROOM: STUDY",
        mirrored=True,
    )
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (mirrored,))))

    regions = _frames(model)
    assert regions[1]["frame"]["basis"] == "sheet_geometry_fallback"
    assert "registered_to_region" not in regions[1]["frame"]


def test_repeated_plan_does_not_register(tmp_path: Path) -> None:
    # The same plan twice on sheet 2, close enough to stay one drawing region:
    # two translations explain the walls, so none is accepted.
    repeated = Plan(
        (ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]),
        "ROOM: STUDY",
        repeat_offset=(414.0, 0.0),
        repeat_label="ROOM: ATRIUM",
    )
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (repeated,))))

    regions = _frames(model)
    assert regions[1]["frame"]["basis"] == "sheet_geometry_fallback"
    assert "registered_to_region" not in regions[1]["frame"]


def test_registration_is_deterministic(tmp_path: Path) -> None:
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,)))
    first = _import(path).to_json()
    second = _import(path).to_json()
    assert first == second
