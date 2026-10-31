"""Public synthetic invariants for opt-in canonical physical routes, not safety certification."""

from decimal import Decimal, localcontext

import pytest

from oabm.model import ContractError
from oabm.model.physical_routes import exact_ticks


@pytest.mark.parametrize("value, expected", [(0, 0), (0.3, 300_000_000), (1e-9, 1), (-1000, -1_000_000_000_000), (999.999999999, 999_999_999_999)])
def test_exact_decimal_ticks(value, expected):
    assert exact_ticks(value, "test") == expected


@pytest.mark.parametrize("value", [True, "0.3", float("inf"), float("nan"), 1000.000000001, -1000.000000001, 0.30000000000000004, Decimal("0.30000000000000001"), Decimal("0.0000000001")])
def test_no_coercion_or_snapping(value):
    with pytest.raises(ContractError):
        exact_ticks(value, "test")


def test_exact_grid_does_not_depend_on_decimal_context():
    with localcontext() as context:
        context.prec = 2
        assert exact_ticks(Decimal("999.999999999"), "test") == 999_999_999_999
        with pytest.raises(ContractError):
            exact_ticks(Decimal("0.3000000000000000000000000000000001"), "test")


from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import copy
import itertools
import json

from jsonschema import Draft202012Validator
from oabm.model import (
    BuildingModel, LineSegment3D, PhysicalRouteJoint, QuarterArc3D, Point3, Pose,
    Vector3, Provenance, Obstacle, Box3D, Size3, RouteFitting, Level, Wall, Polyline3D,
    PhysicalGeometryCoverage, compute_physical_source_sha256,
    compute_physical_request_sha256, validate_physical_context_source,
    physical_segment_facts, physical_box_bounds,
    upgrade_to_v1_1, upgrade_to_v1_2, upgrade_to_v1_3, upgrade_to_v1_4,
    upgrade_to_v1_5, upgrade_to_v1_6, upgrade_to_v1_7,
)
from physical_route_fixture import make_physical_model, make_physical_source, make_physical_route, supplied

ROOT = Path(__file__).resolve().parents[1]


def rebind(route, **context_changes):
    context = replace(route.context, **context_changes)
    context = replace(context, request_sha256=compute_physical_request_sha256(context, route.requirements))
    return replace(route, context=context)


def make_arc_model(transform=lambda p: p):
    coords = ((0,0,0),(.7,0,0),(1,.3,0),(1,1,0))
    points = tuple(Point3(**dict(zip("xyz", transform(p)))) for p in coords)
    source = make_physical_source()
    start_direction = Vector3(**dict(zip("xyz", transform((1,0,0)))))
    end_direction = Vector3(**dict(zip("xyz", transform((0,-1,0)))))
    source = replace(source,
        ports=(replace(source.ports[0],pose=Pose(position=points[0]),direction=start_direction),
               replace(source.ports[1],pose=Pose(position=points[-1]),direction=end_direction)),
        electrical_devices=(replace(source.electrical_devices[0],pose=Pose(position=points[0])),
                            replace(source.electrical_devices[1],pose=Pose(position=points[-1]))))
    base = rebind(make_physical_route(), source_model_sha256=compute_physical_source_sha256(source))
    joints = tuple(PhysicalRouteJoint(id=f"joint:arc-{i}",route_id=base.id,position=p,provenance=supplied("position"))
                   for i,p in enumerate(points))
    normal = transform((0,0,1))
    positive = next(v for v in normal if v)
    normal = tuple(v*positive for v in normal)
    segments = (
        LineSegment3D(id="segment:before",route_id=base.id,start_joint_id=joints[0].id,end_joint_id=joints[1].id,provenance=supplied()),
        QuarterArc3D(id="segment:arc",route_id=base.id,start_joint_id=joints[1].id,end_joint_id=joints[2].id,
                     plane_normal=Vector3(**dict(zip("xyz",normal))),
                     turn="positive_quarter" if positive==1 else "negative_quarter",provenance=supplied()),
        LineSegment3D(id="segment:after",route_id=base.id,start_joint_id=joints[2].id,end_joint_id=joints[3].id,provenance=supplied()),
    )
    route=replace(base,joints=joints,segments=segments)
    return replace(source,routes=(route,))


def rotations():
    for permutation in itertools.permutations(range(3)):
        inversions=sum(permutation[i]>permutation[j] for i in range(3) for j in range(i+1,3))
        for signs in itertools.product((-1,1),repeat=3):
            if (-1)**inversions * signs[0]*signs[1]*signs[2] == 1:
                yield lambda p, permutation=permutation,signs=signs: tuple(signs[i]*p[permutation[i]] for i in range(3))


def test_all_24_cardinal_rotations_exact_tangency_radius_roundtrip():
    transforms=list(rotations())
    assert len(transforms)==24
    for transform in transforms:
        model=make_arc_model(transform)
        route=model.routes[0]
        facts=physical_segment_facts(route)
        assert facts[1]["radius_ticks"]==300_000_000
        expected_centre=transform((700_000_000,300_000_000,0))
        assert facts[1]["centre_ticks"]==expected_centre
        assert facts[1]["start_tangent"]==transform((1,0,0))
        assert facts[1]["end_tangent"]==transform((0,1,0))
        restored=BuildingModel.from_json(model.to_json())
        assert restored==model
        assert physical_segment_facts(restored.routes[0])==facts


def test_schema_and_semantic_reader_accept_same_physical_fixture():
    # Decimal-aware validator avoids binary-float multipleOf artifacts; schema
    # itself states an exact JSON decimal number contract.
    schema=json.loads((ROOT/"contracts/oabm-model-v1.7.schema.json").read_text(),parse_float=Decimal)
    Draft202012Validator.check_schema(schema)
    model=make_arc_model()
    document=json.loads(model.to_json(),parse_float=Decimal)
    assert not list(Draft202012Validator(schema).iter_errors(document))
    assert BuildingModel.from_json(model.to_json())==model


@pytest.mark.parametrize("path", [
    ("geometry_kind",), ("segments",0,"kind"), ("requirements","profile"), ("context","kind"),
    ("context","profile"), ("context","objective","kind"),
])
def test_required_discriminators_cannot_be_missing_or_unknown(path):
    model=make_physical_model()
    for operation in ("missing","unknown"):
        document=model.to_dict()
        cursor=document["routes"][0]
        for key in path[:-1]: cursor=cursor[key]
        if operation=="missing": cursor.pop(path[-1])
        else: cursor[path[-1]]="unrecognized"
        with pytest.raises(ContractError): BuildingModel.from_dict(document)


def test_shadow_polyline_is_not_accepted():
    document=make_physical_model().to_dict()
    document["routes"][0]["centerline"]={"kind":"polyline3d","points":[{"x":0,"y":0,"z":0},{"x":2,"y":0,"z":0}]}
    with pytest.raises(ContractError,match="unknown field"): BuildingModel.from_dict(document)


def test_original_json_lexeme_rejected_before_binary_rounding():
    payload=make_physical_model().to_json()
    assert '"centerline_radius_m": 0.3' in payload
    payload=payload.replace('"centerline_radius_m": 0.3','"centerline_radius_m": 0.30000000000000001')
    with pytest.raises(ContractError,match="nanometre"): BuildingModel.from_json(payload)
    # The mapping API explicitly cannot recover a lexeme already rounded by its caller.
    assert BuildingModel.from_dict(json.loads(payload))==make_physical_model()


def test_port_raw_lexeme_cannot_be_erased():
    document=make_physical_model().to_dict()
    payload=json.dumps(document).replace('"x": 2.0','"x": 2.00000000000000001')
    with pytest.raises(ContractError,match="nanometre"): BuildingModel.from_json(payload)


def test_one_nanometre_gap_has_no_tolerance_permission():
    model=make_physical_model(); route=model.routes[0]
    joints=(replace(route.joints[0],position=Point3(x=1e-9,y=0,z=0)),route.joints[1])
    with pytest.raises(ContractError,match="exactly"):
        replace(model,routes=(replace(route,joints=joints),))


@pytest.mark.parametrize("direction", [Vector3(x=2,y=0,z=0),Vector3(x=1,y=1e-9,z=0),Vector3(x=-1,y=0,z=0)])
def test_terminal_direction_is_exact_and_oriented(direction):
    model=make_physical_model()
    with pytest.raises(ContractError,match="direction"):
        replace(model,ports=(replace(model.ports[0],direction=direction),model.ports[1]))


def test_nested_global_ids_and_scope_ownership():
    model=make_physical_model(); route=model.routes[0]
    with pytest.raises(ContractError,match="duplicate entity id"):
        replace(model,levels=(Level(id=route.joints[0].id,elevation_m=0),))
    with pytest.raises(ContractError,match="another route"):
        replace(route,joints=(replace(route.joints[0],route_id="route:other"),route.joints[1]))
    with pytest.raises(ContractError,match="scope path"):
        bad=replace(route.joints[0],provenance=supplied("imaginary"))
        replace(model,routes=(replace(route,joints=(bad,route.joints[1])),))
    with pytest.raises(ContractError,match="field-qualified"):
        replace(route.requirements,provenance=supplied())


def test_half_tick_box_extrema_are_exact_rational_not_snapped():
    box=Box3D(pose=Pose(position=Point3(x=0,y=0,z=0)),size=Size3(x=1e-9,y=1e-9,z=1e-9))
    lo,hi=physical_box_bounds(box)
    assert lo==(Fraction(-1,2),)*3
    assert hi==(Fraction(1,2),)*3


def test_context_binding_detects_source_change_and_does_not_certify_geometry():
    source=make_physical_source(); route=make_physical_route(source)
    assert validate_physical_context_source(source,route) == "canonical_python_snapshot"
    changed=replace(source,name="changed snapshot")
    with pytest.raises(ContractError,match="stale"):
        validate_physical_context_source(changed,route)
    obstruction=Obstacle(id="obstacle:new",geometry=Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    changed=replace(source,obstacles=(obstruction,))
    with pytest.raises(ContractError,match="stale"):
        validate_physical_context_source(changed,route)
    # Forging a fresh source hash cannot hide the missing inventory entry.
    rebound=rebind(route,source_model_sha256=compute_physical_source_sha256(changed))
    with pytest.raises(ContractError,match="incomplete"):
        validate_physical_context_source(changed,rebound)


def test_exclusion_cannot_remove_mandatory_blocker():
    source=make_physical_source()
    obstacle=Obstacle(id="obstacle:a",geometry=Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    source=replace(source,obstacles=(obstacle,))
    route=make_physical_route(source)
    row=PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision="outside_proof_scope",blocker_entity_ids=(),
        rationale="Synthetic prohibited exclusion",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    route=rebind(route,coverage=(row,))
    with pytest.raises(ContractError,match="cannot be excluded"):
        validate_physical_context_source(source,route)


def test_structural_binding_is_not_obstacle_safety():
    source=make_physical_source()
    obstacle=Obstacle(id="obstacle:collision",geometry=Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    source=replace(source,obstacles=(obstacle,))
    route=make_physical_route(source)
    coverage=PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision="direct_blocker",blocker_entity_ids=(obstacle.id,),
        rationale="Supplied obstacle",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    route=rebind(route,coverage=(coverage,))
    assert validate_physical_context_source(source,route) == "canonical_python_snapshot"
    # A route crossing this blocker remains structurally valid. No verified flag/API exists.
    assert BuildingModel.from_json(replace(source,routes=(route,)).to_json()).routes[0]==route


@pytest.mark.parametrize("upgrade",[upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3,upgrade_to_v1_4,upgrade_to_v1_5,upgrade_to_v1_6])
def test_all_older_upgrades_refuse_17_even_empty(upgrade):
    for model in (make_physical_source(),make_physical_model()):
        with pytest.raises(ContractError,match="cannot downgrade version 1.7"): upgrade(model)


def test_legacy_upgrade_preserves_old_routes_and_bytes():
    source=BuildingModel.load(ROOT/"fixtures/model/v1/garage-route.json")
    before=source.to_json()
    upgraded=upgrade_to_v1_7(source)
    assert upgraded.routes==source.routes
    assert upgraded.to_dict()["routes"]==source.to_dict()["routes"]
    assert source.to_json()==before
    assert BuildingModel.from_json(upgraded.to_json())==upgraded
    for schema in ("v1","v1.1","v1.2","v1.3","v1.4","v1.5","v1.6"):
        original=json.loads((ROOT/f"contracts/oabm-model-{schema}.schema.json").read_text())
        assert original["properties"]["schema_version"]["const"]!="1.7.0"


def test_legacy_fitting_reference_refuses_physical_route():
    model=make_physical_model()
    fitting=RouteFitting(id="fitting:legacy",route_id=model.routes[0].id,fitting_type="coupling",pose=Pose(position=Point3(x=1,y=0,z=0)))
    with pytest.raises(ContractError,match="legacy fitting/selection"):
        replace(model,route_fittings=(fitting,))


@pytest.mark.parametrize("name,value", [
    ("outside_diameter_m",.000999999),("outside_diameter_m",1.000000001),
    ("centerline_radius_m",.000999999),("centerline_radius_m",10.000000001),
    ("clearance_m",-1e-9),("clearance_m",100.000000001),
    ("start_straight_min_m",-1e-9),("start_straight_min_m",2000.000000001),
    ("end_straight_min_m",-1e-9),("end_straight_min_m",2000.000000001),
    ("between_bends_straight_min_m",-1e-9),("between_bends_straight_min_m",2000.000000001),
])
def test_each_requirement_scalar_bound(name,value):
    requirements=make_physical_model().routes[0].requirements
    with pytest.raises(ContractError): replace(requirements,**{name:value})


def test_radius_tube_interior_and_straight_minima():
    route=make_physical_model().routes[0]
    with pytest.raises(ContractError,match="outside radius"):
        replace(route.requirements,outside_diameter_m=.6)
    req=replace(route.requirements,start_straight_min_m=1.5,end_straight_min_m=1.5)
    ctx=replace(route.context,request_sha256=compute_physical_request_sha256(route.context,req))
    assert replace(route,requirements=req,context=ctx).segments  # max, not sum
    req=replace(req,end_straight_min_m=2.000000001)
    with pytest.raises(ContractError,match="minimum"):
        replace(route,requirements=req,context=ctx)


def test_domain_bounds_empty_and_extrema():
    route=make_physical_model().routes[0]
    with pytest.raises(ContractError,match="1..128"):
        replace(route.context.allowed_domain,volumes=())
    with pytest.raises(ContractError,match="1..128"):
        replace(route.context.allowed_domain,volumes=route.context.allowed_domain.volumes*129)
    with pytest.raises(ContractError,match="extrema"):
        physical_box_bounds(Box3D(pose=Pose(position=Point3(x=1000,y=0,z=0)),size=Size3(x=1e-9,y=1,z=1)))


def test_no_bare_context_digest_authority_or_shadow_geometry():
    route=make_physical_model().routes[0]
    with pytest.raises(ContractError,match="digest"):
        replace(route,context=replace(route.context,request_sha256="0"*64))
    with pytest.raises(ContractError,match="shadow"):
        replace(route,attributes={"centerline":{"points":[]}})
    assert replace(route,attributes={"claimed_verified":True}).context==route.context


def test_legacy_offgrid_17_remains_losslessly_legacy():
    source=make_physical_source()
    source=replace(source,ports=(replace(source.ports[0],pose=Pose(position=Point3(x=.30000000000000004,y=0,z=0))),source.ports[1]))
    loaded=BuildingModel.from_json(source.to_json())
    assert loaded.ports[0].pose.position.x==.30000000000000004


def test_source_raw_lexemes_and_python_admission_modes():
    source=make_physical_source()
    obstacle=Obstacle(id="obstacle:raw",geometry=Box3D(pose=Pose(position=Point3(x=.3,y=2,z=0)),size=Size3(x=.1,y=.1,z=.1)))
    source=replace(source,obstacles=(obstacle,))
    route=make_physical_route(source)
    coverage=PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision="direct_blocker",blocker_entity_ids=(obstacle.id,),
        rationale="Exact byte admission",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    route=rebind(route,coverage=(coverage,))
    assert validate_physical_context_source(source,route,source_json=source.to_json())=="raw_json_lexemes"
    altered=source.to_json().replace('"x": 0.3','"x": 0.30000000000000001')
    assert BuildingModel.from_json(altered)==source  # legacy-only ordinary load unchanged
    with pytest.raises(ContractError,match="nanometre"):
        validate_physical_context_source(source,route,source_json=altered)
    assert validate_physical_context_source(source,route)=="canonical_python_snapshot"
    different=replace(source,name="different")
    with pytest.raises(ContractError,match="differs"):
        validate_physical_context_source(source,route,source_json=different.to_json())


def test_missing_wall_coverage_and_same_id_source_geometry_change():
    source=make_physical_source()
    source=replace(source,levels=(Level(id="level:a",elevation_m=0),),
        walls=(Wall(id="wall:a",level_id="level:a",centerline=Polyline3D(points=(Point3(x=0,y=2,z=0),Point3(x=2,y=2,z=0))),
                    thickness_m=.1,height_m=3),))
    route=make_physical_route(source)
    with pytest.raises(ContractError,match="incomplete"):
        validate_physical_context_source(source,route)
    changed=replace(source,walls=(replace(source.walls[0],height_m=4),))
    with pytest.raises(ContractError,match="stale"):
        validate_physical_context_source(changed,route)


def test_exact_source_hash_normalizes_integer_typed_floats_without_changing_old_bytes():
    source=make_physical_source()
    integer=replace(source,ports=(replace(source.ports[0],direction=Vector3(x=1,y=0,z=0)),source.ports[1]))
    floating=replace(source,ports=(replace(source.ports[0],direction=Vector3(x=1.0,y=0.0,z=0.0)),source.ports[1]))
    assert integer.to_json()!=floating.to_json()
    assert compute_physical_source_sha256(integer)==compute_physical_source_sha256(floating)


def test_route_type_is_bound_and_source_upgrade_is_explicit():
    route=make_physical_model().routes[0]
    with pytest.raises(ContractError,match="route_type"):
        replace(route,route_type="pvc")
    with pytest.raises(ContractError,match="explicitly upgraded"):
        replace(route.context,source_model_schema_version="1.6.0")
    assert compute_physical_request_sha256(replace(route.context,route_type="pvc"),route.requirements)!=route.context.request_sha256


@pytest.mark.parametrize("name,maximum",[
    ("candidate_routes",128),("candidate_grid_nodes",200000),("pushed_search_states",500000),
    ("interval_refinement_depth",64),("total_refinement_cells",1000000),
])
def test_execution_limits_are_explicit_positive_integers(name,maximum):
    limits=make_physical_model().routes[0].context.execution_limits
    for value in (0,maximum+1,True,1.5):
        with pytest.raises(ContractError):replace(limits,**{name:value})


def test_arc_wrong_radius_kink_reused_joint_and_adjacent_lines_fail():
    model=make_arc_model();route=model.routes[0]
    joints=list(route.joints)
    joints[2]=replace(joints[2],position=Point3(x=1.000000001,y=.3,z=0))
    with pytest.raises(ContractError,match="equal nonzero"):
        replace(route,joints=tuple(joints))
    arc=replace(route.segments[1],turn="negative_quarter")
    with pytest.raises(ContractError,match="tangents"):
        replace(route,segments=(route.segments[0],arc,route.segments[-1]))
    with pytest.raises(ContractError,match="unrepeated"):
        replace(route,joints=(route.joints[0],route.joints[1],route.joints[1],route.joints[3]))
    straight=make_physical_model().routes[0]
    mid=PhysicalRouteJoint(id="joint:mid",route_id=straight.id,position=Point3(x=1,y=0,z=0),provenance=supplied())
    a=replace(straight.segments[0],end_joint_id=mid.id)
    b=replace(straight.segments[0],id="segment:extra",start_joint_id=mid.id)
    with pytest.raises(ContractError,match="coalesced"):
        replace(straight,joints=(straight.joints[0],mid,straight.joints[1]),segments=(a,b))


def test_profile_counts_and_objective_bounds():
    route=make_physical_model().routes[0]
    with pytest.raises(ContractError,match="1..1024"):
        replace(route,segments=route.segments*1025)
    with pytest.raises(ContractError,match="1..1024"):
        replace(route,segments=())
    for value in (-1e-9,1000.000000001):
        with pytest.raises(ContractError):
            replace(route.context.objective,bend_penalty_m=value)
    coverage=PhysicalGeometryCoverage(source_entity_id="obstacle:x",decision="direct_blocker",blocker_entity_ids=("obstacle:x",),
        rationale="Synthetic count test",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    with pytest.raises(ContractError,match="10000"):
        replace(route.context,coverage=(coverage,)*10001)


def test_direct_structural_source_identity_and_zero_extra_clearance_policy():
    from oabm.model import StructuralMember
    source=make_physical_source()
    member=StructuralMember(id="member:a",kind="beam",level_id="level:a",
        pose=Pose(position=Point3(x=0,y=3,z=0)),size=Size3(x=1,y=.1,z=.1))
    source=replace(source,levels=(Level(id="level:a",elevation_m=0),),structural_members=(member,))
    route=make_physical_route(source)
    coverage=PhysicalGeometryCoverage(source_entity_id=member.id,decision="direct_blocker",blocker_entity_ids=(member.id,),
        rationale="Supplied rectangular member",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    route=rebind(route,coverage=(coverage,))
    assert validate_physical_context_source(source,route,source_json=source.to_json())=="raw_json_lexemes"


def test_active_constraints_never_silently_ignore_soft_unknown_or_wildcard():
    from oabm.model import RouteConstraint
    box=Box3D(pose=Pose(position=Point3(x=0,y=3,z=0)),size=Size3(x=1,y=1,z=1))
    for hard,kind in ((False,"preferred_corridor"),(True,"required_corridor"),(True,"unknown")):
        source=replace(make_physical_source(),route_constraints=(RouteConstraint(id="constraint:a",constraint_type=kind,
            geometry=box,hard=hard,applies_to=("*",)),))
        route=make_physical_route(source)
        with pytest.raises(ContractError,match="unsupported applicable"):
            validate_physical_context_source(source,route)


def test_resolved_blocker_limit_is_checked_without_omission():
    source=make_physical_source()
    box=Box3D(pose=Pose(position=Point3(x=0,y=3,z=0)),size=Size3(x=.1,y=.1,z=.1))
    obstacles=tuple(Obstacle(id=f"obstacle:limit-{i}",geometry=box) for i in range(4097))
    source=replace(source,obstacles=obstacles)
    route=make_physical_route(source)
    provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale")
    rows=tuple(PhysicalGeometryCoverage(source_entity_id=o.id,decision="direct_blocker",blocker_entity_ids=(o.id,),
                                       rationale="Synthetic count boundary",provenance=provenance) for o in obstacles)
    route=rebind(route,coverage=rows)
    with pytest.raises(ContractError,match="4096 resolved"):
        validate_physical_context_source(source,route)


def test_geometry_inventory_covers_dimensioned_fitting_envelopes():
    # Inventory's enumeration is tested independently of fitting producer
    # fixtures. This dataclass models a future canonical collection addition.
    from dataclasses import dataclass
    from types import SimpleNamespace
    from oabm.model import physical_geometry_inventory
    @dataclass
    class Inventory:
        fitting_selections: tuple
    solid=SimpleNamespace(id="selection:solid",size=Size3(x=.1,y=.1,z=.1))
    metadata=SimpleNamespace(id="selection:metadata",size=None)
    assert physical_geometry_inventory(Inventory((solid,metadata)),"emt")=={solid.id:solid}
    @dataclass
    class Future:
        unknown_solid_collection: tuple
    with pytest.raises(ContractError,match="unknown_solid_collection"):
        physical_geometry_inventory(Future((solid,)),"emt")


def test_decimal_translated_geometry_preserves_all_ticks_through_generated_bytes():
    model=make_arc_model();route=model.routes[0]
    offset=(Decimal(".1"),Decimal(".3"),Decimal(".4"))
    def move(point):
        return Point3(**{axis:float(Decimal(str(getattr(point,axis)))+offset[i]) for i,axis in enumerate("xyz")})
    source=replace(model,routes=(),
        ports=tuple(replace(p,pose=replace(p.pose,position=move(p.pose.position))) for p in model.ports),
        electrical_devices=tuple(replace(d,pose=replace(d.pose,position=move(d.pose.position))) for d in model.electrical_devices))
    joints=tuple(replace(j,position=move(j.position)) for j in route.joints)
    context=replace(route.context,source_model_sha256=compute_physical_source_sha256(source))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,route.requirements))
    translated=replace(source,routes=(replace(route,joints=joints,context=context),))
    restored=BuildingModel.from_json(translated.to_json())
    from oabm.model.physical_routes import point_ticks
    assert tuple(point_ticks(j.position) for j in restored.routes[0].joints)==tuple(point_ticks(j.position) for j in joints)
    assert physical_segment_facts(restored.routes[0])==physical_segment_facts(translated.routes[0])


def test_malformed_physical_numeric_structure_returns_contract_error():
    source=make_physical_source();route=make_physical_route(source)
    for key,value in (("ports",None),("obstacles",[None]),("structural_members",[{}])):
        document=source.to_dict();document[key]=value
        with pytest.raises(ContractError):
            validate_physical_context_source(source,route,source_json=json.dumps(document))
    document=make_physical_model().to_dict();document["routes"][0]["context"]=None
    with pytest.raises(ContractError):BuildingModel.from_json(json.dumps(document))


def test_mutated_requirement_metadata_invalidates_binding_on_model_validation():
    from oabm.model import validate_model
    model=make_physical_model()
    model.routes[0].requirements.attributes["evidence_revision"]="changed"
    with pytest.raises(ContractError,match="digest"):validate_model(model)


@pytest.mark.parametrize("owner_path", ["route", "joint", "segment", "requirements", "domain"])
def test_mutated_physical_owner_shadow_metadata_fails_validate_and_reload(owner_path):
    from oabm.model import validate_model
    model=make_physical_model(); route=model.routes[0]
    owner={"route":route,"joint":route.joints[0],"segment":route.segments[0],
           "requirements":route.requirements,"domain":route.context.allowed_domain}[owner_path]
    owner.attributes["centerline"]={"points":[]}
    with pytest.raises(ContractError,match="shadow"):validate_model(model)
    with pytest.raises(ContractError,match="shadow"):BuildingModel.from_json(model.to_json())


def test_mutated_route_provenance_json_is_revalidated():
    from oabm.model import validate_model
    model=make_physical_model()
    model.routes[0].provenance[0].attributes["invalid"]=object()
    with pytest.raises(ContractError,match="JSON-compatible"):validate_model(model)


def test_decimal_exponent_admission_precedes_any_fraction_expansion(monkeypatch):
    import oabm.model.physical_routes as physical
    def forbidden_fraction(*args,**kwargs):
        raise AssertionError("authored grid admission must not expand a Fraction denominator")
    monkeypatch.setattr(physical,"Fraction",forbidden_fraction)
    # Constructing Decimal from this token is bounded; no rational conversion
    # or power-of-ten expansion is permitted before rejection.
    with pytest.raises(ContractError,match="nanometre"):
        physical.exact_ticks(Decimal("1e-999999999"),"tiny exponent")
    with pytest.raises(ContractError,match="nanometre"):
        physical.exact_ticks(Decimal("0.00000000001"),"small off-grid")
    assert physical.exact_ticks(Decimal("0e-999999999"),"zero")==0
    assert physical.exact_ticks(Decimal("0.3000000000000000000000"),"trailing zeros")==300000000
    assert physical.exact_ticks(Decimal("2E+3"),"bounded maximum",minimum="0",maximum="2000")==2000000000000


def test_physical_serializers_refuse_invalid_metadata_before_any_file_write(tmp_path):
    model=make_physical_model()
    model.routes[0].joints[0].attributes["centerline"]={"points":[]}
    with pytest.raises(ContractError,match="shadow"):model.to_dict()
    with pytest.raises(ContractError,match="shadow"):model.to_json()
    absent=tmp_path/"absent.json"
    with pytest.raises(ContractError,match="shadow"):model.dump(absent)
    assert not absent.exists()
    existing=tmp_path/"existing.json"
    existing.write_text("retained sentinel")
    with pytest.raises(ContractError,match="shadow"):model.dump(existing)
    assert existing.read_text()=="retained sentinel"


def test_legacy_only_serialization_does_not_add_physical_validation(monkeypatch):
    import oabm.model.model as model_module
    source=make_physical_source()
    def forbidden(_):
        raise AssertionError("legacy-only serialization must not add model revalidation")
    monkeypatch.setattr(model_module,"validate_model",forbidden)
    assert source.to_dict()["schema_version"]=="1.7.0"
    assert '"schema_version": "1.7.0"' in source.to_json()
