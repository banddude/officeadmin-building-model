"""Register later architectural sheets to the resolved frame by shared walls.

Every case writes a synthetic multi-sheet architectural source PDF and runs it
through the real extractor and importer. No pre-extracted observations or
expected-output files are used.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
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

from oabm.importers.pdf_architecture import ImportOptions
from oabm.importers.pdf_architecture import importer as architecture_importer
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_convergence.sheet_registration import (
    REGISTERED,
    REGISTRATION_PENDING,
    register_electrical_sheets,
)
from oabm.model import validate_model

MPP = 48 * 0.0254 / 72  # 1/4" = 1'-0"
PAGE_W, PAGE_H = 1728, 1152
WALL_T = 9.0
QUARTER = "SCALE: 1/4\" = 1'-0\""
OFFSET = (60.0, -45.0)
ORIGIN = (300.0, 300.0)
METHOD = "inter-sheet registration by shared wall vectors"


@dataclass(frozen=True)
class Plan:
    """One synthetic plan drawing: a double-line room plus an off-center stub."""

    origin: tuple[float, float]
    label: str
    room: tuple[float, float] = (354.0, 236.0)
    stub: tuple[float, float] | None = (120.0, 60.0)  # length, height above room bottom
    mirrored: bool = False
    title: str | None = None
    notes: tuple[str, ...] = ()


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
    commands = ["/OC /WALL BDC"]
    for inset in (0.0, WALL_T):
        a, b, c, d = inset, inset, w - inset, h - inset
        commands.extend((
            _line(plan, (a, b), (c, b)),
            _line(plan, (c, b), (c, d)),
            _line(plan, (c, d), (a, d)),
            _line(plan, (a, d), (a, b)),
        ))
    if plan.stub is not None:
        # A wall stub off the east wall breaks the room's mirror symmetry.
        stub_len, stub_y = plan.stub
        for offset in (0.0, WALL_T):
            commands.append(_line(plan, (w, stub_y + offset), (w + stub_len, stub_y + offset)))
    commands.append("EMC")
    lx, ly = _local(plan, (60.0, h / 2.0))
    commands.append(_text(lx, ly, plan.label, size=12))
    x0, y0 = _local(plan, (0.0, 0.0))
    if plan.title:
        commands.append(_text(x0, y0 - 40, plan.title, size=14))
    for index, note in enumerate(plan.notes):
        commands.append(_text(x0, y0 - 60 - 16 * index, note))
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


def _import(path: Path, *, source=None):
    source = source or extract_pdf(path, source_id="fixture:architecture")
    model = import_observations(source, options=ImportOptions())
    validate_model(model)
    return model


def _regions(model):
    return model.attributes["pdf_architecture"]["drawing_regions"]


def _import_without_shared_walls(path: Path, monkeypatch: pytest.MonkeyPatch):
    """The importer as it behaves when shared-wall registration refuses every sheet."""

    with monkeypatch.context() as patch:
        patch.setattr(
            architecture_importer,
            "_register_region_by_shared_walls",
            lambda *args, **kwargs: (None, {"disabled": True}),
        )
        return _import(path)


def _without_attempts(model) -> dict:
    data = json.loads(model.to_json())
    for region in data["attributes"]["pdf_architecture"]["drawing_regions"]:
        region.pop("shared_wall_registration", None)
    return data


def _assert_previous_behavior_kept(path: Path, model, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused registration leaves the whole model exactly as it was without it."""

    assert _without_attempts(model) == _without_attempts(_import_without_shared_walls(path, monkeypatch))


def _wall_points(model) -> set[tuple[float, float]]:
    return {
        (round(point.x, 3), round(point.y, 3))
        for wall in model.walls
        for point in (wall.centerline.points[0], wall.centerline.points[-1])
    }


FIRST = Plan(ORIGIN, "ROOM: OFFICE")
SECOND_COPY = Plan((ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]), "ROOM: STUDY")
# A different building: room and stub sizes shared with nothing on FIRST.
OTHER = Plan((ORIGIN[0] + OFFSET[0], ORIGIN[1] + OFFSET[1]), "ROOM: SHOP", room=(260.0, 200.0), stub=(80.0, 40.0))


def test_offset_copy_of_the_plan_registers_to_the_first_region(tmp_path: Path) -> None:
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,))))

    regions = _regions(model)
    assert [region["status"] for region in regions] == ["resolved", "resolved"]
    first, second = regions
    assert first["frame"]["basis"] == "project_origin"
    assert "shared_wall_registration" not in first
    assert second["frame"]["basis"] == "registered_to_region"
    assert second["frame"]["method"] == METHOD
    evidence = second["frame"]["registered_to_region"]
    assert evidence["target_region_id"] == first["region_id"]
    assert evidence["target_page"] == 1
    assert evidence["target_frame_basis"] == "project_origin"
    assert evidence["agreeing_region_ids"] == [first["region_id"]]
    assert evidence["derivation"] == "inferred"
    assert evidence["evidence_kind"] == "visible_wall_layer"
    assert evidence["wall_inlier_count"] == 10
    assert evidence["wall_coverage"] == pytest.approx(1.0)
    assert evidence["wall_residual_rms_m"] == pytest.approx(0.0, abs=1e-9)
    assert evidence["scale_ratio"] == pytest.approx(1.0)
    assert evidence["translation_pt"] == pytest.approx([-OFFSET[0], -OFFSET[1]], abs=1e-6)
    assert evidence["matched_evidence_sample"]
    attempt = second["shared_wall_registration"]
    assert attempt["status"] == "registered"
    assert attempt["reason_codes"] == []
    [candidate] = attempt["candidates"]
    assert candidate["accepted"] is True
    # The strongest turned placement explains fewer walls than the true one.
    assert candidate["orientation_alternative"]["wall_inlier_count"] < 10

    # The registered frame maps sheet 2's drawing onto sheet 1's drawing.
    second_frame, first_frame = second["frame"], first["frame"]
    assert second_frame["rotation_radians"] == pytest.approx(first_frame["rotation_radians"])
    assert second_frame["meters_per_point"] == pytest.approx(first_frame["meters_per_point"])
    assert second_frame["translation_m"][0] - first_frame["translation_m"][0] == pytest.approx(-OFFSET[0] * MPP, abs=1e-6)
    assert second_frame["translation_m"][1] - first_frame["translation_m"][1] == pytest.approx(-OFFSET[1] * MPP, abs=1e-6)
    # The target is the project origin (1.0); the wall method caps it at 0.85.
    assert second_frame["confidence"] == pytest.approx(0.85)
    assert evidence["confidence"] == pytest.approx(0.85)

    # Canonical geometry of the two sheets coincides: one building, one frame.
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (FIRST,))))
    assert _wall_points(first_only) == _wall_points(model)

    # The registration is recorded as inferred provenance, not as an observed
    # sheet fact.
    [inferred] = [item for item in model.provenance if item.method == METHOD]
    assert inferred.derivation == "inferred"
    assert inferred.page == 2
    assert inferred.confidence == pytest.approx(0.85)


def test_a_different_plan_keeps_the_sheet_geometry_fallback_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (OTHER,)))
    model = _import(path)

    first, second = _regions(model)
    assert first["frame"]["basis"] == "project_origin"
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    assert "registered_to_region" not in second["frame"]
    assert second["frame"]["confidence"] <= 0.40
    attempt = second["shared_wall_registration"]
    assert attempt["status"] == "refused"
    assert attempt["reason_codes"] == ["insufficient_matched_evidence"]
    assert not any(item.method == METHOD for item in model.provenance)
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_mirrored_plan_does_not_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirrored = replace(SECOND_COPY, mirrored=True)
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (mirrored,)))
    model = _import(path)

    second = _regions(model)[1]
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    assert "registered_to_region" not in second["frame"]
    attempt = second["shared_wall_registration"]
    assert attempt["reason_codes"] == ["orientation_incompatible"]
    [candidate] = attempt["candidates"]
    # The room outline alone matches unmirrored; the mirror explains every wall.
    assert candidate["wall_inlier_count"] == 8
    assert candidate["orientation_alternative"] == {
        "mirrored": True, "rotation_degrees": 0, "wall_inlier_count": 10,
    }
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_symmetric_plan_does_not_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the stub, a half turn explains the walls exactly as well.
    symmetric_first = replace(FIRST, stub=None)
    symmetric_copy = replace(SECOND_COPY, stub=None)
    path = _write(tmp_path / "plans.pdf", ("A101", (symmetric_first,)), ("A102", (symmetric_copy,)))
    model = _import(path)

    second = _regions(model)[1]
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    assert second["shared_wall_registration"]["reason_codes"] == ["ambiguous_orientation"]
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_repeated_plan_does_not_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same plan twice on sheet 2, close enough to stay one drawing region:
    # two translations explain the walls, so none is accepted.
    twin = replace(SECOND_COPY, origin=(SECOND_COPY.origin[0] + 534.0, SECOND_COPY.origin[1]), label="ROOM: ATRIUM")
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY, twin)))
    model = _import(path)

    regions = _regions(model)
    assert [region["scope"] for region in regions] == ["sheet", "sheet"]
    second = regions[1]
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    attempt = second["shared_wall_registration"]
    assert attempt["reason_codes"] == ["competing_transforms"]
    [candidate] = attempt["candidates"]
    assert candidate["competing_inlier_count"] == candidate["wall_inlier_count"] == 10
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_regions_placing_the_sheet_differently_are_competing_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sheet 2 is another building and falls back to its own lower-left frame.
    # Sheet 3 shows both buildings in one drawing, so it matches sheet 1 and
    # sheet 2, whose frames disagree. Neither is chosen.
    both = (
        replace(FIRST, origin=(200.0, 500.0), label="ROOM: STUDY"),
        replace(OTHER, origin=(200.0 + 354.0 + 60.0, 500.0)),
    )
    path = _write(
        tmp_path / "plans.pdf",
        ("A101", (FIRST,)),
        ("A102", (OTHER,)),
        ("A103", both),
    )
    model = _import(path)

    first, second, third = _regions(model)
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    assert third["frame"]["basis"] == "sheet_geometry_fallback"
    attempt = third["shared_wall_registration"]
    assert attempt["reason_codes"] == ["competing_targets"]
    assert attempt["competing_region_ids"] == sorted([first["region_id"], second["region_id"]])
    assert [candidate["accepted"] for candidate in attempt["candidates"]] == [True, True]
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_a_third_sheet_registers_when_every_same_level_region_agrees(tmp_path: Path) -> None:
    third_copy = replace(FIRST, origin=(ORIGIN[0] - 150.0, ORIGIN[1] + 200.0), label="ROOM: DEN")
    model = _import(
        _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,)), ("A103", (third_copy,))),
    )

    first, second, third = _regions(model)
    assert [region["frame"]["basis"] for region in (second, third)] == ["registered_to_region"] * 2
    evidence = third["frame"]["registered_to_region"]
    assert evidence["agreeing_region_ids"] == sorted([first["region_id"], second["region_id"]])
    assert evidence["translation_pt"] == pytest.approx(
        [
            (FIRST.origin[0] if evidence["target_page"] == 1 else SECOND_COPY.origin[0]) - third_copy.origin[0],
            (FIRST.origin[1] if evidence["target_page"] == 1 else SECOND_COPY.origin[1]) - third_copy.origin[1],
        ],
        abs=1e-6,
    )
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (FIRST,))))
    assert _wall_points(first_only) == _wall_points(model)


def test_the_same_plan_on_another_level_is_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_floor = replace(FIRST, title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))
    third_floor = replace(SECOND_COPY, title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",))
    path = _write(tmp_path / "plans.pdf", ("A101", (second_floor,)), ("A102", (third_floor,)))
    model = _import(path)

    first, second = _regions(model)
    assert first["level"]["name"] != second["level"]["name"]
    assert second["frame"]["basis"] == "sheet_geometry_fallback"
    # Only regions of the sheet's own level are candidates; none exists.
    assert "shared_wall_registration" not in second
    _assert_previous_behavior_kept(path, model, monkeypatch)

    # Control: the same sheet naming the same level registers.
    same_level = replace(SECOND_COPY, title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))
    control = _import(_write(tmp_path / "control.pdf", ("A101", (second_floor,)), ("A102", (same_level,))))
    assert _regions(control)[1]["frame"]["basis"] == "registered_to_region"


def test_a_region_of_a_split_sheet_registers_to_its_own_level(tmp_path: Path) -> None:
    # Sheet 2 holds two separately drawn floors. The second-floor drawing has
    # a resolved same-level region on sheet 1 and registers to it; the
    # third-floor drawing has none and still needs a RegistrationHint.
    second_floor = replace(FIRST, title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))
    split_second = replace(
        SECOND_COPY, origin=(120.0, 400.0), title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",),
    )
    split_third = replace(
        OTHER, origin=(800.0, 400.0), label="ROOM: DEN", title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",),
    )
    model = _import(
        _write(tmp_path / "plans.pdf", ("A101", (second_floor,)), ("A102", (split_second, split_third))),
    )

    first, second, third = _regions(model)
    assert (second["scope"], third["scope"]) == ("region", "region")
    assert second["status"] == "resolved"
    assert second["frame"]["basis"] == "registered_to_region"
    assert second["frame"]["registered_to_region"]["target_region_id"] == first["region_id"]
    assert second["frame"]["translation_m"] == pytest.approx(
        [(FIRST.origin[0] - 120.0) * MPP, (FIRST.origin[1] - 400.0) * MPP], abs=1e-6,
    )
    assert third["status"] == "unresolved"
    assert "registration_unresolved" in third["reason_codes"]
    assert "shared_wall_registration" not in third


def test_registered_sheets_let_an_electrical_sheet_see_one_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The electrical sheet matches both architectural sheets. With the later
    # sheet on its lower-left fallback frame the two targets disagree (#104
    # refuses competing_targets); registered to sheet 1 they agree.
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,)))
    source = extract_pdf(path, source_id="fixture:architecture")
    electrical = extract_pdf(
        _write(tmp_path / "power.pdf", ("E101", (replace(FIRST, origin=(500.0, 350.0), label="ROOM: POWER"),))),
        source_id="fixture:electrical",
    )

    [registered] = register_electrical_sheets(_import(path, source=source), source, electrical).pages
    assert registered.status == REGISTERED
    assert len(registered.record["registration"]["agreeing_region_ids"]) == 2
    mapped = registered.transform.apply(500.0, 350.0)
    assert (mapped.x, mapped.y) == pytest.approx((ORIGIN[0] * MPP, ORIGIN[1] * MPP), abs=1e-6)

    with monkeypatch.context() as patch:
        patch.setattr(
            architecture_importer,
            "_register_region_by_shared_walls",
            lambda *args, **kwargs: (None, {"disabled": True}),
        )
        unregistered = _import(path, source=source)
    [pending] = register_electrical_sheets(unregistered, source, electrical).pages
    assert pending.status == REGISTRATION_PENDING
    assert pending.reason_codes == ("competing_targets",)


def test_registration_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    path = _write(tmp_path / "plans.pdf", ("A101", (FIRST,)), ("A102", (SECOND_COPY,)))
    first = _import(path).to_json()
    second = _import(path).to_json()
    assert first == second

    rng = random.Random(72)
    observations = extract_pdf(path, source_id="fixture:architecture")
    pages = []
    for page in observations.pages:
        lines, texts = list(page.lines), list(page.texts)
        rng.shuffle(lines)
        rng.shuffle(texts)
        pages.append(replace(page, lines=tuple(lines), texts=tuple(texts)))
    shuffled = _import(path, source=replace(observations, pages=tuple(pages)))
    assert _regions(shuffled)[1]["frame"]["basis"] == "registered_to_region"
    assert shuffled.to_json() == first
