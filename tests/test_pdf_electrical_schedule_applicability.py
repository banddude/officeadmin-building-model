"""Authored multi-sheet observations and PDF sources with independent expectations."""
import re
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from oabm.importers.pdf_electrical.importer import (
    PdfTextObservation, _detect_lighting_fixture_schedules, _normalize_lighting_tag,
    ElectricalPdfImporter, extract_pdf,
)
from oabm.importers.pdf_electrical.schedule_applicability import resolve_schedule_associations
from oabm.model import validate_model
from test_pdf_electrical import _write_generated_lighting_pdf


def _text(page, name, text, x=50, y=700):
    return PdfTextObservation(f"p{page}:{name}", page, text, x, y)


def _case(*, titles=("LIGHTING FIXTURE SCHEDULE",), descriptions=None,
          field_title="FIRST FLOOR LIGHTING PLAN", references=()):
    descriptions = descriptions or ["WALL LIGHT"] * len(titles)
    texts = [_text(1, "title", field_title), _text(1, "field", "A", 100, 100)]
    for i, reference in enumerate(references):
        texts.append(_text(1, f"reference{i}", reference, 50, 50-i*12))
    for page, (title, description) in enumerate(zip(titles, descriptions), 2):
        texts += [_text(page, "heading", title, 50, 700),
                  _text(page, "header-tag", "TAG", 50, 680),
                  _text(page, "header-description", "DESCRIPTION", 150, 680),
                  _text(page, "tag", "A", 50, 660),
                  _text(page, "description", description, 150, 660),
                  _text(page, "sheet", f"E9.{page-1}", 500, 30)]
    return texts


def _resolve(texts):
    rows, claimed, problems, metadata = _detect_lighting_fixture_schedules(texts)
    return resolve_schedule_associations(texts=texts, schedules=rows, metadata=metadata,
        problems=problems, excluded_text_ids=claimed, normalize_tag=_normalize_lighting_tag)


def test_document_unique_exact_tag_has_inferred_source_bound_basis():
    texts = _case()
    rows, bases, problems = _resolve(texts)
    assert not problems
    assert rows[(1, "A")].source_element_ids == ("p2:description", "p2:tag")
    basis = bases[(1, "A")]
    assert basis["tier"] == "document_unique_exact_tag"
    assert basis["derivation"] == "inferred"
    assert basis["confidence"] == 0.75
    assert basis["field_tag_source_element_ids"] == ["p1:field"]
    assert basis["heading_source_element_ids"] == ["p2:heading"]
    assert not basis["reference_outcomes"]
    assert _resolve(texts) == (rows, bases, problems)


@pytest.mark.parametrize("descriptions", [("WALL LIGHT", "WALL LIGHT"), ("WALL LIGHT", "PENDANT")])
def test_competing_definitions_never_resolve_by_payload_or_page_order(descriptions):
    rows, bases, problems = _resolve(_case(titles=("LIGHTING FIXTURE SCHEDULE",)*2,
                                         descriptions=descriptions))
    assert (1, "A") not in rows and (1, "A") not in bases
    assert len(problems[0]["candidate_schedules"]) == 2


def test_explicit_sheet_reference_selects_definition_and_keeps_source():
    rows, bases, problems = _resolve(_case(titles=("LIGHTING FIXTURE SCHEDULE",)*2,
        descriptions=("WALL LIGHT", "PENDANT"),
        references=("SEE LIGHTING FIXTURE SCHEDULE ON E9.2",)))
    assert not problems
    assert rows[(1, "A")].fields["description"] == "PENDANT"
    assert bases[(1, "A")]["tier"] == "explicit_reference"
    assert bases[(1, "A")]["derivation"] == "observed"
    assert bases[(1, "A")]["confidence"] == 0.95
    assert bases[(1, "A")]["reference_outcomes"][0]["source_element_id"] == "p1:reference0"


def test_exact_schedule_title_reference_is_explicit_when_unique():
    _, bases, problems = _resolve(_case(references=("REFER TO LIGHTING FIXTURE SCHEDULE",)))
    assert not problems
    assert bases[(1, "A")]["tier"] == "explicit_reference"


@pytest.mark.parametrize("references", [
    ("SEE LIGHTING FIXTURE SCHEDULE ON E99.9",),
    ("SEE LIGHTING FIXTURE SCHEDULE ON E9.1 AND E99.9",),
    ("SEE LIGHTING FIXTURE SCHEDULE ON E9.1", "SEE LIGHTING FIXTURE SCHEDULE ON E9.2"),
])
def test_unresolved_or_conflicting_explicit_reference_never_falls_back(references):
    rows, _, problems = _resolve(_case(references=references))
    assert (1, "A") not in rows
    assert problems[0]["reason_code"] == "lighting_schedule_reference_unresolved"


def test_building_and_floor_scope_selects_only_applicable_definition():
    rows, bases, problems = _resolve(_case(
        field_title="BUILDING A FIRST FLOOR LIGHTING PLAN",
        titles=("BUILDING A FIRST FLOOR LIGHTING FIXTURE SCHEDULE",
                "BUILDING B SECOND FLOOR LIGHTING FIXTURE SCHEDULE")))
    assert not problems
    assert rows[(1, "A")].page == 2
    assert bases[(1, "A")]["field_scope"]["values"] == {"building": ["A"], "floor": ["1"]}
    assert bases[(1, "A")]["candidate_schedules"][1]["status"] == "scope_conflict"


@pytest.mark.parametrize("field_title", ["SECOND FLOOR LIGHTING PLAN", "LIGHTING PLAN"])
def test_conflicting_or_unknown_required_floor_scope_stays_unresolved(field_title):
    rows, _, problems = _resolve(_case(field_title=field_title,
        titles=("FIRST FLOOR LIGHTING FIXTURE SCHEDULE",)))
    assert (1, "A") not in rows
    assert problems


def test_explicit_reference_cannot_override_observed_scope_conflict():
    rows, _, problems = _resolve(_case(field_title="SECOND FLOOR LIGHTING PLAN",
        titles=("FIRST FLOOR LIGHTING FIXTURE SCHEDULE",),
        references=("SEE LIGHTING FIXTURE SCHEDULE ON E9.1",)))
    assert (1, "A") not in rows
    assert problems[0]["candidate_schedules"][0]["status"] == "scope_conflict"


@pytest.mark.parametrize("trade", ["ARCHITECTURAL", "STRUCTURAL"])
def test_nonlighting_tables_cannot_supply_cross_sheet_attributes(trade):
    rows, _, _ = _resolve(_case(titles=(f"{trade} FIXTURE SCHEDULE",)))
    assert not rows


def test_exact_tag_required_and_repeated_instances_do_not_change_schedule_quantity():
    texts = _case()
    texts += [_text(1, "second-instance", "A", 200, 100),
              _text(1, "unrelated-tag", "A1", 300, 100)]
    rows, bases, _ = _resolve(texts)
    assert (1, "A1") not in rows
    assert bases[(1, "A")]["field_tag_source_element_ids"] == ["p1:field", "p1:second-instance"]
    assert "quantity" not in rows[(1, "A")].fields


def _two_page_source(path, *, omit_legend=False, mismatch=False):
    original = path.with_name("authored-single-sheet.pdf")
    _write_generated_lighting_pdf(original)
    reader = PdfReader(original)
    commands = reader.pages[0].get_contents().get_data().decode("ascii").splitlines()
    field, schedule = [], []
    # This is the source author's known schedule region, independent of the
    # importer, detector, model output, or expected-answer generation.
    for command in commands:
        if omit_legend:
            point = re.search(r"([-\d.]+) ([-\d.]+) (?:Tm|m)", command)
            if point and float(point[1]) >= 700 and float(point[2]) >= 500:
                continue
        if mismatch and command.startswith("113.000 555.000 m"):
            command = "113 555 m 127 555 l 120 566 l h S"
        match = re.search(r"([-\d.]+) ([-\d.]+) Tm", command)
        target = schedule if match and float(match[1]) >= 620 and 345 <= float(match[2]) <= 465 else field
        target.append(command)
    writer = PdfWriter()
    for source_commands in [field, schedule]:
        page = writer.add_page(reader.pages[0])
        content = DecodedStreamObject()
        content.set_data("\n".join(source_commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)


def test_two_page_pdf_enriches_five_observed_fixtures_with_inferred_relation(tmp_path):
    path = tmp_path / "two-sheet.pdf"
    _two_page_source(path)
    model = ElectricalPdfImporter().import_pdf(path)
    validate_model(model)
    fixtures = [device for device in model.electrical_devices if device.device_type == "luminaire"]
    assert len(fixtures) == 5
    for fixture in fixtures:
        schedule = fixture.attributes["pdf_electrical"]["fixture_schedule"]
        assert schedule["source_page"] == 2
        assert schedule["association"]["tier"] == "document_unique_exact_tag"
        association = [p for p in fixture.provenance if p.method == "pdf-lighting-schedule-association"]
        assert len(association) == 1
        assert association[0].derivation == "inferred" and association[0].confidence == 0.75
        assert all(source.startswith("p2:") for source in schedule["source_element_ids"])


def test_schedule_page_geometry_alone_never_places_fixtures(tmp_path):
    path = tmp_path / "two-sheet.pdf"
    _two_page_source(path)
    document = extract_pdf(path)
    schedule_only = replace(document, texts=tuple(t for t in document.texts if t.page == 2),
                            vectors=tuple(v for v in document.vectors if v.page == 2))
    model = ElectricalPdfImporter().import_document(schedule_only)
    validate_model(model)
    assert not model.electrical_devices


def test_competing_invalid_definition_blocks_inference_but_not_explicit_valid_target():
    texts = _case(titles=("LIGHTING FIXTURE SCHEDULE",)*2)
    texts += [_text(3, "conflict-tag", "A", 50, 640),
              _text(3, "conflict-description", "PENDANT", 150, 640)]
    rows, _, problems = _resolve(texts)
    assert (1, "A") not in rows
    assert problems[0]["blocking_schedule_diagnostics"]
    texts += [_text(1, "reference", "SEE LIGHTING FIXTURE SCHEDULE ON E9.1")]
    rows, bases, problems = _resolve(texts)
    assert not problems
    assert rows[(1, "A")].page == 2
    assert bases[(1, "A")]["tier"] == "explicit_reference"


def test_cross_sheet_schedule_does_not_replace_missing_legend(tmp_path):
    path = tmp_path / "no-legend.pdf"
    _two_page_source(path, omit_legend=True)
    model = ElectricalPdfImporter().import_pdf(path)
    assert not [device for device in model.electrical_devices if device.device_type == "luminaire"]
    assert "lighting_fixture_symbol_unconfirmed" in model.to_json()


def test_cross_sheet_schedule_cannot_override_symbol_mismatch(tmp_path):
    path = tmp_path / "mismatch.pdf"
    _two_page_source(path, mismatch=True)
    model = ElectricalPdfImporter().import_pdf(path)
    assert len([device for device in model.electrical_devices if device.device_type == "luminaire"]) == 4
    assert "lighting_fixture_symbol_mismatch" in model.to_json()


@pytest.mark.parametrize("reference", [
    "FOR EXTERIOR LIGHTING SEE FIXTURE SCHEDULE ON E9.2",
    "SEE EXTERIOR LIGHTING FIXTURE SCHEDULE ON E9.2",
    "SEE FIXTURE SCHEDULE ON E9.2 FOR EXTERIOR LIGHTING",
    "FOR BUILDING B SEE LIGHTING FIXTURE SCHEDULE ON E9.2",
])
def test_scoped_reference_has_no_page_wide_authority(reference):
    texts = _case(titles=("LIGHTING FIXTURE SCHEDULE",)*2,
                  descriptions=("INTERIOR WALL LIGHT", "EXTERIOR WALL LIGHT"),
                  references=(reference,))
    rows, bases, problems = _resolve(texts)
    assert (1, "A") not in rows and (1, "A") not in bases
    assert len(problems[0]["candidate_schedules"]) == 2
    ref = problems[0]["reference_outcomes"][0]
    assert ref["applicability_qualifiers"]
    assert ref["applicability_status"] == "unresolved"
    assert ref["source_text"] == reference
    assert ref["source_element_id"] == "p1:reference0"


def test_architectural_lighting_schedule_retains_explicit_lighting_scope():
    rows, bases, problems = _resolve(_case(titles=("ARCHITECTURAL LIGHTING FIXTURE SCHEDULE",)))
    assert not problems
    assert rows[(1, "A")].fields["description"] == "WALL LIGHT"
    assert bases[(1, "A")]["tier"] == "document_unique_exact_tag"
