"""Synthetic real-producer proof for bounded #356 wall-cavity routing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_source
from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_assembly
from oabm.building_assemblies.wall import WallAssemblyRequest, build_wall_assembly
from oabm.model import (
    ContractError,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Port,
    Pose,
    Polyline3D,
    Vector3,
    compute_assembly_output_digest,
)
from oabm.routing import (
    NoRouteError,
    RoutingError,
    RoutingOptions,
    derive_wall_cavity_route_domain,
    route_between_ports,
)
from oabm.routing.router import (
    _collect_routing_geometry,
    _point_within_allowed_domain,
    _segment_clear,
    _segment_within_allowed_domain,
)


def _built_wall(*, end: Point3 | None = None):
    source = synthetic_two_opening_wall_source()
    wall = source.walls[0]
    endpoint = end or Point3(x=4.8, y=0.0, z=0.0)
    wall = replace(
        wall,
        centerline=Polyline3D(points=(wall.centerline.points[0], endpoint)),
    )
    source = replace(source, walls=(wall,), openings=())
    return build_wall_assembly(
        source,
        WallAssemblyRequest(
            assembly_id="assembly:wall-route-proof",
            wall_id=wall.id,
            selected_by="agent:synthetic-wall-route-test",
        ),
    )


def _with_ports(
    model,
    start: Point3,
    end: Point3,
    *,
    start_direction: Vector3 = Vector3(x=1.0, y=0.0, z=0.0),
    end_direction: Vector3 = Vector3(x=-1.0, y=0.0, z=0.0),
):
    level_id = model.walls[0].level_id
    equipment = ElectricalEquipment(
        id="equipment:wall-route-source",
        equipment_type="synthetic-source",
        pose=Pose(position=start),
        level_id=level_id,
    )
    device = ElectricalDevice(
        id="device:wall-route-load",
        device_type="synthetic-load",
        pose=Pose(position=end),
        level_id=level_id,
    )
    ports = (
        Port(
            id="port:wall-route-source", owner_id=equipment.id,
            domain="electrical", role="source", pose=equipment.pose,
            direction=start_direction, nominal_diameter_m=0.02,
        ),
        Port(
            id="port:wall-route-load", owner_id=device.id,
            domain="electrical", role="load", pose=device.pose,
            direction=end_direction, nominal_diameter_m=0.02,
        ),
    )
    return replace(
        model,
        electrical_equipment=(equipment,),
        electrical_devices=(device,),
        ports=ports,
    )


def _cell_bounds(volume):
    center = volume.pose.position
    return (
        center.x - volume.size.x / 2, center.x + volume.size.x / 2,
        center.y - volume.size.y / 2, center.y + volume.size.y / 2,
        center.z - volume.size.z / 2, center.z + volume.size.z / 2,
    )


def test_real_wall_producer_routes_inside_one_generated_bay_with_bound_receipts():
    model = _built_wall()
    options = RoutingOptions(port_stub_m=0.05, clearance_m=0.005)
    domain = derive_wall_cavity_route_domain(
        model, "assembly:wall-route-proof", nominal_diameter_m=0.02,
        options=options,
    )
    first_cell = sorted(domain.volumes, key=lambda row: row.pose.position.x)[0]
    x_min, x_max, y_min, y_max, z_min, z_max = _cell_bounds(first_cell)
    center = first_cell.pose.position
    model = _with_ports(
        model,
        Point3(x=center.x - 0.08, y=center.y, z=center.z),
        Point3(x=center.x + 0.08, y=center.y, z=center.z),
    )

    first = route_between_ports(
        model, "port:wall-route-source", "port:wall-route-load", "emt",
        options=options, allowed_domain=domain,
    )
    assert first == route_between_ports(
        model, "port:wall-route-source", "port:wall-route-load", "emt",
        options=options, allowed_domain=domain,
    )
    route, fittings = first
    padding = 0.02 / 2 + options.clearance_m
    route_points = route.centerline.points
    assert all(
        x_min + padding - 1e-9 <= point.x <= x_max - padding + 1e-9
        and y_min + padding - 1e-9 <= point.y <= y_max - padding + 1e-9
        and z_min + padding - 1e-9 <= point.z <= z_max - padding + 1e-9
        for point in route_points
    )
    geometry = _collect_routing_geometry(
        model, "emt", 0.01, options, allowed_domain=domain,
    )
    assert all(_segment_within_allowed_domain(a, b, geometry.allowed_domain)
               for a, b in zip(route_points, route_points[1:]))
    assert all(_segment_clear(a, b, geometry.hard_blockers)
               for a, b in zip(route_points, route_points[1:]))
    assert all(_point_within_allowed_domain(point, geometry.allowed_domain)
               for point in route_points)
    checked_ids = set(route.attributes["geometry_check"]["checked_blocker_ids"])
    component_ids = {row.id for row in model.assembly_components}
    assert component_ids <= checked_ids
    assert route.attributes["geometry_check"]["status"] == "contained_and_clear_of_checked_blockers"

    context = route.provenance[0].attributes["bounded_routing_context"]
    assert context["allowed_route_domain"]["id"] == domain.id
    assert context["allowed_route_domain"]["source_ids"] == list(domain.source_ids)
    assert context["assembly_routing_context"]["checked_component_ids"] == sorted(component_ids)
    assert not fittings or all(row.route_id == route.id for row in fittings)


def test_wall_domain_refuses_outside_drywall_and_adjacent_bay_routes():
    model = _built_wall()
    options = RoutingOptions(port_stub_m=0.03, clearance_m=0.002)
    domain = derive_wall_cavity_route_domain(
        model, "assembly:wall-route-proof", nominal_diameter_m=0.02,
        options=options,
    )
    cells = sorted(domain.volumes, key=lambda row: row.pose.position.x)
    first = cells[0].pose.position
    outside = Point3(x=first.x, y=first.y + cells[0].size.y / 2 + 0.005, z=first.z)
    outside_model = _with_ports(
        model,
        outside,
        Point3(x=first.x + 0.08, y=first.y, z=first.z),
    )
    with pytest.raises(NoRouteError) as outside_error:
        route_between_ports(
            outside_model, "port:wall-route-source", "port:wall-route-load", "emt",
            options=options, allowed_domain=domain,
        )
    assert outside_error.value.reason == "start_outside_allowed_domain"

    left, right = cells[0].pose.position, cells[1].pose.position
    adjacent_model = _with_ports(
        model,
        Point3(x=left.x, y=left.y, z=left.z),
        Point3(x=right.x, y=right.y, z=right.z),
    )
    with pytest.raises(NoRouteError):
        route_between_ports(
            adjacent_model, "port:wall-route-source", "port:wall-route-load", "emt",
            options=options, allowed_domain=domain,
        )


def test_wall_domain_binds_generated_parts_and_invalidates_old_domain_and_route():
    model = _built_wall()
    options = RoutingOptions(port_stub_m=0.05)
    domain = derive_wall_cavity_route_domain(
        model, "assembly:wall-route-proof", nominal_diameter_m=0.02,
        options=options,
    )
    center = domain.volumes[0].pose.position
    model = _with_ports(
        model,
        Point3(x=center.x - 0.08, y=center.y, z=center.z),
        Point3(x=center.x + 0.08, y=center.y, z=center.z),
    )
    route, fittings = route_between_ports(
        model, "port:wall-route-source", "port:wall-route-load", "emt",
        options=options, allowed_domain=domain,
    )
    routed_model = replace(model, routes=(route,), route_fittings=fittings)
    forged_volume = replace(
        domain.volumes[0],
        size=replace(domain.volumes[0].size, x=domain.volumes[0].size.x + 0.001),
    )
    forged_domain = replace(
        domain, volumes=(forged_volume, *domain.volumes[1:]),
    )
    with pytest.raises(RoutingError, match="stale or does not match current geometry"):
        route_between_ports(
            model, "port:wall-route-source", "port:wall-route-load", "emt",
            options=options, allowed_domain=forged_domain,
        )

    edited_components = list(model.assembly_components)
    target_index = next(
        index for index, row in enumerate(edited_components)
        if row.component_kind == "stud"
        and row.part_key.startswith("stud:station:station-00004")
    )
    component = edited_components[target_index]
    position = component.geometry.pose.position
    edited_components[target_index] = replace(
        component,
        geometry=replace(
            component.geometry,
            pose=replace(component.geometry.pose, position=replace(position, x=position.x + 0.001)),
        ),
    )
    edited_components = tuple(edited_components)
    assignment = model.building_assemblies[0]
    edited_assignment = replace(
        assignment,
        output_digest=compute_assembly_output_digest(edited_components),
    )
    edited_model = replace(
        model,
        assembly_components=edited_components,
        building_assemblies=(edited_assignment,),
    )
    updated_domain = derive_wall_cavity_route_domain(
        edited_model, assignment.id, nominal_diameter_m=0.02, options=options,
    )
    assert updated_domain.id != domain.id
    with pytest.raises(RoutingError, match="stale or does not match current geometry"):
        route_between_ports(
            edited_model, "port:wall-route-source", "port:wall-route-load", "emt",
            options=options, allowed_domain=domain,
        )

    with pytest.raises(ContractError, match="stale assembly routing context"):
        replace(
            routed_model,
            assembly_components=edited_components,
            building_assemblies=(edited_assignment,),
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        Point3(x=-4.8, y=0.0, z=0.0),
        Point3(x=0.0, y=4.8, z=0.0),
        Point3(x=0.0, y=-4.8, z=0.0),
    ),
)
def test_wall_domain_supports_all_cardinal_wall_axis_directions(endpoint):
    model = _built_wall(end=endpoint)
    domain = derive_wall_cavity_route_domain(
        model, "assembly:wall-route-proof", nominal_diameter_m=0.02,
    )
    assert len(domain.volumes) == 12
    assert all(
        (volume.size.x < volume.size.y) == (endpoint.x == 0.0)
        for volume in domain.volumes
    )


def test_wall_domain_fails_closed_for_unsupported_inputs_and_finite_limits():
    opening_model = synthetic_two_opening_wall_assembly()
    with pytest.raises(RoutingError, match="does not support walls with openings"):
        derive_wall_cavity_route_domain(
            opening_model, opening_model.building_assemblies[0].id,
            nominal_diameter_m=0.02,
        )

    diagonal = _built_wall(end=Point3(x=3.3941125497, y=3.3941125497, z=0.0))
    with pytest.raises(RoutingError, match="cardinal-axis"):
        derive_wall_cavity_route_domain(
            diagonal, diagonal.building_assemblies[0].id, nominal_diameter_m=0.02,
        )

    model = _built_wall()
    with pytest.raises(RoutingError, match="no positive swept-clearance volume"):
        derive_wall_cavity_route_domain(
            model, model.building_assemblies[0].id, nominal_diameter_m=0.1,
        )

    long_wall = _built_wall(end=Point3(x=54.0, y=0.0, z=0.0))
    with pytest.raises(RoutingError, match="bounded bay limit"):
        derive_wall_cavity_route_domain(
            long_wall, long_wall.building_assemblies[0].id, nominal_diameter_m=0.02,
        )
