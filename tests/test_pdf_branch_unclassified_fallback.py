"""An unrelated positive wall claim cannot resolve a local unknown branch (#76)."""
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import pytest

from test_pdf_branch_geometry import inputs, sheet
from oabm.importers.pdf_electrical import ElectricalPdfImporter, import_pdf


@pytest.mark.parametrize("callout,method", [
    ("LP-1", "pdf-device-circuit-tag"),
    ("PANEL LP CIRCUIT 1 EVSE-2", "pdf-circuit-text-link"),
])
def test_unclassified_open_chain_cannot_gain_membership_through_fallback(
    tmp_path, callout, method,
):
    def source(name, *, touching):
        # The lawful arrowed run is x=420..500, y=350. The paired faces at
        # x=300 are classified elsewhere. The single-stroke L is deliberately
        # unclassified, and reaches a second recognized load. Moving only its
        # first endpoint disconnects it from the arrowed run.
        path = sheet(tmp_path / name, length=80, extra=(
            "300 200 m 300 500 l S", "303 200 m 303 500 l S",
            "300 500 m 380 500 l S", "300 503 m 380 503 l S",
            f"460 {350 if touching else 330} m 460 200 l S",
            "460 200 m 650 200 l S",
            "BT /F1 8 Tf 645 216 Td (EVSE-2) Tj ET",
            "645 211 10 10 re S",
        ))
        reader = PdfReader(path)
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        stream = DecodedStreamObject()
        # This callout is close enough to EVSE-2 for the direct-tag path,
        # but outside the branch annotation radius. The homerun cannot
        # establish its circuit intent; named text must not rescue it either.
        stream.set_data(page.get_contents().get_data().replace(
            b"510 352 Td (LP-1)", f"650 275 Td ({callout})".encode(),
        ))
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(path)
        assert not path.with_suffix(".expected.json").exists()
        return path

    control_path = source("disconnected.pdf", touching=False)
    control_doc, control_arch, control_evidence, _, _ = inputs(control_path)
    control = ElectricalPdfImporter().import_document(
        control_doc, wall_evidence=control_evidence, architecture_document=control_arch,
    )
    assert {item.name for item in control.electrical_equipment} == {"LP"}
    assert {item.name for item in control.electrical_devices} == {"EVSE-1", "EVSE-2"}
    # Disconnecting an unknown path does not positively identify it as wiring.
    # Both components now require source ownership before any membership.
    assert not control.circuits and not control.ports
    assert any(row.get("reason_code") == "branch_ownership_unresolved"
               for row in control.attributes["pdf_electrical"]["unresolved_circuits"])

    path = source("touching.pdf", touching=True)
    doc, arch, evidence, _, _ = inputs(path)
    assert evidence.chains, "positive classification must come from the real producer"
    local_chain = [
        item for item in arch.pages[0].lines
        if (*item.start_pt, *item.end_pt) in {
            (460., 200., 460., 350.), (460., 200., 650., 200.),
        }
    ]
    assert len(local_chain) == 2
    classified = {
        element_id for chain in evidence.chains for segment in chain.segments
        for element_id in segment.source_element_ids
    }
    assert classified.isdisjoint(item.element_id for item in local_chain)

    # The legacy path demonstrates the exact alternate materializer at risk.
    legacy = ElectricalPdfImporter().import_document(doc, wall_evidence=evidence)
    assert len(legacy.circuits) == 1 and len(legacy.ports) == 2
    assert {record.method for record in legacy.circuits[0].provenance} == {method}

    guarded = ElectricalPdfImporter().import_document(
        doc, wall_evidence=evidence, architecture_document=arch,
    )
    assert {item.name for item in guarded.electrical_devices} == {"EVSE-1", "EVSE-2"}
    lane = guarded.attributes["pdf_electrical"]
    check, = lane["branch_geometry_checks"]
    assert check["status"] == "eligible"
    assert check["architecture_evidence_status"] == "supplied_positive"
    assert not check["wall_chain_ids"] and not check["wall_evidence_element_ids"]
    local_vectors = {
        item.element_id for item in doc.vectors
        if item.points_pt in {
            ((460., 350.), (460., 200.)), ((460., 200.), (650., 200.)),
        }
    }
    assert len(local_vectors) == 2
    assert local_vectors.issubset(check["source_element_ids"])
    assert not guarded.circuits and not guarded.ports
    assert any(row.get("reason_code") == "branch_ownership_unresolved" for row in lane["unresolved_circuits"])
    miss, = [
        row for row in lane["unresolved_circuits"]
        if row.get("method") == method
    ]
    assert miss["reason_code"] == "branch_geometry_rejected"
    assert miss["load_ids"] == [
        item.id for item in guarded.electrical_devices if item.name == "EVSE-2"
    ]
    assert miss["source_element_id"] == next(
        item.element_id for item in doc.texts if item.text == callout
    )
    assert any(row.get("reason") == "branch_geometry_rejected" for row in lane["unresolved_topology"])
    assert guarded.to_json() == import_pdf(
        path, wall_evidence=evidence, architecture_document=arch,
    ).to_json()
