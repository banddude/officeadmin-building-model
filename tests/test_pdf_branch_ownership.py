"""Source-PDF positive and negative ownership controls for #377."""
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NumberObject, RectangleObject, TextStringObject

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


@pytest.mark.parametrize('mark', [
    '460 350 m 470 330 480 300 490 250 c S',
    '460 350 80 90 re S',
    'q 2 w 460 350 m 466 354 l 460 358 l S Q',
])
@pytest.mark.parametrize('direct_tag', [False, True])
def test_unknown_curve_or_closed_path_refuses_complete_run(tmp_path, mark, direct_tag):
    path = sheet(tmp_path/'unknown-mark.pdf', length=80, extra=(*LEGEND, mark))
    if direct_tag:
        rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    doc, model = run(path)
    assert any(v.closed or v.metadata.get('curve_commands') for v in doc.vectors)
    assert not model.circuits and not model.ports
    assert any(r.get('reason_code') == 'branch_ownership_unresolved'
               for r in model.attributes['pdf_electrical']['unresolved_circuits'])


@pytest.mark.parametrize('mode', ['hidden', 'hidden_reset_before_ET', 'off_page', 'white', 'alpha', 'clip'])
def test_unpainted_or_out_of_frame_legend_cannot_supply_ownership(tmp_path, mode):
    path = sheet(tmp_path/'invisible-legend.pdf', length=80, extra=LEGEND)
    if mode in ('hidden', 'hidden_reset_before_ET', 'white'):
        for text in (b'(LINE LEGEND)', b'(ELECTRICAL BRANCH WIRING)'):
            if mode == 'hidden_reset_before_ET':
                rewrite(path, text + b' Tj ET', b'3 Tr ' + text + b' Tj 0 Tr ET')
            else:
                prefix, suffix = (b'3 Tr ', b' 0 Tr') if mode == 'hidden' else (b'1 g ', b' 0 g')
                rewrite(path, text + b' Tj ET', prefix + text + b' Tj ET' + suffix)
    elif mode == 'off_page':
        rewrite(path, b'675 Td (LINE LEGEND)', b'750 Td (LINE LEGEND)')
        rewrite(path, b'650 Td (ELECTRICAL', b'725 Td (ELECTRICAL')
        rewrite(path, b'20 650 m 80 650 l S', b'20 725 m 80 725 l S')
    elif mode == 'clip':
        rewrite(path, b'(LINE LEGEND) Tj ET', b'0 0 1 1 re W n (LINE LEGEND) Tj ET')
    else:
        reader = PdfReader(path)
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        state = DictionaryObject({NameObject('/Type'): NameObject('/ExtGState'),
                                  NameObject('/ca'): FloatObject(0)})
        page['/Resources'][NameObject('/ExtGState')] = DictionaryObject({
            NameObject('/Hidden'): writer._add_object(state)})
        stream = DecodedStreamObject()
        stream.set_data(page.get_contents().get_data().replace(
            b'(LINE LEGEND) Tj ET', b'/Hidden gs (LINE LEGEND) Tj ET'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(path)
    _, model = run(path)
    assert not model.circuits and not model.ports


def test_named_text_scalar_facts_keep_observed_scopes_and_ownership_support(tmp_path):
    path = sheet(tmp_path/'scalar-facts.pdf', length=80, extra=LEGEND)
    rewrite(path, b'(LP-1)', b'(PANEL LP CIRCUIT 1 EVSE-1 240V 2P 1PH)')
    rewrite(path, b'494 346 m 500 350 l 494 354 l S', b'')
    _, model = run(path)
    circuit, = model.circuits
    assert circuit.voltage_v == 240 and circuit.poles == 2 and circuit.phase == '1ph'
    for field in ('circuit_number', 'voltage_v', 'poles', 'phase'):
        assert any(record.derivation == 'observed' and provenance_applies_to(record, (field,))
                   for record in circuit.provenance), field
    assert any(record.method == 'pdf-source-legend-branch-membership'
               for record in circuit.provenance)


def test_direct_tag_keeps_actual_ownership_support(tmp_path):
    path = sheet(tmp_path/'direct-support.pdf', length=80, extra=LEGEND)
    rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    rewrite(path, b'494 346 m 500 350 l 494 354 l S', b'')
    _, model = run(path)
    assert model.circuits
    assert any(record.method == 'pdf-source-legend-branch-membership'
               for circuit in model.circuits for record in circuit.provenance)


def test_topology_keeps_actual_ownership_support(tmp_path):
    path = sheet(tmp_path/'topology-support.pdf', length=80, extra=LEGEND)
    rewrite(path, b'494 346 m 500 350 l 494 354 l S', b'')
    rewrite(path, b'70 600 Td (PANEL LP', b'500 350 Td (PANEL LP')
    _, model = run(path)
    assert model.circuits
    assert any(r.method == 'pdf-vector-topology-link' for c in model.circuits for r in c.provenance)
    assert all(any(r.method == 'pdf-source-legend-branch-membership' for r in c.provenance)
               for c in model.circuits)


def test_ownership_coverage_uses_selected_original_pages(tmp_path):
    path = sheet(tmp_path/'selected.pdf', length=80, extra=LEGEND)
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.add_page(reader.pages[0])
    writer.write(path)
    doc, arch, walls, _, _ = inputs(path)
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=walls,
        architecture_document=arch, selected_pages=[1],
        excluded_page_reasons={2: 'independent duplicate excluded by caller'})
    assert len(model.circuits) == 1
    assert {row['page'] for row in model.attributes['pdf_electrical']['branch_ownership_checks']} == {1}


@pytest.mark.parametrize('pathway', ['homerun', 'direct_tag', 'named_text', 'unarrowed'])
def test_enclosure_of_separate_owned_strokes_cannot_be_rescued(tmp_path, pathway):
    path = sheet(tmp_path/'open-stroke-enclosure.pdf', length=80, extra=(*LEGEND,
        '460 350 m 540 350 l S', '540 350 m 500 440 l S', '500 440 m 460 350 l S'))
    if pathway == 'direct_tag':
        rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    elif pathway == 'named_text':
        rewrite(path, b'(LP-1)', b'(PANEL LP CIRCUIT 1 EVSE-1)')
    elif pathway == 'unarrowed':
        rewrite(path, b'494 346 m 500 350 l 494 354 l S', b'')
        rewrite(path, b'510 352 Td (LP-1)', b'425 358 Td (LP-1)')
    doc, arch, walls, _, _ = inputs(path)
    assert not walls.chains, 'the triangle must not rely on wall classification'
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=walls, architecture_document=arch)
    assert not model.circuits and not model.ports
    assert any(row.get('reason_code') == 'branch_run_not_isolated'
               for row in model.attributes['pdf_electrical']['unresolved_circuits'])


@pytest.mark.parametrize('visibility', ['cropped', 'optional_off', 'equal_crop'])
def test_crop_and_optional_content_visibility_boundaries(tmp_path, visibility):
    path = sheet(tmp_path/'visibility.pdf', length=80, extra=LEGEND)
    reader = PdfReader(path)
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    if visibility in ('cropped', 'equal_crop'):
        page[NameObject('/CropBox')] = RectangleObject([0, 0, 800,
            630 if visibility == 'cropped' else 700])
    else:
        group = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/OCG'),
            NameObject('/Name'): TextStringObject('Synthetic hidden legend')}))
        page['/Resources'][NameObject('/Properties')] = DictionaryObject({NameObject('/HiddenLegend'): group})
        writer._root_object[NameObject('/OCProperties')] = DictionaryObject({
            NameObject('/OCGs'): ArrayObject([group]),
            NameObject('/D'): DictionaryObject({NameObject('/OFF'): ArrayObject([group])})})
        stream = DecodedStreamObject()
        content = page.get_contents().get_data()
        start = b'BT /F1 8 Tf 20 675 Td (LINE LEGEND) Tj ET'
        end = b'BT /F1 8 Tf 90 650 Td (ELECTRICAL BRANCH WIRING) Tj ET'
        assert start in content and end in content
        stream.set_data(content.replace(start, b'/OC /HiddenLegend BDC ' + start)
                        .replace(end, end + b' EMC'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)
    doc, arch, walls, _, _ = inputs(path)
    legacy = ElectricalPdfImporter().import_document(doc)
    assert legacy.circuits and legacy.ports  # Evidence policy is strict-path only.
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=walls, architecture_document=arch)
    if visibility == 'equal_crop':
        assert len(model.circuits) == 1 and len(model.ports) == 2
    else:
        assert not model.circuits and not model.ports
        assert any(row.get('reason_code') == 'branch_ownership_unresolved'
                   for row in model.attributes['pdf_electrical']['unresolved_circuits'])


@pytest.mark.parametrize('container', ['clipped_form', 'visible_form', 'direct_page'])
def test_legend_form_bbox_visibility_is_not_assumed_from_extracted_text(tmp_path, container):
    path = sheet(tmp_path/'form-legend.pdf', length=80, extra=LEGEND)
    if container != 'direct_page':
        reader = PdfReader(path)
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        content = page.get_contents().get_data()
        legend = ('\n'.join(LEGEND) + '\n').encode()
        assert legend in content
        form = DecodedStreamObject()
        form.set_data(legend)
        form.update({NameObject('/Type'): NameObject('/XObject'),
            NameObject('/Subtype'): NameObject('/Form'),
            NameObject('/BBox'): RectangleObject([0, 0, 1, 1] if container == 'clipped_form'
                                                 else [0, 0, 800, 700]),
            NameObject('/Matrix'): ArrayObject([FloatObject(v) for v in (1, 0, 0, 1, 0, 0)]),
            NameObject('/Resources'): DictionaryObject({
                NameObject('/Font'): page['/Resources']['/Font']})})
        page['/Resources'][NameObject('/XObject')] = DictionaryObject({
            NameObject('/LegendForm'): writer._add_object(form)})
        stream = DecodedStreamObject()
        stream.set_data(content.replace(legend, b'/LegendForm Do\n'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(path)
    doc, arch, walls, _, _ = inputs(path)
    assert any(text.text == 'ELECTRICAL BRANCH WIRING' for text in doc.texts)
    assert ElectricalPdfImporter().import_document(doc).circuits
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=walls, architecture_document=arch)
    if container == 'direct_page':
        assert len(model.circuits) == 1 and len(model.ports) == 2
    else:
        assert not model.circuits and not model.ports
        assert any(row.get('reason_code') == 'branch_ownership_unresolved'
                   for row in model.attributes['pdf_electrical']['unresolved_circuits'])


@pytest.mark.parametrize('paint', ['fill_cover', 'collapsed', 'raised', 'degenerate',
    'white_strokes', 'image_cover', 'shading_cover', 'thick_stroke_cover', 'custom_widths'])
def test_only_supported_visible_source_subset_can_qualify_legend(tmp_path, paint):
    extra = (*LEGEND, '0 640 400 60 re f') if paint == 'fill_cover' else LEGEND
    if paint == 'thick_stroke_cover':
        extra = (*LEGEND, 'q 60 w 0 670 m 400 670 l S Q')
    path = sheet(tmp_path/'paint-subset.pdf', length=80, extra=extra)
    if paint in ('collapsed', 'raised'):
        before, after = ((b'0 Tz ', b' 100 Tz') if paint == 'collapsed'
                         else (b'1000 Ts ', b' 0 Ts'))
        for text in (b'(LINE LEGEND)', b'(ELECTRICAL BRANCH WIRING)'):
            rewrite(path, text + b' Tj ET', before + text + b' Tj' + after + b' ET')
    elif paint == 'degenerate':
        rewrite(path, b'20 675 Td', b'0 0 0 0 20 675 Tm')
        rewrite(path, b'90 650 Td', b'0 0 0 0 90 650 Tm')
    elif paint in ('white_strokes', 'image_cover', 'shading_cover', 'custom_widths'):
        reader = PdfReader(path)
        writer = PdfWriter()
        page = writer.add_page(reader.pages[0])
        content = page.get_contents().get_data()
        if paint == 'white_strokes':
            content = b'1 G\n' + content
        elif paint == 'image_cover':
            bitmap = DecodedStreamObject()
            bitmap.set_data(b'\x00')
            bitmap.update({NameObject('/Type'): NameObject('/XObject'),
                NameObject('/Subtype'): NameObject('/Image'), NameObject('/Width'): NumberObject(1),
                NameObject('/Height'): NumberObject(1), NameObject('/BitsPerComponent'): NumberObject(8),
                NameObject('/ColorSpace'): NameObject('/DeviceGray')})
            page['/Resources'][NameObject('/XObject')] = DictionaryObject({NameObject('/Cover'): writer._add_object(bitmap)})
            content += b'q 400 0 0 60 0 640 cm /Cover Do Q\n'
        elif paint == 'shading_cover':
            function = DictionaryObject({NameObject('/FunctionType'): NumberObject(2),
                NameObject('/Domain'): ArrayObject([NumberObject(0), NumberObject(1)]),
                NameObject('/C0'): ArrayObject([NumberObject(0)]),
                NameObject('/C1'): ArrayObject([NumberObject(0)]), NameObject('/N'): NumberObject(1)})
            shading = DictionaryObject({NameObject('/ShadingType'): NumberObject(2),
                NameObject('/ColorSpace'): NameObject('/DeviceGray'),
                NameObject('/Coords'): ArrayObject([NumberObject(v) for v in (0, 640, 400, 700)]),
                NameObject('/BBox'): RectangleObject([0, 640, 400, 700]),
                NameObject('/Function'): function,
                NameObject('/Extend'): ArrayObject([BooleanObject(True), BooleanObject(True)])})
            page['/Resources'][NameObject('/Shading')] = DictionaryObject({NameObject('/Cover'): writer._add_object(shading)})
            content += b'/Cover sh\n'
        else:
            font = page['/Resources']['/Font']['/F1']
            font[NameObject('/FirstChar')] = NumberObject(32)
            font[NameObject('/LastChar')] = NumberObject(126)
            font[NameObject('/Widths')] = ArrayObject([NumberObject(0) for _ in range(95)])
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(path)
    doc, arch, walls, _, _ = inputs(path)
    assert any(text.text == 'ELECTRICAL BRANCH WIRING' for text in doc.texts)
    if paint == 'thick_stroke_cover':
        assert all(text.branch_legend_paint_supported is True for text in doc.texts)
    assert ElectricalPdfImporter().import_document(doc).circuits
    model = ElectricalPdfImporter().import_document(doc, wall_evidence=walls, architecture_document=arch)
    assert not model.circuits and not model.ports
    assert any(row.get('reason_code') == 'branch_ownership_unresolved'
               for row in model.attributes['pdf_electrical']['unresolved_circuits'])


def test_explicit_normal_text_state_and_nondegenerate_transform_are_supported(tmp_path):
    path = sheet(tmp_path/'normal-state.pdf', length=80, extra=LEGEND)
    rewrite(path, b'20 675 Td', b'1 0 0 1 20 675 Tm')
    rewrite(path, b'90 650 Td', b'1 0 0 1 90 650 Tm')
    for text in (b'(LINE LEGEND)', b'(ELECTRICAL BRANCH WIRING)'):
        rewrite(path, text + b' Tj', b'0 Tr 100 Tz 0 Ts 0 Tc 0 Tw ' + text + b' Tj')
    doc, model = run(path)
    assert all(text.branch_legend_paint_supported is True for text in doc.texts)
    assert len(model.circuits) == 1 and len(model.ports) == 2


@pytest.mark.parametrize('overlap', [True, False])
def test_default_miter_join_bound_cannot_hide_legend_overpaint(tmp_path, overlap):
    offset = 0 if overlap else 500
    path = sheet(tmp_path/'miter-paint.pdf', length=80, extra=(*LEGEND,
        f'q 20 w {110+offset} 600 m {120+offset} 630 l {130+offset} 600 l S Q'))
    doc, model = run(path)
    assert all(text.branch_legend_paint_supported is True for text in doc.texts)
    if overlap:
        assert not model.circuits and not model.ports
        assert any(row.get('reason_code') == 'branch_ownership_unresolved'
                   for row in model.attributes['pdf_electrical']['unresolved_circuits'])
    else:
        assert len(model.circuits) == 1 and len(model.ports) == 2
