"""Actual-source probe for the annotated, unclassified open-chain gap (#76).

The strict xfail is an unsatisfied acceptance check, not a classifier or fix.
Only the dedicated membership exception can xfail; fixture/binding failures fail.
"""
import pytest

from test_pdf_branch_geometry import inputs, sheet
from oabm.importers.pdf_electrical import ElectricalPdfImporter
from oabm.model import provenance_applies_to


class UnclassifiedBranchMembership(AssertionError):
    """The unresolved semantic distinction still produced canonical membership."""


def annotated_sheet(path, *, touching=True):
    # The sole circuit annotation is LP-1, next to the arrow at (500, 350).
    # EVSE-2 and EVSE-3 are identities, not named circuit members in that text.
    return sheet(path, length=80, extra=(
        "300 200 m 300 500 l S", "303 200 m 303 500 l S",
        "300 500 m 380 500 l S", "300 503 m 380 503 l S",
        f"460 {350 if touching else 330} m 460 200 l S",
        "460 200 m 650 200 l S",
        "BT /F1 8 Tf 455 268 Td (EVSE-2) Tj ET", "455 255 10 10 re S",
        "BT /F1 8 Tf 645 208 Td (EVSE-3) Tj ET", "645 195 10 10 re S",
    ))


def observed_import(path):
    assert not path.with_suffix(".expected.json").exists()
    document, architecture, evidence, _, _ = inputs(path)
    assert evidence.chains, "the actual producer must classify the distant paired faces"
    model = ElectricalPdfImporter().import_document(
        document, wall_evidence=evidence, architecture_document=architecture,
    )
    assert {item.name for item in model.electrical_equipment} == {"LP"}
    assert {item.name for item in model.electrical_devices} == {
        "EVSE-1", "EVSE-2", "EVSE-3",
    }
    assert any(item.text == "LP-1" for item in document.texts)
    assert not any(
        "LP-1" in item.text and ("EVSE-2" in item.text or "EVSE-3" in item.text)
        for item in document.texts
    )
    return document, architecture, evidence, model


def load_names(model):
    owners = {port.id: port.owner_id for port in model.ports}
    names = {item.id: item.name for item in model.electrical_devices}
    return {
        names[owners[port_id]]
        for circuit in model.circuits for port_id in circuit.load_port_ids
    }


def test_disconnecting_only_the_open_chain_preserves_the_annotated_branch(tmp_path):
    _, _, _, model = observed_import(
        annotated_sheet(tmp_path / "disconnected.pdf", touching=False),
    )
    assert len(model.circuits) == 1 and len(model.ports) == 2
    assert load_names(model) == {"EVSE-1"}
    assert model.circuits[0].circuit_number == "1"
    assert {record.method for record in model.circuits[0].provenance} == {
        "pdf-homerun-annotation",
    }


def test_authored_intent_without_source_evidence_cannot_change_the_extraction(tmp_path):
    # These copies admit opposite author interpretations of the same tree:
    # an architectural L touching wiring, or a lawful three-load branch.
    # Those interpretations are deliberately NOT supplied to either API.
    architectural_interpretation = annotated_sheet(tmp_path / "architectural.pdf")
    electrical_interpretation = annotated_sheet(tmp_path / "electrical.pdf")
    assert architectural_interpretation.read_bytes() == electrical_interpretation.read_bytes()
    left_doc, left_arch, left_evidence, _ = observed_import(architectural_interpretation)
    right_doc, right_arch, right_evidence, _ = observed_import(electrical_interpretation)
    assert left_doc == right_doc
    assert left_arch.content_sha256 == right_arch.content_sha256
    assert left_arch.pages == right_arch.pages
    assert left_evidence.chains == right_evidence.chains
    assert left_evidence.frame_pt == right_evidence.frame_pt


@pytest.mark.xfail(
    strict=True, raises=UnclassifiedBranchMembership,
    reason="#76 lacks source-bound electrical branch ownership/local semantic coverage",
)
def test_annotated_unclassified_component_requires_semantics_before_membership(tmp_path):
    document, architecture, evidence, model = observed_import(
        annotated_sheet(tmp_path / "touching.pdf"),
    )
    local_paths = {
        ((460., 350.), (460., 200.)),
        ((460., 200.), (650., 200.)),
    }
    local_vectors = [item for item in document.vectors if item.points_pt in local_paths]
    assert len(local_vectors) == 2
    local_lines = [
        item for item in architecture.pages[0].lines
        if {item.start_pt, item.end_pt} in [
            {(460., 350.), (460., 200.)}, {(460., 200.), (650., 200.)},
        ]
    ]
    assert len(local_lines) == 2
    classified_ids = {
        element_id for chain in evidence.chains for segment in chain.segments
        for element_id in segment.source_element_ids
    }
    assert classified_ids.isdisjoint(item.element_id for item in local_lines)
    lawful_vectors = [
        item for item in document.vectors
        if not item.closed and len(item.points_pt) == 2
        and all(point[1] == 350. for point in item.points_pt)
    ]
    assert len(lawful_vectors) == 6
    styles = {
        (item.metadata.get("paint_operator"), item.metadata.get("stroke_gray"),
         item.metadata.get("line_width_pt"))
        for item in (*lawful_vectors, *local_vectors)
    }
    assert styles == {("S", 0., 1.)}
    lane = model.attributes["pdf_electrical"]
    check, = lane["branch_geometry_checks"]
    assert check["status"] == "eligible"
    assert check["architecture_evidence_status"] == "supplied_positive"
    assert not check["wall_chain_ids"] and not check["wall_evidence_element_ids"]
    assert {item.element_id for item in local_vectors}.issubset(check["source_element_ids"])
    observed_membership = [
        record for circuit in model.circuits for record in circuit.provenance
        if record.derivation == "observed"
        and provenance_applies_to(record, ("load_port_ids",))
    ]
    # This cannot be repaired by cutting out the unknown strokes and certifying
    # the residual component. Circuit membership must stay unresolved until an
    # owning source-evidence lane can distinguish this case from lawful wiring.
    if model.circuits or model.ports:
        raise UnclassifiedBranchMembership(
            f"circuits={len(model.circuits)}, ports={len(model.ports)}, "
            f"loads={sorted(load_names(model))}, "
            f"observed_membership_records={len(observed_membership)}, "
            f"unresolved_circuits={len(lane['unresolved_circuits'])}"
        )
