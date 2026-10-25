"""Public-safe known-answer tests for the first preset-driven wall producer."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import math

import pytest

from oabm.building_assemblies import (
    BuildingAssemblyPresetCatalog,
    DEFAULT_PRESET_CATALOG,
    ParameterOverride,
    WallAssemblyRequest,
    WallPresetError,
    build_wall_assembly,
    resolve_wall_preset,
)
from oabm.building_assemblies.fixtures import (
    SYNTHETIC_ASSEMBLY_ID,
    SYNTHETIC_DOOR_ID,
    SYNTHETIC_WALL_ID,
    SYNTHETIC_WINDOW_ID,
    synthetic_two_opening_wall_assembly,
    synthetic_two_opening_wall_source,
)
from oabm.model import (
    AssemblyComponent,
    Box3D,
    BuildingModel,
    ContractError,
    Opening,
    PanelGeometry,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Quaternion,
    Route,
    Size3,
    Vector3,
    Wall,
    compute_assembly_digests,
    component_world_points,
    stable_assembly_component_id,
)
from oabm.model.assembly_validation import _opening_clearance


def _request(*, assembly_id=SYNTHETIC_ASSEMBLY_ID, overrides=()):
    return WallAssemblyRequest(
        assembly_id=assembly_id,
        wall_id=SYNTHETIC_WALL_ID,
        selected_by="agent:test-selector",
        parameter_overrides=tuple(overrides),
    )


def _wall_frame_projection(wall: Wall, point: Point3):
    first, last = wall.centerline.points
    dx, dy = last.x - first.x, last.y - first.y
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    x, y, z = ((point.x, point.y, point.z) if isinstance(point, Point3) else point)
    return (
        (x - first.x) * ux + (y - first.y) * uy,
        (x - first.x) * nx + (y - first.y) * ny,
        z,
    )


def _local_component_bounds(wall: Wall, component: AssemblyComponent):
    points = tuple(_wall_frame_projection(wall, point)
                   for point in component_world_points(component))
    return (
        tuple(min(row[index] for row in points) for index in range(3)),
        tuple(max(row[index] for row in points) for index in range(3)),
    )


def test_versioned_catalog_is_finite_immutable_and_strictly_decoded():
    catalog = DEFAULT_PRESET_CATALOG
    assert catalog.catalog_version == "1"
    assert catalog.catalog_revision == "1.0.0"
    assert tuple(row.preset_id for row in catalog.wall_presets) == ("wall.wood_rect_stock",)
    assert BuildingAssemblyPresetCatalog.from_dict(catalog.to_dict()) == catalog
    assert BuildingAssemblyPresetCatalog.from_json(catalog.to_json(indent=None)) == catalog

    bad = catalog.to_dict()
    bad["not_a_catalog_field"] = "ignored fields are forbidden"
    with pytest.raises(ContractError, match="extra=.*not_a_catalog_field"):
        BuildingAssemblyPresetCatalog.from_dict(bad)
    with pytest.raises(ContractError, match="duplicate catalog JSON key"):
        BuildingAssemblyPresetCatalog.from_json('{"catalog_version":"1","catalog_version":"1"}')

    bad = catalog.to_dict()
    bad["wall_presets"][0]["parameters"][0]["unit"] = "mm"
    with pytest.raises(ContractError, match="unit must be 'm'"):
        BuildingAssemblyPresetCatalog.from_dict(bad)

    bad = catalog.to_dict()
    bad["wall_presets"].append(dict(bad["wall_presets"][0]))
    with pytest.raises(ContractError, match="preset IDs must be unique"):
        BuildingAssemblyPresetCatalog.from_dict(bad)


def test_resolver_snapshots_explicit_defaults_and_distinct_typed_overrides():
    default_request = _request()
    default = resolve_wall_preset(default_request)
    assert len(default.parameters) == 14
    assert {row.origin for row in default.parameters} == {"preset_default"}
    spacing = default.parameter_map()["wall.stud.spacing_m"]
    assert spacing.value == 0.4064
    assert spacing.value_kind == "length" and spacing.unit == "m"
    assert spacing.provenance[0].derivation == "inferred"
    assert spacing.provenance[0].source_kind == "preset_parameter"
    assert spacing.provenance[0].attributes["default_basis"] == "illustrative_modeling_assumption"

    override = ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.6096, unit="m",
    )
    resolved = resolve_wall_preset(_request(overrides=(override,)))
    selected = resolved.parameter_map()["wall.stud.spacing_m"]
    assert selected.value == 0.6096
    assert selected.origin == "user_override"
    assert selected.provenance[0].derivation == "user"
    assert selected.provenance[0].source_kind == "user_override"
    assert selected.provenance[0].source_element_id == selected.parameter_id
    assert all(row.origin == "preset_default" for row in resolved.parameters if row.parameter_id != selected.parameter_id)

    with pytest.raises(WallPresetError, match="must be length in m"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.stud.spacing_m", value_kind="length",
            value=16.0, unit="in",
        ),)))
    with pytest.raises(WallPresetError, match="invalid typed value"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.plate.top_count", value_kind="count",
            value=2.0, unit="each",
        ),)))
    with pytest.raises(WallPresetError, match="unsupported wall parameter overrides"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.structural_capacity", value_kind="token",
            value="unknown", unit="1",
        ),)))
    with pytest.raises(WallPresetError, match="unknown or unsupported wall preset"):
        resolve_wall_preset(replace(default_request, preset_id="wall.cold_formed_c_u"))


def test_canonical_validator_consumes_nonzero_producer_opening_clearance():
    clearance = 0.05
    model = build_wall_assembly(
        synthetic_two_opening_wall_source(),
        _request(overrides=(ParameterOverride(
            parameter_id="wall.opening_clearance_m", value_kind="length",
            value=clearance, unit="m",
        ),)),
    )
    assembly = model.building_assemblies[0]
    parameter = next(row for row in assembly.resolved_parameters
                     if row.parameter_id == "wall.opening_clearance_m")
    assert parameter.value == clearance
    assert parameter.origin == "user_override"
    # The returned BuildingModel was validated by this contract function. It
    # must see the producer's exact nonzero envelope rather than defaulting to 0.
    assert _opening_clearance(assembly) == clearance


def test_override_count_limit_rejects_before_scanning_override_rows():
    # Invalid rows would otherwise fail the per-item type check. The count
    # guard must fire first, before scanning or uniqueness tuple/set allocation.
    too_many_invalid_rows = (object(),) * 15
    with pytest.raises(WallPresetError, match="override count exceeds the finite v1 limit 14"):
        WallAssemblyRequest(
            assembly_id=SYNTHETIC_ASSEMBLY_ID,
            wall_id=SYNTHETIC_WALL_ID,
            selected_by="agent:test-selector",
            parameter_overrides=too_many_invalid_rows,
        )


def test_two_opening_wall_known_answer_fit_voids_ids_counts_materials_and_digests():
    source = synthetic_two_opening_wall_source()
    source_json = source.to_json(indent=None)
    model = build_wall_assembly(source, _request(assembly_id=SYNTHETIC_ASSEMBLY_ID))
    wall = model.walls[0]
    assembly = model.building_assemblies[0]
    parts = model.assembly_components

    assert source.to_json(indent=None) == source_json
    assert model.schema_version == "1.6.0"
    assert wall.thickness_m == 0.1143 and wall.height_m == 2.4384
    assert math.dist(
        (wall.centerline.points[0].x, wall.centerline.points[0].y),
        (wall.centerline.points[1].x, wall.centerline.points[1].y),
    ) == 4.8
    assert assembly.assembly_kind == "wall"
    assert assembly.host_id == SYNTHETIC_WALL_ID
    assert assembly.host_representation == "aggregate_host"
    assert assembly.preset_id == "wall.wood_rect_stock"
    assert assembly.catalog_revision == "1.0.0"
    assert assembly.generator_version == "oabm-wall-producer/1.0.0"
    assert set(assembly.component_ids) == {part.id for part in parts}
    assert assembly.component_ids == tuple(sorted(assembly.component_ids))
    assert all(part.id == stable_assembly_component_id(assembly.id, part.part_key) for part in parts)
    assert all(part.geometry_basis == "specified_parameterized_geometry" for part in parts)
    assert all(part.material_id is None and part.material_basis == "unknown" for part in parts)
    assert all(part.provenance[0].derivation == "inferred" for part in parts)
    assert all("geometry" in part.provenance[0].scope_paths for part in parts)
    assert any(isinstance(part.geometry, Box3D) for part in parts)
    assert any(isinstance(part.geometry, PanelGeometry) for part in parts)
    assert not any(part.component_kind in {"door_leaf", "door_frame", "window_frame", "window_glazing"} for part in parts)

    counts = Counter(part.component_kind for part in parts)
    assert counts == Counter({
        "bottom_plate": 2,
        "top_plate": 2,
        "stud": 12,
        "cripple": 8,
        "header": 2,
        "sill": 1,
        "drywall_panel": 24,
    })
    parameters = {row.parameter_id: row.value for row in assembly.resolved_parameters}
    assert abs(math.fsum((
        parameters["wall.stack.framing_depth_m"],
        parameters["wall.stack.face_left.layer_1_m"],
        parameters["wall.stack.face_right.layer_1_m"],
    )) - wall.thickness_m) <= 1e-9
    assert parameters["wall.opening_clearance_m"] == 0.0

    # Exact parameterized section values and the explicitly labeled illustrative
    # construction assumptions are visible in the resolved evidence snapshot.
    assert parameters["wall.stock.wall_axis_width_m"] == 0.0381
    assert parameters["wall.stack.framing_depth_m"] == 0.0889
    assert parameters["wall.drywall.thickness_m"] == 0.0127
    for parameter_id in (
        "wall.stud.spacing_m", "wall.stud.start_offset_m", "wall.stud.end_offset_m",
        "wall.plate.bottom_count", "wall.plate.top_count",
        "wall.opening.header_height_m", "wall.opening.sill_height_m",
    ):
        record = next(row for row in assembly.resolved_parameters if row.parameter_id == parameter_id)
        assert record.origin == "preset_default"
        assert record.provenance[0].attributes["default_basis"] == "illustrative_modeling_assumption"

    bottoms = [part for part in parts if part.component_kind == "bottom_plate"]
    tops = [part for part in parts if part.component_kind == "top_plate"]
    headers = [part for part in parts if part.component_kind == "header"]
    sills = [part for part in parts if part.component_kind == "sill"]
    drywall = [part for part in parts if part.component_kind == "drywall_panel"]
    assert len(bottoms) == 2  # the bottom plate is cut at the door opening
    assert len(tops) == 2
    assert all(isinstance(part.geometry, Box3D) for part in (*bottoms, *tops, *headers, *sills))
    assert all(abs(part.geometry.size.y - 0.0889) <= 1e-12 for part in (*bottoms, *tops, *headers, *sills))
    assert all(abs(part.geometry.size.z - 0.0381) <= 1e-12 for part in (*tops, *headers, *sills))
    assert all(abs(part.geometry.size.x - 4.8) <= 1e-12 for part in tops)
    assert {part.geometry.size.x for part in headers} == {0.9906, 1.0922}
    assert {part.geometry.size.x for part in sills} == {1.0922}
    full_station = next(part for part in parts if part.part_key == "stud:station:wall-start")
    assert isinstance(full_station.geometry, Box3D)
    assert full_station.geometry.size.x == 0.0381
    assert full_station.geometry.size.y == 0.0889
    assert abs(full_station.geometry.size.z - (2.4384 - 3 * 0.0381)) <= 1e-12
    assert all(isinstance(part.geometry, PanelGeometry) and part.geometry.thickness_m == 0.0127
               for part in drywall)

    assert compute_assembly_digests(model, assembly, parts) == (
        assembly.input_digest, assembly.output_digest,
    )
    openings = {opening.id: opening for opening in model.openings}
    projected_openings = {}
    for opening in openings.values():
        center = _wall_frame_projection(wall, opening.pose.position)[0]
        projected_openings[opening.id] = (
            center - opening.size.x / 2.0, center + opening.size.x / 2.0,
            opening.pose.position.z - opening.size.z / 2.0,
            opening.pose.position.z + opening.size.z / 2.0,
        )

    # Every physical extremum is inside the host envelope and no component
    # crosses the exact observed opening void. This includes panel thickness.
    bounds = []
    for part in parts:
        lower, upper = _local_component_bounds(wall, part)
        axis_min, axis_max = lower[0], upper[0]
        depth_min, depth_max = lower[1], upper[1]
        z_min, z_max = lower[2], upper[2]
        assert axis_min >= -1e-9 and axis_max <= 4.8 + 1e-9
        assert depth_min >= -wall.thickness_m / 2 - 1e-9
        assert depth_max <= wall.thickness_m / 2 + 1e-9
        assert z_min >= -1e-9 and z_max <= 2.4384 + 1e-9
        for left, right, bottom, top in projected_openings.values():
            assert not (
                min(axis_max, right) - max(axis_min, left) > 1e-9
                and min(z_max, top) - max(z_min, bottom) > 1e-9
                and depth_max - depth_min > 1e-9
            ), f"{part.id} bridges a source opening void"
        bounds.append((part, lower, upper))

    # This illustrative recipe uses touching interfaces, never positive-volume
    # component overlap: studs/cripples meet plates, headers/sills meet jambs,
    # and finish panels meet framing without creating duplicate solids.
    for index, (first, first_lo, first_hi) in enumerate(bounds):
        for second, second_lo, second_hi in bounds[index + 1:]:
            overlap = tuple(
                min(first_hi[axis], second_hi[axis]) - max(first_lo[axis], second_lo[axis])
                for axis in range(3)
            )
            assert not all(value > 1e-9 for value in overlap), (
                f"{first.part_key} and {second.part_key} overlap in positive volume"
            )

    again = build_wall_assembly(source, _request(assembly_id=SYNTHETIC_ASSEMBLY_ID))
    assert again.to_json(indent=None) == model.to_json(indent=None)
    restored = BuildingModel.from_json(model.to_json(indent=None))
    assert restored.to_json(indent=None) == model.to_json(indent=None)


def test_user_override_changes_layout_without_mutating_input_or_relabeling_default():
    source = synthetic_two_opening_wall_source()
    original = source.to_json(indent=None)
    request = _request(overrides=(ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.6096, unit="m",
    ),))
    model = build_wall_assembly(source, request)
    assembly = model.building_assemblies[0]
    values = {row.parameter_id: row for row in assembly.resolved_parameters}
    assert values["wall.stud.spacing_m"].origin == "user_override"
    assert values["wall.stud.spacing_m"].value == 0.6096
    assert values["wall.stack.framing_depth_m"].origin == "preset_default"
    assert source.to_json(indent=None) == original
    assert len(model.assembly_components) != len(synthetic_two_opening_wall_assembly().assembly_components)
    assert model.building_assemblies[0].input_digest != synthetic_two_opening_wall_assembly().building_assemblies[0].input_digest


def test_invalid_stack_orientation_overlap_and_dependency_fail_atomically():
    source = synthetic_two_opening_wall_source()
    before = source.to_json(indent=None)
    wrong_stack = replace(source.walls[0], thickness_m=0.1)
    with pytest.raises(WallPresetError, match="unresolved wall thickness stack"):
        build_wall_assembly(replace(source, walls=(wrong_stack,), openings=()), _request())
    assert source.to_json(indent=None) == before

    window = next(row for row in source.openings if row.id == SYNTHETIC_WINDOW_ID)
    bad_window = replace(window, pose=replace(window.pose, rotation=Quaternion(z=0.1, w=math.sqrt(0.99))))
    with pytest.raises(WallPresetError, match="orientation is not aligned"):
        build_wall_assembly(replace(source, openings=tuple(
            bad_window if row.id == window.id else row for row in source.openings
        )), _request())
    assert source.to_json(indent=None) == before

    door = next(row for row in source.openings if row.id == SYNTHETIC_DOOR_ID)
    overlap = replace(window, pose=replace(window.pose, position=Point3(x=1.6, y=0, z=1.524)))
    with pytest.raises(WallPresetError, match="opening/frame envelopes overlap"):
        build_wall_assembly(replace(source, openings=(door, overlap)), _request())
    assert source.to_json(indent=None) == before

    already_built = synthetic_two_opening_wall_assembly()
    with pytest.raises(WallPresetError, match="requires_derived_rebuild"):
        build_wall_assembly(already_built, _request(assembly_id="assembly:replacement-attempt"))

    wall = source.walls[0]
    port_a = Port(
        id="port:existing-route-a", owner_id=wall.id, domain="electrical", role="source",
        pose=Pose(position=Point3(x=0.1, y=0, z=1)), direction=Vector3(x=1, y=0, z=0),
    )
    port_b = Port(
        id="port:existing-route-b", owner_id=wall.id, domain="electrical", role="load",
        pose=Pose(position=Point3(x=4.7, y=0, z=1)), direction=Vector3(x=-1, y=0, z=0),
    )
    route = Route(
        id="route:existing", route_type="conduit", start_port_id=port_a.id,
        end_port_id=port_b.id,
        centerline=Polyline3D(points=(port_a.pose.position, port_b.pose.position)),
    )
    routed_source = replace(source, ports=(port_a, port_b), routes=(route,))
    routed_json = routed_source.to_json(indent=None)
    with pytest.raises(WallPresetError, match="refuses models with existing routes"):
        build_wall_assembly(routed_source, _request())
    assert routed_source.to_json(indent=None) == routed_json


def test_curved_or_sloped_wall_and_resource_overrun_are_refused_before_generation():
    source = synthetic_two_opening_wall_source()
    wall = source.walls[0]
    curved = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=2, y=0.1, z=0), Point3(x=4.8, y=0, z=0),
    )))
    with pytest.raises(WallPresetError, match="two-point straight centerline"):
        build_wall_assembly(replace(source, walls=(curved,)), _request())
    sloped = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=4.8, y=0, z=0.01),
    )))
    with pytest.raises(WallPresetError, match="share one horizontal base elevation"):
        build_wall_assembly(replace(source, walls=(sloped,)), _request())

    long_wall = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=1000, y=0, z=0),
    )))
    too_dense = ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.0508, unit="m",
    )
    with pytest.raises(WallPresetError, match="component_cap_exceeded"):
        build_wall_assembly(replace(source, walls=(long_wall,), openings=()),
                            _request(overrides=(too_dense,)))


def test_ordered_rotated_horizontal_wall_preserves_stable_semantic_identity():
    source = synthetic_two_opening_wall_source()
    original = build_wall_assembly(source, _request())
    wall = replace(source.walls[0], centerline=Polyline3D(points=(
        Point3(x=10.0, y=20.0, z=0.0), Point3(x=10.0, y=24.8, z=0.0),
    )))
    rotation = Quaternion(z=math.sqrt(0.5), w=math.sqrt(0.5))
    rotated_openings = []
    for opening in source.openings:
        station = opening.pose.position.x
        rotated_openings.append(replace(
            opening,
            pose=replace(
                opening.pose,
                position=Point3(x=10.0, y=20.0 + station, z=opening.pose.position.z),
                rotation=rotation,
            ),
        ))
    rotated_source = replace(source, walls=(wall,), openings=tuple(rotated_openings))
    rotated = build_wall_assembly(rotated_source, _request())
    assert tuple(row.id for row in original.assembly_components) == tuple(
        row.id for row in rotated.assembly_components
    )
    assert original.building_assemblies[0].input_digest != rotated.building_assemblies[0].input_digest
    assert original.building_assemblies[0].output_digest != rotated.building_assemblies[0].output_digest
    assert rotated.building_assemblies[0].wall_frame.axis == Vector3(x=0.0, y=1.0, z=0.0)
    assert rotated.building_assemblies[0].wall_frame.left_normal == Vector3(x=-1.0, y=0.0, z=0.0)


def test_drywall_stack_mismatch_and_missing_source_evidence_fail_closed():
    source = synthetic_two_opening_wall_source()
    mismatched = (
        ParameterOverride(parameter_id="wall.drywall.thickness_m", value_kind="length", value=0.0159, unit="m"),
        ParameterOverride(parameter_id="wall.stack.face_left.layer_1_m", value_kind="length", value=0.0159, unit="m"),
        ParameterOverride(parameter_id="wall.stack.face_right.layer_1_m", value_kind="length", value=0.0159, unit="m"),
    )
    with pytest.raises(WallPresetError, match="unresolved wall thickness stack"):
        build_wall_assembly(source, _request(overrides=mismatched))

    missing_evidence = replace(source.walls[0], provenance=())
    with pytest.raises(WallPresetError, match="no observed source provenance"):
        build_wall_assembly(replace(source, walls=(missing_evidence,)), _request())
