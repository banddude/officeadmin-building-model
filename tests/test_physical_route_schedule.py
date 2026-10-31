import csv
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import pytest
from oabm.model import *
from oabm.reports.physical_routes import (
    generate_physical_route_schedule, validate_physical_route_schedule, main,
)
from oabm.routing.physical_generation_math import realize
from oabm.routing.physical_generation_adapter import canonical_candidate
from physical_route_fixture import make_physical_source, make_physical_route, make_physical_model


def measured_model(two=False, odd=False, name=None, route_type='emt'):
    source=make_physical_source();template=make_physical_route(source)
    radius=250000001 if odd else 250000000
    points=((0,0,0),(1250000000,0,0),(1250000000,1500000000,0),(2500000000,1500000000,0)) if two else ((0,0,0),(1000000000+radius,0,0),(1000000000+radius,2000000000+radius,0))
    end=Point3(x=points[-1][0]/1e9,y=points[-1][1]/1e9,z=0)
    direction=Vector3(x=-1,y=0,z=0) if two else Vector3(x=0,y=-1,z=0)
    source=replace(source,ports=(source.ports[0],replace(source.ports[1],pose=Pose(position=end),direction=direction)),electrical_devices=(source.electrical_devices[0],replace(source.electrical_devices[1],pose=Pose(position=end))))
    requirements=replace(template.requirements,centerline_radius_m=radius/1e9)
    context=replace(template.context,route_type=route_type,source_model_sha256=compute_physical_source_sha256(source),allowed_domain=replace(template.context.allowed_domain,volumes=(Box3D(pose=Pose(position=Point3(x=0,y=0,z=0)),size=Size3(x=20,y=20,z=20)),)))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    shape=realize(points,radius,(1,0,0),(1,0,0) if two else (0,1,0))
    route=replace(canonical_candidate(source,requirements,context,shape),name=name)
    return replace(source,routes=(route,))


def test_one_bend_uses_analytic_arc_not_chord_or_control_polygon():
    model=measured_model();before=model.to_json()
    report=generate_physical_route_schedule(model);document=report.to_dict();row=document['routes'][0]
    assert row['length']['straight_ticks']==3000000000
    assert row['length']['pi_coefficient_ticks']=={'numerator':125000000,'denominator':1}
    assert row['length']['curved_m_approx']==pytest.approx(.125*math.pi)
    assert row['length']['total_m_approx']==pytest.approx(3+.125*math.pi)
    assert row['quarter_bend_count']==1 and row['absolute_turn_degrees']==90
    assert row['outside_diameter_m']==.02 and row['centerline_radius_m']==.25
    assert len(document['segments'])==3
    assert document['segments'][1]['source_segment']['kind']=='quarter_arc'
    assert document['segments'][1]['source_start_joint']['id']==model.routes[0].joints[1].id
    assert document['live_clearance_verification_performed'] is False
    assert model.to_json()==before
    assert generate_physical_route_schedule(model).to_json()==report.to_json()
    report.validate_against_model(model)


def test_opposite_turns_do_not_cancel():
    row=generate_physical_route_schedule(measured_model(two=True)).to_dict()['routes'][0]
    assert row['length']['straight_ticks']==3000000000
    assert row['length']['pi_coefficient_ticks']=={'numerator':250000000,'denominator':1}
    assert row['length']['total_m_approx']==pytest.approx(3+.25*math.pi)
    assert row['quarter_bend_count']==2 and row['absolute_turn_degrees']==180


def test_odd_nanometre_radius_retains_half_tick():
    row=generate_physical_route_schedule(measured_model(odd=True)).to_dict()['routes'][0]
    assert row['length']['straight_ticks']==3000000000
    assert row['length']['pi_coefficient_ticks']=={'numerator':250000001,'denominator':2}
    straight=generate_physical_route_schedule(make_physical_model()).to_dict()['routes'][0]
    assert straight['length']['pi_coefficient_ticks']=={'numerator':0,'denominator':1}
    assert straight['quarter_bend_count']==0 and straight['length']['total_m_approx']==2


def test_mixed_model_exclusions_are_in_json_and_csv_without_zero_measurements():
    model=measured_model()
    legacy=Route(id='route:legacy',route_type='emt',start_port_id='port:a',end_port_id='port:b',centerline=Polyline3D(points=tuple(p.pose.position for p in model.ports)))
    report=generate_physical_route_schedule(replace(model,routes=(legacy,*model.routes)))
    document=report.to_dict()
    assert document['scope']['excluded_legacy_route_ids']==['route:legacy']
    assert document['totals']['measured_route_count']==1
    assert document['totals']['excluded_legacy_route_count']==1
    rows=list(csv.DictReader(io.StringIO(report.routes_csv())))
    assert [row['status'] for row in rows]==['measured_physical_route','excluded_legacy_route']
    assert rows[1]['total_centerline_m_approx']==''
    assert document['totals']['length']==document['routes'][0]['length']


def test_empty_model_is_zero_measured_scope_not_complete_takeoff():
    report=generate_physical_route_schedule(BuildingModel(model_id='model:empty'))
    assert report.to_dict()['totals']['measured_route_count']==0
    assert report.to_dict()['scope']['other_model_entities_measured'] is False
    assert len(list(csv.reader(io.StringIO(report.routes_csv()))))==1
    assert report.to_dict()['limitations']


def test_report_detaches_and_detects_changed_source_and_tampered_measurement():
    model=measured_model();report=generate_physical_route_schedule(model)
    altered=report.to_dict();altered['routes'][0]['length']['total_m_approx']=999
    assert report.to_dict()['routes'][0]['length']['total_m_approx']!=999
    with pytest.raises(ContractError):validate_physical_route_schedule(json.dumps(altered),model)
    assert validate_physical_route_schedule(report.to_json(indent=2),model).to_json()==report.to_json()
    changed=replace(model,attributes={'unrelated_source_change':True})
    with pytest.raises(ContractError):report.validate_against_model(changed)
    assert generate_physical_route_schedule(changed).to_dict()['routes']==report.to_dict()['routes']


@pytest.mark.parametrize('text',['=SUM(1,2)','+1','-1','@function','  =1','\tplain','\rplain','\u00a0=1','\ufeff =1'])
def test_csv_neutralizes_formula_like_source_text_without_changing_json(text):
    report=generate_physical_route_schedule(measured_model(name=text,route_type=text))
    row=next(csv.DictReader(io.StringIO(report.routes_csv())))
    assert row['route_name'].startswith("'") and row['route_type'].startswith("'")
    assert report.to_dict()['routes'][0]['route_name']==text
    assert report.to_dict()['routes'][0]['route_type']==text


def test_csv_preserves_quoted_multiline_text_and_handles_non_utf8_surrogate():
    text='name, with "quotes"\nand next line'
    report=generate_physical_route_schedule(measured_model(name=text))
    rows=list(csv.DictReader(io.StringIO(report.routes_csv())))
    assert len(rows)==1 and rows[0]['route_name']==text
    report=generate_physical_route_schedule(measured_model(name='\ud800'))
    assert next(csv.DictReader(io.StringIO(report.routes_csv())))['route_name']=='\\ud800'
    assert report.to_dict()['routes'][0]['route_name']=='\ud800'
    report.routes_csv().encode('utf-8')


def test_cli_writes_connected_artifacts_and_never_overwrites(tmp_path,capsys):
    source=tmp_path/'input.json';source.write_text(measured_model().to_json())
    output=tmp_path/'schedule'
    assert main([str(source),str(output)])==0
    assert {p.name for p in output.iterdir()}=={'model.json','report.json','routes.csv','segments.csv'}
    model=BuildingModel.from_json((output/'model.json').read_bytes())
    report=validate_physical_route_schedule((output/'report.json').read_bytes(),model)
    assert (output/'routes.csv').read_text()==report.routes_csv()
    assert len(list(csv.DictReader(io.StringIO((output/'segments.csv').read_text()))))==3
    before={p.name:p.read_bytes() for p in output.iterdir()}
    with pytest.raises(SystemExit) as exc:main([str(source),str(output)])
    assert exc.value.code==2
    assert {p.name:p.read_bytes() for p in output.iterdir()}==before


def test_bad_source_fails_before_output_creation(tmp_path):
    source=tmp_path/'invalid.json';source.write_text('{bad')
    output=tmp_path/'schedule'
    with pytest.raises(SystemExit):main([str(source),str(output)])
    assert not output.exists()
