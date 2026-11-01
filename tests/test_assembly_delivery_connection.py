"""Real supported assembly views through existing delivery/composition boundaries."""
import hashlib,json,sys
from pathlib import Path
from dataclasses import replace
import pytest
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.drawings import PlanSpec,SectionSpec,generate_drawing_set,Bounds2,DrawingPrimitive,DrawingView,Point2,LineStyle
from oabm.drawings.view_body import render_scaled_view_body
from oabm.drawings.production_set import create_production_sheet_set
from oabm.exports.model_delivery import deliver_model,ModelDeliveryError
from oabm.model import Point3,Vector3
sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
from assembly_production_sheets import prepare


def test_drawing_set_indexes_every_canonical_child_and_refuses_unsupported_scope():
    m=synthetic_wall_inserts_ceiling_assembly();p=PlanSpec(id='P',level_id=m.levels[0].id)
    section=SectionSpec(id='S',origin=Point3(x=1,y=0,z=0),direction=Vector3(x=1,y=0,z=0))
    result=generate_drawing_set(m,plans=[p],sections=[section],include_schedules=False,assembly_geometry='cut')
    assert len(result.views)==2
    indexed={r.canonical_id for r in result.source_index}
    assert {c.id for c in m.assembly_components}|{a.id for a in m.building_assemblies}<=indexed
    assert all(set(p.source_ids)<=indexed for v in result.views for p in v.primitives)
    scheduled=generate_drawing_set(m,plans=[p],assembly_geometry='cut')
    assert any(s.id=='schedule:assembly-components' for s in scheduled.schedules)
    assert all(row.source_id in indexed for schedule in scheduled.schedules for row in schedule.rows)
    with pytest.raises(ValueError,match='assembly_geometry'):generate_drawing_set(m,plans=[p],assembly_geometry='guessed')


def test_model_delivery_v2_uses_same_model_for_assembly_plans_and_preview(tmp_path):
    m=synthetic_wall_inserts_ceiling_assembly();mp=tmp_path/'model.json';mp.write_text(m.to_json())
    config={'version':'model-delivery-v2','assembly_geometry':'cut','bend_policy':'skip','radii_m':{},'preview_glb':True,
        'plans':[{'id':'P','level_id':m.levels[0].id,'cut_height_m':1.2,'view_depth_below_m':.25,'view_depth_above_m':.25,'scale':20}]}
    cp=tmp_path/'options.json';cp.write_text(json.dumps(config))
    root=deliver_model(mp,cp,output_directory=tmp_path/'run')
    manifest=json.loads((root/'MANIFEST.json').read_bytes());assert manifest['version']=='model-delivery-v2'
    assert manifest['assembly_geometry']=='cut' and manifest['assembly_views'][0]['cut_component_ids']
    assert (root/'inputs/model.json').read_bytes()==mp.read_bytes()
    assert (root/'preview.glb').is_file()
    assert (root/'drawing-set.json').is_file()
    for row in manifest['files']:assert hashlib.sha256((root/row['path']).read_bytes()).hexdigest()==row['sha256']
    config.pop('assembly_geometry');config['version']='model-delivery-v1';cp.write_text(json.dumps(config))
    with pytest.raises(ModelDeliveryError,match='populated'):deliver_model(mp,cp,output_directory=tmp_path/'legacy')
    assert not (tmp_path/'legacy/delivery').exists()


def test_scaled_view_preserves_scale_identity_and_refuses_unsupported_presentations():
    primitive=DrawingPrimitive(id='edge',kind='line',source_ids=('child',),layer='architecture:assembly-cuts',
        points=(Point2(0,0),Point2(1,1)),style=LineStyle(weight='heavy'))
    v=DrawingView(id='V',title='test',view_type='plan',bounds=Bounds2(0,0,1,1),scale=10,primitives=(primitive,))
    r=render_scaled_view_body(v,drawing_bounds_mm=(0,0,200,200),origin_mm=(10,10))
    assert '10,110 110,10' in r.body_svg
    assert r.coverage['primitive_sources'][0]['source_ids']==['child']
    assert r.coverage['scale']==10
    with pytest.raises(ValueError,match='requested scale'):render_scaled_view_body(v,drawing_bounds_mm=(0,0,50,50),origin_mm=(10,10))
    bad=replace(v,primitives=(replace(primitive,style=LineStyle(pattern='dash')),))
    with pytest.raises(ValueError,match='patterned'):render_scaled_view_body(bad,drawing_bounds_mm=(0,0,200,200),origin_mm=(10,10))


def test_production_v2_needs_no_fake_component_inputs_and_rebuilds_exactly(tmp_path):
    request=prepare(tmp_path/'inputs')
    a=create_production_sheet_set(request,output_directory=tmp_path/'a')
    b=create_production_sheet_set(request,output_directory=tmp_path/'b')
    tree=lambda r:{p.relative_to(r).as_posix():p.read_bytes() for p in r.rglob('*') if p.is_file()}
    assert tree(a)==tree(b)
    manifest=json.loads((a/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['sheet_order']==['A101','A301']
    assert manifest['content_owners']['model_views']==['A101','A301']
    assert not manifest['content_owners']['details']
    assert manifest['single_line']['status']=='not_requested'
    for sid in ['A101','A301']:
        coverage=json.loads((a/'derived'/f'{sid}.model-view.json').read_bytes())
        assert coverage['scale']==20 and coverage['view_metadata']['assembly_mode']=='cut-plane-only'
    for row in manifest['files']:assert hashlib.sha256((a/row['path']).read_bytes()).hexdigest()==row['sha256']
    with pytest.raises(FileExistsError):create_production_sheet_set(request,output_directory=tmp_path/'a')


def test_production_view_failure_never_publishes_partial_or_stale_packet(tmp_path,monkeypatch):
    request=prepare(tmp_path/'inputs')
    from oabm.drawings import production_set as ps
    def fail(*args,**kwargs):raise ValueError('synthetic export failure')
    monkeypatch.setattr(ps,'create_company_sheet_set',fail)
    with pytest.raises(ValueError,match='shared ordered'):ps.create_production_sheet_set(request,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad/delivery').exists()
    assert (tmp_path/'bad/failure.json').exists()
    config=json.loads(request.read_bytes());config['model_views'][0]['origin_mm']=[10000,10000];request.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='requested scale'):ps.create_production_sheet_set(request,output_directory=tmp_path/'capacity')
    assert not (tmp_path/'capacity').exists()

def test_cover_and_physical_views_share_final_index_and_common_fields(tmp_path):
    request=prepare(tmp_path/'inputs')
    job_path=request.parent/'job.json';job=json.loads(job_path.read_bytes())
    job['fields']['address']='SYNTHETIC SITE'
    job['sheets'].insert(0,{'id':'G001','title':'PHYSICAL MODEL REVIEW COVER'})
    job_path.write_text(json.dumps(job))
    cover={'version':'cover-sheet-v1','id':'G001',
        'project':{'name':job['fields']['project'],'location':job['fields']['address'],'number':job['fields']['project_number']},
        'scope':['Present supplied synthetic physical assembly cuts; not construction approval.'],
        'sheets':job['sheets'],'contacts':[{'role':'Designer','name':'EXAMPLE DESIGN TEAM'}]}
    (request.parent/'cover.json').write_text(json.dumps(cover))
    config=json.loads(request.read_bytes());config['cover']='cover.json';request.write_text(json.dumps(config))
    root=create_production_sheet_set(request,output_directory=tmp_path/'covered')
    manifest=json.loads((root/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['sheet_order']==['G001','A101','A301']
    assert [r['id'] for r in manifest['cover_index']]==manifest['sheet_order']
    assert json.loads((root/'derived/cover-expanded.json').read_bytes())['sheets']==manifest['cover_index']
    import xml.etree.ElementTree as ET
    resolved=json.loads((root/'company/delivery/resolved-sheets.json').read_bytes())
    assert all(s['fields']['project']==job['fields']['project'] for s in resolved)
    for svg in (root/'company/delivery/sheets').glob('*.svg'):
        text=' '.join(n.text or '' for n in ET.fromstring(svg.read_bytes()).iter() if n.tag.endswith('}text'))
        assert ' '.join(job['fields']['project'].split()) in ' '.join(text.split())

@pytest.mark.parametrize('change,match',[
    ({'type':'elevation'},'elevation'),
    ({'annotations':'automatic'},'annotations'),
    ({'assembly_geometry':None},'assembly_geometry'),
    ({'options':{'level_id':'absent','scale':20}},'level'),
    ({'options':{'level_id':'level:proof','scale':10}},'titleblock scale'),
])
def test_bad_production_view_requests_refuse_before_attempt(tmp_path,change,match):
    request=prepare(tmp_path/'inputs');config=json.loads(request.read_bytes())
    config['model_views'][0].update(change);request.write_text(json.dumps(config))
    with pytest.raises(ValueError,match=match):create_production_sheet_set(request,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
