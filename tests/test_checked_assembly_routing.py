"""Real hanger detour, ordered selection, refusal and immutable delivery."""
from dataclasses import replace, asdict
import hashlib,json
import pytest
from oabm.model import Point3, Pose
from oabm.routing import RoutingOptions
from oabm.routing.checked_assembly import AssemblyRouteCandidate, NoCheckedAssemblyRoute, route_checked_assembly
from oabm.routing.checked_delivery import deliver_checked_assembly_route
from test_routing_assembly_producer_integration_356 import _build_source_assembly, _with_synthetic_ports

@pytest.fixture(scope='module')
def source():
    model=_with_synthetic_ports(_build_source_assembly(),y=.3,z=2.4)
    positions=(Point3(x=.25,y=.3,z=2.4),Point3(x=1.55,y=.3,z=2.4))
    return replace(model,
        electrical_equipment=(replace(model.electrical_equipment[0],pose=Pose(position=positions[0])),),
        electrical_devices=(replace(model.electrical_devices[0],pose=Pose(position=positions[1])),),
        ports=tuple(replace(p,pose=Pose(position=q)) for p,q in zip(model.ports,positions)))

def candidates():
    return (AssemblyRouteCandidate('tight',RoutingOptions(port_stub_m=.05,clearance_m=.005)),
            AssemblyRouteCandidate('wide',RoutingOptions(port_stub_m=.10,clearance_m=.17)))

def run(model,**overrides):
    args=dict(assembly_id=model.building_assemblies[0].id,domain_kind='ceiling-plenum',
              nominal_diameter_m=.02,centerline_radius_m=.08,candidates=candidates())
    args.update(overrides)
    return route_checked_assembly(model,'port:plenum-source','port:plenum-load','emt',**args)

def test_bend_failure_selects_next_explicit_candidate(source):
    before=source.to_json();result=run(source)
    assert source.to_json()==before
    assert result.selected_candidate_id=='wide'
    assert [r['status'] for r in result.attempts]==['bend_preflight_failed','selected']
    assert any(d['code']=='short_straight' for d in result.attempts[0]['bend_diagnostics'])
    assert result.bend_report.ready
    assert result.model.routes==(result.route,) and result.model.route_fittings==result.fittings
    assert result.route.attributes['geometry_check']['status']=='contained_and_clear_of_checked_blockers'
    assert result==run(source)
    assert result.attempts[0]['domain_id']!=result.attempts[1]['domain_id']

def test_no_candidate_is_not_partial_success(source):
    before=source.to_json()
    with pytest.raises(NoCheckedAssemblyRoute) as error:run(source,candidates=candidates()[:1])
    assert error.value.attempts[0]['status']=='bend_preflight_failed'
    assert source.to_json()==before and not source.routes

@pytest.mark.parametrize('override',[
    {'centerline_radius_m':None},{'centerline_radius_m':0},{'centerline_radius_m':True},
    {'centerline_radius_m':float('nan')},{'nominal_diameter_m':-1},
    {'domain_kind':'guess'},{'assembly_id':'absent'},{'candidates':()},
    {'candidates':candidates()*5},{'candidates':(candidates()[0],candidates()[0])},
    {'candidates':(AssemblyRouteCandidate('boolean',RoutingOptions(clearance_m=True)),)},
])
def test_invalid_request_is_not_hidden_as_candidate_failure(source,override):
    with pytest.raises(ValueError):run(source,**override)

def test_geometry_failure_has_reason(source):
    impossible=AssemblyRouteCandidate('too-wide',RoutingOptions(clearance_m=2))
    with pytest.raises(NoCheckedAssemblyRoute) as error:run(source,candidates=(impossible,))
    assert error.value.attempts[0]['status']=='geometry_unresolved'
    assert error.value.attempts[0]['reason']

def test_delivery_repeatable_and_never_publishes_failed_candidate(source,tmp_path):
    model=tmp_path/'source.json';model.write_text(source.to_json())
    request={'version':'checked-assembly-route-v1','start_port_id':'port:plenum-source',
        'end_port_id':'port:plenum-load','route_type':'emt','assembly_id':source.building_assemblies[0].id,
        'domain_kind':'ceiling-plenum','nominal_diameter_m':.02,'centerline_radius_m':.08,
        'candidates':[{'id':c.id,'options':asdict(c.options)} for c in candidates()]}
    config=tmp_path/'request.json';config.write_text(json.dumps(request))
    a=deliver_checked_assembly_route(model,config,output_directory=tmp_path/'a')
    b=deliver_checked_assembly_route(model,config,output_directory=tmp_path/'b')
    tree=lambda root:{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert tree(a)==tree(b)
    assert (a/'inputs/model.json').read_bytes()==model.read_bytes()
    for row in json.loads((a/'MANIFEST.json').read_bytes())['files']:
        assert hashlib.sha256((a/row['path']).read_bytes()).hexdigest()==row['sha256']
    with pytest.raises(FileExistsError):deliver_checked_assembly_route(model,config,output_directory=tmp_path/'a')
    request['candidates']=request['candidates'][:1];config.write_text(json.dumps(request))
    with pytest.raises(NoCheckedAssemblyRoute):deliver_checked_assembly_route(model,config,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad/delivery').exists()
    assert json.loads((tmp_path/'bad/failure.json').read_bytes())['attempts'][0]['status']=='bend_preflight_failed'
