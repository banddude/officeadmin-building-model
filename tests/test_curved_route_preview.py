"""Known-radius display geometry, default compatibility and explicit limits."""
from dataclasses import replace
import json
import math
from pathlib import Path
import struct

import pytest
from oabm.model import BuildingModel, Point3, Circuit, Conductor
from oabm.exports.curved_routes import fillet_points, sweep_vertices
from oabm.exports.gltf import build_glb_bytes, to_glb


def point(x,y,z):return Point3(x=x,y=y,z=z)


def coords(p):return (p.x,p.y,p.z)


def model():
    original=BuildingModel.load(Path(__file__).parents[1]/'fixtures/model_delivery/v1/model.json')
    route=replace(original.routes[0],nominal_diameter_m=.02)
    circuit=Circuit(id='circuit:curve',source_port_id=route.start_port_id,load_port_ids=(route.end_port_id,),route_ids=(route.id,))
    wire=Conductor(id='conductor:curve',circuit_id=circuit.id,role='phase',count=3,route_ids=(route.id,))
    return replace(original,routes=(route,),circuits=(circuit,),conductors=(wire,))


def document(payload):
    size=struct.unpack_from('<I',payload,12)[0]
    return json.loads(payload[20:20+size])


@pytest.mark.parametrize('vertical',[False,True])
def test_quarter_circle_tangency_radius_endpoints_and_length(vertical):
    p=(point(0,0,0),point(1,0,0),point(1,0,1) if vertical else point(1,1,0))
    result=fillet_points(p,.2)
    assert result==fillet_points(p,.2)
    assert result[0]==p[0] and result[-1]==p[-1]
    center=(.8,0,.2) if vertical else (.8,.2,0)
    assert coords(result[1])==pytest.approx((.8,0,0))
    assert coords(result[-2])==pytest.approx((1,0,.2) if vertical else (1,.2,0))
    assert all(math.dist(coords(q),center)==pytest.approx(.2) for q in result[1:-1])
    assert p[1] not in result
    length=sum(math.dist(coords(a),coords(b)) for a,b in zip(result,result[1:]))
    assert length==pytest.approx(1.6+math.pi*.1,abs=.0002)
    vertices=sweep_vertices(result,.01)
    assert vertices and len(vertices)%3==0
    assert all(math.isfinite(x) for v in vertices for x in v)


def test_redundant_straight_vertex_does_not_shorten_available_budget():
    assert fillet_points((point(0,0,0),point(.5,0,0),point(1,0,0),point(1,1,0)),.8)==fillet_points((point(0,0,0),point(1,0,0),point(1,1,0)),.8)


@pytest.mark.parametrize('points',[
    (point(0,0,0),point(.1,0,0),point(.1,.1,0)),
    (point(0,0,0),point(1,0,0),point(0,0,0)),
    (point(0,0,0),point(0,0,0)),
])
def test_degenerate_or_too_short_geometry_refuses(points):
    with pytest.raises(ValueError):fillet_points(points,.2)


def test_glb_opt_in_discloses_curve_and_preserves_model_and_default_bytes():
    source=model();before=source.to_json()
    default=build_glb_bytes(source)
    assert default==build_glb_bytes(source,route_bend_radii_m={})
    changed=build_glb_bytes(source,route_bend_radii_m={'route:synthetic':.3})
    assert changed!=default and source.to_json()==before
    nodes=document(changed)['nodes']
    route=next(n for n in nodes if n['name']=='route:synthetic')
    proof=route['extras']['curved_preview']
    assert proof['centerline_radius_m']==.3
    assert proof['clearance_verified'] is False
    assert proof['manufacturer_fitting_geometry'] is False
    assert proof['sampled_centerline_length_m']<route['extras']['length_m']
    wires=[n for n in nodes if n['name'].startswith('conductor:')]
    assert len(wires)==3 and all('curved_preview' in n['extras'] for n in wires)
    assert 'curved_preview' not in next(n for n in document(default)['nodes'] if n['name']=='route:synthetic')['extras']


@pytest.mark.parametrize('radii',[{'route:synthetic':True},{'route:synthetic':float('nan')},
    {'route:missing':.2},{'route:synthetic':.001},{'route:synthetic':100}])
def test_invalid_or_impossible_radius_fails_before_output(tmp_path,radii):
    target=tmp_path/'preview.glb'
    with pytest.raises(ValueError):to_glb(model(),target,route_bend_radii_m=radii)
    assert not target.exists()


def test_wire_sweep_uses_same_curve_frames_without_axis_flips():
    points=fillet_points((point(0,0,0),point(1,0,0),point(1,0,1)),.2)
    vertices=sweep_vertices(points,.0015,offset_m=.003,offset_angle=math.pi/3)
    assert all(math.isfinite(x) for p in vertices for x in p)
    assert len(vertices)==len(sweep_vertices(points,.01))


def test_delivery_passes_explicit_preview_option_and_preserves_source(tmp_path):
    from oabm.exports.model_delivery import deliver_model
    source=model();path=tmp_path/'model.json';path.write_text(source.to_json())
    config={'version':'model-delivery-v1','plans':[],'preview_glb':True,'bend_policy':'require',
            'radii_m':{'route:synthetic':.3},'preview_bends':'caller_radius'}
    options=tmp_path/'options.json';options.write_text(json.dumps(config))
    delivery=deliver_model(path,options,output_directory=tmp_path/'attempt')
    manifest=json.loads((delivery/'MANIFEST.json').read_bytes())
    assert manifest['glb_options']=={'route_bend_radii_m':config['radii_m']}
    assert 'swept_clearance_unverified' in manifest['status']['preview_geometry']
    assert 'Canonical polylines and quantity lengths are unchanged' in (delivery/'READ-ME.txt').read_text()
    assert BuildingModel.load(delivery/'canonical-model.json').to_json()==source.to_json()
    assert any('curved_preview' in n.get('extras',{}) for n in document((delivery/'preview.glb').read_bytes())['nodes'])


@pytest.mark.parametrize('change',[{'bend_policy':'report'},{'bend_policy':'skip'},
    {'preview_glb':False},{'radii_m':{}},{'preview_bends':'guess'}])
def test_delivery_refuses_ignored_or_unguarded_curve_option(change):
    from oabm.exports.model_delivery import _options
    config={'version':'model-delivery-v1','plans':[],'preview_glb':True,'bend_policy':'require',
            'radii_m':{'route:synthetic':.3},'preview_bends':'caller_radius',**change}
    with pytest.raises(ValueError):_options(json.dumps(config).encode())


def test_selected_fitting_envelope_is_not_silently_relocated():
    from test_fitting_consumers import _selected
    source=_selected(envelope=True)
    with pytest.raises(ValueError,match='fitting envelopes'):
        build_glb_bytes(source,route_bend_radii_m={source.routes[0].id:.3})
