"""Combined drawing/circuit persistence, retained replay and job compatibility."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from oabm.model import BuildingModel, ContractError
from oabm.pipeline import saved_electrical as saved
from oabm.pipeline.saved_revisions import _load, compare_saved_reviews
from oabm.pipeline.reviewed_electrical import deliver_reviewed_electrical, KINDS
from test_reviewed_circuit_design import case, save_inputs
from test_review_electrical_plans import config
from test_saved_job import setup, run, approve, revise, tree_bytes


def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(path.read_bytes())
def write(path,value):path.write_text(json.dumps(value,sort_keys=True))


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root=tmp_path_factory.mktemp("drawing-circuit-source")
    return save_inputs(root/"inputs",case(root/"source"))


def combined(source,path,cfg):
    doc=read(source)
    # Keep the existing reader's relative references tied to their original source.
    for key in ("model","review","runs","decisions","registrations","assertion_review","scope_policy","review_config","circuit_design"):
        if doc.get(key) is not None:
            p=Path(doc[key]["path"]);doc[key]["path"]=str(p if p.is_absolute() else source.parent/p)
    for key in ("documents","images","evidence_documents","journals"):
        for ref in doc[key].values():
            p=Path(ref["path"]);ref["path"]=str(p if p.is_absolute() else source.parent/p)
    doc["version"]=saved.CIRCUIT_DRAWING_VERSION
    doc["review_drawings"]=None
    if cfg is not None:
        p=path.parent/"drawing-config.json";p.write_bytes(json.dumps(cfg.to_dict(),sort_keys=True).encode())
        doc["review_drawings"]={"path":str(p),"sha256":sha(p.read_bytes())}
    write(path,doc);return path


def drawing_config(**changes):
    return config(bounds_m=(0.,0.,20.,15.),show_routes=True,**changes)


def test_combined_published_snapshot_has_circuit_and_drawing_replay(source,tmp_path):
    manifest=combined(source,tmp_path/"inputs.json",drawing_config())
    a=saved.persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="a")
    model,report,_,metadata=_load(a)
    assert len(model.circuits)==2 and len(model.routes)==2
    assert report["stages"]["circuit_design"]["status"]==report["stages"]["review_drawings"]["status"]=="passed"
    assert report["review_drawings"]["canonical_model_sha256"]==report["canonical_output_sha256"]
    shown=report["review_drawings"]["views"][0]["rendered_electrical_ids"]
    assert {r.id for r in model.routes}<=set(shown)
    assert len(report["review_drawings"]["source_proposals_not_admitted"])==1
    assert (a/"circuit-inputs/request.json").is_file() and (a/"drawing-inputs/config.json").is_file()
    assert metadata["circuit_design_lineage"] and metadata["review_drawings_config_fingerprint"]
    canonical=(a/"model.json").read_bytes()
    combined(source,manifest,drawing_config(cut_height_m=1.))
    b=saved.persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="b")
    assert canonical==(b/"model.json").read_bytes()
    comparison=compare_saved_reviews(a,b)
    assert not comparison.diff.changes and not comparison.diff.quantity_deltas
    assert comparison.context["base"]["review_drawings_config_fingerprint"]!=comparison.context["target"]["review_drawings_config_fingerprint"]


def test_null_drawing_configuration_keeps_existing_circuit_payloads(source,tmp_path):
    def delivery(manifest,label):
        inputs=saved.load_saved_electrical_inputs(manifest,journal_directory=tmp_path/label)
        refs={k:"same-refs/"+k for k in (*KINDS,"ifc","circuit_design")}
        return deliver_reviewed_electrical(inputs.model,inputs.review,**inputs.kwargs,artifact_references=refs)
    legacy=delivery(source,"legacy-journals")
    disabled=delivery(combined(source,tmp_path/"disabled.json",None),"disabled-journals")
    assert legacy.model_json==disabled.model_json
    assert legacy.completion_json==disabled.completion_json
    assert legacy.artifacts==disabled.artifacts
    assert "review_drawings" not in disabled.completion


def test_late_svg_failure_keeps_partial_circuit_attempt_and_fresh_retry(source,tmp_path,monkeypatch):
    manifest=combined(source,tmp_path/"inputs.json",drawing_config())
    original=saved._write
    def fail(path,data):
        if path.suffix==".svg":raise OSError("Synthetic SVG write failure")
        return original(path,data)
    with monkeypatch.context() as patch:
        patch.setattr(saved,"_write",fail)
        with pytest.raises(saved.SavedDeliveryError):
            saved.persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="failed")
    old=tmp_path/"out/failed";frozen=tree_bytes(old)
    assert (old/".pending/circuit-design.json").exists() and not (old/"delivery").exists()
    completed=saved.persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="fresh")
    _load(completed)
    assert tree_bytes(old)==frozen


def rehash(final):
    marker=read(final/"COMPLETED.json")
    for row in marker["files"]:
        data=(final/row["path"]).read_bytes();row.update(sha256=sha(data),bytes=len(data))
    write(final/"COMPLETED.json",marker)


@pytest.mark.parametrize("fault",["svg","config","qualified_source"])
def test_rehashed_drawing_tampering_cannot_replace_canonical_replay(source,tmp_path,fault):
    final=saved.persist_saved_electrical(combined(source,tmp_path/"inputs.json",drawing_config()),
        output_root=tmp_path/"out",run_id="a")
    report=read(final/"completion-report.json")
    if fault=="config":
        p=final/"drawing-inputs/config.json";doc=read(p);doc["plans"][0]["cut_height_m"]=1.
        write(p,doc);receipt=read(final/"input-receipt.json")
        row=next(r for r in receipt["files"] if r["input"]=="review_drawings")
        row.update(sha256=sha(p.read_bytes()),bytes=len(p.read_bytes()))
        write(final/"input-receipt.json",receipt)
        from oabm.pipeline.reviewed_electrical import _json
        report["input_bindings"]["review_drawings_fingerprint"]=sha(_json(doc).encode())
    else:
        kind="review_plan:review:floor" if fault=="svg" else "drawing_set"
        p=Path(report["artifacts"][kind]["reference"])
        if fault=="svg":p.write_bytes(p.read_bytes()+b"<!-- synthetic stale drawing -->")
        else:
            doc=read(p);doc["drawing_set"]["source_index"][0]["provenance"]=[]
            write(p,doc)
        report["artifacts"][kind].update(sha256=sha(p.read_bytes()),bytes=len(p.read_bytes()))
    write(final/"completion-report.json",report);rehash(final)
    with pytest.raises(ContractError,match="drawing/model replay mismatch"):_load(final)


@pytest.mark.parametrize("enabled",[True,False])
def test_persisted_job_preserves_drawing_configuration_and_provider_reuse(tmp_path,enabled):
    path,source,calls,adapter=setup(tmp_path)
    upstream=Path(read(path)["upstream"]["path"]);doc=read(upstream)
    doc["version"]=saved.DRAWING_VERSION;doc.pop("circuit_design")
    doc["review_drawings"]=None
    if enabled:
        p=upstream.parent/"job-drawing-config.json";write(p,drawing_config().to_dict())
        doc["review_drawings"]={"path":str(p),"sha256":sha(p.read_bytes())}
    write(upstream,doc);job=read(path);job["upstream"]["sha256"]=sha(upstream.read_bytes());write(path,job)
    pending=run(path,adapter);approve(path,source,pending)
    first=run(path,adapter);again=run(path,adapter)
    assert first["status"]==again["status"]=="delivery_persisted" and len(calls)==1
    assert again["proposal_reused"] and again["export_reused"]
    final=Path(first["delivery"]);model,report,_,_=_load(final)
    assert bool(report.get("review_drawings"))==enabled
    assert len(model.circuits)==2
    if enabled:
        frozen=tree_bytes(final)
        cfg=read(p);cfg["plans"][0]["cut_height_m"]=1.;write(p,cfg)
        doc["review_drawings"]["sha256"]=sha(p.read_bytes());write(upstream,doc)
        revise(path,lambda d:d["upstream"].update(sha256=sha(upstream.read_bytes())))
        changed=run(path,adapter)
        assert changed["status"]=="delivery_persisted" and changed["proposal_reused"] and len(calls)==1
        assert changed["export_attempt_id"]!=first["export_attempt_id"]
        assert (Path(changed["delivery"])/"model.json").read_bytes()==(final/"model.json").read_bytes()
        assert tree_bytes(final)==frozen
