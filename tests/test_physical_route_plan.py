from dataclasses import replace
import json
import math
import xml.etree.ElementTree as ET
import pytest

from oabm.drawings.physical_routes import generate_physical_route_plan, main
from oabm.model import BuildingModel, ContractError, Box3D, Obstacle, Pose, Point3, Size3, Quaternion
from oabm.reports.physical_routes import generate_physical_route_schedule
from test_physical_route_schedule import measured_model

NS={'s':'http://www.w3.org/2000/svg'}


def test_arc_is_native_scaled_curve_with_actual_od_and_bound_3d_measurement():
    model=measured_model(); before=model.to_json()
    plan=generate_physical_route_plan(model,scale=25)
    root=ET.fromstring(plan.svg)
    arc=[p for p in root.findall('.//s:path',NS) if ' A ' in p.attrib['d']]
    assert len(arc)==1
    assert ' A 10 10 0 0 0 ' in arc[0].attrib['d']
    assert arc[0].attrib['stroke-width']=='0.8'
    assert model.routes[0].segments[1].id in arc[0].attrib['data-source-ids']
    assert root.attrib['width'].endswith('mm')
    assert root.attrib['data-source-model-sha256']==plan.schedule.to_dict()['source']['sha256']
    assert plan.schedule.to_json()==generate_physical_route_schedule(model).to_json()
    assert f'{3+.125*math.pi:.9f}' in plan.svg
    assert '1 quarter bends' in plan.svg
    assert model.to_json()==before
    assert generate_physical_route_plan(model).svg==plan.svg


def test_vertical_plane_arcs_are_marked_centerlines_with_full_3d_lengths():
    model=measured_model()
    # Rotate the entire connected example about +X: old Y becomes model Z.
    data=model.to_dict()
    def turn(v):
        v['y'],v['z']=-v['z'],v['y']
    for device in data['electrical_devices']:turn(device['pose']['position'])
    for port in data['ports']:
        turn(port['pose']['position']);turn(port['direction'])
    route=data['routes'][0]
    for joint in route['joints']:turn(joint['position'])
    # canonical positive normal changes sign, so invert turn as well.
    arc=route['segments'][1]
    arc['plane_normal']={'x':0,'y':1,'z':0};arc['turn']='negative_quarter'
    model=BuildingModel.from_dict(data)
    plan=generate_physical_route_plan(model)
    root=ET.fromstring(plan.svg)
    paths=root.findall('.//s:path',NS)
    assert not any(' A ' in p.attrib['d'] for p in paths)
    assert any('stroke-dasharray' in p.attrib for p in paths)
    assert 'CENTERLINE ONLY' in plan.svg and 'V1' in plan.svg
    assert 'delta +0.25 m' in plan.svg
    assert plan.schedule.to_dict()['routes'][0]['length']['total_m_approx']==pytest.approx(3+.125*math.pi)


def test_obstacles_heights_endpoints_omissions_and_xml_escaping():
    model=measured_model()
    obstacle=Obstacle(id='obstacle:box',geometry=Box3D(pose=Pose(position=Point3(x=1,y=1,z=4)),size=Size3(x=.5,y=.6,z=1)))
    model=replace(model,obstacles=(obstacle,),routes=(replace(model.routes[0],name='<script>'),))
    plan=generate_physical_route_plan(model)
    root=ET.fromstring(plan.svg)
    assert root.find('.//s:rect[@data-source-ids="obstacle:box"]',NS) is not None
    assert 'model Z 3.5 to 4.5 m' in plan.svg
    assert 'port:a' in plan.svg and 'port:b' in plan.svg
    assert 'Other collections omitted' in plan.svg
    assert '<script>' not in plan.svg
    rotated=replace(obstacle,geometry=replace(obstacle.geometry,pose=replace(obstacle.geometry.pose,rotation=Quaternion(x=0,y=0,z=1,w=0))))
    with pytest.raises(ContractError,match='rotation'):
        generate_physical_route_plan(replace(model,obstacles=(rotated,)))


@pytest.mark.parametrize('scale',[0,True,float('nan'),1001])
def test_bad_scale_refuses(scale):
    with pytest.raises(ContractError):generate_physical_route_plan(measured_model(),scale=scale)


def test_cli_and_existing_consumer_refusal(tmp_path):
    model=measured_model();source=tmp_path/'input.json';source.write_text(model.to_json())
    output=tmp_path/'drawing'
    assert main([str(source),str(output)])==0
    assert set(p.name for p in output.iterdir())=={'plan.svg','model.json','schedule.json','routes.csv','segments.csv'}
    assert json.loads((output/'schedule.json').read_text())==generate_physical_route_schedule(model).to_dict()
    with pytest.raises(SystemExit):main([str(source),str(output)])
    from oabm.drawings import generate_plan, PlanSpec
    with pytest.raises(ContractError):generate_plan(model,PlanSpec(id='view:test',level_id='level:test'))


def test_empty_refuses_before_writing(tmp_path):
    source=tmp_path/'empty.json';source.write_text(BuildingModel(model_id='model:empty').to_json())
    with pytest.raises(SystemExit):main([str(source),str(tmp_path/'out')])
    assert not (tmp_path/'out').exists()


def test_both_xy_sweep_directions_are_preserved():
    root=ET.fromstring(generate_physical_route_plan(measured_model(two=True)).svg)
    arcs=[p.attrib['d'] for p in root.findall('.//s:path',NS) if ' A ' in p.attrib['d']]
    assert len(arcs)==2
    assert any('0 0 0' in path for path in arcs)
    assert any('0 0 1' in path for path in arcs)


def test_mixed_legacy_routes_are_listed_without_drawing_them():
    from oabm.model import Route, Polyline3D
    model=measured_model()
    legacy=Route(id='route:legacy',route_type='emt',start_port_id='port:a',end_port_id='port:b',centerline=Polyline3D(points=tuple(p.pose.position for p in model.ports)))
    plan=generate_physical_route_plan(replace(model,routes=(legacy,*model.routes)))
    assert 'Excluded legacy routes: route:legacy' in plan.svg
    assert plan.schedule.to_dict()['totals']['excluded_legacy_route_count']==1
    root=ET.fromstring(plan.svg)
    assert all('route:legacy' not in p.attrib.get('data-source-ids','') for p in root.findall('.//s:path',NS))


def test_unsupported_obstacle_geometry_and_oversized_print_fail_explicitly():
    from oabm.model import Polyline3D
    model=measured_model()
    obstacle=Obstacle(id='obstacle:line',geometry=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=1,y=0,z=0))))
    with pytest.raises(ContractError,match='Box3D'):
        generate_physical_route_plan(replace(model,obstacles=(obstacle,)))
    with pytest.raises(ContractError,match='extent'):
        generate_physical_route_plan(model,scale=1)
