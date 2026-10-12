"""#94 confirmations are exercised through actual public synthetic source PDFs."""

from dataclasses import replace
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from oabm.importers.pdf_electrical import (
    ConfirmedScheduleRow, ElectricalPdfImporter, PanelScheduleReview,
    extract_pdf, import_pdf, schedule_source_fingerprint,
)
from oabm.model import BuildingModel, DERIVATION_INFERRED, DERIVATION_USER, is_observed, validate_model


def source_pdf(tmp_path, *, rotation=0, single_column=False, extra_number=False, pages=1, legacy=False, row_one=1, two_panels=False, topology=False):
    writer = PdfWriter()
    for page_index in range(pages):
        width, height = (612, 792) if rotation in (90, 270) else (792, 612)
        page = writer.add_blank_page(width=width, height=height)
        page[NameObject("/Rotate")] = NumberObject(rotation)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        # Keep the same displayed geometry for actual quarter-turn PDF pages.
        prefix = {0: "", 90: "0 1 -1 0 612 0 cm\n", 180: "-1 0 0 -1 792 612 cm\n",
                  270: "0 -1 1 0 0 792 cm\n"}[rotation]
        rows = [(600, 550, "PANEL LP SCHEDULE")]
        if single_column:
            rows += [(600, 530, "CIRCUIT LOAD NOTES"), (600, 525, "99 KEYNOTE")]
        else:
            rows += [(600, 525, str(row_one)), (640, 525, "LIGHTING LOAD"),
                     (600, 505, "3"), (640, 505, "SPARE LOAD")]
        if extra_number:
            rows.append((610, 525, "9"))
        if two_panels:
            rows += [(70, 200, "PANEL DP 120/208V 3PH"),
                     (430, 182, "EVSE-3"), (452, 171, "DP-5"),
                     (600, 350, "PANEL DP SCHEDULE"), (600, 325, "5"), (640, 325, "OTHER LOAD")]
        if page_index == 0:
            rows += [(70, 560, "PANEL LP 120/208V 3PH"),
                     (170, 462, "EVSE-1"), (300, 322, "EVSE-2"),
                     (192, 451, "LP-99" if single_column else "LP-1"),
                     (322, 311, "LP-3")]
            if legacy:
                rows = [r for r in rows if r[2] not in {"LP-1", "LP-3"}]
                rows += [(192, 451, "PANEL LP CKT 1 EVSE-1"),
                         (322, 311, "PANEL LP CKT 3 EVSE-2")]
            if topology:
                rows = [r for r in rows if r[2] not in {"LP-1", "LP-3"}]
                rows.append((100, 510, "CKT 3"))
        content = prefix + "175 445 10 10 re S\n305 305 10 10 re S\n" if page_index == 0 else prefix
        content += "590 480 170 80 re S\n"  # one outline, no divider or cell grid
        if two_panels:
            content += "435 165 10 10 re S\n590 280 170 80 re S\n"
        if topology:
            content += "70 560 m 170 462 l S\n"
        content += "".join(f"BT /F1 8 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n" for x, y, text in rows)
        stream = DecodedStreamObject()
        stream.set_data(content.encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / f"review-source-{rotation}-{pages}-{single_column}-{extra_number}-{legacy}-{row_one}.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    assert not path.with_suffix(".json").exists()
    return path, extract_pdf(path, source_id="synthetic:review-source")


def review(document, *, page=1, partial=False, review_id="review:synthetic-1"):
    by_text = {t.text.strip(): t for t in document.texts if t.page == page}
    return PanelScheduleReview(
        review_id=review_id, source_id=document.source_id,
        source_fingerprint=schedule_source_fingerprint(document), page=page,
        page_provenance=dict(document.page_provenance[page]), reviewer="synthetic-reviewer",
        reviewed_at="2026-10-11T12:00:00-07:00", panel_tag="LP",
        heading_element_id=by_text["PANEL LP SCHEDULE"].element_id,
        region_bbox_pt=(590, 480, 760, 560), number_column_pt=(590, 620), load_column_pt=(630, 760),
        rows=(ConfirmedScheduleRow(1, (520, 530), by_text["1"].element_id,
                                   (by_text["LIGHTING LOAD"].element_id,)),
              ConfirmedScheduleRow(3, (500, 510), by_text["3"].element_id,
                                   (by_text["SPARE LOAD"].element_id,), confirmed=not partial)),
    )


def imported(document, *reviews):
    model = ElectricalPdfImporter(schedule_reviews=reviews).import_document(document)
    validate_model(model)
    assert BuildingModel.from_json(model.to_json()).to_json() == model.to_json()
    return model


def test_confirmed_source_rows_validate_explicit_tags_and_preserve_user_review(tmp_path):
    path, document = source_pdf(tmp_path)
    baseline = imported(document)
    assert baseline.circuits == ()
    assert baseline.attributes["pdf_electrical"]["panel_schedules"]["LP"]["status"] == "structure_not_confirmed"
    confirmation = review(document)
    model = imported(document, confirmation)
    assert {c.circuit_number for c in model.circuits} == {"1", "3"}
    for circuit in model.circuits:
        assert not is_observed(circuit.provenance)
        human = [p for p in circuit.provenance if p.derivation == DERIVATION_USER]
        assert len(human) == 1
        assert human[0].page == 1
        assert human[0].attributes["review"] == confirmation.to_dict()
        assert human[0].source_element_id in human[0].attributes["source_element_ids"]
        assert int(human[0].attributes["source_cells"]["number"]["text"]) == int(circuit.circuit_number)
        assert human[0].attributes["source_cells"]["loads"][0]["text"].strip()
    assert all(all(p.derivation == DERIVATION_INFERRED for p in port.provenance) for port in model.ports)
    assert imported(document, confirmation).to_json() == model.to_json()
    assert import_pdf(path, source_id=document.source_id, schedule_reviews=[confirmation.to_dict()]).to_json() == model.to_json()
    assert PanelScheduleReview.from_dict(json.loads(json.dumps(confirmation.to_dict()))).to_dict() == confirmation.to_dict()


def test_partial_review_does_not_validate_unconfirmed_row(tmp_path):
    _, document = source_pdf(tmp_path)
    model = imported(document, review(document, partial=True))
    assert [c.circuit_number for c in model.circuits] == ["1"]
    schedule = model.attributes["pdf_electrical"]["panel_schedules"]["LP"]
    assert schedule["circuits"] == [1]
    assert schedule["human_reviews"][0]["rows"][1]["reason_code"] == "schedule_row_not_confirmed"
    assert any(r.get("source_text") == "LP-3" and r["status"] == "unresolved"
               for r in model.attributes["pdf_electrical"]["unresolved_circuits"])


def test_omitted_rows_and_empty_confirmation_do_not_invent_membership(tmp_path):
    _, document = source_pdf(tmp_path)
    one = review(document)
    assert [c.circuit_number for c in imported(document, replace(one, rows=one.rows[:1])).circuits] == ["1"]
    assert imported(document, replace(one, rows=())).circuits == ()


def test_conflicting_confirmations_fail_closed_independent_of_input_order(tmp_path):
    _, document = source_pdf(tmp_path)
    full = review(document)
    partial = review(document, partial=True, review_id="review:synthetic-2")
    first, second = imported(document, full, partial), imported(document, partial, full)
    assert first.to_json() == second.to_json()
    assert first.circuits == first.ports == ()
    assert first.attributes["pdf_electrical"]["panel_schedules"]["LP"]["reason_code"] == "schedule_confirmation_conflict"
    assert any(r.get("reason_code") == "schedule_confirmation_conflict"
               for r in first.attributes["pdf_electrical"]["unresolved_circuits"])


def test_equivalent_independent_reviewers_keep_every_confirmation_and_stable_ids(tmp_path):
    _, document = source_pdf(tmp_path)
    one = review(document)
    two = replace(one, review_id="review:synthetic-2", reviewer="other-synthetic-reviewer")
    single, both = imported(document, one), imported(document, one, two)
    assert [c.id for c in both.circuits] == [c.id for c in single.circuits]
    assert [p.id for p in both.ports] == [p.id for p in single.ports]
    assert all(len([p for p in c.provenance if p.derivation == DERIVATION_USER]) == 2 for c in both.circuits)
    assert imported(document, two, one).to_json() == both.to_json()


def test_reused_review_id_across_panels_retracts_both_panels_and_truthful_diagnostics(tmp_path):
    from oabm.importers.pdf_electrical.schedule_review import reviewed_schedule_rows
    _, document = source_pdf(tmp_path, two_panels=True)
    first = review(document)
    by_text = {t.text: t for t in document.texts}
    second = replace(first, panel_tag="DP", heading_element_id=by_text["PANEL DP SCHEDULE"].element_id,
                     region_bbox_pt=(590, 280, 760, 360), rows=(ConfirmedScheduleRow(
                         5, (320, 330), by_text["5"].element_id, (by_text["OTHER LOAD"].element_id,)),))
    # Both individual mappings are valid; reusing their review identity is not.
    assert [c.circuit_number for c in imported(document, second).circuits if c.name.startswith("DP")] == ["5"]
    model = imported(document, first, second)
    assert model.circuits == model.ports == ()
    assert imported(document, second, first).to_json() == model.to_json()
    for panel in model.attributes["pdf_electrical"]["panel_schedules"].values():
        assert panel["circuits"] == []
        assert panel["reason_code"] == "schedule_confirmation_conflict"
        assert all(d["status"] == "rejected" for d in panel["human_reviews"])
        assert all(r["status"] == "unresolved" and r["reason_code"] == "schedule_confirmation_conflict"
                   for d in panel["human_reviews"] for r in d["rows"])
    headings = {(1, first.heading_element_id): "LP", (1, second.heading_element_id): "DP"}
    _, consumed, _, evidence, _ = reviewed_schedule_rows(document, [first, second],
        recognized_panels={"LP", "DP"}, heading_tags=headings)
    assert not consumed and not evidence


def test_single_column_notes_cannot_be_split_into_source_cells_by_confirmation(tmp_path):
    _, good = source_pdf(tmp_path)
    _, notes = source_pdf(tmp_path, single_column=True)
    confirmation = review(good)
    note = next(t for t in notes.texts if t.text == "99 KEYNOTE")
    confirmation = replace(confirmation, source_fingerprint=schedule_source_fingerprint(notes),
        heading_element_id=next(t.element_id for t in notes.texts if t.text == "PANEL LP SCHEDULE"),
        rows=(ConfirmedScheduleRow(99, (520, 530), note.element_id, (note.element_id + ":invented-load",)),))
    model = imported(notes, confirmation)
    assert model.circuits == model.ports == ()
    assert model.attributes["pdf_electrical"]["panel_schedules"]["LP"]["human_reviews"][0]["rows"][0]["reason_code"] == "schedule_row_source_mismatch"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_confirmations_use_the_extracted_display_frame_on_rotated_source_pdfs(tmp_path, rotation):
    _, document = source_pdf(tmp_path, rotation=rotation)
    model = imported(document, review(document))
    assert {c.circuit_number for c in model.circuits} == {"1", "3"}
    assert all(next(p for p in c.provenance if p.derivation == DERIVATION_USER)
               .attributes["review"]["page_provenance"]["page_rotation"] == rotation for c in model.circuits)


@pytest.mark.parametrize("case", ["source", "fingerprint", "frame", "heading", "page", "outside"])
def test_stale_or_mismatched_review_never_validates_a_circuit(tmp_path, case):
    _, document = source_pdf(tmp_path)
    original = review(document)
    changes = {
        "source": {"source_id": "synthetic:other-source"},
        "fingerprint": {"source_fingerprint": "0" * 64},
        "frame": {"page_provenance": {**original.page_provenance, "page_rotation": 90}},
        "heading": {"heading_element_id": original.rows[0].number_element_id},
        "page": {"page": 2},
        "outside": {"region_bbox_pt": (590, 480, 900, 560)},
    }
    model = imported(document, replace(original, **changes[case]))
    assert model.circuits == model.ports == ()
    assert model.attributes["pdf_electrical"]["panel_schedules"]["LP"]["human_reviews"][0]["status"] == "rejected"


def test_changed_actual_pdf_invalidates_review_even_with_same_source_alias(tmp_path):
    _, first = source_pdf(tmp_path)
    _, changed = source_pdf(tmp_path, row_one=2)
    model = imported(changed, review(first))
    assert model.circuits == ()
    assert model.attributes["pdf_electrical"]["panel_schedules"]["LP"]["reason_code"] == "schedule_confirmation_source_mismatch"


def test_source_ids_from_another_page_do_not_confirm_identical_looking_rows(tmp_path):
    _, document = source_pdf(tmp_path, pages=2)
    first, second = review(document, page=1), review(document, page=2)
    mistaken = replace(second, rows=first.rows)
    model = imported(document, mistaken)
    assert model.circuits == ()
    assert all(r.get("reason_code") == "schedule_row_source_mismatch"
               for r in model.attributes["pdf_electrical"]["panel_schedules"]["LP"]["human_reviews"][0]["rows"])


def test_extra_cell_text_is_not_silently_ignored_and_other_confirmed_row_survives(tmp_path):
    _, document = source_pdf(tmp_path, extra_number=True)
    model = imported(document, review(document))
    assert [c.circuit_number for c in model.circuits] == ["3"]
    assert model.attributes["pdf_electrical"]["panel_schedules"]["LP"]["human_reviews"][0]["rows"][0]["reason_code"] == "schedule_row_source_mismatch"


def test_legacy_text_link_cannot_bypass_partial_or_conflicting_review(tmp_path):
    _, document = source_pdf(tmp_path, legacy=True)
    partial = review(document, partial=True)
    model = imported(document, partial)
    assert [c.circuit_number for c in model.circuits] == ["1"]
    assert any(r.get("circuit_number") == "3" and r.get("reason_code") == "schedule_row_not_confirmed"
               for r in model.attributes["pdf_electrical"]["unresolved_circuits"])
    assert imported(document, partial, review(document)).circuits == ()


def test_vector_topology_cannot_bypass_review_and_preserves_confirmation_if_supported(tmp_path):
    _, document = source_pdf(tmp_path, topology=True)
    full = imported(document, review(document))
    assert [c.circuit_number for c in full.circuits] == ["3"]
    assert "pdf-vector-topology-link" in full.circuits[0].attributes["pdf_electrical"]["evidence_methods"]
    assert any(p.derivation == DERIVATION_USER for p in full.circuits[0].provenance)
    partial = imported(document, review(document, partial=True))
    assert partial.circuits == partial.ports == ()
    assert any(r.get("method") == "pdf-vector-topology-link" and r.get("reason_code") == "schedule_row_not_confirmed"
               for r in partial.attributes["pdf_electrical"]["unresolved_circuits"])


@pytest.mark.parametrize("changes", [{"version": "2.0"}, {"coordinate_space": "raw-pdf"},
    {"number_column_pt": (590, 650)}, {"reviewer": ""}, {"reviewed_at": "2026-10-11T12:00:00"}])
def test_invalid_review_shape_is_rejected_before_import(tmp_path, changes):
    _, document = source_pdf(tmp_path)
    with pytest.raises(ValueError):
        replace(review(document), **changes)


@pytest.mark.parametrize("coordinate", [False, True, "590", None])
def test_portable_review_rejects_boolean_or_nonnumeric_source_bounds(tmp_path, coordinate):
    _, document = source_pdf(tmp_path)
    record = review(document).to_dict()
    record["region_bbox_pt"][0] = coordinate
    with pytest.raises(ValueError, match="finite increasing coordinates"):
        PanelScheduleReview.from_dict(record)


def test_no_review_keeps_default_canonical_bytes(tmp_path):
    path, document = source_pdf(tmp_path)
    expected = ElectricalPdfImporter().import_document(document).to_json()
    assert ElectricalPdfImporter(schedule_reviews=[]).import_document(document).to_json() == expected
    assert import_pdf(path, source_id=document.source_id, schedule_reviews=[]).to_json() == expected
