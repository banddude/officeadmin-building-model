import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

from oabm.model import BuildingModel, validate_model
from oabm.importers.pdf_architecture import ImportOptions, LevelOverride, RegistrationHint, ScaleOverride
from oabm.importers.pdf_architecture.extract import _group_words, _unique_lines, extract_pdf
from oabm.importers.pdf_architecture.importer import classify_page, import_architectural_pdf, import_observations
from oabm.importers.pdf_architecture.types import (
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "pdf_architecture" / "v1"
CAD_GEOMETRY_FIXTURE = FIXTURE_DIR / "cad-export-geometry-only.pdf"
CAD_GEOMETRY_TEXT_FIXTURE = FIXTURE_DIR / "cad-export-geometry-plus-text.pdf"
REGISTRATION_FALLBACK_FIXTURE = FIXTURE_DIR / "cad-export-scale-no-registration.pdf"
DENSE_LABEL_FIXTURE = FIXTURE_DIR / "dense-room-labels.pdf"
ADJACENT_LABEL_PAIR_FIXTURE = FIXTURE_DIR / "adjacent-room-label-pairs.pdf"
WALL_PRIMITIVE_FAMILIES_FIXTURE = FIXTURE_DIR / "cad-wall-primitive-families.pdf"


def _text(element_id: str, text: str, x: float, y: float, width: float = 80, height: float = 10) -> PdfTextObservation:
    return PdfTextObservation(element_id=element_id, text=text, bbox_pt=(x, y, x + width, y + height))


def _plan_page(
    *,
    page_number: int = 1,
    room_name: str = "OFFICE",
    dx: float = 0.0,
    include_height: bool = True,
    scales: tuple[str, ...] = ("SCALE: 1:100",),
) -> PdfPageObservation:
    texts = [
        _text(f"p{page_number}:title", "A1.1 FLOOR PLAN", 10 + dx, 180),
        *(_text(f"p{page_number}:scale:{index}", value, 10 + dx, 165 - index * 12) for index, value in enumerate(scales)),
        _text(f"p{page_number}:level", "LEVEL: GROUND", 10 + dx, 140),
        _text(f"p{page_number}:elev", "ELEVATION: 0'-0\"", 10 + dx, 128),
        _text(f"p{page_number}:room", f"ROOM: {room_name}", 70 + dx, 70),
    ]
    if include_height:
        texts.append(_text(f"p{page_number}:height", "CEILING HEIGHT: 9'-0\"", 10 + dx, 116))
    return PdfPageObservation(
        page_number=page_number,
        width_pt=300,
        height_pt=220,
        texts=tuple(texts),
        rects=(
            PdfRectObservation(element_id=f"p{page_number}:outer", bbox_pt=(20 + dx, 20, 220 + dx, 120)),
            PdfRectObservation(element_id=f"p{page_number}:inner", bbox_pt=(24 + dx, 24, 216 + dx, 116)),
        ),
    )


def _document(*pages: PdfPageObservation, source_id: str = "fixture:observations", digest: str = "a" * 64) -> PdfDocumentObservation:
    return PdfDocumentObservation(source_id=source_id, content_sha256=digest, pages=tuple(pages))


def _line(
    element_id: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> PdfLineObservation:
    return PdfLineObservation(element_id=element_id, start_pt=start, end_pt=end)


def _ordinary_vector_fixture_page() -> PdfPageObservation:
    payload = json.loads(
        (FIXTURE_DIR / "ordinary-vector-room.json").read_text(encoding="utf-8")
    )
    return PdfPageObservation(
        page_number=payload["page_number"],
        width_pt=payload["width_pt"],
        height_pt=payload["height_pt"],
        texts=tuple(
            PdfTextObservation(
                element_id=item["element_id"],
                text=item["text"],
                bbox_pt=tuple(item["bbox_pt"]),
            )
            for item in payload["texts"]
        ),
        lines=tuple(
            PdfLineObservation(
                element_id=item["element_id"],
                start_pt=tuple(item["start_pt"]),
                end_pt=tuple(item["end_pt"]),
            )
            for item in payload["lines"]
        ),
    )


def _cad_derived_wall_face_page() -> PdfPageObservation:
    payload = json.loads(
        (FIXTURE_DIR / "cad-derived-wall-faces.json").read_text(encoding="utf-8")
    )
    return PdfPageObservation(
        page_number=payload["page_number"],
        width_pt=payload["width_pt"],
        height_pt=payload["height_pt"],
        texts=(
            _text("cad:title", "A44 FLOOR PLAN", 10, 190),
            _text("cad:scale", "SCALE: 1:100", 10, 176),
            _text("cad:level", "LEVEL: GROUND", 10, 162),
        ),
        lines=tuple(
            PdfLineObservation(
                element_id=item["element_id"],
                start_pt=tuple(item["start_pt"]),
                end_pt=tuple(item["end_pt"]),
            )
            for item in payload["lines"]
        ),
    )


def _loop_lines(
    prefix: str,
    bbox: tuple[float, float, float, float],
) -> tuple[PdfLineObservation, ...]:
    x0, y0, x1, y1 = bbox
    return (
        _line(f"{prefix}:south", (x0, y0), (x1, y0)),
        _line(f"{prefix}:east", (x1, y0), (x1, y1)),
        _line(f"{prefix}:north", (x0, y1), (x1, y1)),
        _line(f"{prefix}:west", (x0, y0), (x0, y1)),
    )


def _ambiguity_codes(model: BuildingModel) -> set[str]:
    return {item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]}


def _write_source_pdf_with_page_frame(
    path: Path, *, room: bool, drawing_title: str | None = None,
    frame_as_lines: bool = False,
) -> None:
    """Synthetic CAD-like source PDF: page frame plus optional true room."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    if frame_as_lines:
        frame = [
            "10 10 m 602 10 l S", "602 10 m 602 782 l S",
            "602 782 m 10 782 l S", "10 782 m 10 10 l S",
            "12 12 m 600 12 l S", "600 12 m 600 780 l S",
            "600 780 m 12 780 l S", "12 780 m 12 12 l S",
        ]
    else:
        frame = ["10 10 592 772 re S", "12 12 588 768 re S"]
    commands = [
        *frame,
        "BT /F1 10 Tf 1 0 0 1 25 740 Tm (A210 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 25 722 Tm (SCALE: 1:100) Tj ET",
        "BT /F1 9 Tf 1 0 0 1 320 650 Tm (TAPED TO LEVEL 4 FINISH) Tj ET",
    ]
    if room:
        commands.extend((
            "100 200 200 200 re S", "104 204 192 192 re S",
            "BT /F1 12 Tf 1 0 0 1 175 300 Tm (ROOM: OFFICE) Tj ET",
        ))
    if drawing_title:
        commands.extend((
            "BT /F1 10 Tf 1 0 0 1 470 140 Tm (DRAWING TITLE:) Tj ET",
            f"BT /F1 10 Tf 1 0 0 1 470 110 Tm ({drawing_title}) Tj ET",
            "BT /F1 10 Tf 1 0 0 1 470 60 Tm (SHEET NO:) Tj ET",
        ))
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_source_pdf_rejects_sheet_frame_and_keeps_real_room(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-floor-with-frame.pdf"
    _write_source_pdf_with_page_frame(source, room=True)
    assert not source.with_suffix(".expected.json").exists()
    extracted = extract_pdf(source, source_id="fixture:floor-with-frame")
    model = import_observations(extracted, options=ImportOptions(default_wall_height_m=3.0))
    assert len(model.spaces) == 1
    assert len(model.walls) == 4
    assert model.levels[0].name == "Unlabeled Level"
    assert "sheet_frame_enclosure_rejected" in _ambiguity_codes(model)
    assert max(point.x for point in model.spaces[0].footprint.points) < 12.0


def test_source_pdf_sheet_frame_alone_is_not_building_geometry(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-floor-frame-only.pdf"
    _write_source_pdf_with_page_frame(source, room=False)
    assert not source.with_suffix(".expected.json").exists()
    model = import_observations(extract_pdf(source, source_id="fixture:frame-only"))
    assert model.spaces == ()
    assert model.walls == ()
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == "no_supported_geometry_recognized"


def test_source_pdf_ordinary_vector_frame_is_not_a_room(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-vector-frame.pdf"
    _write_source_pdf_with_page_frame(source, room=False, frame_as_lines=True)
    assert not source.with_suffix(".expected.json").exists()
    model = import_observations(extract_pdf(source, source_id="fixture:vector-frame"))
    assert model.spaces == ()
    assert model.walls == ()
    assert "sheet_frame_enclosure_rejected" in _ambiguity_codes(model)


def test_source_pdf_detail_title_overrules_incidental_floor_plan_words(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-detail-sheet.pdf"
    _write_source_pdf_with_page_frame(source, room=True, drawing_title="INTERIOR ELEVATIONS")
    assert not source.with_suffix(".expected.json").exists()
    extracted = extract_pdf(source, source_id="fixture:detail-sheet")
    assert classify_page(extracted.pages[0]).kind == "other"
    model = import_observations(extracted)
    assert model.spaces == ()
    assert model.walls == ()


def test_source_pdf_wall_layers_support_partial_walls_without_false_rooms(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-layered-walls.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    wall_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-WALL"),
    })
    annotation_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-ANNO-DIMS"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        NameObject("/Properties"): DictionaryObject({
            NameObject("/WALL"): writer._add_object(wall_group),
            NameObject("/NOTE"): writer._add_object(annotation_group),
        }),
    })
    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (A210 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 720 Tm (SCALE: 1/8\" = 1'-0\") Tj ET",
        "/OC /WALL BDC",
        "100 200 m 100 500 l S", "103 200 m 103 500 l S",
        "100 200 m 300 200 l S", "100 203 m 300 203 l S",
        "EMC",
        "/OC /NOTE BDC",
        "350 200 m 350 500 l S", "353 200 m 353 500 l S",
        "EMC",
    ]
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with source.open("wb") as handle:
        writer.write(handle)

    extracted = extract_pdf(source, source_id="fixture:layered-walls")
    wall_lines = [
        line for line in extracted.pages[0].lines
        if "A-WALL" in line.source_layers
    ]
    assert len(wall_lines) == 4
    assert sum("A-ANNO-DIMS" in line.source_layers for line in extracted.pages[0].lines) == 2
    model = import_observations(
        extracted,
        options=ImportOptions(
            default_wall_height_m=3.0,
            scale_overrides=(ScaleOverride(1, 0.03386666666666666),),
        ),
    )
    assert model.spaces == ()
    assert len(model.walls) == 2
    assert all(
        wall.attributes["pdf_architecture"]["source_layers"] == ["A-WALL"]
        for wall in model.walls
    )
    validate_model(model)


def test_identical_pdf_geometry_preserves_all_source_layer_evidence() -> None:
    line = {"x0": 10.0, "y0": 20.0, "x1": 110.0, "y1": 20.0}
    observations = _unique_lines(
        (
            {**line, "_oabm_source_layer": "A-ANNO-DIMS"},
            {**line, "_oabm_source_layer": "A-WALL"},
        ),
        1,
    )
    assert len(observations) == 1
    assert observations[0].source_layers == ("A-ANNO-DIMS", "A-WALL")




def test_cad_export_curves_survive_architectural_extraction_as_lines() -> None:
    document = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )
    repeated = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )

    assert document == repeated
    assert len(document.pages) == 1
    page = document.pages[0]
    assert not page.texts
    assert not page.rects
    assert len(page.lines) == 17
    assert all(line.native_id is None for line in page.lines)
    segments = {(line.start_pt, line.end_pt) for line in page.lines}
    assert ((18.0, 24.0), (66.0, 38.0)) in segments
    assert ((66.0, 38.0), (108.0, 24.0)) in segments
    assert ((132.0, 24.0), (154.0, 24.0)) in segments
    assert {
        ((40.0, 40.0), (240.0, 40.0)),
        ((40.0, 44.0), (240.0, 44.0)),
        ((236.0, 40.0), (236.0, 140.0)),
        ((240.0, 40.0), (240.0, 140.0)),
        ((40.0, 136.0), (240.0, 136.0)),
        ((40.0, 140.0), (240.0, 140.0)),
        ((40.0, 40.0), (40.0, 140.0)),
        ((44.0, 40.0), (44.0, 140.0)),
    }.issubset(segments)


def test_cad_export_fixture_extracts_to_untagged_walls_and_space() -> None:
    document = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )
    assert len(document.pages) == 1
    extracted_page = document.pages[0]
    assert extracted_page.lines
    assert all(line.native_id is None for line in extracted_page.lines)

    contextual_page = replace(
        extracted_page,
        texts=(
            _text("acceptance:title", "A44 FLOOR PLAN", 10, 160),
            _text("acceptance:scale", "SCALE: 1:100", 10, 146),
            _text("acceptance:level", "LEVEL: GROUND", 10, 132),
        ),
    )
    model = import_observations(replace(document, pages=(contextual_page,)))

    validate_model(model)
    assert len(model.walls) > 0
    assert len(model.spaces) >= 1
    assert all(
        wall.attributes["pdf_architecture"]["recognition"]
        == "geometric_parallel_wall_faces"
        for wall in model.walls
    )
    assert any(
        space.attributes["pdf_architecture"]["recognition"]
        == "geometric_parallel_wall_closed_loop"
        for space in model.spaces
    )
    page_meta = model.attributes["pdf_architecture"]["pages"][0]
    assert page_meta["status"] == "geometry_imported"
    assert page_meta["resolved_wall_count"] > 0
    assert page_meta["resolved_room_count"] >= 1



def test_realistic_wall_primitive_families_join_before_pairing() -> None:
    assert not WALL_PRIMITIVE_FAMILIES_FIXTURE.with_suffix(".expected.json").exists()

    document = extract_pdf(
        WALL_PRIMITIVE_FAMILIES_FIXTURE,
        source_id="fixture:cad-wall-primitive-families",
    )
    assert len(document.pages) == 1
    extracted_page = document.pages[0]
    assert not extracted_page.texts
    assert not extracted_page.rects
    assert {line.primitive_family for line in extracted_page.lines} == {
        "line",
        "polyline",
        "curve",
    }
    assert any(line.dashed for line in extracted_page.lines)

    contextual_page = replace(
        extracted_page,
        texts=(
            _text("a55:title", "A55 FLOOR PLAN", 10, 180),
            _text("a55:scale", "SCALE: 1:100", 10, 166),
            _text("a55:level", "LEVEL: GROUND", 10, 152),
        ),
    )
    model = import_observations(replace(document, pages=(contextual_page,)))

    validate_model(model)
    assert len(model.walls) > 0
    assert len(model.spaces) == 1
    assert all(
        wall.attributes["pdf_architecture"]["recognition"]
        == "geometric_parallel_wall_faces"
        for wall in model.walls
    )
    page_meta = model.attributes["pdf_architecture"]["pages"][0]
    diagnostics = page_meta["geometric_wall_pair_diagnostics"]
    assert diagnostics["input_segment_count"] == len(extracted_page.lines)
    assert diagnostics["joined_run_count"] < diagnostics["unique_segment_count"]
    assert diagnostics["collinear_join_component_count"] > 0
    assert diagnostics["primitive_family_counts"]["polyline"] > 0
    assert diagnostics["primitive_family_counts"]["curve"] > 0
    assert diagnostics["dashed_input_segment_count"] > 0
    assert diagnostics["wall_gap_range_m"] == pytest.approx([0.0508, 0.4572])
    assert diagnostics["accepted_pair_count"] > 0
    assert diagnostics["style_or_layer_gate_applied"] is False
    assert set(diagnostics["rejected"]) == {
        "parallel",
        "overlap_ratio",
        "gap_range",
        "ambiguous_or_non_mutual",
    }


def test_open_unique_wall_pair_emits_lower_confidence_partial_wall() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("partial:title", "A55 FLOOR PLAN", 10, 180),
            _text("partial:scale", "SCALE: 1:100", 10, 166),
            _text("partial:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=(
            PdfLineObservation(
                element_id="partial:face-a:1",
                start_pt=(40.0, 40.0),
                end_pt=(90.0, 40.0),
                primitive_family="polyline",
            ),
            PdfLineObservation(
                element_id="partial:face-a:2",
                start_pt=(92.0, 40.0),
                end_pt=(160.0, 40.0),
                primitive_family="polyline",
            ),
            PdfLineObservation(
                element_id="partial:face-b",
                start_pt=(40.0, 44.0),
                end_pt=(160.0, 44.0),
                primitive_family="curve",
                dashed=True,
            ),
            _line("partial:junction", (40.0, 42.0), (40.0, 70.0)),
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:partial-wall-face")
    )

    validate_model(model)
    assert len(model.walls) == 1
    assert not model.spaces
    wall = model.walls[0]
    assert wall.confidence <= 0.42
    assert wall.attributes["pdf_architecture"]["recognition"] == (
        "geometric_parallel_wall_face_partial"
    )
    assert wall.attributes["pdf_architecture"]["primitive_families"] == [
        "curve",
        "polyline",
    ]
    assert wall.attributes["pdf_architecture"]["dashed_source"] is True
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["partial_pair_count"] == 1
    assert diagnostics["partial_no_junction_rejected_count"] == 0
    assert diagnostics["closed_loop_pair_count"] == 0
    assert wall.attributes["pdf_architecture"]["junction_supported"] is True


def test_isolated_open_wall_pair_without_junction_is_not_promoted() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("isolated:title", "A55 FLOOR PLAN", 10, 180),
            _text("isolated:scale", "SCALE: 1:100", 10, 166),
            _text("isolated:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=(
            _line("isolated:face-a", (40.0, 40.0), (160.0, 40.0)),
            _line("isolated:face-b", (40.0, 44.0), (160.0, 44.0)),
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:isolated-partial-wall-face")
    )

    validate_model(model)
    assert not model.walls
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["accepted_pair_count"] == 1
    assert diagnostics["partial_candidate_pair_count"] == 1
    assert diagnostics["partial_no_junction_rejected_count"] == 1
    assert diagnostics["partial_pair_count"] == 0


def test_short_open_wall_pair_with_junction_is_not_promoted() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("short:title", "A55 FLOOR PLAN", 10, 180),
            _text("short:scale", "SCALE: 1:100", 10, 166),
            _text("short:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=(
            _line("short:face-a", (40.0, 40.0), (52.0, 40.0)),
            _line("short:face-b", (40.0, 44.0), (52.0, 44.0)),
            _line("short:junction", (40.0, 42.0), (40.0, 70.0)),
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:short-partial-wall-face")
    )

    validate_model(model)
    assert not model.walls
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["accepted_pair_count"] == 1
    assert diagnostics["partial_candidate_pair_count"] == 1
    assert diagnostics["partial_short_rejected_count"] == 1
    assert diagnostics["partial_pair_count"] == 0


@pytest.mark.parametrize("dimension_text", ("6'-0\"", "1830"))
def test_dimension_string_parallel_pair_is_not_wall_evidence(
    dimension_text: str,
) -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("dim:title", "A55 FLOOR PLAN", 10, 180),
            _text("dim:scale", "SCALE: 1:100", 10, 166),
            _text("dim:level", "LEVEL: GROUND", 10, 152),
            _text("dim:value", dimension_text, 90, 38, width=40, height=8),
        ),
        lines=(
            _line("dim:face-a", (40.0, 40.0), (180.0, 40.0)),
            _line("dim:face-b", (40.0, 44.32), (180.0, 44.32)),
        ),
    )

    model = import_observations(
        _document(page, source_id=f"fixture:dimension-wall-{dimension_text}")
    )

    validate_model(model)
    assert not model.walls
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["dimension_evidence_rejected_count"] == 2
    assert diagnostics["accepted_pair_count"] == 0


def test_regular_hatch_field_is_not_wall_evidence() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("hatch:title", "A55 FLOOR PLAN", 10, 180),
            _text("hatch:scale", "SCALE: 1:100", 10, 166),
            _text("hatch:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=tuple(
            _line(
                f"hatch:{index}",
                (50.0, 40.0 + index * 4.0),
                (90.0, 40.0 + index * 4.0),
            )
            for index in range(6)
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:regular-hatch-field")
    )

    validate_model(model)
    assert not model.walls
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["hatch_evidence_rejected_count"] == 6
    assert diagnostics["accepted_pair_count"] == 0


def test_filled_region_parallel_lines_are_not_wall_evidence() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("filled:title", "A55 FLOOR PLAN", 10, 180),
            _text("filled:scale", "SCALE: 1:100", 10, 166),
            _text("filled:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=(
            _line("filled:line-a", (50.0, 40.0), (90.0, 40.0)),
            _line("filled:line-b", (50.0, 44.0), (90.0, 44.0)),
        ),
        rects=(
            PdfRectObservation(
                element_id="filled:region",
                bbox_pt=(45.0, 35.0, 95.0, 50.0),
                filled=True,
            ),
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:filled-hatch-region")
    )

    validate_model(model)
    assert not model.walls
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["hatch_evidence_rejected_count"] == 2
    assert diagnostics["accepted_pair_count"] == 0


@pytest.mark.parametrize(
    ("gap_inches", "expected_wall_count"),
    ((1.0, 0), (2.0, 1), (18.0, 1), (19.0, 0)),
)
def test_wall_gap_range_is_two_to_eighteen_inches_at_sheet_scale(
    gap_inches: float,
    expected_wall_count: int,
) -> None:
    meters_per_point = 100.0 * 0.0254 / 72.0
    gap_pt = gap_inches * 0.0254 / meters_per_point
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=200,
        texts=(
            _text("gap:title", "A55 FLOOR PLAN", 10, 180),
            _text("gap:scale", "SCALE: 1:100", 10, 166),
            _text("gap:level", "LEVEL: GROUND", 10, 152),
        ),
        lines=(
            _line("gap:face-a", (40.0, 40.0), (160.0, 40.0)),
            _line("gap:face-b", (40.0, 40.0 + gap_pt), (160.0, 40.0 + gap_pt)),
            _line(
                "gap:junction",
                (40.0, 40.0 + gap_pt / 2.0),
                (40.0, 70.0),
            ),
        ),
    )

    model = import_observations(
        _document(page, source_id=f"fixture:wall-gap-{gap_inches:g}in")
    )

    validate_model(model)
    assert len(model.walls) == expected_wall_count
    diagnostics = model.attributes["pdf_architecture"]["pages"][0][
        "geometric_wall_pair_diagnostics"
    ]
    assert diagnostics["wall_gap_range_m"] == pytest.approx([0.0508, 0.4572])
    if expected_wall_count:
        assert model.walls[0].thickness_m == pytest.approx(gap_inches * 0.0254)
    else:
        assert diagnostics["rejected"]["gap_range"] >= 1


def test_scale_known_page_without_registration_cue_uses_sheet_geometry_fallback() -> None:
    assert not REGISTRATION_FALLBACK_FIXTURE.with_suffix(".expected.json").exists()

    observations = extract_pdf(
        REGISTRATION_FALLBACK_FIXTURE,
        source_id="fixture:cad-export-scale-no-registration",
    )
    assert len(observations.pages) == 2
    assert all(page.lines for page in observations.pages)

    model = import_architectural_pdf(
        REGISTRATION_FALLBACK_FIXTURE,
        source_id="fixture:cad-export-scale-no-registration",
    )

    validate_model(model)
    pages = model.attributes["pdf_architecture"]["pages"]
    assert pages[0]["status"] == "geometry_imported"
    fallback = pages[1]
    assert fallback["status"] == "geometry_imported"
    assert fallback["resolved_wall_count"] > 0
    assert fallback["registration_method"] == (
        "sheet geometry largest_closed_wall_loop_bbox lower-left registration fallback"
    )
    assert fallback["registration_confidence"] == pytest.approx(0.40)
    registration = fallback["registration_provenance"]
    assert registration["anchor_basis"] == "largest_closed_wall_loop_bbox"
    assert registration["title_block_excluded"] is True
    assert registration["source_anchor_pt"][0] > 150.0
    assert registration["source_anchor_pt"][1] > 90.0
    assert sum(
        1
        for wall in model.walls
        if wall.provenance and wall.provenance[0].page == 2
    ) > 0
    provenance = [
        item
        for item in model.provenance
        if item.page == 2 and item.method == fallback["registration_method"]
    ]
    assert len(provenance) == 1
    assert provenance[0].confidence == pytest.approx(0.40)
    assert provenance[0].attributes["source_anchor_pt"] == registration["source_anchor_pt"]
    assert not any(
        item["page"] == 2 and item["code"] == "registration_unresolved"
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    )


def test_geometry_plus_text_fixture_labels_geometric_space_by_position() -> None:
    assert not CAD_GEOMETRY_TEXT_FIXTURE.with_suffix(".expected.json").exists()
    observations = extract_pdf(
        CAD_GEOMETRY_TEXT_FIXTURE,
        source_id="fixture:cad-export-geometry-plus-text",
    )
    assert len(observations.pages) == 1
    page = observations.pages[0]
    extracted_text = {item.text for item in page.texts}
    assert {"214", "KEYNOTE: 7", "DRAWING: A45"}.issubset(extracted_text)
    assert any(item.startswith("144") for item in extracted_text)
    room_observation = next(item for item in page.texts if item.text == "214")

    model = import_architectural_pdf(
        CAD_GEOMETRY_TEXT_FIXTURE,
        source_id="fixture:cad-export-geometry-plus-text",
    )

    validate_model(model)
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    space = model.spaces[0]
    assert space.name == "214"
    assert space.usage is None
    assert space.attributes["pdf_architecture"]["recognition"] == (
        "geometric_parallel_wall_closed_loop"
    )
    assert space.attributes["pdf_architecture"]["label_confidence"] == pytest.approx(
        0.88
    )
    assert space.attributes["pdf_architecture"]["label_source_element_id"] == (
        room_observation.element_id
    )
    assert any(
        provenance.source_element_id == room_observation.element_id
        and "text position inside closed wall loop" in provenance.method
        for provenance in space.provenance
    )
    assert "multiple_room_labels_in_enclosure" not in _ambiguity_codes(model)



def test_dense_room_label_fixture_selects_one_label_per_enclosure() -> None:
    assert not DENSE_LABEL_FIXTURE.with_suffix(".expected.json").exists()
    observations = extract_pdf(
        DENSE_LABEL_FIXTURE,
        source_id="fixture:dense-room-labels",
    )
    assert len(observations.pages) == 1
    page = observations.pages[0]
    extracted = {item.text: item for item in page.texts}
    assert extracted["OFFICE"].font_size_pt == pytest.approx(12.0)
    assert extracted["101"].font_size_pt == pytest.approx(11.0)
    assert {"10'-0\"", "KEYNOTE: 1", "8", "TYP"}.issubset(extracted)

    model = import_architectural_pdf(
        DENSE_LABEL_FIXTURE,
        source_id="fixture:dense-room-labels",
    )

    validate_model(model)
    assert len(model.spaces) == 3
    assert len(model.walls) == 12
    assert {space.name for space in model.spaces} == {
        "OFFICE 101",
        "STORAGE A102",
        "CONFERENCE 103",
    }
    assert all(space.name for space in model.spaces)
    assert "multiple_room_labels_in_enclosure" not in _ambiguity_codes(model)

    for space in model.spaces:
        label_provenance = next(
            provenance
            for provenance in space.provenance
            if "label_selection" in provenance.attributes
        )
        selection = label_provenance.attributes["label_selection"]
        assert selection["selection_method"] == "enclosure_room_label_ranking"
        assert selection["room_number_pattern"] is True
        assert len(selection["source_text_elements"]) == 2
        assert [item["text"] for item in selection["runner_ups"]] == ["TYP"]
        assert all(
            item["text"] not in {"10'-0\"", "KEYNOTE: 1", "8", "9", "10"}
            for item in selection["runner_ups"]
        )


def test_adjacent_room_label_pairing_never_crosses_enclosures() -> None:
    assert not ADJACENT_LABEL_PAIR_FIXTURE.with_suffix(".expected.json").exists()
    observations = extract_pdf(
        ADJACENT_LABEL_PAIR_FIXTURE,
        source_id="fixture:adjacent-room-label-pairs",
    )
    assert len(observations.pages) == 1
    page = observations.pages[0]
    extracted = {item.text: item for item in page.texts}
    assert {"OFFICE", "101", "STORAGE", "102"}.issubset(extracted)

    office = extracted["OFFICE"].center_pt
    room_101 = extracted["101"].center_pt
    storage = extracted["STORAGE"].center_pt
    room_102 = extracted["102"].center_pt
    assert abs(storage[1] - room_101[1]) < abs(office[1] - room_101[1])
    assert room_101[0] < 130 < room_102[0]

    model = import_architectural_pdf(
        ADJACENT_LABEL_PAIR_FIXTURE,
        source_id="fixture:adjacent-room-label-pairs",
    )

    validate_model(model)
    assert len(model.spaces) == 2
    assert {space.name for space in model.spaces} == {"OFFICE 101", "STORAGE 102"}
    assert "multiple_room_labels_in_enclosure" not in _ambiguity_codes(model)

    source_text_by_name = {}
    for space in model.spaces:
        source_text_by_name[space.name] = {
            next(
                item.text
                for item in page.texts
                if item.element_id == element_id
            )
            for element_id in space.attributes["pdf_architecture"][
                "label_source_element_ids"
            ]
        }

    assert source_text_by_name == {
        "OFFICE 101": {"OFFICE", "101"},
        "STORAGE 102": {"STORAGE", "102"},
    }


def test_room_label_ranking_marks_only_close_top_two_as_ambiguous() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A52 FLOOR PLAN", 10, 190),
            _text("scale", "SCALE: 1:100", 10, 176),
            _text("level", "LEVEL: GROUND", 10, 162),
            _text("height", "LEVEL CEILING HEIGHT: 9'-0\"", 10, 148),
            _text("office", "OFFICE", 70, 70, width=40, height=10),
            _text("lobby", "LOBBY", 130, 70, width=40, height=10),
        ),
        rects=(
            PdfRectObservation(element_id="outer", bbox_pt=(20, 20, 220, 120)),
            PdfRectObservation(element_id="inner", bbox_pt=(24, 24, 216, 116)),
        ),
    )

    model = import_observations(_document(page, source_id="fixture:close-labels"))

    validate_model(model)
    assert len(model.spaces) == 1
    ambiguity = next(
        item
        for item in model.attributes["pdf_architecture"]["ambiguities"]
        if item["code"] == "multiple_room_labels_in_enclosure"
    )
    assert ambiguity["ranking_margin"] <= ambiguity["ambiguity_margin"]
    assert model.spaces[0].confidence <= 0.65
    selection = model.spaces[0].provenance[0].attributes["label_selection"]
    assert len(selection["runner_ups"]) == 1
    assert {model.spaces[0].name, selection["runner_ups"][0]["text"]} == {
        "OFFICE",
        "LOBBY",
    }


def test_geometric_space_identity_does_not_depend_on_room_label() -> None:
    source_id = "fixture:cad-room-label-identity"
    geometry_only = extract_pdf(CAD_GEOMETRY_FIXTURE, source_id=source_id)
    page = replace(
        geometry_only.pages[0],
        texts=(
            _text("identity:title", "A45 FLOOR PLAN", 8, 160),
            _text("identity:scale", "SCALE: 1:100", 8, 148),
            _text("identity:level", "LEVEL: GROUND", 8, 136),
        ),
    )
    unlabeled = import_observations(replace(geometry_only, pages=(page,)))
    labeled = import_architectural_pdf(
        CAD_GEOMETRY_TEXT_FIXTURE,
        source_id=source_id,
    )

    validate_model(unlabeled)
    validate_model(labeled)
    assert len(unlabeled.spaces) == len(labeled.spaces) == 1
    assert unlabeled.spaces[0].name is None
    assert labeled.spaces[0].name == "214"
    assert unlabeled.spaces[0].id == labeled.spaces[0].id
    assert {wall.id for wall in unlabeled.walls} == {wall.id for wall in labeled.walls}


@pytest.mark.parametrize("label", ("214", "ELEC"))
def test_room_labels_do_not_require_vocabulary_or_prefix(label: str) -> None:
    page = _ordinary_vector_fixture_page()
    texts = tuple(
        replace(item, text=label) if item.element_id == "ov:room" else item
        for item in page.texts
    )
    page = replace(
        page,
        texts=(
            *texts,
            _text("ov:dimension", "144″", 95, 48),
            _text("ov:prefixed-dimension", "ROOM: 144″", 95, 108),
            _text("ov:keynote", "KEYNOTE: 7", 95, 82),
            _text("ov:title-block", "DRAWING: A2.1", 95, 96),
        ),
    )

    model = import_observations(
        _document(page, source_id=f"fixture:room-label-position:{label.lower()}")
    )

    validate_model(model)
    assert len(model.spaces) == 1
    assert model.spaces[0].name == label
    assert model.spaces[0].usage is None
    assert model.spaces[0].attributes["pdf_architecture"][
        "label_confidence"
    ] == pytest.approx(0.88)
    assert "multiple_room_labels_in_enclosure" not in _ambiguity_codes(model)


def test_cad_derived_untagged_wall_faces_emit_closed_space_with_low_confidence_height() -> None:
    page = _cad_derived_wall_face_page()
    assert page.lines
    assert all(line.native_id is None for line in page.lines)

    model = import_observations(
        _document(page, source_id="fixture:cad-derived-wall-faces")
    )

    validate_model(model)
    assert len(model.levels) == 1
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    assert model.spaces[0].name is None
    assert model.spaces[0].attributes["pdf_architecture"]["recognition"] == (
        "geometric_parallel_wall_closed_loop"
    )
    assert model.levels[0].height_m == pytest.approx(2.7432)
    assert model.levels[0].confidence == pytest.approx(0.45)
    assert all(wall.height_m == pytest.approx(2.7432) for wall in model.walls)
    assert all(wall.confidence <= 0.45 for wall in model.walls)
    assert model.spaces[0].confidence <= 0.45
    assert all(
        wall.attributes["pdf_architecture"]["recognition"]
        == "geometric_parallel_wall_faces"
        for wall in model.walls
    )
    assert "level_height_default_assumed" in _ambiguity_codes(model)
    page_meta = model.attributes["pdf_architecture"]["pages"][0]
    assert page_meta["status"] == "geometry_imported"
    assert page_meta["resolved_wall_count"] == 4
    assert page_meta["resolved_room_count"] == 1
    assert page_meta["geometric_wall_loop_count"] == 1


def test_cad_derived_wall_ids_and_serialization_are_order_independent() -> None:
    page = _cad_derived_wall_face_page()
    baseline = import_observations(
        _document(page, source_id="fixture:cad-derived-wall-faces")
    )
    reordered = import_observations(
        _document(
            replace(page, lines=tuple(reversed(page.lines))),
            source_id="fixture:cad-derived-wall-faces",
        )
    )

    validate_model(baseline)
    validate_model(reordered)
    assert baseline.to_json() == reordered.to_json()
    assert {wall.id for wall in baseline.walls} == {wall.id for wall in reordered.walls}
    assert {space.id for space in baseline.spaces} == {space.id for space in reordered.spaces}


def test_cad_derived_geometric_pairing_fails_closed_on_ambiguous_parallel_face() -> None:
    page = _cad_derived_wall_face_page()
    ambiguous = _line("cad:south-face-c", (40.0, 48.0), (240.0, 48.0))
    page = replace(page, lines=(*page.lines, ambiguous))

    model = import_observations(
        _document(page, source_id="fixture:cad-derived-wall-faces-ambiguous")
    )

    validate_model(model)
    assert not model.walls
    assert not model.spaces
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == (
        "no_supported_geometry_recognized"
    )


def test_cad_derived_wall_geometry_uses_explicit_level_height_when_present() -> None:
    page = _cad_derived_wall_face_page()
    page = replace(
        page,
        texts=(
            *page.texts,
            _text("cad:height", "LEVEL CEILING HEIGHT: 10'-0\"", 10, 148),
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:cad-derived-wall-faces-explicit-height")
    )

    validate_model(model)
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    assert model.levels[0].height_m == pytest.approx(3.048)
    assert all(wall.height_m == pytest.approx(3.048) for wall in model.walls)
    assert "level_height_default_assumed" not in _ambiguity_codes(model)


def test_pdf_text_extraction_splits_widely_separated_same_row_annotations() -> None:
    class FakePage:
        height = 200.0

        @staticmethod
        def extract_words(**_kwargs):
            return [
                {"text": "BEDROOM", "x0": 10.0, "x1": 55.0, "top": 20.0, "bottom": 30.0},
                {"text": "1", "x0": 58.0, "x1": 63.0, "top": 20.0, "bottom": 30.0},
                {"text": "D-12", "x0": 140.0, "x1": 165.0, "top": 20.0, "bottom": 30.0},
            ]

    observations = _group_words(FakePage(), 1)

    assert [item.text for item in observations] == ["BEDROOM 1", "D-12"]
    bedroom = observations[0]

    class WithUnrelatedFarWord(FakePage):
        @staticmethod
        def extract_words(**_kwargs):
            return FakePage.extract_words() + [
                {"text": "A-5.2", "x0": 220.0, "x1": 250.0, "top": 20.0, "bottom": 30.0},
            ]

    edited = _group_words(WithUnrelatedFarWord(), 1)
    assert edited[0].text == "BEDROOM 1"
    assert edited[0].element_id == bedroom.element_id

def test_synthetic_pdf_matches_known_answer_and_canonical_contract() -> None:
    pdf_path = FIXTURE_DIR / "simple-floor-plan.pdf"
    expected = json.loads((FIXTURE_DIR / "simple-floor-plan.expected.json").read_text(encoding="utf-8"))

    observations = extract_pdf(pdf_path, source_id="fixture:simple-floor-plan")
    assert len(observations.pages) == 1
    assert classify_page(observations.pages[0]).kind == "architectural_plan"

    model = import_architectural_pdf(pdf_path, source_id="fixture:simple-floor-plan")
    validate_model(model)
    assert model.to_dict() == expected
    assert len(model.levels) == 1
    assert len(model.spaces) == 1
    assert len(model.walls) == 4
    assert len(model.slabs) == 1
    assert len(model.ceilings) == 1
    assert len(model.openings) == 1
    assert not model.electrical_devices
    assert not model.electrical_equipment
    assert model.openings[0].host_id in {wall.id for wall in model.walls}
    assert model.spaces[0].name == "GARAGE"
    assert model.levels[0].height_m is None
    assert model.spaces[0].height_m == pytest.approx(2.7432)
    assert model.spaces[0].confidence == pytest.approx(0.9)
    assert {round(wall.thickness_m, 3) for wall in model.walls} == {0.14}


def test_repeatability_and_stable_semantic_identity_survive_geometry_change() -> None:
    first = import_observations(_document(_plan_page()), options=ImportOptions())
    second = import_observations(_document(_plan_page()), options=ImportOptions())
    moved = import_observations(
        _document(_plan_page(dx=12.0), digest="b" * 64),
        options=ImportOptions(),
    )

    assert first.to_json() == second.to_json()
    assert {item.id for item in first.levels} == {item.id for item in moved.levels}
    assert {item.id for item in first.spaces} == {item.id for item in moved.spaces}
    assert {item.id for item in first.walls} == {item.id for item in moved.walls}
    assert first.spaces[0].footprint != moved.spaces[0].footprint


def test_single_ordinary_vector_loop_promotes_only_a_2d_space() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 180),
            _text("scale", "SCALE: 1:100", 10, 165),
            _text("room", "ROOM: GARAGE", 80, 70),
        ),
        lines=_loop_lines("single", (40.0, 35.0, 220.0, 125.0)),
    )

    model = import_observations(_document(page))

    assert len(model.spaces) == 1
    assert model.spaces[0].name == "GARAGE"
    assert not model.walls
    assert model.spaces[0].attributes["pdf_architecture"]["recognition"] == (
        "ordinary_vector_single_loop_space"
    )
    assert "wall_thickness_unresolved" in _ambiguity_codes(model)


@pytest.mark.parametrize(
    "partial_lines",
    (
        (
            _line("partial:south", (45.0, 39.0), (215.0, 39.0)),
        ),
        (
            _line("partial:south", (45.0, 39.0), (215.0, 39.0)),
            _line("partial:west", (44.0, 40.0), (44.0, 120.0)),
        ),
    ),
    ids=("one-sided-partial-pair", "two-sided-partial-pair"),
)
def test_single_ordinary_vector_loop_with_partial_paired_wall_evidence_fails_closed(
    partial_lines: tuple[PdfLineObservation, ...],
) -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 180),
            _text("scale", "SCALE: 1:100", 10, 165),
            _text("room", "ROOM: GARAGE", 80, 70),
        ),
        lines=(
            *_loop_lines("single", (40.0, 35.0, 220.0, 125.0)),
            *partial_lines,
        ),
    )

    model = import_observations(_document(page))

    validate_model(model)
    assert not model.spaces
    assert not model.walls
    assert not model.slabs
    assert not model.ceilings
    assert "ordinary_vector_enclosure_unresolved" in _ambiguity_codes(model)
    assert "architectural_geometry_unrecognized" in _ambiguity_codes(model)


@pytest.mark.parametrize(
    ("height_text", "default_wall_height_m"),
    (
        ("LEVEL CEILING HEIGHT: 9'-0\"", None),
        (None, 2.4),
    ),
    ids=("explicit-level-height", "default-wall-height"),
)
def test_single_ordinary_vector_loop_remains_2d_with_height_and_slab_inputs(
    height_text: str | None,
    default_wall_height_m: float | None,
) -> None:
    texts = [
        _text("title", "A1.1 FLOOR PLAN", 10, 180),
        _text("scale", "SCALE: 1:100", 10, 165),
        _text("room", "ROOM: GARAGE", 80, 70),
        _text("slab", "FLOOR SLAB THICKNESS: 4\"", 10, 145),
    ]
    if height_text is not None:
        texts.append(_text("height", height_text, 10, 130))

    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=tuple(texts),
        lines=_loop_lines("single", (40.0, 35.0, 220.0, 125.0)),
    )

    model = import_observations(
        _document(page),
        options=ImportOptions(default_wall_height_m=default_wall_height_m),
    )

    validate_model(model)
    assert len(model.spaces) == 1
    assert model.spaces[0].height_m is None
    assert not model.walls
    assert not model.slabs
    assert not model.ceilings
    assert model.spaces[0].attributes["pdf_architecture"]["recognition"] == (
        "ordinary_vector_single_loop_space"
    )
    assert "wall_thickness_unresolved" in _ambiguity_codes(model)


def test_competing_single_ordinary_vector_loops_stay_unresolved() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 180),
            _text("scale", "SCALE: 1:100", 10, 165),
            _text("room", "ROOM: GARAGE", 100, 80),
        ),
        lines=(
            *_loop_lines("outer", (30.0, 25.0, 240.0, 145.0)),
            *_loop_lines("inner", (45.0, 40.0, 225.0, 130.0)),
        ),
    )

    model = import_observations(_document(page))

    assert not model.spaces
    assert "ordinary_vector_enclosure_unresolved" in _ambiguity_codes(model)


def test_conflicting_scale_is_preserved_as_ambiguity_not_fabricated_geometry() -> None:
    page = _plan_page(scales=("SCALE: 1/4\" = 1'-0\"", "SCALE: 1/8\" = 1'-0\""))
    model = import_observations(_document(page))

    assert len(model.levels) == 1
    assert not model.spaces
    assert not model.walls
    assert "scale_conflict" in _ambiguity_codes(model)
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == "skipped_unresolved_scale_or_registration"


def test_missing_height_keeps_space_but_does_not_invent_3d_walls_or_ceiling() -> None:
    model = import_observations(_document(_plan_page(include_height=False)))

    assert len(model.spaces) == 1
    assert model.spaces[0].height_m is None
    assert not model.walls
    assert not model.ceilings
    assert "wall_height_unresolved" in _ambiguity_codes(model)


def test_two_point_registration_controls_scale_rotation_and_translation() -> None:
    page = _plan_page()
    hint = RegistrationHint(
        page_number=1,
        source_a_pt=(0.0, 0.0),
        source_b_pt=(72.0, 0.0),
        model_a_m=(10.0, 20.0),
        model_b_m=(10.0, 22.54),
    )
    model = import_observations(_document(page), options=ImportOptions(registrations=(hint,)))

    assert len(model.spaces) == 1
    first_point = model.spaces[0].footprint.points[0]
    one_hundred_scale = 100 * 0.0254 / 72
    assert first_point.x == pytest.approx(10.0 - 24.0 * one_hundred_scale, abs=1e-9)
    assert first_point.y == pytest.approx(20.0 + 24.0 * one_hundred_scale, abs=1e-9)
    page_meta = model.attributes["pdf_architecture"]["pages"][0]
    assert page_meta["registration_rotation_radians"] == pytest.approx(math.pi / 2)
    assert page_meta["scale_meters_per_point"] == pytest.approx(one_hundred_scale)


def test_empty_architectural_page_does_not_claim_geometry_or_own_shared_frame() -> None:
    empty = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("p1:title", "A1.0 FLOOR PLAN", 10, 180),
            _text("p1:scale", "SCALE: 1:100", 10, 165),
            _text("p1:level", "LEVEL: GROUND", 10, 140),
            _text("p1:elev", "ELEVATION: 0'-0\"", 10, 128),
        ),
    )
    second = _plan_page(page_number=2, room_name="STORAGE")

    model = import_observations(_document(empty, second))

    assert len(model.spaces) == 1
    pages = model.attributes["pdf_architecture"]["pages"]
    assert pages[0]["status"] == "no_supported_geometry_recognized"
    assert pages[1]["status"] == "geometry_imported"
    assert "architectural_geometry_unrecognized" in _ambiguity_codes(model)
    assert "registration_unresolved" not in _ambiguity_codes(model)


def test_additional_plan_page_requires_explicit_registration() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE")
    second = _plan_page(page_number=2, room_name="STORAGE", dx=20)
    document = _document(first, second)

    unresolved = import_observations(document)
    assert len(unresolved.spaces) == 1
    assert "registration_unresolved" in _ambiguity_codes(unresolved)

    mpp = 100 * 0.0254 / 72
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(8, 0),
        model_b_m=(8 + 72 * mpp, 0),
    )
    resolved = import_observations(document, options=ImportOptions(registrations=(hint,)))
    assert {space.name for space in resolved.spaces} == {"OFFICE", "STORAGE"}
    assert "registration_unresolved" not in _ambiguity_codes(resolved)


def test_electrical_sheet_is_classified_but_never_interpreted_by_architecture_lane() -> None:
    architectural = _plan_page(page_number=1)
    electrical = PdfPageObservation(
        page_number=2,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("e-title", "E1.1 ELECTRICAL POWER PLAN", 10, 180),
            _text("e-panel", "PANEL SCHEDULE LP", 10, 160),
        ),
    )
    model = import_observations(_document(architectural, electrical))

    page_meta = model.attributes["pdf_architecture"]["pages"]
    assert page_meta[1]["classification"] == "electrical"
    assert page_meta[1]["status"] == "ignored_non_architectural_plan"
    assert not model.electrical_devices
    assert not model.electrical_equipment


def test_registration_scale_conflict_is_explicit_and_blocks_page_geometry() -> None:
    page = _plan_page()
    # Printed scale is 1:100, but these controls imply 1:50.
    hint = RegistrationHint(
        page_number=1,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(0, 0),
        model_b_m=(1.27, 0),
    )
    model = import_observations(_document(page), options=ImportOptions(registrations=(hint,)))

    assert not model.spaces
    assert "scale_registration_conflict" in _ambiguity_codes(model)


def test_not_to_scale_sheet_can_be_resolved_only_with_explicit_scale_override() -> None:
    from oabm.importers.pdf_architecture import ScaleOverride

    page = _plan_page(scales=("NOT TO SCALE",))
    unresolved = import_observations(_document(page))
    assert not unresolved.spaces
    assert "scale_unresolved" in _ambiguity_codes(unresolved)

    one_hundred_scale = 100 * 0.0254 / 72
    resolved = import_observations(
        _document(page),
        options=ImportOptions(
            scale_overrides=(
                ScaleOverride(page_number=1, meters_per_point=one_hundred_scale),
            )
        ),
    )
    assert len(resolved.spaces) == 1
    assert resolved.attributes["pdf_architecture"]["pages"][0]["scale_method"] == "explicit scale override"


def test_repeated_room_identity_on_registered_page_is_explicitly_not_replaced() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE")
    second = _plan_page(page_number=2, room_name="OFFICE", dx=20)
    mpp = 100 * 0.0254 / 72
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(8, 0),
        model_b_m=(8 + 72 * mpp, 0),
    )
    model = import_observations(
        _document(first, second),
        options=ImportOptions(registrations=(hint,)),
    )

    assert len(model.spaces) == 1
    assert len(model.walls) == 4
    assert "duplicate_room_identity_across_pages" in _ambiguity_codes(model)
    validate_model(model)



def test_later_explicit_elevation_upgrades_local_datum_and_prior_geometry() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE")
    first = replace(
        first,
        texts=tuple(item for item in first.texts if "ELEVATION" not in item.text.upper()),
    )
    second = _plan_page(page_number=2, room_name="STORAGE", dx=20)
    second = replace(
        second,
        texts=tuple(
            replace(item, text="ELEVATION: 10'-0\"") if item.element_id == "p2:elev" else item
            for item in second.texts
        ),
    )
    mpp = 100 * 0.0254 / 72
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(8, 0),
        model_b_m=(8 + 72 * mpp, 0),
    )

    model = import_observations(
        _document(first, second),
        options=ImportOptions(registrations=(hint,)),
    )

    level = model.levels[0]
    assert level.elevation_m == pytest.approx(3.048)
    assert "level_elevation_reconciled" in _ambiguity_codes(model)
    assert [
        provenance.page
        for provenance in level.provenance
        if provenance.attributes.get("field") == "elevation_m"
    ] == [2]
    elevation_provenance = next(
        provenance
        for provenance in level.provenance
        if provenance.attributes.get("field") == "elevation_m"
    )
    assert elevation_provenance.source_element_id == "p2:elev"
    assert elevation_provenance.attributes["source_text"] == "ELEVATION: 10'-0\""
    assert {space.name for space in model.spaces} == {"OFFICE", "STORAGE"}
    assert all(
        point.z == pytest.approx(3.048)
        for space in model.spaces
        for point in space.footprint.points
    )
    validate_model(model)


def test_later_explicit_level_height_upgrades_default_and_prior_geometry() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE", include_height=False)
    second = _plan_page(page_number=2, room_name="STORAGE", dx=20)
    second = replace(
        second,
        texts=tuple(
            replace(item, text="LEVEL CEILING HEIGHT: 10'-0\"")
            if item.element_id == "p2:height"
            else item
            for item in second.texts
        ),
    )
    mpp = 100 * 0.0254 / 72
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(8, 0),
        model_b_m=(8 + 72 * mpp, 0),
    )

    model = import_observations(
        _document(first, second),
        options=ImportOptions(
            registrations=(hint,),
            default_wall_height_m=2.4,
        ),
    )

    level = model.levels[0]
    assert level.height_m == pytest.approx(3.048)
    assert "level_height_reconciled" in _ambiguity_codes(model)
    height_provenance = next(
        provenance
        for provenance in level.provenance
        if provenance.attributes.get("field") == "height_m"
    )
    assert height_provenance.page == 2
    assert height_provenance.source_element_id == "p2:height"
    assert height_provenance.attributes["source_text"] == "LEVEL CEILING HEIGHT: 10'-0\""
    assert len(model.walls) == 8
    assert all(wall.height_m == pytest.approx(3.048) for wall in model.walls)
    assert all(space.height_m == pytest.approx(3.048) for space in model.spaces)
    validate_model(model)


def test_conflicting_explicit_level_elevations_are_not_silently_collapsed() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE")
    second = _plan_page(page_number=2, room_name="STORAGE", dx=20)
    second = replace(
        second,
        texts=tuple(
            replace(item, text="ELEVATION: 10'-0\"") if item.element_id == "p2:elev" else item
            for item in second.texts
        ),
    )

    model = import_observations(_document(first, second))

    assert model.levels[0].elevation_m == pytest.approx(0.0)
    assert model.levels[0].confidence == pytest.approx(0.5)
    assert "level_elevation_conflict" in _ambiguity_codes(model)
    conflict = next(
        item
        for item in model.attributes["pdf_architecture"]["ambiguities"]
        if item["code"] == "level_elevation_conflict"
    )
    assert conflict["existing_page"] == 1
    assert conflict["conflicting_page"] == 2
    assert conflict["existing_value_m"] == pytest.approx(0.0)
    assert conflict["conflicting_value_m"] == pytest.approx(3.048)
    assert conflict["existing_source_text"] == "ELEVATION: 0'-0\""
    assert conflict["conflicting_source_text"] == "ELEVATION: 10'-0\""
    assert conflict["existing_source_element_id"] == "p1:elev"
    assert conflict["conflicting_source_element_id"] == "p2:elev"
    assert {space.name for space in model.spaces} == {"OFFICE"}
    assert model.attributes["pdf_architecture"]["pages"][1]["status"] == "skipped_unresolved_level"
    validate_model(model)


def test_level_override_updates_already_seen_level_and_all_geometry() -> None:
    first = _plan_page(page_number=1, room_name="OFFICE")
    second = _plan_page(page_number=2, room_name="STORAGE", dx=20)
    mpp = 100 * 0.0254 / 72
    hint = RegistrationHint(
        page_number=2,
        source_a_pt=(0, 0),
        source_b_pt=(72, 0),
        model_a_m=(8, 0),
        model_b_m=(8 + 72 * mpp, 0),
    )
    override = LevelOverride(
        page_number=2,
        elevation_m=3.0,
        height_m=3.2,
        note="review regression override",
    )

    model = import_observations(
        _document(first, second),
        options=ImportOptions(
            registrations=(hint,),
            level_overrides=(override,),
        ),
    )

    level = model.levels[0]
    assert level.elevation_m == pytest.approx(3.0)
    assert level.height_m == pytest.approx(3.2)
    assert "level_elevation_reconciled" in _ambiguity_codes(model)
    assert [provenance.page for provenance in level.provenance] == [2, 2]
    assert {
        provenance.attributes.get("field")
        for provenance in level.provenance
    } == {"elevation_m", "height_m"}
    assert all(
        point.z == pytest.approx(3.0)
        for space in model.spaces
        for point in space.footprint.points
    )
    assert all(wall.height_m == pytest.approx(3.2) for wall in model.walls)
    assert {
        round(ceiling.footprint.points[0].z, 6)
        for ceiling in model.ceilings
    } == {6.2}
    validate_model(model)


def test_room_specific_ceiling_heights_stay_scoped_to_their_rooms() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 180),
            _text("scale", "SCALE: 1:100", 10, 165),
            _text("level", "LEVEL: GROUND", 10, 150),
            _text("elev", "ELEVATION: 0'-0\"", 10, 136),
            _text("office-room", "ROOM: OFFICE", 40, 60),
            _text("office-height", "CEILING HEIGHT: 9'-0\"", 45, 90),
            _text("lobby-room", "ROOM: LOBBY", 170, 60),
            _text("lobby-height", "CEILING HEIGHT: 12'-0\"", 175, 90),
        ),
        rects=(
            PdfRectObservation(element_id="office-outer", bbox_pt=(20, 20, 130, 120)),
            PdfRectObservation(element_id="office-inner", bbox_pt=(24, 24, 126, 116)),
            PdfRectObservation(element_id="lobby-outer", bbox_pt=(150, 20, 260, 120)),
            PdfRectObservation(element_id="lobby-inner", bbox_pt=(154, 24, 256, 116)),
        ),
    )

    model = import_observations(_document(page))

    assert model.levels[0].height_m is None
    spaces = {space.name: space for space in model.spaces}
    assert spaces["OFFICE"].height_m == pytest.approx(2.7432)
    assert spaces["LOBBY"].height_m == pytest.approx(3.6576)
    assert spaces["OFFICE"].confidence == pytest.approx(0.9)
    assert spaces["LOBBY"].confidence == pytest.approx(0.9)
    assert any(
        provenance.source_element_id == "office-height"
        and "room-scoped" in provenance.method
        for provenance in spaces["OFFICE"].provenance
    )
    assert any(
        provenance.source_element_id == "lobby-height"
        and "room-scoped" in provenance.method
        for provenance in spaces["LOBBY"].provenance
    )

    wall_heights = {
        anchor: {wall.height_m for wall in model.walls if wall.attributes["pdf_architecture"]["room_anchor"] == anchor}
        for anchor in ("office", "lobby")
    }
    assert sorted(wall_heights["office"]) == pytest.approx([2.7432])
    assert sorted(wall_heights["lobby"]) == pytest.approx([3.6576])
    assert sorted(
        ceiling.footprint.points[0].z
        for ceiling in model.ceilings
    ) == pytest.approx([2.7432, 3.6576])
    assert sorted({wall.confidence for wall in model.walls}) == pytest.approx([0.9])
    assert sorted({ceiling.confidence for ceiling in model.ceilings}) == pytest.approx([0.85])
    assert {
        provenance.source_element_id
        for ceiling in model.ceilings
        for provenance in ceiling.provenance
        if provenance.attributes.get("scope") == "room"
    } == {"office-height", "lobby-height"}
    assert "level_height_conflict" not in _ambiguity_codes(model)
    assert "ceiling_height_scope_unresolved" not in _ambiguity_codes(model)
    validate_model(model)



def test_conflicting_room_heights_do_not_fall_back_to_global_level_height() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=300,
        height_pt=220,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 190),
            _text("scale", "SCALE: 1:100", 10, 176),
            _text("level", "LEVEL: GROUND", 10, 162),
            _text("elev", "ELEVATION: 0'-0\"", 10, 148),
            _text("global-height", "LEVEL CEILING HEIGHT: 11'-0\"", 10, 134),
            _text("office-room", "ROOM: OFFICE", 70, 55),
            _text("office-height-a", "CEILING HEIGHT: 9'-0\"", 70, 80),
            _text("office-height-b", "CEILING HEIGHT: 10'-0\"", 70, 95),
        ),
        rects=(
            PdfRectObservation(element_id="office-outer", bbox_pt=(20, 20, 220, 120)),
            PdfRectObservation(element_id="office-inner", bbox_pt=(24, 24, 216, 116)),
        ),
    )

    model = import_observations(_document(page))

    assert model.levels[0].height_m == pytest.approx(3.3528)
    assert len(model.spaces) == 1
    assert model.spaces[0].height_m is None
    assert not model.walls
    assert not model.ceilings
    assert "room_ceiling_height_conflict" in _ambiguity_codes(model)
    assert "wall_height_unresolved" in _ambiguity_codes(model)
    validate_model(model)


def test_unqualified_height_outside_rooms_is_not_promoted_to_level() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=320,
        height_pt=240,
        texts=(
            _text("title", "A1.1 FLOOR PLAN", 10, 205),
            _text("scale", "SCALE: 1:100", 10, 190),
            _text("level", "LEVEL: GROUND", 10, 175),
            _text("elev", "ELEVATION: 0'-0\"", 10, 160),
            _text("unqualified-height", "CEILING HEIGHT: 10'-0\"", 10, 145),
            _text("office-room", "ROOM: OFFICE", 45, 60),
            _text("lobby-room", "ROOM: LOBBY", 185, 60),
        ),
        rects=(
            PdfRectObservation(element_id="office-outer", bbox_pt=(20, 20, 130, 120)),
            PdfRectObservation(element_id="office-inner", bbox_pt=(24, 24, 126, 116)),
            PdfRectObservation(element_id="lobby-outer", bbox_pt=(160, 20, 270, 120)),
            PdfRectObservation(element_id="lobby-inner", bbox_pt=(164, 24, 266, 116)),
        ),
    )

    model = import_observations(_document(page))

    assert model.levels[0].height_m is None
    assert {space.name for space in model.spaces} == {"OFFICE", "LOBBY"}
    assert all(space.height_m is None for space in model.spaces)
    assert not model.walls
    assert not model.ceilings
    assert "ceiling_height_scope_unresolved" in _ambiguity_codes(model)
    validate_model(model)


def test_ordinary_vector_line_loops_emit_canonical_space_and_walls() -> None:
    page = _ordinary_vector_fixture_page()
    model = import_observations(
        _document(page, source_id="fixture:ordinary-vector-room")
    )

    validate_model(model)
    assert len(model.levels) == 1
    assert len(model.spaces) == 1
    assert len(model.walls) == 4
    assert len(model.ceilings) == 1
    assert not model.slabs
    assert model.spaces[0].name == "OFFICE"
    assert model.spaces[0].attributes["pdf_architecture"]["recognition"] == "ordinary_vector_line_loops"
    assert model.spaces[0].confidence == pytest.approx(0.86)
    assert all(wall.confidence == pytest.approx(0.86) for wall in model.walls)
    assert all(
        wall.attributes["pdf_architecture"]["recognition"] == "ordinary_vector_line_loops"
        for wall in model.walls
    )

    meters_per_point = 100 * 0.0254 / 72
    footprint = model.spaces[0].footprint.points
    expected_points = (
        (24 * meters_per_point, 24 * meters_per_point, 0.0),
        (216 * meters_per_point, 24 * meters_per_point, 0.0),
        (216 * meters_per_point, 116 * meters_per_point, 0.0),
        (24 * meters_per_point, 116 * meters_per_point, 0.0),
    )
    for point, expected in zip(footprint, expected_points, strict=True):
        assert point.x == pytest.approx(expected[0])
        assert point.y == pytest.approx(expected[1])
        assert point.z == pytest.approx(expected[2])
    assert all(
        wall.thickness_m == pytest.approx(4 * meters_per_point)
        for wall in model.walls
    )
    assert all(wall.height_m == pytest.approx(2.7432) for wall in model.walls)
    assert model.coordinate_system.length_unit == "m"

    space_provenance = model.spaces[0].provenance[0]
    assert "ordinary vector" in space_provenance.method
    assert space_provenance.source_element_id == "ov:room"
    assert space_provenance.attributes["outer_boundary_elements"] == [
        "ov:outer-east",
        "ov:outer-north",
        "ov:outer-south",
        "ov:outer-west",
    ]
    assert space_provenance.attributes["inner_boundary_elements"] == [
        "ov:inner-east",
        "ov:inner-north",
        "ov:inner-south",
        "ov:inner-west",
    ]
    page_meta = model.attributes["pdf_architecture"]["pages"][0]
    assert page_meta["status"] == "geometry_imported"
    assert page_meta["ordinary_vector_enclosure_count"] == 1
    assert "architectural_geometry_unrecognized" not in _ambiguity_codes(model)


def test_ordinary_vector_ids_and_output_do_not_depend_on_extraction_order() -> None:
    page = _ordinary_vector_fixture_page()
    baseline = import_observations(
        _document(page, source_id="fixture:ordinary-vector-room")
    )
    reordered = import_observations(
        _document(
            replace(page, lines=tuple(reversed(page.lines))),
            source_id="fixture:ordinary-vector-room",
        )
    )
    assert baseline.to_json() == reordered.to_json()

    unrelated = _line("ov:unrelated", (245, 80), (270, 80))
    edited = import_observations(
        _document(
            replace(page, lines=(unrelated, *page.lines)),
            source_id="fixture:ordinary-vector-room",
            digest="b" * 64,
        )
    )
    assert {item.id for item in baseline.spaces} == {item.id for item in edited.spaces}
    assert {item.id for item in baseline.walls} == {item.id for item in edited.walls}
    assert {item.id for item in baseline.ceilings} == {item.id for item in edited.ceilings}
    validate_model(edited)


def test_open_ordinary_vector_enclosure_fails_closed() -> None:
    page = _ordinary_vector_fixture_page()
    page = replace(
        page,
        lines=tuple(
            line
            for line in page.lines
            if line.element_id != "ov:inner-north"
        ),
    )

    model = import_observations(
        _document(page, source_id="fixture:ordinary-vector-open")
    )

    validate_model(model)
    assert not model.spaces
    assert not model.walls
    assert not model.ceilings
    assert "ordinary_vector_enclosure_unresolved" in _ambiguity_codes(model)
    assert "architectural_geometry_unrecognized" in _ambiguity_codes(model)
    assert (
        model.attributes["pdf_architecture"]["pages"][0]["status"]
        == "no_supported_geometry_recognized"
    )


def test_multiple_ordinary_vector_enclosures_are_explicitly_ambiguous() -> None:
    page = _ordinary_vector_fixture_page()
    second_shell = (
        *_loop_lines("ov:outer-2", (40, 35, 200, 105)),
        *_loop_lines("ov:inner-2", (44, 39, 196, 101)),
    )
    page = replace(page, lines=(*page.lines, *second_shell))

    model = import_observations(
        _document(page, source_id="fixture:ordinary-vector-ambiguous")
    )

    validate_model(model)
    assert not model.spaces
    assert not model.walls
    assert not model.ceilings
    assert "ordinary_vector_enclosure_ambiguous" in _ambiguity_codes(model)
    ambiguity = next(
        item
        for item in model.attributes["pdf_architecture"]["ambiguities"]
        if item["code"] == "ordinary_vector_enclosure_ambiguous"
    )
    assert ambiguity["room_anchor"] == "office"
    assert len(ambiguity["source_boundaries"]) == 2
    assert (
        model.attributes["pdf_architecture"]["pages"][0]["status"]
        == "no_supported_geometry_recognized"
    )
