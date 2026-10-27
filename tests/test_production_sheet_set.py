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
