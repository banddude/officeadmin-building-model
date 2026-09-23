"""Public synthetic checks for provisional equipment and circuit design."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.drawings import generate_drawing_set
from oabm.ifc import to_ifc
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import (
    Box3D, BuildingModel, ElectricalDevice, Level, Obstacle, Opening, Point3, Polygon3D,
    Polyline3D, Pose, Size3, Space, Vector3, Wall,
)
from oabm.quantities import extract_quantities
from oabm.routing import (
    PlacementError, apply_equipment_proposal, design_proposed_circuits,
    propose_equipment_placement, set_user_equipment_placement,
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
        model_id="model:synthetic-design", levels=(level,), spaces=spaces,
        walls=walls, openings=openings, electrical_devices=devices,
    )


def _proposal(model: BuildingModel):
    return propose_equipment_placement(
        model, identity_key="proposed-source-panel", equipment_type="panelboard",
        name="LP-DESIGN", level_id="level:one",
        served_device_ids=tuple(item.id for item in model.electrical_devices),
    )


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
    content.set_data(b"BT /F1 10 Tf 1 0 0 1 70 500 Tm (DUPLEX OUTLET R1) Tj ET\n")
    page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "architectural-devices-no-panel.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    imported = ElectricalPdfImporter().import_document(
        extract_pdf(path, source_id="fixture:architectural-devices-no-panel")
    )
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
    assert all(item.to_dict()["design_status"] == "inferred"
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
    assert extract_quantities(replaced).items
    with pytest.raises(PlacementError, match="exactly once"):
        design_proposed_circuits(
            first, equipment_id=equipment_id, device_ids=device_ids,
            user_groups=((device_ids[0], device_ids[0]), (device_ids[2],)),
            user_input_id="decision:bad-duplicate",
        )
    with pytest.raises(PlacementError, match="already exists"):
        apply_equipment_proposal(placed, proposal)


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
