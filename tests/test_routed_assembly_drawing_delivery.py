"""A real generated plenum route must survive drawing and delivery unchanged."""
from dataclasses import replace
import json

import pytest

from oabm.drawings import PlanSpec, SectionSpec, generate_plan, generate_section
from oabm.exports.model_delivery import deliver_model
from oabm.model import BuildingModel, ContractError, Point3, Vector3
from oabm.routing import derive_suspended_ceiling_route_domain, route_between_ports
from test_routing_assembly_producer_integration_356 import (
    _build_source_assembly, _with_synthetic_ports, _OPTIONS, _DIAMETER_M,
)


@pytest.fixture(scope='module')
def routed_model():
    model=_with_synthetic_ports(_build_source_assembly(),y=.3,z=2.4)
    domain=derive_suspended_ceiling_route_domain(model,model.building_assemblies[0].id,
        nominal_diameter_m=_DIAMETER_M,options=_OPTIONS)
    route,fittings=route_between_ports(model,'port:plenum-source','port:plenum-load','emt',
                                      options=_OPTIONS,allowed_domain=domain)
    return replace(model,routes=(route,),route_fittings=fittings)


@pytest.mark.parametrize('kind',['plan','section'])
def test_cut_views_retain_route_receipts_and_component_identity(routed_model,kind):
    model=routed_model;before=model.to_json()
    if kind=='plan':
        spec=PlanSpec(id='P',level_id=model.levels[0].id,cut_height_m=2.4)
        generate=generate_plan
    else:
        spec=SectionSpec(id='S',origin=Point3(x=0,y=.3,z=0),direction=Vector3(x=0,y=1,z=0))
        generate=generate_section
    result=generate(model,spec,assembly_geometry='cut')
    assert any(model.routes[0].id in p.source_ids and p.layer=='electrical:routes' for p in result.primitives)
    assert dict(result.metadata)['assembly_mode']=='cut-plane-only'
    assert result==generate(model,spec,assembly_geometry='cut')
    assert model.to_json()==before
    assert BuildingModel.from_json(before)==model
    with pytest.raises(ValueError,match='populated'):
        generate(model,spec)


def test_model_delivery_includes_checked_route_and_glb_from_same_model(routed_model,tmp_path):
    model=routed_model;source=tmp_path/'model.json';source.write_text(model.to_json())
    options={'version':'model-delivery-v2','assembly_geometry':'cut','bend_policy':'skip',
             'radii_m':{},'preview_glb':True,'plans':[{'id':'P','level_id':model.levels[0].id,
             'cut_height_m':2.4,'view_depth_below_m':.25,'view_depth_above_m':.25,'scale':20}]}
    config=tmp_path/'options.json';config.write_text(json.dumps(options))
    output=deliver_model(source,config,output_directory=tmp_path/'run')
    assert (output/'inputs/model.json').read_bytes()==source.read_bytes()
    restored=BuildingModel.from_json((output/'canonical-model.json').read_bytes())
    assert restored==model
    drawings=json.loads((output/'drawing-set.json').read_bytes())
    assert model.routes[0].id in json.dumps(drawings)
    assert (output/'preview.glb').read_bytes()[:4]==b'glTF'


def test_original_model_cannot_drop_receipted_assemblies(routed_model):
    with pytest.raises(ContractError,match='stale assembly routing context'):
        replace(routed_model,building_assemblies=(),assembly_components=())


def test_request_local_edge_cache_preserves_exact_route(routed_model,monkeypatch):
    from oabm.routing import router
    model=replace(routed_model,routes=(),route_fittings=())
    domain=derive_suspended_ceiling_route_domain(model,model.building_assemblies[0].id,
        nominal_diameter_m=_DIAMETER_M,options=_OPTIONS)
    counts=[]
    original=router._point_blocked
    def counted(*args,**kwargs):
        counts.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(router,'_point_blocked',counted)
    def route():
        return route_between_ports(model,'port:plenum-source','port:plenum-load','emt',
                                   options=_OPTIONS,allowed_domain=domain)
    cached=route();cached_count=len(counts);counts.clear()
    monkeypatch.setattr(router,'lru_cache',lambda **kwargs:lambda function:function)
    assert route()==cached
    assert cached_count<len(counts)


def test_bounded_dominance_retains_exhaustive_small_case_result(routed_model,monkeypatch):
    from oabm.routing import router
    model=replace(routed_model,routes=(),route_fittings=())
    domain=derive_suspended_ceiling_route_domain(model,model.building_assemblies[0].id,
        nominal_diameter_m=_DIAMETER_M,options=_OPTIONS)
    def route():
        return route_between_ports(model,'port:plenum-source','port:plenum-load','emt',
                                   options=_OPTIONS,allowed_domain=domain)
    pruned=route()
    monkeypatch.setattr(router,'_dominated_route_state',lambda *a,**kw:False)
    assert route()==pruned
