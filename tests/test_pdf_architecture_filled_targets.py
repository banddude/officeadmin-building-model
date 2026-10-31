"""Synthetic construction-plan target coverage for issue #224."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.importers.pdf_architecture import sheet_wall_evidence
from oabm.importers.pdf_architecture import importer as architecture_importer
from oabm.importers.pdf_architecture.importer import classify_page, import_observations
from oabm.importers.pdf_architecture.types import (
    PdfDocumentObservation, PdfLineObservation, PdfPageObservation, PdfTextObservation,
)
from oabm.importers.pdf_convergence.sheet_registration import register_electrical_sheets
from oabm.importers.pdf_convergence import converge_pdf_models
from oabm.importers.pdf_electrical import (
    ElectricalPdfImporter, PdfElectricalDocument, PdfSymbolObservation,
    PdfTextObservation as ElectricalTextObservation,
)
from oabm.model import validate_model

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures/pdf_architecture/v1/filled-construction-targets.json").read_text())
MPP = 48 * .0254 / 72


def _text(key: str, value: str, x: float, y: float) -> PdfTextObservation:
    return PdfTextObservation(element_id=key, text=value, bbox_pt=(x, y, x + 220, y + 11))


def _strip(key: str, box: list[float], *, filled: bool = True) -> tuple[PdfLineObservation, ...]:
    x0, y0, x1, y1 = box
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return tuple(PdfLineObservation(
        element_id=f"{key}:{i}", start_pt=corners[i], end_pt=corners[(i + 1) % 4],
        primitive_family="polyline", filled=filled,
        fill_grays=(0.0,) if filled else (), stroke_present=False if filled else True,
        source_layers=(FIXTURE["fill_layer"],),
    ) for i in range(4))


def _plan(*, second: bool = False, excluded: bool = True) -> PdfPageObservation:
    boxes = list(FIXTURE["plan_a_strips"])
    if second:
        dx, dy = FIXTURE["plan_b_offset_pt"]
        boxes.extend([[x0 + dx, y0 + dy, x1 + dx + (25 if i == 4 else 0), y1 + dy]
                      for i, (x0, y0, x1, y1) in enumerate(FIXTURE["plan_a_strips"])])
    lines = tuple(line for i, box in enumerate(boxes) for line in _strip(f"wall:{i}", box))
    if excluded:
        lines += tuple(line for key, box in FIXTURE["excluded_fills"].items()
                       for line in _strip(key, box, filled=key != "sheet_frame"))
    texts = [
        _text("title", FIXTURE["construction_title"], 1040, 50),
        _text("scale", FIXTURE["scale_text"], 120, 150),
        _text("level", FIXTURE["level_text"], 120, 135),
        _text("room", "ROOM: SALES", 230, 350),
        _text("electrical-note", FIXTURE["electrical_note"], 950, 690),
    ]
    if second:
        texts += [_text("scale-b", FIXTURE["scale_text"], 770, 150),
                  _text("level-b", "LEVEL: SECOND FLOOR", 770, 135),
                  _text("elevation-b", "ELEVATION: 10'-0\"", 770, 120),
                  _text("room-b", "ROOM: STOCK", 880, 350),
                  _text("title-a", "CONSTRUCTION PLAN", 200, 50)]
    return PdfPageObservation(page_number=1, width_pt=FIXTURE["page_width_pt"],
                              height_pt=FIXTURE["page_height_pt"], texts=tuple(texts), lines=lines)


def _electrical() -> PdfPageObservation:
    dx, dy = FIXTURE["electrical_offset_pt"]
    lines = []
    for i, (x0, y0, x1, y1) in enumerate(FIXTURE["plan_a_strips"]):
        if x1 - x0 > y1 - y0:
            faces = [((x0 + dx, y0 + dy), (x1 + dx, y0 + dy)),
                     ((x0 + dx, y1 + dy), (x1 + dx, y1 + dy))]
        else:
            faces = [((x0 + dx, y0 + dy), (x0 + dx, y1 + dy)),
                     ((x1 + dx, y0 + dy), (x1 + dx, y1 + dy))]
        for j, (start, end) in enumerate(faces):
            lines.append(PdfLineObservation(element_id=f"e-wall:{i}:{j}", start_pt=start,
                                            end_pt=end, source_layers=("A-WALL",)))
    return PdfPageObservation(page_number=1, width_pt=FIXTURE["page_width_pt"],
                              height_pt=FIXTURE["page_height_pt"],
                              texts=(_text("e-title", "E-110 POWER PLAN", 960, 735),
                                     _text("e-scale", FIXTURE["scale_text"], 120, 150)),
                              lines=tuple(lines))


def _document(page: PdfPageObservation, key: str) -> PdfDocumentObservation:
    return PdfDocumentObservation(source_id=key, content_sha256=("a" if key == "arch" else "b") * 64,
                                  pages=(page,))


def _retitle(page: PdfPageObservation, key: str, value: str, x: float, y: float) -> PdfPageObservation:
    texts = tuple(item for item in page.texts if item.element_id != key)
    return replace(page, texts=(*texts, _text(key, value, x, y)))


def test_construction_plan_with_electrical_note_is_architectural_and_resolves_filled_region() -> None:
    page = _plan()
    assert classify_page(page).kind == "architectural_plan"
    evidence = sheet_wall_evidence(page, meters_per_point=MPP)
    assert evidence.evidence_kind in {"paired_wall_faces", "visible_wall_layer"}
    assert len(evidence.drawings) == 1
    bbox, segments = evidence.drawings[0]
    assert len(segments) >= 8
    assert 100 <= bbox[0] < bbox[2] <= 470
    assert 160 <= bbox[1] < bbox[3] <= 535
    model = import_observations(_document(page, "arch"))
    validate_model(model)
    assert len(model.walls) >= 4
    assert model.attributes["pdf_architecture"]["drawing_regions"][0]["status"] == "resolved"
    assert model.to_json() == import_observations(_document(page, "arch")).to_json()


def test_filled_strips_supply_evidence_when_face_pairing_is_sparse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(architecture_importer, "_geometric_wall_face_pairs", lambda *args, **kwargs: [])
    evidence = sheet_wall_evidence(_plan(excluded=False), meters_per_point=MPP)
    assert evidence.evidence_kind == "paired_wall_faces"
    assert len(evidence.drawings) == 1
    assert len(evidence.drawings[0][1]) == len(FIXTURE["plan_a_strips"])


def _electrical_devices_document(x: float, y: float, *, source_id: str) -> PdfElectricalDocument:
    return PdfElectricalDocument(
        source_id=source_id,
        page_count=1,
        texts=(ElectricalTextObservation(
            element_id="electrical-sheet", page=1, text="E-110 POWER PLAN",
            x_pt=1100, y_pt=50,
        ),),
        symbols=(PdfSymbolObservation(
            element_id="device:1", page=1, name="DUPLEX RECEPTACLE OUTLET",
            x_pt=x, y_pt=y, metadata={"native_id": "device-1"},
        ),),
        page_provenance={1: {
            "page_rotation": 0,
            "displayed_page_width_pt": FIXTURE["page_width_pt"],
            "displayed_page_height_pt": FIXTURE["page_height_pt"],
            "coordinate_space": "displayed",
        }},
    )


def test_filled_plan_registers_sparse_electrical_walls() -> None:
    source = _document(_plan(), "arch")
    model = import_observations(source)
    electrical = _document(_electrical(), "electrical")
    result = register_electrical_sheets(model, source, electrical)
    assert result.all_registered
    record = result.pages[0].record["registration"]
    assert record["wall_inlier_count"] >= 8
    assert record["wall_residual_rms_m"] <= .05
    transform = result.pages[0].transform
    assert transform is not None
    # A synthetic device point is positioned by the accepted canonical transform.
    dx, dy = FIXTURE["electrical_offset_pt"]
    x, y = 250 + dx, 300 + dy
    point = transform.apply(x, y)
    assert point.x == pytest.approx((x - dx) * MPP, abs=1e-6)
    assert point.y == pytest.approx((y - dy) * MPP, abs=1e-6)
    assert point.z == model.levels[0].elevation_m
    electrical_model = ElectricalPdfImporter().import_document(
        _electrical_devices_document(x, y, source_id=electrical.source_id),
        page_transforms=result.page_transforms(),
    )
    merged = converge_pdf_models(model, electrical_model, registration=result)
    validate_model(merged)
    assert len(merged.electrical_devices) == 1
    placed = merged.electrical_devices[0]
    assert placed.device_type == "receptacle_duplex"
    assert placed.pose.position.x == pytest.approx(point.x, abs=1e-6)
    assert placed.pose.position.y == pytest.approx(point.y, abs=1e-6)
    assert placed.level_id == model.levels[0].id


def test_true_electrical_sheet_and_unsupported_fill_do_not_become_targets() -> None:
    assert classify_page(_electrical()).kind == "electrical"
    electrical_with_reference = replace(
        _electrical(),
        texts=(*_electrical().texts,
               _text("construction-reference", "SEE A-110 CONSTRUCTION PLAN", 120, 50)),
    )
    assert classify_page(electrical_with_reference).kind == "electrical"
    page = _plan()
    unsupported = replace(page, lines=tuple(
        line for line in page.lines if line.element_id.startswith(("legend_symbol", "wide_panel", "sheet_frame"))
    ))
    evidence = sheet_wall_evidence(unsupported, meters_per_point=MPP)
    assert not evidence.drawings
    model = import_observations(_document(unsupported, "arch"))
    assert not [r for r in model.attributes["pdf_architecture"]["drawing_regions"] if r["status"] == "resolved"]


def test_two_filled_floor_drawings_split_into_distinct_regions() -> None:
    page = _plan(second=True, excluded=False)
    evidence = sheet_wall_evidence(page, meters_per_point=MPP)
    assert len(evidence.drawings) == 2
    model = import_observations(_document(page, "arch"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    assert len(regions) == 2
    assert regions[0]["status"] == "resolved"
    assert regions[1]["status"] == "unresolved"
    assert regions[1]["reason_codes"] == ["registration_unresolved"]
    assert regions[0]["source_bbox_pt"][2] < regions[1]["source_bbox_pt"][0]


def test_upper_wall_detail_does_not_become_an_unnamed_floor() -> None:
    base = _plan(excluded=False)
    detail = (
        [120, 780, 445, 786], [120, 1074, 445, 1080],
        [110, 790, 116, 1070], [449, 790, 455, 1070],
    )
    page = replace(
        base,
        height_pt=1200,
        lines=(*base.lines, *(line for i, box in enumerate(detail)
                              for line in _strip(f"detail:{i}", box))),
        texts=(*base.texts, _text("local-title", "CONSTRUCTION PLAN", 240, 50)),
    )
    evidence = sheet_wall_evidence(page, meters_per_point=MPP)
    assert len(evidence.drawings) == 1
    assert evidence.drawings[0][0][3] < 600
    model = import_observations(_document(page, "arch"))
    validate_model(model)
    assert [r["status"] for r in model.attributes["pdf_architecture"]["drawing_regions"]] == ["resolved"]


def test_earlier_egress_sheet_does_not_claim_the_construction_frame() -> None:
    original = _plan(excluded=False)
    egress = replace(
        original,
        lines=tuple(line for line in original.lines
                    if int(line.element_id.split(":")[1]) < 4),
        texts=tuple(replace(item, text="A-001 EGRESS PLAN") if item.element_id == "title"
                    else item for item in original.texts),
    )
    construction = replace(original, page_number=2)
    source = PdfDocumentObservation(
        source_id="fixture:construction-after-egress", content_sha256="c" * 64,
        pages=(egress, construction),
    )
    model = import_observations(source)
    validate_model(model)
    pages = model.attributes["pdf_architecture"]["pages"]
    assert [item["page"] for item in pages] == [1, 2]
    assert pages[1]["status"] == "geometry_imported"
    assert any(region["page"] == 2 and region["status"] == "resolved"
               for region in model.attributes["pdf_architecture"]["drawing_regions"])


def test_construction_titles_assign_distinct_floor_and_mezzanine_names() -> None:
    page = _plan(second=True, excluded=False)
    texts = tuple(item for item in page.texts if item.element_id not in {"level", "level-b"})
    page = replace(page, texts=(*texts,
        _text("first-title", "CONSTRUCTION PLAN - FIRST FLOOR", 200, 50),
        _text("mezz-title", "CONSTRUCTION PLAN - MEZZANINE", 850, 50),
    ))
    evidence = sheet_wall_evidence(page, meters_per_point=MPP)
    assert evidence.drawing_level_names == (("First Floor",), ("Mezzanine",))


def test_single_construction_plan_keeps_legend_boxes_out_of_canonical_rooms() -> None:
    from oabm.importers.pdf_architecture.types import PdfRectObservation
    base = _plan(excluded=False)
    page = replace(
        base,
        height_pt=1200,
        rects=(PdfRectObservation('legend-table', (100, 820, 220, 940)),
               PdfRectObservation('legend-inner', (106, 826, 214, 934))),
        texts=(*base.texts, PdfTextObservation('legend-caption', 'BRACING GUIDE', (120, 860, 200, 872)),
               _text('local-plan-title', 'CONSTRUCTION PLAN', 180, 50)),
    )
    model = import_observations(_document(page, 'arch'))
    validate_model(model)
    regions = model.attributes['pdf_architecture']['drawing_regions']
    assert len(regions) == 1 and regions[0]['scope'] == 'region'
    assert not any(space.name == 'BRACING GUIDE' for space in model.spaces)
    assert all(point.y < 600 * MPP for wall in model.walls for point in wall.centerline.points)
    assert all(point.y < 600 * MPP for space in model.spaces for point in space.footprint.points)


def test_unlayered_unknown_paint_is_not_positive_filled_registration_evidence(monkeypatch) -> None:
    monkeypatch.setattr(architecture_importer, '_geometric_wall_face_pairs', lambda *args, **kwargs: [])
    base = _plan(excluded=False)
    unknown = replace(base, lines=tuple(replace(line, source_layers=(), fill_grays=(), stroke_present=None) for line in base.lines))
    assert not sheet_wall_evidence(unknown, meters_per_point=MPP).drawings


def test_white_fill_masks_never_supply_the_filled_target_fallback(monkeypatch) -> None:
    monkeypatch.setattr(architecture_importer, '_geometric_wall_face_pairs', lambda *args, **kwargs: [])
    base = _plan(excluded=False)
    white = replace(base, lines=tuple(replace(line, source_layers=(), fill_grays=(1.0,), stroke_present=False) for line in base.lines))
    assert not sheet_wall_evidence(white, meters_per_point=MPP).drawings


def test_single_scoped_plan_retains_an_unambiguous_page_registration_hint() -> None:
    from oabm.importers.pdf_architecture.types import ImportOptions, RegistrationHint
    source = _document(_plan(excluded=False), 'arch')
    hint = RegistrationHint(1, (0, 0), (100, 0), (2, 3), (2 + 100 * MPP, 3))
    model = import_observations(source, options=ImportOptions(registrations=(hint,)))
    [region] = model.attributes['pdf_architecture']['drawing_regions']
    assert region['scope'] == 'region' and region['status'] == 'resolved'
    assert region['frame']['translation_m'] == pytest.approx([2, 3])
    assert not any(item['code'] == 'registration_hint_region_unresolved' for item in model.attributes['pdf_architecture']['ambiguities'])


def test_bounded_unlayered_wall_material_survives_nearby_dimension_labels() -> None:
    page = _plan(excluded=False)
    dimensions = tuple(
        PdfTextObservation(
            element_id=f"dimension:{index}", text="10'-0\"",
            bbox_pt=((box[0]+box[2])/2-18, (box[1]+box[3])/2-4,
                     (box[0]+box[2])/2+18, (box[1]+box[3])/2+4),
        )
        for index, box in enumerate(FIXTURE["plan_a_strips"])
    )
    page = replace(page, lines=tuple(replace(line, source_layers=()) for line in page.lines),
                   texts=(*page.texts, *dimensions))
    model = import_observations(_document(page, "unlayered-material"))
    validate_model(model)
    assert len(model.walls) == len(FIXTURE["plan_a_strips"])
    assert all("poche" in wall.attributes["pdf_architecture"]["recognition"] for wall in model.walls)
    assert model.to_json() == import_observations(_document(page, "unlayered-material")).to_json()


@pytest.mark.parametrize("paint", [(), (None,), (1.0,), (0.0, 1.0)])
def test_unlayered_poche_materialization_requires_unambiguous_nonmasking_paint(paint) -> None:
    page = _plan(excluded=False)
    labels = tuple(
        PdfTextObservation(element_id=f"dimension:{i}", text="10'-0\"",
                           bbox_pt=((b[0]+b[2])/2-18, (b[1]+b[3])/2-4,
                                    (b[0]+b[2])/2+18, (b[1]+b[3])/2+4))
        for i, b in enumerate(FIXTURE["plan_a_strips"])
    )
    page = replace(page, lines=tuple(replace(line, source_layers=(), fill_grays=paint)
                                    for line in page.lines), texts=(*page.texts, *labels))
    model = import_observations(_document(page, "unsupported-unlayered-fill"))
    assert not model.walls


def test_bounded_fallback_preserves_explicit_wall_layer_unknown_paint_authority() -> None:
    page = _plan(excluded=False)
    page = replace(page, lines=tuple(replace(line, fill_grays=()) for line in page.lines))
    model = import_observations(_document(page, "explicit-layer-unknown-paint"))
    validate_model(model)
    assert len(model.walls) == len(FIXTURE["plan_a_strips"])
    assert all("poche" in wall.attributes["pdf_architecture"]["recognition"] for wall in model.walls)


def _pricing_plan(*, sheet="PP-2.0", title="PRICING PLAN", legend=True):
    page = _plan(excluded=False)
    texts = tuple(t for t in page.texts if t.element_id != "title")
    texts += (
        _text("drawing-title-marker", "SHEET TITLE:", 1040, 130),
        _text("pricing-drawing-title", title, 1040, 95),
        _text("sheet-number-marker", "SHEET NO:", 1040, 65),
        _text("pricing-sheet-number", sheet, 1040, 30),
        _text("local-pricing-title", title, 180, 50),
    )
    if legend:
        texts += (_text("construction-legend", "CONSTRUCTION LEGEND", 950, 690),)
    return replace(page, texts=texts)


def test_explicit_pricing_plan_with_construction_legend_retains_architectural_geometry():
    page = _pricing_plan()
    assert classify_page(page).kind == "architectural_plan"
    model = import_observations(_document(page, "synthetic-pricing"))
    validate_model(model)
    assert model.walls
    assert model.to_json() == import_observations(_document(page, "synthetic-pricing")).to_json()


@pytest.mark.parametrize("sheet,title,legend", [
    ("E-2.0", "PRICING PLAN", True),
    ("PPP-2.0", "PRICING PLAN", True),
    ("PP-2.0", "POWER PLAN", True),
    ("PP-2.0", "PRICING PLAN", False),
])
def test_pricing_recovery_needs_explicit_role_and_construction_legend(sheet,title,legend):
    assert classify_page(_pricing_plan(sheet=sheet,title=title,legend=legend)).kind == "electrical"


def test_pricing_note_without_title_block_marker_does_not_override_discipline():
    page = _pricing_plan()
    page = replace(page,texts=tuple(t for t in page.texts if t.element_id != "drawing-title-marker"))
    assert classify_page(page).kind == "electrical"


def test_pricing_role_without_substantial_geometry_is_not_a_target():
    assert classify_page(replace(_pricing_plan(),lines=())).kind == "electrical"


def test_conflicting_pricing_and_electrical_sheet_marks_are_not_recovered():
    page = _pricing_plan()
    page = replace(page,texts=(*page.texts,_text("competing-mark","E-2.0",1040,15)))
    assert classify_page(page).kind == "electrical"


def test_small_print_disclaimer_does_not_become_part_of_explicit_pricing_title():
    page = _pricing_plan()
    page = replace(page,texts=tuple(replace(t,font_size_pt=20) if t.element_id == "pricing-drawing-title" else t for t in page.texts))
    page = replace(page,texts=(*page.texts,PdfTextObservation(
        "small-print", "PRICING INFORMATION ONLY; SEE ELECTRICAL DETAILS", (1040,78,1260,85),font_size_pt=8)))
    assert architecture_importer._explicit_drawing_title(page) == "PRICING PLAN"
    assert classify_page(page).kind == "architectural_plan"


def test_ambiguous_equal_size_text_is_not_discarded_to_force_pricing_role():
    page = _pricing_plan()
    page = replace(page,texts=tuple(replace(t,font_size_pt=20) if t.element_id == "pricing-drawing-title" else t for t in page.texts))
    page = replace(page,texts=(*page.texts,PdfTextObservation(
        "competing-title", "POWER PLAN", (1040,78,1260,89),font_size_pt=20)))
    assert architecture_importer._explicit_drawing_title(page) == "PRICING PLAN POWER PLAN"
    assert not architecture_importer._construction_title_and_sheet_mark(page)


def test_electrical_sheet_frame_never_masks_its_registration_wall_evidence():
    from oabm.importers.pdf_architecture.types import PdfRectObservation
    page = _electrical()
    base = sheet_wall_evidence(page,meters_per_point=MPP)
    framed = replace(page,rects=(PdfRectObservation("sheet-frame",(5,5,page.width_pt-5,page.height_pt-5)),),
                     texts=(*page.texts,_text("drawn-marker","DRAWN BY:",1050,90),_text("checked-marker","CHECKED BY:",1050,65)))
    assert architecture_importer.classify_page(framed).kind == "electrical"
    assert not architecture_importer._title_block_exclusion(framed)[0]
    assert sheet_wall_evidence(framed,meters_per_point=MPP).drawings == base.drawings


def test_electrical_sheet_keeps_its_actual_small_title_block_excluded():
    from oabm.importers.pdf_architecture.types import PdfRectObservation
    page = _electrical()
    title = (1030,35,1290,130)
    page = replace(page,rects=(PdfRectObservation("title-box",title),),
                   texts=(*page.texts,_text("drawn-marker","DRAWN BY:",1050,90),_text("checked-marker","CHECKED BY:",1050,65)))
    assert architecture_importer._title_block_exclusion(page)[0] == (title,)


def test_site_plan_printed_first_does_not_displace_the_floor_plan():
    site = PdfPageObservation(
        page_number=1, width_pt=FIXTURE["page_width_pt"], height_pt=FIXTURE["page_height_pt"],
        texts=(_text("site-title", "SITE PLAN", 1040, 50),
               _text("site-scale", FIXTURE["scale_text"], 120, 150)),
        lines=tuple(
            PdfLineObservation(
                element_id=f"site:{index}", start_pt=(x0, y0), end_pt=(x1, y1),
                primitive_family="polyline", source_layers=("C-SITE",),
            )
            for index, (x0, y0, x1, y1) in enumerate((
                (120, 180, 900, 186), (120, 700, 900, 706),
                (120, 180, 126, 700), (894, 180, 900, 700),
            ))
        ),
    )
    plan = _retitle(_plan(), "title", "A-110 CONSTRUCTION PLAN", 1040, 50)
    model = import_observations(PdfDocumentObservation(
        source_id="fixture:site-plan-first", content_sha256="d" * 64,
        pages=(site, replace(plan, page_number=2)),
    ))
    validate_model(model)
    resolved = [item for item in model.attributes["pdf_architecture"]["drawing_regions"]
                if item["status"] == "resolved"]
    assert resolved, "the floor plan must resolve behind the site plan"
    bbox = resolved[0]["source_bbox_pt"]
    assert 100 <= bbox[0] and bbox[2] <= 470


def test_key_plan_printed_first_does_not_displace_the_floor_plan():
    key = PdfPageObservation(
        page_number=1, width_pt=FIXTURE["page_width_pt"], height_pt=FIXTURE["page_height_pt"],
        texts=(_text("key-title", "KEY PLAN", 1040, 50),
               _text("key-scale", FIXTURE["scale_text"], 120, 150)),
        lines=tuple(
            PdfLineObservation(
                element_id=f"key:{index}", start_pt=(x0, y0), end_pt=(x1, y1),
                primitive_family="polyline", source_layers=("A-KEY",),
            )
            for index, (x0, y0, x1, y1) in enumerate((
                (120, 180, 700, 186), (120, 560, 700, 566),
                (120, 180, 126, 566), (694, 180, 700, 566),
            ))
        ),
    )
    model = import_observations(PdfDocumentObservation(
        source_id="fixture:key-plan-first", content_sha256="e" * 64,
        pages=(key, replace(_plan(), page_number=2)),
    ))
    validate_model(model)
    resolved = [item for item in model.attributes["pdf_architecture"]["drawing_regions"]
                if item["status"] == "resolved"]
    assert resolved, "the floor plan must resolve behind the key plan"
    bbox = resolved[0]["source_bbox_pt"]
    assert 100 <= bbox[0] and bbox[2] <= 470


def test_small_filled_symbols_inside_the_plan_area_stay_out_of_wall_evidence():
    page = _plan()
    symbols = tuple(
        line
        for index, (x, y) in enumerate(((200, 240), (300, 300), (250, 420), (350, 470)))
        for line in _strip(f"symbol:{index}", [x, y, x + 8, y + 8])
    )
    evidence = sheet_wall_evidence(replace(page, lines=(*page.lines, *symbols)), meters_per_point=MPP)
    baseline = sheet_wall_evidence(page, meters_per_point=MPP)
    assert evidence.drawings == baseline.drawings


def test_missing_scale_keeps_a_precise_reason_code():
    page = _plan()
    page = replace(page, texts=tuple(item for item in page.texts if item.element_id != "scale"))
    model = import_observations(_document(page, "no-scale"))
    unresolved = [item for item in model.attributes["pdf_architecture"]["drawing_regions"]
                  if item["status"] != "resolved"]
    assert unresolved, "a plan without any scale annotation must not resolve"
    assert any("scale" in code for code in unresolved[0]["reason_codes"])


def test_dimension_ticks_and_hatch_fields_stay_out_of_construction_wall_evidence():
    page = _plan()
    extra = [
        PdfLineObservation(
            element_id=f"hatch:{index}", start_pt=(360, 240 + index * 6),
            end_pt=(430, 250 + index * 6), primitive_family="polyline",
            filled=False, stroke_present=True, source_layers=("A-WALL-PATT",),
        )
        for index in range(6)
    ]
    extra.append(PdfLineObservation(
        element_id="dimension-line", start_pt=(100, 170), end_pt=(460, 170),
        primitive_family="polyline", source_layers=("A-DIMS",),
    ))
    extra.extend(_strip("dimension-tick-left", [96, 166, 104, 174], filled=False))
    extra.extend(_strip("dimension-tick-right", [456, 166, 464, 174], filled=False))
    annotated = replace(page, lines=(*page.lines, *extra))
    baseline = sheet_wall_evidence(page, meters_per_point=MPP)
    evidence = sheet_wall_evidence(annotated, meters_per_point=MPP)
    # Ticks and hatch strokes on a wall-pattern layer must not flip the sheet
    # into the visible-wall-layer mode and evict the filled strips.
    assert evidence.evidence_kind == baseline.evidence_kind
    assert evidence.drawings == baseline.drawings
    model = import_observations(_document(annotated, "annotated"))
    validate_model(model)
    reference = import_observations(_document(page, "annotated"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    reference_regions = reference.attributes["pdf_architecture"]["drawing_regions"]
    assert [item["status"] for item in regions] == [item["status"] for item in reference_regions]
    assert [item["source_bbox_pt"] for item in regions] == [
        item["source_bbox_pt"] for item in reference_regions
    ]
    assert len(model.walls) == len(reference.walls) == len(FIXTURE["plan_a_strips"])


def test_multi_scale_sheet_splits_and_binds_each_region_to_its_own_scale():
    page = _plan(second=True, excluded=False)
    page = replace(page, texts=tuple(
        _text("scale-b", 'SCALE: 1/8" = 1\'-0"', 770, 150)
        if item.element_id == "scale-b" else item
        for item in page.texts
    ))
    model = import_observations(_document(page, "multi-scale"))
    validate_model(model)
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    assert [item["scope"] for item in regions] == ["region", "region"]
    assert regions[0]["source_bbox_pt"][2] < regions[1]["source_bbox_pt"][0]
    first, second = regions
    assert first["status"] == "resolved"
    assert first["scale"]["source_element_id"] == "scale"
    assert first["scale"]["meters_per_point"] == pytest.approx(48 * .0254 / 72, abs=1e-9)
    # The second drawing keeps its own printed scale evidence but carries no
    # frame: nothing registers a second floor drawn beside the first onto the
    # canonical frame without a region-scoped registration hint.
    assert second["scale"]["source_element_id"] == "scale-b"
    assert second["scale"]["meters_per_point"] == pytest.approx(96 * .0254 / 72, abs=1e-9)
    assert second["status"] == "unresolved"
    assert second["reason_codes"] == ["registration_unresolved"]
    assert second["frame"] is None
    detection = model.attributes["pdf_architecture"]["pages"][0]["drawing_region_detection"]
    assert detection["drawing_cluster_count"] == 2
    assert detection["detection_meters_per_point"] == pytest.approx(48 * .0254 / 72, abs=1e-9)
    assert model.to_json() == import_observations(_document(page, "multi-scale")).to_json()


def test_native_rectangle_ticks_stay_out_of_construction_wall_evidence():
    from oabm.importers.pdf_architecture.types import PdfRectObservation
    page = _plan()
    ticks = (
        PdfRectObservation("rect-tick-left", (96, 166, 104, 174), source_layer=FIXTURE["fill_layer"]),
        PdfRectObservation("rect-tick-right", (456, 166, 464, 174), source_layer=FIXTURE["fill_layer"]),
    )
    annotated = replace(page, rects=(*page.rects, *ticks))
    baseline = sheet_wall_evidence(page, meters_per_point=MPP)
    evidence = sheet_wall_evidence(annotated, meters_per_point=MPP)
    # Native stroked rectangles on a wall-pattern layer are the same ticks to
    # a reader as drawn ones; their outline segments must not claim the
    # wall-layer mode or grow the evidence bbox.
    assert evidence.evidence_kind == baseline.evidence_kind == "paired_wall_faces"
    assert evidence.drawings == baseline.drawings
    model = import_observations(_document(annotated, "rect-ticks"))
    validate_model(model)
    reference = import_observations(_document(page, "rect-ticks"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    reference_regions = reference.attributes["pdf_architecture"]["drawing_regions"]
    assert [item["status"] for item in regions] == [item["status"] for item in reference_regions]
    assert [item["source_bbox_pt"] for item in regions] == [
        item["source_bbox_pt"] for item in reference_regions
    ]
    assert len(model.walls) == len(reference.walls) == len(FIXTURE["plan_a_strips"])


def test_native_rectangle_ticks_do_not_break_sparse_electrical_registration():
    from oabm.importers.pdf_architecture.types import PdfRectObservation
    page = _plan()
    ticks = (
        PdfRectObservation("rect-tick-left", (96, 166, 104, 174), source_layer=FIXTURE["fill_layer"]),
        PdfRectObservation("rect-tick-right", (456, 166, 464, 174), source_layer=FIXTURE["fill_layer"]),
    )
    annotated = replace(page, rects=(*page.rects, *ticks))
    source = _document(annotated, "arch")
    model = import_observations(source)
    validate_model(model)
    electrical_source = _document(_electrical(), "electrical")
    result = register_electrical_sheets(model, source, electrical_source)
    assert result.all_registered
    record = result.pages[0].record["registration"]
    assert record["wall_inlier_count"] >= 8
    assert record["wall_residual_rms_m"] <= .05
    transform = result.pages[0].transform
    assert transform is not None
    dx, dy = FIXTURE["electrical_offset_pt"]
    x, y = 250 + dx, 300 + dy
    point = transform.apply(x, y)
    assert point.x == pytest.approx((x - dx) * MPP, abs=1e-6)
    assert point.y == pytest.approx((y - dy) * MPP, abs=1e-6)
    electrical_model = ElectricalPdfImporter().import_document(
        _electrical_devices_document(x, y, source_id=electrical_source.source_id),
        page_transforms=result.page_transforms(),
    )
    merged = converge_pdf_models(model, electrical_model, registration=result)
    validate_model(merged)
    assert len(merged.electrical_devices) == 1
    placed = merged.electrical_devices[0]
    assert placed.device_type == "receptacle_duplex"
    assert placed.pose.position.x == pytest.approx(point.x, abs=1e-6)
    assert placed.pose.position.y == pytest.approx(point.y, abs=1e-6)
    assert placed.level_id == model.levels[0].id
