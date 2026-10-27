import hashlib
import io
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from PIL import Image
from pypdf import PdfReader
from oabm.drawings import company_sheets as cs

FIXTURE=Path(__file__).parents[1]/'fixtures/company_sheets/v1'

def inputs(tmp_path, formats=('svg',)):
    p=tmp_path/'profile.json';j=tmp_path/'job.json'
    p.write_bytes((FIXTURE/'profile.json').read_bytes())
    job=json.loads((FIXTURE/'job.json').read_bytes());job['formats']=list(formats)
    j.write_text(json.dumps(job))
    return p,j


def test_precedence_rendering_never_changes_defaults_across_jobs(tmp_path):
    p,j=inputs(tmp_path);original=p.read_bytes()
    job=json.loads(j.read_bytes());job['fields']['company']='JOB ONLY'
    job['sheets'][1]['fields']={'company':'SHEET ONLY'};j.write_text(json.dumps(job))
    r=cs.resolve_company_sheet_set(p,j)
    assert [s.fields['company'] for s in r.sheets]==['JOB ONLY','SHEET ONLY','JOB ONLY']
    result=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'first')
    assert p.read_bytes()==original
    job['fields'].pop('company');job['sheets'][1].pop('fields');j.write_text(json.dumps(job))
    assert cs.resolve_company_sheet_set(p,j).sheets[0].fields['company']=='EXAMPLE ELECTRIC'
    assert 'JOB ONLY' in (result/'sheets/001-E0.0.svg').read_text()
    assert p.read_bytes()==original


def test_explicit_defaults_update_guard_and_backup(tmp_path):
    p,j=inputs(tmp_path);raw=p.read_bytes();change=tmp_path/'changes.json'
    change.write_text(json.dumps({'fields':{'company':'NEW DEFAULT'}}))
    digest=hashlib.sha256(raw).hexdigest()
    new=cs.update_company_profile(p,change,expected_sha256=digest)
    assert new!=digest and cs.resolve_company_sheet_set(p,j).sheets[0].fields['company']=='NEW DEFAULT'
    assert p.with_name(p.name+'.'+digest+'.bak').read_bytes()==raw
    with pytest.raises(ValueError,match='profile changed'):cs.update_company_profile(p,change,expected_sha256=digest)


def test_local_logo_override_clear_and_snapshotted_assets(tmp_path):
    p,j=inputs(tmp_path)
    Image.new('RGB',(20,10),'blue').save(tmp_path/'default.png')
    Image.new('RGB',(10,20),'red').save(tmp_path/'override.png')
    profile=json.loads(p.read_bytes());profile['logos']={'icon':'default.png'};p.write_text(json.dumps(profile))
    job=json.loads(j.read_bytes());job['logos']={'icon':'override.png'};job['sheets'][1]['logos']={'icon':None};j.write_text(json.dumps(job))
    r=cs.resolve_company_sheet_set(p,j)
    assert r.sheets[0].logos[0][1]==(tmp_path/'override.png').read_bytes()
    assert r.sheets[1].logos==()
    (tmp_path/'override.png').unlink()
    assert 'data:image/png;base64,' in cs.render_company_sheet_svg(r.sheets[0])
    assert profile==json.loads(p.read_bytes())


def test_body_bounds_and_no_resource_inputs(tmp_path):
    p,j=inputs(tmp_path);s=cs.resolve_company_sheet_set(p,j).sheets[0]
    assert s.drawing_bounds_mm[2]<s.width_mm-s.template['margin_mm']
    body='<g><rect x="20" y="20" width="50" height="40" fill="#ffffff" stroke="#000000" stroke-width="0.2"/><text x="22" y="30" font-size="3">BODY</text></g>'
    assert 'BODY' in cs.render_company_sheet_svg(s,body_svg=body)
    for body in ('<g><text x="900" y="30">OUT</text></g>', '<g><image href="https://example.invalid/x"/></g>', '<g transform="translate(1000)"></g>'):
        with pytest.raises(ValueError):cs.render_company_sheet_svg(s,body_svg=body)


def test_errors_before_output_reservation(tmp_path):
    p,j=inputs(tmp_path);job=json.loads(j.read_bytes())
    job['fields']['project']='X'*300;j.write_text(json.dumps(job))
    with pytest.raises(ValueError,match='capacity'):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    job['fields']['project']='EXAMPLE';job['logos']={'icon':'missing.png'};j.write_text(json.dumps(job))
    with pytest.raises(ValueError,match='valid local PNG'):cs.resolve_company_sheet_set(p,j)


def test_pdf_failure_retains_pending_no_delivery(tmp_path,monkeypatch):
    p,j=inputs(tmp_path,('svg','pdf'))
    def fail(*a):raise ImportError('missing converter')
    monkeypatch.setattr(cs,'_pdf',fail)
    with pytest.raises(ValueError,match='missing converter'):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'bad')
    assert (tmp_path/'bad/failure.json').exists() and not (tmp_path/'bad/delivery').exists()


def test_real_order_size_text_and_reproducible_packet(tmp_path):
    try:cs._dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    p,j=inputs(tmp_path,('svg','pdf'))
    a=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'a')
    b=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'b')
    assert {x.relative_to(a):x.read_bytes() for x in a.rglob('*') if x.is_file()}=={x.relative_to(b):x.read_bytes() for x in b.rglob('*') if x.is_file()}
    reader=PdfReader(io.BytesIO((a/'sheet-set.pdf').read_bytes()))
    assert len(reader.pages)==3
    for page,sid in zip(reader.pages,['E0.0','E1.0','E2.0']):
        assert float(page.mediabox.width)==pytest.approx(2592,abs=.01)
        assert float(page.mediabox.height)==pytest.approx(1728,abs=.01)
        assert sid in page.extract_text() and 'EXAMPLE ELECTRIC' in page.extract_text()
    with pytest.raises(FileExistsError):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'a')
    manifest=json.loads((a/'MANIFEST.json').read_bytes())
    for row in manifest['files']:assert hashlib.sha256((a/row['path']).read_bytes()).hexdigest()==row['sha256']


def test_existing_update_temporary_is_not_removed(tmp_path):
    p,j=inputs(tmp_path);raw=p.read_bytes();change=tmp_path/'change.json'
    change.write_text('{"fields":{"company":"UPDATED"}}')
    pending=p.with_name(p.name+'.pending');pending.write_text('another writer')
    with pytest.raises(FileExistsError):cs.update_company_profile(p,change,expected_sha256=hashlib.sha256(raw).hexdigest())
    assert pending.read_text()=='another writer' and p.read_bytes()==raw


def test_logo_png_is_embedded_in_pdf_not_a_whole_page_raster(tmp_path):
    try:cs._dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    p,j=inputs(tmp_path,('svg','pdf'))
    Image.new('RGB',(20,10),'blue').save(tmp_path/'logo.png')
    profile=json.loads(p.read_bytes());profile['logos']={'icon':'logo.png'};p.write_text(json.dumps(profile))
    root=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'out')
    reader=PdfReader(io.BytesIO((root/'sheet-set.pdf').read_bytes()))
    assert len(list(reader.pages[0].images))==1
    assert 'EXAMPLE ELECTRIC' in reader.pages[0].extract_text()
    assert b'BT' in reader.pages[0].get_contents().get_data()


def test_body_text_defaults_are_physical_and_text_overflow_refused(tmp_path):
    p,j=inputs(tmp_path);s=cs.resolve_company_sheet_set(p,j).sheets[0]
    xml=cs.render_company_sheet_svg(s,body_svg='<g><text x="20" y="20">LABEL</text></g>')
    node=next(n for n in ET.fromstring(xml).iter() if n.text=='LABEL')
    assert node.get('font-size')=='3' and node.get('font-family')=='Liberation Sans'
    with pytest.raises(ValueError,match='drawing_bounds_mm'):
        cs.render_company_sheet_svg(s,body_svg='<g><text x="20" y="20" font-size="30">'+('W'*100)+'</text></g>')


def test_portable_rebuild_uses_retained_assets_and_body(tmp_path):
    p,j=inputs(tmp_path,('svg','pdf'))
    try:cs._dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    Image.new('RGB',(20,10),'blue').save(tmp_path/'logo.png')
    profile=json.loads(p.read_bytes());profile['logos']={'icon':'logo.png'};p.write_text(json.dumps(profile))
    a=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'a',bodies={'E0.0':'<g><text x="20" y="20" font-size="3">BODY</text></g>'})
    p.unlink();j.unlink();(tmp_path/'logo.png').unlink()
    b=cs.rebuild_company_sheet_set(a,output_directory=tmp_path/'b')
    for path in a.glob('sheets/*.svg'):assert path.read_bytes()==(b/'sheets'/path.name).read_bytes()
    assert (a/'sheet-set.pdf').read_bytes()==(b/'sheet-set.pdf').read_bytes()
    (a/'inputs/job-portable.json').write_text('{}')
    with pytest.raises(ValueError,match='receipt mismatch'):cs.rebuild_company_sheet_set(a,output_directory=tmp_path/'bad')
