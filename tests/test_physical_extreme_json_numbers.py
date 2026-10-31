"""Original physical numeric tokens refuse safely; legacy metadata keeps meaning."""
import json
from dataclasses import replace
from decimal import InvalidOperation, localcontext
import pytest
from oabm.model import BuildingModel, ContractError
from oabm.model.physical_route_context import validate_physical_request_source
from oabm.routing.physical_generate import generate_physical_route
from oabm.routing.physical_verify import verify_physical_route
from physical_route_fixture import make_physical_source, make_physical_route, make_physical_model

POSITIVE='1e99999999999999999999999999999999'
NEGATIVE='1e-99999999999999999999999999999999'


def raw_endpoint(source, token):
    doc=json.loads(source.to_json())
    doc['ports'][0]['pose']['position']['x']='EXACT_TOKEN'
    return json.dumps(doc).replace('"EXACT_TOKEN"',token)


@pytest.mark.parametrize('token',[POSITIVE,NEGATIVE])
@pytest.mark.parametrize('trap',[True,False])
def test_consumed_source_exponents_refuse_both_apis_and_helper(token,trap):
    source=make_physical_source();route=make_physical_route(source)
    raw=raw_endpoint(source,token)
    with localcontext() as context:
        context.prec=1;context.traps[InvalidOperation]=trap
        before=dict(context.flags)
        v=verify_physical_route(source,route,source_json=raw)
        g=generate_physical_route(source,route.context,route.requirements,source_json=raw)
        assert v.status=='unsupported_input';assert v.reason=='numeric_decimal_range'
        assert g.status=='unsupported_input';assert g.reason=='numeric_decimal_range';assert g.route is None
        with pytest.raises(ContractError):validate_physical_request_source(source,route.context,route.requirements,source_json=raw)
        assert dict(context.flags)==before


@pytest.mark.parametrize('token',[POSITIVE,NEGATIVE])
@pytest.mark.parametrize('trap',[True,False])
def test_consumed_physical_model_number_refuses_before_float_conversion(token,trap):
    raw=make_physical_model().to_json().replace('"centerline_radius_m": 0.3','"centerline_radius_m": '+token)
    with localcontext() as context:
        context.traps[InvalidOperation]=trap;before=dict(context.flags)
        with pytest.raises(ContractError):BuildingModel.from_json(raw)
        assert dict(context.flags)==before


@pytest.mark.parametrize('version',[f'1.{minor}.0' for minor in range(8)])
@pytest.mark.parametrize('trap',[True,False])
def test_unconsumed_legacy_metadata_retains_ordinary_underflow(version,trap):
    model=BuildingModel(model_id='model:legacy-extreme',schema_version=version,attributes={'legacy_numeric_annotation':0.0})
    raw=model.to_json().replace('"legacy_numeric_annotation": 0.0','"legacy_numeric_annotation": '+NEGATIVE)
    with localcontext() as context:
        context.traps[InvalidOperation]=trap;before=dict(context.flags)
        decoded=BuildingModel.from_json(raw)
        assert decoded.attributes['legacy_numeric_annotation']==0.0
        assert decoded.to_json()==model.to_json()
        assert dict(context.flags)==before


def test_unconsumed_metadata_in_physical_document_retains_legacy_semantics():
    model=replace(make_physical_model(),attributes={'legacy_numeric_annotation':0.0})
    raw=model.to_json().replace('"legacy_numeric_annotation": 0.0','"legacy_numeric_annotation": '+NEGATIVE)
    assert BuildingModel.from_json(raw).to_json()==BuildingModel.from_json(model.to_json()).to_json()


@pytest.mark.parametrize('version',['1.0.0','1.7.0'])
def test_duplicate_key_refusal_is_unchanged(version):
    raw=BuildingModel(model_id='model:duplicate',schema_version=version).to_json()
    raw=raw.replace('"attributes": {}','"attributes": {"x": 1, "x": 2}')
    with pytest.raises(ContractError,match='duplicate JSON key'):BuildingModel.from_json(raw)


def test_original_first_zero_reproducer_is_typed_not_exception():
    source=make_physical_source();route=make_physical_route(source)
    raw=source.to_json(indent=None).replace('0.0',POSITIVE,1)
    assert verify_physical_route(source,route,source_json=raw).status in ('invalid_input','unsupported_input')
    assert generate_physical_route(source,route.context,route.requirements,source_json=raw).status in ('invalid_input','unsupported_input')
