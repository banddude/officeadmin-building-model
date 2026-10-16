"""Canonical admission + explicitly authored panel facts, with no engineering defaults."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.model import (BuildingModel, ElectricalEquipment, ElectricalDevice, Point3, Pose,
    Size3, Port, Vector3, Circuit, Provenance, ContractError)
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.pipeline.review_drawings import ReviewPlan, ReviewDrawingConfig, VERSION, BANNER
from oabm.pipeline.reviewed_electrical import deliver_reviewed_electrical, ReviewedElectricalDeliveryError
from oabm.reports import PanelScheduleReport
from test_reviewed_electrical_pipeline import inputs


def config(**changes):
    plan = ReviewPlan(id="review:floor", level_id="floor", bounds_m=(0., 0., 10., 10.),
        cut_height_m=4.5, view_depth_below_m=0., view_depth_above_m=.25, scale=50.,
        show_architecture=True, show_spaces=True, show_routes=False, show_dimensions=False)
    if changes: plan = replace(plan, **changes)
    return ReviewDrawingConfig(version=VERSION, plans=(plan,), pixels_per_model_unit=40.)


def case(tmp_path, *, defer=False, shifted=False):
    model, review, args = inputs(tmp_path, include_ifc=True)
    prov = (Provenance(source_kind="synthetic", source_id="fixture:review-sheet-base", derivation="user"),)
    panel = ElectricalEquipment(id="equipment:base-panel", equipment_type="panelboard", level_id="floor",
        pose=Pose(position=Point3(x=1., y=1., z=4.5)), size=Size3(x=.4,y=.2,z=.8), provenance=prov)
    light = ElectricalDevice(id="device:base-ceiling", device_type="luminaire", level_id="floor",
        pose=Pose(position=Point3(x=3., y=3., z=7.)), provenance=prov)
    ports = (Port(id="port:base-source", owner_id=panel.id, domain="electrical", role="source",
                  pose=panel.pose, direction=Vector3(x=1.,y=0.,z=0.), provenance=prov),
             Port(id="port:base-load", owner_id=light.id, domain="electrical", role="sink",
                  pose=light.pose, direction=Vector3(x=1.,y=0.,z=0.), provenance=prov))
    circuit = Circuit(id="circuit:base-facts", source_port_id=ports[0].id,
        load_port_ids=(ports[1].id,), circuit_number="A-1", load_va=None, provenance=prov)
    model = BuildingModel.from_dict(replace(model, electrical_equipment=(panel,), electrical_devices=(light,),
        ports=ports, circuits=(circuit,)).to_dict())
    assertion = args["assertion_review"]
    facts = assertion.assertions
    if shifted:
        # Explicit fresh reviewed project transform: +1m in canonical X, not an inferred move.
        reg = args["registrations"][0]
        reg = replace(reg, transform=replace(reg.transform, tx_m=reg.transform.tx_m+1.))
        args["registrations"] = (reg,)
        decision = args["decisions"][0]
        args["decisions"] = (replace(decision, placement=replace(decision.placement,
            registration_fingerprint=reg.fingerprint)),)
        facts = tuple(replace(f, reference_id=reg.fingerprint,
            value=(.1,0.,0.,.1,2.,2.) if f.field=="registration_xy" else f.value)
            if f.field in ("registration_xy","anchor") else f for f in facts)
    if defer:
        facts = tuple(f for f in facts if f.field != "elevation")
    args["assertion_review"] = PlacementAssertionReview.capture(model, review,
        decisions=args["decisions"], registrations=args["registrations"],
        canonical_datum_id=assertion.canonical_datum_id, reviewer_id=assertion.reviewer_id,
        reviewer_derivation=assertion.reviewer_derivation, rationale=assertion.rationale, assertions=facts)
    args["canonical_groups"] = {"base-supplied": (panel.id, light.id)}
    args["source_references"][("synthetic","fixture:review-sheet-base")] = "evidence/synthetic-base"
    return model, review, args


def deliver(value, cfg):
    model, review, args = value
    if cfg is not None:
        args = {**args, "artifact_references": {**args["artifact_references"],
            **{k:"review-artifacts/"+str(i) for i,k in enumerate(cfg.artifact_kinds)}}}
    return deliver_reviewed_electrical(model, review, **args, review_drawings=cfg)


def unpack(result):
    return (json.loads(result.artifacts["drawing_set"]),
            PanelScheduleReport.from_dict(json.loads(result.artifacts["panel_facts"])["panel_facts"]))


def test_exact_admitted_snapshot_drives_plan_panel_quantity_and_ifc(tmp_path):
    value=case(tmp_path); before=value[0].to_json()
    result=deliver(value,config()); drawing,facts=unpack(result)
    ids={e.id for e in (*result.model.electrical_devices,*result.model.electrical_equipment)}
    assert len(ids)==3 and value[0].to_json()==before
    facts.validate_against_model(result.model)
    group,=facts.panels
    assert group.unknown_load_circuit_ids==("circuit:base-facts",)
    assert not group.all_listed_loads_known and group.known_connected_va==0.
    assert group.circuits[0].circuit.entity["load_va"] is None
    visibility=drawing["review"]["views"][0]
    assert set(visibility["rendered_electrical_ids"])==ids and not visibility["omitted_electrical_ids"]
    assert drawing["review"]["canonical_model_sha256"]==result.completion["canonical_output_sha256"]
    assert {s["canonical_id"] for s in drawing["drawing_set"]["source_index"]} >= ids
    assert all(BANNER in s["title"] for s in drawing["drawing_set"]["schedules"])
    svg=result.artifacts["review_plan:review:floor"]
    tree=ET.fromstring(svg)
    assert tree.attrib["data-review-status"]=="incomplete" and BANNER in svg.decode()
    assert tree.attrib["data-view-id"]=="review:floor"
    assert tree.find("{http://www.w3.org/2000/svg}g").attrib["transform"]=="translate(0 64)"
    assert {i for node in tree.iter() for i in node.attrib.get("data-source-ids","").split()} >= ids
    quantities=json.loads(result.artifacts["quantities"])
    assert {i for r in quantities["items"] for i in r["source_entity_ids"]} >= ids
    from oabm.ifc.adapter import from_ifc
    path=tmp_path/"review.ifc"; path.write_bytes(result.artifacts["ifc"])
    restored=from_ifc(path)
    assert {e.id for e in (*restored.electrical_devices,*restored.electrical_equipment)}==ids
    assert not result.completion["review_drawings"]["permit_ready"]
    assert result.completion["stages"]["review_drawings"]["status"]=="passed"


def test_explicit_low_depth_reports_ceiling_omission_and_extent_crossings(tmp_path):
    result=deliver(case(tmp_path), config(cut_height_m=1.2,view_depth_above_m=.25))
    view=result.completion["review_drawings"]["views"][0]
    assert "device:base-ceiling" in view["omitted_electrical_ids"]
    assert "equipment:base-panel" in view["clipped_electrical_ids"]
    cropped=deliver(case(tmp_path/"cropped"),config(bounds_m=(0.,0.,2.,2.)))
    assert "device:base-ceiling" in cropped.completion["review_drawings"]["views"][0]["omitted_electrical_ids"]


def test_deferred_symbols_absent_from_plan_but_retained_in_completion(tmp_path):
    result=deliver(case(tmp_path,defer=True),config())
    assert len(result.model.electrical_devices)==1
    pending=result.completion["review_drawings"]["source_proposals_not_admitted"]
    assert len(pending)==1 and pending[0]["status"]=="defer"
    assert pending[0]["proposal_id"].encode() not in result.artifacts["review_plan:review:floor"]
    assert not any(result.completion["claims"].values())


def test_repeat_and_disabled_outputs_preserve_existing_delivery_bytes(tmp_path):
    value=case(tmp_path)
    a=deliver(value,config()); b=deliver(value,config())
    assert a.artifacts==b.artifacts and a.completion_json==b.completion_json
    model,review,args=value
    default=deliver_reviewed_electrical(model,review,**args)
    disabled=deliver_reviewed_electrical(model,review,**args,review_drawings=None)
    assert default.artifacts==disabled.artifacts and default.completion_json==disabled.completion_json
    assert "review_drawings" not in disabled.completion
    assert all(a.artifacts[k]==v for k,v in disabled.artifacts.items())


def test_fresh_reviewed_transform_changes_sheet_positions_and_hash(tmp_path):
    a=deliver(case(tmp_path/"original"),config())
    b=deliver(case(tmp_path/"moved",shifted=True),config())
    da=next(d for d in a.model.electrical_devices if d.id!="device:base-ceiling")
    db=next(d for d in b.model.electrical_devices if d.id!="device:base-ceiling")
    assert db.pose.position.x==pytest.approx(da.pose.position.x+1.)
    assert a.artifacts["review_plan:review:floor"] != b.artifacts["review_plan:review:floor"]
    assert a.completion["canonical_output_sha256"] != b.completion["canonical_output_sha256"]


def test_missing_requested_level_is_a_drawing_failure_not_a_partial_result(tmp_path):
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        deliver(case(tmp_path),config(level_id="unknown"))
    assert error.value.stage=="review_drawings" and not error.value.completion["delivery_ready"]


@pytest.mark.parametrize("field,value",[("cut_height_m",float('nan')),("scale",True),
    ("view_depth_above_m",float('inf')),("show_routes",1)])
def test_view_configuration_refuses_invalid_or_implicit_values(field,value):
    with pytest.raises((ContractError,ValueError)):
        config(**{field:value})


def test_empty_model_and_empty_view_request_do_not_invent_a_plan_or_panel():
    from oabm.pipeline.review_drawings import generate_review_drawings
    model=BuildingModel(model_id="synthetic-empty-review",schema_version="1.3.0")
    payloads,status=generate_review_drawings(model,ReviewDrawingConfig(version=VERSION,plans=(),pixels_per_model_unit=40.))
    assert set(payloads)=={"drawing_set","panel_facts"}
    assert status["no_canonical_levels"] and status["no_plan_views_requested"]
    assert not status["permit_ready"] and status["panel_count"]==0
    assert json.loads(payloads["drawing_set"])["drawing_set"]["views"]==[]


def test_finite_config_cannot_overflow_rendered_svg_extent(tmp_path):
    cfg=replace(config(),pixels_per_model_unit=1e308)
    with pytest.raises(ReviewedElectricalDeliveryError) as error:
        deliver(case(tmp_path),cfg)
    assert error.value.stage=="review_drawings"
    assert not error.value.completion["delivery_ready"]


def saved_manifest(path, value, cfg):
    """Save independent synthetic input files for the actual public entrypoint."""
    from oabm.pipeline.saved_electrical import DRAWING_VERSION
    path.mkdir(parents=True,exist_ok=True)
    model,review,args=value
    def put(name,data):
        if not isinstance(data,bytes):data=json.dumps(data,sort_keys=True).encode()
        (path/name).write_bytes(data)
        return {"path":name,"sha256":hashlib.sha256(data).hexdigest()}
    data={"version":DRAWING_VERSION,"delivery_id":args["delivery_id"],
        "model":put("base-model.json",model.to_dict()),"review":put("review.json",review.to_dict()),
        "review_drawings":None if cfg is None else put("drawing-config.json",cfg.to_dict())}
    for key in ("runs","decisions","registrations"):
        data[key]=put(key+".json",[r.to_dict() for r in args[key]])
    for key in ("assertion_review","scope_policy","review_config"):
        data[key]=put(key+".json",args[key].to_dict())
    for key in ("documents","images","evidence_documents"):
        data[key]={identity:put(key+"-"+str(i),payload) for i,(identity,payload) in enumerate(args[key].items())}
    data["journals"]={identity:put("journal-"+str(i),Path(p).read_bytes()) for i,(identity,p) in enumerate(args["journal_paths"].items())}
    data["source_references"]=[{"source_kind":k,"source_id":s,"reference":v} for (k,s),v in sorted(args["source_references"].items())]
    for key in ("proposal_groups","canonical_groups","glb_options","include_ifc","coverage_report"):
        data[key]=args[key]
    manifest=path/"inputs.json";manifest.write_text(json.dumps(data))
    return manifest


def checked_persistence(final):
    marker=json.loads((final/"COMPLETED.json").read_bytes())
    for row in marker["files"]:
        payload=(final/row["path"]).read_bytes()
        assert len(payload)==row["bytes"] and hashlib.sha256(payload).hexdigest()==row["sha256"]
    report=json.loads((final/"completion-report.json").read_bytes())
    for row in report["artifacts"].values():
        p=Path(row["reference"])
        assert p.parent==final and hashlib.sha256(p.read_bytes()).hexdigest()==row["sha256"]
    return report


def test_actual_saved_cli_persists_review_plans_facts_and_repeated_versions(tmp_path):
    import subprocess,sys
    value=case(tmp_path/"source")
    manifest=saved_manifest(tmp_path/"saved",value,config())
    original={p.name:p.read_bytes() for p in manifest.parent.iterdir() if p.is_file()}
    def cli(label):
        output=subprocess.run([sys.executable,"-m","oabm.pipeline.saved_electrical",str(manifest),
            "--output-root",str(tmp_path/"deliveries"),"--run-id",label],capture_output=True,text=True)
        assert output.returncode==0,output.stderr
        return Path(output.stdout.strip())
    first=cli("first"); report=checked_persistence(first)
    second=cli("second");checked_persistence(second)
    assert original=={p.name:p.read_bytes() for p in manifest.parent.iterdir() if p.is_file()}
    assert (first/"review-plan-001.svg").read_bytes()==(second/"review-plan-001.svg").read_bytes()
    assert (first/"drawing-set.json").read_bytes()==(second/"drawing-set.json").read_bytes()
    assert (first/"panel-facts.json").read_bytes()==(second/"panel-facts.json").read_bytes()
    model=BuildingModel.from_json((first/"model.json").read_text())
    ids={e.id for e in (*model.electrical_devices,*model.electrical_equipment)}
    assert set(report["review_drawings"]["views"][0]["rendered_electrical_ids"])==ids
    assert len(ids)==3 and report["counts"]["dispositions"]=={"accept":1}
    quantities=json.loads((first/"quantities.json").read_bytes())
    assert {s for r in quantities["items"] for s in r["source_entity_ids"]} >= ids
    from oabm.ifc.adapter import from_ifc
    restored=from_ifc(first/"model.ifc")
    assert {e.id for e in (*restored.electrical_devices,*restored.electrical_equipment)}==ids
    facts=PanelScheduleReport.from_dict(json.loads((first/"panel-facts.json").read_bytes())["panel_facts"])
    facts.validate_against_model(model)
    assert facts.panels[0].unknown_load_circuit_ids==("circuit:base-facts",)
    rows=json.loads((first/"drawing-set.json").read_bytes())["drawing_set"]["schedules"]
    assert "circuit:base-facts" in {r["source_id"] for s in rows for r in s["rows"]}
    assert not report["review_drawings"]["permit_ready"] and not any(report["claims"].values())


def test_saved_entrypoint_fresh_review_changes_geometry_and_deferred_remains_visible(tmp_path):
    from oabm.pipeline.saved_electrical import persist_saved_electrical
    original=saved_manifest(tmp_path/"original",case(tmp_path/"source-a"),config())
    moved=saved_manifest(tmp_path/"moved",case(tmp_path/"source-b",shifted=True),config())
    deferred=saved_manifest(tmp_path/"deferred",case(tmp_path/"source-c",defer=True),config())
    a=persist_saved_electrical(original,output_root=tmp_path/"out",run_id="a")
    b=persist_saved_electrical(moved,output_root=tmp_path/"out",run_id="b")
    c=persist_saved_electrical(deferred,output_root=tmp_path/"out",run_id="c")
    ra,rb,rc=(checked_persistence(p) for p in (a,b,c))
    assert ra["canonical_output_sha256"]!=rb["canonical_output_sha256"]
    assert (a/"review-plan-001.svg").read_bytes()!=(b/"review-plan-001.svg").read_bytes()
    pending=rc["review_drawings"]["source_proposals_not_admitted"]
    assert len(pending)==1 and pending[0]["status"]=="defer"
    model=BuildingModel.from_json((c/"model.json").read_text())
    assert {e.id for e in model.electrical_devices}=={"device:base-ceiling"}


def test_saved_requested_drawing_failure_cannot_publish_partial_delivery(tmp_path):
    from oabm.pipeline.saved_electrical import persist_saved_electrical,SavedDeliveryError
    manifest=saved_manifest(tmp_path/"saved",case(tmp_path/"source"),config(level_id="absent"))
    with pytest.raises(SavedDeliveryError) as error:
        persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="failed")
    assert error.value.stage=="compute_delivery"
    failure=json.loads((tmp_path/"out/failed/failure.json").read_bytes())
    assert failure["runner_completion"]["failure_stage"]=="review_drawings"
    assert not failure["runner_completion"]["delivery_ready"]
    assert not (tmp_path/"out/failed/delivery").exists()


def test_saved_late_svg_write_failure_retains_uncommitted_artifacts(tmp_path,monkeypatch):
    from oabm.pipeline import saved_electrical as saved
    manifest=saved_manifest(tmp_path/"saved",case(tmp_path/"source"),config())
    original=saved._write
    def fail(path,data):
        if path.suffix==".svg":raise OSError("synthetic SVG storage failure")
        original(path,data)
    monkeypatch.setattr(saved,"_write",fail)
    with pytest.raises(saved.SavedDeliveryError) as error:
        saved.persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="failed")
    assert error.value.stage=="persist_delivery"
    assert (tmp_path/"out/failed/.pending/drawing-set.json").exists()
    assert not (tmp_path/"out/failed/delivery").exists()
    assert not list((tmp_path/"out/failed").rglob("COMPLETED.json"))


def test_v1_manifest_rejects_drawing_extension_and_v2_null_preserves_runner_shape(tmp_path):
    from oabm.pipeline.saved_electrical import persist_saved_electrical,SavedDeliveryError,VERSION as V1
    manifest=saved_manifest(tmp_path/"saved",case(tmp_path/"source"),None)
    final=persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="disabled")
    report=checked_persistence(final)
    assert "review_drawings" not in report and not (final/"drawing-set.json").exists()
    data=json.loads(manifest.read_bytes());data["version"]=V1
    manifest.write_text(json.dumps(data))
    with pytest.raises(SavedDeliveryError):
        persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="invalid-v1")


def test_multiple_views_keep_identity_mapping_without_using_ids_as_paths(tmp_path):
    from oabm.pipeline.saved_electrical import persist_saved_electrical
    cfg=config()
    cfg=replace(cfg,plans=(replace(cfg.plans[0],id="z-view"),replace(cfg.plans[0],id="../not-a-path")))
    manifest=saved_manifest(tmp_path/"saved",case(tmp_path/"source"),cfg)
    final=persist_saved_electrical(manifest,output_root=tmp_path/"out",run_id="views")
    report=checked_persistence(final)
    assert Path(report["artifacts"]["review_plan:../not-a-path"]["reference"])==final/"review-plan-001.svg"
    assert Path(report["artifacts"]["review_plan:z-view"]["reference"])==final/"review-plan-002.svg"
    assert [r["view_id"] for r in report["review_drawings"]["views"]]==["../not-a-path","z-view"]
    assert ET.fromstring((final/"review-plan-001.svg").read_bytes()).attrib["data-view-id"]=="../not-a-path"
    assert not (final.parent/"not-a-path").exists()
