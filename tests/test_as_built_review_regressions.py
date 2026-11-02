"""Source-review regressions; execution remains pending the coordinated window."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import pytest

from oabm.model import (BuildingModel, CatalogProduct, ContractError, Point3, Provenance,
    RuleReference, Size3, SupportAssembly, SupportComponent, SupportInterface,
    SupportSelectionOutcome, upgrade_to_v1_3)
from oabm.pipeline.as_built import (apply_transaction, record_sha256, replay_history,
                                   snapshot, start_history, _sha)
from oabm.pipeline.as_built_delivery import _drawings, installed_materials
from oabm.pipeline import as_built_admission as admission
from oabm.quantities import extract_quantities

ROOT = Path(__file__).resolve().parents[1]


def inputs():
    spec = importlib.util.spec_from_file_location("as_built_review_example", ROOT / "examples/as_built_quick_edit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.synthetic_inputs()


def edit(model, collection, identity, record):
    old = next(row for row in model.to_dict()[collection] if row["id"] == identity)
    return {"collection": collection, "id": identity, "expected_sha256": record_sha256(old),
            "record": record, "evidence_ids": ["field:note"]}


def test_three_story_reaffirmation_hashes_retained_typed_parent_and_still_rejects_stale_records():
    spec = importlib.util.spec_from_file_location("as_built_typed_parent_example", ROOT / "examples/as_built_quick_edit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    history, request = module.synthetic_three_story_inputs()
    original_history = deepcopy(history)
    parent = replay_history(history)
    record = parent.to_dict()["routes"][0]
    # The real example deliberately constructs Point3(x=2, ...). Construction
    # accepts an int; retained typed JSON admission stores the coordinate as a
    # float. Their JSON hashes differ even though Python numeric equality holds.
    direct = Point3(x=2, y=0, z=0)
    assert type(direct.x) is int
    assert record["centerline"]["points"][2]["x"] == direct.x
    assert type(record["centerline"]["points"][2]["x"]) is float
    proposal = request["edits"][0]
    assert proposal["expected_sha256"] == record_sha256(record)
    assert record_sha256(proposal["record"]) == record_sha256(record)
    assert request["parent_sha256"] == _sha(snapshot(parent))
    revised = apply_transaction(history, request)
    current = replay_history(revised)
    assert current.routes[0].id == parent.routes[0].id
    assert current.routes[0].centerline == parent.routes[0].centerline
    assert revised["design_json"] == history["design_json"]
    assert history == original_history
    stale_record = deepcopy(record)
    stale_record["centerline"]["points"][2]["x"] += .25
    stale_request = deepcopy(request)
    stale_request["edits"][0]["expected_sha256"] = record_sha256(stale_record)
    with pytest.raises(ContractError, match="stale entity or unexpected new ID"):
        apply_transaction(history, stale_request)
    assert history == original_history


def test_cycles_and_deep_mutable_inputs_are_controlled_before_serialization():
    history, request = inputs()
    model = replay_history(history)
    model.attributes["cycle"] = model.attributes
    with pytest.raises(ContractError, match="admission"):
        snapshot(model)
    request["cycle"] = request
    with pytest.raises(ContractError, match="admission"):
        apply_transaction(history, request)
    deep = {}
    current = deep
    for _ in range(admission.MAX_DEPTH + 1):
        current["child"] = {}
        current = current["child"]
    with pytest.raises(ContractError, match="admission"):
        record_sha256(deep)


def test_json_and_file_limits_precede_parser_allocations(tmp_path, monkeypatch):
    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON parser reached before resource admission")
    monkeypatch.setattr(admission.json, "loads", must_not_parse)
    for raw in (b" " * (admission.REQUEST_BYTES + 1), b"[" * 65 + b"]" * 65):
        with pytest.raises(ContractError, match="admission"):
            admission.decode(raw, byte_limit=admission.REQUEST_BYTES)
    path = tmp_path / "oversize.json"
    path.write_bytes(b" " * (admission.REQUEST_BYTES + 1))
    with pytest.raises(ContractError, match="admission"):
        admission.read_bytes(path, byte_limit=admission.REQUEST_BYTES)
    with pytest.raises(ContractError, match="admission"):
        admission.read_bytes(tmp_path, byte_limit=admission.REQUEST_BYTES)


@pytest.mark.parametrize("field,maximum", [("edits", admission.MAX_EDITS),
    ("evidence", admission.MAX_EVIDENCE), ("installed_assertions", admission.MAX_ASSERTIONS)])
def test_per_transaction_work_is_bounded_before_replay(field, maximum, monkeypatch):
    history, request = inputs()
    request[field] = [None] * (maximum + 1)
    from oabm.pipeline import as_built
    monkeypatch.setattr(as_built, "replay_history", lambda value: pytest.fail("replayed before request admission"))
    with pytest.raises(ContractError, match="admission"):
        apply_transaction(history, request)


def test_retained_nested_json_and_cumulative_work_are_preflighted(monkeypatch):
    history, request = inputs()
    deep = deepcopy(history)
    deep["design_json"] = "[" * 65 + "]" * 65
    with pytest.raises(ContractError, match="admission"):
        replay_history(deep)
    retained = {"request": request, "result_json": history["design_json"], "result_sha256": "0" * 64}
    history["transactions"] = [retained, deepcopy(retained)]
    monkeypatch.setattr(admission, "MAX_HISTORY_EDITS", len(request["edits"]))
    with pytest.raises(ContractError, match="cumulative history"):
        replay_history(history)


@pytest.mark.parametrize("identity", [[], {}, 1, True, None, "", "   "])
def test_malformed_transaction_ids_are_controlled_in_requests_and_history(identity):
    history, request = inputs()
    retained = apply_transaction(history, request)
    malformed = deepcopy(request)
    malformed["transaction_id"] = deepcopy(identity)
    with pytest.raises(ContractError, match="transaction_id must be a nonempty string"):
        apply_transaction(history, malformed)
    retained["transactions"][0]["request"]["transaction_id"] = deepcopy(identity)
    with pytest.raises(ContractError, match="transaction_id must be a nonempty string"):
        replay_history(retained)


def test_unchanged_port_and_route_cannot_validate_translated_device():
    history, request = inputs()
    baseline = replay_history(history)
    port = next(port for port in baseline.ports if port.owner_id == "device:evse")
    port_edit = next(row for row in request["edits"] if row["collection"] == "ports")
    port_edit["record"] = next(row for row in baseline.to_dict()["ports"] if row["id"] == port.id)
    route_edit = next(row for row in request["edits"] if row["collection"] == "routes")
    route_edit["record"]["centerline"]["points"][-1] = deepcopy(port_edit["record"]["pose"]["position"])
    request["installed_assertions"] = []
    with pytest.raises(ContractError, match="contradict rigid device movement"):
        apply_transaction(history, request)


def test_invariant_absolute_connection_position_can_survive_rigid_rotation():
    history, request = inputs()
    model = replay_history(history)
    document = model.to_dict()
    device = deepcopy(document["electrical_devices"][0])
    port = deepcopy(next(row for row in document["ports"] if row["owner_id"] == device["id"]))
    device["pose"]["rotation"] = {"x": 0, "y": 0, "z": 1, "w": 0}
    port["pose"]["rotation"] = deepcopy(device["pose"]["rotation"])
    route = document["routes"][0]
    request.update(edits=[edit(model, "electrical_devices", device["id"], device),
        edit(model, "ports", port["id"], port), edit(model, "routes", route["id"], route)],
        method_reconciliations=[], installed_assertions=[])
    current = replay_history(apply_transaction(history, request))
    actual = next(row for row in current.ports if row.id == port["id"])
    original = next(row for row in model.ports if row.id == port["id"])
    assert actual.pose.position == original.pose.position
    assert actual.pose.rotation != original.pose.rotation


def supported_model(model, supported_id):
    model = upgrade_to_v1_3(model)
    level = model.levels[0].id
    pose = model.ports[0].pose
    basis = RuleReference(authority="synthetic", revision="1", rule_id="support")
    product = CatalogProduct(catalog_id="synthetic", catalog_revision="1", product_id="clip")
    assembly = SupportAssembly(id="support:assembly", intent_id="intent:support", assembly_type="clip",
        level_id=level, supported_object_ids=(supported_id,), component_ids=("support:clip",),
        interface_ids=("support:interface",), basis=basis)
    component = SupportComponent(id="support:clip", assembly_id=assembly.id, part_key="part:clip",
        level_id=level, category="support", component_type="clip", product=product,
        pose=pose, size=Size3(x=.1, y=.1, z=.1))
    interface = SupportInterface(id="support:interface", assembly_id=assembly.id, interface_key="interface:clip",
        level_id=level, host_id=model.walls[0].id, substrate="concrete", pose=pose, component_ids=(component.id,))
    outcome = SupportSelectionOutcome(id="support:outcome", intent_id=assembly.intent_id,
        supported_object_ids=assembly.supported_object_ids, status="selected", selected_assembly_id=assembly.id,
        requested_level_id=level, requested_basis=basis)
    return replace(model, support_assemblies=(assembly,), support_components=(component,),
                   support_interfaces=(interface,), support_selection_outcomes=(outcome,))


@pytest.mark.parametrize("supported_id", ["route:panel-evse", "device:evse"])
def test_changed_supported_objects_fail_without_publishing_stale_hardware(supported_id):
    history, request = inputs()
    model = supported_model(replay_history(history), supported_id)
    history = start_history(model)
    request.update(design_sha256=history["design_sha256"], parent_sha256=_sha(snapshot(model)), installed_assertions=[])
    original = deepcopy(history)
    with pytest.raises(ContractError, match="support reconciliation unavailable"):
        apply_transaction(history, request)
    assert history == original


def test_prior_support_installation_assertion_cannot_survive_a_reroute():
    initial, request = inputs()
    model = supported_model(replay_history(initial), "route:panel-evse")
    history = start_history(model)
    document = model.to_dict()
    route = document["routes"][0]
    seed = {**deepcopy(request), "transaction_id": "field:confirmed-support",
        "design_sha256": history["design_sha256"], "parent_sha256": _sha(snapshot(model)),
        "edits": [edit(model, "routes", route["id"], route)], "method_reconciliations": [],
        "installed_assertions": [{"collection": collection, "id": row["id"],
            "record_sha256": record_sha256(row), "evidence_ids": ["field:note"]}
            for collection in ("support_components", "support_assemblies") for row in document[collection]]}
    history = apply_transaction(history, seed)
    current = replay_history(history)
    assert sum(row["quantity"] for row in installed_materials(history)["installed_items"]
               if row["category"] == "support_component") == 1
    request.update(design_sha256=history["design_sha256"], parent_sha256=_sha(snapshot(current)), installed_assertions=[])
    current_document = current.to_dict()
    for proposed in request["edits"]:
        old = next((row for row in current_document[proposed["collection"]] if row["id"] == proposed["id"]), None)
        if old is not None:
            proposed["expected_sha256"] = record_sha256(old)
            if proposed["record"] is not None:
                proposed["record"]["provenance"] = deepcopy(old["provenance"])
    retained = deepcopy(history)
    with pytest.raises(ContractError, match="support reconciliation unavailable"):
        apply_transaction(history, request)
    assert history == retained


def test_one_of_two_identical_boxes_is_installed_before_aggregation():
    history, template = inputs()
    model = replay_history(history)
    evse = model.electrical_devices[0]
    common = (Provenance(source_kind="synthetic", source_id="field:two-boxes", derivation="user"),)
    first = replace(model.electrical_boxes[0], provenance=common)
    second = replace(first, id="box:identical-second", intent_id="intent:identical-second",
                     occupant_ids=(evse.id,), pose=evse.pose)
    old_outcome = next(row for row in model.box_selection_outcomes if evse.id in row.occupant_ids)
    second_outcome = replace(old_outcome, intent_id=second.intent_id, status="selected",
        selected_box_id=second.id, requested_product=second.product, reason_codes=())
    model = replace(model, electrical_boxes=(first, second), box_selection_outcomes=tuple(
        second_outcome if row.id == old_outcome.id else row for row in model.box_selection_outcomes))
    assert [row.quantity for row in extract_quantities(model).items if row.category == "electrical_box"] == [2]
    history = start_history(model)
    spare = next(row for row in model.to_dict()["electrical_devices"] if row["id"] == "device:spare")
    request = {**template, "design_sha256": history["design_sha256"], "parent_sha256": _sha(snapshot(model)),
        "edits": [edit(model, "electrical_devices", spare["id"], spare)], "method_reconciliations": [],
        "installed_assertions": [{"collection": "electrical_boxes", "id": first.id,
            "record_sha256": record_sha256(model.to_dict()["electrical_boxes"][0]), "evidence_ids": ["field:note"]}]}
    materials = installed_materials(apply_transaction(history, request))
    installed = [row for row in materials["installed_items"] if row["category"] == "electrical_box"]
    unresolved = [row for row in materials["unresolved_items"] if row["category"] == "electrical_box"]
    assert sum(row["quantity"] for row in installed) == 1
    assert sum(row["quantity"] for row in unresolved) == 1
    assert installed[0]["variant"] == unresolved[0]["variant"]
    assert installed[0]["unit"] == unresolved[0]["unit"] == "ea"
    assert installed[0]["source_entity_ids"] == [first.id]
    assert unresolved[0]["source_entity_ids"] == [second.id]


def two_level_model():
    base = BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")
    def raised(pose):
        return replace(pose, position=replace(pose.position, z=pose.position.z + 3))
    level = replace(base.levels[0], id="level:upper", name="Upper", elevation_m=3)
    equipment = replace(base.electrical_equipment[0], id="equipment:upper", level_id=level.id,
        pose=raised(base.electrical_equipment[0].pose), host_id=None, space_id=None)
    device = replace(base.electrical_devices[0], id="device:upper", level_id=level.id,
        pose=raised(base.electrical_devices[0].pose), host_id=None, space_id=None)
    ids = {row.id: row.id + ":upper" for row in base.ports}
    ports = tuple(replace(row, id=ids[row.id], pose=raised(row.pose),
        owner_id=equipment.id if row.owner_id == base.electrical_equipment[0].id else device.id) for row in base.ports)
    old_route = base.routes[0]
    route = replace(old_route, id="route:upper", start_port_id=ids[old_route.start_port_id],
        end_port_id=ids[old_route.end_port_id], fitting_ids=tuple(identity + ":upper" for identity in old_route.fitting_ids),
        centerline=replace(old_route.centerline, points=tuple(replace(point, z=point.z + 3) for point in old_route.centerline.points)))
    fittings = tuple(replace(row, id=row.id + ":upper", route_id=route.id, pose=raised(row.pose)) for row in base.route_fittings)
    circuit = replace(base.circuits[0], id="circuit:upper", source_port_id=ids[base.circuits[0].source_port_id],
        load_port_ids=tuple(ids[identity] for identity in base.circuits[0].load_port_ids), route_ids=(route.id,))
    conductors = tuple(replace(row, id=row.id + ":upper", circuit_id=circuit.id, route_ids=(route.id,)) for row in base.conductors)
    return replace(base, levels=(*base.levels, level), electrical_equipment=(*base.electrical_equipment, equipment),
        electrical_devices=(*base.electrical_devices, device), ports=(*base.ports, *ports),
        routes=(*base.routes, route), route_fittings=(*base.route_fittings, *fittings),
        circuits=(*base.circuits, circuit), conductors=(*base.conductors, *conductors))


def test_actual_two_level_drawings_do_not_repeat_routes_or_fittings():
    model = two_level_model()
    outputs = _drawings(model, "field:two-levels")
    views = json.loads(outputs["drawings/plans.json"])["views"]
    by_level = {dict(view["metadata"])["level_id"]: view for view in views}
    ground = {identity for primitive in by_level[model.levels[0].id]["primitives"] for identity in primitive["source_ids"]}
    upper = {identity for primitive in by_level["level:upper"]["primitives"] for identity in primitive["source_ids"]}
    assert model.routes[0].id in ground and model.routes[0].id not in upper
    assert "route:upper" in upper and "route:upper" not in ground
    assert set(model.routes[0].fitting_ids) <= ground
    assert not set(model.routes[0].fitting_ids) & upper
    assert set(model.routes[1].fitting_ids) <= upper
    assert not set(model.routes[1].fitting_ids) & ground
    assert "drawings/plan-1.svg" in outputs and "drawings/plan-2.svg" in outputs


def test_actual_three_story_riser_is_present_and_clipped_on_intermediate_plan():
    base = BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")
    middle = replace(base.levels[0], id="level:middle", name="Middle", elevation_m=3)
    top = replace(base.levels[0], id="level:third", name="Third", elevation_m=6)
    device = replace(base.electrical_devices[0], level_id=top.id, host_id=None, space_id=None,
        pose=replace(base.electrical_devices[0].pose, position=Point3(x=5.2, y=.2, z=7.2)))
    ports = tuple(replace(port, pose=device.pose) if port.owner_id == device.id else port for port in base.ports)
    route = replace(base.routes[0], fitting_ids=(), name=None,
        centerline=replace(base.routes[0].centerline, points=(
            base.routes[0].centerline.points[0], Point3(x=.2, y=.2, z=2.5),
            Point3(x=2, y=.2, z=2.5), Point3(x=2, y=.2, z=6.5),
            Point3(x=5.2, y=.2, z=6.5), device.pose.position)))
    model = replace(base, levels=(*base.levels, middle, top), electrical_devices=(device,),
                    ports=ports, routes=(route,), route_fittings=())
    outputs = _drawings(model, "field:three-stories")
    views = json.loads(outputs["drawings/plans.json"])["views"]
    by_level = {dict(view["metadata"])["level_id"]: view for view in views}
    for level in model.levels:
        assert any(route.id in primitive["source_ids"] for primitive in by_level[level.id]["primitives"])
    middle_markers = [primitive for primitive in by_level[middle.id]["primitives"]
        if primitive["kind"] == "symbol" and primitive["symbol"] == "route-vertical-projection"]
    assert len(middle_markers) == 1
    assert middle_markers[0]["points"] == [{"x": 2, "y": .2}]
    metadata = dict(middle_markers[0]["metadata"])
    assert float(metadata["min_z_m"]) == pytest.approx(3)
    assert float(metadata["max_z_m"]) == pytest.approx(5.8)
    assert any(primitive.get("text") == "Vertical route" for primitive in by_level[middle.id]["primitives"])
    assert len([name for name in outputs if name.startswith("drawings/plan-") and name.endswith(".svg")]) == 3


def test_quantity_partition_preserves_original_bid_groups_and_variants():
    model = two_level_model()
    groups = {"UPPER": [model.routes[1].id]}
    ordinary = extract_quantities(model, groups=groups)
    lower = extract_quantities(model, groups=groups,
        contribution_filter=lambda row: model.routes[0].id in row.source_entity_ids)
    upper = extract_quantities(model, groups=groups,
        contribution_filter=lambda row: model.routes[1].id in row.source_entity_ids)
    assert all(row.group == "UPPER" for row in upper.items if row.category == "route_length")
    assert all(row.group is None for row in lower.items if row.category == "route_length")
    assert sum(row.quantity for row in (*lower.items, *upper.items) if row.category == "route_length") == pytest.approx(
        sum(row.quantity for row in ordinary.items if row.category == "route_length"))
