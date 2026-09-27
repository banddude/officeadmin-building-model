"""Consolidating bundled per-circuit routes into shared trunk raceways (#196).

These tests pin the opt-in post-process behind the overlap report: shared
stretches become trunk routes, member routes keep their unshared ends as
branches, conductors keep their exact lengths, and a model without overlaps
is returned untouched. All geometry is synthetic rectilinear runs.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from oabm.model import (
    BuildingModel, Circuit, Conductor, ElectricalDevice, ElectricalEquipment,
    Level, Point3, Polyline3D, Port, Pose, Route, RouteFitting, Vector3,
    validate_model,
)
from oabm.quantities import extract_quantities
from oabm.routing import (
    consolidate_bundled_routes, find_overlapping_route_runs, RoutingError,
)
from oabm.ifc.adapter import from_ifc, to_ifc


def _length(route: Route) -> float:
    points = route.centerline.points
    return math.fsum(
        math.sqrt(
            (first.x - second.x) ** 2
            + (first.y - second.y) ** 2
            + (first.z - second.z) ** 2
        )
        for first, second in zip(points, points[1:])
    )


def _total_length(model: BuildingModel) -> float:
    return math.fsum(_length(route) for route in model.routes)


def _trunks(model: BuildingModel) -> tuple[Route, ...]:
    return tuple(
        route for route in model.routes
        if "member_route_ids" in route.attributes.get("consolidation", {})
    )


def _branches(model: BuildingModel) -> tuple[Route, ...]:
    return tuple(
        route for route in model.routes
        if "source_route_id" in route.attributes.get("consolidation", {})
    )


def _device(device_id: str, position: tuple[float, float, float]) -> ElectricalDevice:
    x, y, z = position
    return ElectricalDevice(
        id=device_id, device_type="receptacle",
        pose=Pose(position=Point3(x=x, y=y, z=z)),
    )


def _port(port_id: str, owner_id: str, position: tuple[float, float, float], role: str) -> Port:
    x, y, z = position
    return Port(
        id=port_id, owner_id=owner_id, domain="power", role=role,
        pose=Pose(position=Point3(x=x, y=y, z=z)),
        direction=Vector3(x=1.0, y=0.0, z=0.0),
    )


def _route(
    route_id: str, start_port_id: str, end_port_id: str,
    points: tuple[tuple[float, float, float], ...], diameter: float = 0.021,
) -> Route:
    return Route(
        id=route_id, route_type="emt", start_port_id=start_port_id,
        end_port_id=end_port_id,
        centerline=Polyline3D(points=tuple(Point3(x=x, y=y, z=z) for x, y, z in points)),
        nominal_diameter_m=diameter,
    )


def _wiring(
    circuits: list[Circuit], conductors: list[Conductor],
    index: int, route_id: str, source_port_id: str, load_port_id: str,
) -> None:
    circuits.append(Circuit(
        id=f"circuit:acc-{index}", source_port_id=source_port_id,
        load_port_ids=(load_port_id,), route_ids=(route_id,),
    ))
    conductors.append(Conductor(
        id=f"conductor:acc-{index}", circuit_id=f"circuit:acc-{index}",
        role="circuit", count=2, route_ids=(route_id,),
    ))


def acceptance_model() -> BuildingModel:
    """Three device-to-panel home runs sharing a 20 m trunk, each with a 3 m drop.

    The trunk runs 0..20 m along +x at y=0, z=3; the drops leave its far end
    along +y, -y, and +z, so every route is 23 m and the bundle totals 69 m.
    """

    panel = ElectricalEquipment(
        id="equipment:acc-panel", equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=3.0)),
    )
    panel_port = _port("port:acc-panel-out", panel.id, (0.0, 0.0, 3.0), "source")
    drops = ((20.0, 3.0, 3.0), (20.0, -3.0, 3.0), (20.0, 0.0, 6.0))
    devices: list[ElectricalDevice] = []
    ports: list[Port] = [panel_port]
    routes: list[Route] = []
    circuits: list[Circuit] = []
    conductors: list[Conductor] = []
    for index, drop in enumerate(drops):
        device = _device(f"device:acc-{index}", drop)
        device_port = _port(f"port:acc-{index}", device.id, drop, "load")
        devices.append(device)
        ports.append(device_port)
        routes.append(_route(
            f"route:acc-{index}", panel_port.id, device_port.id,
            ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0), drop),
        ))
        _wiring(circuits, conductors, index, f"route:acc-{index}", panel_port.id, device_port.id)
    return BuildingModel(
        model_id="model:route-consolidation-acceptance",
        electrical_equipment=(panel,),
        electrical_devices=tuple(devices),
        ports=tuple(ports),
        routes=tuple(routes),
        circuits=tuple(circuits),
        conductors=tuple(conductors),
    )


def messy_model() -> BuildingModel:
    """Partial overlaps on one line plus a route that turns a corner.

    Route a rides the whole 0..20 m line, b joins for 5..10, c for 15..20
    (sharing a's end device), and d comes down from (8, 7, 3), turns at
    (8, 0, 3), and rides the line to the shared end. Old total is 49 m; the
    one maximal run double-counts 22 m; consolidation must land on 27 m.
    """

    panel = ElectricalEquipment(
        id="equipment:messy-panel", equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=3.0)),
    )
    panel_port = _port("port:messy-panel-out", panel.id, (0.0, 0.0, 3.0), "source")
    device_positions = {
        "device:messy-a": (0.0, 0.0, 3.0),
        "device:messy-b1": (5.0, 0.0, 3.0),
        "device:messy-b2": (10.0, 0.0, 3.0),
        "device:messy-c1": (15.0, 0.0, 3.0),
        "device:messy-d1": (8.0, 7.0, 3.0),
        "device:messy-end": (20.0, 0.0, 3.0),
    }
    devices = tuple(_device(device_id, position) for device_id, position in device_positions.items())
    ports = [
        _port("port:messy-a", "device:messy-a", (0.0, 0.0, 3.0), "load"),
        _port("port:messy-b1", "device:messy-b1", (5.0, 0.0, 3.0), "load"),
        _port("port:messy-b2", "device:messy-b2", (10.0, 0.0, 3.0), "load"),
        _port("port:messy-c1", "device:messy-c1", (15.0, 0.0, 3.0), "load"),
        _port("port:messy-d1", "device:messy-d1", (8.0, 7.0, 3.0), "load"),
        _port("port:messy-end", "device:messy-end", (20.0, 0.0, 3.0), "load"),
        panel_port,
    ]
    routes = [
        _route("route:messy-a", "port:messy-a", "port:messy-end",
               ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0))),
        _route("route:messy-b", "port:messy-b1", "port:messy-b2",
               ((5.0, 0.0, 3.0), (10.0, 0.0, 3.0))),
        _route("route:messy-c", "port:messy-c1", "port:messy-end",
               ((15.0, 0.0, 3.0), (20.0, 0.0, 3.0))),
        _route("route:messy-d", "port:messy-d1", "port:messy-end",
               ((8.0, 7.0, 3.0), (8.0, 0.0, 3.0), (20.0, 0.0, 3.0))),
    ]
    corner_elbow = RouteFitting(
        id="fitting:messy-d-corner", route_id="route:messy-d",
        fitting_type="elbow-90", pose=Pose(position=Point3(x=8.0, y=0.0, z=3.0)),
        nominal_diameter_m=0.021, angle_radians=math.pi / 2,
    )
    routes[3] = Route(
        id=routes[3].id, route_type=routes[3].route_type,
        start_port_id=routes[3].start_port_id, end_port_id=routes[3].end_port_id,
        centerline=routes[3].centerline,
        nominal_diameter_m=routes[3].nominal_diameter_m,
        fitting_ids=(corner_elbow.id,),
    )
    circuits: list[Circuit] = []
    conductors: list[Conductor] = []
    for index, route in enumerate(routes):
        circuits.append(Circuit(
            id=f"circuit:messy-{index}", source_port_id=panel_port.id,
            load_port_ids=(route.end_port_id,), route_ids=(route.id,),
        ))
        conductors.append(Conductor(
            id=f"conductor:messy-{index}", circuit_id=f"circuit:messy-{index}",
            role="circuit", count=2, route_ids=(route.id,),
        ))
    return BuildingModel(
        model_id="model:route-consolidation-messy",
        electrical_equipment=(panel,),
        electrical_devices=devices,
        ports=tuple(ports),
        routes=tuple(routes),
        route_fittings=(corner_elbow,),
        circuits=tuple(circuits),
        conductors=tuple(conductors),
    )


def test_no_runs_returns_the_model_untouched() -> None:
    device_a = _device("device:seq-a", (0.0, 0.0, 3.0))
    device_b = _device("device:seq-b", (10.0, 0.0, 3.0))
    device_c = _device("device:seq-c", (20.0, 0.0, 3.0))
    model = BuildingModel(
        model_id="model:route-consolidation-sequential",
        electrical_devices=(device_a, device_b, device_c),
        ports=(
            _port("port:seq-a", device_a.id, (0.0, 0.0, 3.0), "load"),
            _port("port:seq-b", device_b.id, (10.0, 0.0, 3.0), "load"),
            _port("port:seq-c", device_c.id, (20.0, 0.0, 3.0), "load"),
        ),
        routes=(
            # Two routes that only touch at (10, 0, 3) share nothing.
            _route("route:seq-0", "port:seq-a", "port:seq-b",
                   ((0.0, 0.0, 3.0), (10.0, 0.0, 3.0))),
            _route("route:seq-1", "port:seq-b", "port:seq-c",
                   ((10.0, 0.0, 3.0), (20.0, 0.0, 3.0))),
        ),
    )
    result = consolidate_bundled_routes(model)
    assert result.model is model
    assert result.report.routes_before == 2
    assert result.report.routes_after == 2
    assert result.report.trunk_routes == 0
    assert result.report.shared_length_m == 0.0


def test_acceptance_bundle_becomes_one_trunk_and_three_branches() -> None:
    model = acceptance_model()
    assert _total_length(model) == pytest.approx(69.0)

    result = consolidate_bundled_routes(model)
    consolidated = result.model
    validate_model(consolidated)

    assert result.report.routes_before == 3
    assert result.report.routes_after == 4
    assert result.report.trunk_routes == 1
    assert result.report.branch_routes == 3
    assert _total_length(consolidated) == pytest.approx(29.0)

    trunks = _trunks(consolidated)
    assert len(trunks) == 1
    trunk = trunks[0]
    assert _length(trunk) == pytest.approx(20.0)
    assert trunk.attributes["consolidation"]["member_route_ids"] == [
        "route:acc-0", "route:acc-1", "route:acc-2",
    ]
    assert trunk.attributes["consolidation"]["member_count"] == 3
    assert trunk.nominal_diameter_m == pytest.approx(0.021)
    assert trunk.provenance[0].derivation == "inferred"
    assert trunk.provenance[0].method == "route-consolidation"

    branches = {branch.attributes["consolidation"]["source_route_id"]: branch for branch in _branches(consolidated)}
    assert set(branches) == {"route:acc-0", "route:acc-1", "route:acc-2"}
    for branch in branches.values():
        assert _length(branch) == pytest.approx(3.0)
        assert branch.provenance[0].method == "route-consolidation"

    # The trunk starts at the panel port and taps off at a junction port.
    assert trunk.start_port_id == "port:acc-panel-out"
    junction = next(port for port in consolidated.ports if port.role == "junction")
    assert trunk.end_port_id == junction.id
    assert (junction.pose.position.x, junction.pose.position.y, junction.pose.position.z) == (20.0, 0.0, 3.0)
    owner = next(
        entity for entity in (*consolidated.electrical_devices, *consolidated.electrical_equipment)
        if entity.id == junction.owner_id
    )
    assert isinstance(owner, ElectricalDevice)


def test_one_tee_where_the_branches_leave_the_trunk() -> None:
    consolidated = consolidate_bundled_routes(acceptance_model()).model
    tees = [fitting for fitting in consolidated.route_fittings if fitting.fitting_type == "tee"]
    assert len(tees) == 1
    tee = tees[0]
    trunk = _trunks(consolidated)[0]
    assert tee.route_id == trunk.id
    assert (tee.pose.position.x, tee.pose.position.y, tee.pose.position.z) == (20.0, 0.0, 3.0)
    assert trunk.fitting_ids == (tee.id,)


def test_total_length_matches_the_overlap_identity() -> None:
    for model in (acceptance_model(), messy_model()):
        runs = find_overlapping_route_runs(model)
        double_counted = math.fsum(run.double_counted_length_m for run in runs)
        result = consolidate_bundled_routes(model)
        assert _total_length(result.model) == pytest.approx(
            _total_length(model) - double_counted, abs=1e-6 * len(model.routes),
        )


def test_conductor_lengths_are_preserved_exactly() -> None:
    for model in (acceptance_model(), messy_model()):
        result = consolidate_bundled_routes(model)
        routes_by_id = {route.id: route for route in result.model.routes}
        old_conductors = {conductor.id: conductor for conductor in model.conductors}
        for conductor in result.model.conductors:
            old = math.fsum(
                _length(next(r for r in model.routes if r.id == route_id))
                for route_id in old_conductors[conductor.id].route_ids
            )
            new = math.fsum(_length(routes_by_id[route_id]) for route_id in conductor.route_ids)
            assert new == old


def test_conductor_route_ids_follow_the_centerline_order() -> None:
    model = acceptance_model()
    result = consolidate_bundled_routes(model)
    consolidated = result.model
    trunk = _trunks(consolidated)[0]
    branches = {branch.attributes["consolidation"]["source_route_id"]: branch for branch in _branches(consolidated)}
    for conductor in consolidated.conductors:
        assert conductor.route_ids == (trunk.id, branches[conductor.id.replace("conductor", "route")].id)


def test_messy_partial_overlaps_split_at_coverage_boundaries() -> None:
    model = messy_model()
    assert _total_length(model) == pytest.approx(49.0)
    runs = find_overlapping_route_runs(model)
    assert len(runs) == 1
    assert runs[0].double_counted_length_m == pytest.approx(22.0)

    result = consolidate_bundled_routes(model)
    consolidated = result.model
    validate_model(consolidated)

    trunks = _trunks(consolidated)
    assert len(trunks) == 4
    # Routes come back sorted by id, not by position: compare as sets.
    assert sorted(_length(trunk) for trunk in trunks) == [2.0, 3.0, 5.0, 5.0]
    assert sorted(
        tuple(trunk.attributes["consolidation"]["member_route_ids"]) for trunk in trunks
    ) == sorted([
        ("route:messy-a", "route:messy-b"),
        ("route:messy-a", "route:messy-b", "route:messy-d"),
        ("route:messy-a", "route:messy-d"),
        ("route:messy-a", "route:messy-c", "route:messy-d"),
    ])
    branches = {branch.attributes["consolidation"]["source_route_id"]: branch for branch in _branches(consolidated)}
    assert set(branches) == {"route:messy-a", "route:messy-d"}
    assert _length(branches["route:messy-a"]) == pytest.approx(5.0)
    assert _length(branches["route:messy-d"]) == pytest.approx(7.0)
    assert (branches["route:messy-d"].centerline.points[0].x,
            branches["route:messy-d"].centerline.points[0].y) == (8.0, 7.0)

    # b and c ride trunks only; their conductors reference exactly their old length.
    pieces = {
        conductor.id: [_length(next(r for r in consolidated.routes if r.id == rid)) for rid in conductor.route_ids]
        for conductor in consolidated.conductors
    }
    assert pieces["conductor:messy-0"] == [5.0, 3.0, 2.0, 5.0, 5.0]
    assert pieces["conductor:messy-1"] == [3.0, 2.0]
    assert pieces["conductor:messy-2"] == [5.0]
    assert pieces["conductor:messy-3"] == [7.0, 2.0, 5.0, 5.0]

    # One junction port at the corner vertex (8, 0, 3); the elbow gives way to tees.
    junctions = [port for port in consolidated.ports if port.role == "junction"]
    assert len(junctions) == 1
    assert (junctions[0].pose.position.x, junctions[0].pose.position.y, junctions[0].pose.position.z) == (8.0, 0.0, 3.0)
    assert not any(fitting.fitting_type.startswith("elbow") for fitting in consolidated.route_fittings)
    tees = [fitting for fitting in consolidated.route_fittings if fitting.fitting_type == "tee"]
    assert len(tees) == 7
    tee_positions = sorted(
        (fitting.pose.position.x, fitting.pose.position.y, fitting.pose.position.z)
        for fitting in tees
    )
    assert tee_positions.count((5.0, 0.0, 3.0)) == 1
    assert tee_positions.count((8.0, 0.0, 3.0)) == 2
    assert tee_positions.count((10.0, 0.0, 3.0)) == 2
    assert tee_positions.count((15.0, 0.0, 3.0)) == 2
    for trunk in trunks:
        own_tees = sorted(
            (fitting for fitting in tees if fitting.route_id == trunk.id),
            key=lambda fitting: fitting.pose.position.x,
        )
        assert trunk.fitting_ids == tuple(fitting.id for fitting in own_tees)
    assert result.report.ports_added == 1
    assert result.report.routes_after == 6


def test_different_route_types_are_not_consolidated() -> None:
    model = acceptance_model()
    flex = dataclasses.replace(
        next(route for route in model.routes if route.id == "route:acc-1"),
        id="route:acc-flex", route_type="flex",
    )
    rerouted = [flex if route.id == "route:acc-1" else route for route in model.routes]
    rewired = [
        circuit if circuit.id != "circuit:acc-1" else dataclasses.replace(
            circuit, route_ids=("route:acc-flex",),
        )
        for circuit in model.circuits
    ]
    recond = [
        conductor if conductor.id != "conductor:acc-1" else dataclasses.replace(
            conductor, route_ids=("route:acc-flex",),
        )
        for conductor in model.conductors
    ]
    model = dataclasses.replace(
        model, routes=tuple(rerouted), circuits=tuple(rewired), conductors=tuple(recond),
    )
    result = consolidate_bundled_routes(model)
    # The flex reroute overlaps nothing of its own type, so it survives as is;
    # the remaining emt pair still consolidates, without it.
    survivors = [route for route in result.model.routes if route.id == "route:acc-flex"]
    assert len(survivors) == 1
    assert _length(survivors[0]) == pytest.approx(23.0)
    members = [
        trunk.attributes["consolidation"]["member_route_ids"]
        for trunk in _trunks(result.model)
    ]
    assert all("route:acc-flex" not in chunk for chunk in members)


def test_trunk_sizing_takes_the_largest_member_diameter() -> None:
    model = acceptance_model()
    thicker = dataclasses.replace(
        next(route for route in model.routes if route.id == "route:acc-2"),
        nominal_diameter_m=0.035,
    )
    rerouted = [thicker if route.id == "route:acc-2" else route for route in model.routes]
    model = dataclasses.replace(model, routes=tuple(rerouted))
    trunk = _trunks(consolidate_bundled_routes(model).model)[0]
    assert trunk.nominal_diameter_m == pytest.approx(0.035)


def test_junction_ports_fail_closed_without_a_device_owner() -> None:
    level = Level(id="level:orphan", elevation_m=0.0)
    # The messy bundle's geometry, but every port owned by the level: a trunk
    # junction at the corner vertex (8, 0, 3) then has no distribution
    # element to hang the new port on, so consolidation must fail closed.
    model = messy_model()
    orphan_ports = tuple(
        dataclasses.replace(port, owner_id=level.id) for port in model.ports
    )
    model = dataclasses.replace(
        model,
        model_id="model:route-consolidation-orphan",
        levels=(level,),
        electrical_equipment=(),
        electrical_devices=(),
        ports=orphan_ports,
    )
    with pytest.raises(RoutingError, match="junction port needs an electrical device"):
        consolidate_bundled_routes(model)


def test_fitting_off_a_vertex_fails_closed() -> None:
    model = acceptance_model()
    mid = RouteFitting(
        id="fitting:mid-segment", route_id="route:acc-0",
        fitting_type="pull", pose=Pose(position=Point3(x=10.0, y=0.0, z=3.0)),
    )
    original = next(route for route in model.routes if route.id == "route:acc-0")
    with_elbow = dataclasses.replace(original, fitting_ids=(mid.id,))
    rerouted = tuple(with_elbow if route.id == "route:acc-0" else route for route in model.routes)
    model = dataclasses.replace(model, routes=rerouted, route_fittings=(*model.route_fittings, mid))
    with pytest.raises(RoutingError, match="does not sit on a vertex"):
        consolidate_bundled_routes(model)


def test_quantities_overlap_warning_is_gone_after_consolidation() -> None:
    for model in (acceptance_model(), messy_model()):
        before = extract_quantities(model)
        assert any(warning.code == "overlapping_route_runs" for warning in before.warnings)
        result = consolidate_bundled_routes(model)
        after = extract_quantities(result.model)
        assert not any(warning.code == "overlapping_route_runs" for warning in after.warnings)
        route_total = sum(
            item.quantity for item in after.items if item.category == "route_length"
        )
        assert route_total == pytest.approx(_total_length(result.model))


def test_consolidation_is_deterministic_byte_for_byte() -> None:
    for builder in (acceptance_model, messy_model):
        first = consolidate_bundled_routes(builder()).model.to_json()
        second = consolidate_bundled_routes(builder()).model.to_json()
        assert first == second


def test_shuffled_route_order_gives_identical_output() -> None:
    model = messy_model()
    shuffled = dataclasses.replace(
        model,
        routes=tuple(reversed(model.routes)),
        circuits=tuple(reversed(model.circuits)),
        conductors=tuple(reversed(model.conductors)),
        ports=tuple(reversed(model.ports)),
    )
    assert consolidate_bundled_routes(shuffled).model.to_json() == (
        consolidate_bundled_routes(model).model.to_json()
    )


def test_second_pass_is_a_no_op() -> None:
    model = messy_model()
    once = consolidate_bundled_routes(model)
    twice = consolidate_bundled_routes(once.model)
    assert twice.report.trunk_routes == 0
    assert twice.model.to_json() == once.model.to_json()


def test_ifc_round_trip_keeps_route_count_and_total_length(tmp_path) -> None:
    for builder in (acceptance_model, messy_model):
        result = consolidate_bundled_routes(builder())
        path = tmp_path / f"{result.model.model_id}.ifc"
        to_ifc(result.model, path)
        back = from_ifc(path)
        validate_model(back)
        assert len(back.routes) == len(result.model.routes)
        assert _total_length(back) == pytest.approx(_total_length(result.model))
        assert len(back.route_fittings) == len(result.model.route_fittings)
        assert sum(
            _length(route) for route in back.routes if "member_route_ids" in route.attributes.get("consolidation", {})
        ) == pytest.approx(math.fsum(
            _length(route) for route in result.model.routes
            if "member_route_ids" in route.attributes.get("consolidation", {})
        ))


def test_provenance_marks_new_entities_as_inferred() -> None:
    consolidated = consolidate_bundled_routes(messy_model()).model
    old_ids = {"route:messy-a", "route:messy-b", "route:messy-c", "route:messy-d"}
    for route in consolidated.routes:
        if route.id in old_ids:
            continue
        assert route.provenance[0].method == "route-consolidation"
        assert route.provenance[0].derivation == "inferred"
        assert route.provenance[0].source_kind == "derived"
    for fitting in consolidated.route_fittings:
        if fitting.fitting_type == "tee":
            assert fitting.provenance[0].method == "route-consolidation"
            assert fitting.provenance[0].derivation == "inferred"
