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
