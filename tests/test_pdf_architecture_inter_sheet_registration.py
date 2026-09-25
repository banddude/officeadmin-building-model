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

from oabm.importers.pdf_architecture import ImportOptions, RegistrationHint
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
    texts: tuple[tuple[float, float, str], ...] = ()  # plan-local annotations


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
    for x, y, value in plan.texts:
        tx, ty = _local(plan, (x, y))
        commands.append(_text(tx, ty, value))
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


def _import(path: Path, *, source=None, options: ImportOptions | None = None):
    source = source or extract_pdf(path, source_id="fixture:architecture")
    model = import_observations(source, options=options or ImportOptions())
    validate_model(model)
    return model


def _regions(model):
    return model.attributes["pdf_architecture"]["drawing_regions"]


def _import_without_shared_walls(path: Path, monkeypatch: pytest.MonkeyPatch):
    """The importer as it behaved before shared-wall registration existed.

    Every sheet's registration is refused, and no two frames share evidence,
    so neither repeated walls nor repeated wall-loop footprints merge across
    regions: the only cross-region rule left is the exact-footprint space rule
    that predates this lane. (``_frames_share_evidence`` is the single gate of
    both merges.)
    """

    with monkeypatch.context() as patch:
        patch.setattr(
            architecture_importer,
            "_register_region_by_shared_walls",
            lambda *args, **kwargs: (None, {"disabled": True}),
        )
        patch.setattr(architecture_importer, "_frames_share_evidence", lambda first, second: False)
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


def _ambiguities(model, code: str) -> list[dict]:
    return [item for item in model.attributes["pdf_architecture"]["ambiguities"] if item["code"] == code]


def _wall_geometry(model) -> list[tuple]:
    return [
        (wall.id, wall.level_id, wall.centerline, wall.thickness_m, wall.height_m, wall.confidence)
        for wall in model.walls
    ]


@pytest.mark.parametrize(
    ("sheet_numbers", "labels"),
    [
        (("A101", "A102"), ("NOTE: GRID", "NOTE: GRID")),
        # Every sheet prints the same sheet number, so the repeated walls get
        # the very same stable ids: before, the model failed validation with a
        # duplicate wall entity id.
        (("A101", "A101"), ("NOTE: GRID", "NOTE: GRID")),
        (("A101", "A102"), ("ROOM: OFFICE", "ROOM: STUDY")),
    ],
    ids=["wall-faces", "repeated-sheet-number", "labelled-room-walls"],
)
def test_walls_repeated_by_a_registered_sheet_are_emitted_once(
    tmp_path: Path, sheet_numbers: tuple[str, str], labels: tuple[str, str],
) -> None:
    first_plan = replace(FIRST, label=labels[0])
    second_plan = replace(SECOND_COPY, label=labels[1])
    model = _import(_write(
        tmp_path / "plans.pdf", (sheet_numbers[0], (first_plan,)), (sheet_numbers[1], (second_plan,)),
    ))
    first_only = _import(_write(tmp_path / "first.pdf", (sheet_numbers[0], (first_plan,))))
    assert len(first_only.walls) == 4

    # One canonical wall per wall, with the identity, geometry, and confidence
    # the first sheet gives it.
    assert _wall_geometry(model) == _wall_geometry(first_only)

    first, second = _regions(model)
    assert second["status"] == "resolved"
    assert second["reason_codes"] == []
    assert second["frame"]["basis"] == "registered_to_region"
    assert second["entity_counts"]["walls"] == 0
    assert second["repeated_wall_count"] == 4
    assert second["repeated_geometry_region_ids"] == [first["region_id"]]
    assert "repeated_wall_count" not in first
    [duplicate] = _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert duplicate["page"] == 2
    assert duplicate["source_region_ids"] == [first["region_id"]]
    assert duplicate["wall_ids"] == sorted(wall.id for wall in first_only.walls)
    assert not _ambiguities(model, "repeated_wall_dimension_conflict")

    # The spaces name only kept wall ids, and the geometric loop space is not
    # emitted twice: with the same room label the model has exactly the first
    # sheet's spaces, naming the same kept wall ids. A renamed room adds its
    # own space on the same footprint, and the unreconciled names are recorded.
    assert len(model.spaces) == len(first_only.spaces) + (1 if labels[0] != labels[1] else 0)
    if labels[0] != labels[1]:
        [kept] = first_only.spaces
        [added] = [space for space in model.spaces if space.id != kept.id]
        assert added.name != kept.name
        # Each sheet's transform rounds the shared geometry differently in the
        # last bits, so the footprints agree to the lane's rounding, not bitwise.
        def footprint(space):
            return [(round(point.x, 6), round(point.y, 6)) for point in space.footprint.points]

        assert footprint(added) == footprint(kept)
        [renamed] = _ambiguities(model, "repeated_space_name_conflict")
        assert renamed["page"] == 2
        assert renamed["space_ids"] == [kept.id, added.id]
    else:
        assert not _ambiguities(model, "repeated_space_name_conflict")
    emitted_wall_ids = {wall.id for wall in model.walls}
    for space in model.spaces:
        lane = space.attributes["pdf_architecture"]
        if "wall_ids" in lane:
            assert set(lane["wall_ids"]) <= emitted_wall_ids
    if labels[0] == labels[1]:
        assert [space.id for space in model.spaces] == [space.id for space in first_only.spaces]
        for space, original in zip(model.spaces, first_only.spaces):
            assert (
                space.attributes["pdf_architecture"].get("wall_ids")
                == original.attributes["pdf_architecture"].get("wall_ids")
            )

    # The repeat is not dropped silently: each kept wall records the second
    # sheet's observation after its own.
    for wall, original in zip(model.walls, first_only.walls):
        assert wall.provenance[: len(original.provenance)] == original.provenance
        [repeat] = wall.provenance[len(original.provenance):]
        assert repeat.page == 2
        assert repeat.derivation == "observed"
        assert repeat.source_element_id
        assert repeat.source_element_id != original.provenance[0].source_element_id
        assert repeat.attributes["drawing_region_id"] == second["region_id"]
        assert repeat.attributes["repeats_drawing_region_id"] == first["region_id"]
    validate_model(model)


def test_a_repeated_wall_of_another_height_keeps_the_first_and_records_the_conflict(tmp_path: Path) -> None:
    # The two sheets draw the same room walls but scope different ceiling
    # heights to them. The first wall is kept as it was; the disagreement is
    # recorded instead of being resolved silently.
    first_plan = replace(FIRST, texts=((60.0, 95.0, "OFFICE CEILING HEIGHT 10'-0\""),))
    second_plan = replace(SECOND_COPY, texts=((60.0, 95.0, "STUDY CEILING HEIGHT 8'-0\""),))
    model = _import(_write(tmp_path / "plans.pdf", ("A101", (first_plan,)), ("A102", (second_plan,))))
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (first_plan,))))

    assert _wall_geometry(model) == _wall_geometry(first_only)
    assert [wall.height_m for wall in model.walls] == pytest.approx([10 * 12 * 0.0254] * 4)
    first, second = _regions(model)
    [conflict] = _ambiguities(model, "repeated_wall_dimension_conflict")
    assert conflict["page"] == 2
    assert conflict["source_region_ids"] == [first["region_id"]]
    assert conflict["wall_ids"] == sorted(wall.id for wall in first_only.walls)
    assert second["repeated_wall_count"] == 4


def test_a_door_drawn_only_on_the_repeating_sheet_is_hosted_by_the_kept_wall(tmp_path: Path) -> None:
    door = (120.0, 45.0, "DOOR D1 3'-0\" X 7'-0\"")  # inside the room, by the south wall
    model = _import(_write(
        tmp_path / "plans.pdf",
        ("A101", (replace(FIRST, label="NOTE: GRID"),)),
        ("A102", (replace(SECOND_COPY, label="NOTE: GRID", texts=(door,)),)),
    ))

    [opening] = model.openings
    wall_ids = {wall.id for wall in model.walls}
    assert opening.host_id in wall_ids
    [host] = [wall for wall in model.walls if wall.id == opening.host_id]
    # The host is the first sheet's south wall, which the second sheet repeats.
    assert host.provenance[0].page == 1
    assert {item.page for item in host.provenance} == {1, 2}
    assert opening.provenance[0].page == 2
    assert _regions(model)[1]["entity_counts"]["openings"] == 1
    assert not _ambiguities(model, "opening_host_unresolved")
    validate_model(model)


def test_the_same_plan_on_another_level_keeps_its_own_walls(tmp_path: Path) -> None:
    # Lookalike: sheet 2 is sheet 1's plan on the level above, placed at the
    # very same canonical XY by an explicit registration. Its walls coincide in
    # plan but are different walls.
    second_floor = replace(FIRST, label="NOTE: GRID", title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))
    third_floor = replace(SECOND_COPY, label="NOTE: GRID", title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",))
    a, b = (0.0, 0.0), (100.0, 0.0)
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(a[0] + OFFSET[0], a[1] + OFFSET[1]),
        source_b_pt=(b[0] + OFFSET[0], b[1] + OFFSET[1]),
        model_a_m=(a[0] * MPP, a[1] * MPP),
        model_b_m=(b[0] * MPP, b[1] * MPP),
    )
    model = _import(
        _write(tmp_path / "plans.pdf", ("A101", (second_floor,)), ("A102", (third_floor,))),
        options=ImportOptions(registrations=(hint,)),
    )

    first, second = _regions(model)
    assert second["frame"]["basis"] == "explicit_registration"
    assert first["level"]["level_id"] != second["level"]["level_id"]
    assert len(model.walls) == 8
    by_level: dict[str, set] = {}
    for wall in model.walls:
        by_level.setdefault(wall.level_id, set()).add(
            tuple(sorted((round(point.x, 6), round(point.y, 6)) for point in wall.centerline.points))
        )
    assert len(by_level) == 2
    [first_level, second_level] = by_level.values()
    assert first_level == second_level
    assert second["entity_counts"]["walls"] == 4
    assert "repeated_wall_count" not in second
    assert second["repeated_geometry_region_ids"] == []
    assert not _ambiguities(model, "duplicate_wall_identity_across_pages")


def test_a_lookalike_wall_beside_a_repeated_wall_stays_distinct(tmp_path: Path) -> None:
    # Sheet 2 repeats sheet 1's room and adds a second room just east of it.
    # The new room's west wall has the east wall's length and orientation and
    # lies half a metre east of it, which is far beyond the 0.05 m across
    # tolerance, so it stays distinct. It is also raised 40 pt, so this case
    # alone does not isolate the across bound: the aligned-end negatives below
    # pin which bound rejects which lookalike.
    half_metre_pt = 0.5 / MPP
    beside = Plan(
        (SECOND_COPY.origin[0] + FIRST.room[0] - WALL_T + half_metre_pt, SECOND_COPY.origin[1] + 40.0),
        "NOTE: GRID",
        room=(200.0, FIRST.room[1]),
        stub=None,
    )
    model = _import(_write(
        tmp_path / "plans.pdf",
        ("A101", (replace(FIRST, label="NOTE: GRID"),)),
        ("A102", (replace(SECOND_COPY, label="NOTE: GRID"), beside)),
    ))
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (replace(FIRST, label="NOTE: GRID"),))))

    first, second = _regions(model)
    assert second["frame"]["basis"] == "registered_to_region"
    assert second["repeated_wall_count"] == 4
    assert second["entity_counts"]["walls"] == 4
    assert len(model.walls) == 8
    kept = {wall.id: wall for wall in first_only.walls}
    new = [wall for wall in model.walls if wall.id not in kept]
    assert len(new) == 4
    for wall in new:
        assert wall.provenance[0].page == 2
        assert not any("repeats_drawing_region_id" in item.attributes for item in wall.provenance)
    for wall in model.walls:
        if wall.id in kept:
            assert wall.provenance[0] == kept[wall.id].provenance[0]
            assert wall.provenance[-1].attributes["drawing_region_id"] == second["region_id"]

    def x_span(wall) -> tuple[float, float]:
        xs = sorted(point.x for point in wall.centerline.points)
        return xs[0], xs[-1]

    [east] = [wall for wall in first_only.walls if x_span(wall)[0] == x_span(wall)[1] == max(x_span(w)[1] for w in first_only.walls)]
    [lookalike] = [wall for wall in new if x_span(wall)[0] == x_span(wall)[1] == min(x_span(w)[0] for w in new)]
    east_length = abs(east.centerline.points[-1].y - east.centerline.points[0].y)
    lookalike_length = abs(lookalike.centerline.points[-1].y - lookalike.centerline.points[0].y)
    assert lookalike_length == pytest.approx(east_length, abs=1e-6)
    # Half a metre between true centerlines; far beyond the 0.05 m repeat tolerance.
    assert 0.45 < x_span(lookalike)[0] - x_span(east)[0] < 0.65
    validate_model(model)


@pytest.mark.parametrize(
    "labels",
    [("ROOM: SHOP", "ROOM: DEN"), ("NOTE: GRID", "NOTE: GRID")],
    ids=["labelled-rooms", "unlabelled-loops"],
)
def test_fallback_sheets_of_different_plans_never_share_wall_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: tuple[str, str],
) -> None:
    # Two later sheets are both refused registration and both fall back to
    # their own lower-left frame. The fallback puts every sheet's largest wall
    # loop at the same canonical spot, so unrelated plans coincide there by
    # construction: their frames share no evidence and their walls stay
    # distinct (main's 12 walls and 3 spaces, no cross-page identity claim).
    path = _write(
        tmp_path / "plans.pdf",
        ("A101", (FIRST,)),
        ("A102", (Plan((900.0, 500.0), labels[0], room=(260.0, 200.0), stub=(80.0, 40.0)),)),
        ("A103", (Plan((200.0, 300.0), labels[1], room=(260.0, 140.0), stub=(150.0, 100.0)),)),
    )
    model = _import(path)
    assert len(model.spaces) == 3

    first, second, third = _regions(model)
    assert first["frame"]["basis"] == "project_origin"
    assert first["frame"]["evidence_roots"] == ["project"]
    for region in (second, third):
        assert region["frame"]["basis"] == "sheet_geometry_fallback"
        assert region["frame"]["evidence_roots"] == [f"region:{region['region_id']}"]
    assert second["frame"]["evidence_roots"] != third["frame"]["evidence_roots"]
    assert second["shared_wall_registration"]["reason_codes"] == ["insufficient_matched_evidence"]
    assert third["shared_wall_registration"]["reason_codes"] == ["insufficient_matched_evidence"]
    assert second["entity_counts"]["walls"] == 4
    assert third["entity_counts"]["walls"] == 4
    assert len(model.walls) == 12
    assert not _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert not any("repeats_drawing_region_id" in item.attributes for wall in model.walls for item in wall.provenance)
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_a_mirrored_refused_sheet_keeps_all_of_its_walls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sheet 3 is sheet 2 mirrored, so it is refused as orientation-incompatible
    # and falls back. The fallback then places its room on sheet 2's fallback
    # room exactly; unrelated fallback frames still must not fold its walls
    # into sheet 2's (main: 12 walls, not 8). Main's exact-footprint space rule,
    # which predates this lane, still drops sheet 3's loop space, which lands
    # exactly on sheet 2's: main's 2 spaces, unchanged.
    first = replace(FIRST, label="NOTE: GRID")
    second = Plan((250.0, 400.0), "NOTE: GRID", room=(260.0, 200.0), stub=(80.0, 40.0))
    mirrored = replace(second, origin=(700.0, 250.0), mirrored=True)
    path = _write(tmp_path / "plans.pdf", ("A101", (first,)), ("A102", (second,)), ("A103", (mirrored,)))
    model = _import(path)

    regions = _regions(model)
    assert [region["frame"]["basis"] for region in regions] == [
        "project_origin", "sheet_geometry_fallback", "sheet_geometry_fallback",
    ]
    attempt = regions[2]["shared_wall_registration"]
    assert "orientation_incompatible" in attempt["reason_codes"]
    assert [region["entity_counts"]["walls"] for region in regions] == [4, 4, 4]
    assert len(model.walls) == 12
    assert len(model.spaces) == 2
    assert not _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert not any("repeats_drawing_region_id" in item.attributes for wall in model.walls for item in wall.provenance)
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_an_ambiguously_oriented_refused_sheet_keeps_all_of_its_walls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sheet 3 has the same room as sheet 2 but its stub sits elsewhere, so no
    # orientation explains everything and the registration is refused as
    # ambiguous. Its fallback frame shares no evidence with sheet 2's, so its
    # walls stay distinct (main: 12 walls, not 8). As on main, its loop space
    # lands exactly on sheet 2's and is not emitted twice (main: 2 spaces).
    first = replace(FIRST, label="NOTE: GRID")
    second = Plan((250.0, 400.0), "NOTE: GRID", room=(260.0, 200.0), stub=(80.0, 40.0))
    third = Plan((500.0, 600.0), "NOTE: GRID", room=(260.0, 200.0), stub=(150.0, 120.0))
    path = _write(tmp_path / "plans.pdf", ("A101", (first,)), ("A102", (second,)), ("A103", (third,)))
    model = _import(path)

    regions = _regions(model)
    assert [region["frame"]["basis"] for region in regions] == [
        "project_origin", "sheet_geometry_fallback", "sheet_geometry_fallback",
    ]
    attempt = regions[2]["shared_wall_registration"]
    assert "ambiguous_orientation" in attempt["reason_codes"]
    assert [region["entity_counts"]["walls"] for region in regions] == [4, 4, 4]
    assert len(model.walls) == 12
    assert len(model.spaces) == 2
    assert not _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert not any("repeats_drawing_region_id" in item.attributes for wall in model.walls for item in wall.provenance)
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_a_near_copy_on_an_unrelated_fallback_frame_keeps_its_walls_and_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sheet 3 is sheet 2 mirrored and 1 pt wider: refused, it falls back, and
    # its walls and loop land within the 0.05 m repeat tolerance of sheet 2's
    # without coinciding exactly. Only the evidence gate keeps them apart:
    # main emits all 12 walls and 3 spaces, and so does this lane.
    first = replace(FIRST, label="NOTE: GRID")
    second = Plan((250.0, 400.0), "NOTE: GRID", room=(260.0, 200.0), stub=(80.0, 40.0))
    near = replace(second, origin=(700.0, 250.0), room=(261.0, 200.0), mirrored=True)
    path = _write(tmp_path / "plans.pdf", ("A101", (first,)), ("A102", (second,)), ("A103", (near,)))
    model = _import(path)

    regions = _regions(model)
    assert [region["frame"]["basis"] for region in regions] == [
        "project_origin", "sheet_geometry_fallback", "sheet_geometry_fallback",
    ]
    assert regions[1]["frame"]["evidence_roots"] != regions[2]["frame"]["evidence_roots"]
    assert [region["entity_counts"]["walls"] for region in regions] == [4, 4, 4]
    assert len(model.walls) == 12
    assert len(model.spaces) == 3
    assert not _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert not any("repeats_drawing_region_id" in item.attributes for wall in model.walls for item in wall.provenance)

    # The lookalikes are within the repeat bounds, so the gate is what holds.
    def page_of(entity) -> int:
        return entity.provenance[0].page

    walls = {page: [wall for wall in model.walls if page_of(wall) == page] for page in (2, 3)}
    along_limit = architecture_importer._MaterializedWalls(
        architecture_importer._REPEATED_WALL_TOLERANCE_M, ImportOptions().max_wall_thickness_m,
    ).along_limit_m
    for wall in walls[3]:
        assert any(
            architecture_importer._wall_repeats(
                wall, other, architecture_importer._REPEATED_WALL_TOLERANCE_M, along_limit,
            ) is not None
            for other in walls[2]
        )
    [second_space] = [space for space in model.spaces if page_of(space) == 2]
    [near_space] = [space for space in model.spaces if page_of(space) == 3]
    assert architecture_importer._footprints_coincide(
        near_space, second_space, architecture_importer._REPEATED_WALL_TOLERANCE_M,
    )
    assert near_space.footprint != second_space.footprint
    _assert_previous_behavior_kept(path, model, monkeypatch)


def test_a_same_extent_wall_offset_only_across_stays_distinct(tmp_path: Path) -> None:
    # Aligned-end negative for the across bound: sheet 2 registers to sheet 1
    # and adds a room whose east wall is a near copy of the west wall (same
    # orientation, about the same length, ends aligned) about 0.1 m west of
    # it. Nothing along the wall can reject it: the endpoint offsets are far
    # below the along bound, so only the across check keeps it distinct.
    gap_pt = 0.1 / MPP
    # The extra room's east centerline is designed to sit gap_pt west of the
    # main room's west centerline, with the same y extent.
    west_room = Plan(
        (SECOND_COPY.origin[0] + WALL_T - gap_pt - 200.0, SECOND_COPY.origin[1]),
        "ROOM: SHOP",
        room=(200.0, FIRST.room[1]),
        stub=None,
    )
    path = _write(
        tmp_path / "plans.pdf",
        ("A101", (replace(FIRST, label="NOTE: GRID"),)),
        ("A102", (replace(SECOND_COPY, label="NOTE: GRID"), west_room)),
    )
    model = _import(path)
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (replace(FIRST, label="NOTE: GRID"),))))

    first, second = _regions(model)
    assert second["frame"]["basis"] == "registered_to_region"
    assert second["repeated_wall_count"] == 4
    assert second["entity_counts"]["walls"] == 4
    assert len(model.walls) == 8
    kept = {wall.id for wall in first_only.walls}
    new = [wall for wall in model.walls if wall.id not in kept]
    assert len(new) == 4
    assert not any("repeats_drawing_region_id" in item.attributes for wall in new for item in wall.provenance)

    def x_span(wall) -> tuple[float, float]:
        xs = sorted(point.x for point in wall.centerline.points)
        return xs[0], xs[-1]

    def y_ends(wall) -> tuple[float, float]:
        ys = sorted(point.y for point in wall.centerline.points)
        return ys[0], ys[-1]

    [west] = [wall for wall in first_only.walls if x_span(wall)[0] == x_span(wall)[1] == min(x_span(w)[0] for w in first_only.walls)]
    vertical = [wall for wall in new if x_span(wall)[0] == x_span(wall)[1]]
    [lookalike] = [wall for wall in vertical if x_span(wall)[0] == max(x_span(w)[0] for w in vertical)]
    # Aligned ends: every endpoint offset along the wall stays far below the
    # along bound (the wall thickness), so the along bound cannot reject it.
    for a, b in zip(y_ends(west), y_ends(lookalike)):
        assert abs(a - b) < 0.05
    # The whole separation is across, and well beyond the 0.05 m across
    # tolerance.
    across = x_span(west)[0] - x_span(lookalike)[0]
    assert 0.08 < across < 0.30
    west_length = y_ends(west)[1] - y_ends(west)[0]
    lookalike_length = y_ends(lookalike)[1] - y_ends(lookalike)[0]
    assert abs(lookalike_length - west_length) <= 0.1
    validate_model(model)


def test_a_hint_placed_wider_room_repeats_only_the_west_wall(tmp_path: Path) -> None:
    # Aligned-end negative for the along bound: a hint-placed copy of the
    # second sheet with the room 30 pt wider shares the west wall exactly. Its
    # south and north walls are 30 pt longer at the aligned shared end (the
    # along bound keeps them), and its east wall is 30 pt away (the across
    # bound keeps it).
    wider = replace(SECOND_COPY, label="NOTE: GRID", room=(FIRST.room[0] + 30.0, FIRST.room[1]))
    a, b = (0.0, 0.0), (100.0, 0.0)
    # The hint places sheet 2's plan-local coordinates exactly where sheet 1
    # put them, so the shared west wall coincides.
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(SECOND_COPY.origin[0] + a[0], SECOND_COPY.origin[1] + a[1]),
        source_b_pt=(SECOND_COPY.origin[0] + b[0], SECOND_COPY.origin[1] + b[1]),
        model_a_m=((ORIGIN[0] + a[0]) * MPP, (ORIGIN[1] + a[1]) * MPP),
        model_b_m=((ORIGIN[0] + b[0]) * MPP, (ORIGIN[1] + b[1]) * MPP),
    )
    model = _import(
        _write(
            tmp_path / "plans.pdf",
            ("A101", (replace(FIRST, label="NOTE: GRID"),)),
            ("A102", (wider,)),
        ),
        options=ImportOptions(registrations=(hint,)),
    )
    first_only = _import(_write(tmp_path / "first.pdf", ("A101", (replace(FIRST, label="NOTE: GRID"),))))

    first, second = _regions(model)
    assert (first["frame"]["basis"], second["frame"]["basis"]) == ("project_origin", "explicit_registration")
    assert second["repeated_wall_count"] == 1
    assert second["entity_counts"]["walls"] == 3
    assert len(model.walls) == 7
    [duplicate] = _ambiguities(model, "duplicate_wall_identity_across_pages")
    assert not _ambiguities(model, "repeated_wall_dimension_conflict")

    def x_span(wall) -> tuple[float, float]:
        xs = sorted(point.x for point in wall.centerline.points)
        return xs[0], xs[-1]

    def ends(wall) -> tuple[tuple[float, float], tuple[float, float]]:
        return tuple(
            (round(point.x, 6), round(point.y, 6)) for point in (wall.centerline.points[0], wall.centerline.points[-1])
        )

    [west_only] = [
        wall for wall in first_only.walls
        if x_span(wall)[0] == x_span(wall)[1] == min(x_span(w)[0] for w in first_only.walls)
    ]
    [west] = [wall for wall in model.walls if ends(wall) == ends(west_only)]
    assert duplicate["wall_ids"] == [west.id]
    validate_model(model)


def test_a_later_different_plan_still_emits_its_walls(tmp_path: Path) -> None:
    # After sheet 2 (a repeat of sheet 1) contributes no new wall, sheet 3's
    # different plan still resolves and emits its own walls.
    path = _write(
        tmp_path / "plans.pdf",
        ("A101", (replace(FIRST, label="NOTE: GRID"),)),
        ("A101", (replace(SECOND_COPY, label="NOTE: GRID"),)),
        ("A101", (replace(OTHER, label="NOTE: GRID"),)),
    )
    model = _import(path)

    first, second, third = _regions(model)
    assert (first["status"], second["status"], third["status"]) == ("resolved",) * 3
    assert second["entity_counts"]["walls"] == 0
    assert third["entity_counts"]["walls"] > 0
    assert third["repeated_geometry_region_ids"] == []
    first_and_third = _import(_write(
        tmp_path / "two.pdf",
        ("A101", (replace(FIRST, label="NOTE: GRID"),)),
        ("A101", (replace(OTHER, label="NOTE: GRID"),)),
    ))
    assert len(model.walls) == len(first_and_third.walls)
    assert _wall_points(model) == _wall_points(first_and_third)
    validate_model(model)


@pytest.mark.parametrize(
    "sheet_numbers",
    [("A101", "A102", "A103"), ("A101", "A101", "A101")],
    ids=["distinct-sheet-numbers", "one-sheet-number"],
)
def test_repeated_registered_sheets_are_agreeing_electrical_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sheet_numbers: tuple[str, str, str],
) -> None:
    # Three copies of one plan. Registered to one frame they emit one wall set
    # and agree as targets, so the electrical sheet registers; with the
    # registration disabled the sheets fall back to unrelated frames and
    # compete as before.
    path = _write(
        tmp_path / "plans.pdf",
        (sheet_numbers[0], (replace(FIRST, label="NOTE: GRID"),)),
        (sheet_numbers[1], (replace(SECOND_COPY, label="NOTE: GRID"),)),
        (sheet_numbers[2], (replace(FIRST, origin=(ORIGIN[0] - 150.0, ORIGIN[1] + 200.0), label="NOTE: GRID"),)),
    )
    source = extract_pdf(path, source_id="fixture:architecture")
    electrical = extract_pdf(
        _write(tmp_path / "power.pdf", ("E101", (replace(FIRST, origin=(500.0, 350.0), label="ROOM: POWER"),))),
        source_id="fixture:electrical",
    )
    model = _import(path, source=source)

    regions = _regions(model)
    assert [region["status"] for region in regions] == ["resolved"] * 3
    assert [region.get("repeated_wall_count", 0) for region in regions] == [0, 4, 4]
    assert len(model.walls) == 4
    [registered] = register_electrical_sheets(model, source, electrical).pages
    assert registered.status == REGISTERED
    assert registered.record["registration"]["agreeing_region_ids"] == sorted(
        region["region_id"] for region in regions
    )
    mapped = registered.transform.apply(500.0, 350.0)
    assert (mapped.x, mapped.y) == pytest.approx((ORIGIN[0] * MPP, ORIGIN[1] * MPP), abs=1e-6)

    if len(set(sheet_numbers)) == 1:
        # Unregistered, two identical copies on one printed sheet number fall
        # back onto the same canonical spot and claim identical wall ids. No
        # valid model holds them twice (main rejects this input with a
        # duplicate-id ContractError), so the control uses distinct numbers.
        return
    unregistered = _import_without_shared_walls(path, monkeypatch)
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
