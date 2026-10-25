"""Public-safe proof for finite, explicit door/window opening inserts."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import replace

import pytest

from oabm.building_assemblies.fixtures import (
    SYNTHETIC_DOOR_ID,
    SYNTHETIC_WALL_ID,
    SYNTHETIC_WINDOW_ID,
    SYNTHETIC_OPENING_FRAMING_SPECS,
    synthetic_two_opening_wall_assembly,
    synthetic_two_opening_wall_source,
)
from oabm.building_assemblies.opening_insert_presets import (
    DEFAULT_OPENING_INSERT_CATALOG,
    DOOR_SINGLE_LEAF_PRESET_ID,
    OpeningInsertPresetCatalog,
    WINDOW_FIXED_GLAZED_PRESET_ID,
)
from oabm.building_assemblies.opening_inserts import (
    OpeningInsertAssemblyRequest,
    OpeningInsertError,
    OpeningInsertParameterOverride,
    build_opening_insert_assembly,
    resolve_opening_insert_preset,
)
from oabm.exports.gltf import build_glb_bytes
from oabm.model import (
    Box3D,
    ElectricalDevice,
    Opening,
    PanelGeometry,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Quaternion,
    Route,
    Vector3,
    compute_assembly_digests,
    component_world_points,
)
from oabm.quantities import extract_quantities


def _add_insert(model, *, assembly_id: str, opening_id: str, preset_id: str,
                overrides: tuple[OpeningInsertParameterOverride, ...] = ()):
    return build_opening_insert_assembly(model, OpeningInsertAssemblyRequest(
        assembly_id=assembly_id,
        opening_id=opening_id,
        selected_by="agent:synthetic-recipe-selector",
        preset_id=preset_id,
        parameter_overrides=overrides,
    ))


def _proof_model():
    source = synthetic_two_opening_wall_assembly()
    wall_before = source.walls[0]
    openings_before = source.openings
    with_door = _add_insert(
        source, assembly_id="assembly:proof-door-insert", opening_id=SYNTHETIC_DOOR_ID,
        preset_id=DOOR_SINGLE_LEAF_PRESET_ID,
    )
    complete = _add_insert(
        with_door, assembly_id="assembly:proof-window-insert", opening_id=SYNTHETIC_WINDOW_ID,
        preset_id=WINDOW_FIXED_GLAZED_PRESET_ID,
    )
    # Both upstream source fact collections remain byte-for-byte canonical.
    assert source.walls[0] == wall_before
    assert source.openings == openings_before
    assert complete.walls[0] == wall_before
    assert complete.openings == openings_before
    return complete


def _read_glb(payload: bytes):
    magic, version, length = struct.unpack_from("<III", payload, 0)
    assert (magic, version, length) == (0x46546C67, 2, len(payload))
    json_length, json_type = struct.unpack_from("<II", payload, 12)
    assert json_type == 0x4E4F534A
    gltf = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip(" "))
    return gltf


def _local_aabb(component, wall):
    start, end = wall.centerline.points
    axis = (end.x - start.x, end.y - start.y)
    length = math.hypot(*axis)
    ux, uy = axis[0] / length, axis[1] / length
    nx, ny = -uy, ux
    local = []
    for x, y, z in component_world_points(component):
        dx, dy = x - start.x, y - start.y
        local.append((dx * ux + dy * uy, dx * nx + dy * ny, z))
    return tuple(min(row[i] for row in local) for i in range(3)) + tuple(
        max(row[i] for row in local) for i in range(3)
    )


def _positive_intersection(first, second):
    return all(min(first[i + 3], second[i + 3]) - max(first[i], second[i]) > 1e-9
               for i in range(3))


def _assert_butt_join_frame_coverage(frame_parts, opening, wall, *, door: bool):
    """Prove perimeter cells are covered exactly once through the full aperture depth."""
    face = 0.0508
    start, end = wall.centerline.points
    axis = (end.x - start.x, end.y - start.y)
    length = math.hypot(*axis)
    ux, uy = axis[0] / length, axis[1] / length
    station = ((opening.pose.position.x - start.x) * ux +
               (opening.pose.position.y - start.y) * uy)
    left, right = station - opening.size.x / 2, station + opening.size.x / 2
    sill, head = opening.pose.position.z - opening.size.z / 2, opening.pose.position.z + opening.size.z / 2
    x_edges = (left, left + face, right - face, right)
    z_edges = (sill, sill + face, head - face, head)
    bounds = tuple(_local_aabb(row, wall) for row in frame_parts)
    depth_samples = (-opening.size.y / 2 + 1e-6, 0.0, opening.size.y / 2 - 1e-6)

    for box in bounds:
        assert box[1] == pytest.approx(-opening.size.y / 2)
        assert box[4] == pytest.approx(opening.size.y / 2)
    for x_index, (x0, x1) in enumerate(zip(x_edges, x_edges[1:])):
        for z_index, (z0, z1) in enumerate(zip(z_edges, z_edges[1:])):
            perimeter = (x_index in (0, 2) or
                         (z_index == 2 if door else z_index in (0, 2)))
            expected_coverage = 1 if perimeter else 0
            x, z = (x0 + x1) / 2, (z0 + z1) / 2
            for through in depth_samples:
                covering = [box for box in bounds
                            if box[0] < x < box[3] and box[1] < through < box[4]
                            and box[2] < z < box[5]]
                assert len(covering) == expected_coverage

    actual_volume = math.fsum(
        row.geometry.size.x * row.geometry.size.y * row.geometry.size.z
        for row in frame_parts
    )
    horizontal_rails = 1 if door else 2
    expected_net_volume = opening.size.y * face * (
        2 * opening.size.z + horizontal_rails * (opening.size.x - 2 * face)
    )
    assert actual_volume == pytest.approx(expected_net_volume, rel=1e-12, abs=1e-12)


def test_explicit_door_and_fixed_window_inserts_render_and_measure_by_exact_identity():
    model = _proof_model()
    wall_assignment = next(row for row in model.building_assemblies if row.assembly_kind == "wall")
    insert_assignments = tuple(row for row in model.building_assemblies
                               if row.assembly_kind == "opening_insert")
    assert len(insert_assignments) == 2
    door = next(row for row in model.openings if row.id == SYNTHETIC_DOOR_ID)
    window = next(row for row in model.openings if row.id == SYNTHETIC_WINDOW_ID)
    door_parts = tuple(row for row in model.assembly_components
                       if row.assembly_id == next(item.id for item in insert_assignments
                           if item.host_id == door.id))
    window_parts = tuple(row for row in model.assembly_components
                         if row.assembly_id == next(item.id for item in insert_assignments
                             if item.host_id == window.id))

    assert len(door_parts) == 4
    assert {row.component_kind for row in door_parts} == {"door_leaf", "door_frame"}
    assert len([row for row in door_parts if row.component_kind == "door_leaf"]) == 1
    assert len([row for row in door_parts if row.component_kind == "door_frame"]) == 3
    assert len(window_parts) == 5
    assert {row.component_kind for row in window_parts} == {"window_frame", "window_glazing"}
    assert len([row for row in window_parts if row.component_kind == "window_frame"]) == 4
    assert len([row for row in window_parts if row.component_kind == "window_glazing"]) == 1
    assert all(row.opening_id == door.id for row in door_parts)
    assert all(row.opening_id == window.id for row in window_parts)
    assert all(row.material_id is None and row.material_basis == "unknown"
               and row.geometry_basis == "specified_parameterized_geometry"
               for row in (*door_parts, *window_parts))

    leaf = next(row for row in door_parts if row.component_kind == "door_leaf")
    assert isinstance(leaf.geometry, Box3D)
    assert leaf.geometry.size.x == pytest.approx(door.size.x - 2 * (0.0508 + 0.003))
    assert leaf.geometry.size.y == pytest.approx(0.035)
    assert leaf.geometry.size.z == pytest.approx(door.size.z - 0.0508 - 0.003 - 0.006)
    glass = next(row for row in window_parts if row.component_kind == "window_glazing")
    assert isinstance(glass.geometry, PanelGeometry)
    assert glass.geometry.thickness_m == pytest.approx(0.006)
    window_bounds = _local_aabb(glass, model.walls[0])
    assert window_bounds[3] - window_bounds[0] == pytest.approx(
        window.size.x - 2 * (0.0508 + 0.003)
    )
    assert window_bounds[5] - window_bounds[2] == pytest.approx(
        window.size.z - 2 * (0.0508 + 0.003)
    )

    # Independent perimeter members meet only at faces/edges; no two
    # rectangular frame bars claim the same positive-volume corner.
    frame_parts = [row for row in (*door_parts, *window_parts)
                   if row.component_kind in {"door_frame", "window_frame"}]
    frame_bounds = {row.id: _local_aabb(row, model.walls[0]) for row in frame_parts}
    for index, first in enumerate(frame_parts):
        for second in frame_parts[index + 1:]:
            assert not _positive_intersection(frame_bounds[first.id], frame_bounds[second.id])
    _assert_butt_join_frame_coverage(
        [row for row in door_parts if row.component_kind == "door_frame"], door,
        model.walls[0], door=True,
    )
    _assert_butt_join_frame_coverage(
        [row for row in window_parts if row.component_kind == "window_frame"], window,
        model.walls[0], door=False,
    )

    # Exact void semantics: wall-owned children stop at the openings; insert
    # children fit inside the unchanged apertures. The aggregate wall shell is
    # never a second billable/rendered physical body.
    for component in (row for row in model.assembly_components
                     if row.assembly_id == wall_assignment.id):
        bounds = _local_aabb(component, model.walls[0])
        for opening in (door, window):
            center_station = (opening.pose.position.x - model.walls[0].centerline.points[0].x)
            sill, head = opening.pose.position.z - opening.size.z / 2, opening.pose.position.z + opening.size.z / 2
            if min(bounds[3], center_station + opening.size.x / 2) - max(
                    bounds[0], center_station - opening.size.x / 2) > 1e-9:
                assert not (min(bounds[5], head) - max(bounds[2], sill) > 1e-9 and
                            min(bounds[4], opening.size.y / 2) - max(bounds[1], -opening.size.y / 2) > 1e-9)

    first = build_glb_bytes(model)
    assert first == build_glb_bytes(_proof_model())
    gltf = _read_glb(first)
    by_name = {node["name"]: node for node in gltf["nodes"]}
    exact_component_ids = {row.id for row in model.assembly_components}
    assert exact_component_ids <= set(by_name)
    assert SYNTHETIC_WALL_ID not in by_name
    assert len([node for node in gltf["nodes"] if node["name"] in exact_component_ids]) == len(exact_component_ids)
    for row in (*door_parts, *window_parts):
        extras = by_name[row.id]["extras"]
        assert extras["role"] == row.component_kind
        assert extras["assembly_kind"] == "opening_insert"
        assert extras["opening_id"] == row.opening_id
        assert extras["geometry_disclosure"].startswith("specified parameterized geometry")

    report = extract_quantities(model)
    count_ids = {identifier for item in report.items if item.category == "assembly_component_count"
                 for identifier in item.source_entity_ids}
    volume_ids = {identifier for item in report.items
                  if item.category == "assembly_component_geometry_volume"
                  for identifier in item.source_entity_ids}
    assert count_ids == exact_component_ids
    assert set(row.id for row in (*door_parts, *window_parts)) <= volume_ids
    assert {"wall_length", "assembly_component_count", "assembly_component_geometry_volume"} <= {
        item.category for item in report.items
    }
    assert not {"wall_face_area", "wall_volume"} & {item.category for item in report.items}
    assert sum(item.quantity for item in report.items
               if item.category == "assembly_component_count") == len(exact_component_ids)


def test_insert_presets_are_explicit_versioned_and_overrides_keep_user_provenance():
    source = synthetic_two_opening_wall_assembly()
    override = OpeningInsertParameterOverride(
        parameter_id="door.leaf.thickness_m", value_kind="length", value=0.04, unit="m",
    )
    result = _add_insert(source, assembly_id="assembly:door-user-override",
                         opening_id=SYNTHETIC_DOOR_ID,
                         preset_id=DOOR_SINGLE_LEAF_PRESET_ID, overrides=(override,))
    assignment = result.building_assemblies[-1]
    assert assignment.catalog_id == "catalog:oabm-opening-inserts"
    assert assignment.catalog_revision == "1.0.0"
    assert assignment.preset_id == DOOR_SINGLE_LEAF_PRESET_ID
    assert assignment.preset_revision == "1.0.0"
    assert assignment.generator_version == "oabm-opening-insert-producer/1.0.0"
    selection_ref = json.dumps(
        (assignment.catalog_id, assignment.catalog_revision,
         assignment.preset_id, assignment.preset_revision),
        separators=(",", ":"), ensure_ascii=False,
    )
    assert any(row.derivation == "user" and row.source_id == assignment.selected_by and
               row.source_element_id == selection_ref and
               {"catalog_id", "catalog_revision", "preset_id", "preset_revision",
                "selected_by", "host_id", "input_digest"} <= set(row.scope_paths or ())
               for row in assignment.provenance)
    observed_source_ids = {row.source_element_id for row in assignment.provenance
                           if row.derivation == "observed"}
    assert {SYNTHETIC_DOOR_ID, SYNTHETIC_WALL_ID} <= observed_source_ids
    resolved = {row.parameter_id: row for row in assignment.resolved_parameters}
    assert resolved["door.leaf.thickness_m"].value == pytest.approx(0.04)
    assert resolved["door.leaf.thickness_m"].origin == "user_override"
    assert any(row.derivation == "user" and row.source_kind == "user_override" and
               row.source_id == assignment.selected_by and
               row.source_element_id == "door.leaf.thickness_m"
               for row in resolved["door.leaf.thickness_m"].provenance)
    assert resolved["door.frame.face_width_m"].origin == "preset_default"
    assert resolved["door.frame.face_width_m"].provenance[0].attributes["default_basis"] == \
        "illustrative_modeling_assumption"
    leaf = next(row for row in result.assembly_components if row.component_kind == "door_leaf")
    assert leaf.geometry.size.y == pytest.approx(0.04)
    assert leaf.material_id is None and leaf.material_basis == "unknown"
    assert assignment.input_digest == compute_assembly_digests(
        result, assignment, tuple(row for row in result.assembly_components if row.assembly_id == assignment.id),
    )[0]
    assert assignment.output_digest == compute_assembly_digests(
        result, assignment, tuple(row for row in result.assembly_components if row.assembly_id == assignment.id),
    )[1]


def test_insert_catalog_round_trip_is_strict_and_immutable():
    from copy import deepcopy
    from dataclasses import FrozenInstanceError
    from oabm.building_assemblies.opening_insert_presets import (
        DEFAULT_OPENING_INSERT_CATALOG,
        OpeningInsertPresetCatalog,
        OpeningInsertPresetError,
    )

    encoded = DEFAULT_OPENING_INSERT_CATALOG.to_dict()
    decoded = OpeningInsertPresetCatalog.from_dict(encoded)
    assert decoded == DEFAULT_OPENING_INSERT_CATALOG
    assert decoded.to_dict() == encoded
    with pytest.raises(FrozenInstanceError):
        decoded.catalog_revision = "mutable"

    extra_catalog_key = deepcopy(encoded)
    extra_catalog_key["fallback_recipe"] = "automatic"
    with pytest.raises(OpeningInsertPresetError, match="keys are invalid"):
        OpeningInsertPresetCatalog.from_dict(extra_catalog_key)
    extra_parameter_key = deepcopy(encoded)
    extra_parameter_key["presets"][0]["parameters"][0]["unreviewed_default"] = 1
    with pytest.raises(OpeningInsertPresetError, match="keys are invalid"):
        OpeningInsertPresetCatalog.from_dict(extra_parameter_key)
    too_many_recipes = deepcopy(encoded)
    too_many_recipes["presets"] *= 2
    with pytest.raises(OpeningInsertPresetError, match="at most 2 recipes"):
        OpeningInsertPresetCatalog.from_dict(too_many_recipes)


def test_builtin_insert_preset_identity_is_reserved_but_new_revision_is_caller_owned():
    published = DEFAULT_OPENING_INSERT_CATALOG.opening_insert_preset(DOOR_SINGLE_LEAF_PRESET_ID)
    changed_parameters = tuple(
        replace(row, default_value=0.06) if row.parameter_id == "door.frame.face_width_m" else row
        for row in published.parameters
    )
    changed_builtin = replace(published, parameters=changed_parameters)
    builtin_catalog_with_tampered_door = replace(
        DEFAULT_OPENING_INSERT_CATALOG,
        presets=(changed_builtin, DEFAULT_OPENING_INSERT_CATALOG.presets[1]),
    )
    request = OpeningInsertAssemblyRequest(
        assembly_id="assembly:reserved-preset-check",
        opening_id=SYNTHETIC_DOOR_ID,
        selected_by="agent:synthetic-recipe-selector",
        preset_id=DOOR_SINGLE_LEAF_PRESET_ID,
    )
    with pytest.raises(OpeningInsertError, match="published opening-insert identity .* is reserved"):
        resolve_opening_insert_preset(request, catalog=builtin_catalog_with_tampered_door)
    explicit = replace(request, parameter_overrides=(OpeningInsertParameterOverride(
        parameter_id="door.frame.face_width_m", value_kind="length", value=0.04, unit="m",
    ),))
    with pytest.raises(OpeningInsertError, match="published opening-insert identity .* is reserved"):
        resolve_opening_insert_preset(explicit, catalog=builtin_catalog_with_tampered_door)

    # Same finite recipe with a caller-owned catalog/preset revision is
    # allowed; its altered default remains explicitly snapshotted as such.
    custom_preset = replace(changed_builtin, preset_revision="2.0.0")
    custom_catalog = OpeningInsertPresetCatalog(
        catalog_id="catalog:custom-opening-inserts",
        catalog_revision="2.0.0",
        presets=(custom_preset, DEFAULT_OPENING_INSERT_CATALOG.presets[1]),
    )
    model = synthetic_two_opening_wall_assembly()
    custom_model = build_opening_insert_assembly(model, request, catalog=custom_catalog)
    assignment = custom_model.building_assemblies[-1]
    assert assignment.catalog_id == "catalog:custom-opening-inserts"
    assert assignment.catalog_revision == "2.0.0"
    assert assignment.preset_revision == "2.0.0"
    frame_width = next(row for row in assignment.resolved_parameters
                       if row.parameter_id == "door.frame.face_width_m")
    assert frame_width.value == pytest.approx(0.06)
    assert frame_width.origin == "preset_default"
    left_jamb = next(row for row in custom_model.assembly_components
                     if row.assembly_id == assignment.id and row.part_key == "door:frame:jamb:left")
    assert left_jamb.geometry.size.x == pytest.approx(0.06)


def test_field_qualified_source_evidence_is_matched_without_leaking_foreign_scope_paths():
    from oabm.building_assemblies.wall import WallAssemblyRequest, build_wall_assembly
    from oabm.model import Provenance

    source = synthetic_two_opening_wall_source()
    wall = replace(source.walls[0], provenance=(Provenance(
        source_kind="synthetic_wall_fixture", source_id="fixture:wall-assembly-v1",
        source_element_id=SYNTHETIC_WALL_ID, page=1, method="synthetic_fixture_authoring",
        derivation="observed", scope_paths=("level_id", "centerline", "thickness_m", "height_m"),
    ),))
    openings = tuple(replace(row, provenance=(Provenance(
        source_kind="synthetic_opening_fixture", source_id="fixture:wall-assembly-v1",
        source_element_id=row.id, page=1, method="synthetic_fixture_authoring",
        derivation="observed", scope_paths=("host_id", "opening_type", "pose", "size"),
    ),)) for row in source.openings)
    source = replace(source, walls=(wall,), openings=openings)
    built = build_wall_assembly(source, WallAssemblyRequest(
        assembly_id="assembly:scoped-evidence-wall", wall_id=wall.id,
        selected_by="agent:synthetic-recipe-selector",
        opening_framing_specs=SYNTHETIC_OPENING_FRAMING_SPECS,
    ))
    inserted = _add_insert(built, assembly_id="assembly:scoped-evidence-door",
                            opening_id=SYNTHETIC_DOOR_ID,
                            preset_id=DOOR_SINGLE_LEAF_PRESET_ID)
    assignment = inserted.building_assemblies[-1]
    # Assembly evidence only names fields on BuildingAssembly while canonical
    # validation still matches the field scopes on the original source rows.
    assert all(set(row.scope_paths or ()) <= {"host_id", "input_digest", "catalog_id",
               "catalog_revision", "preset_id", "preset_revision", "selected_by"}
               for row in assignment.provenance)
    assert assignment.host_id == SYNTHETIC_DOOR_ID


@pytest.mark.parametrize("reverse", [False, True])
def test_insert_geometry_supports_yaw_and_reversed_ordered_wall_axis(reverse):
    source = synthetic_two_opening_wall_source()
    original_wall = source.walls[0]
    angle = 0.61
    base_axis = (math.cos(angle), math.sin(angle))
    axis = (-base_axis[0], -base_axis[1]) if reverse else base_axis
    start = Point3(x=7.2 + (4.8 * base_axis[0] if reverse else 0.0),
                   y=-3.4 + (4.8 * base_axis[1] if reverse else 0.0), z=0.0)
    end = Point3(x=start.x + 4.8 * axis[0], y=start.y + 4.8 * axis[1], z=0.0)
    wall = replace(original_wall, centerline=Polyline3D(points=(start, end)))
    heading = math.atan2(axis[1], axis[0])
    opening_rows = []
    for original in source.openings:
        station = original.pose.position.x
        if reverse:
            station = 4.8 - station
        opening_rows.append(replace(
            original,
            pose=Pose(
                position=Point3(x=start.x + axis[0] * station,
                                y=start.y + axis[1] * station,
                                z=original.pose.position.z),
                rotation=Quaternion(z=math.sin(heading / 2), w=math.cos(heading / 2)),
            ),
        ))
    transformed = replace(source, walls=(wall,), openings=tuple(opening_rows))
    from oabm.building_assemblies.wall import WallAssemblyRequest, build_wall_assembly
    built_wall = build_wall_assembly(transformed, WallAssemblyRequest(
        assembly_id="assembly:yawed-reversed-wall", wall_id=wall.id,
        selected_by="agent:synthetic-recipe-selector",
        opening_framing_specs=SYNTHETIC_OPENING_FRAMING_SPECS,
    ))
    wall_frame_parts = [part for part in built_wall.assembly_components
                        if part.assembly_id == "assembly:yawed-reversed-wall"]
    frame_specs = {row.opening_id: row for row in SYNTHETIC_OPENING_FRAMING_SPECS}
    for opening in transformed.openings:
        spec = frame_specs[opening.id]
        opening_parts = [part for part in wall_frame_parts
                         if part.provenance[0].attributes.get("opening_id") == opening.id]
        kings = [part for part in opening_parts
                 if part.provenance[0].attributes.get("framing_role") == "king"]
        jacks = [part for part in opening_parts
                 if part.provenance[0].attributes.get("framing_role") == "jack"]
        headers = [part for part in opening_parts
                   if part.provenance[0].attributes.get("framing_role") == "header_ply"]
        assert len(kings) == 2 * spec.king_count_per_side
        assert len(jacks) == 2 * spec.jack_count_per_side
        assert len(headers) == spec.header_ply_count
        start = wall.centerline.points[0]
        ux, uy = axis
        station = ((opening.pose.position.x - start.x) * ux
                   + (opening.pose.position.y - start.y) * uy)
        left, right = station - opening.size.x / 2, station + opening.size.x / 2
        left_king = next(part for part in kings if part.provenance[0].attributes["side"] == "left")
        right_king = next(part for part in kings if part.provenance[0].attributes["side"] == "right")
        left_king_box, right_king_box = _local_aabb(left_king, wall), _local_aabb(right_king, wall)
        assert left_king_box[2] == pytest.approx(0.0381)
        assert left_king_box[5] == pytest.approx(2.3622)
        assert right_king_box[2] == pytest.approx(0.0381)
        assert right_king_box[5] == pytest.approx(2.3622)
        for header in headers:
            header_box = _local_aabb(header, wall)
            assert header_box[0] == pytest.approx(left - spec.bearing_m)
            assert header_box[3] == pytest.approx(right + spec.bearing_m)
            assert header_box[0] == pytest.approx(left_king_box[3])
            assert header_box[3] == pytest.approx(right_king_box[0])
            assert header_box[2] == pytest.approx(
                opening.pose.position.z + opening.size.z / 2,
            )
            for side in ("left", "right"):
                jack = next(part for part in jacks if part.provenance[0].attributes["side"] == side)
                jack_box = _local_aabb(jack, wall)
                assert jack_box[5] == pytest.approx(header_box[2])
    model = _add_insert(built_wall, assembly_id="assembly:yawed-reversed-door",
                        opening_id=SYNTHETIC_DOOR_ID, preset_id=DOOR_SINGLE_LEAF_PRESET_ID)
    model = _add_insert(model, assembly_id="assembly:yawed-reversed-window",
                        opening_id=SYNTHETIC_WINDOW_ID, preset_id=WINDOW_FIXED_GLAZED_PRESET_ID)
    assert len(build_glb_bytes(model)) > 1000
    for component in model.assembly_components:
        if component.assembly_id not in {"assembly:yawed-reversed-door", "assembly:yawed-reversed-window"}:
            continue
        bounds = _local_aabb(component, wall)
        assert bounds[0] >= -1e-9 and bounds[3] <= 4.8 + 1e-9
        assert abs(bounds[1]) <= wall.thickness_m / 2 + 1e-9
        assert abs(bounds[4]) <= wall.thickness_m / 2 + 1e-9


def test_invalid_nonfitting_mismatched_and_ambiguous_selections_refuse_atomically():
    source = synthetic_two_opening_wall_assembly()
    original = source
    too_deep = OpeningInsertParameterOverride(
        parameter_id="door.leaf.thickness_m", value_kind="length", value=0.12, unit="m",
    )
    with pytest.raises(OpeningInsertError, match="leaf thickness must be positive and fit the aperture depth"):
        _add_insert(source, assembly_id="assembly:invalid-door", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id=DOOR_SINGLE_LEAF_PRESET_ID, overrides=(too_deep,))
    with pytest.raises(OpeningInsertError, match="does not match explicit opening type"):
        _add_insert(source, assembly_id="assembly:mismatched-door", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id=WINDOW_FIXED_GLAZED_PRESET_ID)
    with pytest.raises(OpeningInsertError, match="unsupported opening-insert preset"):
        _add_insert(source, assembly_id="assembly:unknown-recipe", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id="opening_insert.door.unlisted_manufacturer_model")
    with pytest.raises(OpeningInsertError, match="already has an active insert assignment"):
        duplicate = _add_insert(source, assembly_id="assembly:first-door", opening_id=SYNTHETIC_DOOR_ID,
                                preset_id=DOOR_SINGLE_LEAF_PRESET_ID)
        _add_insert(duplicate, assembly_id="assembly:second-door", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id=DOOR_SINGLE_LEAF_PRESET_ID)
    assert source == original


def test_request_override_and_component_caps_are_checked_before_component_allocation(monkeypatch):
    import oabm.building_assemblies.opening_inserts as inserts

    typed = OpeningInsertParameterOverride(
        parameter_id="door.leaf.thickness_m", value_kind="length", value=0.035, unit="m",
    )
    too_many = (typed,) * 6
    with pytest.raises(OpeningInsertError, match="parameter_override_cap_exceeded"):
        OpeningInsertAssemblyRequest(
            assembly_id="assembly:overrides-too-many", opening_id=SYNTHETIC_DOOR_ID,
            selected_by="agent:test", preset_id=DOOR_SINGLE_LEAF_PRESET_ID,
            parameter_overrides=too_many,
        )

    def component_factory_must_not_run(**kwargs):
        raise AssertionError("component allocation occurred before recipe cap preflight")

    monkeypatch.setattr(inserts, "ASSEMBLY_MAX_COMPONENTS", 3)
    monkeypatch.setattr(inserts, "_door_components", component_factory_must_not_run)
    with pytest.raises(OpeningInsertError, match="component_cap_exceeded"):
        _add_insert(synthetic_two_opening_wall_assembly(),
                    assembly_id="assembly:component-cap", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id=DOOR_SINGLE_LEAF_PRESET_ID)


def test_existing_routes_refuse_before_any_insert_is_materialized():
    from oabm.model import Polyline3D

    model = synthetic_two_opening_wall_assembly()
    start = Point3(x=0.0, y=1.0, z=1.0)
    end = Point3(x=1.0, y=1.0, z=1.0)
    device1 = ElectricalDevice(id="device:insert-route-a", device_type="synthetic",
        pose=Pose(position=start), level_id="level:proof")
    device2 = ElectricalDevice(id="device:insert-route-b", device_type="synthetic",
        pose=Pose(position=end), level_id="level:proof")
    port1 = Port(id="port:insert-route-a", owner_id=device1.id, domain="electrical",
        role="source", pose=Pose(position=start), direction=Vector3(x=1.0, y=0.0, z=0.0))
    port2 = Port(id="port:insert-route-b", owner_id=device2.id, domain="electrical",
        role="load", pose=Pose(position=end), direction=Vector3(x=-1.0, y=0.0, z=0.0))
    route = Route(id="route:insert-blocker", route_type="conduit",
        start_port_id=port1.id, end_port_id=port2.id,
        centerline=Polyline3D(points=(start, end)))
    routed = replace(model, electrical_devices=(device1, device2), ports=(port1, port2), routes=(route,))
    with pytest.raises(OpeningInsertError, match="refuses models with routes"):
        _add_insert(routed, assembly_id="assembly:routed-door", opening_id=SYNTHETIC_DOOR_ID,
                    preset_id=DOOR_SINGLE_LEAF_PRESET_ID)
