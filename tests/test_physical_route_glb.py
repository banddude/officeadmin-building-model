"""Real canonical curved-route meshes, not a sharp-polyline rendering substitute."""
import json, math, struct
from collections import Counter
from dataclasses import replace
import pytest
from oabm.model import *
from oabm.exports.gltf import build_glb_bytes, to_glb
from oabm.exports.physical_routes import physical_route_mesh
from oabm.routing.physical_generation_math import realize
from oabm.routing.physical_generation_adapter import canonical_candidate
from physical_route_fixture import make_physical_source, make_physical_route, supplied


def curved_model(tiny=False):
    source=make_physical_source();template=make_physical_route(source)
    end=Point3(x=2 if tiny else 1,y=.600000001 if tiny else 1,z=0)
    direction=Vector3(x=-1,y=0,z=0) if tiny else Vector3(x=0,y=-1,z=0)
    source=replace(source,ports=(source.ports[0],replace(source.ports[1],pose=Pose(position=end),direction=direction)),electrical_devices=(source.electrical_devices[0],replace(source.electrical_devices[1],pose=Pose(position=end))))
    requirements=replace(template.requirements,start_straight_min_m=0,end_straight_min_m=0,between_bends_straight_min_m=0)
    context=replace(template.context,source_model_sha256=compute_physical_source_sha256(source))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    points=((0,0,0),(1000000000,0,0),(1000000000,600000001,0),(2000000000,600000001,0)) if tiny else ((0,0,0),(1000000000,0,0),(1000000000,1000000000,0))
    shape=realize(points,300000000,(1,0,0),(1,0,0) if tiny else (0,1,0))
    route=canonical_candidate(source,requirements,context,shape)
    return replace(source,routes=(route,))


def unpack(payload):
    size=struct.unpack_from('<I',payload,12)[0]
    document=json.loads(payload[20:20+size]);binary=payload[28+size:]
    return document,binary


def values(doc,binary,index):
    accessor=doc['accessors'][index];view=doc['bufferViews'][accessor['bufferView']]
    count=accessor['count'];stride=3 if accessor['type']=='VEC3' else 1
    fmt='f' if accessor['componentType']==5126 else 'I'
    result=struct.unpack_from('<'+str(count*stride)+fmt,binary,view.get('byteOffset',0)+accessor.get('byteOffset',0))
    return [tuple(result[i:i+stride]) for i in range(0,len(result),stride)] if stride==3 else result


def test_curved_glb_has_real_dimensions_smooth_normals_and_closed_caps():
    model=curved_model();before=model.to_json()
    payload=build_glb_bytes(model,physical_routes=True)
    doc,binary=unpack(payload)
    nodes=[n for n in doc['nodes'] if n.get('extras',{}).get('canonical_id')=='route:physical']
    assert len(nodes)==1;node=nodes[0];extra=node['extras']
    assert extra['geometry_kind']=='line_arc_v1';assert extra['outside_diameter_m']==.02
    assert extra['centerline_radius_m']==.3;assert extra['bend_count']==1
    assert extra['length_m']==pytest.approx(1.4+.15*math.pi)
    assert extra['clearance_verified_by_export'] is False
    primitive=doc['meshes'][node['mesh']]['primitives'][0]
    vertices=values(doc,binary,primitive['attributes']['POSITION'])
    normals=values(doc,binary,primitive['attributes']['NORMAL'])
    indices=values(doc,binary,primitive['indices'])
    assert all(abs(math.sqrt(sum(v*v for v in n))-1)<1e-6 for n in normals)
    edges=Counter()
    for i in range(0,len(indices),3):
        tri=[vertices[j] for j in indices[i:i+3]]
        assert len(set(tri))==3
        for a,b in zip(tri,tri[1:]+tri[:1]):edges[tuple(sorted((a,b)))]+=1
    assert set(edges.values())=={2}
    accessor=doc['accessors'][primitive['attributes']['POSITION']]
    assert accessor['min']==[min(p[i] for p in vertices) for i in range(3)]
    assert accessor['max']==[max(p[i] for p in vertices) for i in range(3)]
    assert build_glb_bytes(model,physical_routes=True)==payload
    assert model.to_json()==before


def test_default_guard_preserves_existing_output(tmp_path):
    target=tmp_path/'prior.glb';target.write_bytes(b'prior')
    with pytest.raises(ContractError):to_glb(curved_model(),target)
    assert target.read_bytes()==b'prior'


@pytest.mark.parametrize('options',[{'reference_planes':True},{'route_bend_radii_m':{}},{'physical_routes':1}])
def test_unsupported_options_refuse(options):
    with pytest.raises((ValueError,ContractError)):
        build_glb_bytes(curved_model(),**{'physical_routes':True,**options})


def test_precision_and_vertex_budget_refusals():
    with pytest.raises(ValueError,match='float32 mesh collapsed'):
        build_glb_bytes(curved_model(tiny=True),physical_routes=True)
    with pytest.raises(ValueError,match='vertex budget'):
        physical_route_mesh(curved_model().routes[0],vertex_budget=1)


def test_obstacles_are_explicit_and_selectable():
    model=curved_model()
    obstacle=Obstacle(id='obstacle:shown',geometry=Box3D(pose=Pose(position=Point3(x=.5,y=.5,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    model=replace(model,obstacles=(obstacle,))
    hidden,_=unpack(build_glb_bytes(model,physical_routes=True))
    shown,_=unpack(build_glb_bytes(model,physical_routes=True,include_obstacles=True,groups={'blockers':(obstacle.id,)}))
    assert not any(n.get('extras',{}).get('canonical_id')==obstacle.id for n in hidden['nodes'])
    node=next(n for n in shown['nodes'] if n.get('extras',{}).get('canonical_id')==obstacle.id)
    assert node['extras']['canonical_type']=='obstacle';assert 'mesh' in node
    assert any(n['name']=='group:blockers' for n in shown['nodes'])


def test_legacy_default_bytes_are_not_changed_by_explicit_false_options():
    model=make_physical_source()
    assert build_glb_bytes(model)==build_glb_bytes(model,physical_routes=False,include_obstacles=False)
