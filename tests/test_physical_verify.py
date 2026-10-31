"""Live supplied-route verification, synthetic current bound source."""
from dataclasses import replace, FrozenInstanceError
from fractions import Fraction
import json
import pytest

from oabm.model import (Box3D, Point3, Pose, Size3, Obstacle, PhysicalGeometryCoverage,
                       compute_physical_request_sha256, compute_physical_source_sha256)
from oabm.routing.physical_verify import verify_physical_route
from physical_route_fixture import make_physical_source, make_physical_route, supplied


def rebind(route, **changes):
    context = replace(route.context, **changes)
    return replace(route,context=replace(context,request_sha256=
        compute_physical_request_sha256(context,route.requirements)))


def obstacle_source(x=1,y=0):
    source=make_physical_source()
    obstacle=Obstacle(id="obstacle:test",geometry=Box3D(
        pose=Pose(position=Point3(x=x,y=y,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    source=replace(source,obstacles=(obstacle,))
    route=make_physical_route(source)
    row=PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision="direct_blocker",
        blocker_entity_ids=(obstacle.id,),rationale="Supplied exact synthetic hard box.",
        provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    return source,rebind(route,coverage=(row,))


def test_live_hashes_do_not_certify_colliding_route_and_inputs_unchanged():
    source,route=obstacle_source()
    source_before=source.to_json()
    candidate_before=replace(source,routes=(route,)).to_json()
    result=verify_physical_route(source,route)
    assert result.status == "rejected_route"
    assert result.reason == "obstacle_contact_or_overlap"
    assert result.to_dict()["authority"]["source_model_sha256"] == compute_physical_source_sha256(source)
    assert source.to_json() == source_before
    assert replace(source,routes=(route,)).to_json() == candidate_before


def test_stale_source_fails_before_any_geometric_work():
    source=make_physical_source();route=make_physical_route(source)
    result=verify_physical_route(replace(source,name="changed"),route)
    assert result.status == "invalid_input"
    assert result.reason == "stale_source_binding"
    assert "work" not in result.to_dict()


def test_incomplete_coverage_is_typed_unsupported_not_collision():
    source,route=obstacle_source()
    result=verify_physical_route(source,rebind(route,coverage=()))
    assert result.status == "unsupported_input"
    assert result.reason == "incomplete_coverage"


def test_no_implicit_endpoint_owner_or_obstacle_exemption():
    source,route=obstacle_source(x=0)
    assert verify_physical_route(source,route).status == "rejected_route"


def test_budget_limit_never_partial_success():
    source,route=obstacle_source(y=1)
    limits=replace(route.context.execution_limits,total_refinement_cells=1)
    result=verify_physical_route(source,rebind(route,execution_limits=limits))
    assert result.status == "resource_limit"
    assert result.to_dict()["work"]["charged"] <= 1


def test_original_raw_source_checked_before_binary_float_can_hide_offgrid():
    source,route=obstacle_source()
    text=source.to_json(indent=None).replace('"clearance_m": 0.0','"clearance_m": 0.00000000000000001')
    # Use parsed exact field rather than rely on default whitespace conventions.
    document=json.loads(source.to_json())
    document["obstacles"][0]["clearance_m"]=123.456789
    text=json.dumps(document).replace("123.456789","0.00000000000000001")
    result=verify_physical_route(source,route,source_json=text)
    assert result.status == "unsupported_input"
    assert result.reason == "numeric_grid"


def test_receipt_immutable_and_repeatable_for_identical_captured_inputs():
    source,route=obstacle_source()
    first=verify_physical_route(source,route)
    assert first.to_json() == verify_physical_route(source,route).to_json()
    parsed=first.to_dict(); parsed["status"]="verified_route"
    assert first.status == "rejected_route"
    with pytest.raises(FrozenInstanceError):
        first.status="verified_route"


def test_capture_source_and_candidate_deep_mutations_refuse_without_writes():
    source=make_physical_source();route=make_physical_route(source)
    route.attributes["shadow"]={"a":[]}
    route.attributes["shadow"]["a"].append(route.attributes)
    result=verify_physical_route(source,route)
    assert result.status == "invalid_input"
    assert result.reason == "cyclic_input"


def test_live_safe_line_verified_and_raw_admission_distinguished():
    source=make_physical_source(); route=make_physical_route(source)
    first=verify_physical_route(source,route)
    assert first.status == "verified_route", first.to_json()
    assert first.to_json() == verify_physical_route(source,route).to_json()
    raw=verify_physical_route(source,route,source_json=source.to_json())
    assert raw.status == "verified_route"
    assert raw.to_dict()["authority"]["source_admission"] == "raw_json_lexemes"
    assert first.to_dict()["authority"]["source_admission"] == "canonical_python_snapshot"


def test_live_all_cardinal_arc_rotations_verified_with_exact_self_proof():
    from test_physical_route_contract import make_arc_model, rotations
    for transform in rotations():
        model=make_arc_model(transform)
        source=replace(model,routes=())
        route=model.routes[0]
        domain=replace(route.context.allowed_domain,volumes=(Box3D(
            pose=Pose(position=Point3(x=0,y=0,z=0)),size=Size3(x=6,y=6,z=6)),))
        route=rebind(route,allowed_domain=domain)
        result=verify_physical_route(source,route)
        assert result.status == "verified_route",result.to_json()
        assert result.to_json() == verify_physical_route(source,route).to_json()


def test_live_safe_obstacle_is_actually_checked_not_hash_only():
    source,route=obstacle_source(y=1)
    result=verify_physical_route(source,route)
    assert result.status == "verified_route",result.to_json()
    assert result.to_dict()["scope"]["checked_source_ids"] == ["obstacle:test"]
    assert result.to_dict()["work"]["charged_by_kind"]["obstacle_pair"] == 1


def test_equivalent_obstacle_permutations_preserve_charged_work_and_verdict():
    import itertools
    source,route=obstacle_source(y=1)
    obstacles=tuple(replace(source.obstacles[0],id="obstacle:"+str(i),
        geometry=Box3D(pose=Pose(position=Point3(x=1,y=1+i*.1,z=0)),
                       size=Size3(x=.1,y=.1,z=.1))) for i in range(3))
    outcomes=[]
    for order in itertools.permutations(obstacles):
        current=replace(source,obstacles=order)
        coverage=tuple(replace(route.context.coverage[0],source_entity_id=o.id,
                              blocker_entity_ids=(o.id,)) for o in order)
        candidate=rebind(route,source_model_sha256=compute_physical_source_sha256(current),coverage=coverage)
        result=verify_physical_route(current,candidate)
        assert result.status == "verified_route",result.to_json()
        work=result.to_dict()["work"]
        outcomes.append((result.status,work["charged"],json.dumps(work["charged_by_kind"],sort_keys=True),work["trace_sha256"]))
    assert len(set(outcomes)) == 1


def test_current_source_change_after_success_requires_new_live_binding():
    source=make_physical_source();route=make_physical_route(source)
    receipt=verify_physical_route(source,route)
    assert receipt.status == "verified_route"
    source.attributes["new_fact"]=True
    result=verify_physical_route(source,route)
    assert result.status == "invalid_input"
    assert result.reason == "stale_source_binding"
    assert receipt.status == "verified_route" # Historical captured inputs only.


def test_original_snapshot_mismatch_is_not_silently_replaced():
    source,route=obstacle_source(y=1)
    altered=replace(source,name="different snapshot")
    result=verify_physical_route(source,route,source_json=altered.to_json())
    assert result.status == "invalid_input"
    assert result.reason == "raw_source_mismatch"


def test_geometry_unspecified_scope_includes_nonendpoint_owners_separately():
    source=make_physical_source()
    source=replace(source,electrical_devices=(*source.electrical_devices,
        replace(source.electrical_devices[0],id="device:no-port")))
    route=make_physical_route(source)
    result=verify_physical_route(source,route)
    assert result.status == "verified_route",result.to_json()
    scope=result.to_dict()["scope"]
    assert scope["geometry_unspecified_source_ids"] == ["device:a","device:b","device:no-port"]
    assert scope["excluded_source_ids"] == []
    assert scope["checked_source_ids"] == []
    assert result.to_dict()["work"]["charged_by_kind"]["geometry_scope_scan"] == 3


def test_geometry_unspecified_disclosure_limit_is_not_success():
    source=make_physical_source()
    devices=(*source.electrical_devices,*(replace(source.electrical_devices[0],
        id="device:extra-"+str(i)) for i in range(9999)))
    source=replace(source,electrical_devices=devices)
    route=make_physical_route(source)
    result=verify_physical_route(source,route)
    assert result.status == "resource_limit"
    # Capture may independently reach a stricter declared memory cap first.
    assert result.reason in {"scope_disclosure_limit","capture_node_limit","capture_byte_limit"}


def test_limit_receipt_has_bounded_cursor_unknown_remaining_and_no_silent_omission():
    source,route=obstacle_source(y=1)
    limits=replace(route.context.execution_limits,total_refinement_cells=1)
    result=verify_physical_route(source,rebind(route,execution_limits=limits))
    report=result.to_dict()
    assert report["algorithm_profile"] == "physical-route-verifier-v1"
    assert report["remaining_count"] == "unknown"
    assert report["unfinished_cursor"]["kind"] == "geometry_scope_scan"
    assert report["diagnostics_total"] == len(report["diagnostics"])+report["diagnostics_omitted"]


def test_receipt_counts_omitted_completed_diagnostics_exactly():
    from oabm.routing.physical_verify import _finish
    from oabm.routing.physical_verify_types import WorkLedger
    ledger=WorkLedger(1000)
    for index in range(300):
        ledger.record("synthetic_completed_check",(str(index),))
    report=_finish("verified_route","test_only",ledger,{},{}).to_dict()
    assert len(report["diagnostics"]) == 256
    assert report["diagnostics_total"] == 300
    assert report["diagnostics_omitted"] == 44


def test_decisive_witness_reserved_when_normal_diagnostics_are_full():
    from oabm.routing.physical_verify import _finish
    from oabm.routing.physical_verify_types import WorkLedger, VerificationStop
    ledger=WorkLedger(1000)
    for index in range(300):
        ledger.record("completed",(str(index),))
    stop=VerificationStop("rejected_route","self_contact_or_overlap",("segment:a","segment:b"),
                          (("witness_x_m",Fraction(1,10**9)),))
    report=_finish(stop.status,stop.reason,ledger,{}, {},stop).to_dict()
    assert len(report["diagnostics"]) == 256
    assert report["diagnostics"][0]["kind"] == stop.reason
    assert report["diagnostics"][0]["exact"]["witness_x_m"] == {"numerator":"1","denominator":"1000000000"}
    assert report["diagnostics_total"] == 301
    assert report["diagnostics_omitted"] == 45
