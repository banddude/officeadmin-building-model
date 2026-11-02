"""Unrun synthetic integration cases for opt-in site/service source checkpoint."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import oabm.model as canonical
from oabm.model import (BuildingModel,CoordinateSystem,ContractError,Point2D,SiteFeature,
    ServiceNode,ServiceConnection,ServiceEquipmentFacts,PlanServiceLateral,upgrade_to_v1_8)
from oabm.drawings.model_utility import project_model_site
from oabm.drawings.single_line import project_topology
from oabm.reports.service_demand import bind_service_demand_inputs,calculate_service_demand,validate_service_demand_report
from test_site_contract_types import geometry,evidence


def site_model(source_digest='0'*64):
    proof=replace(evidence(),sha256=source_digest)
    boundary=SiteFeature(id='site:boundary',kind='property_boundary',status='existing',
        geometry=geometry('polygon',[(0,0),(10,0),(10,8),(0,8)]),evidence=(proof,))
    intercept=SiteFeature(id='site:intercept',kind='utility_intercept',status='existing',
        geometry=geometry('point',[(0,3)]),evidence=(proof,))
    nodes=(ServiceNode(id='service:utility',name='Utility source',equipment_type='source',
               location_basis='unresolved',unresolved_reason='Street-side service location not supplied'),
           ServiceNode(id='service:main',name='Integrated meter-main',equipment_type='meter_main',
               location_basis='plan_only',plan_position=Point2D(x=3,y=3),evidence=(proof,),unresolved_reason='Elevation and terminal directions not supplied'),
           ServiceNode(id='service:panel',name='Main-lug-only panel',equipment_type='panelboard',
               location_basis='unresolved',unresolved_reason='Panel terminal geometry not supplied'))
    return replace(upgrade_to_v1_8(BuildingModel(model_id='model:synthetic-site',
        coordinate_system=CoordinateSystem(frame_id='site',true_north_radians=0.))),
        site_features=(boundary,intercept),service_nodes=nodes,
        service_connections=(ServiceConnection(id='service-edge:incoming',source_node_id=nodes[0].id,
            target_node_id=nodes[1].id,specification_status='unresolved'),
            ServiceConnection(id='service-edge:panel',source_node_id=nodes[1].id,target_node_id=nodes[2].id,specification_status='unresolved')),
        service_equipment_facts=(ServiceEquipmentFacts(id='service-facts:main',equipment_id=nodes[1].id,
            equipment_rating_a=200,continuous_meter_rating_a=160,integrated_service_disconnect=True),),
        plan_service_laterals=(PlanServiceLateral(id='lateral:private',source_node_id=nodes[1].id,
            intercept_id=intercept.id,property_boundary_id=boundary.id,geometry=geometry('polyline',[(3,3),(0,3)]),
            nominal_diameter_m=.05,raceway_description='Supplied synthetic raceway'),))


def test_opt_in_roundtrip_schema_and_empty_upgrade_preserve_old_bytes():
    old=BuildingModel(model_id='model:old');original=old.to_json()
    upgraded=upgrade_to_v1_8(old)
    assert old.to_json()==original and upgraded.schema_version=='1.8.0'
    model=site_model();document=model.to_dict()
    schema=json.loads((Path(__file__).parents[1]/'contracts/oabm-model-v1.8.schema.json').read_text())
    Draft202012Validator(schema).validate(document)
    decoded=BuildingModel.from_json(model.to_json())
    assert decoded.to_dict()==document  # Numeric facts, not integer/float spelling.
    assert BuildingModel.from_json(decoded.to_json()).to_json()==decoded.to_json()
    assert not model.electrical_equipment and not model.ports and not model.routes
    assert 'z' not in document['service_nodes'][1]['plan_position']
    assert all(field not in old.to_dict() for field in ('site_features','service_nodes','service_connections'))
    for version in range(1,8):
        with pytest.raises(ContractError,match='downgrade'):
            getattr(canonical,f'upgrade_to_v1_{version}')(upgraded)


def test_old_reader_rejects_new_fields_and_physical_binding_migration_refuses():
    document=BuildingModel(model_id='model:old').to_dict();document['service_nodes']=[]
    with pytest.raises(ContractError):BuildingModel.from_dict(document)
    from physical_route_fixture import make_physical_model
    model=make_physical_model();before=model.to_json()
    with pytest.raises(ContractError,match='rebind'):upgrade_to_v1_8(model)
    assert model.to_json()==before


def test_unknown_service_reference_is_not_placed_and_integrated_meter_main_is_one_identity():
    topology=project_topology(site_model(),service_only=True)
    assert len(topology.equipment)==3 and len(topology.connections)==2
    assert [row['id'] for row in topology.equipment if row['equipment_type']=='meter_main']==['service:main']
    assert not [row for row in topology.equipment if row['equipment_type']=='disconnect']
    assert all(row['source_port_id'] is None and row['load_port_id'] is None for row in topology.connections)
    assert any('geometry unresolved' in line for line in topology.diagnostics)


def test_mixed_branch_graph_needs_explicit_service_scope_and_accounts_for_every_omission():
    from test_single_line import make_model
    legacy=make_model(False)
    default=project_topology(legacy)
    assert {row['id'] for row in default.equipment}=={row.id for row in legacy.electrical_equipment}
    assert {row['circuit_id'] for row in default.connections}=={row.id for row in legacy.circuits}
    mixed=replace(site_model(),electrical_equipment=legacy.electrical_equipment,ports=legacy.ports,
        circuits=legacy.circuits,conductors=legacy.conductors,routes=legacy.routes)
    with pytest.raises(ValueError,match='default whole-electrical-model projection cannot omit'):
        project_topology(mixed)
    selected=project_topology(mixed,service_only=True)
    coverage=selected.to_dict()['coverage']
    assert coverage['selection_mode']=='service_only_explicit' and coverage['is_whole_electrical_graph'] is False
    omitted={(row['collection'],row['id']) for row in coverage['omissions']}
    assert {('electrical_equipment',row.id) for row in legacy.electrical_equipment}<=omitted
    assert {('circuits',row.id) for row in legacy.circuits}<=omitted
    projected={(row['collection'],row['id']) for row in coverage['projected']}
    supporting={(row['collection'],row['id']) for row in coverage['supporting']}
    assert not (omitted&projected or omitted&supporting or projected&supporting)
    assert len(omitted|projected|supporting)==coverage['model_entity_count']
    assert 'coverage' not in default.to_dict()  # Unchanged default document shape.


def test_direct_equipment_service_facts_cannot_disappear_from_default_sld():
    from test_single_line import make_model
    model=replace(upgrade_to_v1_8(make_model(False)),service_equipment_facts=(
        ServiceEquipmentFacts(id='facts:direct-panel',equipment_id='MAIN',equipment_rating_a=200,
                              main_lug_only=True,circuit_positions=24),))
    assert not model.service_nodes and not model.service_connections
    with pytest.raises(ValueError,match='cannot omit typed service references/facts'):
        project_topology(model)


def wrap_branch_circuit(model):
    upgraded=upgrade_to_v1_8(model)
    return replace(upgraded,service_nodes=(
        ServiceNode(id='service:branch-source',equipment_type='panelboard',location_basis='placed_equipment',equipment_id='MAIN'),
        ServiceNode(id='service:branch-target',equipment_type='panelboard',location_basis='placed_equipment',equipment_id='SUB')),
        service_connections=(ServiceConnection(id='service:branch-link',source_node_id='service:branch-source',
            target_node_id='service:branch-target',specification_status='canonical_circuit',circuit_id='F003'),))


@pytest.mark.parametrize('case',['domain','terminal_role','auxiliary_conductor','port_only_link'])
def test_service_wrapper_uses_identical_established_circuit_refusals(case):
    from test_single_line import make_model
    model=make_model(False)
    if case=='domain':
        model=replace(model,ports=tuple(replace(port,domain='mechanical') if port.id=='F003-out' else port for port in model.ports))
    elif case=='terminal_role':
        model=replace(model,ports=tuple(replace(port,role='neutral') if port.id=='F003-in' else port for port in model.ports))
    elif case=='auxiliary_conductor':
        model=replace(model,circuits=tuple(replace(circuit,voltage_v=None,poles=None,
            attributes={'single_line_drafting':{'connection_kind':'grounding'}}) if circuit.id=='F003' else circuit for circuit in model.circuits))
    else:
        links={'F001-out':('F003-in',),'F003-in':('F001-out',)}
        model=replace(model,ports=tuple(replace(port,connected_port_ids=links[port.id]) if port.id in links else port for port in model.ports))
    with pytest.raises(ValueError) as plain:project_topology(model)
    with pytest.raises(ValueError) as service:project_topology(wrap_branch_circuit(model),service_only=True)
    assert str(service.value)==str(plain.value)


def test_admitted_service_circuit_retains_original_fields_and_port_identities():
    from test_single_line import make_model
    model=make_model(False)
    original=next(row for row in project_topology(model).connections if row['circuit_id']=='F003')
    projected,=project_topology(wrap_branch_circuit(model),service_only=True).connections
    for key in ('circuit_id','source_port_id','load_port_id','drafting','voltage_v','poles','phase','wire_labels','conductor_specification','route_ids','conductor_ids'):
        assert projected[key]==original[key]


def test_dangling_refs_conflicting_facts_wrong_frame_and_stale_lateral_refuse():
    model=site_model()
    with pytest.raises(ContractError):replace(model,service_nodes=(model.service_nodes[0],))
    with pytest.raises(ContractError):replace(model,service_equipment_facts=(model.service_equipment_facts[0],replace(model.service_equipment_facts[0],id='facts:duplicate')))
    with pytest.raises(ContractError):replace(model,coordinate_system=CoordinateSystem(frame_id='other'))
    moved=replace(model.service_nodes[1],plan_position=Point2D(x=4,y=3))
    with pytest.raises(ContractError,match='stale'):
        replace(model,service_nodes=(model.service_nodes[0],moved,model.service_nodes[2]))


def test_real_source_hash_binding_and_model_move_update_projection(tmp_path):
    source=tmp_path/'synthetic-source.bin';source.write_bytes(b'public-safe synthetic evidence')
    digest=hashlib.sha256(source.read_bytes()).hexdigest();model=site_model(digest)
    kwargs=dict(source_files={'source:synthetic':source},drawing={'scale':100.,'placement_mm':[10.,10.],'padding_mm':5.,'legend_width_mm':95.})
    request,receipt=project_model_site(model,**kwargs)
    assert request['units']=='m' and receipt['plan_only'] is True
    assert any(row['id']=='service:utility' for row in receipt['omissions'])
    assert next(row for row in request['features'] if row['id']=='service:main')['points']==[[3,3]]
    moved=replace(model.service_nodes[1],plan_position=Point2D(x=4,y=3))
    lateral=replace(model.plan_service_laterals[0],geometry=geometry('polyline',[(4,3),(0,3)]))
    revised=replace(model,service_nodes=(model.service_nodes[0],moved,model.service_nodes[2]),plan_service_laterals=(lateral,))
    changed,binding=project_model_site(revised,**kwargs)
    assert binding['model_sha256']!=receipt['model_sha256']
    assert next(row for row in changed['features'] if row['id']=='lateral:private')['points'][0]==[4,3]
    source.write_bytes(b'changed')
    with pytest.raises(ContractError,match='stale'):project_model_site(model,**kwargs)


def test_supplied_demand_is_separate_source_bound_arithmetic(tmp_path):
    source=tmp_path/'source';source.write_bytes(b'synthetic calculation')
    digest=hashlib.sha256(source.read_bytes()).hexdigest();model=site_model(digest)
    components=[dict(id='load:'+group,name=group,group=group,va=va,source_id='source:synthetic',basis='Synthetic supplied allowance',assumption=True)
                for group,va in [('general',20000),('hvac',2000),('floor_heat',500)]]
    profile=dict(kind='supplied_service_demand_inputs_v1',service_node_id='service:main',voltage_v=240,
        sources={'source:synthetic':digest},components=components,
        general_demand={'first_va':10000,'remainder_factor':.4,'basis':'Supplied synthetic policy','source_id':'source:synthetic'},
        sensitivity={'additional_va':4000,'basis':'Supplied sensitivity','source_id':'source:synthetic'})
    original=model.to_json();report=bind_service_demand_inputs(model,profile,source_files={'source:synthetic':source})
    assert report['no_supplemental_heat_a']==68.75 and report['general_demand_va_exact']=='14000'
    assert model.to_json()==original and report['connected_circuit_values_modified'] is False
    assert validate_service_demand_report(model,json.dumps(report),source_files={'source:synthetic':source})==report
    changed={**report,'no_supplemental_heat_a':0}
    with pytest.raises(ContractError):validate_service_demand_report(model,json.dumps(changed),source_files={'source:synthetic':source})
    stale={**report['inputs'],'source_model_sha256':'0'*64}
    with pytest.raises(ContractError,match='stale'):calculate_service_demand(model,stale,source_files={'source:synthetic':source})


def test_physical_consumers_refuse_populated_site_records_before_geometry():
    from oabm.exports.gltf import build_glb_bytes
    from oabm.quantities import extract_quantities
    for consumer in (build_glb_bytes,extract_quantities):
        with pytest.raises(ContractError,match='site/service inventory'):consumer(site_model())


def test_service_feeder_grid_preserves_unresolved_relationship_row_owners():
    from oabm.drawings.single_line.feeder_grid import paginate_feeder_grid
    topology=project_topology(site_model(),service_only=True)
    before=json.dumps(topology.to_dict(),sort_keys=True)
    ids=[edge['id'] for edge in topology.connections]
    pages=paginate_feeder_grid(topology,ids,(10,10,560,350),sheet_id='U2',width_mm=594,height_mm=420)
    rows=[row for page in pages for row in page['row_map']]
    assert {row['source_id'] for row in rows}==set(ids)
    assert {row['connection_id'] for row in rows}==set(ids)
    import xml.etree.ElementTree as ET
    text=' '.join(' '.join(ET.fromstring(page['body_svg']).itertext()) for page in pages)
    assert 'CIRCUIT UNRESOLVED' in text and 'TERMINAL UNRESOLVED' in text
    assert 'None' not in text
    assert all(edge['circuit_id'] is None and edge['source_port_id'] is None and edge['load_port_id'] is None for edge in topology.connections)
    assert json.dumps(topology.to_dict(),sort_keys=True)==before


def test_explicit_field_edit_updates_lateral_atomically_without_fake_ports_or_source_rebinding():
    from oabm.electrical.service_site import move_plan_service_node
    from oabm.model import Provenance
    model=site_model();original=model.to_json()
    edited=move_plan_service_node(model,'service:main',Point2D(x=4,y=3),
        field_evidence=Provenance(source_kind='manual',source_id='field:synthetic-event',derivation='user'))
    assert model.to_json()==original
    assert edited.plan_service_laterals[0].geometry.points[0]==Point2D(x=4,y=3)
    assert edited.plan_service_laterals[0].id==model.plan_service_laterals[0].id
    assert edited.service_nodes[1].id==model.service_nodes[1].id
    assert edited.service_nodes[1].evidence==model.service_nodes[1].evidence
    assert not edited.ports and not edited.routes and not edited.electrical_equipment


def test_meter_main_rejects_a_contradicted_integrated_disconnect():
    model=site_model()
    facts=replace(model.service_equipment_facts[0],integrated_service_disconnect=False)
    with pytest.raises(ContractError,match='contradict'):
        replace(model,service_equipment_facts=(facts,))


def test_known_240v_service_refuses_120v_demand_basis(tmp_path):
    from oabm.model import ElectricalEquipment,Pose,Point3
    source=tmp_path/'source';source.write_bytes(b'synthetic calculation')
    digest=hashlib.sha256(source.read_bytes()).hexdigest();model=site_model(digest)
    owner=ElectricalEquipment(id='equipment:main',equipment_type='meter_main',rated_voltage_v=240,
        pose=Pose(position=Point3(x=3,y=3,z=2)))
    node=replace(model.service_nodes[1],location_basis='placed_equipment',equipment_id=owner.id,
        plan_position=None,unresolved_reason=None)
    model=replace(model,electrical_equipment=(owner,),service_nodes=(model.service_nodes[0],node,model.service_nodes[2]))
    components=[dict(id='load:'+group,name=group,group=group,va=va,source_id='source:synthetic',basis='Synthetic input',assumption=True)
                for group,va in [('general',20000),('hvac',2000),('floor_heat',500)]]
    profile=dict(kind='supplied_service_demand_inputs_v1',service_node_id=node.id,voltage_v=120,
        sources={'source:synthetic':digest},components=components,
        general_demand={'first_va':10000,'remainder_factor':.4,'basis':'Supplied policy','source_id':'source:synthetic'},
        sensitivity={'additional_va':4000,'basis':'Supplied scenario','source_id':'source:synthetic'})
    with pytest.raises(ContractError,match='voltage contradicts'):
        bind_service_demand_inputs(model,profile,source_files={'source:synthetic':source})
    assert bind_service_demand_inputs(model,{**profile,'voltage_v':240},source_files={'source:synthetic':source})['no_supplemental_heat_a']==68.75
