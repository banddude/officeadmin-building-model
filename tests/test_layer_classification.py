"""Canonical-fact classification, honest unknowns, unchanged source model."""
from dataclasses import replace
from pathlib import Path
import sys

from oabm.model import Circuit, LayerSelectionConfig, resolve_preset
from oabm.exports import classify_model_layers, build_layer_glb

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
from assembly_layer_selections import synthetic_model


def classified_source():
    model=synthetic_model()
    devices=list(model.electrical_devices)
    devices[0]=replace(devices[0],system='power',attributes={'pdf_electrical':{'scope_status':'new'}})
    devices[1]=replace(devices[1],system='lighting',attributes={'pdf_electrical':{'scope_status':'existing_to_remain'}})
    circuit=Circuit(id='circuit:classification',source_port_id=model.ports[0].id,
        load_port_ids=(model.ports[1].id,),route_ids=(model.routes[0].id,),phase='A')
    return replace(model,electrical_devices=tuple(devices),circuits=(circuit,))


def test_classification_uses_explicit_fields_and_exposes_unknowns_without_mutation():
    model=classified_source();before=model.to_json()
    result=classify_model_layers(model)
    rows={row['id']:row for row in result.to_dict()['rows']}
    assert rows['device:power']['system']=='power'
    assert rows['device:power']['construction_status']=='new'
    assert rows['device:lighting']['construction_status']=='existing_to_remain'
    assert rows['device:unknown']['system']=='unclassified'
    assert 'system' in rows['device:unknown']['missing_facts']
    assert rows['support:shared']['system']=='shared_supports'
    assert rows['route:room']['construction_status']=='unresolved'
    assert all(row['construction_status']!='A' for row in rows.values())
    assert model.to_json()==before
    assert LayerSelectionConfig.from_json(result.config_json)==result.configuration
    resolved=resolve_preset(result.configuration,model,preset='preset:classified-all',unassigned='error')
    assert len(resolved.visible_ids)==168 and not resolved.unassigned_ids
    assert build_layer_glb(model,result.configuration,preset='preset:classified-all',unassigned='error').to_dict()['unmatched_group_ids']==0


def test_grid_isolation_and_permuted_source_memberships_are_deterministic():
    model=classified_source();result=classify_model_layers(model)
    config=result.configuration
    grid=next(layer for layer in config.layers if layer.system=='ceiling_grid')
    resolved=resolve_preset(config,model,preset='preset:classified-all',isolate=(grid.id,),unassigned='error')
    expected={part.id for part in model.assembly_components if part.component_kind in ('tbar_main_runner','tbar_cross_runner')}
    assert expected and set(resolved.visible_ids)==expected
    reordered=replace(model,electrical_devices=tuple(reversed(model.electrical_devices)))
    assert classify_model_layers(reordered).config_json==result.config_json


def test_malformed_scope_and_names_never_invent_system_or_construction_status():
    model=classified_source();devices=list(model.electrical_devices)
    devices[0]=replace(devices[0],system=None,name='Existing fire alarm lighting power',
        attributes={'pdf_electrical':{'scope_status':'new','scope_reason':'contradiction'}})
    model=replace(model,electrical_devices=tuple(devices))
    row=next(row for row in classify_model_layers(model).to_dict()['rows'] if row['id']==devices[0].id)
    assert row['system']=='unclassified'
    assert row['construction_status']=='unresolved'
    assert 'construction_status:contradictory_scope_evidence' in row['missing_facts']
