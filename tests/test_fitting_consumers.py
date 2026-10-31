"""Synthetic exact-route-fitting consumer coverage; no vendor data or assets."""
from __future__ import annotations

import json
import struct
from dataclasses import replace

import pytest

from oabm.drawings import PlanSpec, generate_drawing_set
from oabm.electrical.catalog_manifest import CatalogManifest
from oabm.electrical.fittings import select_route_fittings
from oabm.exports.gltf import build_glb_bytes
from oabm.exports.review_bundle import _parse_glb_document, _canonical_entity_index, _node_index, _verify_expected_rendered_nodes
from oabm.ifc.adapter import (CANONICAL_PSET, IfcAdapterError, _point_on_segment,
    _placement_3d, _property_set, from_ifc, to_ifc)
from oabm.model import BuildingModel, Level, Point3, Size3
from oabm.model.layers import LayerDefinition, LayerSelectionConfig, SavedPreset, resolve_preset
from oabm.exports.layers import layer_glb_plan
from oabm.quantities import extract_quantities

from test_route_fittings import (BOX_PRODUCT, CONNECTOR_PRODUCT, RULE, build_model,
    catalog_manifest, rule_for, run_selector)


def _selected(*, envelope=False, with_box=False):
    fitting_type = "connector" if with_box else "coupling"
    model, intent, product = build_model(fitting_type, with_box=with_box)
    model = replace(model, name="Synthetic fitting consumer model")
    manifest = catalog_manifest()
    if envelope:
        document = json.loads(manifest.raw)
        entry = next(row for row in document["entries"] if row["product_id"] == product.product_id)
        entry["fields"]["size"] = {"x": 0.2, "y": 0.1, "z": 0.05}
        entry["geometry_basis"] = "dimension_derived_envelope"
        entry["evidence"].append({"field": "size", "source_id": "synthetic-source",
                                  "derivation": "observed", "confidence": 0.9})
        manifest = CatalogManifest(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
    rule = rule_for(product, fitting_type)
    return select_route_fittings(model, intents=(intent,), catalog=manifest, rules=(rule,))


def _glb_document(payload):
    return _parse_glb_document(payload)


def _native_fitting(ifc):
    return next(item for item in ifc.by_type("IfcCableCarrierFitting")
                if json.loads(_property_set(item, CANONICAL_PSET)["CanonicalJson"]).get("id") == "fitting:one")


def test_quantities_count_existing_fitting_once_with_product_evidence_only():
    model = _selected(envelope=True)
    report = extract_quantities(model)
    rows = [row for row in report.items if row.category == "fitting"]
    assert len(rows) == 1
    row = rows[0]
    assert row.quantity == 1.0
    assert row.source_entity_ids == ("fitting:one",)
    variant = dict(row.variant)
    assert variant["product_id"] == "coupling-1"
    assert variant["geometry_basis"] == "dimension_derived_envelope"
    assert variant["mating_pairs"]
    decision_ids = {item.id for item in (*model.fitting_selections, *model.fitting_interfaces,
                                         *model.fitting_selection_outcomes)}
    assert not any(decision_ids.intersection(warning.source_entity_ids)
                   for warning in report.warnings if warning.code == "unmeasured_entities")


def test_unresolved_product_warns_without_inventing_additional_count():
    model, intent, product = build_model()
    selected = run_selector(model, intent, product, check_actual_od=True)
    selected = replace(selected, name="Synthetic unresolved fitting model")
    report = extract_quantities(selected)
    rows = [row for row in report.items if row.category == "fitting"]
    assert len(rows) == 1 and rows[0].quantity == 1.0
    warning = next(row for row in report.warnings if row.code == "unresolved_fitting_selection")
    assert warning.source_entity_ids == ("fitting-outcome:" + selected.fitting_selection_outcomes[0].id.split(":", 1)[-1], "fitting:one") or "actual_od_unknown" in warning.message


@pytest.mark.parametrize("envelope", [False, True])
def test_glb_has_one_physical_fitting_node_and_preserves_nonphysical_records(envelope):
    model = _selected(envelope=envelope)
    document = _glb_document(build_glb_bytes(model))
    fitting_nodes = [node for node in document["nodes"] if node.get("extras", {}).get("canonical_id") == "fitting:one"]
    assert len(fitting_nodes) == 1
    node = fitting_nodes[0]
    extras = node["extras"]
    assert extras["native_cad_geometry"] is False
    assert document["asset"]["extras"]["fitting_selections"] == model.to_dict()["fitting_selections"]
    if envelope:
        assert "mesh" in node
        assert "dimension-derived envelope only" in extras["geometry_disclosure"]
        material = document["materials"][document["meshes"][node["mesh"]]["primitives"][0]["material"]]
        assert material["alphaMode"] == "BLEND"
        assert material["pbrMetallicRoughness"]["baseColorFactor"][3] < 1.0
    else:
        assert "mesh" not in node
        assert extras["geometry_basis"] == "metadata_only"


def test_ifc_roundtrip_exposes_envelope_and_keeps_interfaces_nonphysical():
    model = _selected(envelope=True)
    native = to_ifc(model)
    fitting = _native_fitting(native)
    assert _property_set(fitting, "OABM_Adapter")["GeometryBasis"] == "dimension_derived_envelope"
    assert _property_set(fitting, "OABM_Adapter")["NativeCatalogGeometry"] is False
    assert len(fitting.Representation.Representations) == 1
    native_interfaces = []
    for annotation in native.by_type("IfcAnnotation"):
        meta = _property_set(annotation, "OABM_Canonical")
        if meta is not None and meta.get("CanonicalKind") == "fitting_interface":
            native_interfaces.append(annotation)
    assert len(native_interfaces) == len(model.fitting_interfaces) == 2
    for annotation in native_interfaces:
        assert annotation.Representation is None or not any(
            rep.RepresentationIdentifier == "Body" for rep in annotation.Representation.Representations
        )
        assert annotation.ObjectPlacement is None
        assert _property_set(annotation, "OABM_FittingInterface")["NativePortCreated"] is False
    roundtripped = from_ifc(native)
    assert roundtripped.to_json(indent=None) == model.to_json(indent=None)

    interface_set = _property_set(native_interfaces[0], "OABM_FittingInterface")
    interface_set["MatingPairId"] = "contradictory-pair"
    pset = next(rel.RelatingPropertyDefinition for rel in native_interfaces[0].IsDefinedBy
                if rel.is_a("IfcRelDefinesByProperties") and rel.RelatingPropertyDefinition.Name == "OABM_FittingInterface")
    import ifcopenshell.api.pset
    ifcopenshell.api.pset.edit_pset(native, pset=pset, properties={"MatingPairId": "contradictory-pair"})
    with pytest.raises(IfcAdapterError, match="projection contradicts"):
        from_ifc(native)

    missing_envelope = to_ifc(model)
    _native_fitting(missing_envelope).Representation = None
    with pytest.raises(IfcAdapterError, match="Body claim contradicts"):
        from_ifc(missing_envelope)

    claimed_no_body = to_ifc(model)
    fitting = _native_fitting(claimed_no_body)
    adapter = next(rel.RelatingPropertyDefinition for rel in fitting.IsDefinedBy
                   if rel.is_a("IfcRelDefinesByProperties") and
                   rel.RelatingPropertyDefinition.Name == "OABM_Adapter")
    ifcopenshell.api.pset.edit_pset(claimed_no_body, pset=adapter, properties={"Body": "no"})
    with pytest.raises(IfcAdapterError, match="Body claim contradicts"):
        from_ifc(claimed_no_body)

    duplicate_body = to_ifc(model)
    fitting = _native_fitting(duplicate_body)
    first_body = next(rep for rep in fitting.Representation.Representations
                      if rep.RepresentationIdentifier == "Body")
    profile = duplicate_body.create_entity("IfcRectangleProfileDef", ProfileType="AREA",
        XDim=1.0, YDim=1.0)
    solid = duplicate_body.create_entity("IfcExtrudedAreaSolid", SweptArea=profile,
        Position=_placement_3d(duplicate_body, (0.0, 0.0, -0.5), (0.0, 0.0, 1.0),
                               (1.0, 0.0, 0.0)),
        ExtrudedDirection=duplicate_body.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=1.0)
    extra_body = duplicate_body.create_entity("IfcShapeRepresentation",
        ContextOfItems=first_body.ContextOfItems, RepresentationIdentifier="Body",
        RepresentationType="SweptSolid", Items=[solid])
    fitting.Representation.Representations = (*fitting.Representation.Representations, extra_body)
    with pytest.raises(IfcAdapterError, match="representation count"):
        from_ifc(duplicate_body)


def test_metadata_only_ifc_has_no_fitting_body_and_port_wrapper_is_distinguished():
    model = _selected(envelope=False)
    native = to_ifc(model)
    fitting = _native_fitting(native)
    assert _property_set(fitting, "OABM_Adapter")["Body"] == "no"
    assert fitting.Representation is None
    adapter_ports = []
    for rel in fitting.IsNestedBy:
        adapter_ports.extend(item for item in rel.RelatedObjects if item.is_a("IfcDistributionPort"))
    generated = [port for port in adapter_ports if _property_set(port, "OABM_Adapter")
                 and _property_set(port, "OABM_Adapter").get("Role") == "fitting-port"]
    assert generated
    assert all(_property_set(port, "OABM_Adapter").get("SemanticScope") ==
               "route-endpoint-wrapper; not a catalog interface port" for port in generated)

    claimed_body = to_ifc(model)
    fitting = _native_fitting(claimed_body)
    adapter = next(rel.RelatingPropertyDefinition for rel in fitting.IsDefinedBy
                   if rel.is_a("IfcRelDefinesByProperties") and
                   rel.RelatingPropertyDefinition.Name == "OABM_Adapter")
    import ifcopenshell.api.pset
    ifcopenshell.api.pset.edit_pset(claimed_body, pset=adapter, properties={"Body": "yes"})
    with pytest.raises(IfcAdapterError, match="Body claim contradicts"):
        from_ifc(claimed_body)


def test_drawing_schedules_and_layer_adapter_use_existing_fitting_envelope():
    model = _selected(envelope=True)
    level = Level(id="level:consumer", name="Consumer level", elevation_m=0.0)
    devices = tuple(replace(item, level_id=level.id) for item in model.electrical_devices)
    model = replace(model, levels=(level,), electrical_devices=devices)
    drawing = generate_drawing_set(model, plans=(PlanSpec(id="plan:fittings", level_id=level.id),))
    view = drawing.views[0]
    envelope = [primitive for primitive in view.primitives if primitive.layer == "electrical:fitting-envelopes"]
    assert envelope and all("fitting:one" in primitive.source_ids and
                            model.fitting_selections[0].id in primitive.source_ids for primitive in envelope)
    schedule_ids = {schedule.id for schedule in drawing.schedules}
    assert {"schedule:route-fittings", "schedule:fitting-interfaces", "schedule:fitting-outcomes"} <= schedule_ids
    config = LayerSelectionConfig(layers=(LayerDefinition(id="layer:fittings", name="Fittings",
            member_ids=("fitting:one",)),), presets=(SavedPreset(id="preset:fittings", name="Fittings hidden",
            hidden_layer_ids=("layer:fittings",)),))
    resolution = resolve_preset(config, model, preset="preset:fittings", unassigned="visible")
    assert resolution.hidden_ids == ("fitting:one",)
    plan = layer_glb_plan(model, resolution)
    assert plan.groups["layer:preset:fittings:hidden"] == frozenset({"fitting:one"})
    ghost_config = LayerSelectionConfig(layers=config.layers,
        presets=(SavedPreset(id="preset:fittings-ghost", name="Fittings ghosted",
            ghosted_layer_ids=("layer:fittings",)),))
    ghost_resolution = resolve_preset(ghost_config, model, preset="preset:fittings-ghost",
                                      unassigned="visible")
    ghost_plan = layer_glb_plan(model, ghost_resolution)
    assert ghost_plan.unsupported_ghost_ids == ("fitting:one",)


def test_review_bundle_node_coverage_accepts_fitting_node():
    model = _selected(envelope=True)
    document = _glb_document(build_glb_bytes(model))
    entities, _ = _canonical_entity_index(model)
    nodes = _node_index(document, entities)
    _verify_expected_rendered_nodes(model, nodes)


def test_ifc_route_port_projection_uses_metric_tolerance_on_short_segment():
    start = Point3(x=0.0, y=0.0, z=0.0)
    end = Point3(x=0.001, y=0.0, z=0.0)
    within = Point3(x=0.0010005, y=0.0, z=0.0)
    outside = Point3(x=0.001002, y=0.0, z=0.0)
    assert _point_on_segment(within, start, end, tolerance=1e-6)
    assert not _point_on_segment(outside, start, end, tolerance=1e-6)
    huge_start = Point3(x=-1e200, y=1e200, z=0.0)
    huge_end = Point3(x=1e200, y=-1e200, z=0.0)
    assert _point_on_segment(start, huge_start, huge_end, tolerance=1e-6)
    overflow_start = Point3(x=-1e308, y=0.0, z=0.0)
    overflow_end = Point3(x=1e308, y=0.0, z=0.0)
    assert not _point_on_segment(start, overflow_start, overflow_end, tolerance=1e-6)



def test_two_exact_skus_of_one_fitting_type_count_each_physical_fitting_once():
    from copy import deepcopy
    model, intent, product = build_model()
    second_product = replace(product, product_id="coupling-2")
    second_fitting = replace(model.route_fittings[0], id="fitting:two",
        pose=replace(model.route_fittings[0].pose, position=Point3(x=6.5, y=0.0, z=0.0)))
    peer_ids = {value for binding in intent.bindings
                for value in (binding.fitting_port_id, binding.counterpart_port_id)}
    rename = {value: value + ":second" for value in peer_ids}
    second_ports = tuple(replace(port, id=rename[port.id],
        owner_id=second_fitting.id if port.owner_id == intent.fitting_id else port.owner_id,
        pose=replace(port.pose, position=replace(port.pose.position, x=port.pose.position.x + 3.0)),
        connected_port_ids=tuple(rename[value] for value in port.connected_port_ids))
        for port in model.ports if port.id in peer_ids)
    model = replace(model,
        routes=(replace(model.routes[0], fitting_ids=(intent.fitting_id, second_fitting.id)),),
        route_fittings=(*model.route_fittings, second_fitting),
        ports=(*model.ports, *second_ports))
    second_intent = replace(intent, intent_id="fitting-intent:two", fitting_id=second_fitting.id,
        requested_product=second_product, bindings=tuple(replace(binding,
            fitting_port_id=rename[binding.fitting_port_id],
            counterpart_port_id=rename[binding.counterpart_port_id]) for binding in intent.bindings))
    document = json.loads(catalog_manifest().raw)
    entry = deepcopy(next(row for row in document["entries"] if row["product_id"] == product.product_id))
    entry["product_id"] = entry["part_number"] = second_product.product_id
    for pair in entry["mating_pairs"]:
        pair["id"] += "-second"
    document["entries"].append(entry)
    catalog = CatalogManifest(json.dumps(document).encode())
    rule = replace(rule_for(product, "coupling"), permitted_products=(product, second_product))
    selected = select_route_fittings(model, intents=(second_intent, intent), catalog=catalog, rules=(rule,))
    rows = [row for row in extract_quantities(selected).items if row.category == "fitting"]
    assert len(rows) == 2
    assert {(dict(row.variant)["product_id"], row.quantity, row.source_entity_ids)
            for row in rows} == {
        ("coupling-1", 1.0, ("fitting:one",)), ("coupling-2", 1.0, ("fitting:two",)),
    }
    assert len(selected.fitting_selections) == 2 and len(selected.fitting_interfaces) == 4
    assert sum(row.quantity for row in rows) == 2.0
    permuted = select_route_fittings(model, intents=(intent, second_intent), catalog=catalog, rules=(rule,))
    assert extract_quantities(permuted).to_json() == extract_quantities(selected).to_json()
