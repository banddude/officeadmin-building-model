"""Independently authored source PDFs for the remaining #76 boundary."""
from dataclasses import replace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from oabm.importers.pdf_architecture.extract import extract_pdf as architecture_extract
from oabm.importers.pdf_architecture import source_wall_evidence
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.importers.pdf_electrical.importer import _homerun_arrowhead_apex
from oabm.importers.pdf_electrical.branch_geometry import discriminate_branch_geometry
from oabm.importers.pdf_wall_evidence import WallEvidenceHandoffError


def sheet(path, *, segments=6, length=350, wall=False, crossing=False, device=True, extra=()):
    writer = PdfWriter()
    page = writer.add_blank_page(width=800, height=700)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    commands = ['BT /F1 10 Tf 70 600 Td (PANEL LP 120/208V 3PH) Tj ET',
                'BT /F1 9 Tf 510 352 Td (LP-1) Tj ET']
    if device:
        commands += [f'BT /F1 8 Tf {500-length-5} 358 Td (EVSE-1) Tj ET', f'{500-length-5} 345 10 10 re S']
    start = 500 - length
    for i in range(segments):
        a, b = start + length * i / segments, start + length * (i + 1) / segments
        commands.append(f'{a} 350 m {b} 350 l S')
    commands += ['494 346 m 500 350 l 494 354 l S']
    if wall:
        # Paired source faces form a positively classified open wall chain.
        commands += ['300 200 m 300 500 l S', '303 200 m 303 500 l S',
                     '300 500 m 420 500 l S', '300 503 m 420 503 l S']
    if crossing:
        commands += ['300 200 m 300 500 l S']
    commands.extend(extra)
    stream = DecodedStreamObject()
    stream.set_data(('\n'.join(commands) + '\n').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)
    assert not path.with_suffix('.expected.json').exists()
    return path


def inputs(path):
    electrical = extract_pdf(path)
    architecture = architecture_extract(path)
    evidence = source_wall_evidence(architecture, page_number=1, confidence=.9,
        meters_per_point=.0254 * 96 / 72)
    seeds = tuple(v.element_id for v in electrical.vectors if _homerun_arrowhead_apex(v) is not None)
    # Candidate IDs come from actual extracted straight paths, not answer data.
    eligible = tuple(v.element_id for v in electrical.vectors if not v.closed and v.element_id not in seeds)
    return electrical, architecture, evidence, seeds, eligible


def evaluate(path):
    electrical, architecture, evidence, seeds, eligible = inputs(path)
    return discriminate_branch_geometry(electrical, seed_vector_ids=seeds,
        eligible_vector_ids=eligible, wall_evidence=evidence, architecture_document=architecture)


@pytest.mark.parametrize('segments,length', [(6,350), (65,80), (350,350)])
def test_long_and_dense_run_reaches_device_without_count_or_reach_cap(tmp_path, segments, length):
    path = sheet(tmp_path/'run.pdf', segments=segments, length=length)
    result = evaluate(path)
    assert result.status == 'eligible'
    assert len(result.vector_ids) == segments
    assert result.architecture_evidence_status == 'supplied_empty'
    model = ElectricalPdfImporter().import_document(extract_pdf(path))
    assert [c.circuit_number for c in model.circuits] == ['1']
    assert len(model.circuits[0].load_port_ids) == 1
    assert len(model.ports) == 2
    # This boundary returns real source IDs, never manufactured circuit ports.
    doc = extract_pdf(path)
    vectors = {v.element_id: v for v in doc.vectors}
    assert min(min(point[0] for point in vectors[k].points_pt) for k in result.vector_ids) == 500-length


def test_classified_open_chain_interior_crossing_rejects_whole_run(tmp_path):
    path = sheet(tmp_path/'open-chain.pdf', wall=True)
    result = evaluate(path)
    assert result.status == 'unresolved'
    assert result.reason_code == 'enters_classified_wall_chain'
    assert result.wall_chain_ids and result.wall_source_element_ids
    # Interior contact matters even if endpoint-only assembly never absorbed wall.
    assert len(result.vector_ids) == 6
    electrical, architecture, evidence, seeds, eligible = inputs(path)
    assert evidence.chains and any(chain.open for chain in evidence.chains)
    assert result == discriminate_branch_geometry(replace(electrical, vectors=tuple(reversed(electrical.vectors))),
        seed_vector_ids=tuple(reversed(seeds)), eligible_vector_ids=tuple(reversed(eligible)),
        wall_evidence=evidence, architecture_document=architecture)


def test_unclassified_architecture_is_disclosed_not_silently_certified(tmp_path):
    path = sheet(tmp_path/'unclassified.pdf', crossing=True)
    doc, arch, evidence, seeds, eligible = inputs(path)
    result = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible)
    assert result.status == 'eligible'
    assert result.architecture_evidence_status == 'missing_handoff'


@pytest.mark.parametrize('change,match', [('sha','sha256'), ('page','page'), ('ids','missing source'), ('frame','frame')])
def test_stale_wrong_page_missing_sources_and_wrong_frame_refuse(tmp_path, change, match):
    path = sheet(tmp_path/'bound.pdf', wall=True)
    doc, arch, evidence, seeds, eligible = inputs(path)
    if change == 'sha':
        evidence = replace(evidence, content_sha256='0'*64)
    elif change == 'page':
        evidence = replace(evidence, page_number=2)
    elif change == 'ids':
        chain = evidence.chains[0]
        segment = replace(chain.segments[0], source_element_ids=('missing-source',))
        evidence = replace(evidence, chains=(replace(chain, segments=(segment,)),))
    else:
        evidence = replace(evidence, frame_pt=(0.,0.,900.,900.))
    with pytest.raises(WallEvidenceHandoffError, match=match):
        discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
            wall_evidence=evidence, architecture_document=arch)


def test_dimension_style_arrow_without_device_never_yields_circuits(tmp_path):
    path = sheet(tmp_path/'dimension.pdf', device=False)
    # Traversal eligibility does not promote an arrow into a device or circuit.
    result = evaluate(path)
    assert result.status == 'eligible'
    model = ElectricalPdfImporter().import_document(extract_pdf(path))
    assert not model.electrical_devices and not model.circuits and not model.ports


def test_collinear_classified_wall_is_not_removed_before_conflict_check(tmp_path):
    path = sheet(tmp_path/'coincident-wall.pdf', wall=True,
        extra=('295.5 346 m 301.5 350 l 295.5 354 l S',))
    doc, arch, evidence, seeds, eligible = inputs(path)
    # This arrow touches the classified wall's own face strokes. They must
    # remain positive conflicting evidence even with no other branch stroke.
    seed = next(v.element_id for v in doc.vectors
                if _homerun_arrowhead_apex(v) == (301.5,350.))
    result = discriminate_branch_geometry(doc, seed_vector_ids=(seed,),
        eligible_vector_ids=eligible, wall_evidence=evidence, architecture_document=arch)
    assert result.status == 'unresolved'
    assert result.reason_code == 'enters_classified_wall_chain'


def test_own_recognized_closed_symbol_is_not_added_to_run(tmp_path):
    path = sheet(tmp_path/'own-symbol.pdf')
    doc, arch, evidence, seeds, eligible = inputs(path)
    outlines = tuple(v.element_id for v in doc.vectors if v.closed)
    result = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
        own_symbol_outline_ids=outlines, wall_evidence=evidence, architecture_document=arch)
    assert result.status == 'eligible'
    assert not set(outlines) & set(result.vector_ids)


@pytest.mark.parametrize('probe', [
    ('200 200 12 12 re S',),
    ('200 200 m 210 220 l 220 200 l h S', '210 200 m 210 180 l S'),
    ('200 200 14 14 re S', '200 200 m 214 214 l S'),
])
def test_non_device_symbol_probes_do_not_materialize_loads(tmp_path, probe):
    path = sheet(tmp_path/'non-device.pdf', device=False, extra=probe)
    doc, arch, evidence, seeds, eligible = inputs(path)
    result = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
        wall_evidence=evidence, architecture_document=arch)
    assert result.status == 'eligible'
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=evidence)
    assert not model.electrical_devices and not model.circuits and not model.ports


def test_disconnected_arrow_seeds_cannot_merge_two_runs(tmp_path):
    path = sheet(tmp_path/'two-runs.pdf', extra=(
        '550 200 m 650 200 l S', '644 196 m 650 200 l 644 204 l S'))
    result = evaluate(path)
    assert result.status == 'unresolved'
    assert result.reason_code == 'disconnected_branch_seeds'


def test_incompatible_display_frame_refuses(tmp_path):
    path = sheet(tmp_path/'display.pdf', wall=True)
    doc, arch, evidence, seeds, eligible = inputs(path)
    changed = dict(doc.page_provenance[1], coordinate_space='raw')
    with pytest.raises(WallEvidenceHandoffError, match='displayed page frame'):
        discriminate_branch_geometry(replace(doc, page_provenance={1: changed}),
            seed_vector_ids=seeds, eligible_vector_ids=eligible,
            wall_evidence=evidence, architecture_document=arch)


def test_traversal_does_not_mutate_source_or_default_model(tmp_path):
    path = sheet(tmp_path/'unchanged.pdf')
    doc, arch, evidence, seeds, eligible = inputs(path)
    before = ElectricalPdfImporter().import_document(doc).to_json()
    result = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
        wall_evidence=evidence, architecture_document=arch)
    assert result.wall_evidence == evidence
    assert ElectricalPdfImporter().import_document(doc).to_json() == before


@pytest.mark.parametrize('crossing', [False, True])
def test_missing_and_empty_handoffs_distinct_without_changing_traversal(tmp_path, crossing):
    path = sheet(tmp_path/'coverage-gap.pdf', crossing=crossing)
    doc, arch, evidence, seeds, eligible = inputs(path)
    assert not evidence.chains, 'actual architecture classifier supplies no wall claim'
    missing = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible)
    empty = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
        wall_evidence=evidence, architecture_document=arch)
    assert missing.architecture_evidence_status == 'missing_handoff'
    assert empty.architecture_evidence_status == 'supplied_empty'
    assert missing.wall_evidence is None and empty.wall_evidence == evidence
    # The optional open wall-like stroke crosses the run but has no positive
    # source classification. Empty evidence must not certify it as wiring.
    assert missing.status == empty.status == 'eligible'
    assert missing.reason_code is empty.reason_code is None
    assert replace(empty, architecture_evidence_status='missing_handoff', wall_evidence=None) == missing
    if crossing:
        assert any(v.points_pt == ((300.,200.),(300.,500.)) for v in doc.vectors)


@pytest.mark.parametrize('length,decision', [(350, 'unresolved'), (80, 'eligible')])
def test_positive_handoff_status_is_not_a_clearance_or_coverage_verdict(tmp_path, length, decision):
    path = sheet(tmp_path/'partial-coverage.pdf', wall=True, length=length)
    doc, arch, evidence, seeds, eligible = inputs(path)
    assert evidence.chains
    result = discriminate_branch_geometry(doc, seed_vector_ids=seeds, eligible_vector_ids=eligible,
        wall_evidence=evidence, architecture_document=arch)
    assert result.architecture_evidence_status == 'supplied_positive'
    assert result.status == decision
    assert result.wall_evidence == evidence
    assert bool(result.wall_chain_ids) == (decision == 'unresolved')
    assert result.reason_code == ('enters_classified_wall_chain' if decision == 'unresolved' else None)
