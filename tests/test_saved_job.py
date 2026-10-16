"""Synthetic recovery checks for the private local job runner."""
import hashlib
import json
from pathlib import Path
import pytest
from oabm.pipeline import saved_job as job
from oabm.proposals.circuit_design import CircuitAdapterReply, CircuitProposalRun
from test_circuit_design_proposals import fixture, decision
from test_reviewed_circuit_design import save_inputs

class Crash(BaseException):
    pass

def setup(root):
    source=fixture(root/'source')
    request,policy,output,kwargs,args=source
    # The existing test writer needs a design object before we remove its ref.
    from test_reviewed_circuit_design import case
    design=case(root/'writer-source')[3]
    upstream=save_inputs(root/'saved',(kwargs['model'],kwargs['review'],args,design))
    doc=json.loads(upstream.read_text());doc['circuit_design']=None
    upstream.write_text(json.dumps(doc))
    def put(name,data):
        p=root/name;p.write_bytes(data)
        return {'path':str(p),'sha256':hashlib.sha256(data).hexdigest()}
    settings=job.CircuitJobSettings(policy=policy,review_policy=kwargs['review_policy'],
        source_port_ids=request.source_port_ids,connectors=request.connectors,
        profiles=request.profiles,images=request.images,contexts=request.contexts)
    manifest={'version':job.VERSION,'job_id':'synthetic-job','parent_revision':None,
        'upstream':put('upstream.json',upstream.read_bytes()),
        'settings':put('settings.json',settings.to_json().encode()),'review':None,
        'retry_policy':{'allow_dispatch':True,'proposal_after':None,'proposal_reason':None,
                        'export_after':None,'export_reason':None}}
    # Preserve relative source refs by using the real upstream manifest.
    manifest['upstream']={'path':str(upstream),'sha256':hashlib.sha256(upstream.read_bytes()).hexdigest()}
    for key,values in [('circuit_images',kwargs['images']),('circuit_documents',kwargs['documents']),
                       ('policy_documents',kwargs['policy_documents'])]:
        manifest[key]={k:put(key+str(i),v) for i,(k,v) in enumerate(values.items())}
    path=root/'job.json';path.write_text(json.dumps(manifest))
    calls=[]
    def adapter(*a,**kw):
        calls.append(kw)
        return CircuitAdapterReply(output_json=output.to_json(),resolved_model_id='synthetic-strong-actual')
    return path,source,calls,adapter

def run(path,adapter):
    return job.run_saved_electrical_job(path,job_root=path.parent/'jobs',
        adapters={'vision_sol':adapter}).report

def test_completed_provider_is_reused_without_review(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    a=run(path,adapter);b=run(path,adapter)
    assert a['status']==b['status']=='waiting_for_review'
    assert len(calls)==1 and b['proposal_reused']
    assert a['circuit_run']==b['circuit_run']
    assert not a['code_compliance_verified'] and not a['buildable']

def test_durable_finish_recovers_missing_parsed_checkpoint(tmp_path,monkeypatch):
    path,source,calls,adapter=setup(tmp_path)
    original=job._persist_run
    monkeypatch.setattr(job,'_persist_run',lambda *a:(_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):run(path,adapter)
    monkeypatch.setattr(job,'_persist_run',original)
    assert run(path,adapter)['status']=='waiting_for_review'
    assert len(calls)==1

def test_uncertain_provider_start_blocks_retry(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    def crash(*a,**kw):
        calls.append(kw);raise Crash()
    with pytest.raises(Crash):run(path,crash)
    result=run(path,adapter)
    assert result['status']=='blocked_in_doubt' and len(calls)==1
    doc=json.loads(path.read_text());doc['parent_revision']=hashlib.sha256(path.read_bytes()).hexdigest()
    doc['retry_policy'].update(proposal_after=result['proposal_attempt_id'],proposal_reason='Explicit request')
    path.write_text(json.dumps(doc))
    assert run(path,adapter)['status']=='blocked_in_doubt' and len(calls)==1

def test_reviewed_delivery_reuses_provider_and_export(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    first=run(path,adapter)
    proposed=CircuitProposalRun.from_json(Path(first['circuit_run']['path']).read_text())
    review=decision(proposed,source).to_json().encode()
    p=tmp_path/'approved.json';p.write_bytes(review)
    doc=json.loads(path.read_text());doc['parent_revision']=hashlib.sha256(path.read_bytes()).hexdigest()
    doc['review']={'path':str(p),'sha256':hashlib.sha256(review).hexdigest()}
    path.write_text(json.dumps(doc))
    a=run(path,adapter);b=run(path,adapter)
    assert a['status']==b['status']=='delivery_persisted'
    assert len(calls)==1 and b['export_reused'] and b['proposal_reused']
    assert a['delivery']==b['delivery']

def test_active_job_lock_prevents_second_provider_call(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    path,source,calls,adapter=setup(tmp_path)
    entered=threading.Event();release=threading.Event()
    def blocking(*a,**kw):
        entered.set()
        assert release.wait(30)
        return adapter(*a,**kw)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,path,blocking)
        assert entered.wait(30)
        try:
            with pytest.raises(job.SavedJobError,match='another active caller'):
                run(path,adapter)
            assert not calls
        finally:release.set()
        assert future.result(timeout=30)['status']=='waiting_for_review'
    assert len(calls)==1

def test_existing_export_completion_marker_is_verified(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    first=run(path,adapter)
    proposed=CircuitProposalRun.from_json(Path(first['circuit_run']['path']).read_text())
    review=decision(proposed,source).to_json().encode()
    p=tmp_path/'approved.json';p.write_bytes(review)
    doc=json.loads(path.read_text());doc['parent_revision']=hashlib.sha256(path.read_bytes()).hexdigest()
    doc['review']={'path':str(p),'sha256':hashlib.sha256(review).hexdigest()}
    path.write_text(json.dumps(doc))
    delivered=run(path,adapter)
    wrapper=Path(delivered['delivery']).parents[2]/'COMPLETED.json'
    assert wrapper.is_file()
    wrapper.write_text('{}')
    with pytest.raises(job.SavedJobError,match='export completion binding mismatch'):
        run(path,adapter)
    assert len(calls)==1

def revise(path,mutate):
    raw=path.read_bytes();doc=json.loads(raw)
    doc['parent_revision']=hashlib.sha256(raw).hexdigest()
    mutate(doc);path.write_text(json.dumps(doc))
    return doc

def approve(path,source,report):
    proposed=CircuitProposalRun.from_json(Path(report['circuit_run']['path']).read_text())
    payload=decision(proposed,source).to_json().encode()
    p=path.parent/'approved.json';p.write_bytes(payload)
    revise(path,lambda doc:doc.update(review={'path':str(p),'sha256':hashlib.sha256(payload).hexdigest()}))

def tree_bytes(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}

def test_failed_provider_requires_exact_retry_and_keeps_old_attempt(tmp_path):
    from oabm.proposals.dispatch import AdapterAvailabilityError
    path,source,calls,adapter=setup(tmp_path)
    def limited(*a,**kw):
        calls.append(kw);raise AdapterAvailabilityError('rate_limited')
    first=run(path,limited)
    assert first['status']=='blocked_provider_failure' and first['provider_status']=='rate_limited'
    assert run(path,adapter)['status']=='blocked_provider_failure' and len(calls)==1
    root=tmp_path/'jobs/synthetic-job'
    old=next((root/'proposals').glob('*/'+first['proposal_attempt_id']))
    frozen=tree_bytes(old)
    revise(path,lambda d:d['retry_policy'].update(proposal_after='a-wrong',proposal_reason='Wrong prior attempt'))
    assert run(path,adapter)['status']=='blocked_provider_failure' and len(calls)==1
    revise(path,lambda d:d['retry_policy'].update(proposal_after=first['proposal_attempt_id'],proposal_reason='Caller approves one retry after known rate limit'))
    second=run(path,adapter)
    assert second['status']=='waiting_for_review' and len(calls)==2
    assert second['proposal_attempt_id']!=first['proposal_attempt_id']
    assert run(path,adapter)['proposal_reused'] and len(calls)==2
    assert tree_bytes(old)==frozen

def test_export_only_revision_reuses_reviewed_provider_input(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    pending=run(path,adapter);approve(path,source,pending)
    first=run(path,adapter)
    assert first['status']=='delivery_persisted'
    old_model=(Path(first['delivery'])/'model.json').read_bytes()
    upstream=Path(json.loads(path.read_text())['upstream']['path'])
    document=json.loads(upstream.read_text());document['include_ifc']=not document['include_ifc']
    upstream.write_text(json.dumps(document))
    revise(path,lambda d:d['upstream'].update(sha256=hashlib.sha256(upstream.read_bytes()).hexdigest()))
    second=run(path,adapter)
    assert second['status']=='delivery_persisted' and len(calls)==1
    assert second['proposal_reused'] and second['proposal_attempt_id']==first['proposal_attempt_id']
    assert second['export_attempt_id']!=first['export_attempt_id'] and not second['export_reused']
    assert (Path(second['delivery'])/'model.json').read_bytes()==old_model
    assert (Path(second['delivery'])/'model.ifc').exists()==document['include_ifc']

def test_missing_review_pauses_before_any_export_and_resumes(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    pending=run(path,adapter)
    root=tmp_path/'jobs/synthetic-job'
    assert pending['status']=='waiting_for_review' and not list((root/'exports').iterdir())
    assert run(path,adapter)['status']=='waiting_for_review' and len(calls)==1
    approve(path,source,pending)
    result=run(path,adapter)
    assert result['status']=='delivery_persisted' and len(calls)==1
    assert result['proposal_reused'] and not result['code_compliance_verified']

@pytest.mark.parametrize('corruption',['truncated','altered_terminal','extra_receipt'])
def test_receipt_corruption_never_replays_provider(tmp_path,corruption):
    path,source,calls,adapter=setup(tmp_path)
    pending=run(path,adapter);p=Path(pending['circuit_journal']['path'])
    lines=p.read_bytes().splitlines(keepends=True)
    if corruption=='truncated':p.write_bytes(b'{invalid')
    elif corruption=='extra_receipt':p.write_bytes(b''.join(lines)+lines[-1])
    else:
        rows=[json.loads(line) for line in lines]
        rows[-1]['provider_request_id']='forged-request'
        p.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with pytest.raises(job.SavedJobError):run(path,adapter)
    assert len(calls)==1

def test_late_export_failure_requires_fresh_attempt_without_provider_replay(tmp_path,monkeypatch):
    path,source,calls,adapter=setup(tmp_path)
    pending=run(path,adapter);approve(path,source,pending)
    original=job.saved._write
    def fail(p,data):
        if p.name=='scope-ledger.json':raise OSError('Synthetic full storage')
        return original(p,data)
    with monkeypatch.context() as patch:
        patch.setattr(job.saved,'_write',fail)
        failed=run(path,adapter)
    assert failed['status']=='blocked_export_failure' and len(calls)==1
    root=tmp_path/'jobs/synthetic-job'
    old=next((root/'exports').glob('*/'+failed['export_attempt_id']))
    frozen=tree_bytes(old)
    assert list(old.rglob('model.json')) and not list(old.rglob('delivery/COMPLETED.json'))
    assert run(path,adapter)['status']=='blocked_export_failure' and len(calls)==1
    revise(path,lambda d:d['retry_policy'].update(export_after='a-wrong',export_reason='Wrong export prior'))
    assert run(path,adapter)['status']=='blocked_export_failure' and len(calls)==1
    revise(path,lambda d:d['retry_policy'].update(export_after=failed['export_attempt_id'],export_reason='Storage restored; caller approves fresh export'))
    recovered=run(path,adapter)
    assert recovered['status']=='delivery_persisted' and len(calls)==1
    assert recovered['export_attempt_id']!=failed['export_attempt_id'] and recovered['proposal_reused']
    assert tree_bytes(old)==frozen
    assert run(path,adapter)['export_reused'] and len(calls)==1

def partial_inputs(path):
    from dataclasses import replace
    from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
    upstream=Path(json.loads(path.read_text())['upstream']['path'])
    inputs=job.saved.load_saved_electrical_inputs(upstream,journal_directory=path.parent/'partial-setup-journals')
    model=replace(inputs.model,levels=(),electrical_equipment=(),ports=())
    # All marks remain source observations; no physical placement is approved.
    decisions=tuple(replace(d,action='defer',placement=None,
        rationale='Reviewed registration, level/datum and physical mounting/orientation remain unresolved.')
        for d in inputs.kwargs['decisions'])
    prior=inputs.kwargs['assertion_review']
    assertion=PlacementAssertionReview.capture(model,inputs.review,decisions=decisions,registrations=(),
        canonical_datum_id=prior.canonical_datum_id,reviewer_id=prior.reviewer_id,
        reviewer_derivation=prior.reviewer_derivation,rationale='Source-only synthetic review; no physical facts approved.',assertions=())
    document=json.loads(upstream.read_text())
    def put(name,value):
        p=path.parent/name;data=job.saved._json(value);p.write_bytes(data)
        return {'path':str(p),'sha256':hashlib.sha256(data).hexdigest()}
    document.update(model=put('partial-model.json',model.to_dict()),
        decisions=put('partial-decisions.json',[d.to_dict() for d in decisions]),
        registrations=put('partial-registrations.json',[]),
        assertion_review=put('partial-assertions.json',assertion.to_dict()),
        proposal_groups={'deferred':list(d.proposal_id for d in decisions)},canonical_groups={})
    upstream.write_text(json.dumps(document))
    manifest=json.loads(path.read_text())
    manifest['upstream']['sha256']=hashlib.sha256(upstream.read_bytes()).hexdigest()
    manifest.update(settings=None,review=None,circuit_images={},circuit_documents={},policy_documents={})
    path.write_text(json.dumps(manifest))

def test_partial_source_job_needs_no_circuit_authority_or_provider(tmp_path):
    path,source,calls,adapter=setup(tmp_path);partial_inputs(path)
    # Even allow_dispatch cannot enter a stage without explicit configuration.
    result=run(path,adapter);again=run(path,adapter)
    assert result['status']==again['status']=='waiting_for_placement'
    assert result['job_complete'] is False and not calls
    assert result['placement_readiness']=={'status':'validated','source_proposals':3,
        'dispositions':{'defer':3},'eligible_circuit_devices':0,'canonical_levels':0,
        'canonical_devices':0,'canonical_equipment':0,'canonical_ports':0}
    fields=[row['field'] for row in result['missing_inputs']]
    assert fields.count('placement')==3
    assert {'circuit_configuration','review_authority','source_port','approved_profile','eligibility'}<=set(fields)
    assert len(result['placement_dispositions'])==3
    root=tmp_path/'jobs/synthetic-job'
    assert not list((root/'proposals').iterdir()) and not list((root/'exports').iterdir())
    assert not result['source_complete'] and not result['buildable'] and not result['code_compliance_verified']

def test_configuration_can_arrive_in_new_revision_before_dispatch(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    settings=json.loads(path.read_text())['settings']
    doc=json.loads(path.read_text());doc['settings']=None;path.write_text(json.dumps(doc))
    partial=run(path,adapter)
    assert partial['status']=='waiting_for_circuit_configuration' and not calls
    assert partial['placement_readiness']['eligible_circuit_devices']==2
    assert 'connector' in {row['field'] for row in partial['missing_inputs']}
    revise(path,lambda d:d.update(settings=settings))
    pending=run(path,adapter)
    assert pending['status']=='waiting_for_review' and len(calls)==1
    assert run(path,adapter)['proposal_reused'] and len(calls)==1

def test_empty_authority_still_blocks_configured_stage(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    doc=json.loads(path.read_text());settings=Path(doc['settings']['path'])
    body=json.loads(settings.read_text());body['review_policy']['authorities']=[]
    settings.write_text(json.dumps(body));doc['settings']['sha256']=hashlib.sha256(settings.read_bytes()).hexdigest()
    path.write_text(json.dumps(doc))
    with pytest.raises(job.SavedJobError,match='distinct explicit review authorities required'):
        run(path,adapter)
    assert not calls

@pytest.mark.parametrize('update',[
    {'settings':{}},{'settings':[]},{'settings':None,'review':{'path':'review.json','sha256':'a'*64}},
    {'settings':None,'circuit_images':[]},{'settings':None,'policy_documents':{'x':{'path':'https://invalid/input','sha256':'a'*64}}}])
def test_partial_manifest_keeps_exact_reference_validation(tmp_path,update):
    path,source,calls,adapter=setup(tmp_path)
    doc=json.loads(path.read_text());doc.update(update);path.write_text(json.dumps(doc))
    with pytest.raises(job.SavedJobError) as error:run(path,adapter)
    assert error.value.stage=='load_job' and not calls

def test_no_eligible_devices_with_valid_settings_still_waits(tmp_path):
    from dataclasses import replace
    from oabm.proposals.circuit_design import CircuitReviewPolicy
    path,source,calls,adapter=setup(tmp_path)
    original=json.loads(path.read_text())
    settings=job.CircuitJobSettings.from_json(Path(original['settings']['path']).read_text())
    partial_inputs(path)
    review_policy=CircuitReviewPolicy.capture(authorities=settings.review_policy.authorities,
        source_port_ids=(),connectors=(),profiles=())
    contexts=tuple(replace(c,status='missing',profile_ids=(),
        rationale='No approved profile supplied for this synthetic partial job.')
        if c.role=='project_rules' else c for c in settings.contexts)
    settings=replace(settings,review_policy=review_policy,source_port_ids=(),connectors=(),profiles=(),contexts=contexts)
    p=tmp_path/'empty-choices-settings.json';p.write_text(settings.to_json())
    doc=json.loads(path.read_text())
    doc['settings']={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    for key in ('circuit_images','circuit_documents','policy_documents'):doc[key]=original[key]
    path.write_text(json.dumps(doc))
    result=run(path,adapter)
    assert result['status']=='waiting_for_placement' and not result['job_complete'] and not calls
    assert {'eligibility','source_port','approved_profile','placement'}<={r['field'] for r in result['missing_inputs']}
    root=tmp_path/'jobs/synthetic-job'
    assert not list((root/'proposals').iterdir()) and not list((root/'exports').iterdir())
