"""Known-answer tests for the bounded issue #356 suspended-ceiling producer."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import json
import math
import struct

import pytest

from oabm.building_assemblies.ceiling_fixtures import (
    SYNTHETIC_CEILING_ASSEMBLY_ID,
    SYNTHETIC_CEILING_EDGE_POLICY,
    SYNTHETIC_CEILING_ID,
    SYNTHETIC_DECK_ID,
    synthetic_rectangular_ceiling_source,
    synthetic_rectangular_suspended_ceiling_assembly,
)
from oabm.building_assemblies.suspended_ceiling import (
    CeilingParameterOverride,
    SuspendedCeilingAssemblyRequest,
    SuspendedCeilingPresetError,
    build_suspended_ceiling_assembly,
    resolve_suspended_ceiling_preset,
)
import oabm.building_assemblies.suspended_ceiling as ceiling_producer
from oabm.building_assemblies.suspended_ceiling_catalog import (
    DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
    SUSPENDED_CEILING_PRESET_ID,
    CEILING_DATUM_OFFSET_KEY,
)
from oabm.building_assemblies.catalog import CatalogSource
from oabm.exports.gltf import build_glb_bytes, to_glb
from oabm.model import (
    BuildingModel,
    Ceiling,
    ContractError,
    ElectricalDevice,
    HangerAttachment,
    PanelGeometry,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    ProfileExtrusion,
    Quaternion,
    ResolvedParameter,
    Route,
    Slab,
    Vector3,
    WireSweep,
    component_world_points,
    point_on_component_surface,
    stable_assembly_component_id,
)
from oabm.quantities import extract_quantities


def _request(source: BuildingModel, *, angle: float = 0.0,
             overrides: tuple[CeilingParameterOverride, ...] = (),
             selected_by: str = "agent:fixture-selector") -> SuspendedCeilingAssemblyRequest:
    origin = source.ceilings[0].footprint.points[0]
    return SuspendedCeilingAssemblyRequest(
        assembly_id=SYNTHETIC_CEILING_ASSEMBLY_ID,
        ceiling_id=SYNTHETIC_CEILING_ID,
        deck_id=SYNTHETIC_DECK_ID,
        selected_by=selected_by,
        grid_origin_x_m=origin.x,
        grid_origin_y_m=origin.y,
        grid_axis_angle_rad=angle,
        edge_policy=SYNTHETIC_CEILING_EDGE_POLICY,
        parameter_overrides=overrides,
    )


def _local_world(pose: Pose, point: tuple[float, float, float]) -> Point3:
    q = pose.rotation
    x, y, z = point
    tx = 2.0 * (q.y * z - q.z * y)
    ty = 2.0 * (q.z * x - q.x * z)
    tz = 2.0 * (q.x * y - q.y * x)
    rotated = (
        x + q.w * tx + q.y * tz - q.z * ty,
        y + q.w * ty + q.z * tx - q.x * tz,
        z + q.w * tz + q.x * ty - q.y * tx,
    )
    return Point3(
        x=pose.position.x + rotated[0],
        y=pose.position.y + rotated[1],
        z=pose.position.z + rotated[2],
    )


def _area_xy(points: tuple[Point3, ...]) -> float:
    return abs(math.fsum(
        first.x * second.y - second.x * first.y
        for first, second in zip(points, points[1:] + points[:1])
    )) / 2.0


def _parse_glb_json(payload: bytes) -> dict:
    magic, version, declared_length = struct.unpack_from("<III", payload, 0)
    assert magic == 0x46546C67 and version == 2 and declared_length == len(payload)
    json_length, chunk_type = struct.unpack_from("<II", payload, 12)
    assert chunk_type == 0x4E4F534A
    return json.loads(payload[20:20 + json_length].decode("utf-8"))


def test_synthetic_suspended_ceiling_has_known_physical_parts_and_deterministic_ids():
    source = synthetic_rectangular_ceiling_source()
    source_json = source.to_json(indent=None)
    first = build_suspended_ceiling_assembly(source, _request(source))
    second = build_suspended_ceiling_assembly(source, _request(source))

    assert source.to_json(indent=None) == source_json
    assert first.to_json(indent=None) == second.to_json(indent=None)
    assert first.schema_version == "1.6.0"
    assert len(first.building_assemblies) == 1
    assert len(first.assembly_components) == 87
    assembly = first.building_assemblies[0]
    counts = Counter(component.component_kind for component in first.assembly_components)
    assert counts == {
        "tbar_main_runner": 6,
        "tbar_cross_runner": 28,
        "tile": 35,
        "hanger_wire": 18,
    }
    assert assembly.assembly_kind == "suspended_ceiling"
    assert assembly.host_id == SYNTHETIC_CEILING_ID
    assert assembly.host_representation == "aggregate_host"
    assert assembly.deck_id == SYNTHETIC_DECK_ID
    assert assembly.catalog_revision == "1.0.0"
    assert assembly.generator_version == "oabm-suspended-ceiling-producer/1.0.0"
    assert assembly.component_ids == tuple(sorted(component.id for component in first.assembly_components))
    assert len(assembly.component_ids) == len(set(assembly.component_ids))
    assert all(component.id == stable_assembly_component_id(assembly.id, component.part_key)
               for component in first.assembly_components)
    assert all(component.material_id is None and component.material_basis == "unknown"
               for component in first.assembly_components)
    assert all(component.geometry_basis == "specified_parameterized_geometry"
               for component in first.assembly_components)
    assert not any(component.component_kind == "deck_panel" for component in first.assembly_components)
    assert all(component.geometry_basis != "manufacturer_actual_geometry"
               for component in first.assembly_components)
    assert assembly.input_digest != "0" * 64 and assembly.output_digest != "0" * 64

    origins = {parameter.parameter_id: parameter.origin for parameter in assembly.resolved_parameters}
    assert origins["ceiling.source_thickness_m"] == "plan_observed"
    assert origins["ceiling.grid_origin_x_m"] == "user_override"
    assert origins["ceiling.grid_axis_angle_rad"] == "user_override"
    assert origins["ceiling.module_spacing_m"] == "preset_default"
    assert origins["ceiling.tile.edge_clearance_m"] == "preset_default"
    assert origins[CEILING_DATUM_OFFSET_KEY] == "preset_default"
    source_thickness = next(parameter for parameter in assembly.resolved_parameters
                            if parameter.parameter_id == "ceiling.source_thickness_m")
    assert source_thickness.value == 0.05
    assert source_thickness.source_entity_id == SYNTHETIC_CEILING_ID
    assert source_thickness.source_field_path == "thickness_m"
    placement = next(parameter for parameter in assembly.resolved_parameters
                     if parameter.parameter_id == CEILING_DATUM_OFFSET_KEY)
    assert placement.value == 0.0
    assert placement.provenance[0].attributes["placement_case"] == (
        "observed_ceiling_thickness_align_tile_top_to_source_top"
    )
    assert placement.provenance[0].attributes["source_parameter_id"] == "ceiling.source_thickness_m"
    assert assembly.attributes["source_ceiling_thickness_semantics"].startswith(
        "Ceiling footprint is the top-face datum"
    )
    assert "not evaluated" in assembly.attributes["structural_capacity_fire_listing_and_code_approval"]
    assert tuple(assembly.attributes["unsupported_tile_edge_boundaries"]) == (
        "host_grid_u_min", "host_grid_u_max", "host_grid_v_min", "host_grid_v_max",
    )


def test_synthetic_tile_and_runner_faces_use_ceiling_top_datum_and_only_surface_contact():
    model = synthetic_rectangular_suspended_ceiling_assembly()
    ceiling_z = model.ceilings[0].footprint.points[0].z
    source_bottom_z = ceiling_z - model.ceilings[0].thickness_m
    tile_thickness = 0.01905
    profile_width = 0.0238125
    profile_depth = 0.0428625
    flange_thickness = 0.003175
    expected_tile_top_z = ceiling_z
    expected_tile_bottom_z = expected_tile_top_z - tile_thickness
    expected_runner_bottom_z = expected_tile_bottom_z - flange_thickness
    expected_runner_top_z = expected_runner_bottom_z + profile_depth
    deck_underside_z = model.slabs[0].footprint.points[0].z - model.slabs[0].thickness_m

    tiles = [component for component in model.assembly_components if component.component_kind == "tile"]
    runners = [component for component in model.assembly_components
               if component.component_kind in {"tbar_main_runner", "tbar_cross_runner"}]
    mains = [component for component in runners if component.component_kind == "tbar_main_runner"]
    crosses = [component for component in runners if component.component_kind == "tbar_cross_runner"]
    assert len(tiles) == 35 and len(runners) == 34
    for tile in tiles:
        geometry = tile.geometry
        assert isinstance(geometry, PanelGeometry)
        assert geometry.boundary.points[0].z - geometry.thickness_m / 2 == pytest.approx(expected_tile_bottom_z)
        assert geometry.boundary.points[0].z + geometry.thickness_m / 2 == pytest.approx(expected_tile_top_z)
        world_points = component_world_points(tile)
        assert all(source_bottom_z - 1e-9 <= point[2] <= deck_underside_z + 1e-9
                   for point in world_points)
        assert all(-1e-9 <= point[0] <= 4.0 + 1e-9 and -1e-9 <= point[1] <= 3.0 + 1e-9
                   for point in world_points)

    assert all(isinstance(runner.geometry, ProfileExtrusion) for runner in runners)
    assert all(runner.geometry.pose.position.z == pytest.approx(expected_runner_bottom_z)
               for runner in runners)
    assert all(runner.geometry.profile.overall_width_m == pytest.approx(profile_width)
               and runner.geometry.profile.overall_depth_m == pytest.approx(profile_depth)
               for runner in runners)
    # Interior cross tees terminate at main flange side faces. Additional
    # short cross tees carry interior tile seams through perimeter bays.
    assert sum("main-bay:" in cross.part_key for cross in crosses) == 20
    assert all(cross.geometry.length_m == pytest.approx(0.6096 - profile_width)
               for cross in crosses if "main-bay:" in cross.part_key)

    # PanelGeometry.boundary is a mid-surface by contract. Its physical tile
    # bottom sits on the T flange top; it is not substituted for the source
    # Ceiling top-face datum.
    assert expected_tile_top_z == ceiling_z
    assert expected_runner_bottom_z >= source_bottom_z
    assert expected_runner_bottom_z + flange_thickness == pytest.approx(expected_tile_bottom_z)

    hangers = [component for component in model.assembly_components
               if component.component_kind == "hanger_wire"]
    assert len(hangers) == 18
    for hanger in hangers:
        assert isinstance(hanger.geometry, WireSweep)
        attachment = hanger.hanger_attachment
        assert isinstance(attachment, HangerAttachment)
        top, bottom = hanger.geometry.centerline.points
        assert top == attachment.top_anchor and bottom == attachment.bottom_anchor
        assert attachment.top_host_id == SYNTHETIC_DECK_ID
        assert top.z == pytest.approx(deck_underside_z)
        assert bottom.z == pytest.approx(expected_runner_top_z)
        assert math.dist((top.x, top.y, top.z), (bottom.x, bottom.y, bottom.z)) == pytest.approx(
            deck_underside_z - expected_runner_top_z
        )
        assert attachment.bottom_runner_id in {runner.id for runner in mains}


def test_all_interior_tile_edges_have_positive_flange_bearing_and_runner_joints_are_contact_only():
    model = synthetic_rectangular_suspended_ceiling_assembly()
    assembly = model.building_assemblies[0]
    mains = [component for component in model.assembly_components
             if component.component_kind == "tbar_main_runner"]
    crosses = [component for component in model.assembly_components
               if component.component_kind == "tbar_cross_runner"]
    tiles = [component for component in model.assembly_components if component.component_kind == "tile"]
    half_width = mains[0].geometry.profile.overall_width_m / 2
    clearance = assembly.attributes["tile_edge_clearance_m"]
    bearing_width = half_width - clearance
    assert bearing_width > 0
    assert assembly.resolved_parameters[
        [p.parameter_id for p in assembly.resolved_parameters].index("ceiling.tile.edge_clearance_m")
    ].value == pytest.approx(clearance)
    assert assembly.attributes["tile_runner_bearing"].endswith("coplanar with flange top")

    main_intervals = []
    main_centers = {}
    for main in mains:
        vertices = component_world_points(main)
        x_low, x_high = min(point[0] for point in vertices), max(point[0] for point in vertices)
        y_low, y_high = min(point[1] for point in vertices), max(point[1] for point in vertices)
        main_intervals.append((x_low, x_high))
        index = int(main.part_key.rsplit(":", 1)[1])
        main_centers[index] = index * 0.6096

    cross_rows = {}
    cross_intervals = []
    for cross in crosses:
        vertices = component_world_points(cross)
        x_low, x_high = min(point[0] for point in vertices), max(point[0] for point in vertices)
        y_low, y_high = min(point[1] for point in vertices), max(point[1] for point in vertices)
        cross_intervals.append((x_low, x_high))
        row_index = int(cross.part_key.split(":")[2])
        cross_rows[row_index] = row_index * 0.6096

        span_token = cross.part_key.split(":span:", 1)[1]
        contacts: list[tuple[float, int]] = []
        if span_token.startswith("main-bay:"):
            main_index = int(span_token.split(":", 1)[1])
            contacts = [(x_low, main_index), (x_high, main_index + 1)]
        elif span_token == "perimeter-low":
            contacts = [(x_high, min(main_centers))]
        elif span_token == "perimeter-high":
            contacts = [(x_low, max(main_centers))]
        else:  # pragma: no cover - fixture part-key contract
            pytest.fail(f"unexpected cross runner span {span_token}")
        for x_contact, main_index in contacts:
            main = next(row for row in mains
                        if int(row.part_key.rsplit(":", 1)[1]) == main_index)
            # Midpoint of the real lower-flange contact patch. The tee's upper
            # web-side separation is separately disclosed and not asserted as
            # a full end-face or mechanical connection.
            contact = Point3(x=x_contact, y=0.6096 * row_index,
                             z=main.geometry.pose.position.z + main.geometry.profile.flange_thickness_m / 2)
            assert point_on_component_surface(cross, contact)
            assert point_on_component_surface(main, contact)

    # Runners share only these flange-side end faces; no cross/main pair has
    # positive-volume AABB intersection in the common horizontal grid frame.
    for main in mains:
        main_points = component_world_points(main)
        main_bounds = tuple((min(point[axis] for point in main_points),
                             max(point[axis] for point in main_points)) for axis in range(3))
        for cross in crosses:
            cross_points = component_world_points(cross)
            cross_bounds = tuple((min(point[axis] for point in cross_points),
                                 max(point[axis] for point in cross_points)) for axis in range(3))
            overlaps = tuple(min(a[1], b[1]) - max(a[0], b[0])
                             for a, b in zip(main_bounds, cross_bounds))
            assert not all(overlap > 1e-9 for overlap in overlaps)

    perimeter_edge_count = 0
    for tile in tiles:
        points = tile.geometry.boundary.points
        for first, second in zip(points, points[1:] + points[:1]):
            edge_length = math.dist((first.x, first.y), (second.x, second.y))
            assert edge_length > 0
            if abs(first.x - second.x) <= 1e-9:
                x = first.x
                if abs(x) <= 1e-9 or abs(x - 4.0) <= 1e-9:
                    perimeter_edge_count += 1
                    continue
                center = min(main_centers.values(), key=lambda value: abs(value - x))
                assert abs(abs(center - x) - clearance) <= 1e-9
                assert bearing_width * edge_length > 0
            else:
                y = first.y
                if abs(y) <= 1e-9 or abs(y - 3.0) <= 1e-9:
                    perimeter_edge_count += 1
                    continue
                center = min(cross_rows.values(), key=lambda value: abs(value - y))
                assert abs(abs(center - y) - clearance) <= 1e-9
                # Cross tees and the main-runner flange intervals jointly
                # cover this full interior seam, including the main-line gaps.
                intervals = sorted((*main_intervals, *cross_intervals))
                left, right = sorted((first.x, second.x))
                cursor = left
                for interval_left, interval_right in intervals:
                    if interval_right < cursor - 1e-9:
                        continue
                    if interval_left > cursor + 1e-9:
                        break
                    cursor = max(cursor, interval_right)
                    if cursor >= right - 1e-9:
                        break
                assert cursor >= right - 1e-9
                assert bearing_width * edge_length > 0

    assert perimeter_edge_count > 0
    assert "host_grid_u_min" in assembly.attributes["unsupported_tile_edge_boundaries"]
    assert "host_grid_v_max" in assembly.attributes["unsupported_tile_edge_boundaries"]


def test_known_thickness_must_contain_selected_stack_but_missing_thickness_has_explicit_preset_offset():
    too_thin = synthetic_rectangular_ceiling_source(ceiling_thickness_m=0.01)
    with pytest.raises(SuspendedCeilingPresetError, match="unresolved Ceiling thickness/datum"):
        build_suspended_ceiling_assembly(too_thin, _request(too_thin))

    no_thickness = synthetic_rectangular_ceiling_source(ceiling_thickness_m=None)
    no_thickness_model = build_suspended_ceiling_assembly(no_thickness, _request(no_thickness))
    assembly = no_thickness_model.building_assemblies[0]
    params = {parameter.parameter_id: parameter for parameter in assembly.resolved_parameters}
    offset = params[CEILING_DATUM_OFFSET_KEY]
    assert "ceiling.source_thickness_m" not in params
    assert offset.origin == "preset_default"
    assert offset.value == pytest.approx(0.01905 + 0.003175)
    assert offset.provenance[0].attributes["placement_case"] == (
        "missing_ceiling_thickness_place_flange_bottom_on_source_datum"
    )
    assert offset.provenance[0].attributes["default_formula"] == (
        "resolved(ceiling.tile.thickness_m) + resolved(ceiling.runner.flange_thickness_m)"
    )
    assert offset.provenance[0].attributes["source_entity_id"] == SYNTHETIC_CEILING_ID
    assert offset.provenance[0].attributes["source_field_path"] == "thickness_m"
    assert assembly.attributes["tile_top_offset_m"] == pytest.approx(offset.value)

    overridden = build_suspended_ceiling_assembly(
        no_thickness,
        _request(no_thickness, overrides=(CeilingParameterOverride(
            parameter_id=CEILING_DATUM_OFFSET_KEY,
            value_kind="length", value=0.04, unit="m",
        ),)),
    )
    chosen = next(parameter for parameter in overridden.building_assemblies[0].resolved_parameters
                  if parameter.parameter_id == CEILING_DATUM_OFFSET_KEY)
    assert chosen.origin == "user_override" and chosen.value == pytest.approx(0.04)
    assert overridden.building_assemblies[0].attributes["tile_top_offset_m"] == pytest.approx(0.04)


def test_source_observations_require_geometry_scopes_and_slab_is_explicit():
    source = synthetic_rectangular_ceiling_source()
    ceiling = replace(source.ceilings[0], provenance=(replace(
        source.ceilings[0].provenance[0], scope_paths=("name",),
    ),))
    bad = replace(source, ceilings=(ceiling,))
    with pytest.raises(SuspendedCeilingPresetError, match="does not cover geometry field"):
        build_suspended_ceiling_assembly(bad, _request(bad))

    wrong_deck = _request(source)
    with pytest.raises(SuspendedCeilingPresetError, match="existing deck Slab .* is missing"):
        build_suspended_ceiling_assembly(source, replace(wrong_deck, deck_id="slab:missing"))

    with pytest.raises(SuspendedCeilingPresetError, match="unknown or unsupported suspended-ceiling preset"):
        build_suspended_ceiling_assembly(source, replace(wrong_deck, preset_id="ceiling.unknown"))

    with pytest.raises(SuspendedCeilingPresetError, match="edge_policy must explicitly"):
        SuspendedCeilingAssemblyRequest(
            assembly_id="assembly:bad-edge", ceiling_id=SYNTHETIC_CEILING_ID,
            deck_id=SYNTHETIC_DECK_ID, selected_by="agent:test", grid_origin_x_m=0,
            grid_origin_y_m=0, grid_axis_angle_rad=0, edge_policy="automatic",
        )


def test_component_and_vertex_preflight_rejects_dense_grids_before_span_generation(monkeypatch):
    source = synthetic_rectangular_ceiling_source()

    def forbidden_span_generation(*_args, **_kwargs):
        pytest.fail("geometry span allocation ran before complete component preflight")

    monkeypatch.setattr(ceiling_producer, "_cross_runner_spans", forbidden_span_generation)
    request = _request(source, overrides=(CeilingParameterOverride(
        parameter_id="ceiling.module_spacing_m", value_kind="length", value=0.03, unit="m",
    ),))
    with pytest.raises(SuspendedCeilingPresetError, match="component_cap_exceeded"):
        build_suspended_ceiling_assembly(source, request)


def test_rotated_horizontal_rectangular_grid_and_clockwise_footprints_are_supported():
    rotation = math.pi / 6
    rotated = synthetic_rectangular_suspended_ceiling_assembly(
        rotation_rad=rotation, origin_x_m=5.0, origin_y_m=-2.0,
    )
    clockwise = synthetic_rectangular_suspended_ceiling_assembly(
        rotation_rad=rotation, origin_x_m=5.0, origin_y_m=-2.0, clockwise=True,
    )
    clockwise_repeat = synthetic_rectangular_suspended_ceiling_assembly(
        rotation_rad=rotation, origin_x_m=5.0, origin_y_m=-2.0, clockwise=True,
    )
    assert len(rotated.assembly_components) == len(clockwise.assembly_components) == 87
    assert clockwise.to_json(indent=None) == clockwise_repeat.to_json(indent=None)
    for component in rotated.assembly_components:
        if component.component_kind in {"tbar_main_runner", "tbar_cross_runner"}:
            assert isinstance(component.geometry, ProfileExtrusion)
            direction = _local_world(component.geometry.pose, (0.0, 0.0, 1.0))
            dx = direction.x - component.geometry.pose.position.x
            dy = direction.y - component.geometry.pose.position.y
            assert math.hypot(dx, dy) == pytest.approx(1.0)

    source = synthetic_rectangular_ceiling_source(rotation_rad=rotation, origin_x_m=5.0, origin_y_m=-2.0)
    request = _request(source, angle=rotation + 0.1)
    with pytest.raises(SuspendedCeilingPresetError, match="grid angle must be parallel"):
        build_suspended_ceiling_assembly(source, request)


@pytest.mark.parametrize("bad_kind", ("skew", "sloped"))
def test_nonrectangular_or_sloped_source_footprints_are_refused(bad_kind: str):
    source = synthetic_rectangular_ceiling_source()
    ceiling = source.ceilings[0]
    points = list(ceiling.footprint.points)
    if bad_kind == "skew":
        points[2] = Point3(x=3.8, y=3.0, z=points[2].z)
        message = "opposite rectangular edges must match"
    else:
        points[2] = Point3(x=points[2].x, y=points[2].y, z=points[2].z + 0.1)
        message = "sloped footprints are not supported"
    bad_ceiling = replace(ceiling, footprint=Polygon3D(points=tuple(points)))
    bad_source = replace(source, ceilings=(bad_ceiling,))
    with pytest.raises(SuspendedCeilingPresetError, match=message):
        build_suspended_ceiling_assembly(bad_source, _request(bad_source))

    if bad_kind == "sloped":
        slab = source.slabs[0]
        slab_points = list(slab.footprint.points)
        slab_points[1] = Point3(x=slab_points[1].x, y=slab_points[1].y,
                                z=slab_points[1].z + 0.1)
        bad_slab = replace(slab, footprint=Polygon3D(points=tuple(slab_points)))
        bad_source = replace(source, slabs=(bad_slab,))
        with pytest.raises(SuspendedCeilingPresetError, match="sloped footprints are not supported"):
            build_suspended_ceiling_assembly(bad_source, _request(bad_source))


def test_replacement_and_models_with_routes_are_refused():
    source = synthetic_rectangular_ceiling_source()
    assigned = build_suspended_ceiling_assembly(source, _request(source))
    with pytest.raises(SuspendedCeilingPresetError, match="already has an active suspended-ceiling"):
        build_suspended_ceiling_assembly(assigned, _request(source))

    start = Point3(x=0.0, y=0.0, z=0.1)
    finish = Point3(x=1.0, y=0.0, z=0.1)
    device_a = ElectricalDevice(id="device:route-start", device_type="test", pose=Pose(position=start))
    device_b = ElectricalDevice(id="device:route-end", device_type="test", pose=Pose(position=finish))
    port_a = Port(
        id="port:route-start", owner_id=device_a.id, domain="power", role="source",
        pose=Pose(position=start), direction=Vector3(x=1, y=0, z=0),
    )
    port_b = Port(
        id="port:route-end", owner_id=device_b.id, domain="power", role="sink",
        pose=Pose(position=finish), direction=Vector3(x=-1, y=0, z=0),
    )
    route = Route(
        id="route:already-built", route_type="emt", start_port_id=port_a.id,
        end_port_id=port_b.id, centerline=Polyline3D(points=(start, finish)),
    )
    routed_source = replace(source, electrical_devices=(device_a, device_b),
                            ports=(port_a, port_b), routes=(route,))
    with pytest.raises(SuspendedCeilingPresetError, match="refuses models with existing routes"):
        build_suspended_ceiling_assembly(routed_source, _request(routed_source))


def test_glb_and_quantity_consumers_use_same_component_ids_without_aggregate_or_deck_duplication(tmp_path):
    model = synthetic_rectangular_suspended_ceiling_assembly()
    rebuilt = synthetic_rectangular_suspended_ceiling_assembly()
    payload = build_glb_bytes(model)
    assert payload == build_glb_bytes(rebuilt)
    gltf = _parse_glb_json(payload)
    names = [node["name"] for node in gltf["nodes"]]
    component_ids = {component.id for component in model.assembly_components}
    assert component_ids.issubset(set(names))
    assert names.count(SYNTHETIC_DECK_ID) == 1
    assert SYNTHETIC_CEILING_ID not in names
    assert not any("deck_panel" in name for name in names)
    assert sum(node.get("extras", {}).get("canonical_id") == SYNTHETIC_DECK_ID
               for node in gltf["nodes"]) == 1
    out_a, out_b = tmp_path / "ceiling-a.glb", tmp_path / "ceiling-b.glb"
    summary_a = to_glb(model, out_a)
    summary_b = to_glb(rebuilt, out_b)
    assert out_a.read_bytes() == out_b.read_bytes()
    assert summary_a["assembly_components"] == summary_b["assembly_components"] == len(component_ids)
    assert summary_a["slabs"] == summary_b["slabs"] == 1

    report = extract_quantities(model)
    totals: dict[tuple[str, str], float] = defaultdict(float)
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in report.items:
        totals[(item.category, item.item_type)] += item.quantity
        sources[(item.category, item.item_type)].update(item.source_entity_ids)
    assert totals[("assembly_component_count", "tbar_main_runner")] == 6
    assert totals[("assembly_component_count", "tbar_cross_runner")] == 28
    assert totals[("assembly_component_count", "tile")] == 35
    assert totals[("assembly_component_count", "hanger_wire")] == 18
    assert set().union(*(ids for (category, _), ids in sources.items()
                         if category == "assembly_component_count")) == component_ids
    tile_components = [component for component in model.assembly_components
                       if component.component_kind == "tile"]
    tile_area = math.fsum(_area_xy(component.geometry.boundary.points)
                          for component in tile_components)
    tile_volume = tile_area * 0.01905
    assert totals[("assembly_component_geometry_volume", "tile")] == pytest.approx(tile_volume)
    assert totals[("assembly_component_surface_area", "tile")] == pytest.approx(
        math.fsum(
            2 * _area_xy(component.geometry.boundary.points)
            + math.fsum(
                math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) * 0.01905
                for a, b in zip(component.geometry.boundary.points,
                                component.geometry.boundary.points[1:] + component.geometry.boundary.points[:1])
            )
            for component in tile_components
        )
    )
    def _profile_area(component):
        outline = component.geometry.profile.outline_xy_m()
        return abs(math.fsum(
            x0 * y1 - x1 * y0
            for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1])
        )) / 2.0

    for runner_kind in ("tbar_main_runner", "tbar_cross_runner"):
        runners = [component for component in model.assembly_components
                   if component.component_kind == runner_kind]
        expected_length = math.fsum(component.geometry.length_m for component in runners)
        expected_profile_area = math.fsum(_profile_area(component) for component in runners)
        expected_volume = math.fsum(_profile_area(component) * component.geometry.length_m
                                    for component in runners)
        assert totals[("assembly_component_length", runner_kind)] == pytest.approx(expected_length)
        assert totals[("assembly_component_cross_section_area", runner_kind)] == pytest.approx(
            expected_profile_area
        )
        assert totals[("assembly_component_geometry_volume", runner_kind)] == pytest.approx(expected_volume)

    wire_components = [component for component in model.assembly_components
                       if component.component_kind == "hanger_wire"]
    expected_wire_length = math.fsum(
        math.dist((first.x, first.y, first.z), (second.x, second.y, second.z))
        for component in wire_components
        for first, second in zip(component.geometry.centerline.points,
                                 component.geometry.centerline.points[1:])
    )
    expected_wire_volume = math.fsum(
        math.pi * (component.geometry.diameter_m / 2.0) ** 2 *
        math.dist((first.x, first.y, first.z), (second.x, second.y, second.z))
        for component in wire_components
        for first, second in zip(component.geometry.centerline.points,
                                 component.geometry.centerline.points[1:])
    )
    assert totals[("assembly_component_length", "hanger_wire")] == pytest.approx(expected_wire_length)
    assert totals[("assembly_component_geometry_volume", "hanger_wire")] == pytest.approx(
        expected_wire_volume
    )
    assert "slab_volume" in {item.category for item in report.items}
    assert not any(component_id in sources[("assembly_component_geometry_volume", "deck_panel")]
                   for component_id in component_ids)
    assert not any(item.category == "ceiling_volume" for item in report.items)


def test_catalog_is_immutable_versioned_and_reports_simplifications():
    catalog = DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG
    first = catalog.to_json(indent=None)
    assert first == catalog.to_json(indent=None)
    preset = catalog.suspended_ceiling_preset(SUSPENDED_CEILING_PRESET_ID)
    assert preset.preset_revision == "1.0.0"
    assert preset.preset_id == "ceiling.tbar_609_6mm_module_illustrative"
    assert preset.generator_kind == "rectangular_tbar_609_6mm_module_grid_v1"
    assert preset.parameter("ceiling.module_spacing_m").default_value == pytest.approx(0.6096)
    assert "24-inch (609.6 mm)" in preset.description
    assert "609.6 mm" in synthetic_rectangular_ceiling_source().ceilings[0].name
    assert preset.edge_policy == SYNTHETIC_CEILING_EDGE_POLICY
    assert "Boundary trim, joint tabs and fixings are not modeled." in preset.description
    assert "not_manufacturer_exact" in preset.profile_geometry_basis
    assert preset.tile_top_offset_without_thickness_formula == (
        "resolved(ceiling.tile.thickness_m) + resolved(ceiling.runner.flange_thickness_m)"
    )


def test_builtin_catalog_content_identity_is_reserved_but_overrides_and_owned_revisions_work():
    source = synthetic_rectangular_ceiling_source()
    builtin = DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG
    preset = builtin.presets[0]
    module_id = "ceiling.module_spacing_m"
    edge_clearance_id = "ceiling.tile.edge_clearance_m"
    original_module = preset.parameter(module_id)
    modified_edge_clearance = replace(
        preset.parameter(edge_clearance_id), default_value=0.004,
        note="Changed content kept under the reserved built-in identity for guard testing.",
    )
    modified_parameters = tuple(
        modified_edge_clearance if definition.parameter_id == edge_clearance_id else definition
        for definition in preset.parameters
    )
    mislabeled_module = replace(
        original_module, default_value=0.7, source_id=None,
        default_basis="illustrative_modeling_assumption",
        note="Non-24-in custom grid spacing.",
    )
    mislabeled_parameters = tuple(
        mislabeled_module if definition.parameter_id == module_id else definition
        for definition in preset.parameters
    )
    with pytest.raises(ContractError, match="nominal 609.6 mm generator kind requires"):
        replace(preset, parameters=mislabeled_parameters)

    altered_builtin_preset = replace(preset, parameters=modified_parameters)

    altered_builtin_catalog = replace(builtin, presets=(altered_builtin_preset,))
    with pytest.raises(SuspendedCeilingPresetError, match="reserved built-in suspended-ceiling catalog identity"):
        build_suspended_ceiling_assembly(source, _request(source), catalog=altered_builtin_catalog)

    altered_preset_in_custom_catalog = replace(
        builtin, catalog_id="catalog:caller-owned-ceiling", catalog_revision="2.0.0",
        presets=(altered_builtin_preset,),
    )
    with pytest.raises(SuspendedCeilingPresetError, match="reserved built-in suspended-ceiling preset identity"):
        build_suspended_ceiling_assembly(
            source, _request(source), catalog=altered_preset_in_custom_catalog,
        )

    tampered_source = replace(
        builtin.sources[0], title="Caller content substituted under built-in source ID",
    )
    altered_sources = tuple(tampered_source if row.source_id == tampered_source.source_id else row
                            for row in builtin.sources)
    source_spoof_preset = replace(
        preset,
        preset_id="ceiling.tbar_source_spoof_test",
        preset_revision="1.0.0",
        generator_kind="rectangular_tbar_module_grid_v1",
        description="Caller-owned preset with a substituted published source record.",
    )
    source_spoof_catalog = replace(
        builtin, catalog_id="catalog:caller-sources", catalog_revision="1.0.0",
        sources=altered_sources, presets=(source_spoof_preset,),
    )
    source_spoof_request = replace(_request(source), preset_id=source_spoof_preset.preset_id)
    with pytest.raises(SuspendedCeilingPresetError, match="reserved built-in suspended-ceiling preset sources"):
        build_suspended_ceiling_assembly(source, source_spoof_request, catalog=source_spoof_catalog)

    override_model = build_suspended_ceiling_assembly(
        source, _request(source, overrides=(CeilingParameterOverride(
            parameter_id=module_id, value_kind="length", value=0.7, unit="m",
        ),)),
    )
    override = next(parameter for parameter in override_model.building_assemblies[0].resolved_parameters
                    if parameter.parameter_id == module_id)
    assert override.origin == "user_override" and override.value == pytest.approx(0.7)
    assert override_model.building_assemblies[0].catalog_id == builtin.catalog_id

    caller_source_id = "source:caller-ceiling-module"
    caller_source = CatalogSource(
        source_id=caller_source_id,
        title="Caller-owned 0.7 m module spacing assumption",
        url="https://example.invalid/caller-ceiling-module",
        locator="Synthetic caller test fixture",
        use_note="Demonstrates a separately identified caller source; not a product claim.",
    )
    caller_module = replace(
        original_module, default_value=0.7, source_id=caller_source_id,
        default_basis="published_dimension_modeling_assumption",
        note="Caller-owned 0.7 m grid spacing assumption with distinct source identity.",
    )
    caller_parameters = tuple(
        caller_module if definition.parameter_id == module_id else definition
        for definition in preset.parameters
    )
    caller_preset = replace(
        preset,
        preset_id="ceiling.tbar_custom_700mm_illustrative",
        preset_revision="1.0.0",
        parameters=caller_parameters,
        generator_kind="rectangular_tbar_module_grid_v1",
        description="Caller-owned rectangular T-bar grid with explicit 0.7 m module spacing.",
    )
    caller_catalog = replace(
        builtin,
        catalog_id="catalog:caller-owned-ceiling",
        catalog_revision="1.0.0",
        sources=(*builtin.sources, caller_source),
        presets=(caller_preset,),
    )
    caller_request = replace(
        _request(source), preset_id=caller_preset.preset_id,
    )
    caller_model = build_suspended_ceiling_assembly(source, caller_request, catalog=caller_catalog)
    caller_assignment = caller_model.building_assemblies[0]
    caller_module = next(parameter for parameter in caller_assignment.resolved_parameters
                         if parameter.parameter_id == module_id)
    assert caller_assignment.catalog_id == caller_catalog.catalog_id
    assert caller_assignment.catalog_revision == caller_catalog.catalog_revision
    assert caller_assignment.preset_id == caller_preset.preset_id
    assert caller_module.origin == "preset_default" and caller_module.value == pytest.approx(0.7)
    assert caller_module.provenance[0].attributes["source_id"] == caller_source_id
