"""Oversized printed samples must not widen field-object recognition."""
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf


def _source(path: Path, *, heading='LEGEND', sample_width=72, rotation=0, background=False, fragment=False):
    writer=PdfWriter();page=writer.add_blank_page(
        width=800 if rotation in (90,270) else 900,
        height=900 if rotation in (90,270) else 800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
        NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    def text(x,y,s):return f'BT /F1 8 Tf 1 0 0 1 {x} {y} Tm ({s}) Tj ET'
    def box(x,y,w,h):return f'{x-w/2} {y-h/2} {w} {h} re S'
    def troffer(x,y,w,h):
        if fragment:
            return [f'{x-7} {y-.5} m {x+7} {y-.5} l S',f'{x-7} {y+.5} m {x+7} {y+.5} l S']
        masks=[]
        if background and x < 400:
            masks=['q 1 g 1 G',
                f'{x-w/2} {y-h/2} m {x+w/2} {y-h/2} l {x-w/2} {y+h/2} l h b',
                f'{x+w/2} {y-h/2} m {x-w/2} {y+h/2} l {x+w/2} {y+h/2} l h b','Q']
        return [*masks,box(x,y,w,h),box(x,y,w-4,h-4)]
    def triangle(x,y):return f'{x-6} {y-5} m {x+6} {y-5} l {x} {y+6} l h S'
    commands=[text(600,755,heading),box(610,710,12,12),box(610,710,8,8),text(660,710,'RECESSED DOWNLIGHT'),
        *troffer(610,660,sample_width,30),text(660,660,'RECESSED TROFFER'),
        triangle(610,610),text(660,610,'DECORATIVE PENDANT'),
        *troffer(140,480,36,15),*troffer(280,480,36,15),
        box(140,350,36,15),box(280,350,36,15), # Plain grid cells, not double-border luminaires.
        '100 300 m 320 300 l S','100 315 m 320 315 l S',
        '140 280 m 140 330 l S','176 280 m 176 330 l S']
    # Inverse content transform preserves the displayed sheet while exercising /Rotate.
    if rotation==90:commands=['q 0 1 -1 0 800 0 cm',*commands,'Q']
    elif rotation==270:commands=['q 0 -1 1 0 0 900 cm',*commands,'Q']
    stream=DecodedStreamObject();stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    if rotation:page.rotate(rotation)
    writer.write(path)


@pytest.mark.parametrize('sample_width',[58,72,88])
@pytest.mark.parametrize('rotation',[0,90,270])
def test_large_legend_sample_matches_only_supported_field_symbols(tmp_path,sample_width,rotation):
    path=tmp_path/'large-sample.pdf';_source(path,sample_width=sample_width,rotation=rotation)
    assert not path.with_suffix('.expected.json').exists()
    model=ElectricalPdfImporter().import_document(extract_pdf(path))
    devices=[d for d in model.electrical_devices if d.device_type=='luminaire']
    assert len(devices)==2
    positions=[d.attributes['pdf_electrical']['source_position_pt'] for d in devices]
    assert sorted(round(p['x']) for p in positions)==[140,280]
    assert all(d.attributes['pdf_electrical']['source_page']==1 for d in devices)
    assert all('TROFFER' in d.attributes['pdf_electrical']['legend_row_label'] for d in devices)
    assert all(d.attributes['pdf_electrical'].get('fixture_tag') is None for d in devices)
    assert not model.circuits and not model.ports


@pytest.mark.parametrize('heading',['GENERAL NOTES','PLUMBING FIXTURE SCHEDULE','PLUMBING FIXTURE LEGEND'])
def test_large_samples_without_confirmed_electrical_legend_stay_unplaced(tmp_path,heading):
    path=tmp_path/'not-a-legend.pdf';_source(path,heading=heading)
    model=ElectricalPdfImporter().import_document(extract_pdf(path))
    assert not model.electrical_devices


def test_sample_outside_bounded_recovery_extent_is_not_guessed(tmp_path):
    path=tmp_path/'unsupported-sample.pdf';_source(path,sample_width=144)
    model=ElectricalPdfImporter().import_document(extract_pdf(path))
    assert not model.electrical_devices


def test_large_sample_replay_preserves_identity_and_does_not_invent_dimensions(tmp_path):
    path=tmp_path/'repeat.pdf';_source(path)
    document=extract_pdf(path)
    first=ElectricalPdfImporter().import_document(document)
    second=ElectricalPdfImporter().import_document(document)
    assert first.to_json()==second.to_json()
    assert len(first.electrical_devices)==2
    assert all(d.size is None and d.rated_voltage_v is None for d in first.electrical_devices)
    assert all(d.provenance for d in first.electrical_devices)


def test_white_cad_knockout_triangles_do_not_hide_real_double_borders(tmp_path):
    path=tmp_path/'knockout.pdf';_source(path,background=True)
    model=ElectricalPdfImporter().import_document(extract_pdf(path))
    assert len(model.electrical_devices)==2
    assert all('TROFFER' in d.attributes['pdf_electrical']['legend_row_label'] for d in model.electrical_devices)


def test_partial_dashed_prototype_cannot_classify_matching_nonfixture_fragments(tmp_path):
    path=tmp_path/'incomplete-border.pdf';_source(path,fragment=True)
    model=ElectricalPdfImporter().import_document(extract_pdf(path))
    assert not model.electrical_devices
    problems=model.attributes['pdf_electrical']['unresolved_observations']
    assert any(candidate.get('geometry_refusal')=='troffer_border_topology_unsupported'
               for problem in problems for candidate in problem.get('classification_candidates',[]))
