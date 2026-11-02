"""Analytic finite-story projections and the integrated as-built consumer."""
from dataclasses import replace
import json
import math

import pytest

from oabm.model import (ElectricalEquipment,Level,Point3,Pose,Vector3,
    compute_physical_source_sha256,compute_physical_request_sha256,validate_model)
from oabm.drawings import Bounds2,ProjectionFrame
from oabm.drawings.route_arcs import physical_story_segments,physical_plan_primitives
from oabm.routing.physical_generation_math import realize
from oabm.routing.physical_generation_adapter import canonical_candidate
from physical_route_fixture import make_physical_source,make_physical_route
from oabm.pipeline.as_built import VERSION,start_history,replay_history,snapshot,record_sha256,_sha
from oabm.pipeline.as_built_delivery import create_as_built_delivery


def vertical_bend_model(axis='x',descending=False):
    source=make_physical_source();template=make_physical_route(source)
    points=((0,0,0),(1250000000,0,0),(1250000000,0,7000000000))
    direction=(1,0,0)
    if axis=='y':
        points=tuple((y,x,z) for x,y,z in points);direction=(0,1,0)
    if descending:
        points=tuple((x,y,7000000000-z) for x,y,z in points)
    start,end=(Point3(x=p[0]/1e9,y=p[1]/1e9,z=p[2]/1e9) for p in (points[0],points[-1]))
    levels=tuple(Level(id=f'level:{i}',name=f'Story {i}',elevation_m=i*3,height_m=3) for i in range(3))
    poses=(Pose(position=start),Pose(position=end))
    source=replace(source,levels=levels,
        electrical_equipment=(ElectricalEquipment(id='panel:review',equipment_type='panelboard',pose=poses[0],level_id=levels[0].id),),
        electrical_devices=tuple(replace(device,pose=pose,level_id=levels[2 if pose.position.z else 0].id)
            for device,pose in zip(source.electrical_devices,poses)),
        ports=(replace(source.ports[0],pose=poses[0],direction=Vector3(x=direction[0],y=direction[1],z=0)),
               replace(source.ports[1],pose=poses[1],direction=Vector3(x=0,y=0,z=1 if descending else -1))))
    requirements=replace(template.requirements,centerline_radius_m=.25)
    context=replace(template.context,source_model_sha256=compute_physical_source_sha256(source),
        allowed_domain=replace(template.context.allowed_domain,volumes=(replace(template.context.allowed_domain.volumes[0],
            pose=Pose(position=Point3(x=0,y=0,z=3)),size=replace(template.context.allowed_domain.volumes[0].size,x=20,y=20,z=20)),)))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    shape=realize(points,250000000,direction,(0,0,-1 if descending else 1))
    model=replace(source,routes=(canonical_candidate(source,requirements,context,shape),))
    validate_model(model)
    return model


@pytest.mark.parametrize('axis',['x','y'])
@pytest.mark.parametrize('descending',[False,True])
def test_vertical_plane_arc_is_analytically_clipped_not_a_3d_chord(axis,descending):
    model=vertical_bend_model(axis,descending);route=model.routes[0];before=model.to_json()
    lo,hi=(.125,.25) if not descending else (6.75,6.875)
    fragments=physical_story_segments(route,lo,hi)
    arc=next(row for row in fragments if row[0].kind=='quarter_arc')
    _,fact,a,b,center=arc
    for point in (a,b):
        assert (getattr(point,axis)-getattr(center,axis))**2+(point.z-center.z)**2==pytest.approx(.25**2)
    assert min(a.z,b.z)==lo and max(a.z,b.z)==hi
    # A chord at half Z would produce .125 lateral displacement, not sqrt(3)/8.
    clipped=a if a.z not in {0,.25,6.75,7} else b
    assert abs(getattr(clipped,axis)-getattr(center,axis))==pytest.approx(math.sqrt(3)/8)
    frame=ProjectionFrame(origin=Point3(x=0,y=0,z=0),right=Vector3(x=1,y=0,z=0),
        up=Vector3(x=0,y=1,z=0),normal=Vector3(x=0,y=0,z=1))
    primitives=physical_plan_primitives(route,frame,Bounds2(-1,-1,3,3),depth_interval=(lo,hi))
    projected=next(p for p in primitives if p.layer=='electrical:routes' and arc[0].id in p.source_ids)
    assert projected.kind=='line' and projected.style.pattern=='dash'
    evidence=json.loads(dict(projected.metadata)['analytic_source_and_clip'])
    assert evidence['kind']=='quarter_arc' and evidence['clip_interval_z_m']==[lo,hi]
    assert 'not a 3D chord' in evidence['projection']
    assert any('XY projection only' in (p.text or '') for p in primitives)
    assert model.to_json()==before


def test_three_story_physical_riser_reaches_complete_as_built_delivery(tmp_path):
    model=vertical_bend_model();history=start_history(model);parent=replay_history(history)
    record=parent.to_dict()['routes'][0];statement='Synthetic geometric-only route reaffirmation; products unresolved.'
    request={'version':VERSION,'transaction_id':'field:physical-plan','actor':'synthetic-review','reason':statement,
        'design_sha256':history['design_sha256'],'parent_sha256':_sha(snapshot(parent)),
        'evidence':[{'id':'evidence:physical','kind':'quick_edit','reference':'synthetic-inline',
                     'sha256':_sha(statement),'statement':statement}],
        'edits':[{'collection':'routes','id':record['id'],'expected_sha256':record_sha256(record),
                  'record':record,'evidence_ids':['evidence:physical']}],
        'method_reconciliations':[],'installed_assertions':[]}
    (tmp_path/'history.json').write_text(json.dumps(history));(tmp_path/'request.json').write_text(json.dumps(request))
    root=tmp_path/'attempt'
    manifest=create_as_built_delivery(tmp_path/'history.json',tmp_path/'request.json',output_directory=root)
    delivery=root/'delivery';plans=json.loads((delivery/'drawings/plans.json').read_text())['views']
    layouts=json.loads((delivery/'drawings/plan-layout.json').read_text())['plans']
    assert len(plans)==3
    for view in plans:
        assert any(p['symbol']=='route-vertical-projection' for p in view['primitives'] if p['kind']=='symbol')
        layout=next(row for row in layouts if row['view_id']==view['id'])
        notes=''.join(label for entry in layout['entries'] for label in entry['labels'])
        assert 'no OD/clearance' in notes
        assert any('analytic_source_and_clip' in dict(p['metadata']) for p in view['primitives'])
    assert len(list((delivery/'drawings').glob('plan-*.svg')))==3
    assert list((delivery/'drawings').glob('panel-*.svg'))
    assert manifest['revised_model_sha256']
    assert (delivery/'design-model.json').read_text()==history['design_json']
    materials=json.loads((delivery/'materials.json').read_text())
    assert not materials['installed_items']
    assert any(model.routes[0].id in row['source_entity_ids'] for row in materials['unresolved_items'])
