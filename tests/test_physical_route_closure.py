"""Systematic physical-only Python/schema/JSON closure and write-safety matrix."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.model import (
    BuildingModel, ContractError, PhysicalGeometryCoverage, Point3, Provenance,
    compute_physical_request_sha256, validate_model,
)
from physical_route_fixture import make_physical_model, supplied
from test_physical_route_contract import make_arc_model

OWNERS = ("route", "joint", "line", "arc", "requirements", "domain")


@pytest.fixture(scope="module")
def schema():
    path=Path(__file__).resolve().parents[1]/"contracts/oabm-model-v1.7.schema.json"
    return Draft202012Validator(json.loads(path.read_text(),parse_float=Decimal))


def owner_of(model,kind):
    route=model.routes[0]
    return {"route":route,"joint":route.joints[0],"line":route.segments[0],"arc":route.segments[1],
            "requirements":route.requirements,"domain":route.context.allowed_domain}[kind]


def install(model,kind,owner):
    route=model.routes[0]; requirements=route.requirements; context=route.context
    changes={}
    if kind=="route": route=owner
    elif kind=="joint": changes["joints"]=(owner,*route.joints[1:])
    elif kind=="line": changes["segments"]=(owner,*route.segments[1:])
    elif kind=="arc": changes["segments"]=(route.segments[0],owner,*route.segments[2:])
    elif kind=="requirements": requirements=owner
    elif kind=="domain": context=replace(context,allowed_domain=owner)
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    route=replace(route,requirements=requirements,context=context,**changes)
    return replace(model,routes=(route,))


def assert_closure(model,schema):
    validate_model(model)
    payload=model.to_json()
    assert not list(schema.iter_errors(json.loads(payload,parse_float=Decimal)))
    normalized=BuildingModel.from_dict(model.to_dict()).to_json()
    reparsed=BuildingModel.from_json(payload)
    assert reparsed.to_json()==normalized
    assert BuildingModel.from_json(reparsed.to_json()).to_json()==normalized


def assert_refusal_before_write(model,tmp_path):
    with pytest.raises(ContractError):validate_model(model)
    with pytest.raises(ContractError):model.to_dict()
    with pytest.raises(ContractError):model.to_json()
    missing=tmp_path/"absent.json"
    with pytest.raises(ContractError):model.dump(missing)
    assert not missing.exists()
    retained=tmp_path/"retained.json";retained.write_text("sentinel")
    with pytest.raises(ContractError):model.dump(retained)
    assert retained.read_text()=="sentinel"


@pytest.mark.parametrize("kind",OWNERS)
@pytest.mark.parametrize("change",[
    {"name":None},{"name":"Synthetic metadata"},
    {"confidence":0},{"confidence":.3},{"confidence":1},
    {"attributes":{}},{"attributes":{"note":{"flags":[True,False,None],"value":.3}}},
])
def test_all_owner_valid_metadata_closure(kind,change,schema):
    model=make_arc_model()
    model=install(model,kind,replace(owner_of(model,kind),**change))
    assert_closure(model,schema)


@pytest.mark.parametrize("kind",OWNERS)
@pytest.mark.parametrize("change",[
    {"name":42},{"name":True},{"name":[]},{"attributes":[]},{"attributes":None},
    {"attributes":{1:"not a string key"}},{"attributes":{"bad":float("nan")}},
    {"confidence":True},{"confidence":"0.5"},{"confidence":float("nan")},
    {"confidence":float("inf")},{"confidence":-.1},{"confidence":1.1},
    {"provenance":()},{"provenance":[]},{"provenance":(None,)},
])
def test_all_owner_invalid_metadata_construction_refuses(kind,change,tmp_path):
    model=make_arc_model();before=model.to_json()
    with pytest.raises(ContractError):
        install(model,kind,replace(owner_of(model,kind),**change))
    assert model.to_json()==before
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize("kind",OWNERS)
@pytest.mark.parametrize("metadata",[
    {"source_kind":"synthetic","source_id":"evidence:closure","method":None},
    {"source_kind":"synthetic","source_id":"evidence:closure","source_element_id":"detail:one","method":"supplied"},
])
def test_all_owner_valid_additional_provenance_closure(kind,metadata,schema):
    model=make_arc_model();owner=owner_of(model,kind)
    record=Provenance(**metadata,derivation="user",scope_paths=("name","attributes"),attributes={"retained":[1,"two",None]})
    updated=replace(owner,provenance=(*owner.provenance,record))
    assert_closure(install(model,kind,updated),schema)


@pytest.mark.parametrize("kind",OWNERS)
@pytest.mark.parametrize("metadata",[
    {"source_kind":True},{"source_id":True},{"source_element_id":42},{"method":[]},{"attributes":[]},
])
def test_all_owner_invalid_provenance_shape_refuses(kind,metadata):
    model=make_arc_model();owner=owner_of(model,kind)
    base=Provenance(source_kind="synthetic",source_id="evidence:closure")
    record=replace(base,**metadata)
    with pytest.raises(ContractError):install(model,kind,replace(owner,provenance=(*owner.provenance,record)))


@pytest.mark.parametrize("kind",OWNERS)
@pytest.mark.parametrize("mutation",("shadow","non_json","provenance_non_json"))
def test_all_owner_mutable_invalid_mappings_refuse_before_dump(kind,mutation,tmp_path):
    model=make_arc_model();owner=owner_of(model,kind)
    if mutation=="shadow":owner.attributes["centerline"]={"points":[]}
    elif mutation=="non_json":owner.attributes["nested"]={"bad":object()}
    else:owner.provenance[0].attributes["nested"]={"bad":object()}
    assert_refusal_before_write(model,tmp_path)


@pytest.mark.parametrize("kind",OWNERS)
def test_all_owner_valid_nested_mapping_mutation_closes_after_explicit_rebinding(kind,schema):
    model=make_arc_model();owner=owner_of(model,kind)
    owner.attributes["note"]={"nested":{"retained":[1,2,3]}}
    model=install(model,kind,owner)
    assert_closure(model,schema)


@pytest.mark.parametrize("value",[True,"0.3",None,[],float("nan"),float("inf"),-.001,0.30000000000000004])
@pytest.mark.parametrize("field",[
    "outside_diameter_m","centerline_radius_m","clearance_m","start_straight_min_m",
    "end_straight_min_m","between_bends_straight_min_m",
])
def test_each_supplied_dimension_malformed_numeric_refuses(field,value):
    model=make_physical_model();requirements=model.routes[0].requirements
    with pytest.raises(ContractError):replace(requirements,**{field:value})


@pytest.mark.parametrize("value",[True,"0.3",None,[],float("nan"),float("inf"),1000.000000001,.30000000000000004])
@pytest.mark.parametrize("axis",("x","y","z"))
def test_joint_each_coordinate_malformed_numeric_refuses(axis,value):
    model=make_physical_model();joint=model.routes[0].joints[0]
    with pytest.raises(ContractError):
        position=replace(joint.position,**{axis:value})
        replace(joint,position=position)


@pytest.mark.parametrize("value",[True,"0.3",None,[],float("nan"),float("inf"),-.1,.30000000000000004])
def test_objective_malformed_numeric_refuses(value):
    with pytest.raises(ContractError):replace(make_physical_model().routes[0].context.objective,bend_penalty_m=value)


@pytest.mark.parametrize("change",[
    {"decision":[]},{"decision":True},{"blocker_entity_ids":([],)}
])
def test_coverage_malformed_shapes_refuse_contract_error(change):
    fields=dict(source_entity_id="obstacle:a",decision="direct_blocker",blocker_entity_ids=("obstacle:a",),
                rationale="Explicit synthetic source",provenance=supplied("source_entity_id","decision","blocker_entity_ids","rationale"))
    fields.update(change)
    with pytest.raises(ContractError):PhysicalGeometryCoverage(**fields)
