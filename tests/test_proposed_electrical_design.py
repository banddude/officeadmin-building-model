"""Public synthetic checks for provisional equipment and circuit design."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.drawings import generate_drawing_set
from oabm.ifc import to_ifc
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import (
    Box3D, BuildingModel, Circuit, ElectricalDevice, ElectricalEquipment, Level,
    Obstacle, Opening, Point3, Polygon3D, Polyline3D, Port, Pose, Size3, Space,
    Vector3, Wall,
)
from oabm.quantities import extract_quantities
from oabm.routing import (
    CircuitDesignRules, NoRouteError, PlacementError, RoutingOptions,
    apply_equipment_proposal, design_proposed_circuits,
    propose_architectural_electrical_design, propose_equipment_placement,
    set_user_equipment_placement,
)


def _rectangle(x0: float, y0: float, x1: float, y1: float) -> Polygon3D:
    return Polygon3D(points=(
        Point3(x=x0, y=y0, z=0), Point3(x=x1, y=y0, z=0),
        Point3(x=x1, y=y1, z=0), Point3(x=x0, y=y1, z=0),
    ))


def _model(*, door: bool = False) -> BuildingModel:
    level = Level(id="level:one", elevation_m=0)
    spaces = (
        Space(id="space:electrical", level_id=level.id,
              footprint=_rectangle(0, 0, 3, 4), usage="electrical room"),
        Space(id="space:office", level_id=level.id,
              footprint=_rectangle(3, 0, 8, 4), usage="office"),
    )
    walls = (
        Wall(id="wall:electrical", level_id=level.id,
             centerline=Polyline3D(points=(Point3(x=0, y=0, z=0),
                                            Point3(x=3, y=0, z=0))),
             thickness_m=0.1, height_m=3),
        Wall(id="wall:office", level_id=level.id,
             centerline=Polyline3D(points=(Point3(x=3, y=0, z=0),
                                            Point3(x=8, y=0, z=0))),
             thickness_m=0.1, height_m=3),
    )
    devices = tuple(ElectricalDevice(
        id=f"device:{index}", device_type="receptacle_duplex",
        pose=Pose(position=Point3(x=x, y=3, z=1.5)), level_id=level.id,
        space_id="space:office",
    ) for index, x in enumerate((4.0, 5.0, 6.0), start=1))
    openings = (Opening(
        id="opening:door", host_id=walls[0].id, opening_type="door",
        pose=Pose(position=Point3(x=1.5, y=0, z=1)),
        size=Size3(x=0.2, y=0.2, z=2),
    ),) if door else ()
    return BuildingModel(
        model_id="model:synthetic-design", name="Synthetic electrical design",
        levels=(level,), spaces=spaces,
        walls=walls, openings=openings, electrical_devices=devices,
    )


def _proposal(model: BuildingModel):
    return propose_equipment_placement(
        model, identity_key="proposed-source-panel", equipment_type="panelboard",
        name="LP-DESIGN", level_id="level:one",
        served_device_ids=tuple(item.id for item in model.electrical_devices),
    )


def _explicit_design_inputs(*, design_marker: object, include_third_device: bool = False):
    """Tiny canonical reviewed source/load setup; it supplies every design input."""

    panel = ElectricalEquipment(
        id="equipment:reviewed-panel", equipment_type="panelboard",
        pose=Pose(position=Point3(x=0, y=0, z=0)),
    )
    unrelated_panel = ElectricalEquipment(
        id="equipment:unrelated-panel", equipment_type="panelboard",
        pose=Pose(position=Point3(x=0, y=2, z=0)),
    )
    positions = (("device:load-a", (2, 0, 0)), ("device:load-b", (4, 0, 0)))
    if include_third_device:
        positions = (*positions, ("device:load-c", (5, 1, 0)))
    devices = tuple(ElectricalDevice(
        id=identity, device_type="receptacle_duplex",
        pose=Pose(position=Point3(x=coordinates[0], y=coordinates[1], z=coordinates[2])),
    ) for identity, coordinates in positions)
    other_device = ElectricalDevice(
        id="device:unrelated", device_type="receptacle_duplex",
        pose=Pose(position=Point3(x=0, y=3, z=0)),
    )

    def port(identity: str, owner_id: str, role: str, coordinates: tuple[int, int, int],
             direction: tuple[int, int, int]) -> Port:
        return Port(
            id=identity, owner_id=owner_id, domain="electrical", role=role,
            pose=Pose(position=Point3(x=coordinates[0], y=coordinates[1], z=coordinates[2])),
            direction=Vector3(x=direction[0], y=direction[1], z=direction[2]),
        )

    ports = (
        port("port:reviewed-source", panel.id, "source", (0, 0, 0), (1, 0, 0)),
        *(port(f"port:{identity}", identity, "load", coordinates, (-1, 0, 0))
          for identity, coordinates in positions),
        port("port:unrelated-source", unrelated_panel.id, "source", (0, 2, 0), (1, 0, 0)),
        port("port:unrelated-load", other_device.id, "load", (0, 3, 0), (-1, 0, 0)),
    )
    unrelated = Circuit(
        id="circuit:unrelated", source_port_id="port:unrelated-source",
        load_port_ids=("port:unrelated-load",), circuit_number="U-1",
        attributes={"design": design_marker, "legacy_payload": {"ordered": [2, 1]}},
    )
    model = BuildingModel(
        model_id="model:explicit-design-marker-regression",
        electrical_equipment=(panel, unrelated_panel),
        electrical_devices=(*devices, other_device), ports=ports, circuits=(unrelated,),
    )
    # Exercise the same canonical JSON boundary a caller uses; the unusual marker
    # remains valid JSON metadata and is not coerced into a design-record object.
    model = BuildingModel.from_dict(model.to_dict())
    device_ids = tuple(identity for identity, _ in positions)
    rules = CircuitDesignRules(
        max_devices_per_circuit=3, assumed_voltage_v=277.0, assumed_poles=1,
        route_type="emt", nominal_diameter_m=0.0254, conductor_material="copper",
        conductor_size="10 AWG", insulated_conductor_count=3, include_ground=False,
    )
    routing_options = RoutingOptions(
        bend_penalty_m=0.4, max_bends=8, port_stub_m=0.0, clearance_m=0.02,
        search_margin_m=0.5, corridor_tolerance_m=0.03,
        bundle_alignment_tolerance_m=0.02, preferred_corridor_discount=0.0,
        surface_path_discount=0.0, glazed_wall_penalty=7.0,
        soft_obstacle_penalty_factor=4.0, vertical_cost_factor=1.0,
        coordinate_precision=9,
    )
    kwargs = {
        "equipment_id": panel.id,
        "device_ids": device_ids,
        "rules": rules,
        "user_groups": (device_ids,),
        "user_input_id": "decision:explicit-marker-regression",
        "source_port_id": "port:reviewed-source",
        "load_port_ids": {identity: f"port:{identity}" for identity in device_ids},
        "routing_options": routing_options,
    }
    return model, kwargs, unrelated


@pytest.mark.parametrize("design_marker", [None, "legacy marker", ["legacy", {"v": 1}]])
def test_explicit_design_preserves_non_object_unrelated_design_markers(design_marker) -> None:
    model, kwargs, unrelated = _explicit_design_inputs(design_marker=design_marker)
    input_bytes = model.to_json()

    designed = design_proposed_circuits(model, **kwargs)

    assert model.to_json() == input_bytes
    assert designed.electrical_equipment == model.electrical_equipment
    assert {item.id: item for item in designed.ports} == {
        item.id: item for item in model.ports
    }  # explicit source and load ports are reused without edits
    preserved = next(item for item in designed.circuits if item.id == unrelated.id)
    assert preserved == unrelated
    assert preserved.attributes == unrelated.attributes
    assert preserved.attributes["design"] == design_marker
    assert len(designed.routes) == 2
    created = next(item for item in designed.circuits if item.id != unrelated.id)
    # This legitimate producer-owned dictionary is the receipt used for stable
    # idempotence; malformed/non-object optional metadata above is not ownership.
    assert created.attributes["design"]["equipment_id"] == kwargs["equipment_id"]
    assert created.attributes["design"]["source_observed"] is False
    assert design_proposed_circuits(designed, **kwargs) is designed
    assert model.to_json() == input_bytes


def test_explicit_design_partial_route_failure_leaves_input_unchanged(monkeypatch) -> None:
    marker = ["leave", {"these": "bytes"}]
    model, kwargs, unrelated = _explicit_design_inputs(
        design_marker=marker, include_third_device=True,
    )
    input_bytes = model.to_json()
    blocked_controls = replace(kwargs["routing_options"], max_bends=0)
    routing_design = import_module("oabm.routing.design")
    route_attempts = []
    route_between_ports = routing_design.route_between_ports

    def record_attempt(*args, **route_kwargs):
        route_attempts.append((args[1], args[2]))
        return route_between_ports(*args, **route_kwargs)

    monkeypatch.setattr(routing_design, "route_between_ports", record_attempt)

    with pytest.raises(NoRouteError, match="bends"):
        design_proposed_circuits(
            model, **{**kwargs, "routing_options": blocked_controls},
        )

    assert route_attempts == [
        ("port:reviewed-source", "port:device:load-a"),
        ("port:device:load-a", "port:device:load-b"),
        ("port:device:load-b", "port:device:load-c"),
    ]
    assert model.to_json() == input_bytes
    assert model.circuits == (unrelated,)
    assert unrelated.attributes == {
        "design": marker, "legacy_payload": {"ordered": [2, 1]},
    }


def test_equipment_id_only_dict_is_not_treated_as_a_managed_design_receipt() -> None:
    marker = {"equipment_id": "equipment:reviewed-panel", "manual_note": "keep"}
    model, kwargs, unrelated = _explicit_design_inputs(design_marker=marker)
    input_bytes = model.to_json()

    designed = design_proposed_circuits(model, **kwargs)

    assert model.to_json() == input_bytes
    preserved = next(item for item in designed.circuits if item.id == unrelated.id)
    assert preserved == unrelated
    assert preserved.attributes["design"] == marker


def test_legacy_managed_design_without_user_input_id_can_be_replaced() -> None:
    source_model, kwargs, unrelated = _explicit_design_inputs(design_marker=None)
    initial = design_proposed_circuits(source_model, **kwargs)
    generated = next(item for item in initial.circuits if item.id != unrelated.id)
    old_marker = {
        key: value for key, value in generated.attributes["design"].items()
        if key != "user_input_id"
    }
    legacy_circuit = replace(generated, attributes={"design": old_marker})
    legacy = replace(initial, circuits=tuple(
        legacy_circuit if item.id == generated.id else item
        for item in initial.circuits
    ))
    legacy = BuildingModel.from_dict(legacy.to_dict())
    input_bytes = legacy.to_json()
    device_a, device_b = kwargs["device_ids"]
    replacement_kwargs = {
        **kwargs,
        "user_groups": ((device_a,), (device_b,)),
        "user_input_id": "decision:replacement-after-legacy-record",
    }

    replaced = design_proposed_circuits(legacy, **replacement_kwargs)

    assert legacy.to_json() == input_bytes
    assert len(replaced.circuits) == 3
    assert sum(item.id != unrelated.id for item in replaced.circuits) == 2
    assert all(
        item.attributes["design"]["user_input_id"] == replacement_kwargs["user_input_id"]
        for item in replaced.circuits if item.id != unrelated.id
    )


def test_damaged_managed_design_record_is_not_silently_replaced() -> None:
    source_model, kwargs, unrelated = _explicit_design_inputs(design_marker=None)
    designed = design_proposed_circuits(source_model, **kwargs)
    generated = next(item for item in designed.circuits if item.id != unrelated.id)
    marker = {**generated.attributes["design"], "rules": {}}
    damaged_circuit = replace(generated, attributes={"design": marker})
    damaged = replace(designed, circuits=tuple(
        damaged_circuit if item.id == generated.id else item
        for item in designed.circuits
    ))
    damaged = BuildingModel.from_dict(damaged.to_dict())
    input_bytes = damaged.to_json()

    with pytest.raises(PlacementError, match="conflicting circuit groups"):
        design_proposed_circuits(damaged, **kwargs)

    assert damaged.to_json() == input_bytes
    damaged_marker = next(
        item for item in damaged.circuits if item.id == generated.id
    ).attributes["design"]
    assert damaged_marker["rules"] == {}


def test_source_pdf_without_panel_keeps_observed_circuits_empty(tmp_path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 10 Tf 1 0 0 1 70 500 Tm (EVSE-1) Tj ET\n")
    page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "architectural-devices-no-panel.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    imported = ElectricalPdfImporter().import_document(
        extract_pdf(path, source_id="fixture:architectural-devices-no-panel")
    )
    assert [device.name for device in imported.electrical_devices] == ["EVSE-1"]
    assert imported.electrical_equipment == ()
    assert imported.circuits == ()
    assert imported.ports == ()


def test_proposal_design_recompute_and_provenance() -> None:
    model = _model(door=True)
    proposal = _proposal(model)
    assert proposal.selected.space_id == "space:electrical"
    assert proposal.selected.valid
    assert any("opening:opening:door" in item.reasons for item in proposal.alternatives)
    assert proposal == _proposal(replace(
        model, spaces=tuple(reversed(model.spaces)),
        walls=tuple(reversed(model.walls)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
    ))
    placed = apply_equipment_proposal(model, proposal)
    equipment = placed.electrical_equipment[0]
    assert equipment.provenance[0].derivation == "inferred"
    assert len(placed.ports) == 1
    assert placed.circuits == ()

    designed = design_proposed_circuits(
        placed, equipment_id=equipment.id,
        device_ids=tuple(device.id for device in model.electrical_devices),
    )
    assert len(designed.circuits) == 1
    assert len(designed.routes) == 3
    assert len(designed.conductors) == 2
    assert designed.circuits[0].provenance[0].derivation == "inferred"
    assert designed.circuits[0].attributes["design"]["source_observed"] is False
    before = extract_quantities(designed)
    route_before = sum(item.quantity for item in before.items if item.category == "route_length")
    assert route_before > 0
    assert all(item.to_dict()["design_status"] == "system-designed"
               for item in before.items if item.category in {"route_length", "conductor_length"})
    assert all(item.to_dict()["placement_status"] == "inferred"
               for item in before.items if item.category == "route_length")
    drawings = generate_drawing_set(designed)
    references = {item.canonical_id: item for item in drawings.source_index}
    assert references[equipment.id].provenance[0].derivation == "inferred"
    assert any(item["canonical_id"] == equipment.id
               and item["provenance"][0]["derivation"] == "inferred"
               for item in drawings.to_dict()["source_index"])
    ifc = to_ifc(designed)
    assert "PlacementStatus" in ifc.to_string()
    assert "DesignStatus" in ifc.to_string()

    moved_x = 0.75 if equipment.pose.position.x == 2.25 else 2.25
    moved = set_user_equipment_placement(
        designed, equipment_id=equipment.id, position=Point3(x=moved_x, y=0, z=1.5),
        user_input_id="decision:move-panel", wall_id="wall:electrical",
        space_id="space:electrical", direction=Vector3(x=0, y=1, z=0),
    )
    assert moved.electrical_equipment[0].id == equipment.id
    assert moved.electrical_equipment[0].provenance[0].derivation == "user"
    assert moved.circuits[0].id == designed.circuits[0].id
    assert [item.id for item in moved.routes] == [item.id for item in designed.routes]
    after = extract_quantities(moved)
    route_after = sum(item.quantity for item in after.items if item.category == "route_length")
    assert route_after != route_before
    assert all(item.to_dict()["placement_status"] == "user"
               for item in after.items if item.category == "route_length")
    assert moved.to_json() == set_user_equipment_placement(
        designed, equipment_id=equipment.id, position=Point3(x=moved_x, y=0, z=1.5),
        user_input_id="decision:move-panel", wall_id="wall:electrical",
        space_id="space:electrical", direction=Vector3(x=0, y=1, z=0),
    ).to_json()


def test_user_groups_replace_design_and_conflicts_fail_closed() -> None:
    model = _model()
    proposal = _proposal(model)
    placed = apply_equipment_proposal(model, proposal)
    equipment_id = placed.electrical_equipment[0].id
    device_ids = tuple(item.id for item in model.electrical_devices)
    first = design_proposed_circuits(placed, equipment_id=equipment_id, device_ids=device_ids)
    replaced = design_proposed_circuits(
        first, equipment_id=equipment_id, device_ids=device_ids,
        user_groups=((device_ids[0], device_ids[1]), (device_ids[2],)),
        user_input_id="decision:split-circuits",
    )
    assert len(replaced.circuits) == 2
    assert all(item.provenance[0].derivation == "user" for item in replaced.circuits)
    assert all(item.attributes["design"]["source_observed"] is False for item in replaced.circuits)
    user_takeoff = extract_quantities(replaced)
    assert user_takeoff.items
    assert all(item.to_dict()["design_status"] == "user-directed"
               and item.to_dict()["geometry_status"] == "inferred"
               for item in user_takeoff.items
               if item.category in {"route_length", "conductor_length"})
    with pytest.raises(PlacementError, match="exactly once"):
        design_proposed_circuits(
            first, equipment_id=equipment_id, device_ids=device_ids,
            user_groups=((device_ids[0], device_ids[0]), (device_ids[2],)),
            user_input_id="decision:bad-duplicate",
        )
    assert replaced == design_proposed_circuits(
        replaced, equipment_id=equipment_id, device_ids=device_ids,
        user_groups=((device_ids[0], device_ids[1]), (device_ids[2],)),
        user_input_id="decision:split-circuits",
    )
    with pytest.raises(PlacementError, match="conflicting circuit groups"):
        design_proposed_circuits(
            replaced, equipment_id=equipment_id, device_ids=device_ids,
            user_groups=((device_ids[0],), (device_ids[1], device_ids[2])),
            user_input_id="decision:split-circuits",
        )
    second_override = design_proposed_circuits(
        replaced, equipment_id=equipment_id, device_ids=device_ids,
        user_groups=((device_ids[0],), (device_ids[1], device_ids[2])),
        user_input_id="decision:second-groups",
    )
    with pytest.raises(PlacementError, match="historical user_input_id"):
        design_proposed_circuits(
            second_override, equipment_id=equipment_id, device_ids=device_ids,
            user_groups=((device_ids[0], device_ids[1]), (device_ids[2],)),
            user_input_id="decision:split-circuits",
        )
    with pytest.raises(PlacementError, match="already exists"):
        apply_equipment_proposal(placed, proposal)


def test_architectural_entry_point_designs_without_manual_panel_hint() -> None:
    model = _model()
    designed, proposals = propose_architectural_electrical_design(model)
    assert len(proposals) == 1
    assert len(designed.electrical_equipment) == 1
    assert designed.electrical_equipment[0].name is None
    assert designed.electrical_equipment[0].provenance[0].derivation == "inferred"
    assert len(designed.circuits) == 1
    assert all(circuit.provenance[0].derivation == "inferred"
               for circuit in designed.circuits)
    assert extract_quantities(designed).items
    assert designed.to_json() == propose_architectural_electrical_design(
        replace(model, spaces=tuple(reversed(model.spaces)),
                walls=tuple(reversed(model.walls)),
                electrical_devices=tuple(reversed(model.electrical_devices))),
    )[0].to_json()
    with pytest.raises(PlacementError, match="existing panelboard"):
        propose_architectural_electrical_design(designed)


def test_missing_geometry_and_known_clearance_conflicts_do_not_create_panel() -> None:
    model = _model()
    with pytest.raises(PlacementError, match="registered space"):
        _proposal(replace(
            model, spaces=(),
            electrical_devices=tuple(replace(device, space_id=None)
                                     for device in model.electrical_devices),
        ))
    obstacle = Obstacle(
        id="obstacle:electrical-clearance",
        geometry=Box3D(pose=Pose(position=Point3(x=1.5, y=0.5, z=1.0)),
                       size=Size3(x=3.0, y=1.0, z=2.0)),
    )
    blocked = replace(model, obstacles=(obstacle,))
    proposal = _proposal(blocked)
    assert proposal.selected.space_id == "space:office"
    assert any("obstacle:obstacle:electrical-clearance" in item.reasons
               for item in proposal.alternatives)
    assert blocked.electrical_equipment == ()
    narrow = Obstacle(
        id="obstacle:narrow-middle",
        geometry=Box3D(pose=Pose(position=Point3(x=1.5, y=0.55, z=1)),
                       size=Size3(x=0.1, y=0.1, z=1)),
    )
    narrow_proposal = _proposal(replace(model, obstacles=(narrow,)))
    middle = [candidate for candidate in narrow_proposal.alternatives
              if candidate.wall_id == "wall:electrical" and candidate.position.x == 1.5]
    assert middle and all(not candidate.valid for candidate in middle)


def test_conflicting_user_decision_id_is_rejected() -> None:
    model = _model()
    placed = apply_equipment_proposal(model, _proposal(model))
    equipment = placed.electrical_equipment[0]
    accepted = set_user_equipment_placement(
        placed, equipment_id=equipment.id, position=equipment.pose.position,
        user_input_id="decision:one", wall_id=equipment.host_id,
        space_id=equipment.space_id, direction=Vector3(x=0, y=1, z=0),
    )
    assert accepted == set_user_equipment_placement(
        accepted, equipment_id=equipment.id, position=equipment.pose.position,
        user_input_id="decision:one", wall_id=equipment.host_id,
        space_id=equipment.space_id, direction=Vector3(x=0, y=1, z=0),
    )
    with pytest.raises(PlacementError, match="conflicting"):
        set_user_equipment_placement(
            accepted, equipment_id=equipment.id, position=Point3(x=0.75, y=0, z=1.5),
            user_input_id="decision:one", wall_id=equipment.host_id,
            space_id=equipment.space_id, direction=Vector3(x=0, y=1, z=0),
        )
    with pytest.raises(PlacementError, match="conflicting"):
        set_user_equipment_placement(
            accepted, equipment_id=equipment.id, position=equipment.pose.position,
            user_input_id="decision:one", wall_id=equipment.host_id,
            space_id=equipment.space_id, direction=Vector3(x=0, y=-1, z=0),
        )
    other_x = 0.75 if equipment.pose.position.x == 2.25 else 2.25
    moved = set_user_equipment_placement(
        accepted, equipment_id=equipment.id,
        position=Point3(x=other_x, y=0, z=1.5),
        user_input_id="decision:two", wall_id=equipment.host_id,
        space_id=equipment.space_id, direction=Vector3(x=0, y=1, z=0),
    )
    with pytest.raises(PlacementError, match="historical user_input_id"):
        set_user_equipment_placement(
            moved, equipment_id=equipment.id, position=Point3(x=1.5, y=0, z=1.5),
            user_input_id="decision:one", wall_id=equipment.host_id,
            space_id=equipment.space_id, direction=Vector3(x=0, y=1, z=0),
        )


def test_two_panels_keep_quantity_origins_separate_and_decision_ids_global() -> None:
    model = _model()
    equipment_ids = []
    for key, name in (("first-panel", "FIRST"), ("second-panel", "SECOND")):
        proposal = propose_equipment_placement(
            model, identity_key=key, equipment_type="panelboard", name=name,
            level_id="level:one",
            served_device_ids=tuple(item.id for item in model.electrical_devices),
        )
        model = apply_equipment_proposal(model, proposal)
        equipment_ids.append(next(item.id for item in model.electrical_equipment
                                  if item.name == name))
    first_id, second_id = equipment_ids
    model = design_proposed_circuits(
        model, equipment_id=first_id, device_ids=("device:1",),
    )
    model = design_proposed_circuits(
        model, equipment_id=second_id, device_ids=("device:2", "device:3"),
        user_groups=(("device:2", "device:3"),),
        user_input_id="decision:second-panel-groups",
    )
    route_lines = [item for item in extract_quantities(model).items
                   if item.category == "route_length"]
    assert {item.to_dict()["design_status"] for item in route_lines} == {
        "system-designed", "user-directed",
    }
    assert all(len({record.source_kind for record in item.provenance}
                   & {"system-design", "user-override"}) == 1
               for item in route_lines)
    with pytest.raises(PlacementError, match="another circuit design"):
        design_proposed_circuits(
            model, equipment_id=first_id, device_ids=("device:1",),
            user_groups=(("device:1",),),
            user_input_id="decision:second-panel-groups",
        )

    first = next(item for item in model.electrical_equipment if item.id == first_id)
    second = next(item for item in model.electrical_equipment if item.id == second_id)
    model = set_user_equipment_placement(
        model, equipment_id=first_id, position=first.pose.position,
        user_input_id="decision:shared-placement", wall_id=first.host_id,
        space_id=first.space_id, direction=Vector3(x=0, y=1, z=0),
    )
    model = set_user_equipment_placement(
        model, equipment_id=first_id, position=Point3(x=0.75, y=0, z=1.5),
        user_input_id="decision:first-move", wall_id=first.host_id,
        space_id=first.space_id, direction=Vector3(x=0, y=1, z=0),
    )
    with pytest.raises(PlacementError, match="another equipment placement"):
        set_user_equipment_placement(
            model, equipment_id=second_id, position=second.pose.position,
            user_input_id="decision:shared-placement", wall_id=second.host_id,
            space_id=second.space_id, direction=Vector3(x=0, y=1, z=0),
        )
