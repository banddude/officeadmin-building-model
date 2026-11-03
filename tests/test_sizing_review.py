import copy
from dataclasses import replace
from pathlib import Path
import pytest

from oabm.model import BuildingModel
from oabm.electrical.sizing_provider import size_model_circuit
from oabm.electrical.sizing_review import preview

ROOT=Path(__file__).resolve().parents[1]

def fixture():
    model=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
    settings={'phase':1,'continuous':True,'material':'cu','insulation':'thhn','location_condition':'dry','parallel_sets':1,'neutral':False,'neutral_is_ccc':False,'egc':True,'egc_size':'10','egc_same_insulation_as_phase':True,'temp_rating_c':90,'termination_rating_c':75,'ambient_c':30,'conduit_type':'emt','current_carrying_count':2,'hot_conductor_count':2,'sole_circuit_in_raceway':True,'raceway_inventory_complete':True,'length_ft':50,'length_basis':'user_supplied','vd_limit_pct':3}
    return model,settings

def provider(q):
    return {'version':'sizing-result-v1','provider':{'name':'synthetic','version':'1'},'purpose':'proposed_only','model_id':q['model_id'],'model_sha256':q['model_sha256'],'circuit_id':q['circuit_id'],'request_digest':q['request_digest'],'request_payload':q['request_payload'],'inputs':q['inputs'],'status':'partial','limitations':['Synthetic only.'],'result':{'operating_amps':40,'design_amps':50,'wire_size':'8','conduit_trade_size':'1/2','voltage_drop_percent':1,'violations':[],'unsupported_reason':None}}

def test_preview_compares_but_does_not_apply():
    model,settings=fixture(); before=model.to_json()
    proposal=size_model_circuit(model,'circuit:evse',settings,provider)
    out=preview(model,proposal)
    assert out['comparison_status']=='differences_found' and out['applied'] is False
    assert out['version']=='sizing-preview-v1'
    assert [r['size_comparison'] for r in out['conductors']] == ['different','different']
    assert all(r['material_comparison']=='same' for r in out['conductors'])
    assert out['provider_output_authentication']=='not_verified'
    assert out['provider']['name']=='synthetic' and out['inputs']['length_basis']=='user_supplied'
    assert model.to_json()==before

def test_stale_model_and_tampered_mapping_are_rejected():
    model,settings=fixture(); proposal=size_model_circuit(model,'circuit:evse',settings,provider)
    with pytest.raises(ValueError,match='stale'):
        preview(replace(model,name='changed'),proposal)
    tampered=copy.deepcopy(proposal);tampered['target_mapping']['existing_conductors'][0]['size']='14'
    with pytest.raises(ValueError,match='source facts'):
        preview(model,tampered)

def test_consistently_edited_output_is_never_called_authenticated():
    model,settings=fixture(); proposal=size_model_circuit(model,'circuit:evse',settings,provider)
    edited=copy.deepcopy(proposal)
    edited['result']['wire_size']='10'
    edited['target_mapping']['proposed_wire_size']='10'
    out=preview(model,edited)
    assert out['provider_output_authentication']=='not_verified' and out['applied'] is False
    assert any('unauthenticated' in x for x in out['limitations'])

def test_invalid_schema_errors_do_not_expose_submitted_values():
    model,settings=fixture(); proposal=size_model_circuit(model,'circuit:evse',settings,provider)
    proposal['status']='private-marker-invalid'
    with pytest.raises(ValueError,match='invalid sizing proposal') as exc:
        preview(model,proposal)
    assert 'private-marker-invalid' not in str(exc.value)

def test_missing_or_unrecognized_existing_wires_remain_unresolved():
    model,settings=fixture(); missing=replace(model,conductors=())
    out=preview(missing,size_model_circuit(missing,'circuit:evse',settings,provider))
    assert out['comparison_status']=='unresolved' and out['missing_roles']==['line']
    altered=replace(model,conductors=tuple(replace(c,size='unknown') if c.role=='line' else c for c in model.conductors))
    out=preview(altered,size_model_circuit(altered,'circuit:evse',settings,provider))
    assert out['comparison_status']=='unresolved' and all(x['size_comparison']=='unknown' for x in out['conductors'])

def test_incomplete_conductor_count_and_material_change_are_visible():
    model,settings=fixture()
    one_line = replace(model, conductors=tuple(c for c in model.conductors if c.id != 'conductor:l2'))
    with pytest.raises(ValueError,match='counts conflict'):
        size_model_circuit(one_line,'circuit:evse',settings,provider)
    aluminum = replace(model, conductors=tuple(replace(c,material='aluminum') if c.role=='line' else c for c in model.conductors))
    out=preview(aluminum,size_model_circuit(aluminum,'circuit:evse',settings,provider))
    assert out['comparison_status']=='differences_found' and all(x['material_comparison']=='different' for x in out['conductors'])

def test_extra_neutral_and_missing_ground_remain_unresolved():
    model,settings=fixture()
    extra = replace(model.conductors[0],id='conductor:neutral',role='neutral')
    with_neutral = replace(model,conductors=(*model.conductors,extra))
    with pytest.raises(ValueError,match='counts conflict'):
        size_model_circuit(with_neutral,'circuit:evse',settings,provider)
    without_ground=replace(model,conductors=tuple(c for c in model.conductors if c.role!='equipment-ground'))
    with pytest.raises(ValueError,match='counts conflict'):
        size_model_circuit(without_ground,'circuit:evse',settings,provider)

def test_unknown_conductor_route_membership_remains_unresolved():
    model,settings=fixture()
    missing_routes=replace(model,conductors=tuple(replace(c,route_ids=()) if c.id=='conductor:l1' else c for c in model.conductors))
    with pytest.raises(ValueError,match='membership is unresolved'):
        size_model_circuit(missing_routes,'circuit:evse',settings,provider)

def test_extra_ground_is_not_silently_treated_as_one():
    model,settings=fixture()
    extra=replace(next(c for c in model.conductors if c.role=='equipment-ground'),id='conductor:extra-ground')
    altered=replace(model,conductors=(*model.conductors,extra))
    with pytest.raises(ValueError,match='counts conflict'):
        size_model_circuit(altered,'circuit:evse',settings,provider)

def test_preview_schema_copy_matches_packaged_resource():
    import json
    public=ROOT/'contracts/sizing-preview-v1.schema.json'
    packaged=ROOT/'src/oabm/electrical/_sizing_contracts/sizing-preview-v1.schema.json'
    assert json.loads(public.read_text()) == json.loads(packaged.read_text())
