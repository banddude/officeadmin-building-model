from dataclasses import replace
from pathlib import Path
import copy
import pytest
from oabm.model import BuildingModel
from oabm.reports.source_coverage import model_fingerprint
from oabm.electrical.manual_device_intake import admit_manual_devices, manual_intake_request_sha256

ROOT=Path(__file__).resolve().parents[1]

def fixture_model():return BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
def pose(x):return {'position':{'x':x,'y':0,'z':0},'rotation':{'x':0,'y':0,'z':0,'w':1}}
def item(n):return {'source_item_id':f'item:{n}','quantity':1,'device_id':f'device:manual-{n}',
    'device_type':'receptacle','level_id':'level:ground','device_pose':pose(n),
    'port_id':f'port:manual-{n}','port_pose':pose(n),'port_direction':{'x':1,'y':0,'z':0},'confidence':.8}
def run(model,rows,**over):
    args={'request_id':'request:synthetic','source_sha256':'a'*64,'base_model_sha256':model_fingerprint(model),
          'frame_id':model.coordinate_system.frame_id,'items':rows};args.update(over)
    args.setdefault('request_sha256',manual_intake_request_sha256(**args))
    return admit_manual_devices(model,**args)


def test_two_explicit_devices_and_quantity_only_row(model=None):
    model=fixture_model();before=model.to_json();rows=[item(2),{'source_item_id':'item:bulk','quantity':10},item(1)]
    result,report=run(model,rows)
    assert model.to_json()==before and report['applied_to_original'] is False
    assert [r['status'] for r in report['items']]==['admitted','admitted','unresolved']
    assert {p.id for p in result.ports}>={'port:manual-1','port:manual-2'}
    for p in result.ports:
        if p.id.startswith('port:manual-'):
            assert p.domain=='electrical' and p.role=='load' and not p.connected_port_ids
            assert p.provenance[0].derivation=='user'
    assert report['registration']=='not_verified' and report['source_authentication']=='not_verified'
    replay,other=run(model,list(reversed(rows)))
    assert result.to_json()==replay.to_json() and report==other


def test_missing_fields_unresolved_and_malformed_present_fields_fail():
    model=fixture_model();row=item(1);del row['port_pose']
    out,report=run(model,[row]);assert len(out.electrical_devices)==len(model.electrical_devices)
    assert report['items'][0]['missing_fields']==['port_pose']
    row['quantity']=True
    with pytest.raises(ValueError):run(model,[row])


def test_stale_frame_hash_collision_and_duplicate_rejected():
    model=fixture_model()
    for over in ({'base_model_sha256':'0'*64},{'frame_id':'unregistered'}):
        with pytest.raises(ValueError):run(model,[item(1)],**over)
    with pytest.raises(ValueError):run(model,[item(1),item(1)])
    bad=item(1);bad['device_id']=model.electrical_devices[0].id
    with pytest.raises(ValueError):run(model,[bad])


@pytest.mark.parametrize('change',['bool_coordinate','nonfinite','zero_direction','bad_quaternion','unknown_level','extra'])
def test_malformed_facts_rejected(change):
    model=fixture_model();row=item(1)
    if change=='bool_coordinate':row['device_pose']['position']['x']=True
    if change=='nonfinite':row['device_pose']['position']['x']=float('nan')
    if change=='zero_direction':row['port_direction']={'x':0,'y':0,'z':0}
    if change=='bad_quaternion':row['device_pose']['rotation']['w']=2
    if change=='unknown_level':row['level_id']='level:missing'
    if change=='extra':row['surprise']='unsupported'
    with pytest.raises((ValueError,TypeError)):run(model,[row])


def test_source_revision_preserves_explicit_ids_but_changes_candidate_evidence():
    model=fixture_model();a,ar=run(model,[item(1)])
    b,br=run(model,[item(1)],source_sha256='b'*64)
    assert a.electrical_devices[-1].id==b.electrical_devices[-1].id
    assert ar['candidate_model_sha256']!=br['candidate_model_sha256']


def test_changed_source_invalidates_old_request_binding():
    model=fixture_model(); args={'request_id':'request:synthetic','source_sha256':'a'*64,'base_model_sha256':model_fingerprint(model),'frame_id':model.coordinate_system.frame_id,'items':[item(1)]}
    digest=manual_intake_request_sha256(**args)
    args['source_sha256']='b'*64
    with pytest.raises(ValueError,match='request binding'):admit_manual_devices(model,**args,request_sha256=digest)


def test_schema_copies_match_and_roundtrip_preserves_candidate():
    import json
    from importlib.resources import files
    for name in ('manual-device-request-v1.schema.json','manual-device-report-v1.schema.json'):
        assert json.loads((ROOT/'contracts'/name).read_text())==json.loads(files('oabm.electrical._manual_intake_contracts').joinpath(name).read_text())
    model=fixture_model();candidate,report=run(model,[item(1)])
    reopened=BuildingModel.from_json(candidate.to_json())
    assert {d.id for d in reopened.electrical_devices}=={d.id for d in candidate.electrical_devices}
    assert reopened.ports[-1].id=='port:manual-1' and report['applied_to_original'] is False


def test_empty_rows_and_shared_device_port_id_fail():
    model=fixture_model()
    with pytest.raises(ValueError):run(model,[])
    row=item(1);row['port_id']=row['device_id']
    with pytest.raises(ValueError):run(model,[row])


def test_unsafe_interoperable_integer_rejected():
    model=fixture_model();row=item(1);row['device_pose']['position']['x']=2**53
    with pytest.raises(ValueError):run(model,[row])


def test_same_source_item_cannot_be_readmitted_with_new_ids():
    base=fixture_model();first,_=run(base,[item(1)])
    changed=item(1);changed['device_id']='device:other';changed['port_id']='port:other'
    with pytest.raises(ValueError,match='already admitted'):run(first,[changed])


def test_unresolved_row_with_colliding_id_is_rejected():
    base=fixture_model();row={'source_item_id':'item:unresolved','device_id':base.electrical_devices[0].id}
    with pytest.raises(ValueError,match='collision'):run(base,[row])


def test_explicit_intake_can_feed_preliminary_router_without_mutating_base():
    from oabm.model import Level,ElectricalEquipment,Port,Pose,Point3,Vector3,Box3D,Size3
    from oabm.routing import CircuitDesignRules,RoutingOptions,design_proposed_circuits
    from oabm.routing.router import AllowedRouteDomain
    level=Level(id='level:ground',elevation_m=0)
    panel=ElectricalEquipment(id='equipment:panel',equipment_type='panelboard',pose=Pose(position=Point3(x=0,y=0,z=0)))
    source=Port(id='port:panel-source',owner_id=panel.id,domain='electrical',role='source',pose=panel.pose,direction=Vector3(x=1,y=0,z=0))
    base=BuildingModel(model_id='model:manual-routing',levels=(level,),electrical_equipment=(panel,),ports=(source,))
    one=item(1);two=item(2)
    for row in (one,two):row['port_direction']={'x':-1,'y':0,'z':0}
    before=base.to_json();candidate,report=run(base,[one,two])
    assert len(base.circuits)==0 and base.to_json()==before
    bounds=AllowedRouteDomain(id='domain:explicit',kind='user-supplied',volumes=(Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=6,y=6,z=6)),))
    rules=CircuitDesignRules(max_devices_per_circuit=2,assumed_voltage_v=120,assumed_poles=1,route_type='emt',nominal_diameter_m=.0213,conductor_material='copper',conductor_size='12 AWG',insulated_conductor_count=2,include_ground=True)
    controls=RoutingOptions(bend_penalty_m=.4,max_bends=8,port_stub_m=0.,clearance_m=.02,search_margin_m=.5,corridor_tolerance_m=.03,bundle_alignment_tolerance_m=.02,preferred_corridor_discount=0.,surface_path_discount=0.,glazed_wall_penalty=7.,soft_obstacle_penalty_factor=4.,vertical_cost_factor=1.,coordinate_precision=9)
    designed=design_proposed_circuits(candidate,equipment_id=panel.id,device_ids=('device:manual-1','device:manual-2'),rules=rules,user_groups=(('device:manual-1','device:manual-2'),),user_input_id='decision:synthetic',source_port_id=source.id,load_port_ids={'device:manual-1':'port:manual-1','device:manual-2':'port:manual-2'},routing_options=controls,allowed_domain=bounds)
    assert len(designed.circuits)==1 and len(designed.routes)==2
    assert designed.circuits[0].attributes['design']['source_observed'] is False
    assert base.to_json()==before and len(candidate.circuits)==0


def test_unresolved_id_colliding_with_nested_physical_route_owner_rejected():
    from oabm.model import Level
    from physical_route_fixture import make_physical_model
    base=replace(make_physical_model(),levels=(Level(id='level:ground',elevation_m=0),))
    row={'source_item_id':'item:nested','device_id':base.routes[0].joints[0].id}
    with pytest.raises(ValueError):run(base,[row])
