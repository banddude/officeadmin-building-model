"""Real saved synthetic deliveries, strict replay and unchanged canonical diff."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from oabm.model import BuildingModel, ContractError
from oabm.pipeline.saved_electrical import persist_saved_electrical
from oabm.pipeline.saved_revisions import compare_saved_reviews
from oabm.proposals.contracts import digest
from oabm.qa import build_revision_diff
from test_reviewed_circuit_design import case, save_inputs


def read(path):return json.loads(path.read_bytes())
def write(path,value):path.write_text(json.dumps(value,sort_keys=True))


@pytest.fixture(scope='module')
def deliveries(tmp_path_factory):
    root=tmp_path_factory.mktemp('saved-circuit-revision-source')
    value=case(root/'source')
    manifest=save_inputs(root/'inputs',value)
    cli=subprocess.run([sys.executable,'-m','oabm.pipeline.saved_electrical',str(manifest),
        '--output-root',str(root/'out'),'--run-id','a'],capture_output=True,text=True)
    assert cli.returncode==0,cli.stderr
    a=Path(cli.stdout.strip())
    b=persist_saved_electrical(manifest,output_root=root/'out',run_id='b')
    changed=case(root/'changed-source',height=2.2)
    c=persist_saved_electrical(save_inputs(root/'changed-inputs',changed),output_root=root/'out',run_id='changed')
    document=read(manifest);document['circuit_design']=None
    disabled_manifest=manifest.parent/'disabled.json';write(disabled_manifest,document)
    disabled=persist_saved_electrical(disabled_manifest,output_root=root/'out',run_id='disabled')
    return a,b,c,disabled


def snapshot(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_repeated_actual_deliveries_have_exact_diff_and_explicit_lineage(deliveries):
    a,b,_,_=deliveries;before=(snapshot(a),snapshot(b))
    result=compare_saved_reviews(a,b)
    am=BuildingModel.from_json((a/'model.json').read_text());bm=BuildingModel.from_json((b/'model.json').read_text())
    assert result.diff_json==build_revision_diff(am,bm).to_json()
    assert not result.diff.changes and not result.diff.quantity_deltas
    for side,path in (('base',a),('target',b)):
        lineage=result.context[side]['circuit_design_lineage'];report=read(path/'circuit-design.json')
        assert lineage['request']==report['request']
        assert lineage['request_fingerprint']==report['request_fingerprint']
        assert lineage['created_ids']==report['created_ids']
        assert not lineage['source_observed'] and not lineage['buildable'] and not lineage['code_compliance_verified']
        assert 'source pixels not revalidated' in lineage['verification']
    assert result.context['base']['circuit_design_lineage']==result.context['target']['circuit_design_lineage']
    assert (snapshot(a),snapshot(b))==before


def test_reviewed_height_change_retains_logical_ids_and_exact_canonical_diff(deliveries):
    a,_,c,_=deliveries;result=compare_saved_reviews(a,c)
    am=BuildingModel.from_json((a/'model.json').read_text());cm=BuildingModel.from_json((c/'model.json').read_text())
    for name in ('electrical_devices','circuits','routes','conductors'):
        assert {e.id for e in getattr(am,name)}=={e.id for e in getattr(cm,name)}
    assert result.diff_json==build_revision_diff(am,cm).to_json()
    deltas={r.category+':'+r.item_type:r.delta for r in result.diff.quantity_deltas}
    assert deltas['route_length:pvc']==pytest.approx(1.)
    assert deltas['conductor_length:insulated']==pytest.approx(3.)
    assert deltas['conductor_length:ground']==pytest.approx(1.)
    assert result.context['base']['circuit_design_lineage']['request_fingerprint']!=result.context['target']['circuit_design_lineage']['request_fingerprint']
    assert result.context['base']['circuit_design_lineage']['request']['assertion_review_fingerprint']!=result.context['target']['circuit_design_lineage']['request']['assertion_review_fingerprint']
    assert not result.context['permit_ready']


def test_disabled_comparison_retains_original_shape(deliveries):
    *_,path=deliveries;result=compare_saved_reviews(path,path)
    assert not result.diff.changes
    for side in ('base','target'):
        assert set(result.context[side])=={'directory','completion_marker_sha256','canonical_model_sha256','verified_files','retained_review_base_fingerprint'}
    assert not (path/'circuit-inputs').exists()
    assert 'circuit_design' not in read(path/'completion-report.json')['stages']


@contextmanager
def mutation(path):
    original=snapshot(path)
    try:yield
    finally:
        for name,data in original.items():
            target=path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        for item in path.rglob('*'):
            if item.is_file() and item.relative_to(path).as_posix() not in original:item.unlink()


def rehash(path):
    report=read(path/'completion-report.json')
    for row in report['artifacts'].values():
        data=Path(row['reference']).read_bytes();row.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
    write(path/'completion-report.json',report)
    marker=read(path/'COMPLETED.json')
    for row in marker['files']:
        data=(path/row['path']).read_bytes();row.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
    write(path/'COMPLETED.json',marker)


@pytest.mark.parametrize('fault',['stale_request','changed_rules','created_ids','admitted_pose','missing_design_evidence',
    'missing_placement_evidence','evidence_bytes','missing_stage','missing_receipt','missing_artifact','request_bytes',
    'physical_limits','inventory_group','missing_unresolved'])
def test_rehashed_tampering_cannot_replace_semantic_replay(deliveries,fault):
    _,path,_,_=deliveries
    with mutation(path):
        report=read(path/'completion-report.json');design=read(path/'circuit-design.json')
        receipt=read(path/'input-receipt.json')
        if fault in ('stale_request','changed_rules'):
            request=read(path/'circuit-inputs/request.json')
            if fault=='stale_request':request['assertion_review_fingerprint']='0'*64
            else:request['rules']['assumed_voltage_v']=96.
            write(path/'circuit-inputs/request.json',request)
            data=(path/'circuit-inputs/request.json').read_bytes()
            row=next(r for r in receipt['files'] if r['input']=='circuit_design')
            row.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
            design['request']=request;design['request_fingerprint']=digest(request)
            report['input_bindings']['circuit_design_fingerprint']=digest(request)
        elif fault=='created_ids':design['created_ids']=design['created_ids'][:-1]
        elif fault=='admitted_pose':
            admitted=read(path/'circuit-inputs/admitted-model.json');admitted['electrical_devices'][0]['pose']['position']['z']+=.5
            write(path/'circuit-inputs/admitted-model.json',admitted)
            design['admitted_model_sha256']=hashlib.sha256((path/'circuit-inputs/admitted-model.json').read_bytes()).hexdigest()
        elif fault in ('missing_design_evidence','missing_placement_evidence'):
            identity=design['request']['evidence'][0]['document_sha256'] if fault=='missing_design_evidence' else next(
                e['document_sha256'] for fact in read(path/'circuit-inputs/admitted-model.json')['attributes']['placement_assertion_reviews'][-1]['review']['assertions']
                for e in fact['evidence'] if e['document_sha256']!=design['request']['evidence'][0]['document_sha256'])
            name='circuit-inputs/evidence/'+identity+'.bin';(path/name).unlink()
            marker=read(path/'COMPLETED.json');marker['files']=[r for r in marker['files'] if r['path']!=name];write(path/'COMPLETED.json',marker)
        elif fault=='evidence_bytes':
            identity=design['request']['evidence'][0]['document_sha256'];(path/('circuit-inputs/evidence/'+identity+'.bin')).write_bytes(b'forged')
        elif fault=='physical_limits':report['physical_completeness']['routing']='not_requested'
        elif fault=='inventory_group':report['quantity_eligibility']['group_bindings']['resolved'][design['request']['inventory_group']]=[]
        elif fault=='missing_unresolved':report['unresolved']=[r for r in report['unresolved'] if r['code']!='preliminary_design_engineering_capacity_and_supports_unverified']
        elif fault=='missing_stage':del report['stages']['circuit_design']
        elif fault=='missing_receipt':receipt['files']=[r for r in receipt['files'] if r['input']!='circuit_design']
        elif fault=='missing_artifact':del report['artifacts']['circuit_design'];report['requested_outputs'].remove('circuit_design')
        else:(path/'circuit-inputs/request.json').write_bytes(b'{}')
        report['circuit_design']=design
        write(path/'circuit-design.json',design);write(path/'completion-report.json',report);write(path/'input-receipt.json',receipt)
        rehash(path)
        with pytest.raises(ContractError):compare_saved_reviews(path,path)


@pytest.mark.parametrize('defer',['blocked','incomplete'])
def test_legitimate_deferred_circuit_request_replays_without_partial_graph(tmp_path,defer):
    value=case(tmp_path/'source',blocked=defer=='blocked')
    if defer=='incomplete':value=(*value[:3],replace(value[3],rules=None))
    path=persist_saved_electrical(save_inputs(tmp_path/'input',value),output_root=tmp_path/'out',run_id='deferred')
    result=compare_saved_reviews(path,path)
    assert not result.diff.changes
    lineage=result.context['base']['circuit_design_lineage']
    assert lineage['status']=='deferred' and not lineage['created_ids'] and not lineage['quantity_entity_ids']
    assert lineage['reasons']


@pytest.mark.parametrize('directory',['evidence','circuit-inputs'])
def test_validation_directory_sync_failure_cannot_publish(tmp_path,monkeypatch,directory):
    import oabm.pipeline.saved_electrical as saved
    value=case(tmp_path/'source');manifest=save_inputs(tmp_path/'input',value)
    original=saved._sync_dir
    def fail(path):
        if path.name==directory:raise OSError('Synthetic circuit validation directory sync failure')
        return original(path)
    monkeypatch.setattr(saved,'_sync_dir',fail)
    with pytest.raises(saved.SavedDeliveryError):
        saved.persist_saved_electrical(manifest,output_root=tmp_path/'out',run_id='sync-failed')
    assert not (tmp_path/'out/sync-failed/delivery').exists()
    assert not list((tmp_path/'out').rglob('COMPLETED.json'))
