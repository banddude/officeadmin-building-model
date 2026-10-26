"""Real producer-to-router integration for the bounded #356 ceiling slice."""

from __future__ import annotations

from dataclasses import replace

import pytest

from oabm.building_assemblies.ceiling_fixtures import (
    SYNTHETIC_CEILING_ASSEMBLY_ID,
    SYNTHETIC_CEILING_EDGE_POLICY,
    SYNTHETIC_CEILING_ID,
    SYNTHETIC_DECK_ID,
    synthetic_rectangular_ceiling_source,
)
from oabm.building_assemblies.suspended_ceiling import (
    SuspendedCeilingAssemblyRequest,
    build_suspended_ceiling_assembly,
)
from oabm.model import (
    BuildingModel,
    ContractError,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Port,
    Pose,
    Vector3,
    WireSweep,
    compute_assembly_output_digest,
)
from oabm.routing import (
    NoRouteError,
    RoutingError,
    RoutingOptions,
    derive_suspended_ceiling_route_domain,
    route_between_ports,
)
from oabm.routing.router import (
    _assembly_component_vertical_bounds,
    _allowed_route_domain_request,
    _collect_routing_geometry,
    _rule_intersects_segment,
    _segment_clear,
)


_DIAMETER_M = 0.02
_OPTIONS = RoutingOptions(port_stub_m=0.05, clearance_m=0.005)


def _build_source_assembly(*, width_m: float = 1.8, height_m: float = 1.8) -> BuildingModel:
    source = synthetic_rectangular_ceiling_source(width_m=width_m, height_m=height_m)
    origin = source.ceilings[0].footprint.points[0]
    return build_suspended_ceiling_assembly(
        source,
        SuspendedCeilingAssemblyRequest(
            assembly_id=SYNTHETIC_CEILING_ASSEMBLY_ID,
            ceiling_id=SYNTHETIC_CEILING_ID,
            deck_id=SYNTHETIC_DECK_ID,
            selected_by="agent:synthetic-ceiling-router-proof",
            grid_origin_x_m=origin.x,
            grid_origin_y_m=origin.y,
            grid_axis_angle_rad=0.0,
            edge_policy=SYNTHETIC_CEILING_EDGE_POLICY,
        ),
    )


def _with_synthetic_ports(model: BuildingModel, y: float, z: float) -> BuildingModel:
    level_id = model.ceilings[0].level_id
    x_min = min(point.x for point in model.ceilings[0].footprint.points)
    x_max = max(point.x for point in model.ceilings[0].footprint.points)
    source_position = Point3(x=x_min + 0.1, y=y, z=z)
    load_position = Point3(x=x_max - 0.1, y=y, z=z)
    source = ElectricalEquipment(
        id="equipment:plenum-source", equipment_type="panelboard",
        pose=Pose(position=source_position), level_id=level_id,
    )
    load = ElectricalDevice(
        id="device:plenum-load", device_type="luminaire",
        pose=Pose(position=load_position), level_id=level_id,
    )
    return replace(
        model,
        electrical_equipment=(*model.electrical_equipment, source),
        electrical_devices=(*model.electrical_devices, load),
        ports=(
            *model.ports,
            Port(
                id="port:plenum-source", owner_id=source.id, domain="electrical",
                role="source", pose=Pose(position=source_position),
                direction=Vector3(x=1, y=0, z=0), nominal_diameter_m=_DIAMETER_M,
            ),
            Port(
                id="port:plenum-load", owner_id=load.id, domain="electrical",
                role="load", pose=Pose(position=load_position),
                direction=Vector3(x=-1, y=0, z=0), nominal_diameter_m=_DIAMETER_M,
            ),
        ),
    )


def _wire_point(component):
    assert isinstance(component.geometry, WireSweep)
    first, second = component.geometry.centerline.points
    return Point3(x=first.x, y=first.y, z=(first.z + second.z) / 2.0)


def _expanded_domain_bounds(domain, padding: float):
    box = domain.volumes[0]
    half = (box.size.x / 2, box.size.y / 2, box.size.z / 2)
    center = box.pose.position
    return (
        (center.x - half[0] + padding, center.y - half[1] + padding, center.z - half[2] + padding),
        (center.x + half[0] - padding, center.y + half[1] - padding, center.z + half[2] - padding),
    )


def _move_hanger_within_same_runner(component, delta_y: float):
    assert isinstance(component.geometry, WireSweep)
    assert component.hanger_attachment is not None
    centerline = replace(
        component.geometry.centerline,
        points=tuple(
            replace(point, y=point.y + delta_y)
            for point in component.geometry.centerline.points
        ),
    )
    geometry = replace(component.geometry, centerline=centerline)
    attachment = replace(
        component.hanger_attachment,
        top_anchor=replace(component.hanger_attachment.top_anchor,
                           y=component.hanger_attachment.top_anchor.y + delta_y),
        bottom_anchor=replace(component.hanger_attachment.bottom_anchor,
                              y=component.hanger_attachment.bottom_anchor.y + delta_y),
    )
    return replace(component, geometry=geometry, hanger_attachment=attachment)


def test_real_suspended_ceiling_producer_routes_through_receipted_plenum_and_detours_from_generated_hanger():
    assembly_model = _build_source_assembly(width_m=1.8, height_m=1.8)
    assert len(assembly_model.building_assemblies) == 1
    hangers = sorted(
        (row for row in assembly_model.assembly_components if row.component_kind == "hanger_wire"),
        key=lambda row: (row.geometry.centerline.points[0].x,
                         row.geometry.centerline.points[0].y),
    )
    direct_hanger = next(
        row for row in hangers
        if 0.15 < row.geometry.centerline.points[0].x < 1.65
    )
    hanger_point = _wire_point(direct_hanger)
    model = _with_synthetic_ports(assembly_model, y=hanger_point.y, z=2.4)
    assignment = model.building_assemblies[0]
    components = {row.id: row for row in model.assembly_components}
    direct_hanger = components[direct_hanger.id]

    domain = derive_suspended_ceiling_route_domain(
        model, assignment.id, nominal_diameter_m=_DIAMETER_M, options=_OPTIONS,
    )
    second_domain = derive_suspended_ceiling_route_domain(
        model, assignment.id, nominal_diameter_m=_DIAMETER_M, options=_OPTIONS,
    )
    assert domain == second_domain
    box = domain.volumes[0]
    assert box.pose.position.z - box.size.z / 2 == pytest.approx(
        max(
            _assembly_component_vertical_bounds(row)[1]
            for row in model.assembly_components
            if row.component_kind in {"tbar_main_runner", "tbar_cross_runner", "tile"}
        ), abs=1e-9,
    )
    deck = next(row for row in model.slabs if row.id == assignment.deck_id)
    assert box.pose.position.z + box.size.z / 2 == pytest.approx(
        deck.footprint.points[0].z - deck.thickness_m, abs=1e-9,
    )

    first = route_between_ports(
        model, "port:plenum-source", "port:plenum-load", "emt",
        nominal_diameter_m=_DIAMETER_M, options=_OPTIONS, allowed_domain=domain,
    )
    second = route_between_ports(
        model, "port:plenum-source", "port:plenum-load", "emt",
        nominal_diameter_m=_DIAMETER_M, options=_OPTIONS, allowed_domain=domain,
    )
    assert first == second
    route, fittings = first

    start = model.ports[-2].pose.position
    end = model.ports[-1].pose.position
    start_anchor = replace(start, x=start.x + _OPTIONS.port_stub_m)
    end_anchor = replace(end, x=end.x - _OPTIONS.port_stub_m)
    route_z = start.z
    wire_start, wire_end = direct_hanger.geometry.centerline.points
    assert min(wire_start.z, wire_end.z) < route_z < max(wire_start.z, wire_end.z)
    assert start_anchor.x < wire_start.x < end_anchor.x

    geometry = _collect_routing_geometry(
        model, "emt", _DIAMETER_M / 2.0, _OPTIONS,
        allowed_domain=domain,
    )
    hanger_rule = next(row for row in geometry.hard_blockers if row.id == direct_hanger.id)
    assert _rule_intersects_segment(start_anchor, end_anchor, hanger_rule), (
        "the synthetic direct chord must cross this generated hanger"
    )
    assert any(
        abs(point.y - hanger_point.y) > 1e-6 or abs(point.z - route_z) > 1e-6
        for point in route.centerline.points[1:-1]
    ), "the returned route should detour from the direct hanger crossing"
    for first_point, second_point in zip(
        route.centerline.points, route.centerline.points[1:]
    ):
        assert _segment_clear(first_point, second_point, geometry.hard_blockers)
        lower, upper = _expanded_domain_bounds(
            domain, _DIAMETER_M / 2.0 + _OPTIONS.clearance_m,
        )
        assert min(first_point.x, second_point.x) >= lower[0] - 1e-9
        assert min(first_point.y, second_point.y) >= lower[1] - 1e-9
        assert min(first_point.z, second_point.z) >= lower[2] - 1e-9
        assert max(first_point.x, second_point.x) <= upper[0] + 1e-9
        assert max(first_point.y, second_point.y) <= upper[1] + 1e-9
        assert max(first_point.z, second_point.z) <= upper[2] + 1e-9

    runner = next(row for row in model.assembly_components if row.component_kind == "tbar_main_runner")
    tile = next(row for row in model.assembly_components if row.component_kind == "tile")
    checks = route.attributes["geometry_check"]
    expected_blockers = {row.id for row in model.assembly_components} | {deck.id}
    assert set(checks["checked_blocker_ids"]) == expected_blockers
    assert {direct_hanger.id, runner.id, tile.id, deck.id} <= set(checks["checked_blocker_ids"])
    assert checks["status"] == "contained_and_clear_of_checked_blockers"
    assert checks["code_review"] == "not_evaluated"

    primary = next(
        row for row in route.provenance
        if row.source_kind == "router" and row.method == route.attributes["routing_engine"]
    )
    bounded = primary.attributes["bounded_routing_context"]
    assembly_receipt = bounded["assembly_routing_context"]
    assert bounded["allowed_route_domain"] == _allowed_route_domain_request(domain)
    assert assembly_receipt["checked_component_ids"] == sorted(components)
    assert assembly_receipt["checked_deck_ids"] == [SYNTHETIC_DECK_ID]
    assert assembly_receipt["checked_deck_kinds"] == [
        {"deck_id": SYNTHETIC_DECK_ID, "kind": "existing_slab"}
    ]
    assert assembly_receipt["assignment_digests"] == [{
        "assembly_id": assignment.id,
        "assembly_kind": "suspended_ceiling",
        "host_id": SYNTHETIC_CEILING_ID,
        "input_digest": assignment.input_digest,
        "output_digest": assignment.output_digest,
    }]
    assert checks["assembly_routing_context"] == assembly_receipt
    assert "SelectedSlabDeck" in checks["checked_blocker_classes"]
    assert "AssemblyComponentWire" in checks["checked_blocker_classes"]
    assert "AssemblyComponentProfileEnvelope" in checks["checked_blocker_classes"]
    assert "AssemblyComponentPanel" in checks["checked_blocker_classes"]
    assert not fittings or all(fitting.route_id == route.id for fitting in fittings)

    routed_model = replace(model, routes=(route,), route_fittings=fittings)
    moved_hanger = _move_hanger_within_same_runner(direct_hanger, delta_y=0.001)
    edited_components = tuple(
        moved_hanger if row.id == direct_hanger.id else row
        for row in model.assembly_components
    )
    edited_assignment = replace(
        assignment,
        output_digest=compute_assembly_output_digest(edited_components),
    )
    edited_model = replace(
        model,
        assembly_components=edited_components,
        building_assemblies=(edited_assignment,),
    )
    with pytest.raises(RoutingError, match="derived suspended-ceiling route domain is stale"):
        route_between_ports(
            edited_model, "port:plenum-source", "port:plenum-load", "emt",
            nominal_diameter_m=_DIAMETER_M, options=_OPTIONS, allowed_domain=domain,
        )
    with pytest.raises(ContractError, match="stale assembly routing context"):
        replace(
            routed_model,
            assembly_components=edited_components,
            building_assemblies=(edited_assignment,),
        )


def test_derived_ceiling_domain_refuses_nonpositive_swept_clearance_and_impossible_routes():
    assembly_model = _build_source_assembly(width_m=1.8, height_m=1.8)
    hanger = next(
        row for row in assembly_model.assembly_components
        if row.component_kind == "hanger_wire"
    )
    hanger_point = _wire_point(hanger)
    model = _with_synthetic_ports(assembly_model, y=hanger_point.y, z=2.4)
    assignment = model.building_assemblies[0]
    with pytest.raises(RoutingError, match="no positive swept-clearance volume"):
        derive_suspended_ceiling_route_domain(
            model, assignment.id, nominal_diameter_m=1.0,
            options=RoutingOptions(clearance_m=0.0),
        )

    domain = derive_suspended_ceiling_route_domain(
        model, assignment.id, nominal_diameter_m=_DIAMETER_M, options=_OPTIONS,
    )
    blocked_ports = list(model.ports)
    blocked_ports[-2] = replace(blocked_ports[-2], pose=Pose(position=hanger_point))
    blocked = replace(model, ports=tuple(blocked_ports))
    with pytest.raises(NoRouteError) as failure:
        route_between_ports(
            blocked, "port:plenum-source", "port:plenum-load", "emt",
            nominal_diameter_m=_DIAMETER_M, options=_OPTIONS, allowed_domain=domain,
        )
    assert failure.value.reason == "start_endpoint_inside_hard_blocker"
    assert hanger.id in failure.value.blocker_ids
