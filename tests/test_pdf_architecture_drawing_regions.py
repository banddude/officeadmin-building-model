"""#103: level and canonical frame are resolved per drawing region, not per sheet.

Every case writes a synthetic source PDF and runs it through ``extract_pdf`` and
the importer. No pre-extracted observations or expected-output files are used.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

from oabm.importers.pdf_architecture import ImportOptions, LevelOverride, RegistrationHint, ScaleOverride
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import _level_name_candidates, import_observations
from oabm.importers.pdf_architecture.types import PdfPageObservation, PdfTextObservation
from oabm.model import BuildingModel, validate_model

QUARTER_INCH_MPP = 48 * 0.0254 / 72  # 1/4" = 1'-0"
PAGE_W, PAGE_H = 1728, 1152
LEFT = (120.0, 400.0)
RIGHT = (800.0, 400.0)
ROOM_W, ROOM_H, WALL_T = 354.0, 236.0, 9.0  # about 6.0 m x 4.0 m, 6 in walls
TITLE_BLOCK = (1400.0, 40.0, 1700.0, 400.0)


@dataclass(frozen=True)
class Drawing:
    origin: tuple[float, float]
    room: str = "ROOM: BEDROOM"
    title: str | None = None
    notes: tuple[str, ...] = ()


def _text(x: float, y: float, value: str, size: int = 10) -> str:
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET"


def _room_lines(origin: tuple[float, float]) -> list[str]:
    x0, y0 = origin
    x1, y1 = x0 + ROOM_W, y0 + ROOM_H
    result = []
    for inset in (0.0, WALL_T):
        a, b, c, d = x0 + inset, y0 + inset, x1 - inset, y1 - inset
        result.extend((
            f"{a} {b} m {c} {b} l S",
            f"{c} {b} m {c} {d} l S",
            f"{c} {d} m {a} {d} l S",
            f"{a} {d} m {a} {b} l S",
        ))
    return result


def _write_sheet(
    path: Path,
    drawings: tuple[Drawing, ...],
    *,
    sheet_texts: tuple[str, ...] = ("SCALE: 1/4\" = 1'-0\"",),
    layered: bool = True,
    sheet_frame: bool = True,
) -> Path:
    return _write_set(
        path,
        (drawings,),
        sheet_texts=sheet_texts,
        layered=layered,
        sheet_frame=sheet_frame,
    )


def _write_set(
    path: Path,
    sheets: tuple[tuple[Drawing, ...], ...],
    *,
    sheet_texts: tuple[str, ...] = ("SCALE: 1/4\" = 1'-0\"",),
    layered: bool = True,
    sheet_frame: bool = True,
) -> Path:
    """Write one synthetic sheet per entry of ``sheets``."""

    writer = PdfWriter()
    for drawings in sheets:
        _add_sheet(writer, drawings, sheet_texts=sheet_texts, layered=layered, sheet_frame=sheet_frame)
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


def _add_sheet(
    writer: PdfWriter,
    drawings: tuple[Drawing, ...],
    *,
    sheet_texts: tuple[str, ...],
    layered: bool,
    sheet_frame: bool,
) -> None:
    page = writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        # WinAnsi keeps the ASCII apostrophe a straight foot mark.
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
    commands: list[str] = []
    if sheet_frame:
        commands.extend((
            f"20 20 {PAGE_W - 40} {PAGE_H - 40} re S",
            f"24 24 {PAGE_W - 48} {PAGE_H - 48} re S",
        ))
    tx0, ty0, tx1, ty1 = TITLE_BLOCK
    commands.append(f"{tx0} {ty0} {tx1 - tx0} {ty1 - ty0} re S")
    commands.extend((
        _text(tx0 + 12, ty1 - 30, "PROJECT: SYNTHETIC RESIDENCE"),
        _text(tx0 + 12, ty1 - 60, "DRAWING TITLE: FLOOR PLANS"),
        _text(tx0 + 12, ty1 - 90, "SHEET NO: A9.1"),
    ))
    for index, value in enumerate(sheet_texts):
        commands.append(_text(tx0 + 12, ty1 - 150 - 20 * index, value))
    for drawing in drawings:
        x0, y0 = drawing.origin
        commands.append(_text(x0 + 110, y0 + ROOM_H / 2, drawing.room))
        if drawing.title is not None:
            commands.append(_text(x0, y0 - 40, drawing.title, size=14))
        for index, note in enumerate(drawing.notes):
            commands.append(_text(x0, y0 - 60 - 16 * index, note))
        if layered:
            commands.append("/OC /WALL BDC")
        commands.extend(_room_lines(drawing.origin))
        if layered:
            commands.append("EMC")
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)


def _import(path: Path, options: ImportOptions | None = None) -> BuildingModel:
    model = import_observations(extract_pdf(path, source_id="fixture:drawing-regions"), options=options)
    validate_model(model)
    return model


def _regions(model: BuildingModel) -> list[dict[str, object]]:
    return model.attributes["pdf_architecture"]["drawing_regions"]


def _codes(model: BuildingModel) -> set[str]:
    return {item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]}


def _stacked_hint(**overrides: object) -> RegistrationHint:
    """Register the right-hand drawing directly over the left-hand one."""

    values: dict[str, object] = dict(
        page_number=1,
        source_a_pt=RIGHT,
        source_b_pt=(RIGHT[0] + ROOM_W, RIGHT[1]),
        model_a_m=(LEFT[0] * QUARTER_INCH_MPP, LEFT[1] * QUARTER_INCH_MPP),
        model_b_m=((LEFT[0] + ROOM_W) * QUARTER_INCH_MPP, LEFT[1] * QUARTER_INCH_MPP),
        note="caller-confirmed stacked floor registration",
        region_point_pt=(RIGHT[0] + 20, RIGHT[1] + 20),
    )
    values.update(overrides)
    return RegistrationHint(**values)  # type: ignore[arg-type]


SECOND = Drawing(LEFT, title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))
THIRD = Drawing(RIGHT, room="ROOM: DEN", title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",))


def _entities_on(model: BuildingModel, level_id: str) -> tuple[int, int]:
    return (
        sum(space.level_id == level_id for space in model.spaces),
        sum(wall.level_id == level_id for wall in model.walls),
    )


def test_single_drawing_sheet_resolves_one_region_with_one_level_and_frame(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "single.pdf", (SECOND,))
    model = _import(path)

    [region] = _regions(model)
    assert region["scope"] == "sheet"
    assert region["status"] == "resolved"
    assert region["reason_codes"] == []
    assert region["evidence"] == {"kind": "visible_wall_layer", "segment_count": 8}
    # The region is the wall drawing, not the sheet frame or the title block.
    x0, y0, x1, y1 = region["source_bbox_pt"]
    assert (x0, y0) == pytest.approx(LEFT)
    assert (x1, y1) == pytest.approx((LEFT[0] + ROOM_W, LEFT[1] + ROOM_H))
    [level] = model.levels
    assert region["level"]["level_id"] == level.id
    assert level.name == "Second Floor"
    assert level.elevation_m == pytest.approx(3.048)
    assert region["level"]["name_source_element_ids"]
    assert region["level"]["elevation_source_element_id"]
    assert region["scale"]["meters_per_point"] == pytest.approx(QUARTER_INCH_MPP)
    assert region["scale"]["source_element_id"]
    assert region["frame"]["frame_id"] == model.coordinate_system.frame_id
    assert region["frame"]["basis"] == "project_origin"
    assert region["confidence"] == pytest.approx(min(level.confidence, 0.98, 1.0))
    assert _entities_on(model, level.id) == (1, 4)
    space = model.spaces[0]
    assert space.provenance[0].page == 1
    assert 0 < space.confidence <= 1


def test_two_regions_with_distinct_levels_resolve_separately(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "two-levels.pdf", (SECOND, THIRD))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    assert [left["scope"], right["scope"]] == ["region", "region"]
    assert [left["status"], right["status"]] == ["resolved", "resolved"]
    assert left["level"]["name"] == "Second Floor"
    assert right["level"]["name"] == "Third Floor"
    assert left["level"]["level_id"] != right["level"]["level_id"]
    assert left["level"]["elevation_m"] == pytest.approx(3.048)
    assert right["level"]["elevation_m"] == pytest.approx(6.096)
    # One frame each, and not the same page-level transform.
    assert left["frame"]["basis"] == "project_origin"
    assert right["frame"]["basis"] == "explicit_registration"
    assert right["frame"]["method"] == "caller-confirmed stacked floor registration"
    assert left["frame"]["translation_m"] != right["frame"]["translation_m"]
    assert right["frame"]["translation_m"][0] == pytest.approx(-(RIGHT[0] - LEFT[0]) * QUARTER_INCH_MPP)
    for region in (left, right):
        assert _entities_on(model, region["level"]["level_id"]) == (1, 4)

    by_level = {space.level_id: space for space in model.spaces}
    lower = by_level[left["level"]["level_id"]]
    upper = by_level[right["level"]["level_id"]]
    assert lower.name == "BEDROOM"
    assert upper.name == "DEN"
    # The registered upper floor stacks over the lower one at its own elevation.
    def flat(space: object) -> list[float]:
        return [value for p in sorted((p.x, p.y) for p in space.footprint.points) for value in p]

    assert flat(upper) == pytest.approx(flat(lower))
    assert all(p.z == pytest.approx(6.096) for p in upper.footprint.points)
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == "geometry_imported"

    # Each wall is built only from source vectors inside its own drawing region.
    lines = {line.element_id: line for line in extract_pdf(path, source_id="x").pages[0].lines}
    bbox_by_level = {
        region["level"]["level_id"]: region["source_bbox_pt"] for region in (left, right)
    }
    for wall in model.walls:
        x0, y0, x1, y1 = bbox_by_level[wall.level_id]
        element_ids = wall.provenance[0].source_element_id.rsplit(":", 1)[0].split("+")
        assert element_ids
        for element_id in element_ids:
            line = lines[element_id]
            for x, y in (line.start_pt, line.end_pt):
                assert x0 <= x <= x1 and y0 <= y <= y1


def test_second_region_without_its_own_frame_cue_is_not_promoted(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "no-frame.pdf", (SECOND, THIRD))
    model = _import(path)

    left, right = _regions(model)
    assert left["status"] == "resolved"
    assert right["status"] == "unresolved"
    assert right["reason_codes"] == ["registration_unresolved"]
    assert right["frame"] is None
    third = next(level for level in model.levels if level.name == "Third Floor")
    assert _entities_on(model, third.id) == (0, 0)
    # Every promoted entity sits inside the left drawing's own frame.
    xs = [p.x for space in model.spaces for p in space.footprint.points]
    assert max(xs) <= (LEFT[0] + ROOM_W) * QUARTER_INCH_MPP + 1e-9
    unresolved = [
        item for item in model.attributes["pdf_architecture"]["ambiguities"]
        if item["code"] == "registration_unresolved"
    ]
    assert [item["drawing_region_id"] for item in unresolved] == [right["region_id"]]


def test_missing_level_on_one_region_leaves_only_that_region_unresolved(tmp_path: Path) -> None:
    unlabeled = Drawing(RIGHT, room="ROOM: DEN")
    path = _write_sheet(tmp_path / "missing-level.pdf", (SECOND, unlabeled))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    assert left["status"] == "resolved"
    assert right["status"] == "unresolved"
    assert right["reason_codes"] == ["level_unresolved"]
    assert right["level"] is None
    assert [level.name for level in model.levels] == ["Second Floor"]
    assert {space.name for space in model.spaces} == {"BEDROOM"}


def test_ambiguous_level_names_on_one_region_stay_unresolved(tmp_path: Path) -> None:
    ambiguous = replace(THIRD, notes=(*THIRD.notes, "LEVEL: 4"))
    path = _write_sheet(tmp_path / "ambiguous-level.pdf", (SECOND, ambiguous))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    assert left["status"] == "resolved"
    assert right["status"] == "unresolved"
    assert right["reason_codes"] == ["level_ambiguous"]
    ambiguity = next(
        item for item in model.attributes["pdf_architecture"]["ambiguities"]
        if item["code"] == "level_ambiguous"
    )
    assert ambiguity["drawing_region_id"] == right["region_id"]
    assert ambiguity["level_names"] == ["4", "Third Floor"]
    assert len(ambiguity["source_element_ids"]) == 2


def test_sheet_level_level_text_is_not_shared_by_two_drawings(tmp_path: Path) -> None:
    left_drawing = Drawing(LEFT)
    right_drawing = Drawing(RIGHT, room="ROOM: DEN")
    path = _write_sheet(
        tmp_path / "sheet-level-level.pdf",
        (left_drawing, right_drawing),
        sheet_texts=("SCALE: 1/4\" = 1'-0\"", "LEVEL: 2"),
    )
    model = _import(path)

    assert [region["reason_codes"] for region in _regions(model)] == [
        ["level_unresolved"],
        ["level_unresolved"],
    ]
    assert model.levels == ()
    assert model.spaces == ()
    assert model.walls == ()
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == (
        "skipped_unresolved_drawing_regions"
    )


def test_level_override_must_name_its_drawing_on_a_multi_drawing_sheet(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "override.pdf", (Drawing(LEFT), Drawing(RIGHT, room="ROOM: DEN")))

    page_scoped = _import(path, ImportOptions(level_overrides=(
        LevelOverride(page_number=1, elevation_m=3.048, name="Second Floor"),
    )))
    assert "level_override_region_unresolved" in _codes(page_scoped)
    assert page_scoped.levels == ()
    assert page_scoped.spaces == ()

    region_scoped = _import(path, ImportOptions(level_overrides=(
        LevelOverride(
            page_number=1,
            elevation_m=3.048,
            name="Second Floor",
            note="caller-confirmed level",
            region_point_pt=(LEFT[0] + 10, LEFT[1] + 10),
        ),
    )))
    left, right = _regions(region_scoped)
    assert left["status"] == "resolved"
    assert left["level"]["name_method"] == "caller-confirmed level"
    assert right["reason_codes"] == ["level_unresolved"]
    assert [space.name for space in region_scoped.spaces] == ["BEDROOM"]

    outside = _import(path, ImportOptions(level_overrides=(
        LevelOverride(page_number=1, elevation_m=0.0, name="X", region_point_pt=(700.0, 900.0)),
    )))
    assert "level_override_region_unresolved" in _codes(outside)
    assert outside.levels == ()


def test_duplicate_region_hints_are_rejected(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "duplicate-hints.pdf", (SECOND, THIRD))
    with pytest.raises(ValueError, match="drawing region 2"):
        _import(path, ImportOptions(registrations=(
            _stacked_hint(),
            _stacked_hint(region_point_pt=(RIGHT[0] + 40, RIGHT[1] + 40)),
        )))


def test_missing_scale_leaves_every_drawing_unresolved(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "no-scale.pdf", (SECOND, THIRD), sheet_texts=())
    model = _import(path)

    assert [region["reason_codes"] for region in _regions(model)] == [
        ["scale_unresolved"],
        ["scale_unresolved"],
    ]
    assert model.spaces == ()
    assert model.walls == ()

    # A drawing's own two-point registration is a scale cue for that drawing only.
    registered = _import(path, ImportOptions(registrations=(_stacked_hint(),)))
    left, right = _regions(registered)
    assert left["reason_codes"] == ["scale_unresolved"]
    assert right["status"] == "resolved"
    assert right["scale"]["method"] == "scale resolved by two-point registration"
    assert right["scale"]["meters_per_point"] == pytest.approx(QUARTER_INCH_MPP)
    assert {space.name for space in registered.spaces} == {"DEN"}


def test_conflicting_region_scale_blocks_only_that_region(tmp_path: Path) -> None:
    conflicting = replace(
        SECOND,
        notes=(*SECOND.notes, "SCALE: 1/4\" = 1'-0\"", "SCALE: 1/8\" = 1'-0\""),
    )
    path = _write_sheet(tmp_path / "scale-conflict.pdf", (conflicting, THIRD))
    model = _import(path)

    left, right = _regions(model)
    assert left["reason_codes"] == ["scale_conflict"]
    assert left["frame"] is None
    # The other drawing inherits the unambiguous sheet scale and, being the first
    # drawing to emit geometry, defines the project-local origin.
    assert right["status"] == "resolved"
    assert right["scale"]["method"] == "sheet-level printed scale annotation inherited by drawing region"
    assert right["frame"]["basis"] == "project_origin"
    assert {space.name for space in model.spaces} == {"DEN"}


def test_page_scoped_registration_hint_is_not_guessed_onto_a_drawing(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "page-hint.pdf", (SECOND, THIRD))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(region_point_pt=None),)))

    left, right = _regions(model)
    assert "registration_hint_region_unresolved" in _codes(model)
    assert left["frame"]["basis"] == "project_origin"
    assert right["reason_codes"] == ["registration_unresolved"]


def test_repeated_drawings_of_the_same_level_compete_and_stay_unresolved(tmp_path: Path) -> None:
    existing = Drawing(LEFT, title="EXISTING SECOND FLOOR PLAN")
    proposed = Drawing(RIGHT, title="PROPOSED SECOND FLOOR PLAN")
    path = _write_sheet(tmp_path / "repeated.pdf", (existing, proposed))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    for region, other in ((left, right), (right, left)):
        assert region["status"] == "unresolved"
        assert region["reason_codes"] == [
            "drawing_region_geometry_repeated",
            "drawing_regions_share_level",
        ]
        assert region["repeated_geometry_region_ids"] == [other["region_id"]]
        assert region["frame"] is None
    assert model.levels == ()
    assert model.spaces == ()
    assert model.walls == ()


def test_repeated_geometry_on_distinct_levels_is_recorded_not_blocking(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "typical-floors.pdf", (SECOND, replace(THIRD, room="ROOM: BEDROOM")))
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    assert [left["status"], right["status"]] == ["resolved", "resolved"]
    assert left["repeated_geometry_region_ids"] == [right["region_id"]]
    assert len({space.level_id for space in model.spaces}) == 2


def test_unlayered_drawings_are_split_by_paired_wall_faces(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "unlayered.pdf", (SECOND, THIRD), layered=False)
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    left, right = _regions(model)
    assert left["evidence"]["kind"] == right["evidence"]["kind"] == "paired_wall_faces"
    assert [left["status"], right["status"]] == ["resolved", "resolved"]
    assert [left["level"]["name"], right["level"]["name"]] == ["Second Floor", "Third Floor"]


def test_sheet_frame_and_title_block_never_become_region_geometry_or_anchors(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "framed.pdf", (SECOND, THIRD), layered=False)
    model = _import(path, ImportOptions(registrations=(_stacked_hint(),)))

    regions = _regions(model)
    detection = model.attributes["pdf_architecture"]["pages"][0]["drawing_region_detection"]
    assert detection["drawing_cluster_count"] == 2
    tx0, ty0, tx1, ty1 = TITLE_BLOCK
    for region in regions:
        x0, y0, x1, y1 = region["source_bbox_pt"]
        assert 100 < x0 < x1 < PAGE_W - 100
        assert 100 < y0 < y1 < PAGE_H - 100
        assert x1 < tx0  # title block is outside every region
        assert region["frame"]["basis"] in {"project_origin", "explicit_registration"}
    # No canonical geometry comes from the sheet frame or the title block box.
    limit_x = (RIGHT[0] + ROOM_W) * QUARTER_INCH_MPP
    for wall in model.walls:
        for point in wall.centerline.points:
            assert 0 < point.x <= limit_x
    assert len(model.walls) == 8
    assert len(model.spaces) == 2


def test_region_resolution_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "repeat.pdf", (SECOND, THIRD))
    options = ImportOptions(registrations=(_stacked_hint(),))
    first = import_observations(extract_pdf(path, source_id="fixture:drawing-regions"), options=options)
    second = import_observations(extract_pdf(path, source_id="fixture:drawing-regions"), options=options)
    assert first.to_json() == second.to_json()

    document = extract_pdf(path, source_id="fixture:drawing-regions")
    shuffled_pages = []
    rng = random.Random(103)
    for page in document.pages:
        texts, lines, rects = list(page.texts), list(page.lines), list(page.rects)
        rng.shuffle(texts)
        rng.shuffle(lines)
        rng.shuffle(rects)
        shuffled_pages.append(replace(page, texts=tuple(texts), lines=tuple(lines), rects=tuple(rects)))
    shuffled = import_observations(replace(document, pages=tuple(shuffled_pages)), options=options)
    assert [region["region_id"] for region in _regions(shuffled)] == [
        region["region_id"] for region in _regions(first)
    ]
    assert shuffled.to_json() == first.to_json()


def test_scale_override_can_target_one_region(tmp_path: Path) -> None:
    path = _write_sheet(tmp_path / "region-scale.pdf", (SECOND, THIRD), sheet_texts=())
    model = _import(path, ImportOptions(
        scale_overrides=(
            ScaleOverride(
                page_number=1,
                meters_per_point=QUARTER_INCH_MPP,
                note="caller-confirmed drawing scale",
                region_point_pt=(LEFT[0] + 5, LEFT[1] + 5),
            ),
        ),
    ))
    left, right = _regions(model)
    assert left["status"] == "resolved"
    assert left["scale"]["method"] == "caller-confirmed drawing scale"
    assert right["reason_codes"] == ["scale_unresolved"]


@pytest.mark.parametrize("text,expected", [
    ("SECOND FLOOR PLAN", "Second Floor"),
    ("EXISTING THIRD FLOOR POWER PLAN", "Third Floor"),
    ("7TH FLOOR PLAN", "7th Floor"),
    ("1st FLOOR", "1st Floor"),
    ("LEVEL: 2", "2"),
    ("LEVEL 3 FLOOR PLAN", "3 Floor"),
    ("SECOND FLOOR PLAN - UNIT A", "Second Floor"),
    ("SECOND FLOOR PLAN - UNIT 3", "Second Floor"),
    ("SECOND FLOOR PLAN: AREA A", "Second Floor"),
    ("SECOND FLOOR PLAN (NORTH)", "Second Floor"),
    ("THIRD FLOOR - UNIT A", "Third Floor"),
    ("EXISTING SECOND FLOOR PLAN \u2013 WEST WING", "Second Floor"),
    ("SEE SECOND FLOOR FRAMING FOR BLOCKING", None),
    ("SECOND FLOOR FRAMING FOR BLOCKING", None),
    ("SECOND FLOOR PLAN - SEE SHEET A5 FOR DETAILS", None),
    ("THIRD FLOOR BATH - SEE NOTE 3", None),
    ("TAPED TO LEVEL 4 FINISH", None),
    ("PROVIDE GFCI AT THIRD FLOOR BATH", None),
])
def test_level_names_come_only_from_level_or_drawing_title_text(text: str, expected: str | None) -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=100,
        height_pt=100,
        texts=(PdfTextObservation(element_id="t", text=text, bbox_pt=(0, 0, 50, 10)),),
    )
    names = [name for name, _ in _level_name_candidates(page)]
    assert names == ([expected] if expected else [])


def test_single_drawing_title_with_a_qualifier_keeps_its_level(tmp_path: Path) -> None:
    titled = replace(SECOND, title="SECOND FLOOR PLAN - UNIT A")
    model = _import(_write_sheet(tmp_path / "suffixed.pdf", (titled,)))

    [region] = _regions(model)
    assert region["status"] == "resolved"
    [level] = model.levels
    assert level.name == "Second Floor"
    assert level.elevation_m == pytest.approx(3.048)
    assert _entities_on(model, level.id) == (1, 4)


def test_two_sheet_set_with_qualified_floor_titles_keeps_both_floors(tmp_path: Path) -> None:
    second = replace(SECOND, title="SECOND FLOOR PLAN - UNIT A")
    third = Drawing(LEFT, room="ROOM: DEN", title="THIRD FLOOR PLAN - UNIT A", notes=("ELEVATION: 20'-0\"",))
    path = _write_set(tmp_path / "two-sheets.pdf", ((second,), (third,)))
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=LEFT,
        source_b_pt=(LEFT[0] + ROOM_W, LEFT[1]),
        model_a_m=(LEFT[0] * QUARTER_INCH_MPP, LEFT[1] * QUARTER_INCH_MPP),
        model_b_m=((LEFT[0] + ROOM_W) * QUARTER_INCH_MPP, LEFT[1] * QUARTER_INCH_MPP),
    )
    model = _import(path, ImportOptions(registrations=(hint,)))

    assert sorted(level.name for level in model.levels) == ["Second Floor", "Third Floor"]
    for level in model.levels:
        assert _entities_on(model, level.id) == (1, 4)
    elevations = {level.name: level.elevation_m for level in model.levels}
    assert elevations["Second Floor"] == pytest.approx(3.048)
    assert elevations["Third Floor"] == pytest.approx(6.096)
    assert [page["status"] for page in model.attributes["pdf_architecture"]["pages"]] == [
        "geometry_imported",
        "geometry_imported",
    ]
    assert "level_elevation_conflict" not in _codes(model)
