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
from oabm.importers.pdf_electrical import ElectricalPdfError, ElectricalPdfImporter
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


@dataclass(frozen=True)
class Sheet:
    drawings: tuple[Drawing, ...]
    wall_layer: str = "A-WALL"
    texts: tuple[str, ...] = (QUARTER,)
    sheet_number: str = "A9.1"
    extra: tuple[str, ...] = field(default_factory=tuple)


def _local(drawing: Drawing, point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
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
            ex, ey = _local(drawing, EVSE_AT)
            commands.append(f"q 1 0 0 1 {ex:.4f} {ey:.4f} cm /EVSE1 Do Q")
            commands.append(_text(ex + 5, ey + 5, "EVSE-1 +48\" AFF WALL MTD", size=9))
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
    [page] = _register(architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL))).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("competing_targets",)
    assert page.record["competing_region_ids"] == sorted(region["region_id"] for region in regions)


def test_multiple_drawings_on_one_electrical_page_stay_pending(tmp_path: Path) -> None:
    _, source, architecture = _two_floor_architecture(tmp_path)
    upper = replace(ELECTRICAL, origin=(800.0 + OFFSET[0], 400.0 + OFFSET[1]), title="THIRD FLOOR POWER PLAN")
    [page] = _register(
        architecture, source, _electrical(tmp_path, _e_sheet(ELECTRICAL, upper)),
    ).pages
    assert page.status == REGISTRATION_PENDING
    assert page.reason_codes == ("multiple_drawing_regions_on_page",)
    assert len(page.record["drawings"]) == 2


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
