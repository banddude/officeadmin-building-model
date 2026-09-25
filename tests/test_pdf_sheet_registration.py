"""#104: register electrical sheets to resolved architectural drawing frames.

Every case writes synthetic architectural and electrical source PDFs and runs
them through the real extractors. No pre-extracted observations or
expected-output files are used.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
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

from oabm.importers.pdf_architecture import ImportOptions, RegistrationHint
from oabm.importers.pdf_architecture.extract import extract_pdf as extract_sheets
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_convergence import PdfConvergenceError, converge_pdf_models
from oabm.importers.pdf_convergence.sheet_registration import (
    REGISTERED,
    REGISTRATION_PENDING,
    SheetRegistrationError,
    SheetRegistrationOptions,
    register_electrical_sheets,
)
from oabm.importers.pdf_electrical import (
    DrawingRegionTransform,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfPageTransform,
)
from oabm.model import BuildingModel, validate_model

MPP = 48 * 0.0254 / 72  # 1/4" = 1'-0"
PAGE_W, PAGE_H = 1728, 1152
ARCH_ORIGIN = (120.0, 400.0)
WALL_T = 9.0
QUARTER = "SCALE: 1/4\" = 1'-0\""
EIGHTH = "SCALE: 1/8\" = 1'-0\""


@dataclass(frozen=True)
class Room:
    """A double-line room in building points (1/4" drawing units)."""

    x: float
    y: float
    w: float
    h: float
    label: str


MAIN = Room(0.0, 0.0, 354.0, 236.0, "ROOM: BEDROOM")
# A wall stub off the east wall breaks the room's symmetry.
STUB = (((354.0, 60.0), (474.0, 60.0)), ((354.0, 69.0), (474.0, 69.0)))
GRID = (("A", -60.0, -60.0), ("B", 414.0, -60.0), ("1", -60.0, 296.0), ("2", 414.0, 296.0))
# The same grid inside the room extents, so per-drawing scopes hold the
# bubbles of the drawing that printed them.
TIGHT = (("A", 30.0, 30.0), ("B", 324.0, 30.0), ("1", 30.0, 206.0), ("2", 324.0, 206.0))
EVSE_AT = (330.0, 120.0)  # just inside the east wall


@dataclass(frozen=True)
class Drawing:
    origin: tuple[float, float]
    rooms: tuple[Room, ...] = (MAIN,)
    stub: bool = True
    title: str | None = None
    notes: tuple[str, ...] = ()
    scale: float = 1.0  # paper size relative to 1/4" drawing units
    quarter_turns: int = 0
    grid: tuple[tuple[str, float, float], ...] = ()
    evse: bool = False
    walls: bool = True
    jitter_stub_pt: float = 0.0
    mirrored: bool = False
    evse_tag: str = "EVSE-1"
    evse_at: tuple[float, float] = EVSE_AT


@dataclass(frozen=True)
class Sheet:
    drawings: tuple[Drawing, ...]
    wall_layer: str = "A-WALL"
    texts: tuple[str, ...] = (QUARTER,)
    sheet_number: str = "A9.1"
    extra: tuple[str, ...] = field(default_factory=tuple)


def _local(drawing: Drawing, point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    if drawing.mirrored:
        x = MAIN.w - x  # flipped about the room's vertical centre line
    for _ in range(drawing.quarter_turns % 4):
        x, y = -y, x
    return (drawing.origin[0] + x * drawing.scale, drawing.origin[1] + y * drawing.scale)


def _line(drawing: Drawing, a: tuple[float, float], b: tuple[float, float]) -> str:
    (ax, ay), (bx, by) = _local(drawing, a), _local(drawing, b)
    return f"{ax:.4f} {ay:.4f} m {bx:.4f} {by:.4f} l S"


def _text(x: float, y: float, value: str, size: int = 10) -> str:
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET"


def _circle(cx: float, cy: float, r: float) -> str:
    k = 0.5523 * r
    return (
        f"{cx + r} {cy} m {cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} c "
        f"{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy} c "
        f"{cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} c "
        f"{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} c S"
    )


def _add_sheet(writer: PdfWriter, sheet: Sheet) -> None:
    page = writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))
    wall = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject(sheet.wall_layer),
    }))
    symbol = DecodedStreamObject()
    symbol.set_data(b"0 0 12 12 re S")
    symbol[NameObject("/Type")] = NameObject("/XObject")
    symbol[NameObject("/Subtype")] = NameObject("/Form")
    symbol[NameObject("/BBox")] = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(12), NumberObject(12)])
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({NameObject("/WALL"): wall}),
        NameObject("/XObject"): DictionaryObject({NameObject("/EVSE1"): writer._add_object(symbol)}),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([wall]),
        NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
    })
    commands = [
        "1400 40 300 360 re S",
        _text(1412, 370, "PROJECT: SYNTHETIC BUILDING"),
        _text(1412, 340, "DRAWING TITLE: PLANS"),
        _text(1412, 310, f"SHEET NO: {sheet.sheet_number}"),
        *(_text(1412, 250 - 20 * index, value) for index, value in enumerate(sheet.texts)),
        *sheet.extra,
    ]
    for drawing in sheet.drawings:
        x0, y0 = _local(drawing, (0.0, 0.0))
        if drawing.title:
            commands.append(_text(x0, y0 - 40, drawing.title, size=14))
        for index, note in enumerate(drawing.notes):
            commands.append(_text(x0, y0 - 60 - 16 * index, note))
        if drawing.walls:
            commands.append("/OC /WALL BDC")
            for room in drawing.rooms:
                for inset in (0.0, WALL_T):
                    a, b = room.x + inset, room.y + inset
                    c, d = room.x + room.w - inset, room.y + room.h - inset
                    commands.extend((
                        _line(drawing, (a, b), (c, b)),
                        _line(drawing, (c, b), (c, d)),
                        _line(drawing, (c, d), (a, d)),
                        _line(drawing, (a, d), (a, b)),
                    ))
            if drawing.stub:
                for (a, b) in STUB:
                    shift = drawing.jitter_stub_pt
                    commands.append(_line(drawing, (a[0], a[1] + shift), (b[0], b[1] + shift)))
            commands.append("EMC")
            for room in drawing.rooms:
                lx, ly = _local(drawing, (room.x + 60, room.y + room.h / 2))
                commands.append(_text(lx, ly, room.label))
        for label, gx, gy in drawing.grid:
            cx, cy = _local(drawing, (gx, gy))
            commands.append(_circle(cx, cy, 14))
            commands.append(_text(cx - 3.5, cy - 3.5, label))
        if drawing.evse:
            ex, ey = _local(drawing, drawing.evse_at)
            commands.append(f"q 1 0 0 1 {ex:.4f} {ey:.4f} cm /EVSE1 Do Q")
            commands.append(_text(ex + 5, ey + 5, f"{drawing.evse_tag} +48\" AFF WALL MTD", size=9))
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)


def _write(path: Path, *sheets: Sheet) -> Path:
    writer = PdfWriter()
    for sheet in sheets:
        _add_sheet(writer, sheet)
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


SECOND = Drawing(ARCH_ORIGIN, title="SECOND FLOOR PLAN", notes=("ELEVATION: 10'-0\"",))


def _architecture(tmp_path: Path, *sheets: Sheet, options: ImportOptions | None = None):
    path = _write(tmp_path / "architecture.pdf", *(sheets or (Sheet((SECOND,)),)))
    source = extract_sheets(path, source_id="fixture:architecture")
    model = import_observations(source, options=options)
    validate_model(model)
    return path, source, model


def _electrical(tmp_path: Path, *sheets: Sheet, name: str = "electrical.pdf") -> Path:
    return _write(tmp_path / name, *sheets)


def _e_sheet(*drawings: Drawing, texts: tuple[str, ...] = (QUARTER,)) -> Sheet:
    return Sheet(drawings, wall_layer="xref_Floor Plan|A-Wall", texts=texts, sheet_number="E9.1")


def _register(architecture: BuildingModel, architecture_source, electrical_path: Path, **kwargs):
    return register_electrical_sheets(
        architecture,
        architecture_source,
        extract_sheets(electrical_path, source_id="fixture:electrical"),
        **kwargs,
    )


OFFSET = (60.0, -45.0)
ELECTRICAL = Drawing(
    (ARCH_ORIGIN[0] + OFFSET[0], ARCH_ORIGIN[1] + OFFSET[1]),
    title="SECOND FLOOR POWER PLAN",
    evse=True,
)


def test_known_translation_registers_with_a_composed_canonical_transform(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    [region] = architecture.attributes["pdf_architecture"]["drawing_regions"]
    assert region["status"] == "resolved"
    level = architecture.levels[0]

    result = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL)))

    [page] = result.pages
    assert page.status == REGISTERED
    assert page.reason_codes == ()
    registration = page.record["registration"]
    assert registration["evidence_method"] == "wall_vectors"
    assert registration["derivation"] == "inferred"
    assert registration["target_region_id"] == region["region_id"]
    assert registration["translation_pt"] == pytest.approx([-OFFSET[0], -OFFSET[1]], abs=1e-6)
    assert registration["scale_ratio"] == pytest.approx(1.0)
    assert registration["wall_inlier_count"] == 10
    assert registration["wall_residual_rms_m"] == pytest.approx(0.0, abs=1e-9)
    assert registration["matched_evidence_sample"]
    transform = page.transform
    assert transform is not None
    # The target frame ID and level elevation are preserved.
    assert transform.frame_id == architecture.coordinate_system.frame_id
    assert transform.z_m == pytest.approx(level.elevation_m)
    assert registration["level_id"] == level.id
    # Matched electrical source points land on the architecture's canonical points.
    for corner in ((0.0, 0.0), (354.0, 0.0), (354.0, 236.0), STUB[0][1]):
        electrical_point = _local(ELECTRICAL, corner)
        architecture_point = _local(SECOND, corner)
        mapped = transform.apply(*electrical_point)
        assert mapped.x == pytest.approx(architecture_point[0] * MPP, abs=0.05)
        assert mapped.y == pytest.approx(architecture_point[1] * MPP, abs=0.05)
        assert mapped.z == pytest.approx(level.elevation_m)
    assert result.page_transforms() == {1: transform}


def test_registered_electrical_import_converges_into_the_architecture(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    electrical_path = _electrical(tmp_path, _e_sheet(ELECTRICAL))
    result = _register(architecture, source, electrical_path)
    transforms = result.page_transforms()
    assert transforms is not None

    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=transforms,
    )
    lane = electrical.attributes["pdf_electrical"]
    assert lane["registration_pending"] is False
    inferred = [item for item in electrical.provenance if item.derivation == "inferred"]
    assert [item.method for item in inferred] == ["sheet registration by wall vectors"]
    [device] = electrical.electrical_devices
    assert device.attributes["pdf_electrical"]["source_position_pt"] == pytest.approx(
        {"x": _local(ELECTRICAL, EVSE_AT)[0], "y": _local(ELECTRICAL, EVSE_AT)[1]}
    )

    merged = converge_pdf_models(architecture, electrical)
    validate_model(merged)
    [placed] = merged.electrical_devices
    [space] = architecture.spaces
    expected = _local(SECOND, EVSE_AT)
    assert placed.pose.position.x == pytest.approx(expected[0] * MPP, abs=0.05)
    assert placed.pose.position.y == pytest.approx(expected[1] * MPP, abs=0.05)
    assert placed.pose.position.z == pytest.approx(architecture.levels[0].elevation_m + 48 * 0.0254)
    assert placed.space_id == space.id
    assert placed.level_id == architecture.levels[0].id


def test_grid_bubbles_register_a_sheet_without_wall_vectors(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    grid_only = replace(ELECTRICAL, walls=False, grid=GRID)
    result = _register(architecture, source, _electrical(tmp_path, _e_sheet(grid_only)))

    [page] = result.pages
    assert page.status == REGISTERED
    registration = page.record["registration"]
    assert registration["evidence_method"] == "grid_labels"
    assert registration["grid_labels_shared"] == ["1", "2", "A", "B"]
    assert registration["translation_pt"] == pytest.approx([-OFFSET[0], -OFFSET[1]], abs=1e-6)

    both = replace(ELECTRICAL, grid=GRID)
    combined = _register(architecture, source, _electrical(tmp_path, _e_sheet(both), name="both.pdf"))
    assert combined.pages[0].record["registration"]["evidence_method"] == "wall_vectors_and_grid_labels"


def test_inconsistent_grid_labels_do_not_register(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    moved = tuple(
        (label, x + (90.0 if label == "B" else 0.0), y) for label, x, y in GRID
    )
    grid_only = replace(ELECTRICAL, walls=False, grid=moved)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(grid_only))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("grid_labels_inconsistent",)


def test_a_different_printed_scale_registers_at_the_printed_ratio(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    eighth = replace(ELECTRICAL, scale=0.5)
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(eighth, texts=(EIGHTH,))),
    ).pages
    assert page.status == REGISTERED
    assert page.record["registration"]["scale_ratio"] == pytest.approx(2.0)
    mapped = page.transform.apply(*_local(eighth, (354.0, 236.0)))
    expected = _local(SECOND, (354.0, 236.0))
    assert (mapped.x, mapped.y) == pytest.approx((expected[0] * MPP, expected[1] * MPP), abs=0.05)


def test_same_size_sheet_at_the_wrong_scale_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    # Printed 1/4" but drawn at half size: identity or printed-ratio matching must fail.
    wrong = replace(ELECTRICAL, scale=0.5)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(wrong))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("scale_incompatible",)
    assert page.record["diagnostics"]["matching_scale_ratio_to_printed"] == pytest.approx(2.0)
    assert page.transform is None


def test_orientation_mismatch_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    turned = replace(ELECTRICAL, origin=(900.0, 300.0), quarter_turns=1)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(turned))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("orientation_incompatible",)
    assert page.record["diagnostics"]["matching_rotation_degrees"] == 270


def test_insufficient_wall_evidence_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    stub_only = replace(ELECTRICAL, rooms=())
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(stub_only))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("insufficient_matched_evidence",)


def test_clustered_evidence_is_refused(tmp_path: Path) -> None:
    closet = Room(-150.0, 60.0, 84.0, 84.0, "ROOM: CLOSET")
    _, source, architecture = _architecture(
        tmp_path, Sheet((replace(SECOND, rooms=(MAIN, closet)),)),
    )
    only_closet = replace(ELECTRICAL, rooms=(closet,), stub=False)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(only_closet))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("evidence_clustered",)


def test_repeated_geometry_yields_competing_transforms(tmp_path: Path) -> None:
    twin = Room(414.0, 0.0, 354.0, 236.0, "ROOM: STUDY")
    _, source, architecture = _architecture(
        tmp_path, Sheet((replace(SECOND, rooms=(MAIN, twin), stub=False),)),
    )
    one_room = replace(ELECTRICAL, stub=False)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(one_room))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("competing_transforms",)
    [candidate] = page.record["candidates"]
    assert candidate["competing_translation_pt"] is not None


def test_missing_wall_and_grid_evidence_stops_for_a_bounded_vision_task(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    device_only = replace(ELECTRICAL, walls=False)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(device_only))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("missing_registration_evidence",)
    assert "#98 vision task" in page.record["bounded_vision_question"]


def test_excessive_residual_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    jittered = replace(ELECTRICAL, jitter_stub_pt=4.0)
    options = SheetRegistrationOptions(tolerance_m=0.2, max_residual_m=0.01)
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(jittered)), options=options,
    ).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("excessive_residual",)


def _two_floor_architecture(tmp_path: Path):
    third = Drawing((800.0, 400.0), title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",))
    hint = RegistrationHint(
        page_number=1,
        source_a_pt=(800.0, 400.0),
        source_b_pt=(1154.0, 400.0),
        model_a_m=(ARCH_ORIGIN[0] * MPP, ARCH_ORIGIN[1] * MPP),
        model_b_m=((ARCH_ORIGIN[0] + 354.0) * MPP, ARCH_ORIGIN[1] * MPP),
        region_point_pt=(820.0, 420.0),
    )
    return _architecture(tmp_path, Sheet((SECOND, third)), options=ImportOptions(registrations=(hint,)))


def test_identical_floors_on_distinct_levels_are_competing_targets(tmp_path: Path) -> None:
    _, source, architecture = _two_floor_architecture(tmp_path)
    regions = architecture.attributes["pdf_architecture"]["drawing_regions"]
    assert [region["status"] for region in regions] == ["resolved", "resolved"]
    untitled = replace(ELECTRICAL, title=None)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(untitled))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("competing_targets",)
    assert page.record["competing_region_ids"] == sorted(region["region_id"] for region in regions)


def test_electrical_level_name_selects_among_identical_floors(tmp_path: Path) -> None:
    _, source, architecture = _two_floor_architecture(tmp_path)
    second = next(level for level in architecture.levels if level.name == "Second Floor")
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL))).pages
    assert page.status == REGISTERED
    assert page.record["registration"]["level_id"] == second.id
    rejected = [item for item in page.record["candidates"] if not item["accepted"]]
    assert [item["reason_codes"] for item in rejected] == [["level_name_mismatch"]]


def test_electrical_sheet_naming_another_level_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    third = replace(ELECTRICAL, title="THIRD FLOOR POWER PLAN")
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(third))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("level_name_mismatch",)
    # Equivalent spellings of the same level still register.
    ordinal = replace(ELECTRICAL, title="2ND FLOOR POWER PLAN")
    [same] = _register(architecture, source, _electrical(tmp_path, _e_sheet(ordinal), name="2nd.pdf")).pages
    assert same.status == REGISTERED


def test_refused_registration_stays_pending_and_convergence_rejects_it(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    turned = replace(ELECTRICAL, origin=(900.0, 300.0), quarter_turns=1)
    electrical_path = _electrical(tmp_path, _e_sheet(turned))
    result = _register(architecture, source, electrical_path)
    assert result.page_transforms() is None

    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=result.page_transforms(),
    )
    assert electrical.attributes["pdf_electrical"]["registration_pending"] is True
    with pytest.raises(PdfConvergenceError, match="registered"):
        converge_pdf_models(architecture, electrical)


def test_multi_page_electrical_set_needs_every_page_registered(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    electrical_path = _electrical(
        tmp_path,
        _e_sheet(ELECTRICAL),
        _e_sheet(replace(ELECTRICAL, walls=False)),
    )
    result = _register(architecture, source, electrical_path)
    assert [page.status for page in result.pages] == [REGISTERED, REGISTRATION_PENDING]
    assert result.all_registered is False
    assert result.page_transforms() is None
    # One page's transform cannot stand in for the whole document.
    with pytest.raises(ElectricalPdfError, match="every PDF page"):
        ElectricalPdfImporter().import_pdf(
            electrical_path,
            source_id="fixture:electrical",
            page_transforms={1: result.pages[0].transform},
        )


def test_registration_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    electrical_path = _electrical(tmp_path, _e_sheet(replace(ELECTRICAL, grid=GRID)))
    first = _register(architecture, source, electrical_path).to_dict()
    second = _register(architecture, source, electrical_path).to_dict()
    assert first == second

    rng = random.Random(104)
    observations = extract_sheets(electrical_path, source_id="fixture:electrical")
    pages = []
    for page in observations.pages:
        lines, texts = list(page.lines), list(page.texts)
        rng.shuffle(lines)
        rng.shuffle(texts)
        pages.append(replace(page, lines=tuple(lines), texts=tuple(texts)))
    shuffled = register_electrical_sheets(
        architecture, source, replace(observations, pages=tuple(pages)),
    ).to_dict()
    assert shuffled == first


def test_architecture_source_must_match_the_architecture_model(tmp_path: Path) -> None:
    _, _, architecture = _architecture(tmp_path)
    other = extract_sheets(
        _write(tmp_path / "other.pdf", Sheet((replace(SECOND, stub=False),))),
        source_id="fixture:other",
    )
    with pytest.raises(SheetRegistrationError, match="not from the PDF"):
        register_electrical_sheets(
            architecture, other, extract_sheets(_electrical(tmp_path, _e_sheet(ELECTRICAL))),
        )


def test_no_resolved_architectural_region_leaves_every_page_pending(tmp_path: Path) -> None:
    untitled = Drawing((120.0, 400.0))
    third = Drawing((800.0, 400.0))
    _, source, architecture = _architecture(tmp_path, Sheet((untitled, third)))
    assert all(
        region["status"] == "unresolved"
        for region in architecture.attributes["pdf_architecture"]["drawing_regions"]
    )
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL))).pages
    assert page.reason_codes == ("no_resolved_architectural_region",)


def test_electrical_page_without_a_printed_scale_is_pending(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL, texts=())),
    ).pages
    assert page.reason_codes == ("scale_unresolved",)
    options = SheetRegistrationOptions(electrical_scale_overrides=((1, MPP),))
    [overridden] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL, texts=()), name="o.pdf"),
        options=options,
    ).pages
    assert overridden.status == REGISTERED
    assert overridden.record["electrical_scale"]["method"] == "caller-supplied electrical sheet scale"


def _evse_error_m(transform, drawing: Drawing) -> float:
    mapped = transform.apply(*_local(drawing, EVSE_AT))
    expected = _local(SECOND, EVSE_AT)
    return math.hypot(mapped.x - expected[0] * MPP, mapped.y - expected[1] * MPP)


def test_mirrored_sheet_is_refused_and_the_true_sheet_still_registers(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    mirrored = replace(ELECTRICAL, mirrored=True)
    result = _register(architecture, source, _electrical(tmp_path, _e_sheet(mirrored)))
    [page] = result.pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("orientation_incompatible",)
    alternative = page.record["diagnostics"]["alternative"]
    assert alternative["mirrored"] is True
    assert alternative["wall_inlier_count"] > page.record["diagnostics"]["identity_wall_inlier_count"]
    assert result.page_transforms() is None

    # Control: the unmirrored sheet registers and places the device exactly.
    [control] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL), name="control.pdf"),
    ).pages
    assert control.status == REGISTERED
    assert _evse_error_m(control.transform, ELECTRICAL) == pytest.approx(0.0, abs=1e-9)


def test_mirrored_sheet_with_grid_bubbles_is_refused(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    mirrored = replace(ELECTRICAL, mirrored=True, grid=GRID)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(mirrored))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("orientation_incompatible",)


def test_two_page_set_with_a_mirrored_page_offers_no_transforms(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path)
    result = _register(
        architecture,
        source,
        _electrical(tmp_path, _e_sheet(ELECTRICAL), _e_sheet(replace(ELECTRICAL, mirrored=True))),
    )
    assert [page.status for page in result.pages] == [REGISTERED, REGISTRATION_PENDING]
    assert result.pages[1].reason_codes == ("orientation_incompatible",)
    assert result.page_transforms() is None


def test_grid_labels_contradicting_the_wall_placement_refuse(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    # Walls are true; two grid bubbles on the electrical sheet sit elsewhere.
    scattered = tuple(
        (label, x + (120.0 if label == "B" else -90.0 if label == "1" else 0.0), y)
        for label, x, y in GRID
    )
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(replace(ELECTRICAL, grid=scattered))),
    ).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("grid_labels_inconsistent",)

    # A consistent grid that disagrees with the walls also refuses.
    shifted = tuple((label, x + 100.0, y + 100.0) for label, x, y in GRID)
    [other] = _register(
        architecture,
        source,
        _electrical(tmp_path, _e_sheet(replace(ELECTRICAL, grid=shifted)), name="shifted.pdf"),
    ).pages
    assert other.reason_codes == ("registration_methods_disagree",)


def test_one_stray_grid_label_is_tolerated_when_others_agree(tmp_path: Path) -> None:
    _, source, architecture = _architecture(tmp_path, Sheet((replace(SECOND, grid=GRID),)))
    stray = tuple((label, x + (120.0 if label == "B" else 0.0), y) for label, x, y in GRID)
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(replace(ELECTRICAL, grid=stray))),
    ).pages
    assert page.status == REGISTERED
    assert page.record["registration"]["evidence_method"] == "wall_vectors_and_grid_labels"
    [candidate] = page.record["candidates"]
    assert candidate["grid_label_outliers"] == ["B"]


def test_symmetric_walls_need_grid_labels_to_fix_orientation(tmp_path: Path) -> None:
    symmetric = replace(SECOND, stub=False)
    _, source, architecture = _architecture(tmp_path, Sheet((symmetric,)))
    plain = replace(ELECTRICAL, stub=False)
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(plain))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("ambiguous_orientation",)

    (tmp_path / "grid").mkdir()
    _, gridded_source, gridded = _architecture(
        tmp_path / "grid", Sheet((replace(symmetric, grid=GRID),)),
    )
    [resolved] = _register(
        gridded,
        gridded_source,
        _electrical(tmp_path, _e_sheet(replace(plain, grid=GRID)), name="gridded.pdf"),
    ).pages
    assert resolved.status == REGISTERED
    assert resolved.record["registration"]["evidence_method"] == "wall_vectors_and_grid_labels"
    assert _evse_error_m(resolved.transform, plain) == pytest.approx(0.0, abs=1e-9)


# Per-drawing transforms (#72 part B): one page holding two floor drawings.

THIRD_ROOM = Room(0.0, 0.0, 300.0, 280.0, "ROOM: DEN")
THIRD_EVSE_AT = (276.0, 140.0)  # just inside the den's east wall
THIRD_ORIGIN = (800.0, 400.0)


def _floor_hint() -> RegistrationHint:
    """Stack the third-floor drawing on the second floor's canonical origin."""

    return RegistrationHint(
        page_number=1,
        source_a_pt=THIRD_ORIGIN,
        source_b_pt=(THIRD_ORIGIN[0] + 300.0, THIRD_ORIGIN[1]),
        model_a_m=(ARCH_ORIGIN[0] * MPP, ARCH_ORIGIN[1] * MPP),
        model_b_m=((ARCH_ORIGIN[0] + 300.0) * MPP, ARCH_ORIGIN[1] * MPP),
        region_point_pt=(THIRD_ORIGIN[0] + 20.0, THIRD_ORIGIN[1] + 20.0),
    )


def _distinct_floor_architecture(tmp_path: Path):
    third = Drawing(
        THIRD_ORIGIN, rooms=(THIRD_ROOM,), title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",),
    )
    return _architecture(tmp_path, Sheet((SECOND, third)), options=ImportOptions(registrations=(_floor_hint(),)))


LOWER = replace(ELECTRICAL, title=None)
UPPER = Drawing(
    (THIRD_ORIGIN[0] + OFFSET[0], THIRD_ORIGIN[1] + OFFSET[1]),
    rooms=(THIRD_ROOM,),
    evse=True,
    evse_tag="EVSE-2",
    evse_at=THIRD_EVSE_AT,
)


def _levels(architecture: BuildingModel) -> dict:
    return {level.name: level for level in architecture.levels}


def test_each_drawing_of_a_two_floor_page_registers_to_its_own_level(tmp_path: Path) -> None:
    _, source, architecture = _distinct_floor_architecture(tmp_path)
    regions = architecture.attributes["pdf_architecture"]["drawing_regions"]
    assert [region["status"] for region in regions] == ["resolved", "resolved"]
    levels = _levels(architecture)
    electrical_path = _electrical(tmp_path, _e_sheet(LOWER, UPPER))
    result = _register(architecture, source, electrical_path)

    [page] = result.pages
    assert page.status == REGISTERED
    assert page.reason_codes == ()
    assert page.transform is None
    assert page.record["registration_mode"] == "per_drawing"
    lower, upper = page.record["drawings"]
    assert [lower["status"], upper["status"]] == [REGISTERED, REGISTERED]
    assert lower["registration"]["level_id"] == levels["Second Floor"].id
    assert upper["registration"]["level_id"] == levels["Third Floor"].id
    assert [item["registration"]["derivation"] for item in (lower, upper)] == ["inferred"] * 2
    lower_region, upper_region = page.drawing_transforms
    assert result.page_transforms() == {1: page.drawing_transforms}
    # Each drawing's extents hold its own drawing and not the other one.
    assert lower_region.contains(*_local(LOWER, (0.0, 0.0)))
    assert not lower_region.contains(*_local(UPPER, (0.0, 0.0)))
    assert upper_region.contains(*_local(UPPER, (0.0, 0.0)))
    assert not upper_region.contains(*_local(LOWER, (0.0, 0.0)))
    # Each transform places its own drawing on its own floor.
    mapped = lower_region.transform.apply(*_local(LOWER, (354.0, 236.0)))
    expected = _local(SECOND, (354.0, 236.0))
    assert (mapped.x, mapped.y) == pytest.approx((expected[0] * MPP, expected[1] * MPP), abs=1e-6)
    assert mapped.z == pytest.approx(levels["Second Floor"].elevation_m)
    mapped = upper_region.transform.apply(*_local(UPPER, (300.0, 280.0)))
    assert (mapped.x, mapped.y) == pytest.approx(
        ((ARCH_ORIGIN[0] + 300.0) * MPP, (ARCH_ORIGIN[1] + 280.0) * MPP), abs=1e-6,
    )
    assert mapped.z == pytest.approx(levels["Third Floor"].elevation_m)

    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=result.page_transforms(),
    )
    lane = electrical.attributes["pdf_electrical"]
    assert lane["registration_pending"] is False
    assert lane["spatial_status"] == "registered-to-canonical-frame"
    assert lane["registration_mode"] == "explicit-page-and-drawing-transforms"
    assert lane["drawing_region_assignment"] == {
        "status": "resolved",
        "pages": {"1": {"assigned": 2, "outside_drawing_regions": 0, "in_multiple_drawing_regions": 0}},
    }
    assert len(lane["drawing_region_transforms"]["1"]) == 2
    inferred = [item for item in electrical.provenance if item.derivation == "inferred"]
    assert len(inferred) == 2
    assert all("drawing_region_bbox_pt" in item.attributes for item in inferred)

    merged = converge_pdf_models(architecture, electrical)
    validate_model(merged)
    placed = sorted(
        merged.electrical_devices,
        key=lambda item: item.attributes["pdf_electrical"]["source_position_pt"]["x"],
    )
    assert len(placed) == 2
    lower_device, upper_device = placed
    assert lower_device.level_id == levels["Second Floor"].id
    assert upper_device.level_id == levels["Third Floor"].id
    expected_lower = _local(SECOND, EVSE_AT)
    assert (lower_device.pose.position.x, lower_device.pose.position.y) == pytest.approx(
        (expected_lower[0] * MPP, expected_lower[1] * MPP), abs=1e-6,
    )
    assert (upper_device.pose.position.x, upper_device.pose.position.y) == pytest.approx(
        ((ARCH_ORIGIN[0] + THIRD_EVSE_AT[0]) * MPP, (ARCH_ORIGIN[1] + THIRD_EVSE_AT[1]) * MPP), abs=1e-6,
    )
    assert upper_device.pose.position.z == pytest.approx(levels["Third Floor"].elevation_m + 48 * 0.0254)
    assert lower_device.attributes["pdf_electrical"]["drawing_region_bbox_pt"] == list(
        lower_region.source_bbox_pt
    )


def test_identical_floors_on_one_page_register_by_their_printed_level_names(tmp_path: Path) -> None:
    _, source, architecture = _two_floor_architecture(tmp_path)
    levels = _levels(architecture)
    upper = replace(
        ELECTRICAL,
        origin=(800.0 + OFFSET[0], 400.0 + OFFSET[1]),
        title="THIRD FLOOR POWER PLAN",
        evse_tag="EVSE-2",
    )
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL, upper))).pages
    assert page.status == REGISTERED
    lower_record, upper_record = page.record["drawings"]
    assert lower_record["level_names"] == ["Second Floor"]
    assert upper_record["level_names"] == ["Third Floor"]
    assert lower_record["registration"]["level_id"] == levels["Second Floor"].id
    assert upper_record["registration"]["level_id"] == levels["Third Floor"].id

    # Without the titles, each drawing matches both identical floors.
    untitled = _register(
        architecture,
        source,
        _electrical(
            tmp_path,
            _e_sheet(replace(ELECTRICAL, title=None), replace(upper, title=None)),
            name="untitled.pdf",
        ),
    )
    [pending] = untitled.pages
    assert pending.status == REGISTRATION_PENDING
    assert pending.reason_codes == ("competing_targets",)
    assert [item["reason_codes"] for item in pending.record["drawings"]] == [["competing_targets"]] * 2
    assert untitled.page_transforms() is None


def test_one_unregistered_drawing_keeps_the_page_pending(tmp_path: Path) -> None:
    _, source, architecture = _distinct_floor_architecture(tmp_path)
    # Mirroring flips the stub to the drawing's west side; move the drawing
    # east so the two drawings stay separated on the page.
    mirrored_upper = replace(UPPER, mirrored=True, origin=(UPPER.origin[0] + 200.0, UPPER.origin[1]))
    result = _register(architecture, source, _electrical(tmp_path, _e_sheet(LOWER, mirrored_upper)))
    [page] = result.pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("orientation_incompatible",)
    assert page.drawing_transforms == ()
    lower, upper = page.record["drawings"]
    assert lower["status"] == REGISTERED
    assert upper["status"] == REGISTRATION_PENDING
    assert upper["reason_codes"] == ["orientation_incompatible"]
    assert result.page_transforms() is None


def test_a_point_outside_every_drawing_keeps_the_document_pending(tmp_path: Path) -> None:
    _, source, architecture = _distinct_floor_architecture(tmp_path)
    stray = Sheet(
        (LOWER, UPPER),
        wall_layer="xref_Floor Plan|A-Wall",
        sheet_number="E9.1",
        extra=(
            "q 1 0 0 1 700.0000 1000.0000 cm /EVSE1 Do Q",
            _text(705.0, 1005.0, "EVSE-3 +48\" AFF WALL MTD", size=9),
        ),
    )
    electrical_path = _electrical(tmp_path, stray)
    result = _register(architecture, source, electrical_path)
    transforms = result.page_transforms()
    assert transforms is not None and len(transforms[1]) == 2

    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=transforms,
    )
    lane = electrical.attributes["pdf_electrical"]
    assert lane["registration_pending"] is True
    # The transforms were supplied even though the assignment fell back.
    assert lane["page_transforms_supplied"] is True
    assert lane["registration_mode"] == "drawing-region-transforms-unresolved"
    assignment = lane["drawing_region_assignment"]
    assert assignment["status"] == "unresolved"
    assert assignment["pages"]["1"] == {
        "assigned": 2, "outside_drawing_regions": 1, "in_multiple_drawing_regions": 0,
    }
    [unplaced] = assignment["unplaced_points"]
    assert unplaced["reason"] == "outside_drawing_regions"
    assert unplaced["source_position_pt"]["x"] == pytest.approx(700.0, abs=15.0)
    assert unplaced["source_position_pt"]["y"] == pytest.approx(1000.0, abs=15.0)
    assert "drawing_region_transforms" not in lane
    assert not any(item.derivation == "inferred" and "page_transform" in item.attributes and item.attributes.get("registration_status") == "registered-from-matched-evidence" for item in electrical.provenance)
    with pytest.raises(PdfConvergenceError, match="registered"):
        converge_pdf_models(architecture, electrical)


def test_a_point_inside_two_drawing_regions_keeps_the_document_pending(tmp_path: Path) -> None:
    electrical_path = _electrical(tmp_path, _e_sheet(ELECTRICAL))
    transform = PdfPageTransform(frame_id="frame:test", m11_m_per_pt=MPP, m22_m_per_pt=MPP)
    overlapping = (
        DrawingRegionTransform((0.0, 0.0, 1000.0, 1152.0), transform),
        DrawingRegionTransform((200.0, 0.0, 1728.0, 1152.0), replace(transform, tx_m=1.0)),
    )
    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms={1: overlapping},
    )
    lane = electrical.attributes["pdf_electrical"]
    assert lane["registration_pending"] is True
    assert lane["drawing_region_assignment"]["pages"]["1"]["in_multiple_drawing_regions"] == 1
    # The same regions without the overlap place the point.
    separate = (
        DrawingRegionTransform((0.0, 0.0, 1000.0, 1152.0), transform),
        DrawingRegionTransform((1100.0, 0.0, 1728.0, 1152.0), replace(transform, tx_m=1.0)),
    )
    placed = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms={1: separate},
    )
    assert placed.attributes["pdf_electrical"]["registration_pending"] is False
    [device] = placed.electrical_devices
    source_point = _local(ELECTRICAL, EVSE_AT)
    assert (device.pose.position.x, device.pose.position.y) == pytest.approx(
        (source_point[0] * MPP, source_point[1] * MPP), abs=1e-9,
    )


def test_drawing_region_transforms_are_validated(tmp_path: Path) -> None:
    electrical_path = _electrical(tmp_path, _e_sheet(ELECTRICAL))
    transform = PdfPageTransform(frame_id="frame:test")
    with pytest.raises(ElectricalPdfError, match="ordered"):
        DrawingRegionTransform((10.0, 0.0, 5.0, 10.0), transform)
    with pytest.raises(ElectricalPdfError, match="PdfPageTransform"):
        DrawingRegionTransform((0.0, 0.0, 5.0, 10.0), "not a transform")  # type: ignore[arg-type]
    with pytest.raises(ElectricalPdfError, match="non-empty"):
        ElectricalPdfImporter().import_pdf(electrical_path, page_transforms={1: ()})
    with pytest.raises(ElectricalPdfError, match="same canonical coordinate frame"):
        ElectricalPdfImporter().import_pdf(
            electrical_path,
            page_transforms={1: (
                DrawingRegionTransform((0.0, 0.0, 500.0, 500.0), transform),
                DrawingRegionTransform((600.0, 0.0, 900.0, 500.0), replace(transform, frame_id="frame:other")),
            )},
        )


def test_a_drawing_without_bubbles_of_its_own_does_not_borrow_the_others(
    tmp_path: Path,
) -> None:
    # Grid bubbles count only inside their drawing's own scope. Drawing 2 has
    # no bubbles and walls that match no floor, so it must stay pending; it
    # must not register to a floor through drawing 1's bubbles, which would
    # silently place its device far from the building.
    third = Drawing(
        THIRD_ORIGIN, rooms=(THIRD_ROOM,), grid=TIGHT, title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",),
    )
    _, source, architecture = _architecture(
        tmp_path, Sheet((replace(SECOND, grid=TIGHT), third)), options=ImportOptions(registrations=(_floor_hint(),)),
    )
    storage = Drawing(
        UPPER.origin,
        rooms=(
            Room(0.0, 0.0, 250.0, 420.0, "ROOM: STORAGE"),
            Room(250.0, 0.0, 180.0, 150.0, "ROOM: CLOSET"),
        ),
        stub=False,
        evse=True,
        evse_tag="EVSE-2",
        evse_at=(200.0, 100.0),
    )
    electrical_path = _electrical(tmp_path, _e_sheet(replace(LOWER, grid=TIGHT), storage))
    result = _register(architecture, source, electrical_path)

    [page] = result.pages
    assert page.status == REGISTRATION_PENDING
    assert page.transform is None
    assert page.drawing_transforms == ()
    assert result.page_transforms() is None
    lower, upper = page.record["drawings"]
    # Drawing 1's own bubbles are the only ones in its scope.
    assert lower["grid_labels"] == ["1", "2", "A", "B"]
    assert lower["status"] == REGISTERED
    # Drawing 2 has no bubbles in its scope and walls that match no floor.
    assert upper["grid_labels"] == []
    assert upper["status"] == REGISTRATION_PENDING
    assert "insufficient_matched_evidence" in upper["reason_codes"]
    assert page.reason_codes == ("insufficient_matched_evidence",)

    # The pending page offers no transforms, so convergence refuses instead of
    # placing drawing 2's device somewhere unflagged.
    electrical = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical",
    )
    lane = electrical.attributes["pdf_electrical"]
    assert lane["registration_pending"] is True
    assert "drawing_region_transforms" not in lane
    with pytest.raises(PdfConvergenceError):
        converge_pdf_models(architecture, electrical)


def test_each_drawing_registers_with_the_bubbles_in_its_own_scope(tmp_path: Path) -> None:
    # Both drawings carry the same grid inside their own extents: each must
    # register with its own four bubbles, not a page-wide set.
    third = Drawing(
        THIRD_ORIGIN, rooms=(THIRD_ROOM,), grid=TIGHT, title="THIRD FLOOR PLAN", notes=("ELEVATION: 20'-0\"",),
    )
    _, source, architecture = _architecture(
        tmp_path, Sheet((replace(SECOND, grid=TIGHT), third)), options=ImportOptions(registrations=(_floor_hint(),)),
    )
    [page] = _register(
        architecture,
        source,
        _electrical(tmp_path, _e_sheet(replace(LOWER, grid=TIGHT), replace(UPPER, grid=TIGHT))),
    ).pages
    assert page.status == REGISTERED
    lower, upper = page.record["drawings"]
    assert [lower["grid_labels"], upper["grid_labels"]] == [["1", "2", "A", "B"]] * 2


def test_drawing_region_scopes_may_not_overlap(tmp_path: Path) -> None:
    # Two drawings placed so their scopes (each wall extent plus its annotation
    # margin) overlap cannot say which drawing a point between them belongs
    # to, even though both drawings registered on their own: the page stays
    # pending instead of offering ambiguous region transforms.
    southeast = replace(
        ELECTRICAL, origin=(ELECTRICAL.origin[0] + 560.0, ELECTRICAL.origin[1] + 250.0),
    )
    _, source, architecture = _architecture(tmp_path)
    result = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL, southeast)))
    [page] = result.pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("drawing_regions_overlap",)
    assert page.transform is None
    assert page.drawing_transforms == ()
    assert result.page_transforms() is None
    lower, upper = page.record["drawings"]
    assert [lower["status"], upper["status"]] == [REGISTERED, REGISTERED]


def test_per_drawing_registration_is_deterministic(tmp_path: Path) -> None:
    _, source, architecture = _distinct_floor_architecture(tmp_path)
    electrical_path = _electrical(tmp_path, _e_sheet(LOWER, UPPER))
    first = _register(architecture, source, electrical_path)
    second = _register(architecture, source, electrical_path)
    assert first.to_dict() == second.to_dict()
    assert first.page_transforms() == second.page_transforms()
    one = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=first.page_transforms(),
    )
    two = ElectricalPdfImporter().import_pdf(
        electrical_path, source_id="fixture:electrical", page_transforms=second.page_transforms(),
    )
    assert one.to_json() == two.to_json()
