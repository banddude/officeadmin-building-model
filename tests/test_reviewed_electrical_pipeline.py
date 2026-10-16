"""Generated source and saved stub runs through the real delivery consumers."""
import hashlib
import json
from dataclasses import replace

import pytest

from oabm.model import Provenance, RuleReference, ContractError
from oabm.proposals.contracts import digest
from oabm.qa.confidence_review import ReviewConfig
from oabm.reports.scope_ledger import ScopePolicy
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.pipeline.reviewed_electrical import (
    KINDS, deliver_reviewed_electrical, ReviewedElectricalDeliveryError)
from test_placement_assertions import setup as placement_setup, duplicate_case


def inputs(tmp_path, *, facts_change=None, include_ifc=False, duplicate=False):
    model,review,args,facts=(duplicate_case(tmp_path) if duplicate else placement_setup(tmp_path))
    model=replace(model,name="Synthetic reviewed electrical delivery")
    if facts_change:facts=facts_change(facts)
    assertion=PlacementAssertionReview.capture(model,review,decisions=args["decisions"],
        registrations=args["registrations"],canonical_datum_id="local-project",
        reviewer_id="delivery-reviewer",reviewer_derivation="user",
        rationale="Explicit synthetic project design review.",assertions=facts)
    registry={(p["source_kind"],p["source_id"]):"evidence/"+p["source_kind"]
              for row in review.proposals for p in row["provenance"]}
    for reg in args["registrations"]:
        registry.update({(p.source_kind,p.source_id):"evidence/"+p.source_kind for p in reg.provenance})
    registry.update({("symbol-review","synthetic-reviewer"):"evidence/source-review",
                     ("placement-assertion-review","delivery-reviewer"):"evidence/assertion-review"})
    refs={k:"synthetic/"+k+(".glb" if k=="glb" else ".json") for k in KINDS}
    if include_ifc:refs["ifc"]="synthetic/model.ifc"
    policy=ScopePolicy(version="1.0.0",mode="preserve_unresolved",
        basis=RuleReference(authority="synthetic caller",revision="r1",rule_id="scope.preserve"),
        actor="synthetic-reviewer",provenance=(Provenance(source_kind="project-policy",
            source_id="synthetic:policy",derivation="user",confidence=.8),))
    call={**args,"delivery_id":"synthetic-delivery","assertion_review":assertion,
        "scope_policy":policy,"proposal_groups":{"unresolved-inventory":tuple(p["proposal_id"] for p in review.proposals)},
        "canonical_groups":{},"review_config":ReviewConfig(confidence_threshold=.9),
        "source_references":registry,"artifact_references":refs,"glb_options":{},
        "include_ifc":include_ifc,"coverage_report":None}
    return model,review,call


def run(case,**changes):
    model,review,args=case
    return deliver_reviewed_electrical(model,review,**{**args,**changes})


def test_same_snapshot_drives_quantity_review_and_export_artifact_bindings(tmp_path):
    case=inputs(tmp_path)
    result=run(case)
    report=result.completion
    assert report["delivery_ready"] and set(result.artifacts)==set(KINDS)
    for kind,payload in result.artifacts.items():
        assert report["artifacts"][kind]["sha256"]==hashlib.sha256(payload).hexdigest()
        assert report["artifacts"][kind]["bytes"]==len(payload)
    bundle=json.loads(result.artifacts["review_bundle"])
    joins={"canonical_model":"canonical_model","glb":"glb","confidence_review":"confidence_review",
           "source_reference_mapping":"source_map"}
    for key,artifact in joins.items():
        assert bundle["artifacts"][key]["sha256"]==report["artifacts"][artifact]["sha256"]
        assert bundle["artifacts"][key]["reference"]==report["artifacts"][artifact]["reference"]
    quantity=json.loads(result.artifacts["quantities"])
    item,=quantity["items"]
    assert (item["quantity"],item["unit"],item["group"])==(1.0,"ea","unresolved-inventory")
    entity,=result.model.electrical_devices
    assert item["source_entity_ids"]==[entity.id]
    assert report["canonical_output_sha256"]==report["artifacts"]["canonical_model"]["sha256"]
    assert report["source_coverage"]["status"]=="unavailable"
    assert report["counts"]["dispositions"]=={"accept":1}
    assert report["quantity_eligibility"]["scope_counts"]=={"unresolved":1}
    assert report["quantity_eligibility"]["base_bid"]=="not_established"
    assert not any(report["claims"].values())
    assert report["stages"]["ifc"]["status"]=="not_requested"


def test_explicit_design_roles_and_uncertainty_survive_ifc_roundtrip(tmp_path):
    from oabm.ifc.adapter import from_ifc
    case=inputs(tmp_path,include_ifc=True)
    result=run(case)
    path=tmp_path/"synthetic.ifc"
    path.write_bytes(result.artifacts["ifc"])
    restored=from_ifc(path)
    entity,=restored.electrical_devices
    roles={p.attributes["assertion"]["field"]:p.attributes["role"] for p in entity.provenance
           if p.source_kind=="placement-assertion-review"}
    assert roles["elevation"]=="designed" and roles["level_elevation"]=="project_defined"
    assert entity.provenance[0].derivation=="inferred"
    assert entity.pose.position.z==pytest.approx(4.2) and entity.confidence==.6
    assert restored.attributes==result.model.attributes
    assert result.completion["physical_completeness"]["missing_dimensions"]==[entity.id]
    assert result.completion["physical_completeness"]["status"]=="not_verified"


def test_repeated_delivery_is_idempotent_and_immutable_for_same_saved_inputs(tmp_path):
    case=inputs(tmp_path,include_ifc=True)
    before=case[0].to_json()
    first=run(case);second=run(case)
    assert first.model_json==second.model_json and first.completion_json==second.completion_json
    assert first.artifacts==second.artifacts
    assert case[0].to_json()==before
    with pytest.raises(TypeError): first.artifacts["glb"]=b"altered"
    first.completion["claims"]["production_live"]=True
    assert not first.completion["claims"]["production_live"]
    detached=first.model;detached.attributes["caller-change"]=True
    assert "caller-change" not in first.model.attributes


def test_output_snapshot_is_not_reused_under_a_stale_base_model_review(tmp_path):
    case=inputs(tmp_path)
    result=run(case)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        deliver_reviewed_electrical(result.model,case[1],**case[2])
    assert error.value.stage=="placement_intake" and isinstance(error.value.__cause__,ContractError)
    assert not error.value.completion["delivery_ready"]


def test_missing_height_returns_artifacts_without_claiming_input_completeness(tmp_path):
    case=inputs(tmp_path,facts_change=lambda facts:tuple(f for f in facts if f.field!="elevation"))
    result=run(case)
    assert not result.model.electrical_devices
    report=result.completion
    assert report["delivery_ready"] and report["counts"]["dispositions"]=={"defer":1}
    assert report["counts"]["quantity_lines"]==0
    assert "missing:elevation" in report["placement_assertion_results"][0]["unresolved"]
    assert len(report["quantity_eligibility"]["group_requests_without_canonical_objects"])==1
    assert not any(report["claims"].values())


def test_conflicting_height_is_visible_and_duplicate_group_can_defer(tmp_path):
    def conflict(facts):
        height=next(f for f in facts if f.field=="elevation")
        return (*facts,replace(height,assertion_id="conflicting-height",value=2.))
    result=run(inputs(tmp_path,facts_change=conflict,duplicate=True))
    assert not result.model.electrical_devices
    assert result.completion["counts"]["dispositions"]=={"defer":2}
    assert any("conflict:elevation" in r["unresolved"] for r in result.completion["placement_assertion_results"])


def test_duplicate_proposals_resolve_to_one_canonical_inventory_member(tmp_path):
    result=run(inputs(tmp_path,duplicate=True))
    item,=json.loads(result.artifacts["quantities"])["items"]
    assert item["quantity"]==1 and len(item["source_entity_ids"])==1
    assert result.completion["counts"]["source_proposals"]==2


def test_duplicate_proposals_cannot_assign_survivor_to_conflicting_groups(tmp_path):
    case=inputs(tmp_path,duplicate=True)
    first,second=case[1].proposals
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        run(case,proposal_groups={"group-a":[first["proposal_id"]],"group-b":[second["proposal_id"]]})
    assert error.value.stage=="quantities" and not error.value.completion["delivery_ready"]


@pytest.mark.parametrize("kind",["coverage","missing_refs","duplicate_refs","unknown_group","ungrouped","ifc_name","source_ref_collision"])
def test_incomplete_or_unsupported_inputs_never_return_success(tmp_path,kind):
    case=inputs(tmp_path,include_ifc=kind=="ifc_name")
    changes={}
    if kind=="coverage":changes["coverage_report"]={"accepted":True}
    elif kind=="missing_refs":changes["artifact_references"]={}
    elif kind=="duplicate_refs":
        changes["artifact_references"]={k:"same" for k in case[2]["artifact_references"]}
    elif kind=="unknown_group":changes["proposal_groups"]={"inventory":["absent"]}
    elif kind=="ungrouped":changes["proposal_groups"]={}
    elif kind=="ifc_name":case=(replace(case[0],name=None),case[1],case[2])
    else:changes["source_references"]={("pdf","source"):case[2]["artifact_references"]["canonical_model"]}
    with pytest.raises(ReviewedElectricalDeliveryError) as error:run(case,**changes)
    assert not error.value.completion["delivery_ready"]
    assert error.value.completion["stages"][error.value.stage]["status"]=="failed"
    assert not any(error.value.completion["claims"].values())


def test_stale_review_is_rebuilt_and_refused_before_intake(tmp_path):
    model,review,args=inputs(tmp_path)
    stale=replace(review,proposals=({**review.proposals[0],"confidence":.99},))
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        deliver_reviewed_electrical(model,stale,**args)
    assert error.value.stage=="source_review" and isinstance(error.value.__cause__,ContractError)
    assert not error.value.completion["artifacts"]


def test_source_bytes_and_design_evidence_are_still_checked_by_existing_apis(tmp_path):
    case=inputs(tmp_path)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        run(case,evidence_documents={key:b"changed" for key in case[2]["evidence_documents"]})
    assert error.value.stage=="placement_intake"
    assert "evidence" in str(error.value.__cause__)


def test_unused_source_registry_entries_are_disclosed_but_not_emitted_as_fake_bundle_refs(tmp_path):
    case=inputs(tmp_path)
    registry={**case[2]["source_references"],("unused","source"):"unused-reference"}
    result=run(case,source_references=registry)
    assert result.completion["unused_source_reference_keys"]==[["unused","source"]]
    assert "unused-reference" not in result.artifacts["source_map"].decode()


def test_missing_required_source_reference_cannot_produce_verified_bundle(tmp_path):
    case=inputs(tmp_path)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:run(case,source_references={})
    assert error.value.stage=="confidence_review"
    assert "review_bundle" not in error.value.completion["artifacts"]


def test_exporter_failure_has_truthful_partial_stage_record(tmp_path,monkeypatch):
    import oabm.pipeline.reviewed_electrical as pipeline
    case=inputs(tmp_path)
    def fail(*args,**kwargs):raise RuntimeError("synthetic exporter refusal")
    monkeypatch.setattr(pipeline,"build_glb_bytes",fail)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:run(case)
    report=error.value.completion
    assert error.value.stage=="glb" and report["stages"]["quantities"]["status"]=="passed"
    assert report["stages"]["review_bundle"]["status"]=="not_run"
    assert "glb" not in report["artifacts"] and not report["delivery_ready"]
    assert isinstance(error.value.__cause__,RuntimeError)


def test_missing_optional_ifc_dependency_is_an_error_not_an_empty_export(tmp_path,monkeypatch):
    import builtins
    case=inputs(tmp_path,include_ifc=True)
    original=builtins.__import__
    def no_ifc(name,*args,**kwargs):
        if name=="oabm.ifc.adapter":raise ModuleNotFoundError("synthetic optional IFC dependency absent")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",no_ifc)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:run(case)
    report=error.value.completion
    assert error.value.stage=="ifc" and report["stages"]["review_bundle"]["status"]=="passed"
    assert "ifc" not in report["artifacts"] and not report["delivery_ready"]


@pytest.mark.parametrize("legacy_attributes",[{}, {"assertion":None}])
def test_existing_canonical_inventory_groups_and_unknown_legacy_roles_are_preserved(tmp_path,legacy_attributes):
    from oabm.model import ElectricalDevice,Pose,Point3
    model,review,args=inputs(tmp_path)
    existing=ElectricalDevice(id="existing-device",device_type="receptacle",level_id="floor",
        pose=Pose(position=Point3(x=1,y=1,z=3.5)),confidence=.4,
        provenance=(Provenance(source_kind="placement-assertion-review",source_id="legacy-record",
                              derivation="inferred",confidence=.4,attributes=legacy_attributes),))
    model=replace(model,electrical_devices=(existing,))
    old=args["assertion_review"]
    args["assertion_review"]=PlacementAssertionReview.capture(model,review,
        decisions=args["decisions"],registrations=args["registrations"],
        canonical_datum_id=old.canonical_datum_id,reviewer_id=old.reviewer_id,
        reviewer_derivation=old.reviewer_derivation,rationale=old.rationale,assertions=old.assertions)
    args["canonical_groups"]={"existing-inventory":["existing-device"]}
    args["source_references"]={**args["source_references"],
        ("placement-assertion-review","legacy-record"):"evidence/legacy"}
    result=deliver_reviewed_electrical(model,review,**args)
    assert result.completion["counts"]["canonical_devices"]==2
    assert len(result.completion["counts"]["new_canonical_electrical_ids"])==1
    assert next(r for r in result.completion["field_roles"] if r["canonical_id"]=="existing-device")["role"]=="unknown"
    assert {item["group"] for item in json.loads(result.artifacts["quantities"])["items"]}=={
        "existing-inventory","unresolved-inventory"}


@pytest.mark.parametrize("value",[None,object()])
def test_malformed_delivery_identity_still_has_a_machine_readable_failure(tmp_path,value):
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        run(inputs(tmp_path),delivery_id=value)
    assert error.value.stage=="configuration"
    assert error.value.completion["delivery_id"] is None


def test_failed_saved_run_does_not_masquerade_as_a_confirmed_empty_source(tmp_path):
    from oabm.proposals import AdapterAvailabilityError
    from test_symbol_proposals import run as saved_run
    from oabm.importers.pdf_electrical.symbol_stage import build_symbol_review
    model,review,args=inputs(tmp_path/"original")
    (tmp_path/"failed").mkdir()
    def unavailable(*args,**kwargs):raise AdapterAvailabilityError("synthetic unavailable")
    failed=saved_run(tmp_path/"failed",adapters={"vision_sol":unavailable})
    assert failed.status=="failed"
    args["runs"]=(failed,)
    args["journal_paths"]={failed.request.fingerprint:tmp_path/"failed/attempts.jsonl"}
    review=build_symbol_review((failed,),documents=args["documents"],images=args["images"],journal_paths=args["journal_paths"])
    args["decisions"]=()
    args["proposal_groups"]={}
    old=args["assertion_review"]
    args["assertion_review"]=PlacementAssertionReview.capture(model,review,decisions=(),
        registrations=args["registrations"],canonical_datum_id=old.canonical_datum_id,
        reviewer_id=old.reviewer_id,reviewer_derivation=old.reviewer_derivation,
        rationale="Preserve failed saved run for review.",assertions=())
    result=deliver_reviewed_electrical(model,review,**args)
    assert result.completion["delivery_ready"] and not result.model.electrical_devices
    assert result.completion["stages"]["source_review"]["saved_run_status_counts"]=={"failed":1}
    assert any(r["code"]=="saved_symbol_run_failed" for r in result.completion["unresolved"])
    assert not any(result.completion["claims"].values())
