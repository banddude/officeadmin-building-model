"""Completed synthetic saved-entrypoint deliveries; no patched successful snapshots."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from oabm.model import BuildingModel, ContractError, ElectricalDevice, Pose, Point3
from oabm.importers.pdf_electrical.symbol_stage import SymbolDecision,SymbolRegistration,SymbolReview
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.pipeline.saved_electrical import persist_saved_electrical
from oabm.pipeline.saved_revisions import compare_saved_reviews
from oabm.qa import build_revision_diff
from test_saved_electrical import source,altered,snapshot


@pytest.fixture(scope="module")
def original_source(tmp_path_factory):
    return source.__wrapped__(tmp_path_factory)


def variant(source_path,path,*,height=None,model_id=None,frame=None,schema=None,extra_base=False):
    path.mkdir(parents=True)
    def configure(manifest):
        def read(key):return json.loads(Path(manifest[key]["path"]).read_bytes())
        def put(key,value):
            f=path/(key+".json");f.write_text(json.dumps(value,sort_keys=True))
            manifest[key]={"path":str(f),"sha256":hashlib.sha256(f.read_bytes()).hexdigest()}
        model=BuildingModel.from_dict(read("model"))
        review=SymbolReview.from_dict(read("review"))
        decisions=tuple(SymbolDecision.from_dict(r) for r in read("decisions"))
        registrations=tuple(SymbolRegistration.from_dict(r) for r in read("registrations"))
        prior=PlacementAssertionReview.from_dict(read("assertion_review"))
        facts=prior.assertions
        if height is not None:
            decisions=tuple(replace(d,placement=replace(d.placement,height_above_level_m=height)) for d in decisions)
            facts=tuple(replace(f,value=height) if f.field=="elevation" else f for f in facts)
        if extra_base:
            extra=ElectricalDevice(id="device:additional-base",device_type="luminaire",level_id=model.levels[0].id,
                pose=Pose(position=Point3(x=1.,y=1.,z=4.)))
            model=replace(model,electrical_devices=(*model.electrical_devices,extra))
            manifest["canonical_groups"]={"explicit-base-addition":[extra.id]}
        if model_id is not None:model=replace(model,model_id=model_id)
        if schema is not None:model=replace(model,schema_version=schema)
        if frame is not None:
            old_to_new={r.fingerprint:replace(r,transform=replace(r.transform,frame_id=frame)) for r in registrations}
            registrations=tuple(old_to_new.values())
            decisions=tuple(replace(d,placement=replace(d.placement,
                registration_fingerprint=old_to_new[d.placement.registration_fingerprint].fingerprint)) for d in decisions)
            facts=tuple(replace(f,frame_id=frame,
                reference_id=old_to_new[f.reference_id].fingerprint if f.field in ("anchor","registration_xy") else
                    frame if f.field=="yaw" else f.reference_id) for f in facts)
            model=replace(model,coordinate_system=replace(model.coordinate_system,frame_id=frame))
        model=BuildingModel.from_dict(model.to_dict())
        assertion=PlacementAssertionReview.capture(model,review,decisions=decisions,registrations=registrations,
            canonical_datum_id=prior.canonical_datum_id,reviewer_id=prior.reviewer_id,
            reviewer_derivation=prior.reviewer_derivation,rationale="Newly captured synthetic review revision.",assertions=facts)
        put("model",model.to_dict());put("decisions",[d.to_dict() for d in decisions])
        put("registrations",[r.to_dict() for r in registrations]);put("assertion_review",assertion.to_dict())
    return altered(source_path,path,configure)


def pair(source_path,tmp_path,**changes):
    first=variant(source_path,tmp_path/"input-a")
    second=variant(source_path,tmp_path/"input-b",**changes)
    a=persist_saved_electrical(first,output_root=tmp_path/"out",run_id="a")
    b=persist_saved_electrical(second,output_root=tmp_path/"out",run_id="b")
    return a,b


def test_new_reviewed_elevation_preserves_id_and_has_no_quantity_delta(original_source,tmp_path):
    a,b=pair(original_source,tmp_path,height=2.4)
    before=snapshot(tmp_path)
    result=compare_saved_reviews(a,b)
    am=BuildingModel.from_json((a/"model.json").read_text())
    bm=BuildingModel.from_json((b/"model.json").read_text())
    assert am.electrical_devices[0].id==bm.electrical_devices[0].id
    assert bm.electrical_devices[0].pose.position.z==pytest.approx(5.4)
    assert result.diff_json==build_revision_diff(am,bm).to_json()
    diff=result.diff
    assert diff.added_count==diff.removed_count==0 and diff.changed_count==1
    assert not diff.quantity_deltas
    assert any(f.path.startswith("pose") for f in diff.changes[0].field_changes)
    assert not result.context["permit_ready"] and result.context["pricing"]=="not_performed"
    for side in ("base","target"):
        limits=result.context["inherited_limits"][side]
        assert limits["source_coverage"]["status"]=="unavailable"
        assert limits["physical_completeness"]["status"]=="not_verified"
        assert not any(limits["claims"].values())
    assert snapshot(tmp_path)==before


def test_identical_versions_ignore_reference_paths_and_cli_retains_context(original_source,tmp_path):
    a,b=pair(original_source,tmp_path)
    result=compare_saved_reviews(a,b)
    assert not result.diff.changes and not result.diff.quantity_deltas
    assert result.diff_json==compare_saved_reviews(a,b).diff_json
    process=subprocess.run([sys.executable,"-m","oabm.pipeline.saved_revisions",str(a),str(b)],capture_output=True,text=True)
    assert process.returncode==0,process.stderr
    envelope=json.loads(process.stdout)
    assert envelope["diff"]==result.diff.to_dict() and not envelope["context"]["permit_ready"]


@pytest.mark.parametrize("change",[{"model_id":"synthetic:other-lineage"},{"frame":"other-frame"},{"schema":"1.4.0"}])
def test_legitimately_saved_incomparable_snapshots_refused(original_source,tmp_path,change):
    a,b=pair(original_source,tmp_path,**change)
    with pytest.raises(ContractError,match="different"):
        compare_saved_reviews(a,b)


def test_changed_actual_pdf_bytes_refuse_identity_reconciliation(original_source,tmp_path,tmp_path_factory,monkeypatch):
    import test_takeoff_proposals as takeoff
    import test_symbol_proposals as symbols
    pdf=takeoff.source_pdf()+b"\n% independent synthetic source revision\n"
    # Re-render and dispatch fresh in-process stub receipts through existing helpers.
    with monkeypatch.context() as patch:
        patch.setattr(takeoff,"source_pdf",lambda:pdf)
        patch.setattr(symbols,"source_pdf",lambda:pdf)
        changed=source.__wrapped__(tmp_path_factory)
    a=persist_saved_electrical(variant(original_source,tmp_path/"input-a"),output_root=tmp_path/"out",run_id="a")
    b=persist_saved_electrical(variant(changed,tmp_path/"input-b"),output_root=tmp_path/"out",run_id="b")
    with pytest.raises(ContractError,match="identity reconciliation unavailable"):
        compare_saved_reviews(a,b)


def rehash_marker(final):
    marker=json.loads((final/"COMPLETED.json").read_bytes())
    for row in marker["files"]:
        data=(final/row["path"]).read_bytes()
        row.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
    (final/"COMPLETED.json").write_text(json.dumps(marker))


@pytest.mark.parametrize("mode",["missing_marker","not_ready","tampered_bytes","unlisted_file","pending_path","bad_report_binding","stale_model","durability_unknown","stale_quantities","bad_assertion_binding","false_completeness"])
def test_incomplete_and_tampered_deliveries_refused(original_source,tmp_path,mode):
    a,b=pair(original_source,tmp_path)
    if mode=="missing_marker":(b/"COMPLETED.json").rename(b/"absent-marker")
    elif mode=="tampered_bytes":(b/"model.json").write_bytes(b"{}")
    elif mode=="unlisted_file":(b/"unlisted.json").write_text("{}")
    elif mode=="pending_path":
        pending=b.parent/".pending";b.rename(pending);b=pending
    elif mode=="durability_unknown":(b.parent/"failure.json").write_text('{"state":"durability_unknown"}')
    elif mode=="stale_quantities":
        quantity=json.loads((b/"quantities.json").read_bytes());quantity["items"][0]["quantity"]+=1.
        raw=json.dumps(quantity).encode();(b/"quantities.json").write_bytes(raw)
        report=json.loads((b/"completion-report.json").read_bytes())
        report["artifacts"]["quantities"].update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        (b/"completion-report.json").write_text(json.dumps(report));rehash_marker(b)
    elif mode=="stale_model":
        d=json.loads((b/"model.json").read_bytes());d["electrical_devices"][0]["pose"]["position"]["z"]+=1.
        raw=json.dumps(d).encode();(b/"model.json").write_bytes(raw)
        report=json.loads((b/"completion-report.json").read_bytes())
        report["artifacts"]["canonical_model"].update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        report["canonical_output_sha256"]=hashlib.sha256(raw).hexdigest()
        (b/"completion-report.json").write_text(json.dumps(report));rehash_marker(b)
    else:
        report=json.loads((b/"completion-report.json").read_bytes())
        if mode=="not_ready":report["delivery_ready"]=False
        elif mode=="bad_assertion_binding":report["input_bindings"]["placement_assertion_review_fingerprint"]="0"*64
        elif mode=="false_completeness":report["physical_completeness"]["status"]="verified"
        else:report["canonical_output_sha256"]="0"*64
        (b/"completion-report.json").write_text(json.dumps(report));rehash_marker(b)
    with pytest.raises(ContractError):compare_saved_reviews(a,b)


def test_traversal_and_symlink_manifest_entries_refused(original_source,tmp_path):
    a,b=pair(original_source,tmp_path)
    marker=json.loads((b/"COMPLETED.json").read_bytes())
    marker["files"][0]["path"]="../outside.json"
    (b/"COMPLETED.json").write_text(json.dumps(marker))
    with pytest.raises(ContractError,match="unsafe manifest path"):compare_saved_reviews(a,b)
    marker["files"][0]["path"]="confidence-review.json"
    (b/"COMPLETED.json").write_text(json.dumps(marker))
    original=b/"confidence-review.json"
    original.rename(b/"retained-confidence.json")
    original.symlink_to(b/"retained-confidence.json")
    with pytest.raises(ContractError,match="symlink"):compare_saved_reviews(a,b)


def test_optional_ifc_deliveries_reload_before_comparison(original_source,tmp_path):
    paths=[]
    for label in ("a","b"):
        manifest=variant(original_source,tmp_path/("input-"+label))
        document=json.loads(manifest.read_bytes());document["include_ifc"]=True
        manifest.write_text(json.dumps(document))
        paths.append(persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id=label))
    result=compare_saved_reviews(*paths)
    assert not result.diff.changes and not result.diff.quantity_deltas


def test_rehashed_glb_tampering_still_refused(original_source,tmp_path):
    a,b=pair(original_source,tmp_path)
    path=b/"model.glb";payload=path.read_bytes()+b"bad";path.write_bytes(payload)
    report=json.loads((b/"completion-report.json").read_bytes())
    report["artifacts"]["glb"].update(sha256=hashlib.sha256(payload).hexdigest(),bytes=len(payload))
    (b/"completion-report.json").write_text(json.dumps(report));rehash_marker(b)
    with pytest.raises(ContractError):compare_saved_reviews(a,b)


def test_retained_base_fingerprints_refuse_rehashed_report_lineage_forgery(original_source,tmp_path):
    a,b=pair(original_source,tmp_path,extra_base=True)
    with pytest.raises(ContractError,match="different review base"):
        compare_saved_reviews(a,b)
    first=json.loads((a/"completion-report.json").read_bytes())
    second=json.loads((b/"completion-report.json").read_bytes())
    second["input_bindings"]["base_model_canonical_json_sha256"]=first["input_bindings"]["base_model_canonical_json_sha256"]
    (b/"completion-report.json").write_text(json.dumps(second));rehash_marker(b)
    before=snapshot(tmp_path)
    with pytest.raises(ContractError,match="retained placement evidence"):
        compare_saved_reviews(a,b)
    process=subprocess.run([sys.executable,"-m","oabm.pipeline.saved_revisions",str(a),str(b)],capture_output=True,text=True)
    assert process.returncode==1 and not process.stdout
    assert snapshot(tmp_path)==before


@pytest.mark.parametrize("change",["headers_missing","version","manifest_digest","scalar_missing","scalar_duplicate",
    "journal_duplicate","pdf_digest","image_digest","evidence_digest","pdf_missing","image_missing","bad_size","unknown_input"])
def test_complete_unique_source_bound_receipt_required(original_source,tmp_path,change):
    a,b=pair(original_source,tmp_path)
    receipt=json.loads((b/"input-receipt.json").read_bytes())
    if change=="headers_missing":receipt={"files":[r for r in receipt["files"] if r["input"].startswith("journals/")]}
    elif change=="version":receipt["version"]="unsupported"
    elif change=="manifest_digest":receipt["manifest_sha256"]="not-a-digest"
    elif change=="scalar_missing":receipt["files"]=[r for r in receipt["files"] if r["input"]!="model"]
    elif change in ("scalar_duplicate","journal_duplicate"):
        row=next(r for r in receipt["files"] if (r["input"]=="model" if change=="scalar_duplicate"
                 else r["input"].startswith("journals/")))
        receipt["files"].insert(0,dict(row))
    elif change in ("pdf_digest","image_digest","evidence_digest"):
        prefix={"pdf_digest":"documents/","image_digest":"images/","evidence_digest":"evidence_documents/"}[change]
        next(r for r in receipt["files"] if r["input"].startswith(prefix))["sha256"]="0"*64
    elif change in ("pdf_missing","image_missing"):
        prefix="documents/" if change=="pdf_missing" else "images/"
        receipt["files"]=[r for r in receipt["files"] if not r["input"].startswith(prefix)]
    elif change=="bad_size":receipt["files"][0]["bytes"]=True
    else:receipt["files"].append({"input":"unsupported/name","sha256":"0"*64,"bytes":1})
    (b/"input-receipt.json").write_text(json.dumps(receipt));rehash_marker(b)
    with pytest.raises(ContractError):compare_saved_reviews(a,b)


@pytest.mark.parametrize("kind",["fifo","empty_directory"])
def test_unlisted_non_payload_entries_refused(original_source,tmp_path,kind):
    import os
    a,b=pair(original_source,tmp_path)
    if kind=="fifo":os.mkfifo(b/"unlisted-pipe")
    else:(b/"unlisted-empty-directory").mkdir()
    with pytest.raises(ContractError):compare_saved_reviews(a,b)


@pytest.mark.parametrize('size',[0,128_000_001])
def test_cited_evidence_receipt_requires_original_supported_byte_range(original_source,tmp_path,size):
    a,b=pair(original_source,tmp_path)
    model=BuildingModel.from_json((b/'model.json').read_text())
    cited={e['document_sha256'] for f in model.attributes['placement_assertion_reviews'][-1]['review']['assertions'] for e in f['evidence']}
    receipt=json.loads((b/'input-receipt.json').read_bytes())
    row=next(r for r in receipt['files'] if r['input'].startswith('evidence_documents/') and r['input'].split('/',1)[1] in cited)
    row['bytes']=size
    (b/'input-receipt.json').write_text(json.dumps(receipt));rehash_marker(b)
    with pytest.raises(ContractError,match='cited placement evidence receipt'):
        compare_saved_reviews(a,b)
