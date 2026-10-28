"""Real single-model coordinator proof; no raster acceptance asserted here."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,sys
import pytest
from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.electrical_sheet import plan_electrical_sheets,build_electrical_sheet_bodies
from oabm.drawings.production_set import create_production_sheet_set
from oabm.drawings.single_line.demo_drafting import make_model
sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
from electrical_composition import prepare


def inputs(tmp_path,pdf=False):
    request=prepare(tmp_path/'inputs',pdf=pdf)
    data=json.loads(request.read_bytes());sheet=resolve_company_sheet_set(request.parent/'profile.json',request.parent/'job.json').sheets[0]
    c=data['single_line']['composition']
    return request,sheet,c


def planned(tmp_path,model=None):
    request,sheet,c=inputs(tmp_path);model=model or make_model()
    plan=plan_electrical_sheets(model,sheet,single_line_regions=c['single_line_regions'],panel_region_mm=c['panel_region_mm'],
        panel_width_mm=c['panel_width_mm'],panel_policy=c['panel_policy'],panel_placement=c['panel_placement'])
    sheets=tuple(replace(sheet,id=sid) for sid in plan.sheet_ids)
    return model,plan,sheets


def test_actual_same_model_no_empty_or_unassigned_branch_shaped_tables(tmp_path):
    model,plan,sheets=planned(tmp_path);before=model.to_json()
    bodies,mapping=build_electrical_sheet_bodies(model,sheets,plan=plan)
    assert list(bodies)==['E3.1']
    assert not plan.panel_pages
    assert len(mapping['single_line']['topology']['equipment'])==8
    shown=[symbol['equipment_id'] for page in mapping['single_line']['sheets'] for symbol in page['equipment_symbols']]
    assert sorted(shown)==sorted(e.id for e in model.electrical_equipment)
    grid=[row for page in mapping['single_line']['sheets'] for row in page['feeder_grid_rows']]
    assert sorted(row['source_id'] for row in grid)==sorted(c.id for c in model.circuits)
    assert {engine for p in mapping['single_line']['sheets'] for engine in p['feeder_grid_engine']}=={'oabm.drawings.schedule_layout.paginate_schedules'}
    assert mapping['panel_selection']['not_rendered']
    assert '2 panelboards without listed circuits' in bodies['E3.1']
    assert model.to_json()==before


def test_opaque_circuit_number_is_in_actual_grid_not_only_topology(tmp_path):
    original=make_model();label='CKT-N9/ALPHA'
    model=replace(original,circuits=tuple(replace(c,circuit_number=label) if c.id=='F02' else c for c in original.circuits))
    model,plan,sheets=planned(tmp_path,model)
    _,mapping=build_electrical_sheet_bodies(model,sheets,plan=plan)
    rows=[row for page in mapping['single_line']['sheets'] for row in page['feeder_grid_rows'] if row['source_id']=='F02']
    assert rows and any(label in cell for row in rows for cell in row['cells'])
    assert all(''.join(lines)==cell.replace('\n','') for row in rows for lines,cell in zip(row['wrapped_cells'],row['cells']))


def test_sealed_plan_and_source_mutation_refuse(tmp_path):
    model,plan,sheets=planned(tmp_path)
    plan.sld_layout['composition']='tampered'
    with pytest.raises(ValueError,match='plan/model changed'):build_electrical_sheet_bodies(model,sheets,plan=plan)


def test_existing_coordinator_publishes_one_hash_bound_sheet_and_repeats(tmp_path):
    request,_,_=inputs(tmp_path)
    a=create_production_sheet_set(request,output_directory=tmp_path/'a')
    b=create_production_sheet_set(request,output_directory=tmp_path/'b')
    tree=lambda root:{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert tree(a)==tree(b)
    manifest=json.loads((a/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['sheet_order']==['E3.1'] and manifest['schedule_row_count']==0
    assert manifest['electrical_composition']['panel_row_count']==0
    assert manifest['content_owners']['electrical_composition']==['E3.1']
    assert (a/'inputs/model.json').read_bytes()==(request.parent/'model.json').read_bytes()
    for row in manifest['files']:assert hashlib.sha256((a/row['path']).read_bytes()).hexdigest()==row['sha256']
    with pytest.raises(FileExistsError):create_production_sheet_set(request,output_directory=tmp_path/'a')


def test_atomic_combined_export_failure_retains_no_success_packet(tmp_path,monkeypatch):
    request,_,_=inputs(tmp_path)
    from oabm.drawings import production_set as ps
    def fail(*args,**kwargs):raise ValueError('synthetic conversion failure')
    monkeypatch.setattr(ps,'create_company_sheet_set',fail)
    with pytest.raises(ValueError,match='shared ordered'):create_production_sheet_set(request,output_directory=tmp_path/'failed')
    assert not (tmp_path/'failed/delivery').exists()
    assert (tmp_path/'failed/failure.json').exists()


def test_actual_shared_pdf_has_complete_grid_labels(tmp_path):
    from oabm.drawings.sheet_pdf import _dependencies
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    from pypdf import PdfReader
    request,_,_=inputs(tmp_path,pdf=True)
    root=create_production_sheet_set(request,output_directory=tmp_path/'pdf')
    pdf=PdfReader(root/'company/delivery/sheet-set.pdf')
    assert len(pdf.pages)==1
    text=' '.join(pdf.pages[0].extract_text().split())
    assert 'EXAMPLE ELECTRIC' in text and 'SUPPLIED MODEL:' in text
    assert 'PROTECTION' in text and 'GROUNDING' in text
    assert all(c.id in text for c in make_model().circuits)


def useful_branch_model():
    from oabm.model import ElectricalEquipment,Port,Point3,Pose,Vector3,Circuit
    model=make_model();pose=Pose(position=Point3(x=0,y=0,z=0))
    p=model.electrical_equipment[0].provenance
    load=ElectricalEquipment(id='BR-DISCONNECT',name='Synthetic listed branch disconnect',equipment_type='disconnect',pose=pose,rated_voltage_v=480,provenance=p)
    ports=(Port(id='BR-source',owner_id='HP1',domain='electrical',role='branch',pose=pose,direction=Vector3(x=1,y=0,z=0),provenance=p),
        Port(id='BR-load',owner_id=load.id,domain='electrical',role='incoming',pose=pose,direction=Vector3(x=-1,y=0,z=0),provenance=p))
    circuit=Circuit(id='BR-1',circuit_number='CKT-OPAQUE-7',source_port_id=ports[0].id,load_port_ids=(ports[1].id,),
        voltage_v=480,poles=3,phase='ABC',load_va=1200,provenance=p,
        attributes={'panel_schedule':{'description':'Supplied branch disconnect load','ocpd':'20 A','side':'left','row':7},
            'single_line_drafting':{'protection':{'kind':'breaker','rating_a':20,'poles':3}}})
    return replace(model,electrical_equipment=model.electrical_equipment+(load,),ports=model.ports+ports,circuits=model.circuits+(circuit,))


def test_useful_listed_panel_data_uses_shared_selection_and_full_width_continuation(tmp_path):
    model,plan,sheets=planned(tmp_path,useful_branch_model())
    assert plan.panel_pages
    assert plan.sheet_kinds[-1]=='panel_schedules'
    assert plan.panel_pages[0]['region_mm']==plan.drawing_bounds_mm
    rows=[row for page in plan.panel_pages for row in page['row_map']]
    assert any(row['source_id']=='BR-1' for row in rows)
    assert all(row['source_id'] not in {'F01','F02','F03','F04','F05','F06','F07'} for row in rows)
    bodies,mapping=build_electrical_sheet_bodies(model,sheets,plan=plan)
    assert set(bodies)==set(plan.sheet_ids)
    assert mapping['panel_selection']['source_selection_audit']['selected_schedule_ids']
    assert 'CKT-OPAQUE-7' in ''.join(bodies.values())
    section=plan.panel_pages[0]['sections'][0]
    assert section['bounds_mm'][1]<plan.drawing_bounds_mm[1]+10
    assert all(row['supplied_row']==7 for row in rows if row['source_id']=='BR-1')


def test_empty_panel_register_requires_explicit_request(tmp_path):
    _,sheet,c=inputs(tmp_path)
    plan=plan_electrical_sheets(make_model(),sheet,single_line_regions=c['single_line_regions'],panel_region_mm=None,
        panel_policy='include_empty',panel_placement='continuation')
    assert plan.panel_pages
    assert all(not group['selected'] for group in plan.panel_selection['source_selection_audit']['groups'] if group['panel_id'] is None)
