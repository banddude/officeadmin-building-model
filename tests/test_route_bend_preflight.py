"""Synthetic known-answer bend budgets. No customer or manufacturer facts."""
from dataclasses import replace
import json
import math
from pathlib import Path

import pytest

from oabm.model import (BuildingModel, ContractError, ElectricalEquipment, Point3,
                        Polyline3D, Port, Pose, Route, RouteFitting, Vector3)
from oabm.qa.route_bends import check_route_bends, main


def model_for(points, *, elbows=True):
    pts = tuple(Point3(x=x,y=y,z=z) for x,y,z in points)
    equipment = ElectricalEquipment(id="equipment:synthetic", equipment_type="panelboard", pose=Pose(position=pts[0]))
    ports = tuple(Port(id=name, owner_id=equipment.id, domain="power", role="connection",
                      pose=Pose(position=p), direction=Vector3(x=1,y=0,z=0))
                  for name,p in (("port:start",pts[0]),("port:end",pts[-1])))
    route = Route(id="route:synthetic",route_type="conduit",start_port_id=ports[0].id,
                  end_port_id=ports[1].id,centerline=Polyline3D(points=pts))
    model = BuildingModel(model_id="model:bend-preflight", electrical_equipment=(equipment,), ports=ports, routes=(route,))
    if elbows:
        bends = check_route_bends(model,radii_m={}).bends
        fittings = tuple(RouteFitting(id=f"fitting:{b.vertex_index}",route_id=route.id,
                                     fitting_type="elbow",pose=Pose(position=pts[b.vertex_index]),
                                     angle_radians=b.angle_radians) for b in bends)
        model = replace(model,route_fittings=fittings,routes=(replace(route,fitting_ids=tuple(f.id for f in fittings)),))
    return model


def report(model,radius=.3):
    return check_route_bends(model,radii_m={"route:synthetic":radius})


def codes(result):
    return [d.code for d in result.diagnostics]


def test_right_angle_known_answer_and_immutable():
    model = model_for(((0,0,0),(2,0,0),(2,2,0)))
    before = model.to_json()
    result = report(model)
    assert result.ready
    assert result.bends[0].angle_radians == pytest.approx(math.pi/2)
    assert result.bends[0].tangent_setback_m == pytest.approx(.3)
    assert model.to_json() == before
    assert result.to_json() == report(model).to_json()


def test_shared_straight_charges_both_bends():
    model = model_for(((0,0,0),(2,0,0),(2,.5,0),(4,.5,0)))
    result = report(model)
    assert codes(result) == ["short_straight"]
    assert result.diagnostics[0].index == 1
    assert result.diagnostics[0].required_m == pytest.approx(.6)
    assert result.diagnostics[0].available_m == .5


def test_exact_fit_and_collinear_subdivision():
    model = model_for(((0,0,0),(.1,0,0),(2,0,0),(2,.3,0),(2,.6,0),(4,.6,0)))
    assert report(model).ready
    assert [b.vertex_index for b in report(model).bends] == [2,4]


def test_3d_oblique_45_degree_turn():
    model = model_for(((0,0,0),(2,0,0),(3,0,1)))
    result = report(model)
    assert result.ready
    assert result.bends[0].angle_radians == pytest.approx(math.pi/4)
    assert result.bends[0].tangent_setback_m == pytest.approx(.3*math.tan(math.pi/8))


def test_reversal_and_unknown_radius_never_ready():
    model = model_for(((0,0,0),(2,0,0),(1,0,0)))
    assert "route_reversal" in codes(report(model))
    assert check_route_bends(model,radii_m={}).bends[0].tangent_setback_m is None
    assert "radius_unknown" in codes(check_route_bends(model,radii_m={}))


def test_elbow_missing_angle_wrong_duplicate_and_off_turn():
    model = model_for(((0,0,0),(2,0,0),(2,2,0)),elbows=False)
    assert codes(report(model)) == ["elbow_missing"]
    model = model_for(((0,0,0),(2,0,0),(2,2,0)))
    fitting = model.route_fittings[0]
    wrong = replace(model,route_fittings=(replace(fitting,angle_radians=.2),))
    assert codes(report(wrong)) == ["elbow_angle_mismatch"]
    off = replace(model,route_fittings=(replace(fitting,pose=Pose(position=Point3(x=0,y=0,z=0))),))
    assert set(codes(report(off))) == {"elbow_off_turn","elbow_missing"}
    duplicate = replace(fitting,id="fitting:duplicate")
    model = replace(model,route_fittings=(fitting,duplicate),routes=(replace(model.routes[0],fitting_ids=(fitting.id,duplicate.id)),))
    assert codes(report(model)) == ["elbow_ambiguous"]


@pytest.mark.parametrize("radius",[0,-1,True,float("nan"),float("inf"),".3"])
def test_invalid_radius(radius):
    with pytest.raises(ContractError):
        report(model_for(((0,0,0),(2,0,0))),radius)


def test_unknown_route_and_invalid_tolerance():
    model = model_for(((0,0,0),(2,0,0)))
    with pytest.raises(ContractError):
        check_route_bends(model,radii_m={"typo":.3})
    for value in (0,-1,True,float("nan")):
        with pytest.raises(ContractError):
            check_route_bends(model,radii_m={},tolerance_m=value)


def test_straight_and_nearby_future_port_dont_invent_connections():
    model = model_for(((0,0,0),(2,0,0)))
    future = replace(model.ports[0],id="port:future",role="future",pose=Pose(position=Point3(x=1,y=0,z=0)))
    model = replace(model,ports=(*model.ports,future))
    before = model.to_json()
    assert check_route_bends(model,radii_m={}).ready
    assert model.to_json() == before
    assert not future.connected_port_ids


def test_fixture_cli_and_determinism(tmp_path,capsys):
    fixture = Path(__file__).parents[1]/"fixtures/routing/bend-preflight"
    for name,status in (("valid",0),("short-offset",1)):
        assert main([str(fixture/f"{name}.json"),str(fixture/"radii.json")]) == status
        payload = capsys.readouterr().out
        assert payload == (fixture/f"{name}.expected.json").read_text()
    bad = tmp_path/"radii.json"
    bad.write_text('[]')
    with pytest.raises(SystemExit) as exc:
        main([str(fixture/"valid.json"),str(bad)])
    assert exc.value.code == 2


def test_endpoint_budget_and_missing_angle():
    model = model_for(((0,0,0),(.2,0,0),(.2,2,0)))
    assert codes(report(model)) == ["short_straight"]
    fitting = replace(model.route_fittings[0],angle_radians=None)
    assert "elbow_angle_mismatch" in codes(report(replace(model,route_fittings=(fitting,))))


def test_non_elbow_fitting_is_not_mistaken_for_an_elbow():
    model = model_for(((0,0,0),(2,0,0),(2,2,0)))
    tee = replace(model.route_fittings[0],fitting_type="tee")
    assert codes(report(replace(model,route_fittings=(tee,)))) == ["elbow_missing"]


def test_router_produced_inventory_passes_with_explicit_radius():
    from test_routing import _base_model
    from oabm.routing import route_between_ports
    base = _base_model(end=Point3(x=4,y=2,z=0))
    route, fittings = route_between_ports(base,"port:source","port:load","conduit")
    model = replace(base,routes=(route,),route_fittings=fittings)
    result = check_route_bends(model,radii_m={route.id:.01})
    assert result.bends
    assert result.ready


def test_input_order_does_not_change_report():
    model = model_for(((0,0,0),(2,0,0),(2,.5,0),(4,.5,0)))
    assert report(model).to_json() == report(replace(model,ports=model.ports[::-1],
        route_fittings=model.route_fittings[::-1])).to_json()


def test_one_elbow_cannot_satisfy_two_visits_to_same_position():
    model = model_for(((0,0,0),(1,0,0),(1,1,0),(1,0,0),(2,0,0)))
    fittings = tuple(f for f in model.route_fittings if f.id != "fitting:3")
    model = replace(model,route_fittings=fittings,routes=(replace(model.routes[0],
        fitting_ids=tuple(f.id for f in fittings)),))
    assert "elbow_reused" in codes(report(model))


def test_report_records_tolerances():
    model = model_for(((0,0,0),(2,0,0)))
    result = check_route_bends(model,radii_m={},tolerance_m=.001,angle_tolerance_rad=.002)
    payload = json.loads(result.to_json())
    assert payload["tolerance_m"] == .001
    assert payload["angle_tolerance_rad"] == .002
    with pytest.raises(ContractError):
        check_route_bends(model,radii_m=[])


def test_named_elbow_type_must_agree_with_turn():
    model = model_for(((0,0,0),(2,0,0),(2,2,0)))
    fitting = replace(model.route_fittings[0],fitting_type="elbow-45")
    assert codes(report(replace(model,route_fittings=(fitting,)))) == ["elbow_type_mismatch"]


def consolidated_corner(points=((0,0,0),(2,0,0),(2,2,0))):
    from oabm.model import validate_model
    from oabm.routing import consolidate_bundled_routes

    model = model_for(points)
    original = model.routes[0]
    second_fitting = replace(model.route_fittings[0],id="fitting:second",
                             route_id="route:second")
    second_route = replace(original,id="route:second",fitting_ids=(second_fitting.id,))
    model = replace(model,routes=(original,second_route),
                    route_fittings=(*model.route_fittings,second_fitting))
    assert check_route_bends(model,radii_m={r.id:.3 for r in model.routes}).ready

    consolidated = consolidate_bundled_routes(model).model
    validate_model(consolidated)
    assert len(consolidated.route_fittings) == len(points)-2
    elbow = consolidated.route_fittings[0]
    owner = next(r for r in consolidated.routes if r.id == elbow.route_id)
    assert elbow.fitting_type == "elbow-90"
    assert len(owner.centerline.points) == 2
    assert elbow.pose.position in owner.centerline.points
    return consolidated


def test_valid_consolidated_corner_charges_both_endpoint_legs():
    consolidated = consolidated_corner()
    before = consolidated.to_json()
    result = check_route_bends(consolidated,radii_m={r.id:.3 for r in consolidated.routes})
    assert result.ready
    assert len(result.bends) == 1  # One physical elbow, two charged legs.
    assert result.bends[0].tangent_setback_m == pytest.approx(.3)
    assert result.bends[0].angle_radians == pytest.approx(math.pi/2)
    assert consolidated.to_json() == before  # Keep the valid fitting intact.


@pytest.mark.parametrize("points", [((0,0,0),(.2,0,0),(.2,2,0)),
                                   ((0,0,0),(2,0,0),(2,.2,0)),
                                   ((0,0,0),(.2,0,0),(.2,.2,0))])
def test_consolidated_corner_checks_short_leg_on_either_side(points):
    model = consolidated_corner(points)
    result = check_route_bends(model,radii_m={r.id:.3 for r in model.routes})
    short = [d for d in result.diagnostics if d.code == "short_straight"]
    assert len(short) == (2 if points[1][0] == .2 and points[2][1] == .2 else 1)
    assert all(d.available_m == pytest.approx(.2) and d.required_m == pytest.approx(.3)
               for d in short)
    assert not result.ready


def test_consolidated_corner_unknown_mismatched_radius_and_angle_refuse():
    model = consolidated_corner()
    assert "radius_unknown" in codes(check_route_bends(model,radii_m={model.routes[0].id:.3}))
    assert "junction_radius_mismatch" in codes(check_route_bends(
        model,radii_m={r.id:.3+i*.1 for i,r in enumerate(model.routes)}))
    wrong = replace(model,route_fittings=(replace(model.route_fittings[0],angle_radians=.2),))
    assert "elbow_angle_mismatch" in codes(check_route_bends(wrong,radii_m={r.id:.3 for r in wrong.routes}))
    wrong = replace(model,route_fittings=(replace(model.route_fittings[0],fitting_type="elbow-45"),))
    assert "elbow_type_mismatch" in codes(check_route_bends(wrong,radii_m={r.id:.3 for r in wrong.routes}))


def test_consolidated_adjacent_corners_charge_shared_straight_twice():
    model = consolidated_corner(((0,0,0),(2,0,0),(2,.5,0),(4,.5,0)))
    result = check_route_bends(model,radii_m={r.id:.3 for r in model.routes})
    assert len(result.bends) == 2
    short = [d for d in result.diagnostics if d.code == "short_straight"]
    assert len(short) == 1
    assert short[0].available_m == pytest.approx(.5)
    assert short[0].required_m == pytest.approx(.6)


def test_consolidated_corner_third_coincident_leg_is_ambiguous():
    model = consolidated_corner()
    original = model.routes[0]
    duplicate = replace(original,id="route:ambiguous",fitting_ids=())
    model = replace(model,routes=(*model.routes,duplicate))
    result = check_route_bends(model,radii_m={r.id:.3 for r in model.routes})
    assert "junction_ambiguous" in codes(result)
    assert not result.ready
