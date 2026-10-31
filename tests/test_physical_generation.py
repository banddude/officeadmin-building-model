from dataclasses import replace
import json
import pytest
from oabm.model import *
from oabm.model.physical_route_context import validate_physical_request_source
from oabm.routing.physical_generate import generate_physical_route
from oabm.routing.physical_verify import verify_physical_route
from physical_route_fixture import make_physical_source, make_physical_route, supplied


def request(*, work=30000, candidates=4):
    source=make_physical_source();template=make_physical_route(source)
    context=replace(template.context,execution_limits=replace(template.context.execution_limits,
        total_refinement_cells=work,candidate_routes=candidates))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,template.requirements))
    return source,context,template.requirements


def rebind(source,context,requirements):
    context=replace(context,source_model_sha256=compute_physical_source_sha256(source))
    return replace(context,request_sha256=compute_physical_request_sha256(context,requirements))


def test_generates_canonical_straight_and_keeps_inputs():
    source,context,requirements=request(candidates=1)
    before=repr((source,context,requirements))
    result=generate_physical_route(source,context,requirements)
    assert result.status=='verified_route'
    assert len(result.route.segments)==1
    assert result.route.requirements==requirements
    assert all(p.derivation=='inferred' for p in result.route.provenance)
    assert repr((source,context,requirements))==before
    assert not source.routes
    assert verify_physical_route(source,result.route).status=='verified_route'
    restored=BuildingModel.from_json(replace(source,routes=(result.route,)).to_json()).routes[0]
    assert restored==result.route
    report=result.to_dict()
    assert report['optimality_claim']=='none'
    assert report['search_complete'] is False
    assert report['remaining_count']=='unknown'
    assert report['verification']['scope']['geometry_unspecified_source_ids']==['device:a','device:b']
    assert report['work']['charged']<=report['work']['limit']
    assert generate_physical_route(source,context,requirements).to_json()==result.to_json()


def test_elbow_exact_canonical_joints():
    source,context,requirements=request(candidates=1)
    position=Point3(x=1,y=1,z=0)
    source=replace(source,ports=(source.ports[0],replace(source.ports[1],pose=Pose(position=position),direction=Vector3(x=0,y=-1,z=0))),
        electrical_devices=(source.electrical_devices[0],replace(source.electrical_devices[1],pose=Pose(position=position))))
    context=rebind(source,context,requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='verified_route'
    assert [(j.position.x,j.position.y,j.position.z) for j in result.route.joints]==[(0,0,0),(.7,0,0),(1,.3,0),(1,1,0)]
    assert result.route.segments[1].plane_normal==Vector3(x=0,y=0,z=1)
    assert result.route.segments[1].turn=='positive_quarter'
    assert result.to_dict()['objective']=={'unit':'nanometres','rational_term':'1400000000','pi_coefficient':'150000000'}


def test_obstacle_rejects_direct_but_does_not_stop_search():
    source,context,requirements=request()
    obstacle=Obstacle(id='obstacle:middle',geometry=Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=.2,y=.2,z=.2)))
    source=replace(source,obstacles=(obstacle,))
    coverage=PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision='direct_blocker',blocker_entity_ids=(obstacle.id,),rationale='Synthetic supplied hard box.',provenance=supplied('source_entity_id','decision','blocker_entity_ids','rationale'))
    context=rebind(source,replace(context,coverage=(coverage,)),requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='verified_route'
    assert result.to_dict()['candidates'][0]['status']=='rejected_route'
    assert len(result.route.segments)==9
    assert any(abs(j.position.y)>=.7 or abs(j.position.z)>=.7 for j in result.route.joints)
    assert verify_physical_route(source,result.route).status=='verified_route'


@pytest.mark.parametrize('which',range(3))
def test_wrong_top_level_types(which):
    args=list(request());args[which]={}
    result=generate_physical_route(*args)
    assert result.status=='invalid_input';assert result.route is None


def test_resource_stop_is_not_no_route():
    result=generate_physical_route(*request(work=1))
    assert result.status=='resource_limit';assert result.route is None
    assert result.to_dict()['search_complete'] is False


@pytest.mark.parametrize('target',['source','requirements','domain','coverage'])
def test_stale_inputs_never_create_route(target):
    source,context,requirements=request()
    if target=='source':source=replace(source,name='Synthetic revision')
    if target=='requirements':requirements=replace(requirements,clearance_m=.02)
    if target=='domain':context=replace(context,allowed_domain=replace(context.allowed_domain,name='Changed domain'))
    if target=='coverage':context=replace(context,coverage=(PhysicalGeometryCoverage(source_entity_id='missing:owner',decision='outside_proof_scope',blocker_entity_ids=(),rationale='Synthetic invalid stale coverage.',provenance=supplied('source_entity_id','decision','blocker_entity_ids','rationale')),))
    result=generate_physical_route(source,context,requirements)
    assert result.status=='invalid_input';assert result.route is None


def test_shared_request_helper_has_typed_failures():
    source,context,requirements=request()
    assert validate_physical_request_source(source,context,requirements)=='canonical_python_snapshot'
    for args in ((None,context,requirements),(source,None,requirements),(source,context,None)):
        with pytest.raises(ContractError):validate_physical_request_source(*args)
    with pytest.raises(ContractError):validate_physical_request_source(source,context,requirements,source_json=42)


@pytest.mark.parametrize('identity',['model:physical-fixture','device:a','port:a','domain:physical','requirements:physical'])
def test_reserved_output_identity_collisions(identity):
    source,context,requirements=request()
    requirements=replace(requirements,route_id=identity)
    context=rebind(source,context,requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='invalid_input';assert result.reason=='request_entity_id_collision';assert result.route is None


def test_generated_id_collision_uses_bounded_probe():
    source,context,requirements=request(candidates=1)
    first=stable_id('physical-route-joint',f'{requirements.route_id}:joint-slot:0:collision-probe:0')
    source=replace(source,electrical_devices=(*source.electrical_devices,ElectricalDevice(id=first,device_type='geometry-unspecified',pose=Pose(position=Point3(x=0,y=0,z=0)))))
    context=rebind(source,context,requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='verified_route'
    assert result.route.joints[0].id!=first
    assert result.route.joints[0].id==stable_id('physical-route-joint',f'{requirements.route_id}:joint-slot:0:collision-probe:1')


def test_raw_endpoint_lexeme_not_rounded_into_admission():
    source,context,requirements=request()
    document=json.loads(source.to_json())
    document['ports'][0]['pose']['position']['x']=0.1
    raw=json.dumps(document).replace('0.1,','0.10000000000000001,',1)
    result=generate_physical_route(source,context,requirements,source_json=raw)
    assert result.status in ('invalid_input','unsupported_input');assert result.route is None


def test_first_verified_route_survives_exact_budget_exhaustion():
    source,context,requirements=request(candidates=1)
    baseline=generate_physical_route(source,context,requirements)
    exact=baseline.to_dict()['verification']['session_work']['charged_cumulative']
    context=replace(context,execution_limits=replace(context.execution_limits,total_refinement_cells=exact))
    context=rebind(source,context,requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='verified_route';assert result.route is not None
    assert result.to_dict()['work']['charged']==exact
    assert result.to_dict()['search_stop_status']=='resource_limit'


def test_candidate_capture_cap_stops_before_success(monkeypatch):
    import oabm.routing.physical_generation_adapter as adapter
    monkeypatch.setattr(adapter,'CANDIDATE_BYTES',1000)
    result=generate_physical_route(*request(candidates=1))
    assert result.status=='resource_limit';assert result.route is None
    assert result.reason=='capture_byte_limit'


def test_identity_probe_exhaustion_is_resource_not_numeric_failure():
    source,context,requirements=request(candidates=1)
    occupied=tuple(ElectricalDevice(id=stable_id('physical-route-joint',f'{requirements.route_id}:joint-slot:0:collision-probe:{i}'),device_type='geometry-unspecified',pose=Pose(position=Point3(x=0,y=0,z=0))) for i in range(32))
    source=replace(source,electrical_devices=(*source.electrical_devices,*occupied))
    context=rebind(source,context,requirements)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='resource_limit';assert result.route is None
    assert result.reason=='generated_identity_probe_limit'


def test_mutated_huge_integer_is_typed_admission_failure():
    source,context,requirements=request()
    object.__setattr__(source.ports[0].pose.position,'x',1<<2000)
    result=generate_physical_route(source,context,requirements)
    assert result.status=='invalid_input';assert result.route is None


def test_request_helper_normalizes_nested_numeric_overflow():
    source,context,requirements=request()
    object.__setattr__(context.allowed_domain.volumes[0].pose.position,'x',1<<2000)
    with pytest.raises(ContractError):validate_physical_request_source(source,context,requirements)
