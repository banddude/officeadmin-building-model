"""Actual enclosed-room architecture remains the collision source."""
from dataclasses import replace
import pytest
from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
from oabm.model import validate_model
from oabm.routing import consolidate_bundled_routes, RoutingError
from test_route_bend_preflight import model_for


def room_bundle(points=((.5,1,1),(4,1,1))):
    room = synthetic_enclosed_room_assembly()
    electrical = model_for(points,elbows=False)
    route = replace(electrical.routes[0],nominal_diameter_m=.02)
    return replace(room,electrical_equipment=electrical.electrical_equipment,
        ports=electrical.ports,routes=(route,replace(route,id='route:second')))


def test_room_consolidation_preserves_architecture_and_checks_actual_children():
    model = room_bundle()
    frozen = model.to_json()
    result = consolidate_bundled_routes(model)
    validate_model(result.model)
    assert len(result.model.routes) == 1
    assert result.report.double_counted_length_m == pytest.approx(3.5)
    for field in ('walls','openings','ceilings','slabs','spaces','building_assemblies','assembly_components'):
        assert getattr(result.model,field) == getattr(model,field)
    receipt = result.model.routes[0].attributes['consolidation_collision_check']
    checked = set(receipt['checked_blocker_ids'])
    assert {part.id for part in model.assembly_components} <= checked
    assert model.slabs[0].id in checked
    assert receipt['clearance_m'] == 0
    assert model.to_json() == frozen


def test_room_consolidation_refuses_wall_collision_and_unknown_padding():
    with pytest.raises(RoutingError,match='padded hard blockers'):
        consolidate_bundled_routes(room_bundle(((1,1,1),(1,-1,1))))
    model = room_bundle()
    unknown = replace(model,routes=tuple(replace(route,nominal_diameter_m=None) for route in model.routes))
    with pytest.raises(RoutingError,match='explicit diameter'):
        consolidate_bundled_routes(unknown)


def test_room_consolidation_retains_source_bound_paths_instead_of_relabeling_receipts():
    model = room_bundle()
    model = replace(model,routes=tuple(replace(route,attributes={'geometry_check':{'status':'not_evaluated'}})
        for route in model.routes))
    result = consolidate_bundled_routes(model)
    assert result.model is model
    assert set(dict(result.report.untouched_route_reasons).values()) == {'bounded_routing_binding_requires_regeneration'}
