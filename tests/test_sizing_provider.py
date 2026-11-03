"""Synthetic provider contracts; no result is an approved electrical design."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.electrical.sizing_provider import request_from_model, single_route_length_ft, size_model_circuit
from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def model():
    return BuildingModel.load(ROOT / 'fixtures/model/v1/garage-route.json')

@pytest.fixture
def settings():
    return {'phase':1,'continuous':True,'material':'cu','insulation':'thhn','location_condition':'dry','parallel_sets':1,'neutral':False,'neutral_is_ccc':False,'egc':True,'egc_size':'10','egc_same_insulation_as_phase':True,'temp_rating_c':90,'termination_rating_c':75,'ambient_c':30,'conduit_type':'emt','current_carrying_count':2,'hot_conductor_count':2,'sole_circuit_in_raceway':True,'length_ft':50,'length_basis':'user_supplied','vd_limit_pct':3}

def fake_provider(request):
    return {'version':'sizing-result-v1','provider':{'name':'synthetic','version':'1'},'purpose':'proposed_only','model_id':request['model_id'],'model_sha256':request['model_sha256'],'circuit_id':request['circuit_id'],'request_digest':request['request_digest'],'inputs':request['inputs'],'status':'partial','limitations':['Synthetic provider only.'],'result':{'operating_amps':40,'design_amps':50,'wire_size':'8','conduit_trade_size':'1/2','voltage_drop_percent':1.0,'violations':[],'unsupported_reason':None}}

def test_contract_maps_proposal_and_preserves_original_model(model, settings):
    before = model.to_json()
    out = size_model_circuit(model, 'circuit:evse', settings, fake_provider)
    assert model.to_json() == before
    assert out['status'] == 'partial' and out['target_mapping']['applied'] is False
    assert out['target_mapping']['proposed_wire_scope'] == ['line']
    assert out['target_mapping']['ground_size_verified'] is False
    assert out['target_mapping']['existing_conductors']
    assert any('not been independently verified' in x for x in out['limitations'])

def test_missing_inputs_are_unresolved_without_invoking_provider(model, settings):
    del settings['egc_size']
    def never(_): raise AssertionError('provider should not run')
    out = size_model_circuit(model, 'circuit:evse', settings, never)
    assert out['status'] == 'unresolved' and out['target_mapping']['applied'] is False

def test_provider_cannot_mutate_evidence_or_return_wrong_binding(model, settings):
    before = json.dumps(settings, sort_keys=True)
    def tamper(request):
        request['inputs']['length_ft'] = 1
        return fake_provider(request)
    with pytest.raises(ValueError, match='mismatched'):
        size_model_circuit(model, 'circuit:evse', settings, tamper)
    assert json.dumps(settings, sort_keys=True) == before

def test_non_partial_provider_cannot_smuggle_a_size(model, settings):
    def bad(request):
        response = fake_provider(request)
        response['status'] = 'unresolved'
        return response
    with pytest.raises(ValueError, match='non-partial'):
        size_model_circuit(model, 'circuit:evse', settings, bad)

@pytest.mark.parametrize('changes', [
    {'parallel_sets':2}, {'sole_circuit_in_raceway':False},
    {'egc_same_insulation_as_phase':False}, {'location_condition':'wet'},
    {'insulation':'thw', 'temp_rating_c':90}, {'ambient_c':90},
    {'neutral':True, 'neutral_same_size_as_phase':True},
])
def test_unrepresented_arrangements_do_not_call_provider(model, settings, changes):
    def never(_): raise AssertionError('provider should not run')
    out = size_model_circuit(model, 'circuit:evse', {**settings, **changes}, never)
    assert out['status'] == 'unsupported' and out['target_mapping']['applied'] is False

def test_partial_provider_cannot_hide_failed_voltage_drop(model, settings):
    def bad(request):
        response = fake_provider(request)
        response['result']['voltage_drop_percent'] = 10
        return response
    with pytest.raises(ValueError, match='voltage drop'):
        size_model_circuit(model, 'circuit:evse', settings, bad)

def test_provider_cannot_return_unrecognized_size_or_wrong_current(model, settings):
    def bad_size(request):
        response = fake_provider(request)
        response['result']['wire_size'] = 'not-a-size'
        return response
    with pytest.raises(ValueError, match='invalid provider response'):
        size_model_circuit(model, 'circuit:evse', settings, bad_size)
    def bad_current(request):
        response = fake_provider(request)
        response['result']['design_amps'] = 1
        return response
    with pytest.raises(ValueError, match='partial response'):
        size_model_circuit(model, 'circuit:evse', settings, bad_current)

def test_invalid_topology_is_rejected_before_provider(model, settings):
    with pytest.raises(ValueError, match='poles|hot conductor'):
        size_model_circuit(model, 'circuit:evse', {**settings, 'hot_conductor_count':3}, fake_provider)

def test_three_phase_requires_explicit_balance(model, settings):
    three = replace(model, circuits=(replace(model.circuits[0], phase='3ph', poles=3, voltage_v=208),))
    inputs = {**settings, 'phase':3, 'hot_conductor_count':3, 'current_carrying_count':3}
    def never(_): raise AssertionError('provider should not run')
    assert size_model_circuit(three, 'circuit:evse', inputs, never)['status'] == 'unresolved'
    assert size_model_circuit(three, 'circuit:evse', {**inputs, 'balanced_three_phase':False}, never)['status'] == 'unsupported'

def test_one_hot_without_neutral_is_unsupported(model, settings):
    one_hot = replace(model, circuits=(replace(model.circuits[0], poles=1, voltage_v=120),))
    inputs = {**settings, 'hot_conductor_count':1, 'current_carrying_count':1}
    def never(_): raise AssertionError('provider should not run')
    assert size_model_circuit(one_hot, 'circuit:evse', inputs, never)['status'] == 'unsupported'

def test_phase_and_poles_can_be_mapped_from_canonical_model(model, settings):
    del settings['phase']
    out = size_model_circuit(model, 'circuit:evse', settings, fake_provider)
    assert out['inputs']['phase'] == 1 and out['inputs']['hot_conductor_count'] == 2

def test_poles_do_not_silently_supply_hot_count(model, settings):
    del settings['hot_conductor_count']
    def never(_): raise AssertionError('provider should not run')
    out = size_model_circuit(model, 'circuit:evse', settings, never)
    assert out['status'] == 'unresolved'

def test_route_length_and_shared_route_fail_closed(model, settings):
    feet, route = single_route_length_ft(model, 'circuit:evse')
    bound = {**settings, 'length_ft':feet, 'length_basis':'model_centerline', 'source_route_id':route}
    out = size_model_circuit(model, 'circuit:evse', bound, fake_provider)
    assert out['inputs']['length_evidence'] and out['inputs']['source_route_id'] == route
    with pytest.raises(ValueError, match='route length'):
        request_from_model(model, 'circuit:evse', {**bound, 'length_ft':feet + 1})
    with pytest.raises(ValueError, match='shorter'):
        size_model_circuit(model, 'circuit:evse', {**settings, 'length_ft':feet - 1}, fake_provider)
    multi = replace(model, circuits=(replace(model.circuits[0], load_port_ids=model.circuits[0].load_port_ids * 2),))
    with pytest.raises(ValueError, match='multi-load'):
        size_model_circuit(multi, 'circuit:evse', settings, fake_provider)
    other = replace(model.circuits[0], id='circuit:other', route_ids=())
    conductor = replace(model.conductors[0], id='conductor:other', circuit_id=other.id)
    shared = replace(model, circuits=(*model.circuits, other), conductors=(*model.conductors, conductor))
    with pytest.raises(ValueError, match='shared'):
        size_model_circuit(shared, 'circuit:evse', settings, fake_provider)

def test_published_schemas_match_packaged_copies():
    for name in ('sizing-request-v1.schema.json', 'sizing-result-v1.schema.json'):
        assert json.loads((ROOT/'contracts'/name).read_text()) == json.loads((ROOT/'src/oabm/electrical/_sizing_contracts'/name).read_text())
