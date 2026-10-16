"""Private persisted orchestration over existing saved inputs and circuit gates."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

from oabm.model import ContractError
from oabm.proposals.contracts import Portable, digest, fingerprint, text
from oabm.proposals.symbols import AttemptJournal, SymbolResourceLimits
from oabm.proposals.circuit_design import (VERSION as PROPOSAL_VERSION, TASK, VALIDATOR,
    CircuitProposalRequest, CircuitProposalRun, CircuitReviewDecision, CircuitRoutingPolicy,
    CircuitReviewPolicy, ApprovedConnector, ApprovedCircuitProfile, CircuitImage, CircuitContext,
    PLACEMENT_KEYS, _state, _invocation, dispatch_circuit_proposal, compile_circuit_review)
from oabm.proposals.circuit_transport import TRANSPORT_VERSION
from . import saved_electrical as saved
from .saved_revisions import _load as _verify_delivery

VERSION='persisted-electrical-job-v1'
KEYS={'version','job_id','parent_revision','upstream','settings','circuit_images','circuit_documents',
      'policy_documents','review','retry_policy'}
RETRY_KEYS={'allow_dispatch','proposal_after','proposal_reason','export_after','export_reason'}


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitJobSettings(Portable):
    policy: CircuitRoutingPolicy
    review_policy: CircuitReviewPolicy
    source_port_ids: tuple[str,...]
    connectors: tuple[ApprovedConnector,...]
    profiles: tuple[ApprovedCircuitProfile,...]
    images: tuple[CircuitImage,...]
    contexts: tuple[CircuitContext,...]
    prompt_version: str = 'bounded-circuit-choices-v1'
    resource_limits: SymbolResourceLimits = SymbolResourceLimits()

    def __post_init__(self):
        if not isinstance(self.policy,CircuitRoutingPolicy) or not isinstance(self.review_policy,CircuitReviewPolicy):
            raise ContractError('typed circuit job policies required')
        text(self.prompt_version,'job circuit prompt')


@dataclass(frozen=True,slots=True)
class SavedJobResult:
    report_json: str

    @property
    def report(self):return json.loads(self.report_json)


class SavedJobError(ContractError):
    def __init__(self,stage,cause):
        self.stage=stage
        super().__init__('persisted job failed at '+stage+': '+str(cause))


def _require(condition,reason):
    if not condition:raise ContractError(reason)


def _directory(path):
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    _require(not path.is_symlink() and path.is_dir(),'job directories must be ordinary private directories')
    _require(path.stat().st_mode & 0o077 == 0,'job directory permissions must be private')


def _atomic(path,data,*,replace=False):
    if path.exists() and not replace:
        _require(saved._read(path)==data,'immutable job artifact differs')
        return
    temp=path.parent/('.pending-'+uuid.uuid4().hex)
    saved._write(temp,data)
    if replace:os.replace(temp,path)
    else:
        try:os.link(temp,path)
        except FileExistsError:_require(saved._read(path)==data,'immutable job artifact collision')
        temp.unlink()
    saved._sync_dir(path.parent)


def _json(path,value,*,replace=False):_atomic(path,saved._json(value),replace=replace)

def _read_json(path):return saved._decode(saved._read(path))


def _safe_id(value):
    _require(isinstance(value,str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',value) is not None,'unsafe job/attempt identity')
    return value


def _versions():return {'job':VERSION,'task':TASK,'proposal':PROPOSAL_VERSION,'validator':VALIDATOR,'transport':TRANSPORT_VERSION}


@contextmanager
def _lock(root):
    fd=os.open(root/'LOCK',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise ContractError('job is owned by another active caller') from error
        yield
    finally:os.close(fd)


def _blob(root,data):
    sha=saved._sha(data);path=root/'blobs'/sha
    _atomic(path,data)
    return {'path':str(path),'sha256':sha}


def _blob_read(root,ref):
    _require(isinstance(ref,dict) and set(ref)=={'path','sha256'},'invalid private input reference')
    fingerprint(ref['sha256'],'private input digest')
    _require(ref['path']==str(root/'blobs'/ref['sha256']),'private input reference escaped blob storage')
    return saved.read_saved_reference(ref,root/'manifest.json','private snapshot')


def _runtime_refs(root,refs):
    _require(isinstance(refs,dict) and all(isinstance(k,str) and k for k in refs),'explicit input buffer mapping required')
    return {key:_blob_read(root,ref) for key,ref in refs.items()}


def _manifest_reference(ref, *, nullable=False):
    if nullable and ref is None:return
    _require(isinstance(ref,dict) and set(ref)=={'path','sha256'},
             'job input requires an exact local path/SHA-256 reference')
    _require(isinstance(ref['path'],str) and ref['path'].strip()
             and '://' not in ref['path'] and '\x00' not in ref['path'],
             'job input requires a local path')
    fingerprint(ref['sha256'],'job input SHA-256')


def _check_manifest(manifest):
    _require(isinstance(manifest,dict) and set(manifest)==KEYS and manifest['version']==VERSION,'unsupported exact job manifest')
    _safe_id(manifest['job_id'])
    _manifest_reference(manifest['upstream'])
    _manifest_reference(manifest['settings'],nullable=True)
    _manifest_reference(manifest['review'],nullable=True)
    _require(manifest['settings'] is not None or manifest['review'] is None,
             'circuit review requires circuit settings')
    for key in ('circuit_images','circuit_documents','policy_documents'):
        refs=manifest[key]
        _require(isinstance(refs,dict) and all(isinstance(k,str) and k for k in refs),
                 'job buffers must be explicit named mappings')
        for ref in refs.values():_manifest_reference(ref)
    if manifest['parent_revision'] is not None:fingerprint(manifest['parent_revision'],'parent job revision')
    retry=manifest['retry_policy']
    _require(isinstance(retry,dict) and set(retry)==RETRY_KEYS and isinstance(retry['allow_dispatch'],bool),'explicit complete job retry policy required')
    for stage in ('proposal','export'):
        after,reason=retry[stage+'_after'],retry[stage+'_reason']
        _require((after is None)==(reason is None),'retry identity and reason must be supplied together')
        if after is not None:_safe_id(after);text(reason,'explicit retry reason',1024)


def _session(root):
    path=root/'sessions'/uuid.uuid4().hex;_directory(path);saved._sync_dir(path.parent)
    return path


def _snapshot(root,manifest_path,manifest,raw,revision,session):
    directory=root/'revisions'/revision
    if directory.exists():
        _require(not directory.is_symlink(),'symlink revision refused')
        marker=_read_json(directory/'COMMITTED.json')
        _require(set(marker)=={'version','manifest_sha256','snapshot_sha256'} and marker['version']==VERSION
                 and marker['manifest_sha256']==revision==saved._sha(saved._read(directory/'manifest.json'))
                 and saved._read(directory/'manifest.json')==raw
                 and marker['snapshot_sha256']==saved._sha(saved._read(directory/'snapshot.json')),'revision checkpoint binding mismatch')
        _require({p.name for p in directory.iterdir()}=={'manifest.json','snapshot.json','COMMITTED.json'},'unexpected revision entries')
        snapshot=_read_json(directory/'snapshot.json')
        _require(snapshot['runtime']==_versions(),'job contract version changed; explicit new revision required')
        return snapshot
    parent=manifest['parent_revision']
    if parent is None:
        _require(not any(p.name[0]!='.' for p in (root/'revisions').iterdir()),'new revisions must identify an existing parent')
    else:
        prior=root/'revisions'/parent
        _require(saved._sha(saved._read(prior/'manifest.json'))==parent,'parent revision is unavailable or changed')
        _require(_read_json(prior/'manifest.json')['job_id']==manifest['job_id'],'parent belongs to a different job')
        _require((prior/'COMMITTED.json').is_file(),'parent revision is not committed')
    upstream_raw=saved.read_saved_reference(manifest['upstream'],manifest_path,'upstream saved inputs')
    upstream_path=Path(manifest['upstream']['path'])
    if not upstream_path.is_absolute():upstream_path=manifest_path.parent/upstream_path
    inputs=saved.load_saved_electrical_inputs(upstream_path,journal_directory=session/'capture-journals')
    _require(inputs.manifest_bytes==upstream_raw,'upstream manifest changed during snapshot')
    _require(inputs.kwargs['circuit_design'] is None,'upstream must be placement-only; circuit design belongs to the job')
    upstream=saved._decode(inputs.manifest_bytes)
    # Only the saved reader's known reference labels are snapshotted. Arbitrary
    # model/rule attributes are never interpreted as paths or executable input.
    for label,data in inputs.input_payloads.items():
        key,separator,identity=label.partition('/')
        if separator:upstream[key][identity]=_blob(root,data)
        else:upstream[key]=_blob(root,data)
    settings_raw=None
    if manifest['settings'] is not None:
        settings_raw=saved.read_saved_reference(manifest['settings'],manifest_path,'circuit settings')
        CircuitJobSettings.from_json(settings_raw.decode())
    snapshot={'runtime':_versions(),'upstream':_blob(root,saved._json(upstream)),
        'settings':None if settings_raw is None else _blob(root,settings_raw),'upstream_original':_blob(root,upstream_raw),
        'review':None,'retry_policy':manifest['retry_policy']}
    for key in ('circuit_images','circuit_documents','policy_documents'):
        refs=manifest[key];_require(isinstance(refs,dict),'job buffers must be explicit mappings')
        snapshot[key]={identity:_blob(root,saved.read_saved_reference(ref,manifest_path,key+'/'+identity)) for identity,ref in refs.items()}
    if manifest['review'] is not None:
        data=saved.read_saved_reference(manifest['review'],manifest_path,'trusted circuit review')
        CircuitReviewDecision.from_json(data.decode());snapshot['review']=_blob(root,data)
    pending=root/'revisions'/('.pending-'+uuid.uuid4().hex);_directory(pending)
    _atomic(pending/'manifest.json',raw);_json(pending/'snapshot.json',snapshot)
    _json(pending/'COMMITTED.json',{'version':VERSION,'manifest_sha256':revision,
        'snapshot_sha256':saved._sha(saved._read(pending/'snapshot.json'))})
    saved._sync_dir(pending);os.rename(pending,directory);saved._sync_dir(directory.parent)
    return snapshot


def _bind(root,revision,label,key):
    path=root/'bindings'/revision;_directory(path)
    _json(path/(label+'.json'),{'version':VERSION,'key':key})


def _head(stage):
    path=stage/'HEAD.json'
    if not path.exists():return None
    value=_read_json(path)
    _require(set(value)=={'version','attempt_id'} and value['version']==VERSION,'invalid stage pointer')
    return _safe_id(value['attempt_id'])


def _new_attempt(stage,key,revision,previous,reason):
    identity='a-'+uuid.uuid4().hex;directory=stage/identity;_directory(directory)
    _json(directory/'attempt.json',{'version':VERSION,'attempt_id':identity,'execution_key':key,
        'revision':revision,'retry_of':previous,'retry_reason':reason})
    return identity,directory


def _select(stage,identity):_json(stage/'HEAD.json',{'version':VERSION,'attempt_id':identity},replace=True)


def _attempt_metadata(directory,key,identity):
    value=_read_json(directory/'attempt.json')
    _require(set(value)=={'version','attempt_id','execution_key','revision','retry_of','retry_reason'}
             and value['version']==VERSION and value['attempt_id']==identity and value['execution_key']==key,
             'stage attempt binding mismatch')
    return value


def _persist_run(directory,run):
    _atomic(directory/'run.json',run.to_json().encode())
    _json(directory/'COMPLETED.json',{'version':VERSION,'run_sha256':saved._sha(saved._read(directory/'run.json')),
        'journal_sha256':saved._sha(saved._read(directory/'attempts.jsonl'))})


def _recover_run(directory,request,policy):
    _require(CircuitProposalRequest.from_json(saved._read(directory/'request.json').decode()).to_dict()==request.to_dict()
             and CircuitRoutingPolicy.from_json(saved._read(directory/'policy.json').decode()).to_dict()==policy.to_dict(),
             'proposal checkpoint request/policy mismatch')
    path=directory/'attempts.jsonl'
    if not path.exists():return None
    receipts=AttemptJournal.read(path)
    if len(receipts)<2:return None
    run=CircuitProposalRun(request=request,policy=policy,attempts=receipts,
        selected_attempt_id=receipts[-1].attempt_id if receipts[-1].status=='completed' else None)
    if (directory/'run.json').exists():
        _require(CircuitProposalRun.from_json(saved._read(directory/'run.json').decode()).to_dict()==run.to_dict(),'saved run contradicts raw execution journal')
    if (directory/'COMPLETED.json').exists():
        marker=_read_json(directory/'COMPLETED.json')
        _require(marker=={'version':VERSION,'run_sha256':saved._sha(saved._read(directory/'run.json')),
            'journal_sha256':saved._sha(saved._read(path))},'proposal completion binding mismatch')
    _persist_run(directory,run)
    return run


def _proposal(root,revision,request,settings,kwargs,retry,adapters):
    key=digest(_invocation(request));_bind(root,revision,'proposal',key)
    stage=root/'proposals'/key;_directory(stage)
    previous=_head(stage)
    if previous:
        directory=stage/previous;_attempt_metadata(directory,key,previous)
        run=_recover_run(directory,request,settings.policy)
        if run is None:return None,directory,{'status':'blocked_in_doubt','proposal_attempt_id':previous}
        if run.selected_attempt_id is not None:return run,directory,{'proposal_attempt_id':previous,'proposal_reused':True}
        if retry['proposal_after']!=previous:
            return None,directory,{'status':'blocked_provider_failure','proposal_attempt_id':previous,'provider_status':run.attempts[-1].status}
    if not retry['allow_dispatch']:return None,None,{'status':'blocked_dispatch_policy'}
    if not callable(adapters.get(settings.policy.endpoint.adapter_id)):return None,None,{'status':'waiting_for_adapter'}
    identity,directory=_new_attempt(stage,key,revision,previous,retry['proposal_reason'] if previous else None)
    _atomic(directory/'request.json',request.to_json().encode());_atomic(directory/'policy.json',settings.policy.to_json().encode())
    with AttemptJournal.create(directory/'attempts.jsonl',run_id=identity) as journal:
        _select(stage,identity)  # durable ownership of the external attempt before dispatch
        run=dispatch_circuit_proposal(request,settings.policy,**kwargs,adapters=adapters,journal=journal)
    _persist_run(directory,run)
    info={'proposal_attempt_id':identity,'proposal_reused':False}
    if run.selected_attempt_id is None:info.update(status='blocked_provider_failure',provider_status=run.attempts[-1].status)
    return run if run.selected_attempt_id is not None else None,directory,info


def _export(root,revision,manifest,retry):
    key=digest(manifest);_bind(root,revision,'export',key)
    stage=root/'exports'/key;_directory(stage);previous=_head(stage)
    if previous:
        directory=stage/previous;_attempt_metadata(directory,key,previous)
        output=directory/'published'/previous/'delivery'
        if output.is_dir() and not (output.parent/'failure.json').exists():
            model,report,sources,metadata=_verify_delivery(output)
            marker={'version':VERSION,'delivery_marker_sha256':saved._sha(saved._read(output/'COMPLETED.json'))}
            if (directory/'COMPLETED.json').exists():
                _require(_read_json(directory/'COMPLETED.json')==marker,'export completion binding mismatch')
            else:_json(directory/'COMPLETED.json',marker)
            return output,report,{'export_attempt_id':previous,'export_reused':True}
        if retry['export_after']!=previous:return None,None,{'status':'blocked_export_failure','export_attempt_id':previous}
    identity,directory=_new_attempt(stage,key,revision,previous,retry['export_reason'] if previous else None)
    _json(directory/'inputs.json',manifest);_select(stage,identity)
    try:output=saved.persist_saved_electrical(directory/'inputs.json',output_root=directory/'published',run_id=identity)
    except saved.SavedDeliveryError as error:
        _json(directory/'failure.json',{'version':VERSION,'stage':error.stage,'error_type':type(error.__cause__).__name__})
        return None,None,{'status':'blocked_export_failure','export_attempt_id':identity}
    model,report,sources,metadata=_verify_delivery(output)
    _json(directory/'COMPLETED.json',{'version':VERSION,'delivery_marker_sha256':saved._sha(saved._read(output/'COMPLETED.json'))})
    return output,report,{'export_attempt_id':identity,'export_reused':False}


def _partial_readiness(inputs,placement):
    # Replay the real placement gate without requiring downstream authority.
    # This reports missing choices; it never asserts that source plans lack them.
    outcome,eligible=_state(inputs.model,inputs.review,placement)
    missing=[
        {'field':'circuit_configuration','proposal_id':None,
         'reason':'No downstream circuit settings supplied.',
         'needed':'Explicit caller-selected circuit configuration before entering that stage.'},
        {'field':'review_authority','proposal_id':None,
         'reason':'No circuit review authority supplied.',
         'needed':'Explicit authorized circuit reviewer policy before dispatch and review.'},
        {'field':'source_port','proposal_id':None,
         'reason':'No panel source port has been selected for circuit design.',
         'needed':'Caller selection of an existing reviewed panel source port.'},
        {'field':'approved_profile','proposal_id':None,
         'reason':'No approved circuit design profile has been selected.',
         'needed':'Explicit caller-approved rules and routing controls.'}]
    if not eligible:
        missing.append({'field':'eligibility','proposal_id':None,
            'reason':'No admitted devices available.','needed':'Reviewed physical placements.'})
    for row in outcome.dispositions:
        if row['status'] in ('defer','pending'):
            missing.append({'field':'placement','proposal_id':row['proposal_id'],
                'reason':row['reason'],'needed':'Resolve upstream placement evidence.'})
    for proposal_id,canonical_id in eligible:
        missing.append({'field':'connector','proposal_id':proposal_id,
            'reason':'No approved connector selected.','needed':'Approved connector offset and direction.'})
    return {'status':'waiting_for_placement' if not eligible else 'waiting_for_circuit_configuration',
        'job_complete':False,'missing_inputs':missing,
        'placement_readiness':{'status':'validated','source_proposals':len(inputs.review.proposals),
            'dispositions':dict(Counter(row['status'] for row in outcome.dispositions)),
            'eligible_circuit_devices':len(eligible),
            'canonical_levels':len(outcome.model.levels),
            'canonical_devices':len(outcome.model.electrical_devices),
            'canonical_equipment':len(outcome.model.electrical_equipment),
            'canonical_ports':len(outcome.model.ports)},
        'placement_dispositions':list(outcome.dispositions),
        'circuit_readiness':{'configuration':'missing','review_authority':'missing','provider_called':False}}


def run_saved_electrical_job(manifest_path, *, job_root, adapters=None):
    """Advance one immutable job revision; no automatic review or uncertain retry.

    Existing symbol executions are replayed, never redispatched. A completed
    circuit journal can recover missing parsed checkpoints. An unfinished call
    remains in doubt even if retry was requested. Registered adapters are runtime
    dependencies; the manifest contains no executable adapter loading or secrets.
    """
    stage='load_job';session=None;report={}
    try:
        manifest_path=Path(manifest_path).absolute();raw=saved._read(manifest_path);manifest=saved._decode(raw);_check_manifest(manifest)
        revision=saved._sha(raw);root=Path(job_root).absolute()/manifest['job_id'];_directory(root)
        for name in ('blobs','revisions','bindings','sessions','proposals','exports'):_directory(root/name)
        with _lock(root):
            _json(root/'identity.json',{'version':VERSION,'job_id':manifest['job_id']})
            session=_session(root)
            report={'version':VERSION,'job_id':manifest['job_id'],'revision':revision,'status':'running',
                'source_complete':False,'buildable':False,'code_compliance_verified':False}
            def finish(values):
                report.update(values);_json(session/'result.json',report)
                return SavedJobResult(saved._json(report).decode())
            stage='snapshot_inputs';snapshot=_snapshot(root,manifest_path,manifest,raw,revision,session)
            stage='load_saved_stages'
            upstream_data=_blob_read(root,snapshot['upstream']);upstream_path=session/'upstream.json';_atomic(upstream_path,upstream_data)
            inputs=saved.load_saved_electrical_inputs(upstream_path,journal_directory=session/'journals')
            placement={key:inputs.kwargs[key] for key in PLACEMENT_KEYS}
            if snapshot['settings'] is None:
                stage='placement_readiness'
                return finish(_partial_readiness(inputs,placement))
            settings=CircuitJobSettings.from_json(_blob_read(root,snapshot['settings']).decode())
            kwargs={'review_policy':settings.review_policy,'model':inputs.model,'review':inputs.review,'placement':placement,
                'images':_runtime_refs(root,snapshot['circuit_images']),'documents':_runtime_refs(root,snapshot['circuit_documents']),
                'policy_documents':_runtime_refs(root,snapshot['policy_documents'])}
            stage='capture_circuit_request'
            request=CircuitProposalRequest.capture(inputs.model,inputs.review,placement=placement,policy=settings.policy,
                review_policy=settings.review_policy,source_port_ids=settings.source_port_ids,connectors=settings.connectors,
                profiles=settings.profiles,images=settings.images,contexts=settings.contexts,prompt_version=settings.prompt_version,
                resource_limits=settings.resource_limits)
            request.validate_inputs(policy=settings.policy,**kwargs)
            report['missing_inputs']=[row.to_dict() for row in request.required_missing_inputs]
            if not request.eligible:
                return finish({'status':'waiting_for_placement','job_complete':False,
                    'placement_dispositions':list(request.placement_dispositions)})
            stage='circuit_proposal';run,directory,info=_proposal(root,revision,request,settings,kwargs,snapshot['retry_policy'],adapters or {})
            report.update(info)
            if run is None:return finish({})
            report.update(circuit_run={'path':str(directory/'run.json'),'sha256':saved._sha(saved._read(directory/'run.json'))},
                          circuit_journal={'path':str(directory/'attempts.jsonl'),'sha256':saved._sha(saved._read(directory/'attempts.jsonl'))})
            if snapshot['review'] is None:return finish({'status':'waiting_for_review'})
            stage='compile_review'
            decision=CircuitReviewDecision.from_json(_blob_read(root,snapshot['review']).decode())
            compiled=compile_circuit_review(run,decision,**kwargs,journal_path=directory/'attempts.jsonl')
            compiled_key=digest({'request':compiled.request.to_dict(),'decision_sha256':saved._sha(compiled.decision_bytes)})
            _bind(root,revision,'compiled',compiled_key)
            downstream=saved._decode(upstream_data);downstream['version']=saved.CIRCUIT_VERSION
            downstream['circuit_design']=_blob(root,compiled.request.to_json().encode())
            downstream['evidence_documents'].update({sha:_blob(root,data) for sha,data in compiled.evidence_documents.items()})
            sources=downstream['source_references'];identity=('reviewed-circuit-design',compiled.request.request_id)
            _require(not any((row['source_kind'],row['source_id'])==identity for row in sources),'generated design source reference collides with supplied registry')
            sources.append({'source_kind':identity[0],'source_id':identity[1],'reference':_blob(root,compiled.decision_bytes)['path']})
            report['missing_inputs']=[row.to_dict() for row in compiled.missing_inputs]
            stage='export';output,completion,export_info=_export(root,revision,downstream,snapshot['retry_policy']);report.update(export_info)
            if output is None:return finish({})
            return finish({'status':'delivery_persisted','delivery':str(output),
                'delivery_marker_sha256':saved._sha(saved._read(output/'COMPLETED.json')),
                'delivery_claims':completion['claims'],'physical_completeness':completion['physical_completeness'],
                'circuit_design_status':completion['circuit_design']['status']})
    except Exception as error:
        if session is not None:
            try:_json(session/'failure.json',{'version':VERSION,'stage':stage,'error_type':type(error).__name__})
            except OSError:pass
        raise SavedJobError(stage,error) from error


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest');parser.add_argument('--job-root',required=True)
    args=parser.parse_args(argv)
    try:
        result=run_saved_electrical_job(args.manifest,job_root=args.job_root)
        print(result.report_json)
        return 0 if result.report['status']=='delivery_persisted' else 2
    except (ContractError,OSError) as error:
        print(str(error),file=__import__('sys').stderr);return 1


if __name__=='__main__':raise SystemExit(main())
