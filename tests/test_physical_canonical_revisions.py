"""Mixed canonical revisions retain actual physical paths and auditable quantities."""
from dataclasses import replace
import json
import importlib.util
from pathlib import Path
import math

import pytest

from oabm.model import (
    BuildingModel, ContractError, Obstacle, Route, Polyline3D, Point3, Pose, Box3D, Size3,
    Level, compute_physical_request_sha256,
)
from oabm.qa.revision_diff import build_revision_diff, RevisionDiff
from oabm.reports.physical_route_revisions import generate_physical_route_comparison
from physical_route_fixture import make_physical_model
from test_physical_route_revisions import detour, rename_children
from test_physical_route_schedule import measured_model
_spec = importlib.util.spec_from_file_location("mixed_revision_example", Path(__file__).parents[1]/"examples/canonical_physical_revisions.py")
_example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_example)
mixed_snapshots, create_example = _example.mixed_snapshots, _example.create_example


def changed_route(before, route):
    return replace(before, routes=tuple(route if row.id == route.id else row for row in before.routes))


def fresh_request(route, **changes):
    requirements = changes.get("requirements", route.requirements)
    context = changes.get("context", route.context)
    return replace(route, **{**changes, "context": replace(context,
        request_sha256=compute_physical_request_sha256(context, requirements))})


def route_change(report, route_id="route:physical"):
    return next(row for row in report.changes if row.collection == "routes" and row.entity_id == route_id)


def test_unchanged_physical_snapshots_are_empty_and_roundtrip_without_mutation():
    before = measured_model()
    frozen = before.to_json()
    report = build_revision_diff(before, before)
    assert not report.changes and not report.quantity_deltas
    assert report.added_count == report.removed_count == report.changed_count == 0
    assert RevisionDiff.from_dict(json.loads(report.to_json())).to_json() == report.to_json()
    assert before.to_json() == frozen


def test_same_length_different_paths_are_geometry_without_a_fake_quantity_delta():
    before, after = detour(1), detour(-1)
    report = build_revision_diff(before, after)
    row = route_change(report)
    assert row.change_classes == ("geometry",)
    assert any(field.path.startswith("joints.") and ".position." in field.path for field in row.field_changes)
    assert not report.quantity_deltas
    measured = generate_physical_route_comparison(before, after).to_dict()["routes"][0]
    assert measured["path_changed"] is True
    assert measured["delta"]["length"]["total_m_approx"] == 0


def test_radius_revision_uses_analytic_delta_and_retains_fresh_source_hashes():
    before, after = measured_model(), measured_model(odd=True)
    report = build_revision_diff(before, after)
    row = route_change(report)
    assert "geometry" in row.change_classes and "provenance" in row.change_classes
    assert any(field.path == "requirements.centerline_radius_m" and field.change_classes == ("geometry",)
               for field in row.field_changes)
    assert any(field.path == "context.source_model_sha256" and field.change_classes == ("provenance",)
               for field in row.field_changes)
    delta = next(row for row in report.quantity_deltas if row.category == "route_length")
    exact = generate_physical_route_comparison(before, after).to_dict()["routes"][0]["delta"]
    assert exact["length"]["pi_coefficient_ticks"] == {"numerator": 1, "denominator": 2}
    assert delta.status == "changed" and delta.delta == pytest.approx(math.pi * .5e-9, abs=1e-15)
    reverse = next(row for row in build_revision_diff(after,before).quantity_deltas if row.category == "route_length")
    assert reverse.delta == -delta.delta


@pytest.mark.parametrize("physical_side", ["before", "after"])
def test_added_removed_route_has_its_actual_document_and_one_signed_quantity_scope(physical_side):
    model = make_physical_model(); empty = replace(model, routes=())
    pair = (model,empty) if physical_side == "before" else (empty,model)
    report = build_revision_diff(*pair)
    status = "removed" if physical_side == "before" else "added"
    row = route_change(report)
    assert row.change == status and row.document["geometry_kind"] == "line_arc_v1"
    assert row.document["segments"][0]["id"] == model.routes[0].segments[0].id
    delta = next(row for row in report.quantity_deltas if row.category == "route_length")
    assert delta.status == status and delta.delta is None
    assert (delta.before,delta.after) == ((2.,None) if status == "removed" else (None,2.))


def test_child_identity_changes_are_graph_not_path_or_replacement_quantities():
    before = measured_model(); after = rename_children(before)
    report = build_revision_diff(before,after)
    assert route_change(report).change_classes == ("graph",)
    assert not report.quantity_deltas
    physical = generate_physical_route_comparison(before,after).to_dict()
    assert physical["routes"][0]["path_changed"] is False
    assert not physical["physical_inventory"]["gross_added_route_ids"]


def test_same_id_physical_to_legacy_keeps_measurement_bases_separate():
    before = make_physical_model(); physical = before.routes[0]
    legacy = Route(id=physical.id,route_type=physical.route_type,
        start_port_id=physical.start_port_id,end_port_id=physical.end_port_id,
        centerline=Polyline3D(points=(physical.joints[0].position,physical.joints[-1].position)))
    report = build_revision_diff(before,replace(before,routes=(legacy,)))
    assert report.added_count == report.removed_count == 0 and report.changed_count == 1
    assert "geometry" in route_change(report).change_classes
    lengths = [row for row in report.quantity_deltas if row.category == "route_length"]
    assert len(lengths) == 2 and {row.status for row in lengths} == {"added","removed"}
    assert all(row.delta is None for row in lengths)


def test_supplied_route_type_and_constraint_basis_are_not_inferred_geometry():
    before = make_physical_model(); original = before.routes[0]
    route = fresh_request(original, route_type="supplied-cable", context=replace(original.context,route_type="supplied-cable"))
    report = build_revision_diff(before,changed_route(before,route))
    row = route_change(report)
    assert set(row.change_classes) == {"product", "provenance"}
    assert any(field.path == "route_type" and field.change_classes == ("product",) for field in row.field_changes)
    assert {row.status for row in report.quantity_deltas if row.category == "route_length"} == {"added","removed"}
    route = fresh_request(original,requirements=replace(original.requirements,clearance_m=.02))
    report = build_revision_diff(before,changed_route(before,route))
    assert set(route_change(report).change_classes) == {"product","provenance"}
    assert not report.quantity_deltas


def test_actual_od_change_is_geometry_with_auditable_variant_removal_and_addition():
    before = make_physical_model(); original = before.routes[0]
    route = fresh_request(original,requirements=replace(original.requirements,outside_diameter_m=.03))
    report = build_revision_diff(before,changed_route(before,route))
    assert set(route_change(report).change_classes) == {"geometry","provenance"}
    lines = [row for row in report.quantity_deltas if row.category == "route_length"]
    assert {row.status for row in lines} == {"removed","added"}
    assert all(row.delta is None for row in lines)
    assert {dict(row.variant)["outside_diameter_m"] for row in lines} == {.02,.03}
    physical = generate_physical_route_comparison(before,changed_route(before,route)).to_dict()["routes"][0]
    assert physical["path_changed"] is False and physical["outside_diameter_changed"] is True


def test_explicit_level_ownership_is_graph_without_changing_physical_length():
    before = make_physical_model(); route = replace(before.routes[0],level_id="level:new")
    after = replace(before,levels=(Level(id="level:new",elevation_m=0),),routes=(route,))
    report = build_revision_diff(before,after)
    assert route_change(report).change_classes == ("graph",)
    assert route_change(report).field_changes[0].path == "level_id"
    assert not report.quantity_deltas


@pytest.mark.parametrize("owner", ["route","joint","segment","requirements"])
def test_opaque_metadata_keys_and_missing_null_never_acquire_geometry_or_graph_classes(owner):
    before = make_physical_model(); original = before.routes[0]
    attributes = {"confidence":False,"id":"changed","provenance":{"route_id":"changed"},"new":None}
    if owner == "route": route = replace(original,attributes=attributes)
    elif owner == "joint": route = replace(original,joints=(replace(original.joints[0],attributes=attributes),*original.joints[1:]))
    elif owner == "segment": route = replace(original,segments=(replace(original.segments[0],attributes=attributes),))
    else: route = fresh_request(original,requirements=replace(original.requirements,attributes=attributes))
    report = build_revision_diff(before,changed_route(before,route))
    row = route_change(report)
    assert set(row.change_classes) == ({"attribute","provenance"} if owner == "requirements" else {"attribute"})
    changed = next(field for field in row.field_changes if field.path.endswith("attributes"))
    assert "new" not in changed.before and "new" in changed.after and changed.after["new"] is None
    assert changed.after["confidence"] is False
    assert not report.quantity_deltas


@pytest.mark.parametrize("owner", ["route","joint","segment"])
def test_nested_provenance_reordering_is_not_a_phantom_revision(owner):
    before = make_physical_model(); route = before.routes[0]
    entity = route if owner == "route" else route.joints[0] if owner == "joint" else route.segments[0]
    provenance = (entity.provenance[0],replace(entity.provenance[0],method="second source"))
    def with_records(records):
        revised = replace(entity,provenance=records)
        if owner == "route": return revised
        if owner == "joint": return replace(route,joints=(revised,*route.joints[1:]))
        return replace(route,segments=(revised,))
    report = build_revision_diff(changed_route(before,with_records(provenance)), changed_route(before,with_records(tuple(reversed(provenance)))))
    assert not report.changes and not report.quantity_deltas


def test_changed_source_is_reported_without_rebinding_or_implying_current_clearance():
    before = make_physical_model(); route = before.routes[0]
    after = replace(before,obstacles=(Obstacle(id="obstacle:new",geometry=Box3D(
        pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=1,y=1,z=1))),))
    binding = route.context.source_model_sha256
    report = build_revision_diff(before,after)
    assert not [row for row in report.changes if row.collection == "routes"]
    assert any(row.collection == "obstacles" and row.change == "added" for row in report.changes)
    assert after.routes[0].context.source_model_sha256 == binding
    comparison = generate_physical_route_comparison(before,after).to_dict()
    assert comparison["sources"]["before"]["sha256"] != comparison["sources"]["after"]["sha256"]
    assert comparison["routes"][0]["status"] == "unchanged"


def test_stale_request_binding_refuses_instead_of_being_repaired():
    before = make_physical_model()
    object.__setattr__(before.routes[0].context,"request_sha256","0"*64)
    with pytest.raises(ContractError,match="request|digest|binding"):
        build_revision_diff(before,before)
    assert before.routes[0].context.request_sha256 == "0"*64


def test_bounded_physical_admission_refuses_before_entity_document_work(monkeypatch):
    import oabm.qa.revision_diff as implementation
    attributes = {}
    for _ in range(80): attributes = {"nested":attributes}
    model = replace(make_physical_model(),attributes=attributes)
    def forbidden(*args,**kwargs):
        pytest.fail("unbounded source reached entity diff work")
    monkeypatch.setattr(implementation,"_entity_document",forbidden)
    with pytest.raises(ContractError,match="depth_limit"):
        build_revision_diff(model,model)


def test_complete_coordinate_comparability_remains_required():
    before = make_physical_model()
    after = replace(before,coordinate_system=replace(before.coordinate_system,true_north_radians=.1))
    with pytest.raises(ValueError,match="coordinate frames"):
        build_revision_diff(before,after)
    with pytest.raises(ValueError,match="different model identities"):
        build_revision_diff(before,replace(before,model_id="model:other"))


def test_actual_mixed_room_reports_all_scopes_once_and_exact_measurements(tmp_path):
    before, after = mixed_snapshots(); frozen = (before.to_json(),after.to_json())
    report = build_revision_diff(before,after)
    assert route_change(report).entity_id == "route:physical"
    assert route_change(report,"route:legacy").change_classes == ("geometry",)
    assert any(row.collection == "assembly_components" and "geometry" in row.change_classes for row in report.changes)
    assert any(row.collection == "building_assemblies" and "product" in row.change_classes for row in report.changes)
    lengths = [row for row in report.quantity_deltas if row.category == "route_length"]
    assert len(lengths) == 2 and all(row.status == "changed" for row in lengths)
    physical = next(row for row in lengths if dict(row.variant).get("geometry_kind") == "line_arc_v1")
    legacy = next(row for row in lengths if "geometry_kind" not in dict(row.variant))
    assert physical.delta == pytest.approx(.1+.1*math.pi,abs=1e-12)
    assert legacy.delta == pytest.approx(.5,abs=1e-12)
    assert (before.to_json(),after.to_json()) == frozen
    reordered = replace(after,routes=tuple(reversed(after.routes)),assembly_components=tuple(reversed(after.assembly_components)))
    assert build_revision_diff(before,reordered).to_json() == report.to_json()
    assert RevisionDiff.from_dict(json.loads(report.to_json())).to_json() == report.to_json()
    output = tmp_path/"actual"; written = create_example(output)
    assert (output/"revision.json").read_text() == written.to_json() == report.to_json()
    summary = json.loads((output/"SUMMARY.json").read_text())
    exact = summary["physical_exact_delta"]["length"]
    assert exact["straight_ticks"] == 100000000
    assert exact["pi_coefficient_ticks"] == {"numerator":100000000,"denominator":1}
    assert BuildingModel.load(output/"before.json").to_dict() == before.to_dict()
    assert not build_revision_diff(before,before).changes
