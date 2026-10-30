import json
from dataclasses import replace
from pathlib import Path
import io

import pytest
from pypdf import PdfReader
from oabm.drawings.cover_sheet import paginate_cover, create_cover_sheet, compose_cover_body
from oabm.drawings.company_sheets import resolve_company_sheet_set, render_company_sheet_svg

ROOT = Path(__file__).parents[1]
PROFILE = ROOT/'fixtures/company_sheets/v1/profile.json'
JOB = ROOT/'examples/cover-sheet/job.json'


def request(kind):
    config = json.loads((ROOT/'examples/cover-sheet/input.json').read_text())
    if kind == 'contacts':
        config['contacts'] = [{'role':'Designer','name':f'SYNTHETIC CONTACT {i}',
                               'email':f'contact{i}@example.invalid'} for i in range(160)]
    elif kind == 'scope':
        config['scope'] = [f'SYNTHETIC supplied scope {i}. '+('Explicit supplied description. '*5) for i in range(200)]
    return config


def assert_complete(plan):
    actual = {}
    for page in plan.pages:
        for panel in page.layout['panels']:
            for span in panel.get('source_spans',[]):
                value = actual.setdefault(span['source'], '')
                assert len(value) == span['offset']
                actual[span['source']] += span['text']
    assert actual == plan.pages[0].layout['source_text']


@pytest.mark.parametrize('kind', ['contacts','scope'])
def test_large_content_has_complete_stable_bounded_continuations(kind):
    config = request(kind)
    sheet = resolve_company_sheet_set(PROFILE,JOB).sheets[0]
    plan = paginate_cover(config,sheet.drawing_bounds_mm)
    assert len(plan.pages) > 1
    assert plan == paginate_cover(config,sheet.drawing_bounds_mm)
    assert plan.iterations >= 2
    assert_complete(plan)
    assert [r['id'] for r in plan.sheet_index[:len(plan.pages)]] == [p.id for p in plan.pages]
    for i,page in enumerate(plan.pages):
        svg = render_company_sheet_svg(replace(sheet,id=page.id,title=page.title),body_svg=page.body_svg)
        assert page.id in svg
        assert page.layout['text_height_mm'] == plan.pages[0].layout['text_height_mm'] >= 3.5
        if i:
            assert f'COVER CONTINUATION {i+1} OF {len(plan.pages)}' in svg
    index_text = ''.join(span['text'] for page in plan.pages for panel in page.layout['panels']
                         for span in panel.get('source_spans',[]) if span['source'].startswith('sheets['))
    for row in plan.sheet_index:
        assert f"{row['id']}  {row['title']}" in index_text


def test_small_cover_keeps_original_body():
    config = request('small')
    config['pagination'] = {'text_height_mm':3}
    sheet = resolve_company_sheet_set(PROFILE,JOB).sheets[0]
    plan = paginate_cover(config,sheet.drawing_bounds_mm)
    body = compose_cover_body(config,sheet.drawing_bounds_mm)
    assert len(plan.pages) == 1 and plan.iterations == 1
    assert plan.pages[0].body_svg == '<g xmlns="http://www.w3.org/2000/svg">'+body.svg_elements+'</g>'
    assert list(plan.sheet_index) == config['sheets']


def test_caller_ids_prefix_and_collisions():
    config = request('contacts')
    bounds = resolve_company_sheet_set(PROFILE,JOB).sheets[0].drawing_bounds_mm
    config['pagination'] = {'continuation_ids':['ZZ01','ZZ02','ZZ03'], 'continuation_title':'Cover supplement'}
    plan = paginate_cover(config,bounds)
    assert plan.pages[1].id == 'ZZ01' and plan.pages[1].title == 'Cover supplement 2'
    config['pagination'] = {'continuation_id_prefix':'X', 'text_height_mm':4}
    plan = paginate_cover(config,bounds)
    assert plan.pages[1].id == 'X2' and plan.pages[1].layout['text_height_mm'] == 4
    config['pagination'] = {'continuation_ids':['E101']}
    with pytest.raises(ValueError,match='collides'):
        paginate_cover(config,bounds)
    config['pagination'] = {'continuation_ids':[]}
    with pytest.raises(ValueError,match='supply more caller IDs'):
        paginate_cover(config,bounds)
    config['pagination'] = {'continuation_ids':['XX','xx']}
    with pytest.raises(ValueError,match='duplicate'):
        paginate_cover(config,bounds)


def test_coordinator_expanded_index_stabilizes_with_own_cover_pages():
    config = request('scope')
    index = [*config['sheets'], *[{'id':f'Q{i:04}', 'title':f'Caller detail {i}'} for i in range(450)]]
    bounds = resolve_company_sheet_set(PROFILE,JOB).sheets[0].drawing_bounds_mm
    plan = paginate_cover(config,bounds,sheet_index=index)
    assert len(plan.pages) > 2
    assert len(plan.sheet_index) == len(index)+len(plan.pages)-1
    assert_complete(plan)
    index_text = ''.join(span['text'] for p in plan.pages for panel in p.layout['panels']
                         for span in panel.get('source_spans',[]) if span['source'].startswith('sheets['))
    for page in plan.pages:
        assert f'{page.id}  {page.title}' in index_text


@pytest.mark.parametrize('kind', ['contacts','scope'])
def test_standalone_large_svg_pdf_retains_full_input_and_derived_index(kind,tmp_path,sheet_pdf_runtime):
    config = request(kind)
    source = tmp_path/'cover.json';raw=json.dumps(config).encode();source.write_bytes(raw)
    output = create_cover_sheet(source,profile_path=PROFILE,job_path=JOB,output_directory=tmp_path/'out')
    manifest = json.loads((output/'COVER-MANIFEST.json').read_text())
    shared = output/'company/delivery'
    sm = json.loads((shared/'MANIFEST.json').read_text())
    assert sm['sheet_order'] == [r['id'] for r in manifest['sheet_index']]
    assert len(manifest['cover_pages']) > 1
    pdf = PdfReader(io.BytesIO((shared/'sheet-set.pdf').read_bytes()))
    assert len(pdf.pages) == len(manifest['sheet_index'])
    text = ' '.join(' '.join(page.extract_text().split()) for page in pdf.pages[:len(manifest['cover_pages'])])
    for i in range(160 if kind == 'contacts' else 200):
        assert (f'SYNTHETIC CONTACT {i}' if kind == 'contacts' else f'SYNTHETIC supplied scope {i}.') in text
    for row in manifest['sheet_index']:
        assert row['id'] in text and row['title'] in text
    assert (output/'cover-input.json').read_bytes() == raw == source.read_bytes()
    assert not any(list(p.images) for p in pdf.pages)


def test_large_notes_long_fact_and_supplied_map_preserve_all_sources():
    config = request('contacts')
    config['scope'] = [('W  M @ '*800)]
    config['notes'] = [f'Caller note {i}. Preserve complete supplied text.' for i in range(220)]
    config['location_map'] = {'caption':'Supplied vector location diagram', 'width':100, 'height':60,
                              'segments':[[0,0,100,60],[0,60,100,0]]}
    sheet = resolve_company_sheet_set(PROFILE,JOB).sheets[0]
    raw = json.dumps(config,sort_keys=True)
    plan = paginate_cover(config,sheet.drawing_bounds_mm)
    assert len(plan.pages) > 2
    assert_complete(plan)
    for page in plan.pages:
        render_company_sheet_svg(replace(sheet,id=page.id,title=page.title),body_svg=page.body_svg)
    assert any(panel['title'] == 'Location map diagram' for p in plan.pages for panel in p.layout['panels'])
    assert json.dumps(config,sort_keys=True) == raw


@pytest.mark.parametrize('kind', ['small','contacts'])
@pytest.mark.parametrize('change', ['job_title','profile_field','template','logo'])
def test_cover_rejects_transient_shared_input_changes(kind,change,tmp_path,monkeypatch):
    from PIL import Image
    from oabm.drawings import company_sheets as cs
    config=request(kind)
    source=tmp_path/'cover.json';source.write_text(json.dumps(config))
    profile=json.loads(PROFILE.read_bytes());job=json.loads(JOB.read_bytes());job['formats']=['svg']
    logo=tmp_path/'logo.png'
    def png(color):
        stream=io.BytesIO();Image.new('RGB',(8,8),color).save(stream,format='PNG');return stream.getvalue()
    logo.write_bytes(png('red'));profile['logos']={'icon':logo.name}
    profile_path=tmp_path/'profile.json';profile_path.write_text(json.dumps(profile))
    job_path=tmp_path/'job.json';job_path.write_text(json.dumps(job))
    original=cs.create_company_sheet_set
    def mutate_then_restore(profile_arg,job_arg,**kwargs):
        path=logo if change=='logo' else Path(job_arg) if change=='job_title' else profile_path
        before=path.read_bytes()
        if change=='logo':path.write_bytes(png('blue'))
        else:
            value=json.loads(before)
            if change=='job_title':value['sheets'][-1]['title']='Changed synthetic title'
            elif change=='profile_field':value['fields']['company']='Changed synthetic company'
            else:value['template']['width_mm']+=10
            path.write_text(json.dumps(value))
        try:return original(profile_arg,job_arg,**kwargs)
        finally:path.write_bytes(before)
    monkeypatch.setattr(cs,'create_company_sheet_set',mutate_then_restore)
    output=tmp_path/'out'
    with pytest.raises(ValueError,match='company inputs changed'):
        create_cover_sheet(source,profile_path=profile_path,job_path=job_path,output_directory=output)
    assert not (output/'delivery').exists()


def test_expanded_cover_uses_original_job_snapshot_before_shared_producer(tmp_path,monkeypatch):
    from oabm.drawings import cover_sheet as cover
    config=request('contacts');source=tmp_path/'cover.json';source.write_text(json.dumps(config))
    job=json.loads(JOB.read_bytes());job['formats']=['svg']
    job_path=tmp_path/'job.json';original=json.dumps(job).encode();job_path.write_bytes(original)
    paginate=cover.paginate_cover
    def mutate_after_pagination(*args,**kwargs):
        result=paginate(*args,**kwargs)
        job['sheets'][-1]['title']='Changed synthetic title'
        job_path.write_text(json.dumps(job))
        return result
    monkeypatch.setattr(cover,'paginate_cover',mutate_after_pagination)
    output=create_cover_sheet(source,profile_path=PROFILE,job_path=job_path,output_directory=tmp_path/'out')
    assert (output/'cover-source-job.json').read_bytes()==original
    rows=json.loads((output/'company/delivery/resolved-sheets.json').read_bytes())
    assert rows[-1]['title']==config['sheets'][-1]['title']
