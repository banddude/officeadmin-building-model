"""Overlapping route runs (#201 groundwork for consolidation in #196).

Bundled per-circuit routes ride the same trunk until consolidation lands.
These tests pin the read-only overlap detector and the single quantities
warning that tells a consumer when per-route conduit lengths may be
double-counted. All geometry is synthetic rectilinear runs.
"""

from __future__ import annotations

import pytest

from oabm.model import (
    BuildingModel, ElectricalEquipment, Point3, Polyline3D, Port, Pose, Route,
    Vector3,
)
from oabm.quantities import extract_quantities
from oabm.routing import RouteOverlap, find_overlapping_route_runs


def _model_with_routes(*centerlines, route_types=None) -> BuildingModel:
    """A valid model whose routes carry the given synthetic centerlines.

    One panel owns every port; each route gets its own two ports sitting
    exactly on its centerline endpoints, which is what the contract requires
    of a hand-built route.
    """

    panel = ElectricalEquipment(
        id="equipment:overlap-panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=3.0)),
    )
    ports: list[Port] = []
    routes: list[Route] = []
    for index, points in enumerate(centerlines):
        vertices = tuple(Point3(x=x, y=y, z=z) for x, y, z in points)
        start_id = f"port:overlap-{index}-start"
        end_id = f"port:overlap-{index}-end"
        for port_id, position, role, direction in (
            (start_id, vertices[0], "source", Vector3(x=1.0, y=0.0, z=0.0)),
            (end_id, vertices[-1], "sink", Vector3(x=-1.0, y=0.0, z=0.0)),
        ):
            ports.append(Port(
                id=port_id,
                owner_id=panel.id,
                domain="power",
                role=role,
                pose=Pose(position=position),
                direction=direction,
                nominal_diameter_m=0.021,
            ))
        routes.append(Route(
            id=f"route:overlap-{index}",
            route_type=(route_types[index] if route_types is not None else "emt"),
            start_port_id=start_id,
            end_port_id=end_id,
            centerline=Polyline3D(points=vertices),
            nominal_diameter_m=0.021,
        ))
    return BuildingModel(
        model_id="model:route-overlap-test",
        electrical_equipment=(panel,),
        ports=tuple(ports),
        routes=tuple(routes),
    )


# Three home runs riding one 20 m trunk at y=0, z=3, each with its own
# 3 m drop: down at x=0, up at x=0, and down at x=20. Every drop only
# touches its neighbours at a single point.
TRUNK_BUNDLE = (
    ((0.0, 0.0, 0.0), (0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
    ((0.0, 0.0, 6.0), (0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
    ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0), (20.0, 0.0, 0.0)),
)


def test_shared_trunk_with_unique_drops_makes_one_maximal_run() -> None:
    model = _model_with_routes(*TRUNK_BUNDLE)
    runs = find_overlapping_route_runs(model)

    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, RouteOverlap)
    assert run.route_ids == ("route:overlap-0", "route:overlap-1", "route:overlap-2")
    assert run.route_type == "emt"
    assert run.shared_length_m == pytest.approx(20.0)
    assert (run.start.x, run.start.y, run.start.z) == pytest.approx((0.0, 0.0, 3.0))
    assert (run.end.x, run.end.y, run.end.z) == pytest.approx((20.0, 0.0, 3.0))


def test_quantities_warn_once_per_type_with_the_double_counted_length() -> None:
    model = _model_with_routes(*TRUNK_BUNDLE)
    report = extract_quantities(model)

    overlap_warnings = [w for w in report.warnings if w.code == "overlapping_route_runs"]
    assert len(overlap_warnings) == 1
    warning = overlap_warnings[0]
    assert warning.source_entity_ids == (
        "route:overlap-0", "route:overlap-1", "route:overlap-2",
    )
    assert "20.00 m" in warning.message
    # Two extra copies of the 20 m trunk: the potential double count is 40 m.
    assert "40.00 m" in warning.message

    # The quantities themselves are untouched: three 23 m runs.
    route_items = {
        (item.item_type, tuple(item.variant)): item.quantity
        for item in report.items if item.category == "route_length"
    }
    assert sum(route_items.values()) == pytest.approx(69.0)


def test_routes_touching_only_at_an_endpoint_do_not_overlap() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (10.0, 0.0, 3.0)),
        ((10.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
    )
    assert find_overlapping_route_runs(model) == ()


def test_perpendicular_crossings_do_not_overlap() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        ((10.0, -5.0, 3.0), (10.0, 5.0, 3.0)),
    )
    assert find_overlapping_route_runs(model) == ()


def test_different_route_types_do_not_overlap() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        route_types=("emt", "flex"),
    )
    assert find_overlapping_route_runs(model) == ()


def test_parallel_offset_beyond_tolerance_does_not_overlap() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        ((0.0, 0.5, 3.0), (20.0, 0.5, 3.0)),
    )
    assert find_overlapping_route_runs(model) == ()


def test_parallel_offset_within_tolerance_shares_a_run() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        ((0.0, 0.005, 3.0), (20.0, 0.005, 3.0)),
    )
    runs = find_overlapping_route_runs(model)
    assert len(runs) == 1
    assert runs[0].shared_length_m == pytest.approx(20.0)
    assert len(runs[0].route_ids) == 2


def test_graded_extents_merge_into_one_run_with_unioned_members() -> None:
    # overlap-0 rides the full corridor; the other two turn off part-way.
    # The pairwise spans chain, so one maximal run lists all three routes.
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
        ((0.0, 0.0, 3.0), (15.0, 0.0, 3.0)),
        ((10.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
    )
    runs = find_overlapping_route_runs(model)
    assert len(runs) == 1
    assert runs[0].route_ids == ("route:overlap-0", "route:overlap-1", "route:overlap-2")
    assert runs[0].shared_length_m == pytest.approx(20.0)


def test_multiple_runs_are_ordered_and_scoped_per_type() -> None:
    # Two separate shared corridors, plus a flex pair on its own line.
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (10.0, 0.0, 3.0)),
        ((0.0, 0.0, 3.0), (10.0, 0.0, 3.0)),
        ((30.0, 0.0, 3.0), (40.0, 0.0, 3.0)),
        ((30.0, 0.0, 3.0), (40.0, 0.0, 3.0)),
        ((100.0, 0.0, 3.0), (110.0, 0.0, 3.0)),
        ((100.0, 0.0, 3.0), (110.0, 0.0, 3.0)),
        route_types=("emt", "emt", "emt", "emt", "flex", "flex"),
    )
    runs = find_overlapping_route_runs(model)
    assert [(run.route_ids, run.shared_length_m) for run in runs] == [
        (("route:overlap-0", "route:overlap-1"), 10.0),
        (("route:overlap-2", "route:overlap-3"), 10.0),
        (("route:overlap-4", "route:overlap-5"), 10.0),
    ]
    assert {run.route_type for run in runs} == {"emt", "flex"}


def test_detection_is_deterministic_and_read_only() -> None:
    model = _model_with_routes(*TRUNK_BUNDLE)
    before = model.to_json()

    first = find_overlapping_route_runs(model)
    second = find_overlapping_route_runs(model)
    assert first == second
    assert len(first) > 0
    assert model.to_json() == before


def test_no_warning_when_nothing_overlaps() -> None:
    model = _model_with_routes(
        ((0.0, 0.0, 3.0), (10.0, 0.0, 3.0)),
        ((10.0, 0.0, 3.0), (20.0, 0.0, 3.0)),
    )
    report = extract_quantities(model)
    assert report.warnings == ()
