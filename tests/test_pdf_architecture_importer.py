import json
import math
from pathlib import Path

import pytest

from oabm.model import BuildingModel, validate_model
from oabm.importers.pdf_architecture import ImportOptions, RegistrationHint
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import classify_page, import_architectural_pdf, import_observations
from oabm.importers.pdf_architecture.types import (
    PdfDocumentObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "pdf_architecture" / "v1"


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


def _ambiguity_codes(model: BuildingModel) -> set[str]:
    return {item["code"] for item in model.attributes["pdf_architecture"]["ambiguities"]}


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
    assert model.spaces[0].height_m == pytest.approx(2.7432)
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
