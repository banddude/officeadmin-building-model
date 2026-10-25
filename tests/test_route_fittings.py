"""Synthetic-only route fitting catalog, topology and producer tests."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from oabm.electrical.catalog_manifest import CatalogManifest, VERSION_V2
from oabm.electrical.fittings import (FittingIntent, FittingPortBinding, FittingRule,
    FittingRulePermission, select_route_fittings)
from oabm.model import (BuildingModel, BoxSelectionOutcome, CatalogProduct, ContractError,
    ElectricalBox, ElectricalDevice, FittingSelection, Level, Point3, Polyline3D, Port,
    Pose, Provenance, Quaternion, RacewaySelection, RacewaySelectionOutcome, Route,
    RouteFitting, RuleReference, Size3, Vector3, Wall, upgrade_to_v1_5)

ROUTE_PRODUCT = CatalogProduct(catalog_id="raceway-catalog", catalog_revision="r1", product_id="emt-1")
BOX_PRODUCT = CatalogProduct(catalog_id="box-catalog", catalog_revision="b1", product_id="box-1")
COUPLING_PRODUCT = CatalogProduct(catalog_id="fitting-catalog", catalog_revision="f1", product_id="coupling-1")
CONNECTOR_PRODUCT = CatalogProduct(catalog_id="fitting-catalog", catalog_revision="f1", product_id="connector-1")
RULE = RuleReference(authority="synthetic-caller", revision="r1", rule_id="fitting-test")


def _evidence(fields):
    return [{"field": field, "source_id": "synthetic-source", "derivation": "observed", "confidence": .9}
            for field in fields]


def _product_entry(product_id, fitting_type, interfaces, pairs):
    fields = {"route_type": "emt", "fitting_type": fitting_type, "size": None}
    evidence = _evidence(["product", "route_type", "fitting_type"])
    return {"product_id": product_id, "manufacturer": "Synthetic", "part_number": product_id,
        "kind": "fitting", "description": "Synthetic fitting fixture", "geometry_basis": "metadata_only",
        "assets": [], "fields": fields, "evidence": evidence,
        "interfaces": interfaces, "mating_pairs": pairs}


def _interface(key, role, method="compression"):
    fields = {"key": key, "role": role, "connection_method": method,
        "permitted_wiring_methods": ["raceway"], "nominal_designation": None,
        "actual_accepted_od_m": None, "dry_location": None, "wet_location": None,
        "raintight": None, "listing_refs": []}
    fields["evidence"] = _evidence(["key", "role", "connection_method", "permitted_wiring_methods"])
    return fields


def _pair(identity, interface_key, mating_product, mating_interface_key):
    return {"id": identity, "interface_key": interface_key,
        "mating_product": {"catalog_id": mating_product.catalog_id,
            "catalog_revision": mating_product.catalog_revision, "product_id": mating_product.product_id},
        "mating_interface_key": mating_interface_key,
        "evidence": _evidence(["id", "interface_key", "mating_product", "mating_interface_key"])}


def catalog_manifest():
    coupling_interfaces = [_interface("end_a", "raceway"), _interface("end_b", "raceway")]
    connector_interfaces = [_interface("raceway_end", "raceway"), _interface("enclosure_end", "enclosure")]
    document = {"version": VERSION_V2, "catalog_id": "fitting-catalog", "catalog_revision": "f1",
        "sources": [{"id": "synthetic-source", "url": "https://example.org/synthetic",
            "title": "Synthetic test source", "revision": None, "locator": "test-only",
            "retrieved_at": "2026-01-01", "sha256": "a" * 64}],
        "entries": [
            _product_entry("coupling-1", "coupling", coupling_interfaces,
                [_pair("coupling-a-route", "end_a", ROUTE_PRODUCT, "run_end"),
                 _pair("coupling-b-route", "end_b", ROUTE_PRODUCT, "run_end")]),
            _product_entry("connector-1", "connector", connector_interfaces,
                [_pair("connector-route", "raceway_end", ROUTE_PRODUCT, "run_end"),
                 _pair("connector-box", "enclosure_end", BOX_PRODUCT, "entry")]),
        ]}
    return CatalogManifest(json.dumps(document).encode())


def rule_for(product, fitting_type, *, check_actual_od=False, required_environment=(), required_listing_refs=()):
    evidence = Provenance(source_kind="caller", source_id="synthetic-rule", derivation="user",
        scope_paths=("reference", "permitted_products", "permitted_topologies", "check_actual_od",
                     "required_environment", "required_listing_refs"))
    return FittingRule(reference=RULE, permitted_products=(product,),
        permitted_topologies=(FittingRulePermission(route_type="emt", fitting_type=fitting_type),),
        check_actual_od=check_actual_od, required_environment=tuple(required_environment),
        required_listing_refs=tuple(required_listing_refs), provenance=(evidence,))


def build_model(fitting_type="coupling", *, with_box=False):
    start = Point3(x=0.0, y=0.0, z=0.0)
    finish = Point3(x=10.0, y=0.0, z=0.0)
    fitting_pos = Point3(x=3.5, y=0.0, z=0.0) if not with_box else Point3(x=9.0, y=0.0, z=0.0)
    route = Route(id="route:emt", route_type="emt", start_port_id="port:start", end_port_id="port:end",
        centerline=Polyline3D(points=(start, finish)), fitting_ids=("fitting:one",))
    fitting = RouteFitting(id="fitting:one", route_id=route.id, fitting_type=fitting_type,
        pose=Pose(position=fitting_pos))
    devices = (ElectricalDevice(id="device:start", device_type="test", pose=Pose(position=start)),
               ElectricalDevice(id="device:end", device_type="test", pose=Pose(position=finish)),
        ElectricalDevice(id="device:box-load", device_type="test", pose=Pose(position=Point3(x=9.0, y=0.0, z=0.0))))
    points = {}
    if not with_box:
        points.update({
            "port:fit-a": Port(id="port:fit-a", owner_id=fitting.id, domain="electrical", role="fitting",
                pose=Pose(position=Point3(x=3.0, y=0.0, z=0.0)), direction=Vector3(x=-1.0, y=0.0, z=0.0),
                connected_port_ids=("port:route-a",)),
            "port:route-a": Port(id="port:route-a", owner_id=route.id, domain="electrical", role="run_end",
                pose=Pose(position=Point3(x=3.0, y=0.0, z=0.0)), direction=Vector3(x=1.0, y=0.0, z=0.0),
                connected_port_ids=("port:fit-a",)),
            "port:fit-b": Port(id="port:fit-b", owner_id=fitting.id, domain="electrical", role="fitting",
                pose=Pose(position=Point3(x=4.0, y=0.0, z=0.0)), direction=Vector3(x=1.0, y=0.0, z=0.0),
                connected_port_ids=("port:route-b",)),
            "port:route-b": Port(id="port:route-b", owner_id=route.id, domain="electrical", role="run_end",
                pose=Pose(position=Point3(x=4.0, y=0.0, z=0.0)), direction=Vector3(x=-1.0, y=0.0, z=0.0),
                connected_port_ids=("port:fit-b",)),
        })
        bindings = (FittingPortBinding(interface_key="end_a", mating_interface_key="run_end",
                    fitting_port_id="port:fit-a", counterpart_port_id="port:route-a"),
                    FittingPortBinding(interface_key="end_b", mating_interface_key="run_end",
                    fitting_port_id="port:fit-b", counterpart_port_id="port:route-b"))
        fitting_product = COUPLING_PRODUCT
        interface_ids = ("end_a", "end_b")
        roles = ("raceway", "raceway")
    else:
        level = Level(id="level:one", elevation_m=0.0)
        wall = Wall(id="wall:one", level_id=level.id,
            centerline=Polyline3D(points=(Point3(x=9.0, y=-1.0, z=0.0), Point3(x=9.0, y=1.0, z=0.0))),
            thickness_m=.1, height_m=3.0)
        box = ElectricalBox(id="box:one", name="Synthetic box", intent_id="box-intent:one",
            box_type="test-box", pose=Pose(position=fitting_pos), size=Size3(x=.4, y=.4, z=.4),
            level_id=level.id, host_id=wall.id, mounting="surface", occupant_ids=("device:box-load",),
            product=BOX_PRODUCT)
        box_outcome = BoxSelectionOutcome(id="box-outcome:one", intent_id=box.intent_id,
            occupant_ids=box.occupant_ids, status="selected", selected_box_id=box.id,
            requested_product=BOX_PRODUCT)
        points.update({
            "port:fit-r": Port(id="port:fit-r", owner_id=fitting.id, domain="electrical", role="fitting",
                pose=Pose(position=fitting_pos), direction=Vector3(x=-1.0, y=0.0, z=0.0), connected_port_ids=("port:route-r",)),
            "port:route-r": Port(id="port:route-r", owner_id=route.id, domain="electrical", role="run_end",
                pose=Pose(position=fitting_pos), direction=Vector3(x=1.0, y=0.0, z=0.0), connected_port_ids=("port:fit-r",)),
            "port:fit-box": Port(id="port:fit-box", owner_id=fitting.id, domain="electrical", role="fitting",
                pose=Pose(position=fitting_pos), direction=Vector3(x=1.0, y=0.0, z=0.0), connected_port_ids=("port:box-entry",)),
            "port:box-entry": Port(id="port:box-entry", owner_id=box.id, domain="electrical", role="entry",
                pose=Pose(position=fitting_pos), direction=Vector3(x=-1.0, y=0.0, z=0.0), connected_port_ids=("port:fit-box",)),
        })
        bindings = (FittingPortBinding(interface_key="raceway_end", mating_interface_key="run_end",
                    fitting_port_id="port:fit-r", counterpart_port_id="port:route-r"),
                    FittingPortBinding(interface_key="enclosure_end", mating_interface_key="entry",
                    fitting_port_id="port:fit-box", counterpart_port_id="port:box-entry"))
        fitting_product = CONNECTOR_PRODUCT
        interface_ids = ("raceway_end", "enclosure_end")
        roles = ("raceway", "enclosure")

    points["port:start"] = Port(id="port:start", owner_id="device:start", domain="electrical", role="outlet",
        pose=Pose(position=start), direction=Vector3(x=1.0, y=0.0, z=0.0))
    points["port:end"] = Port(id="port:end", owner_id="device:end", domain="electrical", role="inlet",
        pose=Pose(position=finish), direction=Vector3(x=-1.0, y=0.0, z=0.0))
    route_product = RacewaySelection(id="raceway-selection:one", intent_id="route-intent:one", route_id=route.id,
        product=ROUTE_PRODUCT, route_type="emt", wiring_method="raceway", designation_kind="trade_size",
        designation="1/2 in synthetic", basis=RULE)
    route_outcome = RacewaySelectionOutcome(id="raceway-outcome:one", intent_id=route_product.intent_id,
        route_id=route.id, status="selected", selected_selection_id=route_product.id,
        requested_product=ROUTE_PRODUCT, requested_basis=RULE)
    kwargs = {}
    if with_box:
        kwargs.update(levels=(level,), walls=(wall,), electrical_boxes=(box,), box_selection_outcomes=(box_outcome,))
    model = BuildingModel(model_id="fitting-model", schema_version="1.5.0", electrical_devices=devices,
        ports=tuple(points.values()), routes=(route,), route_fittings=(fitting,),
        raceway_selections=(route_product,), raceway_selection_outcomes=(route_outcome,), **kwargs)
    intent = FittingIntent(intent_id="fitting-intent:one", fitting_id=fitting.id, actor_id="synthetic-reviewer",
        requested_product=fitting_product, basis=RULE, bindings=bindings)
    return model, intent, fitting_product


def run_selector(model, intent, fitting_product, manifest=None, **rule_fields):
    manifest = manifest or catalog_manifest()
    rule = rule_for(fitting_product, "coupling" if fitting_product == COUPLING_PRODUCT else "connector", **rule_fields)
    return select_route_fittings(model, intents=(intent,), catalog=manifest, rules=(rule,))


@pytest.mark.parametrize("with_box", [False, True])
def test_exact_product_pairs_select_only_existing_bound_interfaces(with_box):
    model, intent, product = build_model("connector" if with_box else "coupling", with_box=with_box)
    result = run_selector(model, intent, product)
    assert len(result.fitting_selections) == 1
    assert len(result.fitting_interfaces) == 2
    assert result.fitting_selections[0].product == product
    assert result.fitting_selections[0].geometry_basis == "metadata_only"
    assert result.fitting_selections[0].size is None
    assert result.fitting_selection_outcomes[0].status == "selected"
    assert result.fitting_selection_outcomes[0].actual_od_check_performed is False
    assert BuildingModel.from_json(result.to_json()).to_json() == result.to_json()
    assert run_selector(result, intent, product).to_json() == result.to_json()


def test_missing_port_or_exact_pair_is_unresolved_without_synthesizing_a_port():
    model, intent, product = build_model()
    missing = FittingPortBinding(interface_key="end_b", mating_interface_key="run_end",
        fitting_port_id="port:fit-b", counterpart_port_id=None)
    intent = FittingIntent(intent_id=intent.intent_id, fitting_id=intent.fitting_id,
        actor_id=intent.actor_id, requested_product=intent.requested_product, basis=intent.basis,
        bindings=(intent.bindings[0], missing))
    result = run_selector(model, intent, product)
    assert result.fitting_selections == () and result.fitting_interfaces == ()
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "counterpart_port_missing" in result.fitting_selection_outcomes[0].reason_codes


def test_same_nominal_or_method_never_substitutes_for_exact_pair():
    model, intent, product = build_model()
    manifest = catalog_manifest()
    doc = json.loads(manifest.to_json())
    coupling = next(entry for entry in doc["entries"] if entry["product_id"] == product.product_id)
    pair = coupling["mating_pairs"][0]
    pair["mating_product"]["product_id"] = "another-emt-product"
    wrong = CatalogManifest(json.dumps(doc).encode())
    result = run_selector(model, intent, product, manifest=wrong)
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "mating_pair_unknown" in result.fitting_selection_outcomes[0].reason_codes


@pytest.mark.parametrize("consolidation", [None, "not-an-object", ["nonmaterial"]])
def test_arbitrary_json_consolidation_values_do_not_crash_or_invent_nonmaterial_status(consolidation):
    model, intent, product = build_model()
    fitting = model.route_fittings[0]
    fitting = replace(fitting, attributes={"consolidation": consolidation})
    model = replace(model, route_fittings=(fitting,))
    result = run_selector(model, intent, product)
    assert result.fitting_selection_outcomes[0].status == "selected"


def test_huge_finite_port_directions_do_not_overflow_into_a_false_match():
    model, intent, product = build_model("connector", with_box=True)
    ports = []
    for port in model.ports:
        if port.id == "port:fit-box":
            port = replace(port, direction=Vector3(x=1e200, y=1e200, z=0.0))
        elif port.id == "port:box-entry":
            port = replace(port, direction=Vector3(x=1e200, y=-1e200, z=0.0))
        ports.append(port)
    orthogonal = replace(model, ports=tuple(ports))
    result = run_selector(orthogonal, intent, product)
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "interface_geometry_mismatch" in result.fitting_selection_outcomes[0].reason_codes

    opposite = tuple(replace(port, direction=Vector3(x=1e200, y=0.0, z=0.0))
                     if port.id == "port:fit-box" else
                     replace(port, direction=Vector3(x=-1e200, y=0.0, z=0.0))
                     if port.id == "port:box-entry" else port for port in model.ports)
    selected = run_selector(replace(model, ports=opposite), intent, product)
    assert selected.fitting_selection_outcomes[0].status == "selected"


def test_huge_finite_port_coordinates_fail_closed_as_geometry_mismatch():
    model, intent, product = build_model()
    replacements = {
        "port:fit-a": replace(next(port for port in model.ports if port.id == "port:fit-a"),
            pose=Pose(position=Point3(x=1e200, y=0.0, z=0.0))),
        "port:route-a": replace(next(port for port in model.ports if port.id == "port:route-a"),
            pose=Pose(position=Point3(x=-1e200, y=0.0, z=0.0))),
    }
    extreme = replace(model, ports=tuple(replacements.get(port.id, port) for port in model.ports))
    result = run_selector(extreme, intent, product)
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "interface_geometry_mismatch" in result.fitting_selection_outcomes[0].reason_codes


def test_required_actual_od_is_explicit_and_cannot_be_claimed_by_v15():
    model, intent, product = build_model()
    result = run_selector(model, intent, product, check_actual_od=True)
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "actual_od_unknown" in result.fitting_selection_outcomes[0].reason_codes
    assert result.fitting_selection_outcomes[0].actual_od_check_performed is False


def test_required_environment_and_listing_facts_are_not_guessed():
    model, intent, product = build_model()
    result = run_selector(model, intent, product, required_environment=("dry_location",),
                          required_listing_refs=("synthetic-listing",))
    reasons = result.fitting_selection_outcomes[0].reason_codes
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "environment_unknown" in reasons and "listing_reference_missing" in reasons


def test_conflicting_duplicate_exact_pair_facts_remain_unresolved():
    model, intent, product = build_model()
    manifest = catalog_manifest()
    doc = json.loads(manifest.to_json())
    coupling = next(entry for entry in doc["entries"] if entry["product_id"] == product.product_id)
    duplicate = deepcopy(coupling["mating_pairs"][0])
    duplicate["id"] = "coupling-a-route-duplicate"
    duplicate["evidence"] = _evidence(["id", "interface_key", "mating_product", "mating_interface_key"])
    coupling["mating_pairs"].append(duplicate)
    conflicting = CatalogManifest(json.dumps(doc).encode())
    result = run_selector(model, intent, product, manifest=conflicting)
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "mating_pair_conflict" in result.fitting_selection_outcomes[0].reason_codes


def _add_symmetric_third_peer(model):
    route_peer = next(port for port in model.ports if port.id == "port:route-a")
    branch = Port(id="port:branch", owner_id="device:branch", domain="electrical", role="branch",
        pose=route_peer.pose, direction=Vector3(x=0.0, y=1.0, z=0.0), connected_port_ids=(route_peer.id,))
    branched_peer = replace(route_peer, connected_port_ids=tuple(sorted((*route_peer.connected_port_ids, branch.id))))
    return replace(model, electrical_devices=(*model.electrical_devices,
        ElectricalDevice(id="device:branch", device_type="test", pose=branch.pose)),
        ports=tuple(branched_peer if port.id == route_peer.id else port for port in model.ports) + (branch,))


def test_symmetric_third_branch_is_not_a_pair_and_tampered_selected_model_fails():
    model, intent, product = build_model()
    manifest = catalog_manifest()
    rule = rule_for(product, "coupling")
    branched = _add_symmetric_third_peer(model)
    result = select_route_fittings(branched, intents=(intent,), catalog=manifest, rules=(rule,))
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "unsupported_joint_topology" in result.fitting_selection_outcomes[0].reason_codes
    selected = select_route_fittings(model, intents=(intent,), catalog=manifest, rules=(rule,))
    with pytest.raises(ContractError, match="exclusive one-to-one"):
        _add_symmetric_third_peer(selected)


def test_selected_producer_receipts_reject_stale_pair_product_rule_and_missing_evidence():
    model, intent, product = build_model()
    selected = run_selector(model, intent, product)
    first = selected.fitting_interfaces[0]
    with pytest.raises(ContractError, match="mating fields contradict exact pair"):
        replace(selected, fitting_interfaces=(replace(first, mating_pair_id="pair:tampered"),
            *selected.fitting_interfaces[1:]))

    changed_product = CatalogProduct(catalog_id="other-catalog", catalog_revision="r9", product_id="fake")
    changed_selection = replace(selected.fitting_selections[0], product=changed_product)
    changed_outcome = replace(selected.fitting_selection_outcomes[0], requested_product=changed_product)
    with pytest.raises(ContractError, match="contradicts the consumed intent"):
        replace(selected, fitting_selections=(changed_selection,), fitting_selection_outcomes=(changed_outcome,))

    outcome = selected.fitting_selection_outcomes[0]
    stripped = replace(outcome, provenance=tuple(record for record in outcome.provenance
        if record.method != "exact-route-fitting-selection-outcome"))
    with pytest.raises(ContractError):
        replace(selected, fitting_selection_outcomes=(stripped,))

    generated = next(record for record in selected.fitting_selections[0].provenance
                     if record.method == "exact-route-fitting-selection")
    stale_rule = {**generated.attributes["consumed_caller_rule"], "required_environment": ["wet_location"]}
    changed_generated = replace(generated, attributes={**generated.attributes, "consumed_caller_rule": stale_rule})
    changed_provenance = tuple(changed_generated if record is generated else record
                               for record in selected.fitting_selections[0].provenance)
    with pytest.raises(ContractError, match="contradicts caller rule evidence"):
        replace(selected, fitting_selections=(replace(selected.fitting_selections[0],
            provenance=changed_provenance),))

    selected_receipt = next(record for record in selected.fitting_selections[0].provenance
                            if record.method == "exact-route-fitting-selection")
    widened = replace(selected_receipt, scope_paths=(*selected_receipt.scope_paths, "basis"))
    with pytest.raises(ContractError, match="exact field scope"):
        replace(selected, fitting_selections=(replace(selected.fitting_selections[0], provenance=tuple(
            widened if record is selected_receipt else record
            for record in selected.fitting_selections[0].provenance)),))

    first_interface = selected.fitting_interfaces[0]
    owner_record = next(record for record in first_interface.provenance
                        if record.method == "exact-counterpart-product-binding")
    foreign_owner = replace(owner_record, source_id="foreign-model")
    with pytest.raises(ContractError, match="exact field scope"):
        replace(selected, fitting_interfaces=(replace(first_interface, provenance=tuple(
            foreign_owner if record is owner_record else record for record in first_interface.provenance)),
            *selected.fitting_interfaces[1:]))

    link_record = next(record for record in first_interface.provenance
                       if record.method == "fitting-interface-selection-link")
    foreign_link = replace(link_record, source_id="foreign-model")
    with pytest.raises(ContractError, match="exact field scope"):
        replace(selected, fitting_interfaces=(replace(first_interface, provenance=tuple(
            foreign_link if record is link_record else record for record in first_interface.provenance)),
            *selected.fitting_interfaces[1:]))

    caller_record = next(record for record in selected.fitting_selections[0].provenance
                         if record.method == "explicit-route-fitting-intent")
    foreign_caller = replace(caller_record, source_element_id="another-intent")
    with pytest.raises(ContractError, match="exact field scope"):
        replace(selected, fitting_selections=(replace(selected.fitting_selections[0], provenance=tuple(
            foreign_caller if record is caller_record else record
            for record in selected.fitting_selections[0].provenance)),))


def test_not_found_product_outcome_still_pins_the_catalog_snapshot():
    model, intent, _ = build_model()
    missing_product = CatalogProduct(catalog_id="fitting-catalog", catalog_revision="f1",
                                     product_id="not-in-this-manifest")
    missing_intent = replace(intent, requested_product=missing_product)
    manifest = catalog_manifest()
    result = select_route_fittings(model, intents=(missing_intent,), catalog=manifest,
        rules=(rule_for(missing_product, "coupling"),))
    assert result.fitting_selection_outcomes[0].status == "unresolved"
    assert "missing_catalog_product" in result.fitting_selection_outcomes[0].reason_codes
    decision = next(record for record in result.fitting_selection_outcomes[0].provenance
                    if record.method == "exact-route-fitting-selection-outcome")
    assert decision.attributes["catalog_manifest_sha256"] == manifest.sha256


def test_null_product_and_rule_are_valid_unresolved_intent_evidence():
    model, intent, _ = build_model()
    intent = replace(intent, requested_product=None, basis=None)
    result = select_route_fittings(model, intents=(intent,), catalog=catalog_manifest(), rules=())
    outcome = result.fitting_selection_outcomes[0]
    assert outcome.status == "unresolved"
    assert {"missing_requested_product", "missing_rule_reference"} <= set(outcome.reason_codes)
    assert outcome.requested_product is None and outcome.requested_basis is None


def test_selected_inventory_requires_complete_field_evidence_and_cannot_keep_stale_empty_input():
    model, intent, product = build_model()
    selected = run_selector(model, intent, product)
    with pytest.raises(ContractError, match="field-scoped evidence"):
        replace(selected,
            fitting_selections=tuple(replace(item, provenance=()) for item in selected.fitting_selections),
            fitting_interfaces=tuple(replace(item, provenance=()) for item in selected.fitting_interfaces),
            fitting_selection_outcomes=tuple(replace(item, provenance=()) for item in selected.fitting_selection_outcomes),
            provenance=())

    with pytest.raises(ContractError, match="do not match the consumed intent list"):
        replace(selected, fitting_selections=(), fitting_interfaces=(), fitting_selection_outcomes=())


def test_complete_alternative_source_field_evidence_is_accepted_without_producer_claims():
    model, intent, product = build_model()
    selected = run_selector(model, intent, product)

    def alternative_records(entity):
        from oabm.model.model import _fitting_claim_fields
        return tuple(Provenance(source_kind="independent-review", source_id=f"{entity.id}:{field}",
            derivation="observed", scope_paths=(field,))
            for field in sorted(_fitting_claim_fields(entity)))

    normalized = replace(selected,
        fitting_selections=tuple(replace(item, provenance=alternative_records(item))
                                 for item in selected.fitting_selections),
        fitting_interfaces=tuple(replace(item, provenance=alternative_records(item))
                                 for item in selected.fitting_interfaces),
        fitting_selection_outcomes=tuple(replace(item, provenance=alternative_records(item))
                                         for item in selected.fitting_selection_outcomes),
        provenance=())
    assert not any(record.source_kind == "route-fitting-selection-producer"
                   for entity in (*normalized.fitting_selections, *normalized.fitting_interfaces,
                                  *normalized.fitting_selection_outcomes)
                   for record in entity.provenance)


def test_catalog_manifest_subclass_cannot_override_byte_bound_product_lookup():
    model, intent, product = build_model()
    manifest = catalog_manifest()

    class ForgedManifest(CatalogManifest):
        def fitting_entry(self, product_id):
            entry = super().fitting_entry(product_id)
            return replace(entry, product=CatalogProduct(catalog_id="outside-raw-bytes",
                catalog_revision="forged", product_id=entry.product.product_id))

    forged = ForgedManifest(manifest.raw)
    with pytest.raises(ContractError, match="exact electrical-catalog-v2 byte snapshot"):
        run_selector(model, intent, product, manifest=forged)


def test_catalog_pin_cannot_be_forged_away_from_nested_source_evidence():
    model, intent, product = build_model()
    selected = run_selector(model, intent, product)
    fake_digest = "f" * 64

    def rewrite(record):
        attributes = deepcopy(record.attributes)
        if "catalog_manifest_sha256" in attributes:
            attributes["catalog_manifest_sha256"] = fake_digest
        if record.method == "exact-route-fitting-selection":
            entry = deepcopy(attributes["consumed_catalog_entry"])
            entry["manifest_sha256"] = fake_digest
            attributes["consumed_catalog_entry"] = entry
        return replace(record, attributes=attributes)

    new_selections = tuple(replace(entity, provenance=tuple(rewrite(record) for record in entity.provenance))
                           for entity in selected.fitting_selections)
    new_interfaces = tuple(replace(entity, provenance=tuple(rewrite(record) for record in entity.provenance))
                           for entity in selected.fitting_interfaces)
    new_outcomes = tuple(replace(entity, provenance=tuple(rewrite(record) for record in entity.provenance))
                         for entity in selected.fitting_selection_outcomes)
    new_model_provenance = tuple(
        replace(record, attributes={**record.attributes, "catalog_manifest_sha256": fake_digest})
        if record.method == "explicit-route-fitting-inputs" else record
        for record in selected.provenance)
    with pytest.raises(ContractError, match="not bound to the consumed raw catalog snapshot"):
        replace(selected, fitting_selections=new_selections, fitting_interfaces=new_interfaces,
                fitting_selection_outcomes=new_outcomes, provenance=new_model_provenance)


def test_input_collection_permutations_are_deterministic():
    from oabm.electrical.fittings import _provenance_key
    model, intent, product = build_model()
    manifest = catalog_manifest()
    rule = rule_for(product, "coupling")
    evidence_with_pages = tuple(replace(rule.provenance[0], source_id=f"synthetic-rule-{page}", page=page)
                                for page in (1, 2))
    same_except_page = (replace(evidence_with_pages[0], source_id="same-source", page=1),
                        replace(evidence_with_pages[0], source_id="same-source", page=2))
    assert _provenance_key(same_except_page[0]) != _provenance_key(same_except_page[1])
    assert tuple(sorted(reversed(same_except_page), key=_provenance_key)) == same_except_page
    rule = replace(rule, provenance=evidence_with_pages)
    first = select_route_fittings(model, intents=(intent,), catalog=manifest, rules=(rule,))
    permuted_intent = replace(intent, bindings=tuple(reversed(intent.bindings)))
    permuted_rule = replace(rule,
        permitted_products=tuple(reversed(rule.permitted_products)),
        permitted_topologies=tuple(reversed(rule.permitted_topologies)),
        required_environment=tuple(reversed(rule.required_environment)),
        required_listing_refs=tuple(reversed(rule.required_listing_refs)),
        provenance=tuple(reversed(rule.provenance)))
    unused_rule = replace(permuted_rule,
        reference=RuleReference(authority="synthetic-caller", revision="r1", rule_id="unused"))
    second = select_route_fittings(model, intents=(permuted_intent,), catalog=manifest,
        rules=(unused_rule, permuted_rule))
    assert first.to_json() == second.to_json()


def test_v2_catalog_is_strict_and_v1_stays_separate():
    manifest = catalog_manifest()
    assert manifest.version == VERSION_V2
    assert len(manifest.search(kind="fitting")) == 2
    entry = manifest.fitting_entry("coupling-1")
    assert entry.manifest_sha256 == manifest.sha256
    model, intent, product = build_model()
    rule = rule_for(product, "coupling")
    forged = replace(entry, manifest_sha256="f" * 64)
    with pytest.raises(ContractError, match="byte snapshot"):
        select_route_fittings(model, intents=(intent,), catalog=forged, rules=(rule,))
    doc = json.loads(manifest.to_json())
    bad = deepcopy(doc)
    bad["entries"][0]["interfaces"][1]["key"] = bad["entries"][0]["interfaces"][0]["key"]
    with pytest.raises(ContractError, match="duplicate interface key"):
        CatalogManifest(json.dumps(bad).encode())
    bad = deepcopy(doc)
    bad["entries"][0]["mating_pairs"][0]["evidence"].pop()
    with pytest.raises(ContractError):
        CatalogManifest(json.dumps(bad).encode())
    legacy = deepcopy(doc)
    legacy["version"] = "electrical-catalog-v1"
    with pytest.raises(ContractError):
        CatalogManifest(json.dumps(legacy).encode())
    with pytest.raises(ContractError, match="duplicate JSON key"):
        BuildingModel.from_json('{"schema_version":"1.5.0","schema_version":"1.0.0"}')


def test_new_canonical_types_reject_malformed_direct_python_objects():
    model, intent, product = build_model()
    with pytest.raises(ContractError, match="typed Size3"):
        FittingSelection(id="selection:bad", intent_id=intent.intent_id, fitting_id="fitting:one",
            route_id="route:emt", product=product, route_type="emt", fitting_type="coupling",
            basis=RULE, geometry_basis="dimension_derived_envelope", size={"x": 1})
    binding = FittingPortBinding(interface_key="end_a", mating_interface_key="run_end",
        fitting_port_id="port:fit-a", counterpart_port_id="port:route-a")
    with pytest.raises(ContractError, match="duplicate caller binding"):
        FittingIntent(intent_id="fitting-intent:bad", fitting_id="fitting:one", actor_id="x",
            requested_product=product, basis=RULE, bindings=(binding, binding))
    with pytest.raises(ContractError, match="tuple of typed"):
        BuildingModel(model_id="bad", schema_version="1.5.0", fitting_selections=[])
    rule = rule_for(product, "coupling")
    provenance = rule.provenance[0]
    with pytest.raises(ContractError, match="duplicate same-field/source"):
        replace(rule, provenance=(provenance,
            replace(provenance, scope_paths=("reference",))))
    catalog_interface = catalog_manifest().fitting_entry(product.product_id).interfaces[0]
    with pytest.raises(ContractError, match="duplicate same-field/source"):
        replace(catalog_interface, provenance=(*catalog_interface.provenance,
            catalog_interface.provenance[0]))
    listing_evidence = replace(catalog_interface.provenance[0], scope_paths=("listing_refs",))
    with pytest.raises(ContractError, match="invalid text"):
        replace(catalog_interface, listing_refs=("",), provenance=(*catalog_interface.provenance,
            listing_evidence))


def test_selected_vs_metadata_only_is_not_confused_with_missing_or_malformed_envelope():
    model, intent, product = build_model()
    result = run_selector(model, intent, product)
    selected = result.fitting_selections[0]
    assert selected.geometry_basis == "metadata_only" and selected.size is None
    with pytest.raises(ContractError):
        FittingSelection(id="selection:bad", intent_id=intent.intent_id, fitting_id="fitting:one",
            route_id="route:emt", product=product, route_type="emt", fitting_type="coupling",
            basis=RULE, geometry_basis="dimension_derived_envelope", size=None)


def test_field_provenance_scopes_are_not_widened_across_catalog_pair_or_rule_claims():
    model, intent, product = build_model()
    result = run_selector(model, intent, product)
    selection = result.fitting_selections[0]
    rule_records = [record for record in selection.provenance if record.source_id == "synthetic-rule"]
    assert rule_records and all(record.scope_paths == ("basis",) for record in rule_records)
    decision = next(record for record in selection.provenance
                    if record.method == "exact-route-fitting-selection")
    consumed_rule = decision.attributes["consumed_caller_rule_provenance"]
    assert consumed_rule[0]["scope_paths"] == ["reference", "permitted_products", "permitted_topologies",
        "check_actual_od", "required_environment", "required_listing_refs"]
    interface = result.fitting_interfaces[0]
    pair_rows = [record for record in interface.provenance if "consumed_mating_pair" in record.attributes]
    assert {record.scope_paths[0] for record in pair_rows} == {
        "mating_pair_id", "interface_key", "mating_product", "mating_interface_key"}
    assert all(len(record.scope_paths) == 1 for record in pair_rows)
    assert not any("selection_id" in (record.scope_paths or ()) and record.derivation == "user"
                   for record in interface.provenance)


def test_v15_portable_schema_enforces_version_and_geometry_basis():
    schema_path = Path(__file__).parents[1] / "contracts/oabm-model-v1.5.schema.json"
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    migrated = upgrade_to_v1_5(BuildingModel(model_id="schema-smoke")).to_dict()
    validator.validate(migrated)
    model, intent, product = build_model()
    selected = run_selector(model, intent, product)
    validator.validate(selected.to_dict())
    bad = deepcopy(selected.to_dict())
    bad["fitting_selections"][0]["geometry_basis"] = "dimension_derived_envelope"
    with pytest.raises(ValidationError):
        validator.validate(bad)
    with pytest.raises(ContractError):
        BuildingModel.from_dict({**migrated, "schema_version": "1.4.0"})


def test_legacy_model_provenance_cannot_scope_to_unserialized_v15_fields():
    record = Provenance(source_kind="test", source_id="legacy", scope_paths=("fitting_selections",))
    with pytest.raises(ContractError, match="requires schema_version 1.5.0"):
        BuildingModel(model_id="legacy-scope", schema_version="1.4.0", provenance=(record,))
    legacy_json = BuildingModel(model_id="legacy-scope-json", schema_version="1.4.0").to_dict()
    legacy_json["provenance"] = [{"source_kind": "test", "source_id": "legacy",
                                  "scope_paths": ["fitting_selections"]}]
    with pytest.raises(ContractError, match="requires schema_version 1.5.0"):
        BuildingModel.from_dict(legacy_json)
