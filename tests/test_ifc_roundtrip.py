import copy
import json
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.geometry
import ifcopenshell.api.system
import ifcopenshell.guid
import ifcopenshell.util.placement
import numpy as np
import pytest

from oabm.ifc import (
    IfcAdapterError,
    canonical_id_to_ifc_guid,
    from_ifc,
    round_trip,
    to_ifc,
)
from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]
MODEL_FIXTURE = ROOT / "fixtures" / "model" / "v1" / "garage-route.json"
IFC_EXPECTED = ROOT / "fixtures" / "ifc" / "v1" / "garage-route-expected.json"


def _garage() -> BuildingModel:
    return BuildingModel.load(MODEL_FIXTURE)


def _all_canonical_ids(model: BuildingModel) -> set[str]:
    ids = {model.model_id}
    for name in (
        "levels",
        "spaces",
        "walls",
        "slabs",
        "ceilings",
        "openings",
        "electrical_equipment",
        "electrical_devices",
        "ports",
        "obstacles",
        "route_constraints",
        "routes",
        "route_fittings",
        "circuits",
        "conductors",
    ):
        ids.update(item.id for item in getattr(model, name))
    return ids


def test_garage_fixture_round_trips_in_memory_and_through_step(tmp_path: Path) -> None:
    model = _garage()

    assert round_trip(model).to_dict() == model.to_dict()

    path = tmp_path / "garage-route.ifc"
    to_ifc(model, path)
    assert path.read_text(encoding="utf-8").startswith("ISO-10303-21;")
    assert from_ifc(path).to_dict() == model.to_dict()


def test_ifc_materialization_matches_expected_electrical_shape() -> None:
    model = _garage()
    expected = json.loads(IFC_EXPECTED.read_text(encoding="utf-8"))
    ifc = to_ifc(model)

    assert ifc.schema == expected["ifc_schema"]
    for ifc_class, count in expected["expected_counts"].items():
        assert len(ifc.by_type(ifc_class)) == count, ifc_class

    for canonical_id in _all_canonical_ids(model):
        assert ifc.by_guid(canonical_id_to_ifc_guid(canonical_id)) is not None

    panel = ifc.by_guid(canonical_id_to_ifc_guid("equip:panel"))
    evse = ifc.by_guid(canonical_id_to_ifc_guid("device:evse"))
    assert panel.is_a("IfcElectricDistributionBoard")
    assert panel.PredefinedType == "DISTRIBUTIONBOARD"
    # EVSE is a power outlet in IFC4; its canonical type stays in ObjectType.
    assert evse.is_a("IfcOutlet")
    assert evse.PredefinedType == "POWEROUTLET"
    assert evse.ObjectType == "evse"

    carrier_segments = ifc.by_type("IfcCableCarrierSegment")
    assert all(segment.PredefinedType == "CONDUITSEGMENT" for segment in carrier_segments)

    route_system = ifc.by_guid(canonical_id_to_ifc_guid("route:panel-evse"))
    route_members = {
        product.id()
        for relation in route_system.IsGroupedBy
        for product in relation.RelatedObjects
    }
    expected_route_products = {
        product.id()
        for product in [
            *ifc.by_type("IfcCableCarrierSegment"),
            *ifc.by_type("IfcCableCarrierFitting"),
        ]
    }
    assert route_members == expected_route_products


def test_bonsai_equivalent_native_edit_preserves_identity_and_updates_canonical(tmp_path: Path) -> None:
    model = _garage()
    source = tmp_path / "before.ifc"
    edited_path = tmp_path / "after.ifc"
    to_ifc(model, source)

    ifc = ifcopenshell.open(str(source))
    panel = ifc.by_guid(canonical_id_to_ifc_guid("equip:panel"))
    evse = ifc.by_guid(canonical_id_to_ifc_guid("device:evse"))
    panel.Name = "Panel LP - Bonsai edit"

    matrix = np.asarray(
        ifcopenshell.util.placement.get_local_placement(evse.ObjectPlacement), dtype=float
    ).copy()
    matrix[2, 3] += 0.15
    ifcopenshell.api.geometry.edit_object_placement(
        ifc,
        product=evse,
        matrix=matrix,
        is_si=True,
        should_transform_children=False,
    )
    final_segment = next(
        segment
        for segment in ifc.by_type("IfcCableCarrierSegment")
        if segment.Name == "route:panel-evse segment 3"
    )
    axis = next(
        representation
        for representation in final_segment.Representation.Representations
        if representation.RepresentationIdentifier == "Axis"
    )
    polyline = next(item for item in axis.Items if item.is_a("IfcPolyline"))
    polyline.Points[-1].Coordinates = (5.2, 0.2, 1.35)
    ifc.write(str(edited_path))

    edited = from_ifc(edited_path)
    original_ids = _all_canonical_ids(model)
    edited_ids = _all_canonical_ids(edited)
    assert edited_ids == original_ids

    panel_after = next(item for item in edited.electrical_equipment if item.id == "equip:panel")
    evse_after = next(item for item in edited.electrical_devices if item.id == "device:evse")
    route_after = next(item for item in edited.routes if item.id == "route:panel-evse")
    circuit_after = next(item for item in edited.circuits if item.id == "circuit:evse")

    assert panel_after.name == "Panel LP - Bonsai edit"
    assert evse_after.pose.position.z == pytest.approx(1.35)
    assert route_after.centerline.points[-1].z == pytest.approx(1.35)
    assert circuit_after.route_ids == ("route:panel-evse",)


def test_explicit_canonical_port_connectivity_is_ifc_native_and_round_trips() -> None:
    document = copy.deepcopy(_garage().to_dict())
    first, second = document["ports"]
    first["connected_port_ids"] = [second["id"]]
    second["connected_port_ids"] = [first["id"]]
    document["routes"] = []
    document["route_fittings"] = []
    document["circuits"] = []
    document["conductors"] = []
    model = BuildingModel.from_dict(document)

    ifc = to_ifc(model)
    connections = ifc.by_type("IfcRelConnectsPorts")
    assert len(connections) == 2
    connected_guids = {
        connections[0].RelatingPort.GlobalId,
        connections[0].RelatedPort.GlobalId,
    }
    assert connected_guids == {
        canonical_id_to_ifc_guid(first["id"]),
        canonical_id_to_ifc_guid(second["id"]),
    }
    assert from_ifc(ifc).to_dict() == model.to_dict()


def _connected_port_model() -> BuildingModel:
    document = copy.deepcopy(_garage().to_dict())
    first, second = document["ports"]
    first["connected_port_ids"] = [second["id"]]
    second["connected_port_ids"] = [first["id"]]
    document["routes"] = []
    document["route_fittings"] = []
    document["circuits"] = []
    document["conductors"] = []
    return BuildingModel.from_dict(document)


def test_native_port_disconnect_clears_canonical_connectivity() -> None:
    model = _connected_port_model()
    ifc = to_ifc(model)

    for relation in list(ifc.by_type("IfcRelConnectsPorts")):
        ifc.remove(relation)

    edited = from_ifc(ifc)
    assert all(port.connected_port_ids == () for port in edited.ports)


def test_unexpected_native_connectivity_export_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _connected_port_model()

    def fail_connect_port(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic connect_port failure")

    monkeypatch.setattr(ifcopenshell.api.system, "connect_port", fail_connect_port)

    with pytest.raises(
        IfcAdapterError,
        match="failed to materialize canonical port connection",
    ):
        to_ifc(model)


def test_native_connectivity_export_rejects_unrepresentable_fanout() -> None:
    document = copy.deepcopy(_garage().to_dict())
    first, second = document["ports"]
    third = copy.deepcopy(second)
    third["id"] = "port:evse-feed-alt"
    first["connected_port_ids"] = [second["id"], third["id"]]
    second["connected_port_ids"] = [first["id"]]
    third["connected_port_ids"] = [first["id"]]
    document["ports"].append(third)
    document["routes"] = []
    document["route_fittings"] = []
    document["circuits"] = []
    document["conductors"] = []
    model = BuildingModel.from_dict(document)

    with pytest.raises(
        IfcAdapterError,
        match="cannot be represented natively without loss",
    ):
        to_ifc(model)


def test_removing_all_native_route_segments_is_rejected() -> None:
    ifc = to_ifc(_garage())

    for segment in list(ifc.by_type("IfcCableCarrierSegment")):
        ifc.remove(segment)

    with pytest.raises(
        IfcAdapterError,
        match="has no native route segments",
    ):
        from_ifc(ifc)


def test_stable_global_id_replacement_is_rejected() -> None:
    ifc = to_ifc(_garage())
    panel = ifc.by_guid(canonical_id_to_ifc_guid("equip:panel"))
    panel.GlobalId = ifcopenshell.guid.new()

    with pytest.raises(IfcAdapterError, match="stable identity changed"):
        from_ifc(ifc)

@pytest.mark.parametrize(
    ("route_type", "segment_class", "predefined_type", "fitting_class"),
    [
        ("emt", "IfcCableCarrierSegment", "CONDUITSEGMENT", "IfcCableCarrierFitting"),
        ("pvc", "IfcCableCarrierSegment", "CONDUITSEGMENT", "IfcCableCarrierFitting"),
        ("tray", "IfcCableCarrierSegment", "CABLETRAYSEGMENT", "IfcCableCarrierFitting"),
        ("trunking", "IfcCableCarrierSegment", "CABLETRUNKINGSEGMENT", "IfcCableCarrierFitting"),
        ("wireway", "IfcCableCarrierSegment", "CABLETRUNKINGSEGMENT", "IfcCableCarrierFitting"),
        ("cable", "IfcCableSegment", "CABLESEGMENT", "IfcCableFitting"),
    ],
)
def test_route_types_use_expected_ifc_distribution_classes(
    route_type: str,
    segment_class: str,
    predefined_type: str,
    fitting_class: str,
) -> None:
    document = copy.deepcopy(_garage().to_dict())
    document["routes"][0]["route_type"] = route_type
    model = BuildingModel.from_dict(document)

    ifc = to_ifc(model)
    segments = [
        item
        for item in ifc.by_type(segment_class)
        if (item.Name or "").startswith("route:panel-evse segment ")
    ]
    assert len(segments) == 3
    assert all(item.PredefinedType == predefined_type for item in segments)

    fittings = [
        ifc.by_guid(canonical_id_to_ifc_guid(fitting.id))
        for fitting in model.route_fittings
    ]
    assert all(item.is_a(fitting_class) for item in fittings)
    assert from_ifc(ifc).to_dict() == model.to_dict()
