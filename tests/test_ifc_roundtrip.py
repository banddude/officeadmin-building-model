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
    # One relationship per logical connection. The previous count of 2 pinned
    # the reciprocal pair the old connect_port helper wrote for one logical
    # connection, saturating both IFC4 role slots on both ports.
    assert len(connections) == 1
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
    from oabm.ifc import adapter as ifc_adapter

    def fail_create_port_connection(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic connection write failure")

    monkeypatch.setattr(
        ifc_adapter, "_create_port_connection", fail_create_port_connection
    )

    # The ports are named by canonical id; canonical ports have a null IFC Name.
    with pytest.raises(
        IfcAdapterError,
        match="failed to materialize canonical port connection "
        "'port:panel-load' <-> 'port:evse-feed'",
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


_GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"


def _panel_to_evse() -> BuildingModel:
    from oabm.qa import load_golden_cases, load_golden_model

    case = next(c for c in load_golden_cases(_GOLDEN_ROOT) if c.name == "panel-to-evse")
    return load_golden_model(case)


def test_panel_to_evse_connects_each_logical_connection_exactly_once() -> None:
    """panel-to-evse has one peer link and two route links: three connections.

    The old export doubled every logical connection into reciprocal pairs and
    then purged the peer link, so the file held neither the exact pairs nor the
    canonical connectivity. The pairs are asserted exactly, by GlobalId, not by
    count. The canonical peer link joins the two canonical ports; each route
    end joins its span to that end's own attachment port, never to the
    canonical port.
    """

    ifc = to_ifc(_panel_to_evse())
    panel = canonical_id_to_ifc_guid("port:ev-panel-load")
    evse = canonical_id_to_ifc_guid("port:evse-feed")
    attach_start = canonical_id_to_ifc_guid("route:panel-evse-direct#attach:start")
    attach_end = canonical_id_to_ifc_guid("route:panel-evse-direct#attach:end")
    segment_start = canonical_id_to_ifc_guid("route:panel-evse-direct#segment:0#port:start")
    segment_end = canonical_id_to_ifc_guid("route:panel-evse-direct#segment:0#port:end")

    pairs = sorted(
        (rel.RelatingPort.GlobalId, rel.RelatedPort.GlobalId)
        for rel in ifc.by_type("IfcRelConnectsPorts")
    )
    assert pairs == sorted(
        [
            (panel, evse),
            (attach_start, segment_start),
            (segment_end, attach_end),
        ]
    )
    for canonical in (panel, evse):
        links = _port_links(ifc.by_guid(canonical))
        assert len(links) == 1
        assert {links[0].RelatingPort.GlobalId, links[0].RelatedPort.GlobalId} == {panel, evse}


def test_canonical_port_flow_direction_reaches_the_file() -> None:
    """``_flow_direction`` was dead code: connect_port overwrote every
    FlowDirection with NOTDEFINED. Direct IfcRelConnectsPorts writes leave the
    port's own FlowDirection alone."""

    ifc = to_ifc(_panel_to_evse())
    assert (
        ifc.by_guid(canonical_id_to_ifc_guid("port:ev-panel-load")).FlowDirection == "SOURCE"
    )
    assert ifc.by_guid(canonical_id_to_ifc_guid("port:evse-feed")).FlowDirection == "SINK"


@pytest.mark.parametrize("model", [_garage(), _panel_to_evse()], ids=["garage", "panel-to-evse"])
def test_every_system_is_served_to_the_one_building(model: BuildingModel) -> None:
    """Every IfcSystem gets exactly one IfcRelServicesBuildings to the emitted
    IfcBuilding; an unserved system is dropped by viewers and MVD checkers."""

    ifc = to_ifc(model)
    buildings = ifc.by_type("IfcBuilding")
    assert len(buildings) == 1

    systems = ifc.by_type("IfcSystem")
    assert systems
    services = ifc.by_type("IfcRelServicesBuildings")
    assert sorted(rel.RelatingSystem.GlobalId for rel in services) == sorted(
        system.GlobalId for system in systems
    )
    for rel in services:
        assert [building.id() for building in rel.RelatedBuildings] == [buildings[0].id()]


def test_relationship_guids_are_deterministic_across_exports() -> None:
    """Two exports of one model give identical GlobalIds for the relationships
    this port introduces."""

    def _guids() -> list[str]:
        ifc = to_ifc(_panel_to_evse())
        return sorted(
            rel.GlobalId
            for rel in (*ifc.by_type("IfcRelConnectsPorts"), *ifc.by_type("IfcRelServicesBuildings"))
        )

    assert _guids() == _guids()


# --- Route ends attach through their own ports (PR #134 review) -------------
#
# A route end used to be wired straight to its canonical port. IFC4 gives a
# port one IfcRelConnectsPorts per role (ConnectedTo and ConnectedFrom are each
# SET [0:1]), so a panel port that starts three or more home runs could not be
# exported at all. Each route end now attaches to an adapter-owned port of its
# own, nested on the canonical port's owner.

_PANEL_PORT = "port:ev-panel-load"


def _port_links(port: object) -> list:
    return [*port.ConnectedTo, *port.ConnectedFrom]


def _link_peer(relation: object, port: object) -> object:
    if relation.RelatingPort == port:
        return relation.RelatedPort
    return relation.RelatingPort


def _nesting_owner(port: object) -> object:
    nests = port.Nests
    assert len(nests) == 1
    return nests[0].RelatingObject


def _adapter_psets(item: object) -> dict:
    import ifcopenshell.util.element

    return ifcopenshell.util.element.get_psets(item)


def _panel_with_home_runs(count: int) -> BuildingModel:
    """Synthetic panel whose one load port starts ``count`` home runs.

    Built from the panel-to-evse golden case: clear the canonical peer link,
    then clone the EVSE, its feed port and its route with new ids, so that
    ``count`` routes start at the one canonical panel port.
    """

    document = copy.deepcopy(_panel_to_evse().to_dict())
    for port in document["ports"]:
        port["connected_port_ids"] = []
    device = next(item for item in document["electrical_devices"] if item["id"] == "device:evse")
    feed = next(item for item in document["ports"] if item["id"] == "port:evse-feed")
    route = next(item for item in document["routes"] if item["id"] == "route:panel-evse-direct")
    for index in range(2, count + 1):
        # Clones stand along the east wall, inside the 6 m x 3 m room.
        position = {"x": 5.5, "y": round(1.0 + 0.05 * (index - 1), 4), "z": 1.5}
        clone_device = copy.deepcopy(device)
        clone_device["id"] = f"device:evse-{index}"
        clone_device["name"] = f"EVSE {index}"
        clone_device["pose"]["position"] = dict(position)
        clone_port = copy.deepcopy(feed)
        clone_port["id"] = f"port:evse-feed-{index}"
        clone_port["owner_id"] = clone_device["id"]
        clone_port["pose"]["position"] = dict(position)
        clone_route = copy.deepcopy(route)
        clone_route["id"] = f"route:panel-evse-direct-{index}"
        clone_route["end_port_id"] = clone_port["id"]
        clone_route["centerline"]["points"][-1] = dict(position)
        document["electrical_devices"].append(clone_device)
        document["ports"].append(clone_port)
        document["routes"].append(clone_route)
    model = BuildingModel.from_dict(document)
    assert sum(route.start_port_id == _PANEL_PORT for route in model.routes) == count
    return model


@pytest.mark.parametrize("count", [3, 27])
def test_panel_port_with_many_home_runs_exports_validates_and_round_trips(count: int) -> None:
    """Regression: more than two route ends on one canonical port.

    At a31f464 this raised IfcAdapterError ("cannot be oriented within that
    bound") for three routes from one panel port. Real plan-derived panels
    carry 4, 6 and 27 route ends on a single port.
    """

    import ifcopenshell.validate

    model = _panel_with_home_runs(count)
    ifc = to_ifc(model)

    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []

    # Two links per single-span route: attachment -> span start, span end ->
    # attachment. No canonical peer links remain in this model.
    assert len(ifc.by_type("IfcRelConnectsPorts")) == 2 * count
    for port in ifc.by_type("IfcDistributionPort"):
        assert len(port.ConnectedTo) <= 1
        assert len(port.ConnectedFrom) <= 1

    assert from_ifc(ifc).to_dict() == model.to_dict()
    assert round_trip(model).to_dict() == model.to_dict()


def test_each_route_end_attaches_through_exactly_one_adapter_port() -> None:
    model = _panel_with_home_runs(3)
    ifc = to_ifc(model)
    ports = {port.id: port for port in model.ports}

    attachments = [
        port
        for port in ifc.by_type("IfcDistributionPort")
        if str(_adapter_psets(port).get("OABM_Adapter", {}).get("Role", "")).startswith(
            "route-attach-"
        )
    ]
    assert len(attachments) == 2 * len(model.routes)

    for route in model.routes:
        last_span = len(route.centerline.points) - 2
        for end, canonical_id, span_port_key in (
            ("start", route.start_port_id, f"{route.id}#segment:0#port:start"),
            ("end", route.end_port_id, f"{route.id}#segment:{last_span}#port:end"),
        ):
            attachment = ifc.by_guid(canonical_id_to_ifc_guid(f"{route.id}#attach:{end}"))
            psets = _adapter_psets(attachment)
            assert "OABM_Canonical" not in psets
            assert psets["OABM_Adapter"]["Role"] == f"route-attach-{end}"
            assert psets["OABM_Adapter"]["RouteId"] == route.id
            assert psets["OABM_Adapter"]["CanonicalPortId"] == canonical_id

            canonical = ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))
            owner = ifc.by_guid(canonical_id_to_ifc_guid(ports[canonical_id].owner_id))
            assert _nesting_owner(attachment) == owner
            assert _nesting_owner(canonical) == owner

            position = np.asarray(
                ifcopenshell.util.placement.get_local_placement(attachment.ObjectPlacement)
            )[:3, 3]
            expected = ports[canonical_id].pose.position
            assert position.tolist() == pytest.approx([expected.x, expected.y, expected.z], abs=1e-9)

            links = _port_links(attachment)
            assert len(links) == 1
            assert _link_peer(links[0], attachment).GlobalId == canonical_id_to_ifc_guid(
                span_port_key
            )

    # Canonical ports carry canonical peer connectivity only, and this model
    # has none: the three route starts leave the panel port untouched.
    for port in model.ports:
        assert _port_links(ifc.by_guid(canonical_id_to_ifc_guid(port.id))) == []

    panel = ifc.by_guid(canonical_id_to_ifc_guid("equip:ev-panel"))
    nested = [item for rel in panel.IsNestedBy for item in rel.RelatedObjects]
    assert sorted(item.GlobalId for item in nested) == sorted(
        [
            canonical_id_to_ifc_guid(_PANEL_PORT),
            *(canonical_id_to_ifc_guid(f"{route.id}#attach:start") for route in model.routes),
        ]
    )

    # from_ifc ignores the attachment ports: the canonical port set is exact.
    assert [port.id for port in from_ifc(ifc).ports] == [port.id for port in model.ports]


def test_home_run_export_guids_are_deterministic_across_exports() -> None:
    def _signature() -> tuple:
        ifc = to_ifc(_panel_with_home_runs(3))
        return (
            sorted(port.GlobalId for port in ifc.by_type("IfcDistributionPort")),
            sorted(
                (rel.GlobalId, rel.RelatingPort.GlobalId, rel.RelatedPort.GlobalId)
                for rel in ifc.by_type("IfcRelConnectsPorts")
            ),
            sorted(
                (rel.GlobalId, rel.RelatingSystem.GlobalId)
                for rel in ifc.by_type("IfcRelServicesBuildings")
            ),
        )

    assert _signature() == _signature()


def test_port_slot_refusal_names_the_canonical_port_id() -> None:
    """A refusal names a canonical port by its id, not its (null) IFC Name.

    ``to_ifc`` cannot reach the refusal any more, so the writer is driven
    directly: the panel port's ConnectedTo slot already holds the canonical peer
    link, the first extra link takes its ConnectedFrom slot, and the second has
    nowhere to go.
    """

    from oabm.ifc import adapter as ifc_adapter

    ifc = to_ifc(_panel_to_evse())
    panel_port = ifc.by_guid(canonical_id_to_ifc_guid(_PANEL_PORT))
    owner = ifc.by_guid(canonical_id_to_ifc_guid("equip:ev-panel"))
    extras = []
    for index in range(2):
        extra = ifcopenshell.api.system.add_port(ifc, element=owner)
        extra.GlobalId = canonical_id_to_ifc_guid(f"test:extra-port-{index}")
        extra.Name = f"test:extra-port-{index}"
        extras.append(extra)

    with pytest.raises(
        IfcAdapterError,
        match="ports 'port:ev-panel-load' <-> 'test:extra-port-1' cannot be oriented",
    ) as raised:
        ifc_adapter._materialize_port_connections(
            ifc, [(panel_port, extras[0]), (panel_port, extras[1])]
        )
    assert "None" not in str(raised.value)
