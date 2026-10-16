"""Primary symbol-stage regression fixtures are independent synthetic PDFs."""
import io
import json
import os
from dataclasses import replace

import pytest
from PIL import Image
from oabm.model import ContractError
from oabm.proposals import Endpoint, RoutingConfig, AdapterAvailabilityError
from oabm.proposals.takeoff import render_source_image
from oabm.proposals.symbols import (
    VERSION,TASK,VALIDATOR,CATEGORIES,SymbolContext,SymbolRequest,SymbolMark,
    SymbolOutput,SymbolAdapterReply,SymbolRoutingPolicy,AttemptJournal,
    SymbolRun,SymbolBenchmarkCase,SymbolQualification,dispatch_symbols,
    measure_symbol_qualification,
)
from test_takeoff_proposals import fixture as lighting_fixture, source_pdf


def config():
    return RoutingConfig(version="1.0.0",**{
        role:Endpoint(adapter_id=role,provider_id="synthetic-provider",model_alias=role)
        for role in ("text_glm","text_codex","vision_luna","vision_sol","diagnostic_astra")})


def fixture():
    old,_,images,_=lighting_fixture()
    request=SymbolRequest(version=VERSION,task=TASK,validator_version=VALIDATOR,
        prompt_version="all-electrical-symbols-v1",question="Locate all supplied electrical symbol categories.",
        images=old.images,target_image_ids=("field",),
        contexts=(
            SymbolContext(role="legend",status="supplied",image_ids=("legend",),rationale="Full synthetic legend."),
            SymbolContext(role="schedule",status="supplied",image_ids=("schedule",),rationale="Full synthetic schedule."),
            SymbolContext(role="notes",status="inapplicable",image_ids=(),rationale="Synthetic source has no notes."),
        ),allowed_types=tuple(c+"-type" for c in CATEGORIES),confidence_threshold=.9)
    mark=SymbolMark(image_id="field",category="luminaire",type_proposal="luminaire-type",
        type_tag="F1",instance_label=None,bounds=(.1,.2,.3,.4),anchor=(.2,.3),polyline=(),
        confidence=.95,source_mark="Visible synthetic mark with F1 label.",
        evidence_image_ids=("field","legend","schedule"),unknowns=("Circuit and registered position unknown.",))
    output=SymbolOutput(version=VERSION,marks=(mark,),scanned_image_ids=("field",),
                        coverage_notes=("All target regions inspected.",))
    documents={request.images[0].source.document_sha256:source_pdf()}
    return request,images,documents,output


def policy(**kwargs):
    return SymbolRoutingPolicy(config=config(),expected_strong_model_id="strong-actual",
        expected_cheap_model_id="cheap-actual",expected_diagnostic_model_id="diagnostic-actual",**kwargs)


def adapters_for(output,calls=None):
    identities={"vision_sol":"strong-actual","vision_luna":"cheap-actual",
                "diagnostic_astra":"diagnostic-actual","strong_fallback":"fallback-actual"}
    def adapter(document,*,images,provider_id,model_alias,prompt_version):
        if calls is not None: calls.append(model_alias)
        assert provider_id == "synthetic-provider"
        assert "expected" not in document and "qualification" not in document
        assert set(images) == {i["id"] for i in document["images"]}
        return SymbolAdapterReply(output_json=output.to_json(),
            resolved_model_id=identities[model_alias],provider_request_id="request:"+model_alias)
    return {key:adapter for key in identities}


def run(tmp_path,*,request=None,output=None,routing=None,adapters=None,images=None,documents=None,qualification=None):
    req,ims,docs,raw=fixture()
    with AttemptJournal.create(tmp_path/"attempts.jsonl") as journal:
        result=dispatch_symbols(request or req,routing or policy(),
            images=ims if images is None else images,documents=docs if documents is None else documents,
            adapters=adapters or adapters_for(output or raw),journal=journal,qualification=qualification)
    return result


@pytest.mark.parametrize("category",CATEGORIES)
def test_primary_stage_supports_categories_without_recognizer_candidates(tmp_path,category):
    request,images,documents,output=fixture()
    linear=category == "linear_symbol"
    mark=replace(output.marks[0],category=category,
        type_proposal=None if category=="unknown" else category+"-type",
        anchor=None if linear else (.2,.3),
        polyline=((.1,.2),(.2,.3),(.3,.4)) if linear else ())
    calls=[]
    result=run(tmp_path,output=replace(output,marks=(mark,)),
               adapters=adapters_for(replace(output,marks=(mark,)),calls))
    assert calls == ["vision_sol"]
    assert result.status == "completed"
    assert SymbolRun.from_json(result.to_json()) == result
    proposals=result.proposals(images=images,documents=documents)
    assert len(proposals)==1
    p=proposals[0]
    assert p.category == category and p.geometry_kind == ("linear" if linear else "point")
    assert p.bounds_pt == pytest.approx((60,58,80,74))
    assert (p.anchor_pt is None) == linear
    assert p.polyline_pt == ((60,74),(70,66),(80,58)) if linear else p.polyline_pt == ()
    assert p.accepted is False and p.review_state == "pending"
    assert p.provenance[0].derivation == "inferred"
    assert p.provenance[0].attributes["transport_reported_model_id"] == "strong-actual"
    assert p.fingerprint
    assert AttemptJournal.read(tmp_path/"attempts.jsonl") == result.attempts


def test_missing_context_is_preserved_and_not_declared_complete(tmp_path):
    request,images,documents,output=fixture()
    images={key:value for key,value in images.items() if key!="schedule"}
    request=replace(request,images=tuple(im for im in request.images if im.id!="schedule"),
        contexts=tuple(replace(c,status="missing",image_ids=(),rationale="Schedule source not supplied.")
                       if c.role=="schedule" else c for c in request.contexts))
    output=replace(output,marks=(replace(output.marks[0],evidence_image_ids=("field","legend")),))
    result=run(tmp_path,request=request,images=images,output=output)
    assert any(s.startswith("missing_schedule:") for s in result.proposals(images=images,documents=documents)[0].unknowns)


@pytest.mark.parametrize("change",[
    {"anchor":None,"polyline":()},
    {"polyline":((.1,.2),(.2,.3))},
    {"anchor":(.9,.9)},
    {"category":"switch","anchor":None,"polyline":((.1,.2),(.3,.4))},
    {"category":"linear_symbol"},
    {"anchor":None,"polyline":((.1,.2),(.1,.2)),"category":"linear_symbol"},
    {"confidence":float("nan")},
    {"confidence":True},
])
def test_point_linear_and_confidence_contracts_reject_invention(change):
    _,_,_,output=fixture()
    with pytest.raises(ContractError):
        replace(output.marks[0],**change)


@pytest.mark.parametrize("extra",[
    {"circuits":[{"number":1}]},{"accepted":True},{"model_alias":"spoofed"},{"canonical_devices":[{}]},
])
def test_raw_invalid_output_is_preserved_with_no_proposals(tmp_path,extra):
    _,images,documents,output=fixture()
    data=output.to_dict();data.update(extra)
    raw=json.dumps(data)
    def adapter(*args,**kwargs):return SymbolAdapterReply(output_json=raw,resolved_model_id="strong-actual")
    result=run(tmp_path,adapters={"vision_sol":adapter})
    assert result.status == "failed"
    assert result.attempts[-1].status == "invalid_output"
    assert result.attempts[-1].raw_output == raw
    assert result.proposals(images=images,documents=documents) == ()


def test_start_is_durable_before_call_and_identity_mismatch_is_rejected(tmp_path):
    _,_,_,output=fixture()
    def adapter(*args,**kwargs):
        receipts=AttemptJournal.read(tmp_path/"attempts.jsonl")
        assert len(receipts)==1 and receipts[0].status=="started"
        return SymbolAdapterReply(output_json=output.to_json(),resolved_model_id="wrong-model")
    result=run(tmp_path,adapters={"vision_sol":adapter})
    assert result.status == "failed" and result.attempts[-1].status=="identity_mismatch"
    assert result.attempts[-1].resolved_model_id=="wrong-model"


def test_unknown_resolved_identity_is_explicit_not_invented(tmp_path):
    _,images,documents,output=fixture()
    routing=SymbolRoutingPolicy(config=config())
    result=run(tmp_path,routing=routing,adapters={
        "vision_sol":lambda *a,**kw:SymbolAdapterReply(output_json=output.to_json())})
    proposal,=result.proposals(images=images,documents=documents)
    assert "resolved_model_identity_unavailable" in proposal.unknowns
    assert proposal.provenance[0].attributes["transport_reported_model_id"] is None


def test_explicit_strong_availability_fallback_and_no_diagnostic_retry(tmp_path):
    _,_,_,output=fixture()
    fallback=Endpoint(adapter_id="strong_fallback",provider_id="synthetic-provider",model_alias="strong_fallback")
    routing=policy(strong_availability_fallback=fallback,expected_fallback_model_id="fallback-actual")
    calls=[]
    def unavailable(*args,**kwargs):
        calls.append(kwargs["model_alias"]);raise AdapterAvailabilityError("rate_limited")
    adapters=adapters_for(output,calls);adapters["vision_sol"]=unavailable
    result=run(tmp_path,routing=routing,adapters=adapters)
    assert calls == ["vision_sol","strong_fallback"]
    assert result.status=="completed" and len(result.attempts)==4


def test_invalid_strong_output_never_escalates_or_uses_cheaper(tmp_path):
    calls=[]
    def invalid(*a,**kw):
        calls.append(kw["model_alias"]);return SymbolAdapterReply(output_json="{}",resolved_model_id="strong-actual")
    result=run(tmp_path,adapters={"vision_sol":invalid})
    assert result.status=="failed" and calls==["vision_sol"]


def test_diagnostic_is_explicit_and_yields_no_integration_proposals(tmp_path):
    _,images,documents,output=fixture()
    calls=[]
    result=run(tmp_path,routing=policy(mode="diagnostic"),adapters=adapters_for(output,calls))
    assert calls==["diagnostic_astra"] and result.status=="diagnostic"
    assert result.proposals(images=images,documents=documents)==()


def qualification(tmp_path,*,bad_cheap=False):
    request,images,documents,output=fixture()
    adapters=adapters_for(output)
    if bad_cheap:
        wrong=replace(output,marks=(replace(output.marks[0],category="fan",type_proposal="fan-type"),))
        adapters["vision_luna"]=lambda *a,**kw:SymbolAdapterReply(
            output_json=wrong.to_json(),resolved_model_id="cheap-actual")
    with AttemptJournal.create(tmp_path/"calibration.jsonl") as journal:
        return measure_symbol_qualification(
            (SymbolBenchmarkCase(id="synthetic-one",request=request,expected=output),),
            config=config(),expected_strong_model_id="strong-actual",expected_cheap_model_id="cheap-actual",
            adapters=adapters,images_by_case={"synthetic-one":images},
            documents_by_case={"synthetic-one":documents},journal=journal)


def test_cheaper_requires_passing_measured_task_evidence(tmp_path):
    q=qualification(tmp_path)
    assert SymbolQualification.from_json(q.to_json())==q
    request,images,documents,output=fixture()
    calls=[]
    result=run(tmp_path,routing=policy(mode="qualified_cheaper"),qualification=q,adapters=adapters_for(output,calls))
    assert calls==["vision_luna"] and result.status=="completed"
    q.validate_for(request,policy(mode="qualified_cheaper"))


def test_missing_failing_stale_or_tampered_qualification_refuses_dispatch(tmp_path):
    req,images,documents,output=fixture()
    q=qualification(tmp_path,bad_cheap=True)
    calls=[]
    for name,evidence,request in [
        ("missing",None,req),("failing",q,req),
    ]:
        with AttemptJournal.create(tmp_path/(name+".jsonl")) as journal:
            with pytest.raises(ContractError):
                dispatch_symbols(request,policy(mode="qualified_cheaper"),
                    qualification=evidence,adapters=adapters_for(output,calls),
                    images=images,documents=documents,journal=journal)
    assert not calls
    data=q.to_dict();data["cases"][0]["expected"]["marks"][0]["category"]="fan"
    with pytest.raises(ContractError):SymbolQualification.from_dict(data)


@pytest.mark.parametrize("failure",["invalid_output","low_confidence","unavailable"])
def test_qualified_cheaper_escalates_to_strong_with_raw_receipts(tmp_path,failure):
    q=qualification(tmp_path)
    _,_,_,output=fixture();calls=[]
    adapters=adapters_for(output,calls)
    def cheap(*a,**kw):
        calls.append("vision_luna")
        if failure=="unavailable":raise AdapterAvailabilityError("unavailable")
        raw="{}" if failure=="invalid_output" else replace(output,marks=(replace(output.marks[0],confidence=.2),)).to_json()
        return SymbolAdapterReply(output_json=raw,resolved_model_id="cheap-actual")
    adapters["vision_luna"]=cheap
    result=run(tmp_path,routing=policy(mode="qualified_cheaper"),qualification=q,adapters=adapters)
    assert calls==["vision_luna","vision_sol"]
    assert len(result.attempts)==4 and result.status=="completed"
    assert result.attempts[1].status != "completed"
    assert result.attempts[-1].raw_output==output.to_json()


def test_journal_is_exclusive_chain_checked_and_no_call_after_start_fsync_failure(tmp_path,monkeypatch):
    request,images,documents,output=fixture()
    path=tmp_path/"durable.jsonl";calls=[]
    with AttemptJournal.create(path) as journal:
        def fail(_):raise OSError("simulated disk failure")
        monkeypatch.setattr(os,"fsync",fail)
        with pytest.raises(OSError):
            dispatch_symbols(request,policy(),images=images,documents=documents,
                journal=journal,adapters=adapters_for(output,calls))
    assert not calls
    original=path.read_bytes()
    with pytest.raises(FileExistsError):AttemptJournal.create(path)
    assert path.read_bytes()==original


def second_target(request,*,page=2):
    return render_source_image(source_pdf(),source_id=request.images[0].source.source_id,
        page=page,scale=2,crop_px=(80,20,280,180),image_id="overlap",role="target")


@pytest.mark.parametrize("conflict",[False,True])
def test_cross_tile_candidates_are_preserved_with_stable_source_ids(tmp_path,conflict):
    request,images,documents,output=fixture()
    ref,payload=second_target(request)
    request=replace(request,images=(*request.images,ref),target_image_ids=("field","overlap"))
    a=output.marks[0]
    b=replace(a,image_id="overlap",bounds=(.2,.2,.4,.4),anchor=(.3,.3),
        category="switch" if conflict else a.category,
        type_proposal="switch-type" if conflict else a.type_proposal,
        evidence_image_ids=("overlap","legend"))
    output=replace(output,marks=(a,b),scanned_image_ids=("field","overlap"))
    images={**images,"overlap":payload}
    result=run(tmp_path,request=request,images=images,output=output)
    proposals=result.proposals(images=images,documents=documents)
    assert len(proposals)==2 and len({p.proposal_id for p in proposals})==2
    assert len({p.source_mark_id for p in proposals})==1
    candidates=result.duplicate_candidates(images=images,documents=documents)
    assert len(candidates)==1 and candidates[0].review_state=="pending"
    assert candidates[0].reason == ("same_page_classification_conflict" if conflict else "same_page_overlap")
    assert proposals == result.proposals(images=images,documents=documents)
    assert all(p.image_id in ("field","overlap") for p in proposals)


@pytest.mark.parametrize("instance_label",[None,"PANEL-A"])
def test_cross_page_repeated_type_tags_never_silently_merge(tmp_path,instance_label):
    request,images,documents,output=fixture()
    ref,payload=second_target(request,page=1)
    request=replace(request,images=(*request.images,ref),target_image_ids=("field","overlap"))
    a=replace(output.marks[0],instance_label=instance_label)
    b=replace(a,image_id="overlap",bounds=(.2,.2,.4,.4),anchor=(.3,.3),
              evidence_image_ids=("overlap","legend"))
    output=replace(output,marks=(a,b),scanned_image_ids=("field","overlap"))
    images={**images,"overlap":payload}
    result=run(tmp_path,request=request,images=images,output=output)
    proposals=result.proposals(images=images,documents=documents)
    assert len(proposals)==2 and {p.page for p in proposals}=={1,2}
    candidates=result.duplicate_candidates(images=images,documents=documents)
    assert len(candidates)==(0 if instance_label is None else 1)
    if candidates:assert candidates[0].reason=="cross_page_instance_label"


def test_linear_luminaire_retains_source_polyline_and_no_point_anchor(tmp_path):
    _,images,documents,output=fixture()
    mark=replace(output.marks[0],anchor=None,polyline=((.1,.2),(.2,.3),(.3,.4)))
    result=run(tmp_path,output=replace(output,marks=(mark,)))
    p,=result.proposals(images=images,documents=documents)
    assert p.category=="luminaire" and p.geometry_kind=="linear"
    assert p.anchor_pt is None and len(p.polyline_pt)==3
    assert p.to_dict().get("circuits") is None


def test_missing_tile_coverage_and_non_target_evidence_are_rejected(tmp_path):
    req,images,documents,output=fixture()
    ref,payload=second_target(req)
    req=replace(req,images=(*req.images,ref),target_image_ids=("field","overlap"))
    result=run(tmp_path,request=req,images={**images,"overlap":payload},output=output)
    assert result.status=="failed"
    assert result.attempts[-1].status=="invalid_output"
    bad=replace(output,marks=(replace(output.marks[0],image_id="legend"),))
    other=tmp_path/"other";other.mkdir()
    result=run(other,output=bad)
    assert result.status=="failed"


def test_saved_run_pixel_revalidation_and_current_request_binding(tmp_path):
    req,images,documents,_=fixture()
    result=run(tmp_path)
    altered={**images,"field":images["field"]+b"changed"}
    with pytest.raises(ContractError):
        result.proposals(images=altered,documents=documents)
    with pytest.raises(ContractError,match="stale"):
        result.validate_against(replace(req,question="Different framing."),policy(),
                                images=images,documents=documents)
    assert result.validate_against(req,policy(),images=images,documents=documents)==result
    proposals=result.proposals(images=images,documents=documents)
    proposals[0].provenance[0].attributes.clear()
    assert result.proposals(images=images,documents=documents)[0].provenance[0].attributes


def test_start_and_raw_invalid_terminal_receipts_are_chain_verified(tmp_path):
    result=run(tmp_path)
    path=tmp_path/"attempts.jsonl"
    data=path.read_text()
    tampered=data.replace("strong_baseline:primary","altered_baseline",1)
    copy=tmp_path/"tampered.jsonl";copy.write_text(tampered)
    with pytest.raises(ContractError,match="chain/content"):
        AttemptJournal.read(copy)
    copy.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ContractError,match="incomplete"):
        AttemptJournal.read(copy)


def test_stale_qualification_does_not_reuse_cheap_model(tmp_path):
    q=qualification(tmp_path)
    req,images,documents,output=fixture();calls=[]
    with AttemptJournal.create(tmp_path/"stale.jsonl") as journal:
        with pytest.raises(ContractError):
            dispatch_symbols(replace(req,prompt_version="changed"),policy(mode="qualified_cheaper"),
                qualification=q,journal=journal,images=images,documents=documents,
                adapters=adapters_for(output,calls))
    assert not calls


def test_adapter_mutation_cannot_change_request_or_original_images(tmp_path):
    req,images,documents,output=fixture()
    before=req.to_json()
    def adapter(document,*,images,**kwargs):
        document["target_image_ids"].clear();images.clear()
        return SymbolAdapterReply(output_json=output.to_json(),resolved_model_id="strong-actual")
    result=run(tmp_path,request=req,images=images,adapters={"vision_sol":adapter})
    assert req.to_json()==before and len(images)==3
    assert result.request.to_json()==before
    assert result.status=="completed"


@pytest.mark.parametrize("payload", [b"\xff\n", b"[]\n", b"null\n", b"3\n"])
def test_journal_rejects_invalid_encoding_or_non_object_record(tmp_path,payload):
    path=tmp_path/"malformed.jsonl";path.write_bytes(payload)
    with pytest.raises(ContractError):
        AttemptJournal.read(path)


@pytest.mark.parametrize("ids", [("one",), ("one","one"), ("two","one")])
def test_duplicate_candidate_requires_two_distinct_ordered_proposals(ids):
    from oabm.proposals.symbols import DuplicateCandidate
    with pytest.raises(ContractError):
        DuplicateCandidate(id="candidate",proposal_ids=ids,reason="same_page_overlap")
