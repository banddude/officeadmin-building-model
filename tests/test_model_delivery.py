"""Synthetic one-command canonical plan/preview delivery and publication boundaries."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import struct

import pytest

from oabm.drawings import PlanSpec, generate_drawing_set, layout_view_labels, paper_text_height_m, view_to_svg
from oabm.exports.gltf import to_glb
from oabm.exports import model_delivery as delivery
from oabm.model import BuildingModel, ContractError, ElectricalDevice, Point3, Polyline3D, Port, Pose, Vector3, validate_model

FIXTURE = Path(__file__).parents[1] / "fixtures/model_delivery/v1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inputs(tmp_path, **changes):
    model = tmp_path / "model.json"
    model.write_bytes((FIXTURE / "model.json").read_bytes())
    options = json.loads((FIXTURE / "options.json").read_bytes())
    options.update(changes)
    config = tmp_path / "options.json"
    config.write_text(json.dumps(options))
    return model, config


def publish(tmp_path, **changes):
    model, config = inputs(tmp_path, **changes)
    root = delivery.deliver_model(model, config, output_directory=tmp_path / "attempt")
    return root, json.loads((root / "MANIFEST.json").read_bytes())


def verify(root, manifest):
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert actual == {"MANIFEST.json", *(r["path"] for r in manifest["files"])}
    for row in manifest["files"]:
        data = (root / row["path"]).read_bytes()
        assert (len(data), sha(data)) == (row["bytes"], row["sha256"])


def test_one_model_outputs_match_existing_exporters_and_input_hashes(tmp_path):
    root, manifest = publish(tmp_path)
    verify(root, manifest)
    assert manifest["status"] == {"outputs":"generated_review_outputs", "bend_policy":"require",
                                   "bend_check":"passed_bounded_checks", "engineering_approval":False,
                                   "label_layout":"passed_estimated_clearance"}
    assert not (root.parent / ".pending").exists()
    model = BuildingModel.load(root / "inputs/model.json")
    compact = model.to_json(indent=None).encode()
    assert (root / "canonical-model.json").read_bytes() == compact
    assert manifest["canonical_model_sha256"] == sha(compact)
    raw = (root / "preview.glb").read_bytes()
    size, kind = struct.unpack_from("<II", raw, 12)
    asset = json.loads(raw[20:20+size])["asset"]
    assert asset["extras"]["canonical_model_id"] == model.model_id
    # Legacy 1.0 GLB bytes have no embedded digest; the package manifest binds them.
    assert "canonical_model_sha256" not in asset["extras"]
    original = tmp_path / "direct.glb"
    to_glb(model, original)
    assert original.read_bytes() == raw
    config = json.loads((root / "inputs/options.json").read_bytes())
    drawings = generate_drawing_set(model, plans=(PlanSpec(**config["plans"][0]),), **delivery.PLAN_OPTIONS)
    drawings = replace(drawings,views=tuple(layout_view_labels(v,pixels_per_model_unit=100,
        text_height_m=paper_text_height_m(v.scale)) for v in drawings.views))
    assert (root / "drawing-set.json").read_text() == drawings.to_json()+"\n"
    assert (root / "plans/plan-01.svg").read_text() == view_to_svg(
        drawings.views[0], pixels_per_model_unit=100, text_height_m=paper_text_height_m(50))
    assert manifest["runtime"]["engine_sources"]


def test_repeated_rebuild_has_identical_bytes(tmp_path):
    model, config = inputs(tmp_path)
    a = delivery.deliver_model(model, config, output_directory=tmp_path/"a")
    b = delivery.deliver_model(model, config, output_directory=tmp_path/"b")
    tree = lambda root: {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert tree(a) == tree(b)


def test_requested_outputs_only_and_explicit_skip(tmp_path):
    root, manifest = publish(tmp_path, plans=[], bend_policy="skip", radii_m={})
    assert not (root/"drawing-set.json").exists()
    assert not (root/"bend-report.json").exists()
    assert (root/"preview.glb").exists()
    assert manifest["status"]["bend_check"] == "not_evaluated"
    assert manifest["effective_plans"] == []
    verify(root, manifest)


def test_plan_only_does_not_call_glb(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery,"to_glb",lambda *a,**k: pytest.fail("unrequested preview"))
    root, manifest = publish(tmp_path, preview_glb=False)
    assert (root/"plans/plan-01.svg").exists()
    assert not (root/"preview.glb").exists()
    verify(root,manifest)


def test_required_unresolved_gate_publishes_no_delivery(tmp_path,monkeypatch):
    model, config = inputs(tmp_path,radii_m={})
    monkeypatch.setattr(delivery,"generate_drawing_set",lambda *a,**k: pytest.fail("gate bypass"))
    with pytest.raises(delivery.ModelDeliveryError) as exc:
        delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    assert exc.value.stage == "bend_preflight"
    assert not (tmp_path/"attempt/delivery").exists()
    assert not (tmp_path/"attempt/.pending/MANIFEST.json").exists()
    assert json.loads((tmp_path/"attempt/.pending/bend-report.json").read_bytes())["ready"] is False


def test_late_partial_export_failure_and_stale_retry_cannot_publish(tmp_path,monkeypatch):
    model, config = inputs(tmp_path)
    original = delivery.to_glb
    def failed(model,path):
        Path(path).write_bytes(b"partial GLB")
        raise RuntimeError("synthetic late failure")
    monkeypatch.setattr(delivery,"to_glb",failed)
    attempt = tmp_path/"attempt"
    with pytest.raises(delivery.ModelDeliveryError) as exc:
        delivery.deliver_model(model,config,output_directory=attempt)
    assert exc.value.stage == "preview_glb"
    assert (attempt/".pending/plans/plan-01.svg").exists()
    assert (attempt/".pending/preview.glb").read_bytes() == b"partial GLB"
    assert not (attempt/"delivery").exists()
    before = (attempt/"failure.json").read_bytes()
    monkeypatch.setattr(delivery,"to_glb",original)
    with pytest.raises(FileExistsError):
        delivery.deliver_model(model,config,output_directory=attempt)
    assert (attempt/"failure.json").read_bytes() == before
    root = delivery.deliver_model(model,config,output_directory=tmp_path/"fresh")
    verify(root,json.loads((root/"MANIFEST.json").read_bytes()))


def test_existing_completed_output_is_never_overwritten(tmp_path):
    root, manifest = publish(tmp_path)
    before = (root/"MANIFEST.json").read_bytes()
    with pytest.raises(FileExistsError):
        delivery.deliver_model(tmp_path/"model.json",tmp_path/"options.json",output_directory=root.parent)
    assert (root/"MANIFEST.json").read_bytes() == before
    verify(root,manifest)


def test_input_source_edits_after_snapshot_cannot_mix_models(tmp_path,monkeypatch):
    model, config = inputs(tmp_path)
    before = model.read_bytes()
    original = delivery.to_glb
    def mutate_source(canonical,path):
        model.write_text('{"not":"the snapshotted model"}')
        config.write_text('{}')
        return original(canonical,path)
    monkeypatch.setattr(delivery,"to_glb",mutate_source)
    root = delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    manifest = json.loads((root/"MANIFEST.json").read_bytes())
    assert (root/"inputs/model.json").read_bytes() == before
    assert manifest["canonical_model_sha256"] == sha(BuildingModel.from_json(before).to_json(indent=None).encode())
    verify(root,manifest)


def test_changed_artifact_before_publication_fails(tmp_path,monkeypatch):
    model, config = inputs(tmp_path)
    original = delivery.to_glb
    def corrupt(canonical,path):
        result = original(canonical,path)
        (Path(path).parent/"plans/plan-01.svg").write_text('stale synthetic output')
        return result
    monkeypatch.setattr(delivery,"to_glb",corrupt)
    with pytest.raises(delivery.ModelDeliveryError) as exc:
        delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    assert exc.value.stage == "verify_exports"
    assert not (tmp_path/"attempt/delivery").exists()


def consolidated_corner_inputs(tmp_path):
    from oabm.routing import consolidate_bundled_routes
    model, config = inputs(tmp_path,bend_policy="report")
    m = BuildingModel.load(model)
    r, f = m.routes[0], m.route_fittings[0]
    second_f = replace(f,id="fitting:second",route_id="route:second")
    second_r = replace(r,id="route:second",fitting_ids=(second_f.id,))
    m = replace(m,routes=(r,second_r),route_fittings=(f,second_f))
    m = consolidate_bundled_routes(m).model
    validate_model(m)
    m.dump(model)
    options = json.loads(config.read_bytes())
    options["radii_m"] = {r.id:.3 for r in m.routes}
    config.write_text(json.dumps(options))
    return model,config,m,options


@pytest.mark.parametrize('policy',['report','require'])
def test_unambiguous_consolidated_corner_delivers_with_both_leg_budgets(tmp_path,policy):
    from oabm.qa.route_bends import check_route_bends
    model,config,m,options=consolidated_corner_inputs(tmp_path)
    frozen=m.to_json()
    assert len(m.routes)==2 and len(m.route_fittings)==1
    elbow=m.route_fittings[0];junction=elbow.pose.position
    assert elbow.fitting_type=='elbow-90' and elbow.angle_radians==pytest.approx(math.pi/2)
    legs=[];connection_ports=[]
    xyz=lambda point:(point.x,point.y,point.z)
    for route in m.routes:
        points=route.centerline.points
        assert len(points)==2 and junction in points
        at_start=points[0]==junction
        adjacent=points[1] if at_start else points[0]
        connection_ports.append(route.start_port_id if at_start else route.end_port_id)
        legs.append(tuple(a-b for a,b in zip(xyz(adjacent),xyz(junction))))
        assert math.dist(xyz(junction),xyz(adjacent))==pytest.approx(2.)
    assert len(set(connection_ports))==1
    assert sum(a*b for a,b in zip(*legs))==pytest.approx(0.)
    # Independently charge each actual two-metre leg. Checking only the elbow's
    # owning route would incorrectly pass one of these two over-budget legs.
    over=check_route_bends(m,radii_m={route.id:2.1 for route in m.routes})
    short=[diagnostic for diagnostic in over.diagnostics if diagnostic.code=='short_straight']
    assert {diagnostic.route_id for diagnostic in short}=={route.id for route in m.routes}
    assert len(short)==2
    assert all(d.available_m==pytest.approx(2.) and d.required_m==pytest.approx(2.1) for d in short)

    options['bend_policy']=policy;config.write_text(json.dumps(options))
    root=delivery.deliver_model(model,config,output_directory=tmp_path/policy)
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    report=json.loads((root/'bend-report.json').read_bytes())
    assert manifest['status']['bend_check']=='passed_bounded_checks'
    assert manifest['status']['engineering_approval'] is False
    assert report['ready'] is True and report['diagnostics']==[]
    assert len(report['bends'])==1
    bend=report['bends'][0]
    assert bend['route_id']==elbow.route_id
    assert bend['angle_radians']==pytest.approx(math.pi/2)
    assert bend['radius_m']==pytest.approx(.3)
    assert bend['tangent_setback_m']==pytest.approx(.3)
    assert json.loads((root/'canonical-model.json').read_bytes())==m.to_dict()
    assert m.to_json()==frozen
    verify(root,manifest)
    assert (root/"preview.glb").exists()


def test_ambiguous_consolidated_junction_requires_review_and_blocks_require(tmp_path,monkeypatch):
    model,config,m,options=consolidated_corner_inputs(tmp_path)
    # Three distinct outgoing legs cannot be certified by the surviving
    # two-way elbow; no geometry duplication is needed to make this ambiguous.
    junction=m.route_fittings[0].pose.position
    first=m.routes[0]
    junction_port=first.start_port_id if first.centerline.points[0]==junction else first.end_port_id
    end=Point3(x=2,y=-2,z=0)
    device=ElectricalDevice(id='device:ambiguous-extra',device_type='synthetic-endpoint',pose=Pose(position=end))
    port=Port(id='port:ambiguous-extra',owner_id=device.id,domain='power',role='connection',pose=Pose(position=end),
        direction=Vector3(x=0,y=1,z=0))
    extra=replace(first,id='route:ambiguous-extra',start_port_id=junction_port,end_port_id=port.id,
        centerline=Polyline3D(points=(junction,end)),fitting_ids=(),attributes={},provenance=())
    m=replace(m,routes=(*m.routes,extra),ports=(*m.ports,port),electrical_devices=(*m.electrical_devices,device))
    validate_model(m);m.dump(model)
    options['radii_m']={route.id:.3 for route in m.routes};config.write_text(json.dumps(options))
    root=delivery.deliver_model(model,config,output_directory=tmp_path/'report')
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    assert manifest['status']['bend_check']=='needs_review'
    assert [d['code'] for d in json.loads((root/'bend-report.json').read_bytes())['diagnostics']]==['junction_ambiguous']
    assert (root/'preview.glb').exists()
    verify(root,manifest)
    options["bend_policy"] = "require"
    config.write_text(json.dumps(options))
    with monkeypatch.context() as guarded:
        guarded.setattr(delivery,'generate_drawing_set',lambda *args,**kwargs:pytest.fail('ambiguous bend gate bypassed'))
        with pytest.raises(delivery.ModelDeliveryError) as error:
            delivery.deliver_model(model,config,output_directory=tmp_path/"required")
    assert error.value.stage=='bend_preflight'
    assert not (tmp_path/'required'/'delivery').exists()
    assert not (tmp_path/'required'/'.pending'/'MANIFEST.json').exists()
    options.update(bend_policy="skip",radii_m={},plans=[])
    config.write_text(json.dumps(options))
    root = delivery.deliver_model(model,config,output_directory=tmp_path/"skip")
    assert json.loads((root/"MANIFEST.json").read_bytes())["status"]["bend_check"] == "not_evaluated"


@pytest.mark.parametrize("changes",[
    {"plans":[],"preview_glb":False}, {"preview_glb":1}, {"bend_policy":"approve"},
    {"radii_m":[]}, {"bend_policy":"skip"}, {"plans":[{}]},
    {"version":"unknown"}, {"extra":True},
])
def test_invalid_options_do_not_reserve_output(tmp_path,changes):
    model, config = inputs(tmp_path,**changes)
    with pytest.raises(ContractError):
        delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    assert not (tmp_path/"attempt").exists()


def test_cli_and_unknown_level(tmp_path,capsys):
    model, config = inputs(tmp_path)
    assert delivery.main([str(model),str(config),str(tmp_path/"attempt")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path/"attempt/delivery")
    options = json.loads(config.read_bytes())
    options["plans"][0]["level_id"] = "level:missing"
    config.write_text(json.dumps(options))
    with pytest.raises(SystemExit) as exc:
        delivery.main([str(model),str(config),str(tmp_path/"bad")])
    assert exc.value.code == 1
    assert not (tmp_path/"bad").exists()


def test_exporter_cannot_mutate_canonical_input_and_publish(tmp_path,monkeypatch):
    model, config = inputs(tmp_path)
    original = delivery.to_glb
    def mutate(canonical,path):
        canonical.attributes["synthetic_mutation"] = True
        return original(canonical,path)
    monkeypatch.setattr(delivery,"to_glb",mutate)
    with pytest.raises(delivery.ModelDeliveryError) as exc:
        delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    assert exc.value.stage == "verify_exports"
    assert not (tmp_path/"attempt/delivery").exists()


def test_unrecorded_export_sidecar_cannot_publish(tmp_path,monkeypatch):
    model, config = inputs(tmp_path)
    original = delivery.to_glb
    def sidecar(canonical,path):
        result = original(canonical,path)
        (Path(path).parent/"unrecorded.txt").write_text('synthetic extra file')
        return result
    monkeypatch.setattr(delivery,"to_glb",sidecar)
    with pytest.raises(delivery.ModelDeliveryError) as exc:
        delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    assert exc.value.stage == "verify_exports"
    assert not (tmp_path/"attempt/delivery").exists()


def test_repeated_option_key_and_nonfinite_plan_are_rejected(tmp_path):
    model, config = inputs(tmp_path)
    config.write_text('{"version":"model-delivery-v1","version":"model-delivery-v1"}')
    with pytest.raises(ContractError,match="duplicate"):
        delivery.deliver_model(model,config,output_directory=tmp_path/"duplicate")
    model, config = inputs(tmp_path)
    options = json.loads(config.read_bytes())
    options["plans"][0]["scale"] = float("nan")
    config.write_text(json.dumps(options))
    with pytest.raises(ContractError,match="finite"):
        delivery.deliver_model(model,config,output_directory=tmp_path/"nonfinite")


def test_unplaced_labels_publish_explicit_review_status_and_full_text(tmp_path):
    model, config = inputs(tmp_path)
    canonical=BuildingModel.load(model)
    full="Synthetic equipment description "*12
    canonical=replace(canonical,electrical_equipment=(replace(canonical.electrical_equipment[0],name=full),))
    canonical.dump(model)
    root=delivery.deliver_model(model,config,output_directory=tmp_path/"attempt")
    manifest=json.loads((root/"MANIFEST.json").read_bytes())
    assert manifest["status"]["bend_check"]=="passed_bounded_checks"
    assert manifest["status"]["label_layout"]=="needs_review"
    assert manifest["label_layout"]["unplaced_labels"]==1
    assert "Label layout: needs_review" in (root/"READ-ME.txt").read_text()
    drawings=json.loads((root/"drawing-set.json").read_bytes())
    missing=json.loads(dict(drawings["views"][0]["metadata"])["unplaced_labels"])
    assert missing[0]["text"]==full
    assert missing[0]["reason"]=="text_exceeds_view"
    assert full not in (root/"plans/plan-01.svg").read_text()
    verify(root,manifest)
