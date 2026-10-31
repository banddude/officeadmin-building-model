"""Source-PDF regressions for the opt-in circuit materialization policy."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.importers.pdf_electrical import (
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfTextObservation,
    extract_pdf,
    import_document,
    import_pdf,
    schedule_source_fingerprint,
)
from oabm.model import BuildingModel
from test_pdf_branch_annotated_unclassified import annotated_sheet
from test_pdf_branch_geometry import inputs as branch_inputs, sheet as branch_sheet
from test_panel_schedule_review import review as make_schedule_review
from test_panel_schedule_review import source_pdf as make_schedule_source


def _candidate_ids(model):
    lane = model.attributes["pdf_electrical"]
    candidates = lane["circuit_review_candidates"]
    assert lane["circuit_materialization"]["candidate_association_count"] == len(candidates)
    return candidates


def test_defer_all_withholds_ambiguous_source_pdf_graph_but_keeps_candidate_evidence(tmp_path):
    # Associations now require the actual source legend in the strict handoff.
    path = annotated_sheet(tmp_path / "supported-touching.pdf", touching=True, legend=True)
    document, architecture, wall_evidence, _, _ = branch_inputs(path)
    assert not path.with_suffix(".expected.json").exists()

    legacy = ElectricalPdfImporter().import_document(
        document,
        wall_evidence=wall_evidence,
        architecture_document=architecture,
    )
    deferred = ElectricalPdfImporter(circuit_policy="defer_all").import_document(
        document,
        wall_evidence=wall_evidence,
        architecture_document=architecture,
    )

    assert deferred.electrical_devices == legacy.electrical_devices
    assert deferred.electrical_equipment == legacy.electrical_equipment
    assert deferred.ports == deferred.circuits == deferred.conductors == ()
    assert deferred.routes == deferred.route_fittings == ()
    report = deferred.attributes["pdf_electrical"]["circuit_materialization"]
    assert report["status"] == "deferred_by_caller_policy"
    assert report["schedule_reviews_supplied"] is False
    candidates = _candidate_ids(deferred)
    assert candidates

    legacy_owners = {port.id: port.owner_id for port in legacy.ports}
    legacy_members = {
        legacy_owners[port_id]
        for circuit in legacy.circuits
        for port_id in circuit.load_port_ids
    }
    assert candidates[0]["candidate_member_entity_ids"] == sorted(legacy_members)
    assert candidates[0]["candidate_source_entity_id"] in {
        item.id for item in deferred.electrical_equipment
    }

    source_ids = {
        item.element_id
        for item in (*document.texts, *document.symbols, *document.vectors)
    }
    assert candidates[0]["source_id"] == document.source_id
    assert all(
        set(row["source_element_ids"]) <= source_ids
        for row in candidates[0]["source_evidence"]
    )
    assert all(
        set(row["source_element_ids"]) <= source_ids
        for row in candidates[0]["source_provenance"]
    )
    assert candidates[0]["association_status"] == "candidate_only"
    assert candidates[0]["canonical_membership_emitted"] is False
    assert "source_text" not in str(candidates)
    assert not any(
        key in str(candidates)
        for key in ('"circuit_id"', '"source_port_id"', '"load_port_ids"', '"port_id"')
    )
    assert "resolved" not in str(candidates)
    assert all(item["association_status"] == "candidate_only" for item in candidates)
    assert all(item["canonical_membership_emitted"] is False for item in candidates)
    assert BuildingModel.from_json(deferred.to_json()).to_json() == deferred.to_json()
    assert ElectricalPdfImporter(circuit_policy="defer_all").import_document(
        document,
        wall_evidence=wall_evidence,
        architecture_document=architecture,
    ).to_json() == deferred.to_json()


def test_defer_all_does_not_emit_candidates_when_no_circuit_candidate_exists(tmp_path):
    path = branch_sheet(tmp_path / "no-device.pdf", device=False)
    document = extract_pdf(path)
    model = ElectricalPdfImporter(circuit_policy="defer_all").import_document(document)
    assert model.ports == model.circuits == model.conductors == ()
    assert _candidate_ids(model) == []
    assert model.attributes["pdf_electrical"]["circuit_materialization"][
        "status"
    ] == "deferred_by_caller_policy"


def test_defer_all_keeps_schedule_review_facts_but_explicitly_defers_graph(tmp_path):
    path, document = make_schedule_source(tmp_path)
    schedule_review = make_schedule_review(document)
    legacy = ElectricalPdfImporter(schedule_reviews=(schedule_review,)).import_document(document)
    deferred = import_document(
        document,
        schedule_reviews=(schedule_review.to_dict(),),
        circuit_policy="defer_all",
    )
    assert {item.circuit_number for item in legacy.circuits} == {"1", "3"}
    assert deferred.electrical_devices == legacy.electrical_devices
    assert deferred.electrical_equipment == legacy.electrical_equipment
    assert deferred.ports == deferred.circuits == deferred.conductors == ()
    report = deferred.attributes["pdf_electrical"]["circuit_materialization"]
    assert report["schedule_reviews_supplied"] is True
    assert "no circuit graph is emitted" in report["schedule_review_effect"]
    schedule = deferred.attributes["pdf_electrical"]["panel_schedules"]["LP"]
    assert schedule["status"] == "validated"
    assert schedule["circuits"] == [1, 3]
    assert schedule["canonical_materialization_status"] == "deferred_by_policy"
    assert schedule["human_reviews"]
    candidates = _candidate_ids(deferred)
    assert len(candidates) == len(legacy.circuits)
    assert all(item["status"] == "candidate-association-not-materialized" for item in candidates)
    assert all(item["association_status"] == "candidate_only" for item in candidates)
    assert any(
        record["method"] == "pdf-panel-schedule-human-confirmation"
        for item in candidates
        for record in item["source_provenance"]
    )
    wrapped_pdf = import_pdf(
        path,
        source_id=document.source_id,
        schedule_reviews=(schedule_review.to_dict(),),
        circuit_policy="defer_all",
    )
    class_pdf = ElectricalPdfImporter(
        schedule_reviews=(schedule_review,),
        circuit_policy="defer_all",
    ).import_pdf(path, source_id=document.source_id)
    assert wrapped_pdf.to_json() == deferred.to_json()
    assert class_pdf.to_json() == deferred.to_json()


def test_deferred_report_binds_extraction_evidence_not_source_alias(tmp_path):
    _, document = make_schedule_source(tmp_path)
    first = ElectricalPdfImporter(circuit_policy="defer_all").import_document(document)
    repeated = ElectricalPdfImporter(circuit_policy="defer_all").import_document(document)
    report = first.attributes["pdf_electrical"]["circuit_materialization"]
    assert report["source_observation_fingerprint"] == schedule_source_fingerprint(document)
    assert repeated.attributes["pdf_electrical"]["circuit_materialization"][
        "source_observation_fingerprint"
    ] == report["source_observation_fingerprint"]
    assert "not raw PDF bytes" in report["source_observation_fingerprint_basis"]

    changed = replace(
        document,
        texts=(
            *document.texts,
            PdfTextObservation(
                element_id="synthetic:unrelated-observation",
                page=1,
                text="UNRELATED EVIDENCE CHANGE",
                x_pt=20.0,
                y_pt=20.0,
            ),
        ),
    )
    assert changed.source_id == document.source_id
    changed_model = ElectricalPdfImporter(circuit_policy="defer_all").import_document(changed)
    changed_fingerprint = changed_model.attributes["pdf_electrical"][
        "circuit_materialization"
    ]["source_observation_fingerprint"]
    assert changed_fingerprint != report["source_observation_fingerprint"]


def test_legacy_default_and_explicit_legacy_keep_pinned_prechange_bytes():
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/pdf_electrical/geometry-only-power-sheet-with-legend.pdf"
    )
    document = extract_pdf(fixture, source_id="synthetic:byte-identity")
    baseline = ElectricalPdfImporter().import_document(document)
    explicit_legacy = import_document(document, circuit_policy="legacy")
    digest = hashlib.sha256(baseline.to_json(indent=None).encode()).hexdigest()
    assert digest == "3979e782b9bb48b742608f00c5fafb27353ee0cbab7b9ca0a03ea922ab1f1b92"
    assert explicit_legacy.to_json() == baseline.to_json()
    assert "circuit_materialization" not in baseline.attributes["pdf_electrical"]
    assert "circuit_review_candidates" not in baseline.attributes["pdf_electrical"]


@pytest.mark.parametrize("value", ["review_required", "DEFER_ALL", "", None, True, 1])
def test_circuit_policy_rejects_unknown_values(value):
    with pytest.raises(ElectricalPdfError, match="circuit_policy must be 'legacy' or 'defer_all'"):
        ElectricalPdfImporter(circuit_policy=value)
