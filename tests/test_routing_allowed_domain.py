"""Bounded caller-supplied route-volume containment regression tests."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

import oabm.routing.design as design_module
from oabm.model import (
    Box3D,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Obstacle,
    Point3,
    Port,
    Polyline3D,
    Pose,
    Quaternion,
    Size3,
    StructuralMember,
    Vector3,
)
from oabm.routing import (
    AllowedRouteDomain,
    BundleHints,
    CircuitDesignRules,
    NoRouteError,
    PlacementError,
    RoutingError,
    RoutingOptions,
    design_proposed_circuits,
    route_between_ports,
)


def _box_from_bounds(
    min_x: float,
    min_y: float,
    min_z: float,
    max_x: float,
    max_y: float,
    max_z: float,
) -> Box3D:
    return Box3D(
        pose=Pose(position=Point3(
            x=(min_x + max_x) / 2,
            y=(min_y + max_y) / 2,
            z=(min_z + max_z) / 2,
        )),
        size=Size3(x=max_x - min_x, y=max_y - min_y, z=max_z - min_z),
    )


def _domain(*volumes: Box3D, identity: str = "domain:fixture") -> AllowedRouteDomain:
    return AllowedRouteDomain(
        id=identity,
        kind="synthetic-plenum",
        volumes=tuple(volumes),
        source_ids=("assembly:fixture",),
    )


def _model(
    *,
    start: Point3 = Point3(x=0, y=0, z=0),
    end: Point3 = Point3(x=4, y=0, z=0),
    start_direction: Vector3 = Vector3(x=1, y=0, z=0),
    end_direction: Vector3 = Vector3(x=-1, y=0, z=0),
    obstacles: tuple[Obstacle, ...] = (),
    structural_members: tuple[StructuralMember, ...] = (),
    port_diameter: float | None = 0.021,
) -> BuildingModel:
    source = ElectricalEquipment(
        id="equipment:source", equipment_type="panelboard", pose=Pose(position=start),
    )
    load = ElectricalDevice(
        id="device:load", device_type="load", pose=Pose(position=end),
    )
    ports = (
        Port(
            id="port:source", owner_id=source.id, domain="electrical", role="source",
            pose=Pose(position=start), direction=start_direction,
            nominal_diameter_m=port_diameter,
        ),
        Port(
            id="port:load", owner_id=load.id, domain="electrical", role="load",
            pose=Pose(position=end), direction=end_direction,
            nominal_diameter_m=port_diameter,
        ),
    )
    kwargs = {
        "model_id": "model:allowed-domain-test",
        "electrical_equipment": (source,),
        "electrical_devices": (load,),
        "ports": ports,
        "obstacles": obstacles,
    }
    if structural_members:
        level = Level(id="level:one", elevation_m=0.0, height_m=3.0)
        return BuildingModel(
            **kwargs,
            schema_version="1.4.0",
            levels=(level,),
            structural_members=structural_members,
        )
    return BuildingModel(**kwargs)


def _route_inside_one_box(route, box: Box3D, padding: float) -> None:
    center = box.pose.position
    half = (box.size.x / 2, box.size.y / 2, box.size.z / 2)
    bounds = (
        center.x - half[0], center.y - half[1], center.z - half[2],
        center.x + half[0], center.y + half[1], center.z + half[2],
    )
    for start, end in zip(route.centerline.points, route.centerline.points[1:]):
        assert min(start.x, end.x) - padding >= bounds[0] - 1e-9
        assert min(start.y, end.y) - padding >= bounds[1] - 1e-9
        assert min(start.z, end.z) - padding >= bounds[2] - 1e-9
        assert max(start.x, end.x) + padding <= bounds[3] + 1e-9
        assert max(start.y, end.y) + padding <= bounds[4] + 1e-9
        assert max(start.z, end.z) + padding <= bounds[5] + 1e-9


def test_domain_is_an_opt_in_hard_fence_and_records_exact_geometry_only_scope():
    barrier = Obstacle(
        id="obstacle:plenum-barrier",
        geometry=_box_from_bounds(1.6, -0.4, -0.4, 2.4, 0.4, 0.4),
        clearance_m=0.01,
    )
    source = _model(obstacles=(barrier,))
    before = source.to_json()
    options = RoutingOptions(port_stub_m=0.0, search_margin_m=0.25)

    unbounded, _ = route_between_ports(
        source, "port:source", "port:load", "emt", options=options,
    )
    assert any(abs(point.y) > 0.2 or abs(point.z) > 0.2 for point in unbounded.centerline.points)

    volume = _box_from_bounds(-0.2, -0.2, -0.2, 4.2, 0.2, 0.2)
    domain = _domain(volume, identity="domain:bounded-plenum")
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            source, "port:source", "port:load", "emt",
            options=options, allowed_domain=domain,
        )
    assert caught.value.reason == "no_path_within_allowed_domain"
    assert caught.value.blocker_ids == (barrier.id,)
    assert source.to_json() == before

    # No-domain callers keep the exact legacy result and do not gain metadata.
    no_option = route_between_ports(source, "port:source", "port:load", "emt")
    explicit_none = route_between_ports(
        source, "port:source", "port:load", "emt", allowed_domain=None,
    )
    assert no_option == explicit_none
    assert "allowed_route_domain" not in no_option[0].provenance[0].attributes


def test_connected_box_union_covers_a_seam_but_endpoints_cannot_bridge_a_gap():
    source = _model()
    options = RoutingOptions(port_stub_m=0.0)
    left = _box_from_bounds(-0.2, -1, -1, 2.0, 1, 1)
    right = _box_from_bounds(2.0, -1, -1, 4.2, 1, 1)
    domain = _domain(right, left)  # caller order must not affect the receipt
    route, _ = route_between_ports(
        source, "port:source", "port:load", "emt",
        options=options, allowed_domain=domain,
    )
    assert route.centerline.points == (Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0))
    assert route.provenance[0].attributes["allowed_route_domain"]["volumes"][0]["pose"]["position"]["x"] == 0.9
    assert route.attributes["geometry_check"]["status"] == "contained_and_clear_of_checked_blockers"
    assert route.attributes["geometry_check"]["checked_blocker_ids"] == []
    assert route.attributes["geometry_check"]["code_review"] == "not_evaluated"
    assert route.attributes["geometry_check"]["revalidation"] == "generation_time_only; not_live_after_model_changes"

    gapped = _domain(
        _box_from_bounds(-0.2, -1, -1, 1.8, 1, 1),
        _box_from_bounds(2.2, -1, -1, 4.2, 1, 1),
    )
    # Both endpoint points are inside separate volumes, but the middle sweep
    # crosses the gap and is not licensed by endpoint membership.
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            source, "port:source", "port:load", "emt",
            options=options, allowed_domain=gapped,
        )
    assert caught.value.reason == "no_path_within_allowed_domain"


def test_route_sweep_width_and_port_stubs_must_fit_the_domain():
    source = _model()
    too_narrow = _domain(_box_from_bounds(-0.2, -0.05, -0.2, 4.2, 0.05, 0.2))
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            source, "port:source", "port:load", "emt",
            nominal_diameter_m=0.12,
            options=RoutingOptions(port_stub_m=0.0),
            allowed_domain=too_narrow,
        )
    assert caught.value.reason == "start_outside_allowed_domain"

    stub_model = _model(
        start=Point3(x=0.05, y=0, z=0),
        start_direction=Vector3(x=-1, y=0, z=0),
        end=Point3(x=4, y=0, z=0),
    )
    domain = _domain(_box_from_bounds(0, -0.5, -0.5, 4.5, 0.5, 0.5))
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            stub_model, "port:source", "port:load", "emt",
            allowed_domain=domain,
        )
    assert caught.value.reason == "start_stub_outside_allowed_domain"


def test_allowed_domain_requires_a_known_positive_nominal_diameter():
    source = _model(port_diameter=None)
    domain = _domain(_box_from_bounds(-0.2, -0.5, -0.5, 4.2, 0.5, 0.5))
    with pytest.raises(RoutingError, match="known positive nominal diameter"):
        route_between_ports(
            source, "port:source", "port:load", "emt",
            options=RoutingOptions(port_stub_m=0.0),
            allowed_domain=domain,
        )
    # The legacy unbounded route keeps its established unknown-diameter behavior.
    route_between_ports(
        source, "port:source", "port:load", "emt",
        options=RoutingOptions(port_stub_m=0.0),
    )


@pytest.mark.parametrize(
    "blocked_point, reason",
    (
        (Point3(x=0, y=0, z=0), "start_endpoint_inside_hard_blocker"),
        (Point3(x=4, y=0, z=0), "end_endpoint_inside_hard_blocker"),
    ),
)
def test_domain_mode_endpoint_collision_reports_exact_blocker(blocked_point, reason):
    blocker = Obstacle(
        id="obstacle:endpoint",
        geometry=Box3D(
            pose=Pose(position=blocked_point),
            size=Size3(x=0.4, y=0.4, z=0.4),
        ),
    )
    source = _model(obstacles=(blocker,))
    domain = _domain(_box_from_bounds(-0.5, -1, -1, 4.5, 1, 1))
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            source, "port:source", "port:load", "emt",
            allowed_domain=domain,
        )
    assert caught.value.reason == reason
    assert caught.value.blocker_ids == (blocker.id,)
    with pytest.raises(NoRouteError) as legacy:
        route_between_ports(source, "port:source", "port:load", "emt")
    assert str(legacy.value) == "a route endpoint lies inside hard obstacle/no-go geometry"
    assert legacy.value.reason is None and legacy.value.blocker_ids == ()


@pytest.mark.parametrize(
    "endpoint, direction, obstacle_bounds, reason",
    (
        (Point3(x=0, y=0, z=0), Vector3(x=1, y=0, z=0),
         (0.05, -0.1, -0.1, 0.1, 0.1, 0.1), "start_stub_intersects_hard_blocker"),
        (Point3(x=4, y=0, z=0), Vector3(x=-1, y=0, z=0),
         (3.9, -0.1, -0.1, 3.95, 0.1, 0.1), "end_stub_intersects_hard_blocker"),
    ),
)
def test_domain_mode_stub_collision_reports_exact_blocker(
    endpoint, direction, obstacle_bounds, reason,
):
    blocker = Obstacle(
        id="obstacle:stub",
        geometry=_box_from_bounds(*obstacle_bounds),
    )
    source = _model(
        start=endpoint if reason.startswith("start") else Point3(x=0, y=0, z=0),
        end=endpoint if reason.startswith("end") else Point3(x=4, y=0, z=0),
        start_direction=direction if reason.startswith("start") else Vector3(x=1, y=0, z=0),
        end_direction=direction if reason.startswith("end") else Vector3(x=-1, y=0, z=0),
        obstacles=(blocker,),
    )
    domain = _domain(_box_from_bounds(-0.5, -1, -1, 4.5, 1, 1))
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(source, "port:source", "port:load", "emt", allowed_domain=domain)
    assert caught.value.reason == reason
    assert caught.value.blocker_ids == (blocker.id,)
    with pytest.raises(NoRouteError) as legacy:
        route_between_ports(source, "port:source", "port:load", "emt")
    expected = (
        "start port direction stub intersects hard obstacle/no-go geometry"
        if reason.startswith("start")
        else "end port direction stub intersects hard obstacle/no-go geometry"
    )
    assert str(legacy.value) == expected
    assert legacy.value.reason is None and legacy.value.blocker_ids == ()


def test_structural_member_remains_a_hard_blocker_inside_allowed_domain():
    level_id = "level:one"
    member = StructuralMember(
        id="member:barrier", kind="beam", level_id=level_id,
        pose=Pose(position=Point3(x=2.0, y=0.0, z=0.0)),
        size=Size3(x=0.4, y=3.0, z=3.0),
    )
    source = _model(structural_members=(member,))
    domain = _domain(_box_from_bounds(-0.2, -1, -1, 4.2, 1, 1))
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            source, "port:source", "port:load", "emt",
            options=RoutingOptions(port_stub_m=0.0),
            allowed_domain=domain,
        )
    assert caught.value.reason == "no_path_within_allowed_domain"
    assert member.id in caught.value.blocker_ids


def test_bundle_alignment_is_rechecked_against_the_allowed_domain():
    source = _model(
        start=Point3(x=0, y=0, z=2.7),
        end=Point3(x=1, y=0, z=2.7),
        start_direction=Vector3(x=0, y=0, z=1),
        end_direction=Vector3(x=0, y=0, z=1),
    )
    domain = _domain(_box_from_bounds(-0.02, -0.0125, 2.6, 1.02, 0.0125, 3.0))
    hint = BundleHints(
        paths=(
            # The route centerline shift is within bundle tolerance, but the
            # swept route would leave the caller's narrow plenum volume.
            Polyline3D(points=(
                Point3(x=0, y=0.015, z=2.85), Point3(x=1, y=0.015, z=2.85),
            )),
        ),
        route_type="emt",
    )
    route, _ = route_between_ports(
        source, "port:source", "port:load", "emt",
        bundle_hints=hint,
        options=RoutingOptions(bundle_alignment_tolerance_m=0.02),
        allowed_domain=domain,
    )
    assert all(abs(point.y) <= 1e-9 for point in route.centerline.points)
    assert route.attributes["bundle_alignment"]["status"] == "unchanged"
    assert route.attributes["bundle_alignment"]["reason"] == "allowed_route_domain"
    _route_inside_one_box(route, domain.volumes[0], padding=0.0105)


def test_explicit_design_passes_the_same_domain_to_every_leg_without_mutating_input(monkeypatch):
    panel = ElectricalEquipment(
        id="equipment:reviewed-panel", equipment_type="panelboard",
        pose=Pose(position=Point3(x=0, y=0, z=0)),
    )
    devices = tuple(
        ElectricalDevice(
            id=identity, device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=x, y=0, z=0)),
        )
        for identity, x in (("device:a", 2.0), ("device:b", 4.0))
    )

    def port(identity: str, owner: str, role: str, x: float, direction: float) -> Port:
        return Port(
            id=identity, owner_id=owner, domain="electrical", role=role,
            pose=Pose(position=Point3(x=x, y=0, z=0)),
            direction=Vector3(x=direction, y=0, z=0),
        )

    source_port = port("port:panel", panel.id, "source", 0.0, 1.0)
    load_ports = (
        port("port:device:a", devices[0].id, "load", 2.0, -1.0),
        port("port:device:b", devices[1].id, "load", 4.0, -1.0),
    )
    barrier = Obstacle(
        id="obstacle:between-loads",
        geometry=_box_from_bounds(2.75, -0.4, -0.4, 3.25, 0.4, 0.4),
    )
    source_model = BuildingModel(
        model_id="model:explicit-domain-design",
        electrical_equipment=(panel,), electrical_devices=devices,
        ports=(source_port, *load_ports), obstacles=(barrier,),
    )
    before = source_model.to_json()
    domain = _domain(_box_from_bounds(-0.2, -0.25, -0.25, 4.2, 0.25, 0.25))
    rules = CircuitDesignRules(
        max_devices_per_circuit=2, route_type="emt", nominal_diameter_m=0.0254,
    )
    routed_domains = []
    route_leg = design_module.route_between_ports

    def record_domain(*args, **kwargs):
        routed_domains.append(kwargs.get("allowed_domain"))
        return route_leg(*args, **kwargs)

    monkeypatch.setattr(design_module, "route_between_ports", record_domain)
    with pytest.raises(NoRouteError) as caught:
        design_proposed_circuits(
            source_model,
            equipment_id=panel.id,
            device_ids=tuple(item.id for item in devices),
            rules=rules,
            user_groups=((devices[0].id, devices[1].id),),
            user_input_id="decision:explicit-domain",
            source_port_id=source_port.id,
            load_port_ids={item.id: port.id for item, port in zip(devices, load_ports)},
            routing_options=RoutingOptions(port_stub_m=0.0),
            allowed_domain=domain,
        )
    assert caught.value.reason == "no_path_within_allowed_domain"
    assert routed_domains == [domain, domain]
    assert source_model.to_json() == before


def _reviewed_design(
    model: BuildingModel,
    domain: AllowedRouteDomain | None,
    routing_options: RoutingOptions | None = None,
):
    return design_proposed_circuits(
        model,
        equipment_id="equipment:source",
        device_ids=("device:load",),
        rules=CircuitDesignRules(max_devices_per_circuit=1, nominal_diameter_m=0.0254),
        user_groups=(("device:load",),),
        user_input_id="decision:domain-reuse",
        source_port_id="port:source",
        load_port_ids={"device:load": "port:load"},
        routing_options=routing_options or RoutingOptions(port_stub_m=0.0),
        allowed_domain=domain,
    )


def test_user_decision_idempotence_compares_normalized_domain_both_directions():
    source = _model()
    left = _box_from_bounds(-0.3, -1, -1, 2.0, 1, 1)
    right = _box_from_bounds(2.0, -1, -1, 4.3, 1, 1)
    bounded = _domain(left, right, identity="domain:design-reuse")
    same_normalized_request = _domain(right, left, identity="domain:design-reuse")

    unbounded_result = _reviewed_design(source, None)
    assert unbounded_result.circuits[0].attributes["design"].get("allowed_route_domain") is None
    with pytest.raises(PlacementError, match="allowed route domain"):
        _reviewed_design(unbounded_result, bounded)

    bounded_result = _reviewed_design(source, bounded)
    saved_request = bounded_result.circuits[0].attributes["design"]["allowed_route_domain"]
    route_receipt = bounded_result.routes[0].provenance[0].attributes["allowed_route_domain"]
    assert saved_request == {
        key: route_receipt[key]
        for key in ("id", "kind", "source_ids", "containment", "volumes")
    }
    # Box ordering is normalized; identical user decisions remain idempotent.
    assert _reviewed_design(bounded_result, same_normalized_request) is bounded_result
    with pytest.raises(PlacementError, match="allowed route domain"):
        _reviewed_design(bounded_result, None)


@pytest.mark.parametrize("context_change", ("clearance", "moved_port", "blocking_member"))
def test_bounded_user_decision_rejects_changed_routing_context(context_change):
    source = _model()
    domain = _domain(_box_from_bounds(-0.3, -1, -1, 4.3, 1, 1))
    bounded_result = _reviewed_design(
        source, domain, RoutingOptions(port_stub_m=0.0, clearance_m=0.0),
    )

    if context_change == "clearance":
        with pytest.raises(PlacementError, match="bounded routing context"):
            _reviewed_design(
                bounded_result, domain,
                RoutingOptions(port_stub_m=0.0, clearance_m=0.01),
            )
    elif context_change == "moved_port":
        moved_ports = tuple(
            replace(port, pose=Pose(position=Point3(x=0.1, y=0, z=0)))
            if port.id == "port:source" else port
            for port in bounded_result.ports
        )
        old_route = bounded_result.routes[0]
        moved_route = replace(
            old_route,
            centerline=Polyline3D(points=(Point3(x=0.1, y=0, z=0), *old_route.centerline.points[1:])),
        )
        changed_model = replace(
            bounded_result,
            ports=moved_ports,
            routes=tuple(moved_route if route.id == old_route.id else route
                         for route in bounded_result.routes),
        )
        with pytest.raises(PlacementError, match="bounded routing context"):
            _reviewed_design(changed_model, domain)
    else:
        member = StructuralMember(
            id="member:new-blocker", kind="stud", level_id="level:one",
            pose=Pose(position=Point3(x=2, y=0, z=0)),
            size=Size3(x=0.3, y=0.3, z=0.3),
        )
        changed_model = replace(
            bounded_result,
            schema_version="1.4.0",
            levels=(Level(id="level:one", elevation_m=0.0, height_m=3.0),),
            structural_members=(member,),
        )
        with pytest.raises(PlacementError, match="bounded routing context"):
            _reviewed_design(changed_model, domain)


@pytest.mark.parametrize(
    "factory, message",
    (
        (lambda: AllowedRouteDomain(id="", kind="plenum", volumes=(_box_from_bounds(0, 0, 0, 1, 1, 1),)), "id"),
        (lambda: AllowedRouteDomain(id="domain:x", kind="plenum", volumes=()), "nonempty tuple"),
        (lambda: AllowedRouteDomain(id="domain:x", kind="plenum", volumes=(_box_from_bounds(0, 0, 0, 1, 1, 1),) * 129), "limit"),
        (lambda: AllowedRouteDomain(id="domain:x", kind="plenum", volumes=(
            Box3D(
                pose=Pose(
                    position=Point3(x=0, y=0, z=0),
                    rotation=Quaternion(z=math.sin(math.pi / 8), w=math.cos(math.pi / 8)),
                ),
                size=Size3(x=1, y=1, z=1),
            ),
        )), "axis-aligned"),
        (lambda: AllowedRouteDomain(id="domain:x", kind="plenum", volumes=(
            Box3D(
                pose=Pose(
                    position=Point3(x=0, y=0, z=0),
                    rotation=Quaternion(z=math.sin(5e-11), w=math.cos(5e-11)),
                ),
                size=Size3(x=100, y=1, z=1),
            ),
        )), "positional error"),
        (lambda: AllowedRouteDomain(id="domain:x", kind="plenum", volumes=(
            Box3D(
                pose=Pose(position=Point3(x=1.5e308, y=0, z=0)),
                size=Size3(x=1e308, y=1, z=1),
            ),
        )), "world bounds must be finite"),
    ),
)
def test_invalid_allowed_domain_inputs_fail_closed(factory, message):
    with pytest.raises(RoutingError, match=message):
        factory()


def test_unknown_domain_argument_fails_closed():
    source = _model()
    with pytest.raises(RoutingError, match="AllowedRouteDomain"):
        route_between_ports(
            source, "port:source", "port:load", "emt",
            allowed_domain=object(),  # type: ignore[arg-type]
        )


def test_exact_quarter_turn_box_is_normalized_with_only_rounding_error():
    source = _model()
    quarter_turn = Box3D(
        pose=Pose(
            position=Point3(x=2, y=0, z=0),
            rotation=Quaternion(z=math.sin(math.pi / 4), w=math.cos(math.pi / 4)),
        ),
        size=Size3(x=2, y=4.4, z=2),
    )
    route, _ = route_between_ports(
        source, "port:source", "port:load", "emt",
        options=RoutingOptions(port_stub_m=0.0),
        allowed_domain=_domain(quarter_turn),
    )
    assert route.centerline.points[0] == Point3(x=0, y=0, z=0)
    assert route.centerline.points[-1] == Point3(x=4, y=0, z=0)
    receipt = route.provenance[0].attributes["allowed_route_domain"]
    assert receipt["axis_alignment_epsilon_m"] == 1e-9
    assert receipt["envelope_scope"] == "nominal_radius_only; actual_outer_diameter_not_verified"


def test_search_complexity_limit_fails_explicitly(monkeypatch):
    import oabm.routing.router as router_module

    monkeypatch.setattr(router_module, "_MAX_ALLOWED_DOMAIN_SEARCH_STATES", 1)
    with pytest.raises(RoutingError, match="bounded state limit"):
        route_between_ports(
            _model(), "port:source", "port:load", "emt",
            options=RoutingOptions(port_stub_m=0.0),
            allowed_domain=_domain(_box_from_bounds(-0.2, -1, -1, 4.2, 1, 1)),
        )
