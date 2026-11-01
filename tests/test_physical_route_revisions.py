import csv
from dataclasses import replace
import io
import json
from pathlib import Path

import pytest
from oabm.model import *
from oabm.reports.physical_route_revisions import (
    generate_physical_route_comparison as compare, validate_physical_route_comparison as validate, main,
)
from oabm.routing.physical_generation_math import realize
from oabm.routing.physical_generation_adapter import canonical_candidate
from physical_route_fixture import make_physical_source, make_physical_route, make_physical_model


def detour(sign=1):
    source = make_physical_source()
    end = Point3(x=4, y=0, z=0)
    source = replace(source, ports=(source.ports[0], replace(source.ports[1], pose=Pose(position=end))),
                     electrical_devices=(source.electrical_devices[0], replace(source.electrical_devices[1], pose=Pose(position=end))))
    template = make_physical_route(source)
    context = replace(template.context, allowed_domain=replace(template.context.allowed_domain,
        volumes=(Box3D(pose=Pose(position=Point3(x=2,y=0,z=0)),size=Size3(x=6,y=4,z=4)),)))
    context = replace(context, request_sha256=compute_physical_request_sha256(context, template.requirements))
    points = tuple((x*10**9, y*sign*10**9, 0) for x,y in ((0,0),(1,0),(1,1),(3,1),(3,0),(4,0)))
    route = canonical_candidate(source, template.requirements, context, realize(points,300000000,(1,0,0),(1,0,0)))
    return replace(source, routes=(route,))


def refresh(route, **updates):
    requirements = updates.get('requirements', route.requirements)
    context = updates.get('context', route.context)
    updates['context'] = replace(context, request_sha256=compute_physical_request_sha256(context, requirements))
    return replace(route, **updates)


def rename_children(model):
    data = model.to_dict()
    route = data['routes'][0]
    ids = {row['id']: row['id'] + ':new' for row in route['joints'] + route['segments']}
    def walk(value):
        if isinstance(value, dict): return {k: walk(v) for k,v in value.items()}
        if isinstance(value, list): return [walk(v) for v in value]
        return ids.get(value, value) if isinstance(value, str) else value
    data['routes'][0] = walk(route)
    return BuildingModel.from_dict(data)


def test_same_length_changed_path_and_unchanged_run_do_not_cancel_or_count_twice():
    before, after = detour(1), detour(-1)
    # Separate stable route on the same endpoint pair is structurally permitted;
    # this fixture makes no clearance or live generation claim.
    original = before.routes[0]
    mapping = {j.id: j.id + ':unchanged' for j in original.joints}
    extra = refresh(original, id='route:unchanged',
        joints=tuple(replace(j,id=mapping[j.id],route_id='route:unchanged') for j in original.joints),
        segments=tuple(replace(s,id=s.id+':unchanged',route_id='route:unchanged',start_joint_id=mapping[s.start_joint_id],end_joint_id=mapping[s.end_joint_id]) for s in original.segments),
        requirements=replace(original.requirements,id='requirements:unchanged',route_id='route:unchanged'),
        context=replace(original.context,request_id='request:unchanged',allowed_domain=replace(original.context.allowed_domain,id='domain:unchanged')))
    extra = replace(before,routes=(extra,)).to_dict()['routes'][0]
    for label, model in (('before', before), ('after', after)):
        data = model.to_dict(); data['routes'].append(extra)
        if label == 'before': before = BuildingModel.from_dict(data)
        else: after = BuildingModel.from_dict(data)
    report = compare(before,after); data=report.to_dict()
    assert data['counts'] == {'added':0,'removed':0,'changed':1,'unchanged':1}
    changed = next(row for row in data['routes'] if row['status']=='changed')
    assert changed['path_changed'] is True
    assert changed['delta']['length']['total_m_approx']==0
    assert changed['delta']['quarter_bend_count']==0
    inventory=data['physical_inventory']
    assert inventory['gross_added_route_ids']==['route:physical']
    assert inventory['gross_removed_route_ids']==['route:physical']
    assert inventory['gross_added']['quarter_bend_count']==4
    assert inventory['net']['length']['straight_ticks']==0
    assert validate(report.to_json()).to_json()==report.to_json()
    assert compare(before,after).to_json()==report.to_json()


def test_child_identity_is_separate_from_path():
    model=detour();data=compare(model,rename_children(model)).to_dict()
    row=data['routes'][0]
    assert row['status']=='changed' and row['path_changed'] is False
    assert row['field_change_categories']==['identity']
    assert data['physical_inventory']['gross_added_route_ids']==[]


def test_requirements_metadata_and_context_are_separate():
    before=make_physical_model();route=before.routes[0]
    route=refresh(route, name='=formula', requirements=replace(route.requirements,clearance_m=.02))
    after=replace(before,routes=(route,))
    row=compare(before,after).to_dict()['routes'][0]
    assert row['path_changed'] is False and row['outside_diameter_changed'] is False
    assert row['field_change_categories']==['context','metadata','requirements']
    od=refresh(route,requirements=replace(route.requirements,outside_diameter_m=.03))
    data=compare(after,replace(after,routes=(od,))).to_dict()
    assert data['routes'][0]['outside_diameter_changed'] is True
    assert data['physical_inventory']['gross_added_route_ids']==[route.id]


def test_added_removed_and_legacy_cross_kind_are_visible():
    model=make_physical_model();empty=replace(model,routes=())
    added=compare(empty,model).to_dict()
    removed=compare(model,empty).to_dict()
    assert added['routes'][0]['delta']['length']['straight_ticks']==2000000000
    assert removed['routes'][0]['delta']['length']['straight_ticks']==-2000000000
    legacy=Route(id=model.routes[0].id,route_type='emt',start_port_id='port:a',end_port_id='port:b',
                 centerline=Polyline3D(points=tuple(p.pose.position for p in model.ports)))
    report=compare(model,replace(model,routes=(legacy,)))
    row=report.to_dict()['routes'][0]
    assert row['status']=='changed' and row['legacy_excluded'] and row['delta'] is None
    assert row['before_kind']=='physical' and row['after_kind']=='legacy'
    assert list(csv.DictReader(io.StringIO(report.routes_csv())))[0]['delta_total_m_approx']==''
    assert compare(empty,empty).to_dict()['scope']['complete_takeoff'] is False


@pytest.mark.parametrize('change',[
    {'frame_id':'other'}, {'crs':'EPSG:4326'}, {'origin_in_crs':Point3(x=1,y=0,z=0)},
    {'true_north_radians':.1},
])
def test_complete_coordinate_system_must_match(change):
    # Use empty route snapshots to avoid context frame mismatch masking admission.
    model=replace(make_physical_source(),routes=())
    with pytest.raises(ContractError,match='coordinate_system'):
        compare(model,replace(model,coordinate_system=replace(model.coordinate_system,**change)))


def test_full_source_binding_regeneration_and_tamper_refusal():
    model=make_physical_model();report=compare(model,model)
    report.validate_against_models(model,model)
    changed=replace(model,attributes={'unrelated':True})
    with pytest.raises(ContractError):report.validate_against_models(model,changed)
    for mutate in (lambda d:d['counts'].update(changed=42),
                   lambda d:d['physical_inventory']['net']['length'].update(straight_ticks=1),
                   lambda d:d['sources']['before']['snapshot'].update(attributes={'tampered':1})):
        data=report.to_dict();mutate(data)
        with pytest.raises(ContractError):validate(json.dumps(data))
    with pytest.raises(ContractError):validate('{"kind":1,"kind":2}')


def test_cli_is_fresh_and_csv_id_is_safe(tmp_path):
    model=make_physical_model();data=model.to_dict()
    # Stable route ID is a source-controlled text field, not trusted CSV code.
    data['routes'][0]['name']='  =danger'
    text=json.dumps(data)
    model=BuildingModel.from_json(text)
    source=tmp_path/'source.json';source.write_text(model.to_json())
    output=tmp_path/'out'
    assert main([str(source),str(source),str(output)])==0
    assert validate((output/'comparison.json').read_text()).to_dict()['counts']['unchanged']==1
    assert list(csv.DictReader(io.StringIO((output/'routes.csv').read_text())))[0]['before_name']=="'  =danger"
    with pytest.raises(SystemExit):main([str(source),str(source),str(output)])


def test_original_physical_numeric_tokens_are_not_rounded_by_saved_report_validation():
    report=compare(make_physical_model(),make_physical_model())
    raw=report.to_json()
    assert '"outside_diameter_m":0.02' in raw
    with pytest.raises(ContractError):
        validate(raw.replace('"outside_diameter_m":0.02','"outside_diameter_m":0.020000000000000001'))


def test_exact_signed_rational_bend_and_length_deltas():
    from test_physical_route_schedule import measured_model
    before,after=measured_model(),measured_model(odd=True)
    forward=compare(before,after).to_dict()['routes'][0]['delta']
    reverse=compare(after,before).to_dict()['routes'][0]['delta']
    assert forward['length']['straight_ticks']==0
    assert forward['length']['pi_coefficient_ticks']=={'numerator':1,'denominator':2}
    assert reverse['length']['pi_coefficient_ticks']=={'numerator':-1,'denominator':2}
    empty=replace(before,routes=())
    assert compare(before,empty).to_dict()['routes'][0]['delta']['quarter_bend_count']==-1


def test_renamed_route_is_added_removed_without_inferred_match():
    before=make_physical_model();data=before.to_dict()
    # Requirements route ownership participates in the request hash.
    route=before.routes[0]
    requirements=replace(route.requirements,route_id='route:renamed')
    route=refresh(route,id='route:renamed',requirements=requirements,
                  joints=tuple(replace(j,route_id='route:renamed') for j in route.joints),
                  segments=tuple(replace(s,route_id='route:renamed') for s in route.segments))
    rows=compare(before,replace(before,routes=(route,))).to_dict()['routes']
    assert [row['status'] for row in rows]==['removed','added']


def test_provenance_and_unrelated_obstacle_do_not_claim_path_change():
    before=make_physical_model()
    route=replace(before.routes[0],provenance=(Provenance(source_kind='synthetic',source_id='source:new',derivation='user'),))
    data=compare(before,replace(before,routes=(route,))).to_dict()
    assert data['routes'][0]['field_change_categories']==['provenance']
    assert data['routes'][0]['path_changed'] is False
    assert data['physical_inventory']['gross_added_route_ids']==[]
    obstacle=Obstacle(id='obstacle:added',geometry=Box3D(pose=Pose(position=Point3(x=10,y=10,z=10)),size=Size3(x=1,y=1,z=1)))
    report=compare(before,replace(before,obstacles=(obstacle,)))
    assert report.to_dict()['counts']['unchanged']==1
    assert report.to_dict()['sources']['before']['sha256']!=report.to_dict()['sources']['after']['sha256']
    assert validate(report.to_json()).to_json()==report.to_json()


@pytest.mark.parametrize('before_value,after_value', [
    (False,0), (True,1), ({'nested':[False,{'flag':True}]},{'nested':[0,{'flag':1}]}),
])
def test_metadata_boolean_number_changes_retain_json_types(before_value,after_value):
    model=make_physical_model();route=model.routes[0]
    before=replace(model,routes=(replace(route,attributes={'flag':before_value}),))
    after=replace(model,routes=(replace(route,attributes={'flag':after_value}),))
    report=compare(before,after);document=report.to_dict();row=document['routes'][0]
    assert row['status']=='changed'
    assert row['field_changes'] and row['field_change_categories']==['metadata']
    assert row['path_changed'] is False and row['outside_diameter_changed'] is False
    assert document['physical_inventory']['gross_added_route_ids']==[]
    assert validate(report.to_json()).to_json()==report.to_json()
    for field in row['field_changes']:
        assert type(field['before']) is bool and type(field['after']) is int


@pytest.mark.parametrize('owner', ['route','joint','segment','requirements'])
def test_opaque_attributes_never_acquire_canonical_field_categories(owner):
    model=make_physical_model();route=model.routes[0]
    attributes={'user':{'confidence':False,'provenance':{'id':'before'},'id':'before','route_id':'before'}}
    changed={'user':{'confidence':0,'provenance':{'id':'after'},'id':'after','route_id':'after'}}
    def update(values):
        if owner=='route': return replace(route,attributes=values)
        if owner=='joint': return replace(route,joints=(replace(route.joints[0],attributes=values),*route.joints[1:]))
        if owner=='segment': return replace(route,segments=(replace(route.segments[0],attributes=values),))
        return refresh(route,requirements=replace(route.requirements,attributes=values))
    row=compare(replace(model,routes=(update(attributes),)),replace(model,routes=(update(changed),))).to_dict()['routes'][0]
    # Requirement attributes are request-bound, so the hash changes as context.
    assert set(row['field_change_categories'])==({'metadata','context'} if owner=='requirements' else {'metadata'})
    assert all(change['category']=='metadata' for change in row['field_changes'] if 'attributes' in change['path'])
    assert row['path_changed'] is False


@pytest.mark.parametrize('owner', ['joint','segment','requirements'])
def test_physical_child_name_is_metadata_not_geometry(owner):
    model=make_physical_model();route=model.routes[0]
    if owner=='joint':
        changed=replace(route,joints=(replace(route.joints[0],name='Revised joint label'),*route.joints[1:]))
    elif owner=='segment':
        changed=replace(route,segments=(replace(route.segments[0],name='Revised segment label'),))
    else:
        changed=refresh(route,requirements=replace(route.requirements,name='Revised requirement label'))
    data=compare(model,replace(model,routes=(changed,))).to_dict();row=data['routes'][0]
    assert row['status']=='changed' and row['path_changed'] is False
    assert set(row['field_change_categories'])==({'metadata','context'} if owner=='requirements' else {'metadata'})
    assert data['physical_inventory']['gross_added_route_ids']==[]
