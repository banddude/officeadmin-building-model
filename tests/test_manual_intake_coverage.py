import hashlib
from pathlib import Path
import pytest
from test_manual_device_intake import fixture_model,item
from oabm.electrical.manual_device_intake import manual_intake_request_sha256
from oabm.electrical.manual_intake_coverage import reconcile_manual_intake
from oabm.reports.source_coverage import model_fingerprint


def setup(tmp_path):
    source=tmp_path/'synthetic-source.dat';source.write_bytes(b'synthetic takeoff')
    model=fixture_model();request={'request_id':'request:synthetic','source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'base_model_sha256':model_fingerprint(model),'frame_id':'model','items':[item(1),{'source_item_id':'item:bulk','quantity':3}]}
    request['request_sha256']=manual_intake_request_sha256(**request)
    return model,request,source


def test_maps_admitted_device_and_port_and_keeps_bulk_unresolved(tmp_path):
    model,request,source=setup(tmp_path);before=model.to_json();candidate,report=reconcile_manual_intake(model,intake_request=request,source_path=source)
    rows=report['coverage']['rows'];mapped=next(row for row in rows if row['source_id']=='item:1');unresolved=next(row for row in rows if row['source_id']=='item:bulk')
    assert mapped['status']=='mapped' and set(mapped['canonical_ids'])=={'device:manual-1','port:manual-1'}
    assert unresolved['status']=='unresolved' and report['source_authentication']=='not_verified'
    assert report['candidate_model_sha256']==model_fingerprint(candidate) and model.to_json()==before
    with pytest.raises(ValueError,match='unresolved'):reconcile_manual_intake(model,intake_request=request,source_path=source,strict=True)


def test_source_file_changes_reject_stale_request_without_mutation(tmp_path):
    model,request,source=setup(tmp_path);before=model.to_json();source.write_bytes(b'changed')
    with pytest.raises(ValueError,match='differs'):reconcile_manual_intake(model,intake_request=request,source_path=source)
    assert model.to_json()==before


def test_invalid_strict_and_missing_file_rejected(tmp_path):
    model,request,source=setup(tmp_path)
    with pytest.raises(ValueError):reconcile_manual_intake(model,intake_request=request,source_path=source,strict=1)
    source.unlink()
    with pytest.raises(ValueError,match='source file'):reconcile_manual_intake(model,intake_request=request,source_path=source)


def test_saved_report_requires_current_reconstruction(tmp_path):
    import copy,json
    from importlib.resources import files
    from oabm.electrical.manual_intake_coverage import validate_manual_intake_coverage,_SCHEMA
    model,request,source=setup(tmp_path);candidate,report=reconcile_manual_intake(model,intake_request=request,source_path=source)
    assert validate_manual_intake_coverage(model,intake_request=request,source_path=source,report=report)[1]
    bad=copy.deepcopy(report);bad['strict']=True
    assert not _SCHEMA.is_valid(bad)
    bad=copy.deepcopy(report);bad['coverage']['rows'][0]['source_id']='forged'
    with pytest.raises(ValueError):validate_manual_intake_coverage(model,intake_request=request,source_path=source,report=bad)
    root=Path(__file__).resolve().parents[1];name='manual-intake-coverage-v1.schema.json'
    assert json.loads((root/'contracts'/name).read_text())==json.loads(files('oabm.electrical._manual_coverage_contracts').joinpath(name).read_text())


def test_saved_report_rejects_edited_flags(tmp_path):
    import copy
    from oabm.electrical.manual_intake_coverage import validate_manual_intake_coverage
    model,request,source=setup(tmp_path);_,report=reconcile_manual_intake(model,intake_request=request,source_path=source)
    bad=copy.deepcopy(report);bad['applied_to_original']=0
    with pytest.raises(ValueError):validate_manual_intake_coverage(model,intake_request=request,source_path=source,report=bad)


def test_source_descriptor_snapshot_does_not_claim_path_freshness(tmp_path,monkeypatch):
    import oabm.electrical.manual_intake_coverage as module
    model,request,source=setup(tmp_path);original=module._source_hash
    def swap_after_hash(path):
        digest=original(path);source.write_bytes(b'replacement after descriptor read');return digest
    monkeypatch.setattr(module,'_source_hash',swap_after_hash)
    _,report=module.reconcile_manual_intake(model,intake_request=request,source_path=source)
    assert report['coverage']['source_sha256']==request['source_sha256']
    assert any('path may change' in text for text in report['limitations'])
