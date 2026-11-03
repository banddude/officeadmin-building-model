import copy
from dataclasses import replace
import pytest
from test_sizing_provider import model,settings,fake_provider
from oabm.electrical.sizing_provider import size_model_circuit
from oabm.electrical.voltage_drop_review import summarize_voltage_drop


def test_exposes_provider_report_and_limit_without_model_mutation(model,settings):
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    before=model.to_json(); report=summarize_voltage_drop(model,p)
    assert report['voltage_drop_percent']==1 and report['user_limit_percent']==3
    assert report['one_way_length_ft']==50 and report['length_basis']=='user_supplied'
    assert report['proposed_wire_size']=='8' and report['operating_amps']==40
    assert report['provider_output_authentication']=='not_verified' and not report['applied']
    assert model.to_json()==before


def test_stale_model_or_invalid_proposal_is_rejected(model,settings):
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    with pytest.raises(ValueError): summarize_voltage_drop(replace(model,model_id='changed'),p)
    for mode in ('drop','mapping','inputs','status'):
        q=copy.deepcopy(p)
        if mode=='drop':q['result']['voltage_drop_percent']=99
        if mode=='mapping':q['target_mapping']['proposed_wire_size']='2'
        if mode=='inputs':q['inputs']['length_ft']=1
        if mode=='status':q['status']='unsupported'
        with pytest.raises(ValueError):summarize_voltage_drop(model,q)


def test_consistent_forgery_remains_explicitly_unauthenticated(model,settings):
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    p['result']['voltage_drop_percent']=.1
    report=summarize_voltage_drop(model,p)
    assert report['voltage_drop_percent']==.1
    assert report['provider_output_authentication']=='not_verified'


def test_regenerates_after_model_change(model,settings):
    original=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    changed=replace(model,model_id='model:revised')
    with pytest.raises(ValueError):summarize_voltage_drop(changed,original)
    revised=size_model_circuit(changed,'circuit:evse',settings,fake_provider)
    assert summarize_voltage_drop(changed,revised)['model_sha256'] != summarize_voltage_drop(model,original)['model_sha256']


def test_schema_parity_and_mandatory_limitations(model,settings):
    import json
    from pathlib import Path
    from importlib.resources import files
    from oabm.electrical.voltage_drop_review import _SCHEMA
    root=Path(__file__).resolve().parents[1]
    assert json.loads((root/'contracts/voltage-drop-review-v1.schema.json').read_text())==json.loads(files('oabm.electrical._sizing_contracts').joinpath('voltage-drop-review-v1.schema.json').read_text())
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    report=summarize_voltage_drop(model,p);assert _SCHEMA.is_valid(report)
    report['limitations'].append('CODE COMPLIANT');assert not _SCHEMA.is_valid(report)


def test_model_centerline_report_identifies_route_and_allowance_gap(model,settings):
    from oabm.electrical.sizing_provider import single_route_length_ft
    feet,route=single_route_length_ft(model,'circuit:evse')
    design={**settings,'length_ft':feet,'length_basis':'model_centerline','source_route_id':route}
    report=summarize_voltage_drop(model,size_model_circuit(model,'circuit:evse',design,fake_provider))
    assert report['one_way_length_ft']==feet and report['source_route_id']==route
    assert any('leads and slack' in x for x in report['limitations'])


def test_provider_caveats_are_preserved_separately(model,settings):
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    p['limitations'].append('Assumes a balanced load.')
    report=summarize_voltage_drop(model,p)
    assert 'Assumes a balanced load.' in report['provider_notes_untrusted']
    assert 'Assumes a balanced load.' not in report['limitations']


def test_oversized_input_is_rejected(model,settings):
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    p['limitations'].append('x'*(9*1024*1024))
    with pytest.raises(ValueError):summarize_voltage_drop(model,p)


def test_saved_report_requires_reconstruction_for_cross_field_consistency(model,settings):
    from oabm.electrical.voltage_drop_review import validate_voltage_drop_review
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    report=summarize_voltage_drop(model,p)
    assert validate_voltage_drop_review(model,p,report)
    bad=copy.deepcopy(report);bad['voltage_drop_percent']=100
    with pytest.raises(ValueError):validate_voltage_drop_review(model,p,bad)
    bad=copy.deepcopy(report);bad['load_amps_input']=-1
    with pytest.raises(ValueError):validate_voltage_drop_review(model,p,bad)


def test_cyclic_and_oversized_model_metadata_are_rejected(model,settings):
    from oabm.electrical.voltage_drop_review import validate_voltage_drop_review
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    report=summarize_voltage_drop(model,p)
    attrs=model.routes[0].provenance[0].attributes
    attrs['cycle']=attrs
    with pytest.raises(ValueError,match='invalid or oversized'):
        summarize_voltage_drop(model,p)
    with pytest.raises(ValueError,match='invalid or oversized'):
        validate_voltage_drop_review(model,p,report)
    del attrs['cycle']; attrs['large']='x'*3_000_000
    with pytest.raises(ValueError,match='invalid or oversized'):
        summarize_voltage_drop(model,p)


def test_model_snapshot_is_detached_before_preview(model,settings,monkeypatch):
    import oabm.electrical.voltage_drop_review as module
    p=size_model_circuit(model,'circuit:evse',settings,fake_provider)
    attrs=model.routes[0].provenance[0].attributes
    original=module.capture
    def capture_then_mutate(value, **kwargs):
        captured=original(value, **kwargs)
        if value is model:
            attrs['later_cycle']=attrs
        return captured
    monkeypatch.setattr(module,'capture',capture_then_mutate)
    report=module.summarize_voltage_drop(model,p)
    assert report['model_sha256']==p['model_sha256']
    assert report['provider_output_authentication']=='not_verified'
