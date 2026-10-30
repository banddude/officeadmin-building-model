"""Source-PDF positive and negative ownership controls for #377."""
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from test_pdf_branch_geometry import sheet, inputs
from oabm.importers.pdf_electrical import ElectricalPdfImporter
from oabm.importers.pdf_electrical.branch_ownership import source_branch_evidence
from oabm.model import BuildingModel, provenance_applies_to

LEGEND = ('BT /F1 8 Tf 20 675 Td (LINE LEGEND) Tj ET', '20 650 m 80 650 l S',
          'BT /F1 8 Tf 90 650 Td (ELECTRICAL BRANCH WIRING) Tj ET')


def rewrite(path, before, after):
    reader = PdfReader(path)
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    data = page.get_contents().get_data()
    assert before in data
    stream = DecodedStreamObject()
    stream.set_data(data.replace(before, after))
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


def run(path):
    assert not path.with_suffix('.expected.json').exists()
    doc, arch, walls, _, _ = inputs(path)
    return doc, ElectricalPdfImporter().import_document(
        doc, wall_evidence=walls, architecture_document=arch)


@pytest.mark.parametrize('segments,length,taps', [(1,80,False), (6,350,False),
    (65,80,False), (350,350,False), (6,350,True)])
def test_actual_legend_producer_preserves_lawful_branches(tmp_path, segments, length, taps):
    extra = list(LEGEND)
    if taps:
        extra += ['300 350 m 300 250 l S', '400 350 m 400 200 l S',
            '295 245 10 10 re S', 'BT /F1 8 Tf 295 258 Td (EVSE-2) Tj ET',
            '395 195 10 10 re S', 'BT /F1 8 Tf 395 208 Td (EVSE-3) Tj ET']
    doc, model = run(sheet(tmp_path/'positive.pdf', segments=segments, length=length, extra=extra))
    assert len(model.circuits) == 1
    circuit, = model.circuits
    assert len(circuit.load_port_ids) == (3 if taps else 1)
    assert len(model.ports) == (4 if taps else 2)
    assert circuit.circuit_number == '1'
    assert all(not (record.derivation == 'observed' and
        provenance_applies_to(record, ('load_port_ids',))) for record in circuit.provenance)
    ownership = [r for r in circuit.provenance if r.method == 'pdf-source-legend-branch-membership']
    assert len(ownership) >= segments
    assert all(r.confidence <= .9 and r.attributes['support_element_ids'] for r in ownership)
    assert all(any(r.method == 'pdf-source-legend-branch-membership' for r in p.provenance)
               for p in model.ports)
    assert BuildingModel.from_json(model.to_json()).to_json() == model.to_json()
    evidence = source_branch_evidence(doc, page=1)
    evidence.validate(doc)
    permuted = replace(doc, vectors=tuple(reversed(doc.vectors)), texts=tuple(reversed(doc.texts)))
    assert source_branch_evidence(permuted, page=1) == evidence
    _, arch, walls, _, _ = inputs(tmp_path/'positive.pdf')
    repeated = ElectricalPdfImporter().import_document(permuted, wall_evidence=walls, architecture_document=arch)
    assert repeated.to_json() == model.to_json()


@pytest.mark.parametrize('mode', ['missing', 'conflicting', 'unknown_touch', 'architecture_touch',
    'unsupported_dash', 'unsupported_color', 'unsupported_alpha', 'ambiguous_sample', 'missing_heading'])
def test_unknown_or_conflicting_whole_component_never_materializes(tmp_path, mode):
    extra = list(LEGEND) if mode != 'missing' else []
    if mode == 'conflicting':
        extra += ['20 620 m 80 620 l S', 'BT /F1 8 Tf 90 620 Td (ARCHITECTURAL LINEWORK) Tj ET']
    if mode in ('unknown_touch', 'architecture_touch'):
        extra += ['q 2 w 460 350 m 460 250 l S Q', '455 245 10 10 re S',
                  'BT /F1 8 Tf 455 258 Td (EVSE-2) Tj ET']
        if mode == 'architecture_touch':
            extra += ['q 2 w 20 620 m 80 620 l S Q',
                'BT /F1 8 Tf 90 620 Td (ARCHITECTURAL LINEWORK) Tj ET']
    if mode == 'ambiguous_sample':
        extra += ['25 651 m 75 651 l S']
    path = sheet(tmp_path/'negative.pdf', length=80, extra=extra)
    if mode == 'missing_heading':
        rewrite(path, b'(LINE LEGEND)', b'(GENERAL NOTES)')
    if mode.startswith('unsupported'):
        # Change the actual branch strokes, leaving the observed legend sample
        # unchanged. Unsupported paint cannot collapse onto a known solid style.
        operator = {'unsupported_dash': b'[3 2] 0 d', 'unsupported_color': b'0 0 0 RG',
                    'unsupported_alpha': b'/Unsupported gs'}[mode]
        rewrite(path, b'420.0 350 m', b'q ' + operator + b' 420.0 350 m')
    doc, model = run(path)
    assert not model.circuits and not model.ports
    checks = model.attributes['pdf_electrical']['branch_ownership_checks']
    assert any(row['unresolved_element_ids'] for row in checks)
    classifications = {c['classification'] for row in checks for c in row['claims']}
    if mode == 'conflicting':
        assert 'conflicting' in classifications
    if mode == 'architecture_touch':
        assert 'architectural' in classifications


def test_unknown_interior_crossing_cannot_be_missed_by_endpoint_assembly(tmp_path):
    _, model = run(sheet(tmp_path/'interior.pdf', segments=1, length=80,
        extra=(*LEGEND, 'q 2 w 460 200 m 460 500 l S Q')))
    assert not model.circuits and not model.ports
    assert any(row.get('reason_code') == 'branch_ownership_unresolved'
               for row in model.attributes['pdf_electrical']['unresolved_circuits'])


def test_defer_policy_retains_unknown_source_evidence_without_candidate_membership(tmp_path):
    path = sheet(tmp_path/'unknown-policy.pdf', length=80)
    doc, arch, walls, _, _ = inputs(path)
    model = ElectricalPdfImporter(circuit_policy='defer_all').import_document(
        doc, wall_evidence=walls, architecture_document=arch)
    assert not model.circuits and not model.ports
    lane = model.attributes['pdf_electrical']
    assert not lane['circuit_review_candidates']
    assert any(row['unresolved_element_ids'] for row in lane['branch_ownership_checks'])


@pytest.mark.parametrize('pathway', ['homerun', 'direct_tag', 'named_text', 'unarrowed'])
def test_other_materializers_cannot_rescue_unknown_membership(tmp_path, pathway):
    path = sheet(tmp_path/'bypass.pdf', length=80)
    if pathway == 'direct_tag':
        rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    elif pathway == 'named_text':
        rewrite(path, b'(LP-1)', b'(PANEL LP CIRCUIT 1 EVSE-1)')
    elif pathway == 'unarrowed':
        rewrite(path, b'494 346 m 500 350 l 494 354 l S', b'')
        rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    _, model = run(path)
    assert not model.circuits and not model.ports
    assert any(r.get('reason_code') == 'branch_ownership_unresolved'
               for r in model.attributes['pdf_electrical']['unresolved_circuits'])


@pytest.mark.parametrize('change', ['digest', 'page', 'frame', 'missing_vector', 'text', 'claim'])
def test_evidence_requires_actual_source_replay(tmp_path, change):
    doc, _ = run(sheet(tmp_path/'binding.pdf', length=80, extra=LEGEND))
    evidence = source_branch_evidence(doc, page=1)
    if change == 'digest':
        doc = replace(doc, source_id='sha256:' + '0'*64)
    elif change == 'page':
        evidence = replace(evidence, page=2)
    elif change == 'frame':
        evidence = replace(evidence, frame_pt=(0.,0.,900.,900.))
    elif change == 'missing_vector':
        doc = replace(doc, vectors=doc.vectors[1:])
    elif change == 'text':
        doc = replace(doc, texts=(replace(doc.texts[0], text='changed'), *doc.texts[1:]))
    else:
        evidence = replace(evidence, claims=(replace(evidence.claims[0], classification='electrical'),))
    with pytest.raises(ValueError):
        evidence.validate(doc)
