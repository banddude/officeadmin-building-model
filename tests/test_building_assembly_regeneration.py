"""Actual canonical assignment edits, atomic refusal and connected room proof."""
from dataclasses import replace
import pytest
from oabm.building_assemblies import (
    AssemblyRegenerationError, regenerate_building_assemblies,
    CeilingParameterOverride,
)
from oabm.building_assemblies.room_fixtures import (
    synthetic_enclosed_room_source, synthetic_enclosed_room_assembly, synthetic_room_requests,
    synthetic_room_edit_requests as edited_room_requests,
)
from oabm.model import BuildingModel
from oabm.exports.gltf import build_glb_bytes
from oabm.ifc import to_ifc, from_ifc
from oabm.quantities import extract_quantities
from oabm.routing import derive_suspended_ceiling_route_domain, RoutingOptions


def test_room_edit_preserves_facts_identity_and_unrelated_records():
    before = synthetic_enclosed_room_assembly()
    source = synthetic_enclosed_room_source()
    frozen = before.to_json()
    after = regenerate_building_assemblies(before, edited_room_requests())
    assert regenerate_building_assemblies(before, synthetic_room_requests()).to_json() == frozen
    assert before.to_json() == frozen
    assert after.walls == source.walls and after.openings == source.openings
    assert after.ceilings == source.ceilings and after.slabs == source.slabs
    assert after.spaces == source.spaces and len(after.walls) == 4
    assert {p.id for p in after.assembly_components} == {p.id for p in before.assembly_components}
    assert after.to_json() != frozen
    assert regenerate_building_assemblies(before, tuple(reversed(edited_room_requests()))).to_json() == after.to_json()
    assert regenerate_building_assemblies(after, edited_room_requests()).to_json() == after.to_json()
    assert BuildingModel.from_dict(after.to_dict()).to_json() == after.to_json()
    # Partial edits preserve every unrelated assignment/part byte-for-byte.
    partial = regenerate_building_assemblies(before, (edited_room_requests()[-1],))
    assert tuple(p for p in partial.assembly_components if p.assembly_id != "assembly:ceiling-proof") == tuple(
        p for p in before.assembly_components if p.assembly_id != "assembly:ceiling-proof")
    assert partial.walls == before.walls
    old_volume = sum(item.quantity for item in extract_quantities(before).items
        if item.category == "assembly_component_geometry_volume")
    new_volume = sum(item.quantity for item in extract_quantities(after).items
        if item.category == "assembly_component_geometry_volume")
    assert new_volume - old_volume == pytest.approx(.0933381656966453, abs=1e-12)
    for parameter_id, old_value, new_value in (("ceiling.tile.thickness_m", .01905, .025),
                                             ("door.frame.face_width_m", .0508, .0635)):
        old_parameter = next(p for a in before.building_assemblies for p in a.resolved_parameters
                             if p.parameter_id == parameter_id)
        new_parameter = next(p for a in after.building_assemblies for p in a.resolved_parameters
                             if p.parameter_id == parameter_id)
        assert (old_parameter.origin, old_parameter.value) == ("preset_default", old_value)
        assert (new_parameter.origin, new_parameter.value) == ("user_override", new_value)
    for model in (before, after):
        assert build_glb_bytes(model) == build_glb_bytes(model)
        assert from_ifc(to_ifc(model,assembly_geometry="tessellated")).to_dict() == model.to_dict()
        domain = derive_suspended_ceiling_route_domain(model,"assembly:ceiling-proof",
            nominal_diameter_m=.02,options=RoutingOptions(clearance_m=.005))
        assert domain.volumes[0].size.z > 0
    old = derive_suspended_ceiling_route_domain(before,"assembly:ceiling-proof",nominal_diameter_m=.02)
    new = derive_suspended_ceiling_route_domain(after,"assembly:ceiling-proof",nominal_diameter_m=.02)
    assert old != new
    assert build_glb_bytes(before) != build_glb_bytes(after)


def test_invalid_edits_are_atomic_and_diagnose_ambiguous_ownership():
    model = synthetic_enclosed_room_assembly()
    frozen = model.to_json()
    requests = edited_room_requests()
    bad_ceiling = replace(requests[-1],parameter_overrides=(CeilingParameterOverride(
        parameter_id="ceiling.tile.thickness_m",value_kind="length",unit="m",value=.1),))
    for request,match in (
        ((*requests[:-1], bad_ceiling), "atomically"),
        ((requests[0],), "dependent insert"),
        ((requests[-1],requests[-1]), "duplicate"),
        ((replace(requests[-1],ceiling_id="ceiling:missing"),), "preserve"),
        ((replace(requests[-1],assembly_id="assembly:missing"),), "preserve"),
        ((object(),), "unsupported request"),
        ((replace(requests[-1],parameter_overrides=(CeilingParameterOverride(
            parameter_id="ceiling.tile.thickness_m",value_kind="length",unit="m",value=.1),)),), "atomically"),
    ):
        with pytest.raises(AssemblyRegenerationError,match=match):
            regenerate_building_assemblies(model,request)
        assert model.to_json() == frozen


def test_changed_member_population_keeps_semantic_survivors_and_other_owners():
    from oabm.building_assemblies import ParameterOverride
    model = synthetic_enclosed_room_assembly()
    request = replace(synthetic_room_requests()[3],parameter_overrides=(ParameterOverride(
        parameter_id="wall.stud.spacing_m",value_kind="length",unit="m",value=.6096),))
    after = regenerate_building_assemblies(model,(request,))
    old_parts = {p.part_key:p for p in model.assembly_components if p.assembly_id == request.assembly_id}
    new_parts = {p.part_key:p for p in after.assembly_components if p.assembly_id == request.assembly_id}
    assert len(old_parts) != len(new_parts)
    assert all(old_parts[key].id == new_parts[key].id for key in old_parts.keys() & new_parts.keys())
    assert tuple(p for p in model.assembly_components if p.assembly_id != request.assembly_id) == tuple(
        p for p in after.assembly_components if p.assembly_id != request.assembly_id)
    assert BuildingModel.from_dict(after.to_dict()).to_json() == after.to_json()


def test_routed_output_is_preserved_and_requires_upstream_checkpoint():
    from oabm.model import ElectricalEquipment, Point3, Port, Pose, Vector3, Route, Polyline3D
    model = synthetic_enclosed_room_assembly()
    equipment = tuple(ElectricalEquipment(id=f"equipment:guard-{i}",equipment_type="junction",
        level_id="level:proof",pose=Pose(position=Point3(x=x,y=1.,z=2.6))) for i,x in enumerate((1.,2.)))
    ports = tuple(Port(id=f"port:guard-{i}",owner_id=e.id,domain="electrical",role="source" if i == 0 else "load",
        pose=e.pose,direction=Vector3(x=1. if i == 0 else -1.,y=0.,z=0.)) for i,e in enumerate(equipment))
    route = Route(id="route:guard",route_type="emt",start_port_id=ports[0].id,end_port_id=ports[1].id,
        centerline=Polyline3D(points=tuple(e.pose.position for e in equipment)),nominal_diameter_m=.02)
    routed = replace(model,electrical_equipment=equipment,ports=ports,routes=(route,))
    frozen = routed.to_json()
    with pytest.raises(AssemblyRegenerationError,match="requires_pre_routing_model"):
        regenerate_building_assemblies(routed,edited_room_requests())
    assert routed.to_json() == frozen


def test_custom_catalog_is_not_silently_replaced_with_builtin_recipe():
    from oabm.building_assemblies import DEFAULT_PRESET_CATALOG, build_wall_assembly
    source = synthetic_enclosed_room_source()
    request = synthetic_room_requests()[3]
    custom = replace(DEFAULT_PRESET_CATALOG,catalog_id="catalog:caller-owned")
    model = build_wall_assembly(source,request,catalog=custom)
    frozen = model.to_json()
    with pytest.raises(AssemblyRegenerationError,match="catalog identity/revision mismatch"):
        regenerate_building_assemblies(model,(request,))
    assert model.to_json() == frozen

    assert regenerate_building_assemblies(model,(request,),wall_catalog=custom).to_json() == frozen
