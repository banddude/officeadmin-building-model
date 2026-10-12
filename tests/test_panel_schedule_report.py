"""Synthetic supplied facts, never permit/engineering or OA-issued documents."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry,Resource

from oabm.model import (BuildingModel,Circuit,ContractError,Provenance,ElectricalEquipment,
    Point3,Pose,Port,Vector3,upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3)
from oabm.reports import generate_panel_schedule_report,PanelScheduleReport,SourceSnapshot
from oabm.drawings.panel_schedules import panel_report_to_drawing_schedules

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def two_panels():
    from oabm.importers.roomplan import load_captured_room
    imported=load_captured_room(ROOT/'fixtures/roomplan/captured-room-3d.json',source_id='fixture:panel-facts-roomplan')
    legacy=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
    provenance=(Provenance(source_kind='synthetic',source_id='fixture:panel-facts',derivation='user'),)
    first=replace(legacy.electrical_equipment[0],id='equipment:panel-a',name='Synthetic panel A',
        level_id=imported.levels[0].id,space_id=imported.spaces[0].id,host_id=None,provenance=provenance)
    second=replace(first,id='equipment:panel-b',name='Synthetic panel B')
    transformer=replace(first,id='equipment:transformer',equipment_type='transformer',name='Synthetic non-panel source')
    device=replace(legacy.electrical_devices[0],id='device:shared-load',host_id=None,
        level_id=imported.levels[0].id,space_id=imported.spaces[0].id,provenance=provenance)
    ports=tuple(Port(id=f'port:{key}',owner_id=owner,domain='electrical',role='source' if key.startswith('source') else 'sink',
        pose=Pose(position=Point3(x=float(i),y=0.0,z=1.0)),direction=Vector3(x=1.0,y=0.0,z=0.0),provenance=provenance)
        for i,(key,owner) in enumerate((('source-a',first.id),('source-b',second.id),('source-other',transformer.id),
            ('load-a',device.id),('load-b',device.id))))
    weak=(Provenance(source_kind='synthetic',source_id='fixture:panel-facts',derivation='inferred',
        confidence=.3,scope_paths=('load_va',),attributes={'parser_assumption':'explicit synthetic inference'}),)
    circuits=tuple(Circuit(id=f'circuit:{key}',source_port_id=source,load_port_ids=loads,circuit_number=label,
        load_va=va,poles=poles,voltage_v=voltage,phase=phase,confidence=.6 if key=='b' else 1.0,
        provenance=weak if key=='b' else provenance) for key,source,loads,label,va,poles,voltage,phase in (
        ('a','port:source-a',('port:load-a','port:load-b'),'1',100.0,2,240.0,'single'),
        ('b','port:source-a',('port:load-a',),'1',None,None,None,None),
        ('c','port:source-a',('port:load-b',),'01',0.0,1,120.0,'A'),
        ('d','port:source-b',('port:load-a',),None,200.0,1,None,None),
        ('e','port:source-b',('port:load-b',),'1',25.0,1,120.0,'B'),
        ('f','port:source-other',('port:load-a',),'x',50.0,None,None,None)))
    return replace(imported,electrical_equipment=(first,second,transformer),electrical_devices=(device,),ports=ports,circuits=circuits)


def test_actual_importer_two_panel_report_and_explicit_drawing_adapter(two_panels):
    model=two_panels;before=model.to_json();report=generate_panel_schedule_report(model)
    assert model.provenance[0].source_kind=='roomplan'
    assert model.to_json()==before and len(report.panels)==2
    a,b=report.panels
    assert a.panel.id=='equipment:panel-a' and a.known_connected_va==100.0
    assert a.unknown_load_circuit_ids==('circuit:b',) and a.all_listed_loads_known is False
    assert a.subtotal_source_circuit_ids==('circuit:a','circuit:c') and a.subtotal_derivation=='inferred'
    assert a.label_conflicts[0].label=='1' and a.label_conflicts[0].circuit_ids==('circuit:a','circuit:b')
    assert a.circuits[2].circuit.entity['circuit_number']=='01' and a.circuits[2].circuit.entity['load_va']==0
    assert b.known_connected_va==225.0 and b.all_listed_loads_known and not b.label_conflicts
    assert 'circuit_number' in b.circuits[0].missing_fields
    unassigned=report.unassigned_circuits[0]
    assert unassigned.circuit.id=='circuit:f' and unassigned.assignment_status=='unsupported_source_owner'
    assert unassigned.source_owner.entity['equipment_type']=='transformer' and unassigned.circuit.entity['load_va']==50
    source=a.circuits[1].circuit.entity
    assert source['confidence']==.6
    assert source['provenance'][0]['scope_paths']==['load_va'] and source['provenance'][0]['derivation']=='inferred'
    assert source['provenance'][0]['attributes']['parser_assumption']=='explicit synthetic inference'
    assert report.code_verification_performed is False
    schedules=panel_report_to_drawing_schedules(report)
    assert len(schedules)==5
    assert {r.source_id for s in schedules for r in s.rows} >= {c.id for c in model.circuits}
    assert report.to_dict()==PanelScheduleReport.from_json(report.to_json()).to_dict()
    report.validate_against_model(model)


@pytest.mark.parametrize('upgrade',[lambda m:m,upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3])
def test_legacy_garage_default_omissions_and_all_model_versions(upgrade):
    model=upgrade(BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json'));before=model.to_json()
    report=generate_panel_schedule_report(model)
    assert len(report.panels)==1 and len(report.panels[0].circuits)==1
    restored=PanelScheduleReport.from_json(report.to_json());restored.validate_against_model(model)
    assert restored.to_json()==report.to_json() and model.to_json()==before
    assert panel_report_to_drawing_schedules(restored)
    # Unscoped/unspecified legacy evidence and unknown wall construction remain
    # encoded by the original canonical serializer, never padded with nulls.
    source=report.panels[0].panel.entity
    for p in source['provenance']:
        assert 'scope_paths' not in p


def test_semantic_binding_permutations_and_source_revision_regeneration(two_panels):
    report=generate_panel_schedule_report(two_panels)
    permuted=replace(two_panels,electrical_equipment=tuple(reversed(two_panels.electrical_equipment)),
        circuits=tuple(reversed(two_panels.circuits)),ports=tuple(reversed(two_panels.ports)),walls=tuple(reversed(two_panels.walls)))
    assert generate_panel_schedule_report(permuted).to_json()==report.to_json()
    modified=replace(two_panels,circuits=(replace(two_panels.circuits[0],load_va=300.0,circuit_number='changed'),*two_panels.circuits[1:]))
    revised=generate_panel_schedule_report(modified)
    assert revised.source_model_fingerprint != report.source_model_fingerprint
    assert revised.panels[0].known_connected_va==300.0 and not revised.panels[0].label_conflicts
    with pytest.raises(ContractError,match='does not match'):report.validate_against_model(modified)
    assert report.panels[0].known_connected_va==100.0


def test_schema_portable_report(two_panels):
    schema=json.loads((ROOT/'contracts/panel-schedule-report-v1.schema.json').read_text())
    canonical=json.loads((ROOT/'contracts/oabm-model-v1.3.schema.json').read_text())
    registry=Registry().with_resource(canonical['$id'],Resource.from_contents(canonical))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema,registry=registry).validate(generate_panel_schedule_report(two_panels).to_dict())


@pytest.mark.parametrize('upgrade',[lambda m:m,upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3])
@pytest.mark.parametrize('with_panel',[False,True])
def test_unnamed_models_preserve_null_context_and_validate_schema(upgrade,with_panel):
    model=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json') if with_panel else BuildingModel(model_id='model:unnamed')
    model=upgrade(replace(model,name=None))
    before=model.to_json()
    report=generate_panel_schedule_report(model)
    assert report.model_source_context.name is None
    restored=PanelScheduleReport.from_json(report.to_json())
    restored.validate_against_model(model)
    assert restored.to_json()==report.to_json() and model.to_json()==before
    schema=json.loads((ROOT/'contracts/panel-schedule-report-v1.schema.json').read_text())
    canonical=json.loads((ROOT/'contracts/oabm-model-v1.3.schema.json').read_text())
    registry=Registry().with_resource(canonical['$id'],Resource.from_contents(canonical))
    Draft202012Validator(schema,registry=registry).validate(report.to_dict())
    if with_panel:
        assert len(panel_report_to_drawing_schedules(report))==2
    else:
        assert report.panels==() and report.unassigned_circuits==()
        assert panel_report_to_drawing_schedules(report)==()


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(version='2.0.0'),lambda d:d.update(breaker_rating=20),lambda d:d.update(code_verification_performed=True),
    lambda d:d.update(panels='equipment:panel-a'),lambda d:d.update(unassigned_circuits='circuit:f'),
    lambda d:d['panels'][0].update(circuits='circuit:a'),lambda d:d['panels'][0].update(unknown_load_circuit_ids='circuit:b'),
    lambda d:d['panels'][0].update(known_connected_va=True),lambda d:d['panels'][0].update(known_connected_va=float('inf')),
    lambda d:d['panels'][0].update(known_connected_va=999),lambda d:d['panels'][0].update(all_listed_loads_known=True),
    lambda d:d['panels'][0].update(subtotal_source_circuit_ids='circuit:a'),
    lambda d:d['panels'][0]['circuits'][0].update(missing_fields='load_va'),
    lambda d:d['panels'][0]['circuits'][0].update(assignment_status='nearest_panel'),
    lambda d:d['panels'][0]['circuits'][0]['source_port']['entity'].update(owner_id='equipment:other'),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity'].update(load_va=200),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity'].update(load_port_ids='port:load-a'),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity'].update(route_ids='route:a'),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity'].update(attributes=None),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity']['provenance'][0].update(attributes=[]),
    lambda d:d['panels'][0]['circuits'][0]['circuit']['entity']['provenance'][0].update(scope_paths=[]),
])
def test_strict_malformed_portable_report(two_panels,mutation):
    doc=generate_panel_schedule_report(two_panels).to_dict();mutation(doc)
    with pytest.raises(ContractError):PanelScheduleReport.from_dict(doc)


def test_snapshot_independence_and_mutation_refusal(two_panels):
    report=generate_panel_schedule_report(two_panels);before=report.to_json()
    two_panels.circuits[0].provenance[0].attributes['later_source_edit']='synthetic'
    assert report.to_json()==before
    doc=report.to_dict();doc['panels'][0]['circuits'][0]['circuit']['entity']['load_va']=999
    assert report.to_json()==before
    report.panels[0].circuits[0].circuit.entity['provenance'][0]['attributes']['forged']='edit'
    with pytest.raises(ContractError,match='content binding'):report.to_dict()


def test_route_and_load_port_multiplicity_cannot_multiply_connected_va(two_panels):
    from oabm.model import Route,Polyline3D
    ports={p.id:p for p in two_panels.ports};source=ports['port:source-a']
    routes=tuple(Route(id=f'route:panel-{i}',route_type='emt',start_port_id=source.id,end_port_id=ports[end].id,
        centerline=Polyline3D(points=(source.pose.position,ports[end].pose.position)),provenance=source.provenance)
        for i,end in enumerate(('port:load-a','port:load-b')))
    first=replace(two_panels.circuits[0],route_ids=tuple(r.id for r in routes))
    model=replace(two_panels,circuits=(first,*two_panels.circuits[1:]),routes=routes)
    report=generate_panel_schedule_report(model)
    assert report.panels[0].known_connected_va==100
    assert report.panels[0].circuits[0].circuit.entity['route_ids']==[r.id for r in routes]
    assert report.panels[0].circuits[0].circuit.entity['load_port_ids']==['port:load-a','port:load-b']


def test_owner_relative_evidence_and_scope_set_permutations(two_panels):
    port=two_panels.ports[0]
    evidence=Provenance(source_kind='synthetic',source_id='fixture:panel-owner',derivation='observed',confidence=.2,
        scope_paths=('owner_id','role'))
    model=replace(two_panels,ports=(replace(port,provenance=(evidence,)),*two_panels.ports[1:]))
    report=generate_panel_schedule_report(model)
    row=report.panels[0].circuits[0]
    assert row.source_port.entity['provenance'][0]['scope_paths']==['owner_id','role']
    assert row.source_port.entity['provenance'][0]['confidence']==.2
    assert row.source_port.entity['provenance'][0]['derivation']=='observed'
    assert report.panels[0].subtotal_derivation=='inferred'
    reversed_model=replace(model,ports=(replace(model.ports[0],provenance=(replace(evidence,scope_paths=('role','owner_id')),)),*model.ports[1:]))
    assert generate_panel_schedule_report(reversed_model).to_json()==report.to_json()


def test_opaque_labels_empty_panels_and_unknowns_stay_explicit(two_panels):
    empty=replace(two_panels.electrical_equipment[0],id='equipment:empty-panel')
    model=replace(two_panels,electrical_equipment=(*two_panels.electrical_equipment,empty),
        circuits=(replace(two_panels.circuits[0],circuit_number='1/3',poles=2),
                  replace(two_panels.circuits[1],circuit_number=' 1 ',load_va=0),*two_panels.circuits[2:]))
    report=generate_panel_schedule_report(model)
    assert report.panels[0].panel.id==empty.id
    assert report.panels[0].diagnostic_codes==('no_listed_circuits',)
    assert report.panels[0].known_connected_va==0 and report.panels[0].all_listed_loads_known
    a=next(p for p in report.panels if p.panel.id=='equipment:panel-a')
    assert not a.label_conflicts
    assert [r.circuit.entity['circuit_number'] for r in a.circuits]==['1/3',' 1 ','01']
    assert a.circuits[0].circuit.entity['poles']==2
    assert not any('breaker' in key or 'slot' in key or 'demand' in key for key in report.to_dict())


def test_finite_supplied_loads_cannot_overflow_report_output(two_panels):
    model=replace(two_panels,circuits=(replace(two_panels.circuits[0],load_va=1e308),
        replace(two_panels.circuits[1],load_va=1e308),*two_panels.circuits[2:]))
    with pytest.raises(ContractError,match='finite output range'):generate_panel_schedule_report(model)


@pytest.mark.parametrize('value',[None,'value',[],42])
def test_nonportable_canonical_provenance_attributes_refused_honestly(two_panels,value):
    malformed=replace(two_panels.circuits[0].provenance[0],attributes=value)
    model=replace(two_panels,circuits=(replace(two_panels.circuits[0],provenance=(malformed,)),*two_panels.circuits[1:]))
    with pytest.raises(ContractError):generate_panel_schedule_report(model)


def test_python_constructor_array_invariants_and_duplicate_ids(two_panels):
    report=generate_panel_schedule_report(two_panels);group=report.panels[0];row=group.circuits[0]
    for changes in ({'circuits':'circuit:a'},{'unknown_load_circuit_ids':'circuit:b'},
        {'subtotal_source_circuit_ids':'circuit:a'},{'label_conflicts':'1'},{'diagnostic_codes':'unknown'},
        {'known_connected_va':True},{'all_listed_loads_known':1}):
        with pytest.raises(ContractError):replace(group,**changes)
    with pytest.raises(ContractError):replace(row,missing_fields='load_va')
    for changes in ({'panels':'panel'},{'unassigned_circuits':'circuit:f'},{'source_model_version':[]},
        {'code_verification_performed':0}):
        with pytest.raises(ContractError):replace(report,**changes)
    with pytest.raises(ContractError):replace(group,circuits=(row,row))
    with pytest.raises(ContractError):replace(report,panels=(group,group))


def test_default_outputs_remain_identical_when_opt_in_report_is_generated(two_panels,tmp_path):
    from dataclasses import asdict
    from oabm.drawings import generate_schedules
    from oabm.ifc import to_ifc
    from oabm.quantities import extract_quantities
    from oabm.exports.gltf import to_glb
    def outputs(model,path):
        to_glb(model,path)
        return (model.to_json(),to_ifc(model).to_string(),path.read_bytes(),extract_quantities(model).to_json(),
            json.dumps([asdict(s) for s in generate_schedules(model)],sort_keys=True))
    before=outputs(two_panels,tmp_path/'before.glb')
    panel_report_to_drawing_schedules(generate_panel_schedule_report(two_panels))
    assert outputs(two_panels,tmp_path/'after.glb')==before


def test_model_wide_parser_assumptions_remain_owner_bound_and_visible(two_panels):
    evidence=Provenance(source_kind='synthetic',source_id='fixture:parser-context',derivation='inferred',
        confidence=.2,scope_paths=('circuits',),attributes={'unverified_profile':'synthetic supplied assumptions'})
    model=replace(two_panels,provenance=(*two_panels.provenance,evidence),
        attributes={**two_panels.attributes,'assumptions':{'code_verified':False,'ordered_values':[2,1]}})
    report=generate_panel_schedule_report(model)
    context=report.model_source_context
    assert context.model_id==model.model_id and context.provenance[-1].scope_paths==('circuits',)
    assert context.provenance[-1].derivation=='inferred' and context.provenance[-1].confidence==.2
    assert context.attributes['assumptions']['ordered_values']==[2,1]
    assert PanelScheduleReport.from_json(report.to_json()).to_json()==report.to_json()
    changed=replace(model,attributes={**model.attributes,'assumptions':{'ordered_values':[1,2]}})
    assert generate_panel_schedule_report(changed).source_model_fingerprint != report.source_model_fingerprint
