from dataclasses import replace

import pytest

from oabm.model import (
    Box3D, BuildingModel, Ceiling, ElectricalDevice, ElectricalEquipment, Obstacle,
    Point3, Polygon3D, Polyline3D, Port, Pose, RouteConstraint, Size3, Vector3,
    stable_id,
)
import oabm.routing.router as routing_router
from oabm.routing import NoRouteError, RoutingError, RoutingOptions, route_between_ports


def _box(x, y, z, sx, sy, sz):
    return Box3D(pose=Pose(position=Point3(x=x, y=y, z=z)), size=Size3(x=sx, y=sy, z=sz))


def _base_model(
    *,
    start=Point3(x=0, y=0, z=0),
    end=Point3(x=4, y=0, z=0),
    start_direction=Vector3(x=1, y=0, z=0),
    end_direction=Vector3(x=-1, y=0, z=0),
    obstacles=(), constraints=(), ceilings=(),
):
    source = ElectricalEquipment(id="equipment:source", equipment_type="panelboard", pose=Pose(position=start))
    load = ElectricalDevice(id="device:load", device_type="load", pose=Pose(position=end))
    ports = (
        Port(id="port:source", owner_id=source.id, domain="power", role="source", pose=Pose(position=start), direction=start_direction, nominal_diameter_m=0.021),
        Port(id="port:load", owner_id=load.id, domain="power", role="sink", pose=Pose(position=end), direction=end_direction, nominal_diameter_m=0.021),
    )
    return BuildingModel(
        model_id="model:routing-test",
        electrical_equipment=(source,), electrical_devices=(load,), ports=ports,
        obstacles=tuple(obstacles), route_constraints=tuple(constraints), ceilings=tuple(ceilings),
    )


def _points(route):
    return tuple((p.x, p.y, p.z) for p in route.centerline.points)


def _segment_hits_box(a, b, box):
    bounds = (
        box.pose.position.x - box.size.x / 2,
        box.pose.position.y - box.size.y / 2,
        box.pose.position.z - box.size.z / 2,
        box.pose.position.x + box.size.x / 2,
        box.pose.position.y + box.size.y / 2,
        box.pose.position.z + box.size.z / 2,
    )
    t0, t1 = 0.0, 1.0
    for s, e, lo, hi in ((a.x,b.x,bounds[0],bounds[3]),(a.y,b.y,bounds[1],bounds[4]),(a.z,b.z,bounds[2],bounds[5])):
        d = e - s
        if abs(d) < 1e-9:
            if s < lo or s > hi:
                return False
            continue
        u0, u1 = (lo-s)/d, (hi-s)/d
        if u0 > u1:
            u0, u1 = u1, u0
        t0, t1 = max(t0,u0), min(t1,u1)
        if t0 > t1:
            return False
    return True


def test_normal_route_emits_canonical_route_with_stable_provenance():
    model = _base_model()
    route, fittings = route_between_ports(model, "port:source", "port:load", "emt")
    assert _points(route) == ((0,0,0),(4,0,0))
    assert fittings == ()
    assert route.attributes["bend_count"] == 0
    assert route.provenance[0].source_kind == "router"
    assert route.provenance[0].method == "deterministic-rectilinear-v1"
    attached = replace(model, routes=(route,), route_fittings=fittings)
    assert attached.routes[0] == route


def test_port_directions_and_elevation_changes_create_ordered_fittings():
    model = _base_model(
        start=Point3(x=0,y=0,z=1), end=Point3(x=4,y=0,z=1),
        start_direction=Vector3(x=0,y=0,z=1), end_direction=Vector3(x=0,y=0,z=1),
    )
    route, fittings = route_between_ports(model, "port:source", "port:load", "emt")
    assert route.centerline.points[0] == model.ports[0].pose.position
    assert route.centerline.points[-1] == model.ports[1].pose.position
    assert route.centerline.points[1].z > 1
    assert route.centerline.points[-2].z > 1
    assert len(fittings) == 2
    assert all(item.fitting_type == "elbow-90" for item in fittings)
    assert route.fitting_ids == tuple(item.id for item in fittings)


def test_hard_obstacle_forces_an_alternate_route_without_intersection():
    geometry = _box(2,0,0,0.8,0.8,0.8)
    model = _base_model(obstacles=(Obstacle(id="obstacle:block", geometry=geometry, clearance_m=0.05),))
    route, fittings = route_between_ports(model, "port:source", "port:load", "emt")
    assert len(route.centerline.points) >= 4
    assert len(fittings) >= 2
    assert any(abs(p.y) > 0.4 or abs(p.z) > 0.4 for p in route.centerline.points[1:-1])
    assert not any(_segment_hits_box(a,b,geometry) for a,b in zip(route.centerline.points, route.centerline.points[1:]))


def test_impossible_endpoint_inside_hard_obstacle_fails_explicitly():
    model = _base_model(obstacles=(Obstacle(id="obstacle:sealed", geometry=_box(0,0,0,0.5,0.5,0.5)),))
    with pytest.raises(NoRouteError, match="endpoint lies inside"):
        route_between_ports(model, "port:source", "port:load", "emt")


def test_required_corridor_is_visited_and_can_make_route_impossible():
    required = RouteConstraint(id="constraint:required", constraint_type="required-corridor", hard=True, geometry=_box(2,1,0,0.5,0.3,0.3))
    model = _base_model(constraints=(required,))
    route, _ = route_between_ports(model, "port:source", "port:load", "emt")
    assert any(p.y > 0.7 for p in route.centerline.points)
    assert route.attributes["required_constraint_ids"] == [required.id]
    covering = Obstacle(id="obstacle:covers-required", geometry=_box(2,1,0,2,2,2))
    impossible = _base_model(obstacles=(covering,), constraints=(required,))
    with pytest.raises(NoRouteError, match="required corridors"):
        route_between_ports(impossible, "port:source", "port:load", "emt")


def test_bend_limit_is_enforced():
    required = RouteConstraint(id="constraint:detour", constraint_type="required-corridor", hard=True, geometry=_box(2,1,0,0.5,0.3,0.3))
    model = _base_model(constraints=(required,))
    with pytest.raises(NoRouteError, match="max_bends=1"):
        route_between_ports(model, "port:source", "port:load", "emt", options=RoutingOptions(max_bends=1))


def test_preferred_corridor_affects_cost_but_is_not_hard():
    preferred = RouteConstraint(id="constraint:preferred", constraint_type="preferred-corridor", hard=False, geometry=_box(2,1,0,3.5,0.2,0.2))
    model = _base_model(constraints=(preferred,))
    route, _ = route_between_ports(model, "port:source", "port:load", "emt", options=RoutingOptions(preferred_corridor_discount=0.8, bend_penalty_m=0.1))
    assert any(p.y > 0.7 for p in route.centerline.points)


def test_constraint_scope_is_respected():
    pvc_only = RouteConstraint(id="constraint:pvc-only", constraint_type="keep-out", hard=True, applies_to=("pvc",), geometry=_box(2,0,0,0.8,0.8,0.8))
    route, _ = route_between_ports(_base_model(constraints=(pvc_only,)), "port:source", "port:load", "emt")
    assert _points(route) == ((0,0,0),(4,0,0))


def test_ceiling_geometry_is_available_as_a_preferred_pathway():
    from oabm.model import Level
    ceiling = Ceiling(
        id="ceiling:path", level_id="level:path",
        footprint=Polygon3D(points=(Point3(x=0,y=-0.5,z=1),Point3(x=4,y=-0.5,z=1),Point3(x=4,y=0.5,z=1),Point3(x=0,y=0.5,z=1))),
    )
    model = _base_model(start_direction=Vector3(x=0,y=0,z=1), end_direction=Vector3(x=0,y=0,z=1))
    model = replace(model, levels=(Level(id="level:path", elevation_m=0, height_m=1),), ceilings=(ceiling,))
    route, _ = route_between_ports(model, "port:source", "port:load", "emt", options=RoutingOptions(surface_path_discount=0.8))
    assert any(abs(p.z-1.0) < 0.06 for p in route.centerline.points)


def test_repeatability_is_identical_across_runs_and_input_ordering():
    obstacles = (
        Obstacle(id="obstacle:b", geometry=_box(2.5,0,0,0.4,0.7,0.7)),
        Obstacle(id="obstacle:a", geometry=_box(1.5,0,0,0.4,0.7,0.7)),
    )
    constraints = (
        RouteConstraint(id="constraint:b", constraint_type="preferred-corridor", hard=False, geometry=_box(2,-1,0,3,0.2,0.2)),
        RouteConstraint(id="constraint:a", constraint_type="avoid", hard=False, geometry=_box(2,1,0,3,0.2,0.2)),
    )
    first_model = _base_model(obstacles=obstacles, constraints=constraints)
    second_model = _base_model(obstacles=tuple(reversed(obstacles)), constraints=tuple(reversed(constraints)))
    first = route_between_ports(first_model, "port:source", "port:load", "emt")
    again = route_between_ports(first_model, "port:source", "port:load", "emt")
    reordered = route_between_ports(second_model, "port:source", "port:load", "emt")
    assert first == again == reordered


def test_unknown_active_constraint_fails_instead_of_being_silently_ignored():
    constraint = RouteConstraint(id="constraint:future", constraint_type="future-rule", hard=True, geometry=_box(2,0,0,1,1,1))
    model = _base_model(constraints=(constraint,))
    with pytest.raises(RoutingError, match="unsupported active route constraint"):
        route_between_ports(model, "port:source", "port:load", "emt")


def test_wall_geometry_is_available_as_a_preferred_pathway():
    from oabm.model import Level, Polyline3D, Wall
    wall = Wall(
        id="wall:path", level_id="level:path",
        centerline=Polyline3D(points=(Point3(x=0,y=1,z=0), Point3(x=4,y=1,z=0))),
        thickness_m=0.1, height_m=1.0,
    )
    model = _base_model()
    model = replace(model, levels=(Level(id="level:path", elevation_m=0, height_m=1),), walls=(wall,))
    route, _ = route_between_ports(
        model, "port:source", "port:load", "emt",
        options=RoutingOptions(surface_path_discount=0.8, bend_penalty_m=0.1),
    )
    assert any(abs(p.y - 1.0) < 0.06 for p in route.centerline.points)


def test_route_and_fitting_identity_stays_stable_when_geometry_moves_but_topology_does_not():
    first = _base_model(
        start=Point3(x=0,y=0,z=1), end=Point3(x=4,y=0,z=1),
        start_direction=Vector3(x=0,y=0,z=1), end_direction=Vector3(x=0,y=0,z=1),
    )
    second = _base_model(
        start=Point3(x=0,y=0,z=1), end=Point3(x=5,y=0,z=1),
        start_direction=Vector3(x=0,y=0,z=1), end_direction=Vector3(x=0,y=0,z=1),
    )
    first_route, first_fittings = route_between_ports(first, "port:source", "port:load", "emt")
    second_route, second_fittings = route_between_ports(second, "port:source", "port:load", "emt")
    assert first_route.id == second_route.id
    assert tuple(item.id for item in first_fittings) == tuple(item.id for item in second_fittings)
    assert first_route.centerline != second_route.centerline




@pytest.mark.parametrize(
    "geometry",
    (
        Polyline3D(
            points=(
                Point3(x=1, y=-1, z=0),
                Point3(x=2, y=0, z=1),
                Point3(x=3, y=1, z=2),
            )
        ),
        Polygon3D(
            points=(
                Point3(x=1, y=-1, z=0),
                Point3(x=3, y=-1, z=0),
                Point3(x=3, y=1, z=2),
                Point3(x=1, y=1, z=2),
            )
        ),
    ),
)
def test_required_polyline_and_polygon_constraints_use_actual_geometry(geometry):
    required = RouteConstraint(
        id="constraint:shape-aware",
        constraint_type="required-corridor",
        hard=True,
        geometry=geometry,
    )
    model = _base_model(constraints=(required,))

    with pytest.raises(NoRouteError, match="required corridors=constraint:shape-aware"):
        route_between_ports(
            model,
            "port:source",
            "port:load",
            "emt",
            options=RoutingOptions(max_bends=0),
        )

    route, _ = route_between_ports(model, "port:source", "port:load", "emt")
    assert route.attributes["required_constraint_ids"] == [required.id]
    assert route.attributes["bend_count"] > 0


def test_route_and_fitting_identity_does_not_depend_on_router_version(monkeypatch):
    model = _base_model(
        start=Point3(x=0, y=0, z=1),
        end=Point3(x=4, y=0, z=1),
        start_direction=Vector3(x=0, y=0, z=1),
        end_direction=Vector3(x=0, y=0, z=1),
    )
    first_route, first_fittings = route_between_ports(
        model,
        "port:source",
        "port:load",
        "emt",
    )
    expected_id = stable_id(
        "route",
        f"{model.model_id}:emt:port:source:port:load",
    )
    assert first_route.id == expected_id

    monkeypatch.setattr(routing_router, "_ALGORITHM", "deterministic-rectilinear-v2")
    second_route, second_fittings = route_between_ports(
        model,
        "port:source",
        "port:load",
        "emt",
    )

    assert second_route.id == first_route.id
    assert tuple(item.id for item in second_fittings) == tuple(item.id for item in first_fittings)
    assert second_route.provenance[0].method == "deterministic-rectilinear-v2"
    assert second_route.attributes["routing_engine"] == "deterministic-rectilinear-v2"


def test_checked_in_routing_fixtures_cover_normal_alternate_and_impossible():
    from pathlib import Path
    from oabm.model import BuildingModel

    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "routing" / "v1"

    normal = BuildingModel.load(fixture_dir / "normal.json")
    route, fittings = route_between_ports(normal, "port:source", "port:load", "emt")
    assert _points(route) == ((0.0, 0.0, 0.0), (4.0, 0.0, 0.0))
    assert fittings == ()

    alternate = BuildingModel.load(fixture_dir / "alternate-obstacle.json")
    alternate_route, alternate_fittings = route_between_ports(alternate, "port:source", "port:load", "emt")
    assert _points(alternate_route) == (
        (0.0, 0.0, 0.0),
        (1.539499, 0.0, 0.0),
        (1.539499, -0.460501, 0.0),
        (3.85, -0.460501, 0.0),
        (3.85, 0.0, 0.0),
        (4.0, 0.0, 0.0),
    )
    assert len(alternate_fittings) == 4

    impossible = BuildingModel.load(fixture_dir / "impossible.json")
    with pytest.raises(NoRouteError, match="required corridors=constraint:required"):
        route_between_ports(impossible, "port:source", "port:load", "emt")
