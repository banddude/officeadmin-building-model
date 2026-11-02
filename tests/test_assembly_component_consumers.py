"""Focused 1.6 GLB/quantity consumer tests over synthetic canonical records."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import replace
from collections import Counter

import pytest

from oabm.assembly_geometry import straight_round_wire_m
from oabm.exports.gltf import (
    _assembly_component_mesh,
    _preflight_assembly_preview_vertices,
    _require_assembly_float32_precision,
    build_glb_bytes,
    to_glb,
)
from oabm.model import (
    AssemblyComponent,
    Box3D,
    BuildingModel,
    ContractError,
    HangerAttachment,
    Opening,
    PanelGeometry,
    Point2D,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Profile2D,
    ProfileExtrusion,
    Provenance,
    Quaternion,
    Size3,
    Space,
    Vector3,
    WireSweep,
    compute_assembly_input_digest,
    compute_assembly_digests,
    stable_assembly_component_id,
    upgrade_to_v1_6,
)
from oabm.quantities import extract_quantities

# Reuse the canonical contract test's public-safe builders rather than add a
# parallel serializer/validation shortcut in this consumer suite.
from test_building_assemblies_contract import (
    _assembly,
    _component,
    _component_provenance,
    _door,
    _source_model,
)


def _read_glb(payload: bytes):
    magic, version, length = struct.unpack_from("<III", payload, 0)
    assert (magic, version, length) == (0x46546C67, 2, len(payload))
    json_length, json_type = struct.unpack_from("<II", payload, 12)
    assert json_type == 0x4E4F534A
    gltf = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip(" "))
    bin_header = 20 + json_length
    bin_length, bin_type = struct.unpack_from("<II", payload, bin_header)
    assert bin_type == 0x004E4942
    binary = payload[bin_header + 8:bin_header + 8 + bin_length]
    return gltf, binary


def _positions(gltf, binary, node):
    primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
    position_accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
    view = gltf["bufferViews"][position_accessor["bufferView"]]
    start = view.get("byteOffset", 0) + gltf.get("accessors", [])[primitive["attributes"]["POSITION"]].get("byteOffset", 0)
    return [struct.unpack_from("<3f", binary, start + 12 * index)
            for index in range(position_accessor["count"])], primitive


def _world_positions(gltf, binary, node):
    """Decode actual mesh positions and apply the node's glTF Y-up transform."""
    positions, primitive = _positions(gltf, binary, node)
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    qx, qy, qz, qw = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    result = []
    for x, y, z in positions:
        x, y, z = x * sx, y * sy, z * sz
        result.append((
            (1 - 2 * (qy*qy + qz*qz)) * x + 2 * (qx*qy - qz*qw) * y + 2 * (qx*qz + qy*qw) * z + tx,
            2 * (qx*qy + qz*qw) * x + (1 - 2 * (qx*qx + qz*qz)) * y + 2 * (qy*qz - qx*qw) * z + ty,
            2 * (qx*qz - qy*qw) * x + 2 * (qy*qz + qx*qw) * y + (1 - 2 * (qx*qx + qy*qy)) * z + tz,
        ))
    return result, primitive


def _valid_wall_with_door_and_window():
    observed = Provenance(source_kind="plan", source_id="sheet:synthetic",
                          method="manual-synthetic", derivation="observed")
    door = _door(position=(1.9, 0.0, 1.05), size=(0.9, 0.2, 2.1),
                 opening_id="opening:door")
    window = Opening(
        id="opening:window", host_id="wall:north", opening_type="window",
        pose=Pose(position=Point3(x=3.4, y=0.0, z=1.8)),
        size=Size3(x=0.9, y=0.2, z=0.8), provenance=(observed,),
    )
    source = _source_model(opening=door)
    source = replace(source, openings=(door, window))

    c_profile = Profile2D(
        kind="c_profile", overall_width_m=0.05, overall_depth_m=0.08,
        web_thickness_m=0.006, flange_thickness_m=0.008, return_lip_m=0.004,
    )
    steel_material = Provenance(source_kind="plan", source_id="sheet:synthetic",
        method="material-identification", derivation="observed", scope_paths=("material_id",))
    gypsum_material = Provenance(source_kind="plan", source_id="sheet:synthetic",
        method="material-identification", derivation="observed", scope_paths=("material_id",))
    stud = _component(
        "assembly:wall", part_key="stud:station:0", kind="stud",
        geometry=ProfileExtrusion(profile=c_profile,
            pose=Pose(position=Point3(x=0.1, y=-0.04, z=0.0)), length_m=3.0),
        material_id="material:steel", material_basis="plan_specified",
        provenance=_component_provenance("geometry") + (steel_material,),
    )
    drywall = _component(
        "assembly:wall", part_key="drywall:left:face:left:panel:0", kind="drywall_panel",
        geometry=PanelGeometry(
            boundary=Polygon3D(points=(
                Point3(x=0.2, y=-0.09, z=0.0), Point3(x=1.1, y=-0.09, z=0.0),
                Point3(x=1.1, y=-0.09, z=3.0), Point3(x=0.2, y=-0.09, z=3.0),
            )), thickness_m=0.012, normal=Vector3(x=0.0, y=1.0, z=0.0),
        ), material_id="material:gypsum-board", material_basis="plan_specified",
        provenance=_component_provenance("geometry") + (gypsum_material,),
    )
    door_leaf = _component(
        "assembly:door", part_key="door:leaf", kind="door_leaf", opening_id=door.id,
        geometry=Box3D(pose=Pose(position=Point3(x=1.9, y=0.0, z=1.05)),
                       size=Size3(x=0.82, y=0.035, z=2.0)),
    )
    window_glazing = _component(
        "assembly:window", part_key="window:glazing", kind="window_glazing",
        opening_id=window.id,
        geometry=PanelGeometry(
            boundary=Polygon3D(points=(
                Point3(x=3.05, y=0.0, z=1.5), Point3(x=3.75, y=0.0, z=1.5),
                Point3(x=3.75, y=0.0, z=2.1), Point3(x=3.05, y=0.0, z=2.1),
            )), thickness_m=0.02, normal=Vector3(x=0.0, y=-1.0, z=0.0),
        ),
    )
    wall_parts = (stud, drywall)
    door_parts = (door_leaf,)
    window_parts = (window_glazing,)
    wall_assignment = _assembly(source, wall_parts, assembly_id="assembly:wall")
    door_assignment = _assembly(source, door_parts, kind="opening_insert", host_id=door.id,
                                assembly_id="assembly:door")
    window_assignment = _assembly(source, window_parts, kind="opening_insert", host_id=window.id,
                                  assembly_id="assembly:window")
    model = upgrade_to_v1_6(source)
    return replace(
        model,
        building_assemblies=(wall_assignment, door_assignment, window_assignment),
        assembly_components=wall_parts + door_parts + window_parts,
    )


def _replace_components_and_rehash(model, components):
    assignments = []
    for assignment in model.building_assemblies:
        owned = tuple(row for row in components if row.assembly_id == assignment.id)
        input_digest, output_digest = compute_assembly_digests(model, assignment, owned)
        assignments.append(replace(assignment, input_digest=input_digest,
                                   output_digest=output_digest))
    return replace(model, building_assemblies=tuple(assignments),
                   assembly_components=tuple(components))


def test_glb_emits_one_indexed_node_per_physical_component_and_suppresses_host(tmp_path):
    model = _valid_wall_with_door_and_window()
    first = build_glb_bytes(model)
    second = build_glb_bytes(model)
    assert first == second
    gltf, binary = _read_glb(first)
    by_name = {node["name"]: node for node in gltf["nodes"]}
    component_ids = {component.id for component in model.assembly_components}

    assert component_ids <= set(by_name)
    assert "wall:north" not in by_name  # aggregate host is not a second solid
    assert len([node for node in gltf["nodes"] if node["name"] in component_ids]) == len(component_ids)
    assert len(gltf["meshes"]) == len(component_ids)
    assert all(by_name[component_id]["mesh"] is not None for component_id in component_ids)
    for component in model.assembly_components:
        node = by_name[component.id]
        extras = node["extras"]
        assert extras["canonical_id"] == component.id
        assert extras["role"] == component.component_kind
        assert extras["owner_id"] == component.assembly_id
        assert extras["part_key"] == component.part_key
        assert extras["geometry_basis"] == component.geometry_basis
        positions, primitive = _positions(gltf, binary, node)
        index_accessor = gltf["accessors"][primitive["indices"]]
        index_view = gltf["bufferViews"][index_accessor["bufferView"]]
        index_bytes = binary[index_view.get("byteOffset", 0):
                             index_view.get("byteOffset", 0) + index_view["byteLength"]]
        indices = struct.unpack(f"<{index_accessor['count']}I", index_bytes)
        assert positions and len(indices) >= 36 and len(indices) % 3 == 0
        assert min(indices) == index_accessor["min"][0]
        assert max(indices) == index_accessor["max"][0] < len(positions)
        assert all(max(point[axis] for point in positions) > min(point[axis] for point in positions)
                   for axis in range(3))
        canonical_positions = [(x, -minus_y, z) for x, z, minus_y in positions]
        if extras["assembly_kind"] == "wall":
            # No physical child triangles occupy either source opening void.
            assert all(not (1.45 <= x <= 2.35 and 0 <= z <= 2.1)
                       for x, _y, z in canonical_positions)
            assert all(not (2.95 <= x <= 3.85 and 1.4 <= z <= 2.2)
                       for x, _y, z in canonical_positions)
        else:
            opening = next(row for row in model.openings if row.id == extras["opening_id"])
            sill = opening.pose.position.z - opening.size.z / 2
            for x, _y, z in canonical_positions:
                assert opening.pose.position.x - opening.size.x/2 <= x <= opening.pose.position.x + opening.size.x/2
                assert sill <= z <= sill + opening.size.z
    assert all(by_name[component.id]["extras"]["assembly_kind"] == "opening_insert"
               for component in model.assembly_components
               if component.opening_id is not None)

    output = tmp_path / "components.glb"
    summary = to_glb(model, output)
    assert summary["assembly_components"] == len(component_ids)
    assert summary["aggregate_hosts_suppressed"] == 1
    assert output.read_bytes() == first


def test_component_groups_and_caller_ghosting_apply_without_duplicates():
    model = _valid_wall_with_door_and_window()
    component = model.assembly_components[0]
    payload = build_glb_bytes(
        model, dimmed_ids=(component.id,),
        groups={"cutaway": (component.id,)}, hidden_groups=("cutaway",),
    )
    gltf, _ = _read_glb(payload)
    by_name = {node["name"]: node for node in gltf["nodes"]}
    assert by_name[component.id]["extras"]["display"] == "dimmed (caller-supplied)"
    group = by_name["group:cutaway"]
    assert group["extras"]["hidden_by_default"] is True
    assert group["extensions"]["KHR_node_visibility"]["visible"] is False
    assert len([node for node in gltf["nodes"] if node["name"] == component.id]) == 1

    # Grouping the suppressed wall host groups its owned wall parts and the
    # separately owned insert parts that point back to that parent wall.
    # Layer selection also exposes the canonical host as one meshless node;
    # group members count scene children, not physical material quantities.
    host_payload = build_glb_bytes(
        model, groups={"whole-wall": ("wall:north",)},
        hidden_groups=("whole-wall",),
    )
    host_gltf, host_binary = _read_glb(host_payload)
    host_nodes = {node["name"]: node for node in host_gltf["nodes"]}
    host_group = host_nodes["group:whole-wall"]
    child_names = {host_gltf["nodes"][index]["name"] for index in host_group["children"]}
    component_ids = {component.id for component in model.assembly_components}
    assert child_names == component_ids | {"wall:north"}
    assert host_group["extras"]["members"] == len(component_ids) + 1
    host = host_nodes["wall:north"]
    assert "mesh" not in host
    assert host["extras"]["canonical_id"] == "wall:north"
    assert host["extras"]["canonical_type"] == "wall"
    assert {host_gltf["nodes"][index]["name"] for index in host_group["children"]
            if "mesh" in host_gltf["nodes"][index]} == component_ids

    # Grouping adds semantic nodes only: identical binary geometry/mesh records
    # and exactly the same mesh instances as the ordinary physical preview.
    plain_gltf, plain_binary = _read_glb(build_glb_bytes(model))
    assert host_binary == plain_binary
    assert host_gltf["meshes"] == plain_gltf["meshes"]
    assert Counter(node["name"] for node in host_gltf["nodes"] if "mesh" in node) == Counter(
        node["name"] for node in plain_gltf["nodes"] if "mesh" in node)
    assert all(sum(node["name"] == identity for node in host_gltf["nodes"]) == 1
               for identity in component_ids | {"wall:north"})
    reached = set()
    pending = list(host_gltf["scenes"][host_gltf.get("scene", 0)]["nodes"])
    while pending:
        index = pending.pop()
        assert index not in reached, "duplicate scene parent or cycle"
        reached.add(index)
        pending.extend(host_gltf["nodes"][index].get("children", ()))
    assert reached == set(range(len(host_gltf["nodes"])))


def test_glb_fails_closed_for_insert_without_aggregate_wall_void_geometry():
    model = _valid_wall_with_door_and_window()
    inserts_only = replace(
        model,
        building_assemblies=model.building_assemblies[1:],
        assembly_components=model.assembly_components[2:],
    )
    with pytest.raises(ValueError, match="opening inserts require their wall host"):
        build_glb_bytes(inserts_only)


def test_glb_and_quantities_reject_stale_assembly_component_output_digest():
    model = _valid_wall_with_door_and_window()
    model.assembly_components[0].attributes["mutated-after-validation"] = True
    with pytest.raises(ContractError, match="output_digest is stale"):
        build_glb_bytes(model)
    with pytest.raises(ContractError, match="output_digest is stale"):
        extract_quantities(model)


def test_component_mesh_kernel_supports_profile_panel_and_round_wire():
    profile = ProfileExtrusion(
        profile=Profile2D(kind="c_profile", overall_width_m=.06, overall_depth_m=.08,
                          web_thickness_m=.006, flange_thickness_m=.008, return_lip_m=.004),
        pose=Pose(position=Point3(x=1, y=2, z=3)), length_m=2.0,
    )
    panel = PanelGeometry(
        boundary=Polygon3D(points=(Point3(x=0,y=0,z=0), Point3(x=2,y=0,z=0),
                                   Point3(x=2,y=0,z=1), Point3(x=0,y=0,z=1))),
        thickness_m=.02, normal=Vector3(x=0,y=-1,z=0),
    )
    wire = WireSweep(centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),
                                                   Point3(x=0,y=0,z=1))),
                     diameter_m=.01, end_treatment="cut")
    profile_component = _component("assembly:test", part_key="stud:0", geometry=profile)
    panel_component = _component("assembly:test", part_key="panel:0", kind="drywall_panel", geometry=panel)
    wire_component = _component("assembly:test", part_key="hanger:0", kind="hanger_wire", geometry=wire,
        hanger_attachment=HangerAttachment(
            top_host_id="slab:deck", top_anchor=Point3(x=0,y=0,z=1),
            bottom_runner_id="component:runner", bottom_anchor=Point3(x=0,y=0,z=0),
        ))
    for component in (profile_component, panel_component, wire_component):
        vertices, indices = _assembly_component_mesh(component)
        assert vertices and indices and len(indices) % 3 == 0
        assert max(indices) < len(vertices)
        assert all(math.isfinite(coordinate) for point in vertices for coordinate in point)
    wire_mesh = straight_round_wire_m(wire.centerline.points[0], wire.centerline.points[-1], .005)
    assert wire_mesh.volume_m3 > 0


def test_glb_preflights_model_wide_vertex_budget_before_allocating_meshes():
    points = tuple(Point3(x=0, y=0, z=index * .1) for index in range(33))
    wire = WireSweep(centerline=Polyline3D(points=points), diameter_m=.003, end_treatment="cut")
    components = tuple(
        _component(f"assembly:bounded:{index}", part_key=f"hanger:{index}",
                   kind="hanger_wire", geometry=wire,
                   hanger_attachment=HangerAttachment(
                       top_host_id="slab:deck", top_anchor=points[0],
                       bottom_runner_id="component:runner", bottom_anchor=points[-1],
                   ))
        for index in range(400)
    )
    per_assignment_vertices = (len(points) - 1) * 2 * 10
    assert per_assignment_vertices < 250_000
    assert per_assignment_vertices * len(components) > 250_000
    with pytest.raises(ValueError, match="model-wide vertex budget"):
        _preflight_assembly_preview_vertices(components)


@pytest.mark.parametrize("origin", (1e8, 1e9))
def test_large_world_origin_panel_triangulates_locally_then_fails_float32_precision(origin):
    from oabm.model import Ceiling, Slab

    panel = PanelGeometry(
        boundary=Polygon3D(points=(
            Point3(x=origin, y=origin, z=2),
            Point3(x=origin + 1, y=origin, z=2),
            Point3(x=origin + 1, y=origin + 1, z=2),
            Point3(x=origin, y=origin + 1, z=2),
        )),
        thickness_m=.02,
        normal=Vector3(x=0, y=0, z=1),
    )
    component = _component("assembly:wall", part_key="panel:far", kind="drywall_panel",
                           geometry=panel)
    vertices, indices = _assembly_component_mesh(component)
    assert vertices and indices and len(indices) % 3 == 0
    assert max(point[0] for point in vertices) - min(point[0] for point in vertices) == pytest.approx(1)
    assert max(point[1] for point in vertices) - min(point[1] for point in vertices) == pytest.approx(1)
    with pytest.raises(ValueError, match="float32 positional precision"):
        _require_assembly_float32_precision(component.id, vertices)

    provenance = _source_model().walls[0].provenance
    footprint = Polygon3D(points=(
        Point3(x=origin, y=origin, z=2), Point3(x=origin + 4, y=origin, z=2),
        Point3(x=origin + 4, y=origin + 3, z=2), Point3(x=origin, y=origin + 3, z=2),
    ))
    ceiling = Ceiling(id="ceiling:far", level_id="level:main", footprint=footprint,
                      thickness_m=.2, provenance=provenance)
    slab = Slab(id="slab:far", level_id="level:main",
                footprint=Polygon3D(points=tuple(
                    Point3(x=point.x, y=point.y, z=3) for point in footprint.points)),
                thickness_m=.2, provenance=provenance)
    source = _source_model(ceiling=ceiling, slabs=(slab,))
    component = _component("assembly:ceiling", part_key="tile:far", kind="tile",
                           geometry=panel)
    assignment = _assembly(source, (component,), kind="suspended_ceiling",
                           host_id=ceiling.id, assembly_id="assembly:ceiling",
                           deck_id=slab.id, params=())
    model = upgrade_to_v1_6(source)
    model = replace(model, building_assemblies=(assignment,),
                    assembly_components=(component,))
    with pytest.raises(ValueError, match="float32 positional precision"):
        build_glb_bytes(model)


def test_assembly_quantities_use_component_identity_and_suppress_duplicate_wall_billing():
    model = _valid_wall_with_door_and_window()
    report = extract_quantities(model)
    categories = {item.category for item in report.items}
    assert "wall_length" in categories
    assert "wall_face_area" not in categories
    assert "wall_volume" not in categories
    assert "assembly_component_count" in categories
    assert "assembly_component_parametric_material_volume" in categories
    assert "drywall_modeled_area" in categories
    assert "assembly_component_geometry_volume" in categories
    drywall_id = next(component.id for component in model.assembly_components
                      if component.component_kind == "drywall_panel")
    drywall_area = next(item for item in report.items if item.category == "drywall_modeled_area")
    assert drywall_area.quantity == pytest.approx(2.7)
    assert drywall_id in drywall_area.source_entity_ids
    counts = [item for item in report.items if item.category == "assembly_component_count"]
    assert {source_id for item in counts for source_id in item.source_entity_ids} == {
        component.id for component in model.assembly_components
    }
    warning_codes = {warning.code for warning in report.warnings}
    assert "assemblies_unresolved" not in warning_codes
    assert "drywall_sheet_purchase_unresolved" in warning_codes
    assert "assembly_engineering_approval_not_evaluated" in warning_codes
    assert all(any(source_id in item.source_entity_ids for source_id in
                   (component.id,)) for component in model.assembly_components
               for item in counts if item.item_type == component.component_kind)


def test_envelope_components_have_separate_nonmaterial_volume_category():
    model = _valid_wall_with_door_and_window()
    envelope = replace(model.assembly_components[0],
                       geometry_basis="caller_selected_envelope", material_id=None,
                       material_basis="unknown")
    components = (envelope,) + model.assembly_components[1:]
    model = _replace_components_and_rehash(model, components)
    report = extract_quantities(model)
    envelope_lines = [item for item in report.items
                      if item.category == "assembly_component_envelope_volume"]
    assert len(envelope_lines) == 1
    assert envelope.id in envelope_lines[0].source_entity_ids
    assert not any(item.category == "assembly_component_parametric_material_volume" and
                   envelope.id in item.source_entity_ids for item in report.items)
    assert "assembly_envelope_volume_unknown" in {warning.code for warning in report.warnings}


def test_box_and_panel_envelope_area_are_reported_once_each():
    model = _valid_wall_with_door_and_window()
    box = next(component for component in model.assembly_components
               if component.component_kind == "door_leaf")
    panel = next(component for component in model.assembly_components
                 if component.component_kind == "window_glazing")
    envelope_ids = {box.id, panel.id}
    components = tuple(
        replace(component, geometry_basis="caller_selected_envelope",
                material_id=None, material_basis="unknown")
        if component.id in envelope_ids else component
        for component in model.assembly_components
    )
    report = extract_quantities(_replace_components_and_rehash(model, components))
    areas = [item for item in report.items
             if item.category == "assembly_component_envelope_area"]
    assert sum(box.id in item.source_entity_ids for item in areas) == 1
    assert sum(panel.id in item.source_entity_ids for item in areas) == 1
    box_area = next(item.quantity for item in areas if box.id in item.source_entity_ids)
    panel_area = next(item.quantity for item in areas if panel.id in item.source_entity_ids)
    assert box_area == pytest.approx(2 * (.82 * .035 + .82 * 2.0 + .035 * 2.0))
    assert panel_area == pytest.approx(.7 * .6)


def test_glb_and_quantities_include_ceiling_runner_and_hanger_wire_geometry():
    from test_building_assemblies_contract import _assembly
    from oabm.model import Ceiling, Slab

    provenance = (Provenance(source_kind="plan", source_id="sheet:synthetic",
                             method="manual-synthetic", derivation="observed"),)
    ceiling = Ceiling(
        id="ceiling:grid", level_id="level:main",
        footprint=Polygon3D(points=(Point3(x=0,y=0,z=2), Point3(x=4,y=0,z=2),
                                    Point3(x=4,y=3,z=2), Point3(x=0,y=3,z=2))),
        thickness_m=.2, provenance=provenance,
    )
    slab = Slab(
        id="slab:deck", level_id="level:main",
        footprint=Polygon3D(points=(Point3(x=0,y=0,z=3), Point3(x=4,y=0,z=3),
                                    Point3(x=4,y=3,z=3), Point3(x=0,y=3,z=3))),
        thickness_m=.2, provenance=provenance,
    )
    source = _source_model(ceiling=ceiling, slabs=(slab,))
    rotation = Quaternion(x=0, y=math.sqrt(.5), z=0, w=math.sqrt(.5))
    runner = _component("assembly:ceiling", part_key="runner:main:0",
        kind="tbar_main_runner",
        geometry=ProfileExtrusion(
            profile=Profile2D(kind="t_profile", overall_width_m=.1, overall_depth_m=.05,
                              web_thickness_m=.01, flange_thickness_m=.01),
            pose=Pose(position=Point3(x=0,y=.5,z=2), rotation=rotation), length_m=3.0,
        ))
    top = Point3(x=1.5,y=.5,z=2.8)
    bottom = Point3(x=1.5,y=.5,z=1.95)
    hanger = _component("assembly:ceiling", part_key="hanger:station:0",
        kind="hanger_wire",
        geometry=WireSweep(centerline=Polyline3D(points=(top,bottom)),
                           diameter_m=.003, end_treatment="cut"),
        hanger_attachment=HangerAttachment(
            top_host_id="slab:deck", top_anchor=top,
            bottom_runner_id=runner.id, bottom_anchor=bottom,
        ))
    parts = (runner, hanger)
    assignment = _assembly(source, parts, kind="suspended_ceiling",
        host_id="ceiling:grid", assembly_id="assembly:ceiling", deck_id="slab:deck", params=())
    model = upgrade_to_v1_6(source)
    model = replace(model, building_assemblies=(assignment,), assembly_components=parts)

    payload = build_glb_bytes(model)
    assert payload == build_glb_bytes(model)
    gltf, binary = _read_glb(payload)
    nodes = {node["name"]: node for node in gltf["nodes"]}
    assert {runner.id, hanger.id} <= set(nodes)
    for component in parts:
        positions, primitive = _positions(gltf, binary, nodes[component.id])
        assert len(positions) >= 8 and "indices" in primitive

    # The selected source Slab is the physical deck, not an extra assembly
    # component. Its canonical footprint is the top face and thickness runs
    # down. Decode the emitted meshes (including glTF node transforms) in the
    # actual +Y-up frame and verify the wire cap ends on the slab underside.
    slab_nodes = [node for node in gltf["nodes"] if node.get("name") == slab.id]
    assert len(slab_nodes) == 1
    deck_positions, _ = _world_positions(gltf, binary, slab_nodes[0])
    deck_heights = [point[1] for point in deck_positions]
    deck_top_y = max(deck_heights)
    deck_underside_y = min(deck_heights)
    assert deck_top_y == pytest.approx(3.0, abs=1e-6)
    assert deck_underside_y == pytest.approx(2.8, abs=1e-6)
    wire_positions, _ = _world_positions(gltf, binary, nodes[hanger.id])
    wire_top_ring = [point for point in wire_positions
                     if abs(point[1] - deck_underside_y) <= 1e-6]
    assert wire_top_ring
    wire_top_center_yup = tuple(
        math.fsum(point[axis] for point in wire_top_ring) / len(wire_top_ring)
        for axis in range(3)
    )
    assert wire_top_center_yup == pytest.approx((top.x, top.z, -top.y), abs=1e-6)

    space = Space(
        id="space:under-deck", level_id="level:main",
        footprint=Polygon3D(points=(
            Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0),
            Point3(x=4, y=3, z=0), Point3(x=0, y=3, z=0),
        )),
    )
    space_model = replace(model, spaces=(space,))
    space_gltf, space_binary = _read_glb(build_glb_bytes(space_model))
    space_node = {node["name"]: node for node in space_gltf["nodes"]}[space.id]
    space_positions, _ = _world_positions(space_gltf, space_binary, space_node)
    assert min(point[1] for point in space_positions) == pytest.approx(0.0, abs=1e-6)
    assert max(point[1] for point in space_positions) == pytest.approx(0.02, abs=1e-6)

    floor_slab = Slab(
        id="slab:floor", level_id="level:main",
        footprint=Polygon3D(points=(
            Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0),
            Point3(x=4, y=3, z=0), Point3(x=0, y=3, z=0),
        )),
        thickness_m=.1, provenance=provenance,
    )
    floor_source = replace(source, slabs=(slab, floor_slab))
    floor_assignment = _assembly(
        floor_source, parts, kind="suspended_ceiling", host_id="ceiling:grid",
        assembly_id="assembly:ceiling", deck_id=slab.id, params=(),
    )
    floor_model = replace(
        upgrade_to_v1_6(floor_source), building_assemblies=(floor_assignment,),
        assembly_components=parts, spaces=(space,),
    )
    floor_gltf, floor_binary = _read_glb(build_glb_bytes(floor_model))
    floor_nodes = {node["name"]: node for node in floor_gltf["nodes"]}
    floor_positions, _ = _world_positions(
        floor_gltf, floor_binary, floor_nodes[floor_slab.id]
    )
    assert min(point[1] for point in floor_positions) == pytest.approx(0.0, abs=1e-6)
    assert max(point[1] for point in floor_positions) == pytest.approx(0.1, abs=1e-6)
    floor_space_positions, _ = _world_positions(
        floor_gltf, floor_binary, floor_nodes[space.id]
    )
    assert min(point[1] for point in floor_space_positions) == pytest.approx(0.1, abs=1e-6)
    assert max(point[1] for point in floor_space_positions) == pytest.approx(0.12, abs=1e-6)

    # An explicit empty 1.6 upgrade is not a ceiling assembly and retains its
    # legacy upward slab preview instead of opting in to the new deck rule.
    empty_upgrade_gltf, empty_upgrade_binary = _read_glb(
        build_glb_bytes(upgrade_to_v1_6(replace(source, spaces=(space,))))
    )
    empty_upgrade_nodes = {node["name"]: node for node in empty_upgrade_gltf["nodes"]}
    old_slab_positions, _ = _world_positions(
        empty_upgrade_gltf, empty_upgrade_binary, empty_upgrade_nodes[slab.id]
    )
    old_slab_heights = [point[1] for point in old_slab_positions]
    assert min(old_slab_heights) == pytest.approx(3.0, abs=1e-6)
    assert max(old_slab_heights) == pytest.approx(3.2, abs=1e-6)
    old_space_positions, _ = _world_positions(
        empty_upgrade_gltf, empty_upgrade_binary, empty_upgrade_nodes[space.id]
    )
    assert min(point[1] for point in old_space_positions) == pytest.approx(3.2, abs=1e-6)
    assert max(point[1] for point in old_space_positions) == pytest.approx(3.22, abs=1e-6)

    sloped_footprint = Polygon3D(points=(
        Point3(x=0, y=0, z=3), Point3(x=4, y=0, z=3),
        Point3(x=4, y=3, z=3.1), Point3(x=0, y=3, z=3),
    ))
    sloped_slab = replace(slab, footprint=sloped_footprint)
    sloped_source = replace(source, slabs=(sloped_slab,))
    sloped_assignment = replace(
        assignment,
        input_digest=compute_assembly_input_digest(sloped_source, assignment),
    )
    with pytest.raises(ContractError, match="Slab deck footprint must be horizontal"):
        replace(model, slabs=(sloped_slab,), building_assemblies=(sloped_assignment,))

    wire = next(item for item in extract_quantities(model).items
                if item.category == "assembly_component_length" and
                hanger.id in item.source_entity_ids)
    assert wire.quantity == pytest.approx(.85)
    report = extract_quantities(model)
    assert "ceiling_area" not in {item.category for item in report.items}
    assert "ceiling_volume" not in {item.category for item in report.items}
    assert {"slab_area", "slab_volume"} <= {item.category for item in report.items}

    # Bent centerlines render segment meshes but do not claim a unioned solid
    # volume until the shared geometry kernel defines a join.
    bent_bottom = Point3(x=1.6,y=.5,z=1.95)
    bent_hanger = replace(hanger, geometry=WireSweep(
        centerline=Polyline3D(points=(top, Point3(x=1.5,y=.5,z=2.5),
                                     Point3(x=1.6,y=.5,z=2.5), bent_bottom)),
        diameter_m=.003, end_treatment="cut",
    ), hanger_attachment=replace(hanger.hanger_attachment,
                                  bottom_anchor=bent_bottom))
    bent_parts = (runner, bent_hanger)
    bent_assignment = _assembly(source, bent_parts, kind="suspended_ceiling",
        host_id="ceiling:grid", assembly_id="assembly:ceiling", deck_id="slab:deck", params=())
    bent_model = replace(upgrade_to_v1_6(source), building_assemblies=(bent_assignment,),
                         assembly_components=bent_parts)
    bent_gltf, bent_binary = _read_glb(build_glb_bytes(bent_model))
    bent_node = {node["name"]: node for node in bent_gltf["nodes"]}[bent_hanger.id]
    bent_positions, bent_primitive = _positions(bent_gltf, bent_binary, bent_node)
    assert len(bent_positions) > 20 and "indices" in bent_primitive
    bent_report = extract_quantities(bent_model)
    bent_sources = {source_id for warning in bent_report.warnings
                    if warning.code == "wire_volume_join_undefined"
                    for source_id in warning.source_entity_ids}
    assert bent_hanger.id in bent_sources
    assert not any(item.category in {"assembly_component_geometry_volume",
                                     "assembly_component_parametric_material_volume"}
                   and bent_hanger.id in item.source_entity_ids for item in bent_report.items)


def test_stable_wall_producer_fixture_flows_to_glb_and_quantities():
    from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_assembly

    model = synthetic_two_opening_wall_assembly()
    assert model.schema_version == "1.6.0"
    assert len(model.assembly_components) == 56
    assembly = model.building_assemblies[0]
    assert assembly.assembly_kind == "wall"
    assert assembly.host_representation == "aggregate_host"
    clearance = [parameter for parameter in assembly.resolved_parameters
                 if parameter.parameter_id == "wall.opening_clearance_m"]
    assert len(clearance) == 1 and clearance[0].value == 0.0
    assert not any(parameter.parameter_id == "wall.opening.clearance_m"
                   for parameter in assembly.resolved_parameters)
    assert not any(component.component_kind in {
        "door_leaf", "door_frame", "window_frame", "window_glazing"
    } for component in model.assembly_components)

    first = build_glb_bytes(model)
    second = build_glb_bytes(synthetic_two_opening_wall_assembly())
    assert first == second
    gltf, binary = _read_glb(first)
    nodes = {node["name"]: node for node in gltf["nodes"]}
    component_ids = {component.id for component in model.assembly_components}
    assert assembly.host_id not in nodes
    assert component_ids <= set(nodes)
    assert len([node for node in gltf["nodes"] if node["name"] in component_ids]) == 56
    assert len(gltf["meshes"]) == 56
    assert all(_positions(gltf, binary, nodes[component_id])[0]
               for component_id in component_ids)
    framing = {
        component.id: next((record.attributes for record in component.provenance
                           if record.attributes and record.attributes.get("framing_role")), {})
        for component in model.assembly_components
    }
    roles = Counter(row.get("framing_role") for row in framing.values())
    assert roles["king"] == roles["jack"] == roles["header_ply"] == 4
    assert roles["header_spacer"] == 2
    for component in model.assembly_components:
        role = framing[component.id].get("framing_role")
        if role not in {"king", "jack"}:
            continue
        emitted, _ = _world_positions(gltf, binary, nodes[component.id])
        assert min(point[1] for point in emitted) == pytest.approx(.0381, abs=1e-6)
        expected_top = 2.4384 - 2 * .0381 if role == "king" else 2.032
        assert max(point[1] for point in emitted) == pytest.approx(expected_top, abs=1e-6)
        assert nodes[component.id]["extras"]["part_key"] == component.part_key
    for opening in model.openings:
        jacks = [key for key, row in framing.items()
                 if row.get("framing_role") == "jack" and row.get("opening_id") == opening.id]
        plies = [key for key, row in framing.items()
                 if row.get("framing_role") == "header_ply" and row.get("opening_id") == opening.id]
        assert len(jacks) == len(plies) == 2
        for jack_id in jacks:
            jack_points, _ = _world_positions(gltf, binary, nodes[jack_id])
            for ply_id in plies:
                ply_points, _ = _world_positions(gltf, binary, nodes[ply_id])
                assert max(point[1] for point in jack_points) == pytest.approx(
                    min(point[1] for point in ply_points), abs=1e-6)

    report = extract_quantities(model)
    categories = {item.category for item in report.items}
    assert "wall_length" in categories
    assert "wall_face_area" not in categories and "wall_volume" not in categories
    counts = [item for item in report.items if item.category == "assembly_component_count"]
    assert {source_id for item in counts for source_id in item.source_entity_ids} == component_ids
    drywall_ids = {component.id for component in model.assembly_components
                   if component.component_kind == "drywall_panel"}
    drywall_items = [item for item in report.items if item.category == "drywall_modeled_area"]
    assert drywall_items and {source_id for item in drywall_items
                              for source_id in item.source_entity_ids} == drywall_ids
    assert math.fsum(item.quantity for item in drywall_items) > 0
    warnings = {warning.code for warning in report.warnings}
    assert "drywall_sheet_purchase_unresolved" in warnings
    assert "assembly_engineering_approval_not_evaluated" in warnings

