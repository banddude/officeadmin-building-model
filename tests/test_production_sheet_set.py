"""Actual common-profile chain; no manual PDF concatenation or customer data."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import pytest
from pypdf import PdfReader
from oabm.drawings import production_set as ps
from oabm.drawings.company_sheets import _dependencies

FIXTURE=Path(__file__).parents[1]/'fixtures/production_set/v1'


def copy_inputs(tmp_path):
    target=tmp_path/'inputs';shutil.copytree(FIXTURE,target);return target


def test_real_chain_one_model_profile_index_order_and_repeatable_pdf(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    inputs=copy_inputs(tmp_path)
    a=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'a')
    b=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'b')
    assert {p.relative_to(a):p.read_bytes() for p in a.rglob('*') if p.is_file()}=={p.relative_to(b):p.read_bytes() for p in b.rglob('*') if p.is_file()}
    manifest=json.loads((a/'PRODUCTION-MANIFEST.json').read_bytes())
    shared=a/manifest['shared_delivery']
    assert manifest['sheet_order']==['G0.0','E1.0','E3.0','E3.1','E3.2','E3.3','E3.4','D1','D2','D3']
    assert manifest['model_sha256']==hashlib.sha256((inputs/'model.json').read_bytes()).hexdigest()
    assert manifest['single_line']=={'status':'pending_integration','sheet_produced':False}
    pdf=PdfReader(io.BytesIO((shared/'sheet-set.pdf').read_bytes()))
    assert len(pdf.pages)==len(manifest['sheet_order'])
    cover=pdf.pages[0].extract_text()
    for sid in manifest['sheet_order']:assert sid in cover
    rows=json.loads((a/'derived/schedule-rows.json').read_bytes())
    assert len(rows)==manifest['schedule_row_count']==92
    model=json.loads((inputs/'model.json').read_bytes())
    canonical={item['id'] for group in ('circuits','electrical_equipment') for item in model[group]}
    assert {r['source_id'] for r in rows}<=canonical
    for sid,page,svg in zip(manifest['sheet_order'],pdf.pages,sorted((shared/'sheets').glob('*.svg'))):
        text=page.extract_text();assert sid in text and 'SYNTHETIC WORKSHOP' in text and 'EXAMPLE ELECTRIC' in text
        assert float(page.mediabox.width)==pytest.approx(2592,abs=.01)
        root=ET.fromstring(svg.read_bytes());assert len(root.findall('{http://www.w3.org/2000/svg}g'))==1
    for receipt in manifest['files']:
        assert hashlib.sha256((a/receipt['path']).read_bytes()).hexdigest()==receipt['sha256']
    with pytest.raises(FileExistsError):ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'a')


def test_cover_project_contradiction_and_single_line_false_completion_refused(tmp_path):
    inputs=copy_inputs(tmp_path);cover=json.loads((inputs/'cover.json').read_bytes())
    cover['project']['name']='CONTRADICTORY PROJECT';(inputs/'cover.json').write_text(json.dumps(cover))
    with pytest.raises(ValueError,match='differs from shared job'):
        ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    request=json.loads((inputs/'production.json').read_bytes());request['single_line']='not-produced.json'
    (inputs/'production.json').write_text(json.dumps(request))
    with pytest.raises(ValueError,match='single_line'):
        ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'bad')


def test_shared_export_failure_retains_attempt_without_success_packet(tmp_path,monkeypatch):
    inputs=copy_inputs(tmp_path)
    def fail(*args,**kwargs):raise ValueError('synthetic conversion failure')
    monkeypatch.setattr(ps,'create_company_sheet_set',fail)
    with pytest.raises(ValueError,match='shared ordered SVG/PDF production'):
        ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'bad')
    assert (tmp_path/'bad/failure.json').exists()
    assert not (tmp_path/'bad/delivery').exists()
    assert (tmp_path/'bad/.pending/inputs/model.json').read_bytes()==(inputs/'model.json').read_bytes()


def test_local_compact_chain_single_line_and_three_details_share_one_model(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    from oabm.model import BuildingModel
    source=FIXTURE.parent/'compact'
    inputs=tmp_path/'compact-inputs';shutil.copytree(source,inputs)
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'compact')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['single_line']=={'status':'supplied_topology_only','sheet_produced':True,'sheet_ids':['E601']}
    assert manifest['content_owners']['details']==['ED1']
    assert manifest['sheet_order']==['G0.0','E1.0','E601','E3.0','ED1']
    sections=json.loads((result/'derived/schedule-sections.json').read_bytes())
    assert len(sections)>1 and {s['sheet_id'] for s in sections}=={'E3.0'}
    model=BuildingModel.from_json((inputs/'model.json').read_bytes())
    topology=json.loads((result/'derived/single-line-topology-layout.json').read_bytes())['topology']
    assert topology['model_sha256']==hashlib.sha256(model.to_json(indent=None).encode()).hexdigest()
    assert {e['id'] for e in topology['equipment']}=={e.id for e in model.electrical_equipment}
    assert len(topology['connections'])==3
    detail_request=json.loads((result/'inputs/details.json').read_bytes())
    assert [d['scale'] for d in detail_request['details']]==[10,5,10]
    assert {p['sheet_id'] for p in detail_request['placements']}=={'ED1'}
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    assert len(pdf.pages)==len(manifest['sheet_order'])
    for sid in manifest['sheet_order']:assert sid in pdf.pages[0].extract_text()
    sl=pdf.pages[manifest['sheet_order'].index('E601')].extract_text()
    assert 'F001' in sl and 'F002' in sl and 'F003' in sl
    detail_page=pdf.pages[manifest['sheet_order'].index('ED1')].extract_text()
    for detail in detail_request['details']:assert detail['title'] in detail_page


def test_many_scope_contacts_flow_into_final_shared_cover_index(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    inputs=tmp_path/'input';shutil.copytree(FIXTURE.parent/'compact',inputs)
    cover=json.loads((inputs/'cover.json').read_bytes())
    cover['scope']=[f'SCOPE-{i:03d} supplied synthetic scope.' for i in range(200)]
    cover['contacts']=[{'role':'Contact','name':f'CONTACT-{i:03d}'} for i in range(160)]
    cover['pagination']={'continuation_id_prefix':'G-C'}
    (inputs/'cover.json').write_text(json.dumps(cover))
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'many')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    cover_ids=manifest['content_owners']['cover']
    assert len(cover_ids)>1 and cover_ids[1]=='G-C2'
    assert manifest['sheet_order'][:len(cover_ids)]==cover_ids
    layout=json.loads((result/'derived/cover-layout.json').read_bytes())
    assert [row['id'] for row in layout['sheet_index']]==manifest['sheet_order']
    assert layout['resolution_passes']==manifest['cover_pagination']['resolution_passes']
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    text='\n'.join(pdf.pages[manifest['sheet_order'].index(sid)].extract_text() for sid in cover_ids)
    for i in range(200):assert f'SCOPE-{i:03d}' in text
    for i in range(160):assert f'CONTACT-{i:03d}' in text
    for sid in manifest['sheet_order']:assert sid in text


def test_explicit_site_fit_updates_derived_rail_scale_and_retains_geometry(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    inputs=tmp_path/'input';shutil.copytree(FIXTURE.parent/'compact',inputs)
    source=Path(__file__).parents[1]/'examples/synthetic-site-fit-wide.json'
    raw=source.read_bytes();(inputs/'site.json').write_bytes(raw)
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'fit')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    presentation=manifest['site_presentation'];selected=presentation['selected_scale']
    assert selected in json.loads(raw)['drawing']['allowed_scales'] and selected>200
    assert presentation['coverage']['all_supplied_geometry_shown']
    job=json.loads((result/'inputs/expanded-job.json').read_bytes())
    row=next(row for row in job['sheets'] if row['id']=='E1.0')
    assert row['fields']['scale']==f'1:{selected:g}'
    assert (result/'inputs/site.json').read_bytes()==raw
    original=json.loads((result/'inputs/job.json').read_bytes())
    assert next(r for r in original['sheets'] if r['id']=='E1.0')['fields']['scale']=='1:200'
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    text=pdf.pages[manifest['sheet_order'].index('E1.0')].extract_text()
    assert f'1:{selected:g}' in text


def test_supported_unicode_flows_through_every_consumer_and_actual_pdf(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    inputs=tmp_path/'unicode-inputs';shutil.copytree(FIXTURE.parent/'compact',inputs)
    job=json.loads((inputs/'job.json').read_bytes());job['fields']['project']='ÉCLAIRAGE José Peña'
    (inputs/'job.json').write_text(json.dumps(job,ensure_ascii=False))
    cover=json.loads((inputs/'cover.json').read_bytes());cover['project']['name']=job['fields']['project']
    cover['scope'].append('45° Ø “Éclairage”');(inputs/'cover.json').write_text(json.dumps(cover,ensure_ascii=False))
    site=json.loads((inputs/'site.json').read_bytes());site['features'][4]['label']='Équipement José Peña'
    (inputs/'site.json').write_text(json.dumps(site,ensure_ascii=False))
    model=json.loads((inputs/'model.json').read_bytes())
    next(e for e in model['electrical_equipment'] if e['id']=='SUB')['name']='Panneau Peña — 45° Ø'
    model['circuits'][0]['circuit_number']='45° Ø'
    (inputs/'model.json').write_text(json.dumps(model,ensure_ascii=False))
    details=json.loads((inputs/'details.json').read_bytes())
    details['details'][0]['notes'].append('José Peña — 45° Ø')
    (inputs/'details.json').write_text(json.dumps(details,ensure_ascii=False))
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'unicode')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    text='\n'.join(page.extract_text() for page in pdf.pages)
    for value in ('ÉCLAIRAGE José Peña','Équipement José Peña','Panneau Peña','45° Ø','“Éclairage”'):
        assert value in text
    assert len(pdf.pages)==len(manifest['sheet_order'])
    assert (result/'inputs/model.json').read_bytes()==(inputs/'model.json').read_bytes()


@pytest.mark.parametrize('seed',['SL101','SL-020','PWR','POWER'])
def test_single_line_caller_numbering_enters_final_cover_and_pdf(tmp_path,seed):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    inputs=tmp_path/'input';shutil.copytree(FIXTURE.parent/'compact',inputs)
    request=json.loads((inputs/'production.json').read_bytes());request['single_line']['sheet_id']=seed
    (inputs/'production.json').write_text(json.dumps(request))
    for name,key in [('job.json','sheets'),('cover.json','sheets')]:
        document=json.loads((inputs/name).read_bytes())
        next(row for row in document[key] if row['id']=='E601')['id']=seed
        if name=='job.json' and seed=='POWER':
            next(row for row in document[key] if row['id']==seed)['template']={'rail_width_mm':110}  # Only this sheet needs a wider rail.
        (inputs/name).write_text(json.dumps(document))
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'numbered')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['single_line']['sheet_ids']==[seed] and seed in manifest['sheet_order']
    assert 'E601' not in manifest['sheet_order']
    layout=json.loads((result/'derived/single-line-topology-layout.json').read_bytes())['layout']
    assert layout['first_sheet_id']==seed and [p['id'] for p in layout['pages']]==[seed]
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    assert seed in pdf.pages[0].extract_text()
    assert seed in pdf.pages[manifest['sheet_order'].index(seed)].extract_text()


def test_full_commercial_chain_keeps_all_edges_rows_and_caller_index(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    from oabm.model import BuildingModel
    inputs=tmp_path/'commercial-inputs';shutil.copytree(FIXTURE.parent/'commercial',inputs)
    result=ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'commercial')
    manifest=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())
    layout=json.loads((result/'derived/single-line-topology-layout.json').read_bytes())['layout']
    ids=manifest['single_line']['sheet_ids']
    assert len(ids)>1 and ids==[f'SL-{20+i:03d}' for i in range(len(ids))]
    assert ids==[page['id'] for page in layout['pages']]
    assert [sid for sid in manifest['sheet_order'] if sid in set(ids)]==ids
    assert manifest['schedule_row_count']==58
    model=BuildingModel.from_json((inputs/'model.json').read_bytes())
    topology=json.loads((result/'derived/single-line-topology-layout.json').read_bytes())['topology']
    assert len(topology['equipment'])==len(model.electrical_equipment)==32
    assert len(topology['connections'])==len(model.circuits)==31
    assert topology['model_sha256']==hashlib.sha256(model.to_json(indent=None).encode()).hexdigest()
    rows=json.loads((result/'derived/schedule-rows.json').read_bytes())
    assert len(rows)==58 and all(row['source_id'] in {e.id for e in model.electrical_equipment}|{c.id for c in model.circuits} for row in rows)
    pdf=PdfReader(io.BytesIO((result/'company/delivery/sheet-set.pdf').read_bytes()))
    assert len(pdf.pages)==len(manifest['sheet_order'])
    cover=pdf.pages[0].extract_text()
    for identity in manifest['sheet_order']:assert identity in cover
    for identity in manifest['single_line']['sheet_ids']:
        assert identity in pdf.pages[manifest['sheet_order'].index(identity)].extract_text()


def test_single_line_reserved_page_collision_refuses_before_output(tmp_path):
    inputs=tmp_path/'input';shutil.copytree(FIXTURE.parent/'commercial',inputs)
    request=json.loads((inputs/'production.json').read_bytes());request['single_line']['sheet_id']='E3.0'
    job=json.loads((inputs/'job.json').read_bytes())
    next(row for row in job['sheets'] if row['id']=='SL-020')['id']='E3.0'
    # Keep template owners distinct, while reserving the generated second page.
    schedule=next(row for row in job['sheets'] if row['id']=='E3.0' and row['title']=='PANEL SCHEDULES')
    schedule['id']='E3.1';request['schedules']['sheet_id']='E3.1'
    cover=json.loads((inputs/'cover.json').read_bytes());cover['sheets']=[{'id':r['id'],'title':r['title']} for r in job['sheets']]
    for name,document in [('production.json',request),('job.json',job),('cover.json',cover)]:
        (inputs/name).write_text(json.dumps(document))
    with pytest.raises(ValueError,match='collid|reserved'):
        ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'collision')
    assert not (tmp_path/'collision').exists()


def test_wide_caller_id_respects_explicit_fixed_rail_capacity(tmp_path):
    inputs=tmp_path/'input';shutil.copytree(FIXTURE.parent/'compact',inputs)
    request=json.loads((inputs/'production.json').read_bytes());request['single_line']['sheet_id']='POWER'
    job=json.loads((inputs/'job.json').read_bytes());job['formats']=['svg']
    next(row for row in job['sheets'] if row['id']=='E601')['id']='POWER'
    cover=json.loads((inputs/'cover.json').read_bytes());cover['sheets']=[{'id':r['id'],'title':r['title']} for r in job['sheets']]
    for name,value in [('production.json',request),('job.json',job),('cover.json',cover)]:
        (inputs/name).write_text(json.dumps(value))
    with pytest.raises(ValueError,match='measured cell capacity'):
        ps.create_production_sheet_set(inputs/'production.json',output_directory=tmp_path/'capacity')
    assert not (tmp_path/'capacity/delivery').exists()
