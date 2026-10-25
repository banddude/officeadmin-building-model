"""Bounded choices over independently drawn synthetic sources; no live models."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from oabm.model import BuildingModel, ContractError
from oabm.qa import build_revision_diff
from oabm.pipeline.saved_electrical import persist_saved_electrical
from oabm.pipeline.saved_revisions import compare_saved_reviews
from oabm.proposals.contracts import digest
from oabm.proposals.symbols import AttemptJournal, SymbolResourceLimits
from oabm.proposals.dispatch import AdapterAvailabilityError
from oabm.proposals.circuit_design import (
    VERSION,PLACEMENT_KEYS,CONTEXT_ROLES,CircuitImage,CircuitContext,MissingInput,ApprovedCircuitProfile,
    ApprovedConnector,ReviewAuthority,CircuitReviewPolicy,CircuitRoutingPolicy,CircuitProposalRequest,
    CircuitCitation,DeviceCircuitChoice,CircuitProposalOutput,CircuitProposalRun,CircuitAdapterReply,
    CircuitReviewDecision,dispatch_circuit_proposal,compile_circuit_review,
)
from oabm.proposals.circuit_transport import circuit_output_schema,circuit_transport_prompt
from test_reviewed_circuit_design import case,save_inputs
from test_symbol_proposals import config


def fixture(root,*,height=1.2):
    model,review,args,design=case(root/'source',height=height)
    placement={key:args[key] for key in PLACEMENT_KEYS}
    approval=design.evidence[0]
    profile=ApprovedCircuitProfile(id='profile:explicit',rules=design.rules,routing_controls=design.routing_controls,
        rule_references=design.rule_references,approval=approval)
    connectors=tuple(ApprovedConnector(id='connector:'+p.proposal_id,proposal_id=p.proposal_id,
        offset_canonical_m=p.offset_canonical_m,direction=p.direction,reference=design.rule_references[0],approval=approval)
        for p in design.load_ports)
    authorization=b'{"authority":"agent:synthetic-review","purpose":"bounded circuit design review"}'
    sha=hashlib.sha256(authorization).hexdigest()
    authority=ReviewAuthority(id='agent:synthetic-review',kind='configured_agent',authorization_document_sha256=sha,
        configuration_fingerprint=digest({'review_configuration':'synthetic-independent-r1'}))
    review_policy=CircuitReviewPolicy.capture(authorities=(authority,),source_port_ids=(design.source_port_id,),connectors=connectors,profiles=(profile,))
    policy=CircuitRoutingPolicy(config=config(),expected_model_id='synthetic-strong-actual')
    role_map={'target':'plan','floor_context':'plan','legend':'legend','schedule':'fixture_schedule','notes':'notes'}
    images=tuple(CircuitImage.capture(im,role=role_map[im.role]) for im in args['runs'][0].request.images)
    contexts=tuple(CircuitContext(role=role,status='supplied' if any(im.role==role for im in images) or role=='project_rules' else 'inapplicable',
        image_ids=tuple(im.id for im in images if im.role==role),profile_ids=(profile.id,) if role=='project_rules' else (),
        rationale='Explicit complete synthetic source set for this bounded fixture.') for role in CONTEXT_ROLES)
    request=CircuitProposalRequest.capture(model,review,placement=placement,policy=policy,review_policy=review_policy,
        source_port_ids=(design.source_port_id,),connectors=connectors,profiles=(profile,),images=images,contexts=contexts)
    devices=tuple(DeviceCircuitChoice(proposal_id=p,status='grouped',connector_id='connector:'+p,
        rationale='Propose one caller-permitted circuit per drawn, admitted fixture.',confidence=.9,
        evidence=(CircuitCitation(image_id=next(im.id for im in images if im.role=='plan'),bounds=(.1,.1,.95,.95),
                                  quote='Synthetic floor marks and supplied full legend/schedule context.'),)) for p,_ in request.eligible)
    output=CircuitProposalOutput(version=VERSION,source_port_id=design.source_port_id,profile_id=profile.id,
        groups=tuple((d.proposal_id,) for d in devices),devices=devices,missing_inputs=request.required_missing_inputs)
    kwargs=dict(review_policy=review_policy,model=model,review=review,placement=placement,images=args['images'],documents=args['documents'],
        policy_documents={**args['evidence_documents'],sha:authorization})
    return request,policy,output,kwargs,args


@pytest.fixture(scope='module')
def source(tmp_path_factory):return fixture(tmp_path_factory.mktemp('circuit-proposal-source'))


def execute(root,source,*,output=None,policy=None,adapter=None,**updates):
    request,routing,raw,kwargs,_=source
    kwargs={**kwargs,**updates}
    def fake(document,*,images,provider_id,model_alias,prompt_version):
        assert document['request']['task']=='propose_preliminary_circuit_groups'
        assert document['transport']['prompt_sha256']==hashlib.sha256(document['transport']['prompt'].encode()).hexdigest()
        assert document['transport']['schema_fingerprint']==digest(document['transport']['output_schema'])
        assert set(images)=={im['id'] for im in document['request']['images']}
        return CircuitAdapterReply(output_json=(output or raw).to_json(),resolved_model_id='synthetic-strong-actual',provider_request_id='synthetic:circuit-call')
    root.mkdir(parents=True,exist_ok=True);journal_path=root/'attempts.jsonl'
    with AttemptJournal.create(journal_path,run_id='synthetic-circuit-execution') as journal:
        run=dispatch_circuit_proposal(request,policy or routing,**kwargs,adapters={'vision_sol':adapter or fake,'diagnostic_astra':adapter or fake},journal=journal)
    return run,journal_path


def decision(run,source,**changes):
    return CircuitReviewDecision.capture(run,source[3]['review_policy'],decision_id='review:synthetic-circuit',
        actor_id='agent:synthetic-review',actor_kind='configured_agent',action='approve',selection=changes.pop('selection',run.output),
        rationale='Caller-authorized configured agent approves bounded choices; no human confirmation asserted.',
        confidence=.9,inventory_group='designed-graph',**changes)


def compile_run(run,path,source,**changes):
    return compile_circuit_review(run,decision(run,source,**changes),**source[3],journal_path=path)


def save_compiled(root,source,compiled):
    _,_,_,kwargs,args=source
    args={**args,'evidence_documents':dict(compiled.evidence_documents),
          'source_references':{**args['source_references'],('reviewed-circuit-design',compiled.request.request_id):'evidence/approved-circuit-proposal'}}
    return save_inputs(root,(kwargs['model'],kwargs['review'],args,compiled.request))


def test_pending_ai_run_and_explicit_agent_review(source,tmp_path):
    request,_,output,kwargs,_=source;before=kwargs['model'].to_json()
    run,path=execute(tmp_path/'call',source)
    assert run.output==output and run.accepted is False and run.review_state=='pending'
    assert run.provenance.derivation=='inferred'
    assert AttemptJournal.read(path)==run.attempts
    assert kwargs['model'].to_json()==before
    compiled=compile_run(run,path,source)
    body=json.loads(compiled.decision_bytes)
    assert body['actor']['kind']=='configured_agent' and body['human_confirmation'] is False
    assert compiled.request.reviewer_id=='agent:synthetic-review'
    assert compiled.request.model_fingerprint==request.base_model_fingerprint
    assert compiled.request.groups==output.groups and compiled.missing_inputs==output.missing_inputs


def test_actual_saved_entrypoint_repeat_ifc_and_revision(source,tmp_path):
    run,path=execute(tmp_path/'call',source);compiled=compile_run(run,path,source)
    manifest=save_compiled(tmp_path/'input',source,compiled)
    cli=subprocess.run([sys.executable,'-m','oabm.pipeline.saved_electrical',str(manifest),
        '--output-root',str(tmp_path/'out'),'--run-id','a'],capture_output=True,text=True)
    assert cli.returncode==0,cli.stderr
    a=Path(cli.stdout.strip());b=persist_saved_electrical(manifest,output_root=tmp_path/'out',run_id='b')
    assert (a/'model.json').read_bytes()==(b/'model.json').read_bytes()
    assert (a/'quantities.json').read_bytes()==(b/'quantities.json').read_bytes()
    quantity=json.loads((a/'quantities.json').read_bytes())
    assert sum(r['quantity'] for r in quantity['items'] if r['category']=='route_length')==pytest.approx(30.)
    assert sum(r['quantity'] for r in quantity['items'] if r['category']=='conductor_length')==pytest.approx(120.)
    assert not compare_saved_reviews(a,b).diff.changes
    changed=fixture(tmp_path/'changed',height=2.2)
    moved_run,moved_path=execute(tmp_path/'changed-call',changed);moved=compile_run(moved_run,moved_path,changed)
    c=persist_saved_electrical(save_compiled(tmp_path/'changed-input',changed,moved),output_root=tmp_path/'out',run_id='changed')
    comparison=compare_saved_reviews(a,c)
    assert any(d.category=='route_length' and d.delta==pytest.approx(1.) for d in comparison.diff.quantity_deltas)
    am=BuildingModel.from_json((a/'model.json').read_text());cm=BuildingModel.from_json((c/'model.json').read_text())
    assert comparison.diff_json==build_revision_diff(am,cm).to_json()
    moved_quantity=json.loads((c/'quantities.json').read_bytes())
    assert sum(r['quantity'] for r in moved_quantity['items'] if r['category']=='conductor_length')==pytest.approx(124.)
    report=json.loads((a/'completion-report.json').read_bytes())
    assert not any(report['claims'].values()) and report['circuit_design']['buildable'] is False
    for name in ('electrical_devices','circuits','routes','conductors'):
        assert {e.id for e in getattr(am,name)}=={e.id for e in getattr(cm,name)}
    assert json.loads((a/'completion-report.json').read_bytes())['counts']['dispositions']=={'accept':2,'defer':1}
    from oabm.ifc.adapter import from_ifc
    restored=from_ifc(a/'model.ifc')
    assert {r.id:r.provenance for r in restored.routes}=={r.id:r.provenance for r in am.routes}


def test_transport_is_task_specific_and_strict(source):
    request=source[0];schema=circuit_output_schema(request);prompt=circuit_transport_prompt(request)
    from jsonschema import validate
    validate(source[2].to_dict(),schema)
    assert schema['additionalProperties'] is False
    assert 'SELECTION AND GROUPING' in prompt and 'Do not infer circuits' not in prompt
    assert all(im.id in prompt and im.sha256 in prompt for im in request.images)
    assert all(c.role in prompt for c in request.contexts)
    assert 'reviewer' not in schema['properties'] and 'accepted' not in schema['properties']


@pytest.mark.parametrize('fault',['omitted','duplicate_device','duplicate_group','unknown_group','wrong_connector','unknown_connector',
    'unknown_source','unknown_profile','missing_unknowns','citation','bounds','confidence','accepted','reviewer','identity','new_rules','no_groups','no_profile','oversized_group'])
def test_provider_cannot_escape_finite_choices_or_omit_devices(source,tmp_path,fault):
    data=source[2].to_dict()
    if fault=='omitted':data['devices'].pop()
    elif fault=='duplicate_device':data['devices'].append(data['devices'][0])
    elif fault=='duplicate_group':data['groups'].append(data['groups'][0])
    elif fault=='unknown_group':data['groups'][0]=['proposal:unknown']
    elif fault=='wrong_connector':data['devices'][0]['connector_id']=data['devices'][1]['connector_id']
    elif fault=='unknown_connector':data['devices'][0]['connector_id']='connector:unknown'
    elif fault=='unknown_source':data['source_port_id']='source:unknown'
    elif fault=='unknown_profile':data['profile_id']='profile:invented'
    elif fault=='missing_unknowns':data['missing_inputs']=[]
    elif fault=='citation':data['devices'][0]['evidence'][0]['image_id']='image:absent'
    elif fault=='bounds':data['devices'][0]['evidence'][0]['bounds']=[0,0,2,2]
    elif fault=='confidence':data['devices'][0]['confidence']=True
    elif fault=='accepted':data['accepted']=True
    elif fault=='reviewer':data['reviewer_id']='claimed-owner'
    elif fault=='identity':data['resolved_model_id']='claimed-identity'
    elif fault=='new_rules':data['rules']={'assumed_voltage_v':120}
    elif fault=='no_groups':data['groups']=[]
    elif fault=='no_profile':data['profile_id']=None
    else:data['groups']=[[d['proposal_id'] for d in data['devices']]]
    raw=json.dumps(data)
    run,path=execute(tmp_path,source,adapter=lambda *a,**kw:CircuitAdapterReply(output_json=raw,resolved_model_id='synthetic-strong-actual'))
    assert run.selected_attempt_id is None and run.attempts[-1].status=='invalid_output'
    assert run.attempts[-1].raw_output==raw and AttemptJournal.read(path)==run.attempts
    with pytest.raises(ContractError):run.output


@pytest.mark.parametrize('fault',['model','profile','connector','pixels','documents','authorization','policy_document'])
def test_stale_or_tampered_inputs_reject_before_adapter(source,tmp_path,fault):
    request,policy,output,kwargs,args=source;kwargs=dict(kwargs);calls=[]
    if fault=='model':kwargs['model']=replace(kwargs['model'],name='Changed canonical base')
    elif fault=='profile':request=replace(request,profiles=(replace(request.profiles[0],rules={**request.profiles[0].rules,'assumed_voltage_v':96.}),))
    elif fault=='connector':request=replace(request,connectors=(replace(request.connectors[0],offset_canonical_m=(0.,0.,.5)),*request.connectors[1:]))
    elif fault=='pixels':kwargs['images']={**kwargs['images'],request.images[0].id:b'not source pixels'}
    elif fault=='documents':kwargs['documents']={k:v+b'altered' for k,v in kwargs['documents'].items()}
    else:
        key=kwargs['review_policy'].authorities[0].authorization_document_sha256 if fault=='authorization' else request.profiles[0].approval.document_sha256
        kwargs['policy_documents']={**kwargs['policy_documents'],key:b'altered policy'}
    modified=(request,policy,output,kwargs,args)
    with pytest.raises(ContractError):execute(tmp_path,modified,adapter=lambda *a,**kw:calls.append(kw))
    assert not calls and (tmp_path/'attempts.jsonl').read_bytes()==b''


@pytest.mark.parametrize('field',['run_fingerprint','request_fingerprint','output_fingerprint','review_policy_fingerprint','actor_id','actor_kind','action'])
def test_review_is_an_independent_trusted_binding(source,tmp_path,field):
    run,path=execute(tmp_path/'call',source)
    changes={field:'0'*64 if field.endswith('fingerprint') else 'person:unlisted' if field=='actor_id' else 'human' if field=='actor_kind' else 'defer'}
    review=replace(decision(run,source),**changes)
    with pytest.raises(ContractError):compile_circuit_review(run,review,**source[3],journal_path=path)


def test_explicit_review_edits_are_bound_without_changing_canonical_inputs(source,tmp_path):
    run,path=execute(tmp_path/'call',source);output=run.output
    first=output.devices[0]
    edited=replace(output,groups=tuple(g for g in output.groups if first.proposal_id not in g),
        devices=(replace(first,status='excluded',connector_id=None,rationale='Explicit reviewed design exclusion.'),*output.devices[1:]))
    compiled=compile_run(run,path,source,selection=edited)
    body=json.loads(compiled.decision_bytes)
    assert body['review']['output_fingerprint']==output.fingerprint
    assert body['review']['selection']==edited.to_dict()
    assert json.loads(body['ai_run']['attempts'][-1]['raw_output'])==output.to_dict()
    final=persist_saved_electrical(save_compiled(tmp_path/'input',source,compiled),output_root=tmp_path/'out',run_id='edited')
    model=BuildingModel.from_json((final/'model.json').read_text())
    assert len(model.circuits)==1 and len(model.electrical_devices)==2


def test_no_silent_missing_context_or_unknown_erasure(source,tmp_path):
    request,policy,output,kwargs,args=source
    images=tuple(im for im in request.images if im.role!='legend')
    contexts=tuple(replace(c,status='missing',image_ids=(),rationale='No legend supplied in this explicit context revision.') if c.role=='legend' else c for c in request.contexts)
    request=replace(request,images=images,contexts=contexts)
    output=replace(output,missing_inputs=request.required_missing_inputs)
    kwargs={**kwargs,'images':{im.id:kwargs['images'][im.id] for im in images}}
    revised=(request,policy,output,kwargs,args)
    run,path=execute(tmp_path/'call',revised);compiled=compile_run(run,path,revised)
    assert any(m.field=='legend' for m in compiled.missing_inputs)
    assert any(m['field']=='legend' for m in compiled.request.evidence[0].provenance.attributes['missing_inputs'])
    erased=replace(run.output,missing_inputs=tuple(m for m in run.output.missing_inputs if m.field!='legend'))
    with pytest.raises(ContractError):compile_run(run,path,revised,selection=erased)


def test_absent_choices_produce_explicit_deferred_design(source,tmp_path):
    request,policy,output,kwargs,args=source
    rp=CircuitReviewPolicy.capture(authorities=kwargs['review_policy'].authorities,source_port_ids=(),connectors=(),profiles=())
    contexts=tuple(replace(c,status='missing',profile_ids=(),rationale='No approved physical profile supplied.') if c.role=='project_rules' else c for c in request.contexts)
    request=replace(request,source_port_ids=(),connectors=(),profiles=(),contexts=contexts,review_policy_fingerprint=rp.fingerprint)
    output=replace(output,source_port_id=None,profile_id=None,groups=(),
        devices=tuple(replace(d,status='deferred',connector_id=None,rationale='Required physical choices are missing.') for d in output.devices),
        missing_inputs=request.required_missing_inputs)
    revised=(request,policy,output,{**kwargs,'review_policy':rp},args)
    run,path=execute(tmp_path/'call',revised);compiled=compile_run(run,path,revised)
    final=persist_saved_electrical(save_compiled(tmp_path/'input',revised,compiled),output_root=tmp_path/'out',run_id='missing')
    model=BuildingModel.from_json((final/'model.json').read_text());report=json.loads((final/'completion-report.json').read_bytes())
    assert not model.circuits and model.ports==kwargs['model'].ports
    assert report['circuit_design']['status']=='deferred'
    assert {'source_port','approved_profile','connector'}<={m.field for m in compiled.missing_inputs}


@pytest.mark.parametrize('returned',['different-model',None])
def test_expected_transport_identity_cannot_be_supplied_by_model_json(source,tmp_path,returned):
    run,_=execute(tmp_path,source,adapter=lambda *a,**kw:CircuitAdapterReply(output_json=source[2].to_json(),resolved_model_id=returned))
    assert run.selected_attempt_id is None and run.attempts[-1].status=='identity_mismatch'


def test_unknown_transport_identity_stays_unknown_under_explicit_policy(source,tmp_path):
    request,policy,output,kwargs,args=source;policy=replace(policy,expected_model_id=None)
    request=replace(request,routing_policy_fingerprint=policy.fingerprint)
    revised=(request,policy,output,kwargs,args)
    run,path=execute(tmp_path,revised,adapter=lambda *a,**kw:CircuitAdapterReply(output_json=output.to_json()))
    assert run.provenance.attributes['transport_reported_model_id'] is None
    assert compile_run(run,path,revised).request.reviewer_id.startswith('agent:')


def test_no_automatic_model_substitution_or_inherited_qualification(source,tmp_path):
    calls=[]
    def unavailable(*args,**kwargs):calls.append(kwargs['model_alias']);raise AdapterAvailabilityError('rate_limited')
    run,_=execute(tmp_path,source,adapter=unavailable)
    assert calls==['vision_sol'] and run.attempts[-1].status=='rate_limited'
    with pytest.raises(ContractError):CircuitRoutingPolicy(config=source[1].config,mode='qualified_cheaper')


def test_diagnostic_is_explicit_and_cannot_compile(source,tmp_path):
    request,policy,output,kwargs,args=source;policy=replace(policy,mode='diagnostic')
    request=replace(request,routing_policy_fingerprint=policy.fingerprint)
    revised=(request,policy,output,kwargs,args);run,path=execute(tmp_path,revised)
    assert run.policy.role=='diagnostic_astra'
    with pytest.raises(ContractError):compile_run(run,path,revised)


@pytest.mark.parametrize('phase',['start','finish'])
def test_journal_durability_failure_cannot_return_proposal(source,tmp_path,monkeypatch,phase):
    request,policy,output,kwargs,_=source;calls=[]
    with AttemptJournal.create(tmp_path/'journal.jsonl') as journal:
        original=journal.append
        def append(row):
            if row.phase==phase:raise OSError('Synthetic journal durability failure')
            original(row)
        monkeypatch.setattr(journal,'append',append)
        def adapter(*a,**kw):calls.append(kw);return CircuitAdapterReply(output_json=output.to_json(),resolved_model_id='synthetic-strong-actual')
        with pytest.raises(OSError):dispatch_circuit_proposal(request,policy,**kwargs,adapters={'vision_sol':adapter},journal=journal)
    assert len(calls)==(0 if phase=='start' else 1)


def test_tampered_execution_journal_and_self_acceptance_refused(source,tmp_path):
    run,path=execute(tmp_path,source)
    with pytest.raises(ContractError):replace(run,accepted=True)
    path.write_bytes(path.read_bytes().replace(b'synthetic:circuit-call',b'altered:circuit-call'))
    with pytest.raises(ContractError):compile_run(run,path,source)


def test_malformed_and_incomplete_profiles_are_not_approved_candidates(source):
    profile=source[0].profiles[0]
    for bad in ({k:v for k,v in profile.rules.items() if k!='assumed_voltage_v'},
                {**profile.rules,'assumed_poles':True}):
        with pytest.raises(ContractError):replace(profile,rules=bad)
    with pytest.raises(ContractError):replace(source[0].connectors[0],direction=(0.,0.,0.))


def test_context_budget_failure_never_discards_attachments(source):
    with pytest.raises(ContractError):replace(source[0],resource_limits=SymbolResourceLimits(max_total_pixels=1))


def test_wrong_source_page_is_not_accepted_as_plan_context(source,tmp_path):
    request,policy,output,kwargs,args=source
    plan=next(im for im in request.images if im.role=='plan');legend=next(im for im in request.images if im.role=='legend')
    replacement=replace(legend,id=plan.id,role='plan')
    request=replace(request,images=tuple(replacement if im.id==plan.id else im for im in request.images))
    revised=(request,policy,output,{**kwargs,'images':{**kwargs['images'],plan.id:kwargs['images'][legend.id]}},args)
    calls=[]
    with pytest.raises(ContractError,match='source page/mark'):execute(tmp_path,revised,adapter=lambda *a,**kw:calls.append(kw))
    assert not calls


@pytest.mark.parametrize('citation_kind',['wrong_region','context_only'])
def test_grouped_citation_must_cover_its_source_mark(source,tmp_path,citation_kind):
    data=source[2].to_dict()
    citation=data['devices'][0]['evidence'][0]
    if citation_kind=='wrong_region':citation['bounds']=[0,0,.01,.01]
    else:citation['image_id']=next(im.id for im in source[0].images if im.role=='legend')
    run,_=execute(tmp_path,source,adapter=lambda *a,**kw:CircuitAdapterReply(output_json=json.dumps(data),resolved_model_id='synthetic-strong-actual'))
    assert run.selected_attempt_id is None


def test_caller_declared_human_review_is_distinct_from_agent_review(source,tmp_path):
    request,policy,output,kwargs,args=source
    payload=b'{"authority":"person:synthetic-owner","scope":"explicit circuit choices"}'
    sha=hashlib.sha256(payload).hexdigest()
    authority=ReviewAuthority(id='person:synthetic-owner',kind='human',authorization_document_sha256=sha)
    rp=CircuitReviewPolicy.capture(authorities=(authority,),source_port_ids=request.source_port_ids,connectors=request.connectors,profiles=request.profiles)
    request=replace(request,review_policy_fingerprint=rp.fingerprint)
    revised=(request,policy,output,{**kwargs,'review_policy':rp,'policy_documents':{**kwargs['policy_documents'],sha:payload}},args)
    run,path=execute(tmp_path,revised)
    trusted=replace(decision(run,revised),actor_id=authority.id,actor_kind='human')
    compiled=compile_circuit_review(run,trusted,**revised[3],journal_path=path)
    body=json.loads(compiled.decision_bytes)
    assert body['actor']['kind']=='human' and body['human_confirmation'] is True
    assert body['actor']['configuration_fingerprint'] is None


@pytest.mark.parametrize('raw',['{"version":"1.0.0","version":"1.0.0"}','{"confidence":NaN}','not JSON'])
def test_raw_json_is_never_repaired_or_coerced(source,tmp_path,raw):
    run,_=execute(tmp_path,source,adapter=lambda *a,**kw:CircuitAdapterReply(output_json=raw,resolved_model_id='synthetic-strong-actual'))
    assert run.selected_attempt_id is None and run.attempts[-1].raw_output==raw


def test_all_context_roles_and_input_owner_snapshots_are_explicit(source):
    request=source[0]
    with pytest.raises(ContractError):replace(request,contexts=request.contexts[:-1])
    for snapshot in request.sources:
        assert snapshot.content_fingerprint and snapshot.entity['id']==snapshot.id
    with pytest.raises(ContractError):replace(request,proposal_sources=request.proposal_sources[:-1])


@pytest.mark.parametrize('first',['completed','rate_limited'])
def test_journal_reuse_rejected_before_transport_preserves_prior_run(source,tmp_path,first):
    request,policy,output,kwargs,_=source;calls=[];path=tmp_path/'one-attempt.jsonl'
    def adapter(*a,**kw):
        calls.append(kw)
        if first=='rate_limited':raise AdapterAvailabilityError('rate_limited')
        return CircuitAdapterReply(output_json=output.to_json(),resolved_model_id='synthetic-strong-actual')
    with AttemptJournal.create(path) as journal:
        run=dispatch_circuit_proposal(request,policy,**kwargs,adapters={'vision_sol':adapter},journal=journal)
        before=path.read_bytes()
        with pytest.raises(ContractError,match='new empty'):
            dispatch_circuit_proposal(request,policy,**kwargs,adapters={'vision_sol':adapter},journal=journal)
        assert len(calls)==1 and path.read_bytes()==before and AttemptJournal.read(path)==run.attempts
        if first=='completed':assert compile_run(run,path,source).request.groups==output.groups


def test_durable_nonempty_journal_is_checked_even_if_memory_receipts_are_missing(source,tmp_path):
    request,policy,output,kwargs,_=source;path=tmp_path/'durable.jsonl';calls=[]
    with AttemptJournal.create(path) as journal:
        from oabm.proposals.symbols import AttemptReceipt
        from datetime import datetime,timezone
        journal.append(AttemptReceipt(attempt_id='synthetic:prior-attempt',phase='start',status='started',role=policy.role,
            endpoint=policy.endpoint,request_fingerprint=request.fingerprint,policy_fingerprint=policy.fingerprint,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),reason='Synthetic preexisting execution.'))
        journal._receipts.clear()  # Simulate detached in-memory state; durable bytes are authoritative.
        before=path.read_bytes()
        with pytest.raises(ContractError,match='new empty'):
            dispatch_circuit_proposal(request,policy,**kwargs,adapters={'vision_sol':lambda *a,**kw:calls.append(kw)},journal=journal)
        assert not calls and path.read_bytes()==before


def test_same_journal_concurrent_dispatch_reserves_only_one_execution(source,tmp_path,monkeypatch):
    import concurrent.futures,threading
    request,policy,output,kwargs,_=source;barrier=threading.Barrier(2);calls=[]
    original=CircuitProposalRequest.validate_inputs
    def validate(self,**values):
        result=original(self,**values);barrier.wait(timeout=30);return result
    monkeypatch.setattr(CircuitProposalRequest,'validate_inputs',validate)
    def adapter(*a,**kw):calls.append(kw);return CircuitAdapterReply(output_json=output.to_json(),resolved_model_id='synthetic-strong-actual')
    with AttemptJournal.create(tmp_path/'concurrent.jsonl') as journal:
        def invoke():
            try:return dispatch_circuit_proposal(request,policy,**kwargs,adapters={'vision_sol':adapter},journal=journal)
            except ContractError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:invoke(),range(2)))
        assert sum(r is not None for r in results)==1 and len(calls)==1
        assert len(AttemptJournal.read(journal.path))==2
