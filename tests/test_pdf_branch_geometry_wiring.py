"""Importer integration uses generated source PDFs, never proposal/answer inputs."""
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from test_pdf_branch_geometry import sheet, inputs
from oabm.importers.pdf_electrical import ElectricalPdfImporter, import_document, import_pdf
from oabm.importers.pdf_wall_evidence import WallEvidenceHandoffError


@pytest.mark.parametrize('segments,length', [(6,350), (65,80), (350,350)])
def test_bound_long_segmented_runs_keep_existing_circuit_guards(tmp_path,segments,length):
    path=sheet(tmp_path/'run.pdf',segments=segments,length=length)
    doc,arch,evidence,_,_=inputs(path)
    model=ElectricalPdfImporter().import_document(doc,wall_evidence=evidence,architecture_document=arch)
    assert [c.circuit_number for c in model.circuits]==['1']
    assert len(model.ports)==2
    checks=model.attributes['pdf_electrical']['branch_geometry_checks']
    assert len(checks)==1 and checks[0]['status']=='eligible'
    assert len(checks[0]['source_element_ids'])==segments
    assert model.to_json()==import_document(doc,wall_evidence=evidence,architecture_document=arch).to_json()


def test_interior_wall_crossing_cannot_be_rescued_by_direct_device_tag(tmp_path):
    path=sheet(tmp_path/'crossing.pdf',wall=True)
    # Move the sole generated circuit annotation next to the authored device.
    # It still annotates this run, but the old direct-tag pass can bypass rejection.
    reader=PdfReader(path);writer=PdfWriter();page=writer.add_page(reader.pages[0])
    data=page.get_contents().get_data()
    assert b'510 352 Td (LP-1)' in data
    stream=DecodedStreamObject();stream.set_data(data.replace(b'510 352 Td (LP-1)',b'155 358 Td (LP-1)'))
    page[NameObject('/Contents')]=writer._add_object(stream);writer.write(path)
    assert not path.with_suffix('.expected.json').exists()
    doc,arch,evidence,_,_=inputs(path)
    before=ElectricalPdfImporter().import_document(doc,wall_evidence=evidence)
    assert before.circuits and before.ports  # Existing direct-tag rescue is the trigger.
    model=ElectricalPdfImporter().import_document(doc,wall_evidence=evidence,architecture_document=arch)
    assert not model.circuits and not model.ports
    lane=model.attributes['pdf_electrical']
    check=lane['branch_geometry_checks'][0]
    assert check['reason_code']=='enters_classified_wall_chain'
    assert check['architecture_evidence_status']=='supplied_positive'
    assert check['wall_chain_ids'] and check['wall_evidence_element_ids']
    assert any(row.get('reason_code')=='branch_geometry_rejected' for row in lane['unresolved_circuits'])
    assert any(row.get('reason')=='branch_geometry_rejected' for row in lane['unresolved_topology'])
    assert model.to_json()==import_pdf(path,wall_evidence=evidence,architecture_document=arch).to_json()


def test_bound_dimension_arrow_without_device_stays_unresolved(tmp_path):
    path=sheet(tmp_path/'dimension.pdf',device=False)
    doc,arch,evidence,_,_=inputs(path)
    model=ElectricalPdfImporter().import_document(doc,wall_evidence=evidence,architecture_document=arch)
    assert not model.circuits and not model.ports and not model.electrical_devices


@pytest.mark.parametrize('change',['sha','ids','frame','missing'])
def test_invalid_architecture_handoff_fails_before_materialization(tmp_path,change):
    path=sheet(tmp_path/'bound.pdf',wall=True)
    doc,arch,evidence,_,_=inputs(path)
    if change=='sha':
        evidence=replace(evidence,content_sha256='0'*64)
    elif change=='ids':
        chain=evidence.chains[0]
        evidence=replace(evidence,chains=(replace(chain,segments=(replace(chain.segments[0],source_element_ids=('missing-source',)),)),))
    elif change=='frame':
        evidence=replace(evidence,frame_pt=(0.,0.,900.,900.))
    else:
        evidence=None
    with pytest.raises((WallEvidenceHandoffError,ValueError)):
        ElectricalPdfImporter().import_document(doc,wall_evidence=evidence,architecture_document=arch)


def test_empty_and_missing_page_handoffs_preserve_helper_vocabulary(tmp_path):
    path=sheet(tmp_path/'two-pages.pdf')
    reader=PdfReader(path);writer=PdfWriter()
    for number in (1,2):
        page=writer.add_page(reader.pages[0])
        if number==2:
            stream=DecodedStreamObject()
            stream.set_data(page.get_contents().get_data().replace(b'EVSE-1',b'EVSE-2')
                            .replace(b'PANEL LP 120/208V 3PH',b'SECOND FLOOR'))
            page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(path)
    doc,arch,evidence,_,_=inputs(path)
    evidence=replace(evidence,chains=())
    model=ElectricalPdfImporter().import_document(doc,wall_evidence=evidence,architecture_document=arch)
    checks=model.attributes['pdf_electrical']['branch_geometry_checks']
    assert {(row['page'],row['architecture_evidence_status']) for row in checks}=={
        (1,'supplied_empty'),(2,'missing_handoff')}
    assert all(row['status']=='eligible' for row in checks)
