"""Issue223: bounded source alignment, preserving ports and actual lengths."""

from dataclasses import replace
import math
import pytest
from oabm.model import (
    BuildingModel,
    ElectricalEquipment,
    ElectricalDevice,
    Port,
    Pose,
    Point3,
    Vector3,
    Polyline3D,
    validate_model,
    Obstacle,
    Box3D,
    Size3,
)
from oabm.routing import BundleHints, RoutingOptions, route_between_ports
from oabm.ifc import round_trip


def model():
    panel = ElectricalEquipment(
        id="equipment:panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0, y=0, z=2.7)),
    )
    load = ElectricalDevice(
        id="device:load",
        device_type="luminaire",
        pose=Pose(position=Point3(x=1, y=0, z=2.7)),
    )
    ports = tuple(
        Port(
            id=name,
            owner_id=owner,
            domain="power",
            role=role,
            pose=Pose(position=position),
            direction=Vector3(x=0, y=0, z=1),
            nominal_diameter_m=0.021,
        )
        for name, owner, role, position in [
            ("port:panel", panel.id, "source", panel.pose.position),
            ("port:load", load.id, "sink", load.pose.position),
        ]
    )
    return BuildingModel(
        model_id="model:alignment",
        name="Synthetic alignment",
        electrical_equipment=(panel,),
        electrical_devices=(load,),
        ports=ports,
    )


def hint(offset=0.001, reverse=False, route_type="emt"):
    points = (Point3(x=0, y=offset, z=2.85), Point3(x=1, y=offset, z=2.85))
    return BundleHints(
        paths=(Polyline3D(points=points[::-1] if reverse else points),),
        route_type=route_type,
    )


def length(route):
    return sum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(route.centerline.points, route.centerline.points[1:])
    )


def test_millimetre_offset_snaps_exactly_and_preserves_port_stubs():
    source = model()
    r, f = route_between_ports(
        source, "port:panel", "port:load", "emt", bundle_hints=hint()
    )
    points = r.centerline.points
    assert points[:2] == (Point3(x=0, y=0, z=2.7), Point3(x=0, y=0, z=2.85))
    assert points[-2:] == (Point3(x=1, y=0, z=2.85), Point3(x=1, y=0, z=2.7))
    assert any(
        a.y == b.y == 0.001 and abs(a.x - b.x) == 1 for a, b in zip(points, points[1:])
    )
    assert r.attributes["length_m"] == pytest.approx(1.302)
    assert r.attributes["length_m"] == pytest.approx(length(r))
    final = replace(source, routes=(r,), route_fittings=f)
    assert not validate_model(final)
    assert round_trip(final).to_dict() == final.to_dict()
    assert route_between_ports(
        source, "port:panel", "port:load", "emt", bundle_hints=hint()
    ) == (r, f)


def test_hint_direction_does_not_change_output():
    assert route_between_ports(
        model(), "port:panel", "port:load", "emt", bundle_hints=hint()
    ) == route_between_ports(
        model(), "port:panel", "port:load", "emt", bundle_hints=hint(reverse=True)
    )


def test_other_route_type_is_not_aligned():
    r, _ = route_between_ports(
        model(),
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=hint(route_type="cable-tray"),
    )
    assert all(p.y == 0 for p in r.centerline.points)


def test_outside_alignment_tolerance_stays_separate_and_explicit():
    r, _ = route_between_ports(
        model(),
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=hint(offset=0.02),
        options=RoutingOptions(bundle_alignment_tolerance_m=0.005),
    )
    assert all(p.y == 0 for p in r.centerline.points)
    assert r.attributes["bundle_alignment"]["status"] == "unchanged"
    assert r.attributes["bundle_alignment"]["reason"] == "outside_tolerance"


def test_snap_never_crosses_expanded_obstacle_clearance():
    barrier = Obstacle(
        id="obstacle:barrier",
        name="Synthetic barrier",
        geometry=Box3D(
            pose=Pose(position=Point3(x=0.5, y=0.02, z=2.85)),
            size=Size3(x=0.1, y=0.018, z=0.1),
        ),
    )
    source = replace(model(), obstacles=(barrier,))
    r, _ = route_between_ports(
        source, "port:panel", "port:load", "emt", bundle_hints=hint()
    )
    assert all(p.y == 0 for p in r.centerline.points)
    assert r.attributes["bundle_alignment"]["reason"] == "obstacle_clearance"


def test_reverse_traversal_preserves_connectivity_and_length():
    source = model()
    forward, _ = route_between_ports(
        source, "port:panel", "port:load", "emt", bundle_hints=hint()
    )
    reverse, _ = route_between_ports(
        source, "port:load", "port:panel", "emt", bundle_hints=hint()
    )
    assert reverse.centerline.points == tuple(reversed(forward.centerline.points))
    assert reverse.attributes["length_m"] == forward.attributes["length_m"]


def test_mid_corridor_join_keeps_exact_shared_span_and_connectors():
    source = model()
    path = Polyline3D(
        points=(Point3(x=-1, y=0.001, z=2.85), Point3(x=0.5, y=0.001, z=2.85))
    )
    r, _ = route_between_ports(
        source,
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=BundleHints(paths=(path,), route_type="emt"),
    )
    assert r.attributes["bundle_hint_shared_m"] == 0.5
    assert r.attributes["length_m"] == pytest.approx(length(r))
    assert Point3(x=0.5, y=0.001, z=2.85) in r.centerline.points
    assert Point3(x=0.5, y=0, z=2.85) in r.centerline.points
    assert r.centerline.points[0] == source.ports[0].pose.position
    assert r.centerline.points[-1] == source.ports[1].pose.position


def test_bend_limit_refuses_alignment_without_losing_valid_route():
    r, _ = route_between_ports(
        model(),
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=hint(),
        options=RoutingOptions(max_bends=2),
    )
    assert r.attributes["bend_count"] == 2
    assert r.attributes["bundle_alignment"]["reason"] == "bend_limit"


def test_untyped_legacy_hints_keep_their_exact_behavior():
    typed = hint()
    untyped = BundleHints(paths=typed.paths)
    r, _ = route_between_ports(
        model(), "port:panel", "port:load", "emt", bundle_hints=untyped
    )
    assert "bundle_alignment" not in r.attributes
    assert all(p.y == 0 for p in r.centerline.points)


def test_conductor_takeoff_includes_alignment_connectors():
    from oabm.model import Circuit, Conductor
    from oabm.quantities import extract_quantities

    source = model()
    route, fittings = route_between_ports(
        source, "port:panel", "port:load", "emt", bundle_hints=hint()
    )
    circuit = Circuit(
        id="circuit:one",
        source_port_id="port:panel",
        load_port_ids=("port:load",),
        route_ids=(route.id,),
    )
    wire = Conductor(
        id="conductor:one",
        circuit_id=circuit.id,
        role="line",
        route_ids=(route.id,),
        count=2,
    )
    final = replace(
        source,
        routes=(route,),
        route_fittings=fittings,
        circuits=(circuit,),
        conductors=(wire,),
    )
    assert not validate_model(final)
    quantities = extract_quantities(final)
    rows = [row for row in quantities.items if row.category == "conductor_length"]
    assert sum(row.quantity for row in rows) == pytest.approx(2 * 1.302)
    assert round_trip(final).to_dict() == final.to_dict()


@pytest.mark.parametrize("value", [-0.01, float("nan"), float("inf"), True, "0.01"])
def test_invalid_alignment_tolerance_refused(value):
    from oabm.routing import RoutingError

    with pytest.raises(RoutingError):
        RoutingOptions(bundle_alignment_tolerance_m=value)


def test_already_aligned_hint_wins_over_nearby_alternative():
    exact = Polyline3D(points=(Point3(x=0, y=0, z=2.85), Point3(x=1, y=0, z=2.85)))
    for paths in ((exact, *hint().paths), (*hint().paths, exact)):
        r, _ = route_between_ports(
            model(),
            "port:panel",
            "port:load",
            "emt",
            bundle_hints=BundleHints(paths=paths, route_type="emt"),
        )
        assert all(p.y == 0 for p in r.centerline.points)
        assert r.attributes["bundle_hint_shared_m"] == 1
        assert r.attributes["bundle_alignment"]["status"] == "unchanged"


def test_required_corridor_survives_alignment_attempt():
    from oabm.model import RouteConstraint

    source = model()
    required = RouteConstraint(
        id="constraint:required",
        constraint_type="required-corridor",
        geometry=Box3D(
            pose=Pose(position=Point3(x=0.5, y=0, z=2.85)),
            size=Size3(x=0.001, y=0.00001, z=0.00001),
        ),
    )
    source = replace(
        source,
        ports=tuple(replace(p, nominal_diameter_m=0.00001) for p in source.ports),
        route_constraints=(required,),
    )
    r, _ = route_between_ports(
        source,
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=hint(),
        options=RoutingOptions(corridor_tolerance_m=0),
    )
    assert all(p.y == 0 for p in r.centerline.points)
    assert r.attributes["bundle_alignment"]["reason"] == "required_corridor"


def test_two_actual_routes_share_exact_trunk_and_consolidate_with_correct_wire_lengths():
    from oabm.model import Circuit, Conductor
    from oabm.routing import consolidate_bundled_routes

    source = model()
    device = ElectricalDevice(
        id="device:near",
        device_type="luminaire",
        pose=Pose(position=Point3(x=1, y=0.001, z=2.7)),
    )
    port = replace(
        source.ports[1], id="port:near", owner_id=device.id, pose=device.pose
    )
    source = replace(
        source,
        electrical_devices=(*source.electrical_devices, device),
        ports=(*source.ports, port),
    )
    first, first_fittings = route_between_ports(
        source, "port:panel", "port:near", "emt"
    )
    second, second_fittings = route_between_ports(
        source,
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=BundleHints(paths=(first.centerline,), route_type="emt"),
    )
    assert second.attributes["bundle_alignment"]["status"] == "aligned"
    assert second.attributes["bundle_hint_shared_m"] >= 1
    circuits = tuple(
        Circuit(
            id=f"circuit:{i}",
            source_port_id="port:panel",
            load_port_ids=(r.end_port_id,),
            route_ids=(r.id,),
        )
        for i, r in enumerate((first, second))
    )
    wires = tuple(
        Conductor(
            id=f"conductor:{i}", circuit_id=c.id, role="line", route_ids=c.route_ids
        )
        for i, c in enumerate(circuits)
    )
    complete = replace(
        source,
        routes=(first, second),
        route_fittings=(*first_fittings, *second_fittings),
        circuits=circuits,
        conductors=wires,
    )
    assert not validate_model(complete)
    # Source alignment has already chosen the shared corridor. Consolidate
    # exact geometry only, keeping distinct device-end stubs separate.
    consolidated = consolidate_bundled_routes(complete, tolerance_m=1e-7).model
    assert not validate_model(consolidated)
    lookup = {r.id: r for r in consolidated.routes}
    for original, wire in zip((first, second), consolidated.conductors):
        assert sum(length(lookup[rid]) for rid in wire.route_ids) == pytest.approx(
            length(original)
        )
    assert round_trip(consolidated).to_dict() == consolidated.to_dict()


def test_two_axis_offset_uses_connected_measured_rectilinear_steps():
    path = Polyline3D(
        points=(Point3(x=0, y=0.003, z=2.854), Point3(x=1, y=0.003, z=2.854))
    )
    route, _ = route_between_ports(
        model(),
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=BundleHints(paths=(path,), route_type="emt"),
    )
    assert route.attributes["bundle_alignment"]["status"] == "aligned"
    assert route.attributes["length_m"] == pytest.approx(1.314)
    assert route.attributes["length_m"] == pytest.approx(length(route))
    for a, b in zip(route.centerline.points, route.centerline.points[1:]):
        assert (
            sum(abs(x - y) > 1e-9 for x, y in zip((a.x, a.y, a.z), (b.x, b.y, b.z)))
            == 1
        )


def test_equidistant_corridors_are_not_arbitrarily_selected():
    paths = (*hint(0.001).paths, *hint(-0.001).paths)
    route, _ = route_between_ports(
        model(),
        "port:panel",
        "port:load",
        "emt",
        bundle_hints=BundleHints(paths=paths, route_type="emt"),
    )
    assert all(p.y == 0 for p in route.centerline.points)
    assert route.attributes["bundle_alignment"]["reason"] == "ambiguous_corridor"
