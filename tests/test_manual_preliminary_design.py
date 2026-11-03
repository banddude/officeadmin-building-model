import copy
from dataclasses import asdict,replace
from pathlib import Path
import pytest
from oabm.model import BuildingModel,Level,ElectricalEquipment,Port,Pose,Point3,Vector3,Box3D,Size3,Circuit
from oabm.electrical.manual_device_intake import admit_manual_devices,manual_intake_request_sha256
from oabm.electrical.manual_preliminary_design import design_manual_intake,manual_design_decision_sha256
from oabm.reports.source_coverage import model_fingerprint
from oabm.routing import CircuitDesignRules,RoutingOptions
from test_manual_device_intake import item


def setup():
    panel=ElectricalEquipment(id='equipment:panel',equipment_type='panelboard',pose=Pose(position=Point3(x=0,y=0,z=0)))
    source=Port(id='port:source',owner_id=panel.id,domain='electrical',role='source',pose=panel.pose,direction=Vector3(x=1,y=0,z=0))
    base=BuildingModel(model_id='model:manual-design',levels=(Level(id='level:ground',elevation_m=0),),electrical_equipment=(panel,),ports=(source,))
    rows=[item(1),item(2),{'source_item_id':'item:bulk','quantity':3}]
    for row in rows[:2]:row['port_direction']={'x':-1,'y':0,'z':0}
    intake={'request_id':'request:synthetic','source_sha256':'a'*64,'base_model_sha256':model_fingerprint(base),'frame_id':'model','items':rows}
    intake['request_sha256']=manual_intake_request_sha256(**intake)
    _,receipt=admit_manual_devices(base,**intake)
    rules=CircuitDesignRules(max_devices_per_circuit=2,assumed_voltage_v=120,assumed_poles=1,route_type='emt',nominal_diameter_m=.0213,conductor_material='copper',conductor_size='12 AWG',insulated_conductor_count=2,include_ground=True)
    controls=RoutingOptions(bend_penalty_m=.4,max_bends=8,port_stub_m=0.,clearance_m=.02,search_margin_m=.5,corridor_tolerance_m=.03,bundle_alignment_tolerance_m=.02,preferred_corridor_discount=0.,surface_path_discount=0.,glazed_wall_penalty=7.,soft_obstacle_penalty_factor=4.,vertical_cost_factor=1.,coordinate_precision=9)
    domain={'id':'domain:explicit','kind':'user-supplied','volumes':[{'pose':{'position':{'x':1,'y':0,'z':0},'rotation':{'x':0,'y':0,'z':0,'w':1}},'size':{'x':6,'y':6,'z':6}}],'source_ids':[]}
    decision={'decision_id':'decision:synthetic','candidate_model_sha256':receipt['candidate_model_sha256'],'intake_request_sha256':receipt['request_sha256'],'equipment_id':panel.id,'source_port_id':source.id,'groups':[['device:manual-1','device:manual-2']],
      'load_ports':{'device:manual-1':'port:manual-1','device:manual-2':'port:manual-2'},'rules':asdict(rules),'routing_controls':asdict(controls),'allowed_domain':domain}
    return base,intake,decision

def run(base,intake,decision):return design_manual_intake(base,intake_request=intake,decision=decision,decision_sha256=manual_design_decision_sha256(decision))


def test_explicit_manual_intake_to_preliminary_circuit():
    base,intake,decision=setup();before=base.to_json();result,report=run(base,intake,decision)
    assert len(result.circuits)==1 and len(result.routes)==2
    assert report['new_circuit_ids']==[result.circuits[0].id] and set(report['new_route_ids'])=={r.id for r in result.routes}
    assert report['source_observed'] is False and report['applied_to_original'] is False
    assert report['unresolved_items'][0]['source_item_id']=='item:bulk'
    assert base.to_json()==before and not base.circuits


def test_stale_and_incomplete_decisions_are_rejected():
    base,intake,decision=setup();digest=manual_design_decision_sha256(decision)
    bad=copy.deepcopy(decision);bad['decision_id']='changed'
    with pytest.raises(ValueError,match='decision binding'):design_manual_intake(base,intake_request=intake,decision=bad,decision_sha256=digest)
    for change in ('candidate','groups','mapping','rules','controls','domain'):
        bad=copy.deepcopy(decision)
        if change=='candidate':bad['candidate_model_sha256']='0'*64
        if change=='groups':bad['groups']=[['device:manual-1']]
        if change=='mapping':bad['load_ports']['device:manual-1']='port:wrong'
        if change=='rules':del bad['rules']['conductor_size']
        if change=='controls':del bad['routing_controls']['clearance_m']
        if change=='domain':bad['allowed_domain']=None
        with pytest.raises((ValueError,TypeError)):run(base,intake,bad)


def test_bool_rules_and_out_of_range_routing_controls_fail():
    base,intake,decision=setup()
    for section,key,value in [('rules','max_devices_per_circuit',True),('rules','include_ground',1),('routing_controls','coordinate_precision',2),('routing_controls','preferred_corridor_discount',1)]:
        bad=copy.deepcopy(decision);bad[section][key]=value
        with pytest.raises(ValueError):run(base,intake,bad)


def test_blocked_source_rejects_without_mutating_original():
    from oabm.model import Obstacle
    base,intake,decision=setup()
    obstacle=Obstacle(id='obstacle:source-blocked',geometry=Box3D(pose=Pose(position=Point3(x=0,y=0,z=0)),size=Size3(x=.5,y=.5,z=.5)))
    base=replace(base,obstacles=(obstacle,))
    intake['base_model_sha256']=model_fingerprint(base)
    intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake)
    decision['candidate_model_sha256']=receipt['candidate_model_sha256'];decision['intake_request_sha256']=receipt['request_sha256']
    before=base.to_json()
    from oabm.routing import NoRouteError
    with pytest.raises(NoRouteError,match='inside hard obstacle'):run(base,intake,decision)
    assert base.to_json()==before and not base.circuits


def test_existing_panel_circuiting_is_never_replaced():
    from oabm.model import ElectricalDevice
    base,intake,decision=setup()
    load=ElectricalDevice(id='device:existing',device_type='receptacle',pose=Pose(position=Point3(x=4,y=0,z=0)),level_id='level:ground')
    port=Port(id='port:existing',owner_id=load.id,domain='electrical',role='load',pose=load.pose,direction=Vector3(x=-1,y=0,z=0))
    existing=Circuit(id='circuit:existing',source_port_id='port:source',load_port_ids=(port.id,))
    base=replace(base,electrical_devices=(load,),ports=(*base.ports,port),circuits=(existing,))
    intake['base_model_sha256']=model_fingerprint(base)
    intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake)
    decision['candidate_model_sha256']=receipt['candidate_model_sha256'];decision['intake_request_sha256']=receipt['request_sha256']
    before=base.to_json()
    with pytest.raises(ValueError,match='already has circuiting'):run(base,intake,decision)
    assert base.to_json()==before and base.circuits==(existing,)


def test_public_and_packaged_decision_schemas_match():
    import json
    from importlib.resources import files
    root=Path(__file__).resolve().parents[1]
    for name in ('manual-design-decision-v1.schema.json','manual-design-report-v1.schema.json'):
        assert json.loads((root/'contracts'/name).read_text())==json.loads(files('oabm.electrical._manual_design_contracts').joinpath(name).read_text())


def test_more_than_four_admitted_devices_are_refused():
    base,intake,decision=setup()
    extra=[item(3),item(4),item(5)]
    for row in extra:row['port_direction']={'x':-1,'y':0,'z':0}
    intake['items']=[*intake['items'],*extra]
    intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake)
    decision['candidate_model_sha256']=receipt['candidate_model_sha256'];decision['intake_request_sha256']=receipt['request_sha256']
    with pytest.raises(ValueError,match='at most four devices'):run(base,intake,decision)


def test_integral_float_controls_use_same_normalized_decision_and_result():
    base,intake,decision=setup();a,ar=run(base,intake,decision)
    other=copy.deepcopy(decision);other['routing_controls']['coordinate_precision']=9.0;other['routing_controls']['max_bends']=8.0;other['rules']['assumed_poles']=1.0
    b,br=run(base,intake,other)
    assert ar['decision_sha256']==br['decision_sha256'] and a.to_json()==b.to_json()


def test_existing_panel_route_without_circuit_is_refused():
    from oabm.model import ElectricalDevice,Route,Polyline3D
    base,intake,decision=setup()
    load=ElectricalDevice(id='device:existing',device_type='receptacle',pose=Pose(position=Point3(x=4,y=0,z=0)),level_id='level:ground')
    port=Port(id='port:existing',owner_id=load.id,domain='electrical',role='load',pose=load.pose,direction=Vector3(x=-1,y=0,z=0))
    route=Route(id='route:existing',route_type='emt',start_port_id='port:source',end_port_id=port.id,centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=4,y=0,z=0))))
    base=replace(base,electrical_devices=(load,),ports=(*base.ports,port),routes=(route,))
    intake['base_model_sha256']=model_fingerprint(base);intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake);decision['candidate_model_sha256']=receipt['candidate_model_sha256'];decision['intake_request_sha256']=receipt['request_sha256']
    before=base.to_json()
    with pytest.raises(ValueError,match='connected routes'):run(base,intake,decision)
    assert base.to_json()==before


def test_oversized_model_entity_budget_is_refused_before_routing(monkeypatch):
    base,intake,decision=setup()
    base=replace(base,levels=(*base.levels,*(Level(id=f'level:extra-{i}',elevation_m=i) for i in range(510))))
    intake['base_model_sha256']=model_fingerprint(base)
    intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake)
    decision['candidate_model_sha256']=receipt['candidate_model_sha256'];decision['intake_request_sha256']=receipt['request_sha256']
    called=[]
    monkeypatch.setattr('oabm.electrical.manual_preliminary_design.design_proposed_circuits',lambda *a,**k:called.append(True))
    with pytest.raises(ValueError,match='entity limit'):run(base,intake,decision)
    assert called==[]
