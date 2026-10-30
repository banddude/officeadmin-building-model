"""Fake adapters exercise actual public placement geometry; no external calls."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry,Resource
from oabm.model import ContractError,Provenance,Point3,Box3D,Pose,Size3,Obstacle,RouteConstraint
from oabm.proposals import (
    PlacementOptions,PlacementContext,ImageCrop,RankingRequest,RankedCandidate,RankingOutput,
    Endpoint,RoutingConfig,ProposalResult,BenchmarkCase,BenchmarkTrial,VisionBenchmark,
    AdapterAvailabilityError,dispatch_ranking,measure_luna_benchmark,
)
from test_proposed_electrical_design import _model

ROOT=Path(__file__).resolve().parents[1]


def config():
    return RoutingConfig(version='1.0.0',**{role:Endpoint(adapter_id=role,provider_id='fake:'+role,
        model_alias=alias) for role,alias in (
        ('text_glm','glm-5.3-flash'),('text_codex','codex'),('vision_luna','gpt-6-luna'),
        ('vision_sol','gpt-6-sol'),('diagnostic_astra','gpt-6-astra'))})


def options():
    return PlacementOptions(identity_key='synthetic-panel',equipment_type='panelboard',name=None,
        level_id='level:one',served_device_ids=('device:1','device:2','device:3'),
        mounting_height_m=1.5,working_depth_m=.9,working_width_m=.8)


def request(*,vision=False,model=None,policy=None,prompt='rank-v1',question='Rank supplied candidate references for the service area.'):
    model=_model(door=True) if model is None else model
    policy=config() if policy is None else policy
    crop=ImageCrop.capture(source_id=model.model_id,region_id='level:one',width=8,height=8,
        pixels=bytes((x*7)%256 for x in range(64))) if vision else None
    return RankingRequest.capture(model,options=options(),question=question,prompt_version=prompt,
        policy_fingerprint=policy.fingerprint,crop=crop)


def response(data,*,invalid=False,wrong=False):
    candidates=data['context']['candidates']
    if invalid:
        chosen=next(c for c in candidates if not c['candidate']['valid'])
    else:
        valid=[c for c in candidates if c['candidate']['valid']]
        chosen=valid[-1] if wrong else valid[0]
    ordered=[chosen]+[c for c in candidates if c!=chosen]
    return {'ranking':[dict(candidate_id=c['id'],confidence=.9,reasons=['bounded synthetic semantic ranking'],
        evidence_ids=[c['candidate']['space_id'],c['candidate']['wall_id']]) for c in ordered]}


def adapters(log=None,**overrides):
    result={}
    for role in ('text_glm','text_codex','vision_luna','vision_sol','diagnostic_astra'):
        def fake(data,*,provider_id,model_alias,prompt_version,_role=role):
            if log is not None:log.append((_role,provider_id,model_alias,prompt_version))
            return response(data)
        result[role]=fake
    result.update(overrides)
    return result


@pytest.fixture
def bound():return request()


def test_actual_producer_ranking_roundtrip_and_no_application(bound):
    model=_model(door=True);before=model.to_json();policy=config();log=[]
    assert any(not c.candidate.valid and any('opening:' in r for r in c.candidate.reasons) for c in bound.context.candidates)
    result=dispatch_ranking(bound,policy,adapters=adapters(log))
    assert result.role=='text_glm' and result.endpoint==policy.text_glm
    assert log==[('text_glm','fake:text_glm','glm-5.3-flash','rank-v1')]
    assert not result.accepted and not result.diagnostic
    assert result.provenance[0].derivation=='inferred'
    assert result.provenance[0].source_kind=='ai-proposed'
    assert result.provenance[0].scope_paths==('output',)
    decoded=ProposalResult.from_json(result.to_json())
    decoded.validate_against(model,options(),policy,current_request=bound)
    assert decoded.to_json()==result.to_json() and model.to_json()==before
    assert model.electrical_equipment==model.ports==()


def test_portable_handoff_distinguishes_source_user_and_proposal_evidence():
    from oabm.model import is_observed

    model=_model(door=True)
    observed=Provenance(source_kind='synthetic-plan',source_id='fixture:plan',
        derivation='observed',scope_paths=('centerline',))
    user=Provenance(source_kind='user',source_id='fixture:reviewer',
        derivation='user',scope_paths=('height_m',))
    wall=replace(model.walls[0],provenance=(observed,user))
    model=replace(model,walls=(wall,*model.walls[1:]))
    before=model.to_json(); req=request(model=model); policy=config()
    # A deterministic scorer uses the same adapter contract as a model.
    def scorer(data,**kwargs):return response(data)
    result=dispatch_ranking(req,policy,adapters=adapters(text_glm=scorer))
    decoded=ProposalResult.from_json(result.to_json())
    decoded.validate_against(model,options(),policy,current_request=req)
    source=next(item for item in decoded.request.context.sources if item.id==wall.id)
    assert [(p['derivation'],p['scope_paths']) for p in source.entity['provenance']]==[
        ('observed',['centerline']),('user',['height_m'])]
    assert decoded.provenance[0].source_kind=='ai-proposed'
    assert decoded.provenance[0].derivation=='inferred'
    assert not is_observed(decoded.provenance)
    assert not decoded.accepted and model.to_json()==before


@pytest.mark.parametrize('reason',['rate_limited','unavailable'])
def test_availability_only_text_fallback_with_mutation(bound,reason):
    before=bound.to_json();log=[]
    def unavailable(data,**kwargs):
        log.append('glm')
        data['context']['sources'][0]['entity']['attributes']['forged']=True
        data['context']['candidates'].clear()
        raise AdapterAvailabilityError(reason)
    def fallback(data,**kwargs):
        log.append('codex')
        assert data==bound.to_dict()
        return response(data)
    result=dispatch_ranking(bound,config(),adapters=adapters(text_glm=unavailable,text_codex=fallback))
    assert result.role=='text_codex' and result.availability_reason==reason
    assert log==['glm','codex'] and bound.to_json()==before


@pytest.mark.parametrize('kind',['invalid','clearance','unrelated','provider-spoof'])
def test_invalid_output_never_falls_back(bound,kind):
    log=[]
    def primary(data,**kwargs):
        log.append('primary')
        if kind=='unrelated':raise RuntimeError('synthetic unrelated failure')
        if kind=='invalid':return {'ranking':'bad'}
        output=response(data,invalid=kind=='clearance')
        if kind=='provider-spoof':output['model_alias']='codex'
        return output
    with pytest.raises((ContractError,RuntimeError)):
        dispatch_ranking(bound,config(),adapters=adapters(log,text_glm=primary))
    assert log==['primary']


@pytest.mark.parametrize('vision',[False,True])
def test_mutating_adapter_cannot_change_trusted_request(vision):
    req=request(vision=vision);before=req.to_json()
    def mutate(data,**kwargs):
        original=copy.deepcopy(data)
        data['context']['candidates'][0]['candidate']['valid']=True
        data['question']='forged'
        if data['crop']:data['crop']['gray8_base64']='forged'
        return response(original)
    role='vision_luna' if vision else 'text_glm'
    result=dispatch_ranking(req,config(),adapters=adapters(**{role:mutate}))
    assert result.request.to_json()==before and req.to_json()==before


@pytest.fixture
def failed_benchmark():
    req=request(vision=True)
    expected=response(req.to_dict())['ranking'][0]['candidate_id']
    cases=(BenchmarkCase(id='case:a',request=req,expected_candidate_id=expected),)
    benchmark=measure_luna_benchmark(cases,config(),adapters=adapters(
        vision_luna=lambda data,**kwargs:response(data,wrong=True)),required_success_count=1)
    assert benchmark.success_count==0
    return benchmark


def test_measured_vision_gate_and_explicit_diagnostic(failed_benchmark):
    req=request(vision=True);log=[]
    primary=dispatch_ranking(req,config(),adapters=adapters(log))
    assert primary.role=='vision_luna'
    sol=dispatch_ranking(req,config(),adapters=adapters(log),vision_tier='benchmark_secondary',benchmark=failed_benchmark)
    assert sol.role=='vision_sol' and sol.benchmark.success_count==0
    diagnostic=dispatch_ranking(req,config(),adapters=adapters(log),diagnostic=True)
    assert diagnostic.role=='diagnostic_astra' and diagnostic.diagnostic and diagnostic.diagnostic_requested
    assert [r[0] for r in log]==['vision_luna','vision_sol','diagnostic_astra']
    VisionBenchmark.from_json(failed_benchmark.to_json())
    ProposalResult.from_json(sol.to_json())


@pytest.mark.parametrize('kind',['missing','passing','stale-prompt','stale-config','availability','tampered-count'])
def test_vision_gate_refusals(failed_benchmark,kind):
    req=request(vision=True);bench=failed_benchmark;policy=config();log=[]
    if kind=='missing':bench=None
    elif kind=='passing':
        bench=measure_luna_benchmark(bench.cases,policy,adapters=adapters(),required_success_count=1)
    elif kind=='stale-prompt':req=request(vision=True,prompt='rank-v2')
    elif kind=='stale-config':policy=replace(policy,vision_luna=replace(policy.vision_luna,model_alias='luna-revision2'));req=request(vision=True,policy=policy)
    elif kind=='availability':
        def unavailable(data,**kwargs):raise AdapterAvailabilityError('unavailable')
        bench=measure_luna_benchmark(bench.cases,policy,adapters=adapters(vision_luna=unavailable),required_success_count=1)
    else:
        with pytest.raises(ContractError):replace(bench,success_count=1)
        return
    with pytest.raises(ContractError):dispatch_ranking(req,policy,adapters=adapters(log),vision_tier='benchmark_secondary',benchmark=bench)
    assert not log


@pytest.mark.parametrize('role',['vision_luna','vision_sol'])
def test_invalid_vision_never_automatically_calls_astra(failed_benchmark,role):
    log=[]
    def invalid(data,**kwargs):log.append(role);return {'ranking':[]}
    kwargs={} if role=='vision_luna' else dict(vision_tier='benchmark_secondary',benchmark=failed_benchmark)
    with pytest.raises(ContractError):dispatch_ranking(request(vision=True),config(),adapters=adapters(log,**{role:invalid}),**kwargs)
    assert log==[role]


@pytest.mark.parametrize('axis',['source','candidate-options','prompt','question','crop','config'])
def test_saved_result_rejects_current_input_change(axis):
    model=_model(door=True);req=request(vision=True,model=model);policy=config();opts=options()
    result=dispatch_ranking(req,policy,adapters=adapters())
    current=req
    if axis=='source':model=replace(model,name='Synthetic revised source');current=request(vision=True,model=model)
    elif axis=='candidate-options':
        opts=replace(opts,working_depth_m=1.0)
        current=RankingRequest.capture(model,options=opts,question=req.question,prompt_version=req.prompt_version,policy_fingerprint=policy.fingerprint,crop=req.crop)
    elif axis=='prompt':current=request(vision=True,prompt='rank-v2')
    elif axis=='question':current=request(vision=True,question='Different bounded semantic question')
    elif axis=='crop':
        crop=ImageCrop.capture(source_id=model.model_id,region_id='level:one',width=8,height=8,pixels=b'\0'*64)
        current=RankingRequest.capture(model,options=opts,question=req.question,prompt_version=req.prompt_version,policy_fingerprint=policy.fingerprint,crop=crop)
    else:
        policy=replace(policy,text_codex=replace(policy.text_codex,model_alias='codex-v2'))
        current=request(vision=True,policy=policy)
    with pytest.raises(ContractError):result.validate_against(model,opts,policy,current_request=current)


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(extra=1),lambda d:d.update(version='2.0.0'),lambda d:d.update(task='whole-job'),
    lambda d:d.update(question='x'*4097),lambda d:d.update(input_fingerprint='z'*64),
    lambda d:d.update(context='x'),lambda d:d['context']['options'].update(working_depth_m=True),
    lambda d:d['context']['options'].update(mounting_height_m=float('inf')),
    lambda d:d['context']['options'].update(served_device_ids='x'),
    lambda d:d['context']['options'].update(level_id='level:unknown'),
    lambda d:d['context']['candidates'][0]['candidate'].update(valid=1),
    lambda d:d['context']['candidates'][0]['candidate'].update(score=True),
    lambda d:d['context']['candidates'].append(copy.deepcopy(d['context']['candidates'][0])),
    lambda d:d['context']['sources'][0]['entity']['attributes'].update(forged=True),
])
def test_request_strict_portability(bound,mutate):
    data=bound.to_dict();mutate(data)
    with pytest.raises(ContractError):RankingRequest.from_dict(data)


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(extra=1),lambda d:d.update(ranking='x'),
    lambda d:d['ranking'][0].update(confidence=True),lambda d:d['ranking'][0].update(confidence=float('nan')),
    lambda d:d['ranking'][0].update(candidate_id='candidate:unknown'),
    lambda d:d['ranking'][0].update(evidence_ids=['wall:unknown']),
    lambda d:d['ranking'].append(copy.deepcopy(d['ranking'][0])),
    lambda d:d['ranking'][0].update(reasons='x'),lambda d:d['ranking'][0].update(reasons=['x'*257]),
])
def test_output_strict_portability(bound,mutate):
    data=response(bound.to_dict());mutate(data)
    with pytest.raises(ContractError):RankingOutput.from_dict(data).validate_for(bound)


def test_permutations_source_binding_and_mutation(bound):
    model=_model(door=True)
    reordered=replace(model,walls=tuple(reversed(model.walls)),spaces=tuple(reversed(model.spaces)),
        electrical_devices=tuple(reversed(model.electrical_devices)))
    assert request(model=reordered).to_json()==bound.to_json()
    data=bound.to_dict();data['context']['sources'][0]['entity']['attributes']['outside']=True
    assert bound.to_dict()!=data
    before=bound.to_json()
    model.walls[0].attributes['modified']=True
    assert bound.to_json()==before
    bound.context.sources[0].entity['attributes']['inside']=True
    with pytest.raises(ContractError):bound.to_dict()


@pytest.mark.parametrize('bad',[True,False,float('inf'),float('nan'),0,-1,'1'])
def test_numeric_options_reject_coercion(bad):
    with pytest.raises(ContractError):replace(options(),working_depth_m=bad)


@pytest.mark.parametrize('bad',[True,0,-1,'8'])
def test_crop_strict_dimensions(bad):
    with pytest.raises(ContractError):ImageCrop.capture(source_id='model:synthetic-design',region_id='level:one',width=bad,height=8,pixels=b'\0'*64)


def test_preflight_refuses_before_producer(monkeypatch):
    import oabm.proposals.contracts as contracts
    model=_model(door=True)
    model=replace(model,spaces=(*model.spaces,*(replace(model.spaces[0],id=f'space:{i}') for i in range(40))))
    def forbidden(*args,**kwargs):raise AssertionError('producer must not run')
    monkeypatch.setattr(contracts,'propose_equipment_placement',forbidden)
    with pytest.raises(ContractError,match='preflight'):PlacementContext.capture(model,options())


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(case_count=True),lambda d:d.update(success_count=True),
    lambda d:d.update(required_success_count=True),lambda d:d.update(required_success_count=0),
    lambda d:d.update(cases=[]),lambda d:d.update(trials=[]),
    lambda d:d['cases'].append(copy.deepcopy(d['cases'][0])),
    lambda d:d['trials'].append(copy.deepcopy(d['trials'][0])),
    lambda d:d.update(corpus_fingerprint='0'*64),
    lambda d:d['trials'][0].update(response={'ranking':[]}),
])
def test_benchmark_strict_recomputation(failed_benchmark,mutate):
    data=failed_benchmark.to_dict();mutate(data)
    with pytest.raises(ContractError):VisionBenchmark.from_dict(data)


def test_benchmark_outcome_count_not_caller_claim(failed_benchmark):
    data=failed_benchmark.to_dict()
    data['trials'][0]['response']=response(data['cases'][0]['request'])
    with pytest.raises(ContractError,match='counts'):VisionBenchmark.from_dict(data)


def test_schemas_and_unknown_json_keys(bound,failed_benchmark):
    docs=[json.loads((ROOT/'contracts'/name).read_text()) for name in (
        'oabm-model-v1.3.schema.json','panel-schedule-report-v1.schema.json','bounded-proposal-v1.schema.json')]
    registry=Registry().with_resources((d['$id'],Resource.from_contents(d)) for d in docs)
    validator=Draft202012Validator(docs[-1],registry=registry)
    result=dispatch_ranking(bound,config(),adapters=adapters())
    for value in (bound,config(),failed_benchmark,result):validator.validate(value.to_dict())
    with pytest.raises(ContractError):RoutingConfig.from_json('{"version":"1.0.0","version":"1.0.0"}')


def test_default_consumer_bytes_unchanged(tmp_path,bound):
    from dataclasses import asdict
    from oabm.ifc import to_ifc
    from oabm.exports.gltf import to_glb
    from oabm.quantities import extract_quantities
    from oabm.drawings import generate_schedules
    model=_model(door=True)
    def outputs(path):
        to_glb(model,path)
        return model.to_json(),to_ifc(model).to_string(),path.read_bytes(),extract_quantities(model).to_json(),json.dumps([asdict(s) for s in generate_schedules(model)],sort_keys=True)
    before=outputs(tmp_path/'before.glb')
    dispatch_ranking(bound,config(),adapters=adapters())
    assert outputs(tmp_path/'after.glb')==before


@pytest.mark.parametrize('kind',['too-many','duplicate','threshold-bool','threshold-zero','mixed-prompt','mixed-config','mutated-source'])
def test_benchmark_preflight_has_zero_adapter_calls(kind):
    req=request(vision=True);chosen=response(req.to_dict())['ranking'][0]['candidate_id']
    one=BenchmarkCase(id='case:a',request=req,expected_candidate_id=chosen)
    cases=(one,);threshold=1;log=[]
    if kind=='too-many':cases=tuple(replace(one,id=f'case:{i}') for i in range(17))
    elif kind=='duplicate':cases=(one,one)
    elif kind=='threshold-bool':threshold=True
    elif kind=='threshold-zero':threshold=0
    elif kind=='mixed-prompt':cases=(one,replace(one,id='case:b',request=request(vision=True,prompt='v2')))
    elif kind=='mixed-config':
        policy=replace(config(),text_glm=replace(config().text_glm,model_alias='glm-other'))
        cases=(replace(one,request=request(vision=True,policy=policy)),)
    else:req.context.sources[0].entity['attributes']['changed']=True
    with pytest.raises(ContractError):measure_luna_benchmark(cases,config(),adapters=adapters(log),required_success_count=threshold)
    assert not log


def test_benchmark_valid_threshold_change_invalidates_binding():
    req=request(vision=True);chosen=response(req.to_dict())['ranking'][0]['candidate_id']
    cases=tuple(BenchmarkCase(id=f'case:{i}',request=req,expected_candidate_id=chosen) for i in range(2))
    calls=[]
    def mixed(data,**kwargs):calls.append(1);return response(data,wrong=len(calls)==2)
    bench=measure_luna_benchmark(cases,config(),adapters=adapters(vision_luna=mixed),required_success_count=1)
    assert bench.success_count==1
    data=bench.to_dict();data['required_success_count']=2
    with pytest.raises(ContractError,match='binding'):VisionBenchmark.from_dict(data)


@pytest.mark.parametrize('benchmark',[False,True])
def test_altered_typed_availability_never_calls_secondary(bound,benchmark):
    log=[]
    def bad(data,**kwargs):
        exc=AdapterAvailabilityError('unavailable');exc.reason='invalid_output';raise exc
    if benchmark:
        req=request(vision=True);case=BenchmarkCase(id='case:a',request=req,
            expected_candidate_id=response(req.to_dict())['ranking'][0]['candidate_id'])
        action=lambda:measure_luna_benchmark((case,),config(),adapters=adapters(log,vision_luna=bad),required_success_count=1)
    else:action=lambda:dispatch_ranking(bound,config(),adapters=adapters(log,text_glm=bad))
    with pytest.raises(ContractError):action()
    assert not log


def test_direct_candidate_and_crop_guards():
    from oabm.proposals import CandidateReference
    from oabm.routing.placement import propose_equipment_placement
    candidate=propose_equipment_placement(_model(door=True),**options().kwargs()).selected
    with pytest.raises(ContractError):CandidateReference.capture(replace(candidate,position=None))
    with pytest.raises(ContractError,match='encoded length'):
        ImageCrop(source_id='model:test',region_id='level:one',width=1,height=1,
            gray8_base64='A'*100000,content_fingerprint='0'*64)


def _conflict_model():
    model=_model(door=True)
    geometry=Box3D(pose=Pose(position=Point3(x=1.5,y=.4,z=1.5)),size=Size3(x=.1,y=.1,z=.1))
    return replace(model,openings=(model.openings[0],replace(model.openings[0],id='opening:second')),
        obstacles=tuple(Obstacle(id=f'obstacle:{key}',geometry=geometry) for key in ('a','b')),
        route_constraints=tuple(RouteConstraint(id=f'constraint:{key}',constraint_type='keep-out',geometry=geometry) for key in ('a','b')))


def test_multiple_conflict_record_permutations():
    model=_conflict_model()
    baseline=request(model=model)
    permuted=replace(model,openings=tuple(reversed(model.openings)),obstacles=tuple(reversed(model.obstacles)),
        route_constraints=tuple(reversed(model.route_constraints)))
    assert request(model=permuted).to_json()==baseline.to_json()
    assert any(sum(r.startswith(('opening:','obstacle:','keep_out:')) for r in c.candidate.reasons)>=6
               for c in baseline.context.candidates)


@pytest.mark.parametrize('collection,path',[
    ('levels','elevation_m'),('walls','construction'),('walls','level_id'),
    ('obstacles','geometry'),('obstacles','clearance_m'),('obstacles','obstacle_type'),
    ('route_constraints','geometry'),('route_constraints','hard'),('route_constraints','constraint_type'),
    ('openings','host_id'),
])
def test_actual_consumed_scopes_bound_inferred_confidence(collection,path):
    model=_conflict_model()
    if collection=='walls':model=replace(model,walls=tuple(replace(w,construction='framed') for w in model.walls))
    entities=getattr(model,collection)
    record=Provenance(source_kind='synthetic',source_id='fixture:weak-input',derivation='inferred',
        confidence=.08,scope_paths=(path,))
    unrelated=Provenance(source_kind='synthetic',source_id='fixture:unrelated',confidence=.001,
        scope_paths=('attributes.unrelated',))
    entity=replace(entities[0],provenance=(record,unrelated),attributes={'unrelated':'retained original fact'})
    model=replace(model,**{collection:(entity,*entities[1:])})
    req=request(model=model)
    result=dispatch_ranking(req,config(),adapters=adapters())
    assert result.provenance[0].confidence==.08
    source=next(s for s in req.context.sources if s.id==entity.id)
    assert source.entity['provenance'][0]['scope_paths']==[path]
    assert source.entity['provenance'][1]['confidence']==.001
    assert source.entity['provenance'][1]['scope_paths']==['attributes.unrelated']


@pytest.mark.parametrize('version',['1.0.0','1.1.0','1.2.0','1.3.0'])
def test_all_model_versions_nullable_name_and_schema(version):
    model=replace(_model(door=True),schema_version=version,name=None)
    req=request(model=model)
    result=dispatch_ranking(req,config(),adapters=adapters())
    assert req.context.model_source_context.name is None
    assert RankingRequest.from_json(req.to_json()).to_json()==req.to_json()
    ProposalResult.from_json(result.to_json()).validate_against(model,options(),config(),current_request=req)
    docs=[json.loads((ROOT/'contracts'/name).read_text()) for name in (
        'oabm-model-v1.3.schema.json','panel-schedule-report-v1.schema.json','bounded-proposal-v1.schema.json')]
    registry=Registry().with_resources((d['$id'],Resource.from_contents(d)) for d in docs)
    for value in (req,result):Draft202012Validator(docs[-1],registry=registry).validate(value.to_dict())


def test_integer_float_equivalence_in_factories_and_readers():
    from oabm.proposals import CandidateReference
    from oabm.model import Vector3
    from oabm.model.model import _decode_dataclass
    from oabm.proposals.contracts import _portable
    opts=replace(options(),mounting_height_m=1,working_depth_m=1,working_width_m=1)
    floats=replace(options(),mounting_height_m=1.,working_depth_m=1.,working_width_m=1.)
    model=_model(door=True)
    req=RankingRequest.capture(model,options=opts,question='Rank known candidates.',prompt_version='v1',policy_fingerprint=config().fingerprint)
    other=RankingRequest.capture(model,options=floats,question=req.question,prompt_version='v1',policy_fingerprint=config().fingerprint)
    assert req.to_json()==other.to_json()
    result=dispatch_ranking(RankingRequest.from_json(req.to_json()),config(),adapters=adapters())
    assert ProposalResult.from_json(result.to_json()).to_json()==result.to_json()
    candidate=replace(req.context.candidates[0].candidate,position=Point3(x=1,y=1,z=1),
        direction=Vector3(x=1,y=0,z=0),score=1,confidence=1)
    reference=CandidateReference.capture(candidate)
    assert _decode_dataclass(CandidateReference,_portable(reference))==reference
    output=response(req.to_dict());output['ranking'][0]['confidence']=1
    normalized=RankingOutput.from_dict(output)
    assert isinstance(normalized.ranking[0].confidence,float)
    assert RankingOutput.from_json(normalized.to_json())==normalized


def test_large_polygon_preflight_before_producer(monkeypatch):
    import math
    import oabm.proposals.contracts as contracts
    from oabm.model import Polygon3D
    model=_model(door=True)
    polygon=Polygon3D(points=tuple(Point3(x=1+math.cos(i*2*math.pi/5000),y=2+math.sin(i*2*math.pi/5000),z=0) for i in range(5000)))
    model=replace(model,spaces=(replace(model.spaces[0],footprint=polygon),model.spaces[1]))
    def forbidden(*args,**kwargs):raise AssertionError('producer must not run')
    monkeypatch.setattr(contracts,'propose_equipment_placement',forbidden)
    with pytest.raises(ContractError,match='vertex budget'):PlacementContext.capture(model,options())


def test_candidate_reason_bound():
    from oabm.proposals import CandidateReference
    candidate=request().context.candidates[0].candidate
    with pytest.raises(ContractError,match='reasons exceed'):CandidateReference.capture(replace(candidate,reasons=('bounded',)*513))


def test_huge_integer_numeric_contracts(bound):
    huge=10**400
    from oabm.proposals import CandidateReference
    with pytest.raises(ContractError):replace(options(),working_depth_m=huge)
    with pytest.raises(ContractError):RankedCandidate(candidate_id=bound.context.candidates[0].id,
        confidence=huge,reasons=('bounded',),evidence_ids=())
    candidate=bound.context.candidates[0].candidate
    for key in ('score','confidence'):
        with pytest.raises(ContractError):CandidateReference.capture(replace(candidate,**{key:huge}))
    data=bound.to_dict();data['context']['options']['working_depth_m']=huge
    with pytest.raises(ContractError):RankingRequest.from_dict(data)
    data=response(bound.to_dict());data['ranking'][0]['confidence']=huge
    with pytest.raises(ContractError):RankingOutput.from_dict(data)


def test_huge_integer_output_no_fallback_and_failed_measured_trial(bound):
    huge=10**400;log=[]
    def invalid(data,**kwargs):
        output=response(data);output['ranking'][0]['confidence']=huge;return output
    with pytest.raises(ContractError):dispatch_ranking(bound,config(),adapters=adapters(log,text_glm=invalid))
    assert not log
    req=request(vision=True)
    case=BenchmarkCase(id='case:huge',request=req,expected_candidate_id=response(req.to_dict())['ranking'][0]['candidate_id'])
    benchmark=measure_luna_benchmark((case,),config(),adapters=adapters(vision_luna=invalid),required_success_count=1)
    assert benchmark.success_count==0
    assert benchmark.trials[0].response['ranking'][0]['confidence']==huge
    assert VisionBenchmark.from_json(benchmark.to_json()).success_count==0


def test_json_integer_limit_is_a_contract_rejection(bound):
    huge=10**5000
    data=response(bound.to_dict());data['ranking'][0]['confidence']=huge
    with pytest.raises(ContractError):RankingOutput.from_dict(data)
    with pytest.raises(ContractError):RankingOutput.from_json('{"ranking":'+('9'*5000)+'}')


def test_question_budget_precedes_producer(monkeypatch):
    import oabm.proposals.contracts as contracts
    def forbidden(*args,**kwargs):raise AssertionError('producer must not run')
    monkeypatch.setattr(contracts,'propose_equipment_placement',forbidden)
    with pytest.raises(ContractError,match='bounded ranking question'):
        RankingRequest.capture(_model(door=True),options=options(),question='x'*4097,
            prompt_version='v1',policy_fingerprint=config().fingerprint)
