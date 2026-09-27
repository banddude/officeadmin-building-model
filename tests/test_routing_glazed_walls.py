"""Glazed walls are never raceway pathways or equipment hosts.

Covers issue #162 for the routing lane: a glazed wall contributes no
surface-pathway rule and only the soft ``glazed_wall_penalty``, remaining
concealment is reported as ``route_in_glazed_wall``, placement skips glazed
hosts, and models without glazed walls route byte-identically to main.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import (
    Box3D,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Obstacle,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Size3,
    Space,
    Vector3,
    Wall,
)
from oabm.routing import (
    PlacementError,
    RoutingError,
    RoutingOptions,
    apply_equipment_proposal,
    propose_equipment_placement,
    route_between_ports,
    set_user_equipment_placement,
)

ROOT = Path(__file__).resolve().parents[1]

# sha256 of to_json() for these fixtures routed and reattached at ebf2c67,
# before glazed-wall handling existed. None of them sets a construction, so
# glazed handling must leave their routed documents byte-identical.
PINNED_ROUTED_DIGESTS = {
    "routing/v1/normal.json": "066b10b8faf26a0360d9d3976ebc49bee56f974576b80e7702279cfeaf928c14",
    "routing/v1/alternate-obstacle.json": "8429a8ef4a276aee86af1c976c798b43316cdf99cb576cffb7262bdd1a2211b1",
    "model/v1/garage-route.json": "2a22ea869cd21015a5faeb25a9f6adfc83720f56ef954af405045ba9246e9962",
}


def _box(x, y, z, sx, sy, sz):
    return Box3D(pose=Pose(position=Point3(x=x, y=y, z=z)), size=Size3(x=sx, y=sy, z=sz))


def _wall(wall_id, construction, x0, y0, x1, y1):
    axis = (Point3(x=x0, y=y0, z=0), Point3(x=x1, y=y1, z=0))
    return Wall(
        id=wall_id,
        level_id="level:one",
        construction=construction,
        centerline=Polyline3D(points=axis),
        thickness_m=0.1,
        height_m=3.0,
    )


def _routing_model(walls, *, start, end, start_direction, end_direction, obstacles=()):
    source = ElectricalEquipment(
        id="equipment:source", equipment_type="panelboard", pose=Pose(position=start),
        level_id="level:one",
    )
    load = ElectricalDevice(
        id="device:load", device_type="receptacle_duplex", pose=Pose(position=end),
        level_id="level:one",
    )
    ports = (
        Port(id="port:source", owner_id=source.id, domain="power", role="source",
             pose=Pose(position=start), direction=start_direction, nominal_diameter_m=0.021),
        Port(id="port:load", owner_id=load.id, domain="power", role="sink",
             pose=Pose(position=end), direction=end_direction, nominal_diameter_m=0.021),
    )
    return BuildingModel(
        model_id="model:glazed-routing",
        levels=(Level(id="level:one", elevation_m=0.0, height_m=3.0),),
        walls=tuple(walls), electrical_equipment=(source,), electrical_devices=(load,),
        ports=ports, obstacles=tuple(obstacles),
    )


def _parallel_wall_model(first, second):
    # Source and device sit centered between two parallel walls. A
    # full-height hard box spans the whole gap, so the run must pass through
    # one wall's padded corridor; both corridors have identical geometry,
    # length, and bend count.
    return _routing_model(
        (_wall("wall:first", first, 0, 0, 6, 0), _wall("wall:second", second, 0, 2, 6, 2)),
        start=Point3(x=0, y=1, z=1.5),
        end=Point3(x=6, y=1, z=1.5),
        start_direction=Vector3(x=1, y=0, z=0),
        end_direction=Vector3(x=-1, y=0, z=0),
        obstacles=(Obstacle(id="obstacle:block", geometry=_box(3, 1, 1.5, 1.2, 1.8, 3.0), clearance_m=0.05),),
    )


def _points(route):
    return tuple((p.x, p.y, p.z) for p in route.centerline.points)


def test_route_prefers_framed_wall_over_parallel_glazed_wall():
    model = _parallel_wall_model("framed", "glazed")
    route, _ = route_between_ports(model, "port:source", "port:load", "emt")
    interior = route.centerline.points[1:-1]
    # The long run rides inside the framed wall's discounted padded corridor.
    assert all(point.y <= 1.0 for point in interior)
    assert any(0.0 <= point.y <= 0.1105 for point in interior)
    assert "route_in_glazed_wall" not in route.attributes


def test_preference_follows_construction_not_wall_position():
    model = _parallel_wall_model("glazed", "framed")
    route, _ = route_between_ports(model, "port:source", "port:load", "emt")
    interior = route.centerline.points[1:-1]
    # Mirrored model: the framed corridor at y=2 now wins.
    assert all(point.y >= 1.0 for point in interior)
    assert any(1.8895 <= point.y <= 2.0 for point in interior)


def test_forced_route_completes_and_reports_glazed_segments():
    # Both devices hang on the glazing itself: their stubs start inside the
    # glass, so in-glazing segments are unavoidable. The run must complete
    # and be reported, never rejected -- glazing stays soft.
    model = _routing_model(
        (_wall("wall:glazed", "glazed", 0, 0, 4, 0),),
        start=Point3(x=1, y=0, z=1.5),
        end=Point3(x=3.5, y=0, z=1.5),
        start_direction=Vector3(x=1, y=0, z=0),
        end_direction=Vector3(x=-1, y=0, z=0),
    )
    route, fittings = route_between_ports(model, "port:source", "port:load", "emt")
    assert _points(route)[0] == (1.0, 0.0, 1.5)
    assert _points(route)[-1] == (3.5, 0.0, 1.5)
    hits = route.attributes["route_in_glazed_wall"]
    assert hits
    assert all(hit["wall_id"] == "wall:glazed" for hit in hits)
    assert hits[0]["segment_index"] == 0


def test_reported_segment_index_matches_centerline_segments():
    walls = (_wall("wall:glazed", "glazed", 0, 0, 4, 0),)
    model = _routing_model(
        walls,
        start=Point3(x=1, y=0, z=1.5),
        end=Point3(x=3.5, y=0, z=1.5),
        start_direction=Vector3(x=1, y=0, z=0),
        end_direction=Vector3(x=-1, y=0, z=0),
    )
    route, _ = route_between_ports(model, "port:source", "port:load", "emt")
    radius = 0.021 / 2
    pad = 0.1 / 2 + 0.05 + radius  # surface padding: thickness/2 + tolerance + radius
    for hit in route.attributes["route_in_glazed_wall"]:
        a = route.centerline.points[hit["segment_index"]]
        b = route.centerline.points[hit["segment_index"] + 1]
        midpoint = Point3(
            x=(a.x + b.x) / 2, y=(a.y + b.y) / 2, z=(a.z + b.z) / 2,
        )
        assert -pad <= midpoint.y <= pad
        assert 0 - pad <= midpoint.x <= 4 + pad


def test_glazed_routing_is_deterministic_across_runs():
    model = _parallel_wall_model("framed", "glazed")
    first = route_between_ports(model, "port:source", "port:load", "emt")
    again = route_between_ports(model, "port:source", "port:load", "emt")
    assert first == again
    first_doc = replace(model, routes=(first[0],), route_fittings=first[1]).to_json()
    again_doc = replace(model, routes=(again[0],), route_fittings=again[1]).to_json()
    assert first_doc == again_doc


@pytest.mark.parametrize("penalty", [0, 0.0, -1.0, float("nan"), float("inf")])
def test_glazed_wall_penalty_must_be_finite_and_positive(penalty):
    with pytest.raises(RoutingError, match="glazed_wall_penalty"):
        RoutingOptions(glazed_wall_penalty=penalty)


def test_default_glazed_wall_penalty_is_documented_value():
    assert RoutingOptions().glazed_wall_penalty == 4.0


def _placement_model(*walls):
    level = Level(id="level:one", elevation_m=0)
    space = Space(
        id="space:room", level_id=level.id,
        footprint=Polygon3D(points=(
            Point3(x=0, y=0, z=0), Point3(x=6, y=0, z=0),
            Point3(x=6, y=4, z=0), Point3(x=0, y=4, z=0),
        )),
        usage="office",
    )
    return BuildingModel(model_id="model:glazed-placement", levels=(level,),
                         spaces=(space,), walls=tuple(walls))


def _proposal(model):
    return propose_equipment_placement(
        model, identity_key="proposed-source-panel", equipment_type="panelboard",
        name="LP-GLAZE", level_id="level:one",
    )


def test_placement_never_selects_a_glazed_wall():
    model = _placement_model(
        _wall("wall:framed", "framed", 0, 0, 6, 0),
        _wall("wall:glazed", "glazed", 0, 4, 6, 4),
    )
    proposal = _proposal(model)
    assert proposal.selected.wall_id == "wall:framed"
    assert all(item.wall_id != "wall:glazed" for item in proposal.alternatives)


def test_user_placement_on_glazed_wall_is_refused():
    model = _placement_model(
        _wall("wall:framed", "framed", 0, 0, 6, 0),
        _wall("wall:glazed", "glazed", 0, 4, 6, 4),
    )
    placed = apply_equipment_proposal(model, _proposal(model))
    equipment_id = placed.electrical_equipment[0].id
    with pytest.raises(PlacementError, match="glazed wall cannot host equipment"):
        set_user_equipment_placement(
            placed, equipment_id=equipment_id,
            position=Point3(x=3, y=4, z=1.5), user_input_id="decision:on-glazing",
            wall_id="wall:glazed", space_id="space:room",
            direction=Vector3(x=0, y=-1, z=0),
        )


def test_glazed_only_level_hits_the_existing_host_walls_error():
    model = _placement_model(
        _wall("wall:glazed-north", "glazed", 0, 0, 6, 0),
        _wall("wall:glazed-south", "glazed", 0, 4, 6, 4),
    )
    with pytest.raises(PlacementError, match="host walls"):
        _proposal(model)


@pytest.mark.parametrize("relative", sorted(PINNED_ROUTED_DIGESTS))
def test_routed_fixture_models_still_match_pre_glazed_bytes(relative):
    model = BuildingModel.load(ROOT / "fixtures" / relative)
    if relative.endswith("garage-route.json"):
        old = model.routes[0]
        route, fittings = route_between_ports(
            model, old.start_port_id, old.end_port_id, old.route_type,
        )
        kept = [item for item in model.routes if item.id != old.id]
        model = replace(
            model,
            routes=tuple(sorted((*kept, route), key=lambda item: item.id)),
            route_fittings=fittings,
            circuits=tuple(replace(circuit, route_ids=tuple(
                route.id if item == old.id else item for item in circuit.route_ids
            )) for circuit in model.circuits),
            conductors=tuple(replace(conductor, route_ids=tuple(
                route.id if item == old.id else item for item in conductor.route_ids
            )) for conductor in model.conductors),
        )
    else:
        route, fittings = route_between_ports(model, "port:source", "port:load", "emt")
        model = replace(model, routes=(route,), route_fittings=fittings)
    digest = hashlib.sha256(model.to_json().encode("utf-8")).hexdigest()
    assert digest == PINNED_ROUTED_DIGESTS[relative]


def test_penalty_value_cannot_change_models_without_glazed_walls():
    model = _routing_model(
        (_wall("wall:framed", "framed", 0, 0, 6, 0),),
        start=Point3(x=0, y=0, z=1.5),
        end=Point3(x=6, y=0, z=1.5),
        start_direction=Vector3(x=1, y=0, z=0),
        end_direction=Vector3(x=-1, y=0, z=0),
    )
    small = route_between_ports(
        model, "port:source", "port:load", "emt", options=RoutingOptions(glazed_wall_penalty=1.001),
    )
    large = route_between_ports(
        model, "port:source", "port:load", "emt", options=RoutingOptions(glazed_wall_penalty=50.0),
    )
    assert small == large
    assert "route_in_glazed_wall" not in small[0].attributes
