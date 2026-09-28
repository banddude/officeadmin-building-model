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
    electrical_devices = PdfElectricalDocument(
        source_id="fixture:filled-construction-electrical",
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
    electrical_model = ElectricalPdfImporter().import_document(
        electrical_devices, page_transforms=result.page_transforms(),
    )
    merged = converge_pdf_models(model, electrical_model)
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
