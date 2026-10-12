"""Public synthetic shared inventory; no verified loading/spacing/profile claims."""
import json
import struct
from dataclasses import replace
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from oabm.model import (BuildingModel,CatalogProduct,Ceiling,Circuit,Conductor,ContractError,Point3,
    Polygon3D,Port,Pose,Provenance,RuleReference,Size3,Vector3,upgrade_to_v1_3)
from oabm.electrical import (SupportCatalogEntry,SupportRule,SupportIntent,SupportPartIntent,
    SupportInterfaceIntent,select_supports)
from oabm.ifc import to_ifc,from_ifc,round_trip_differences,IfcAdapterError,canonical_id_to_ifc_guid
from oabm.quantities import extract_quantities
from oabm.drawings import generate_schedules,generate_plan,PlanSpec,Bounds2
from oabm.qa.golden import iter_entities,validate_lane_model
from oabm.routing import route_between_ports
ROOT=Path(__file__).resolve().parents[1]
PROV=(Provenance(source_kind='synthetic',source_id='fixture:shared-trapeze',derivation='user'),)

def pose(x,y,z): return Pose(position=Point3(x=x,y=y,z=z))

@pytest.fixture
def case():
    base=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
    level=base.levels[0].id
    source=replace(base.electrical_equipment[0],pose=pose(1,2.15,2),size=Size3(x=.5,y=.5,z=.6),host_id=None,provenance=PROV)
    first=replace(base.electrical_devices[0],pose=pose(5,2,2),host_id=None,provenance=PROV)
    second=replace(first,id='device:second-load',pose=pose(5,2.3,2))
    ports=(replace(base.ports[0],pose=pose(1,2,2),direction=Vector3(x=1,y=0,z=0),connected_port_ids=(),provenance=PROV),
        replace(base.ports[1],pose=first.pose,direction=Vector3(x=-1,y=0,z=0),connected_port_ids=(),provenance=PROV),
        Port(id='port:source-second',owner_id=source.id,domain=base.ports[0].domain,role='source',pose=pose(1,2.3,2),
             direction=Vector3(x=1,y=0,z=0),provenance=PROV),
        Port(id='port:load-second',owner_id=second.id,domain=base.ports[0].domain,role='sink',pose=second.pose,
             direction=Vector3(x=-1,y=0,z=0),provenance=PROV))
    ceilings=tuple(Ceiling(id=f'ceiling:structure-{key}',level_id=level,thickness_m=.1,
        footprint=Polygon3D(points=(Point3(x=0,y=lo,z=3),Point3(x=6,y=lo,z=3),
                                   Point3(x=6,y=hi,z=3),Point3(x=0,y=hi,z=3))),provenance=PROV)
        for key,lo,hi in (('a',0,2.15),('b',2.15,5)))
    base=replace(base,electrical_equipment=(source,),electrical_devices=(first,second),ports=ports,
        routes=(),route_fittings=(),circuits=(),conductors=(),ceilings=ceilings,obstacles=(),route_constraints=())
    routes=[];fittings=[]
    for start,end in ((ports[0],ports[1]),(ports[2],ports[3])):
        route,fs=route_between_ports(base,start.id,end.id,'emt',nominal_diameter_m=.02)
        routes.append(route);fittings.extend(fs)
    circuits=(Circuit(id='circuit:evse',source_port_id=ports[0].id,load_port_ids=(ports[1].id,),route_ids=(routes[0].id,),provenance=PROV),
        Circuit(id='circuit:second',source_port_id=ports[2].id,load_port_ids=(ports[3].id,),route_ids=(routes[1].id,),provenance=PROV))
    conductors=tuple(Conductor(id=f'conductor:fixture-{i}',circuit_id=c.id,role='phase',count=2,route_ids=c.route_ids,provenance=PROV)
                     for i,c in enumerate(circuits))
    base=replace(base,routes=tuple(routes),route_fittings=tuple(fittings),circuits=circuits,conductors=conductors)
    products={key:CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id=key) for key in ('rail','rod','anchor','clip')}
    catalog=tuple(SupportCatalogEntry(product=products[key],category=category,component_type=key,
        size=Size3(x=x,y=y,z=z),provenance=PROV) for key,category,x,y,z in
        (('rail','support',.12,.7,.1),('rod','support',.012,.012,1.04),
         ('anchor','fastener',.08,.08,.06),('clip','support',.06,.06,.08)))
    basis=RuleReference(authority='synthetic-company',revision='1',rule_id='explicit-trapeze-a')
    rule=SupportRule(reference=basis,permitted_products=tuple(products.values()),permitted_substrates=('steel','concrete'),provenance=PROV)
    parts=tuple(SupportPartIntent(part_key=f'part:{key}',product=products[product],pose=pose(x,y,z))
        for key,product,x,y,z in (('rail','rail',3,2.15,1.91),('rod-a','rod',3,1.9,2.48),
            ('rod-b','rod',3,2.4,2.48),('anchor-a','anchor',3,1.9,3),('anchor-b','anchor',3,2.4,3),
            ('clip-a','clip',3,2,2),('clip-b','clip',3,2.3,2)))
    interfaces=(SupportInterfaceIntent(interface_key='interface:a',host_id=ceilings[0].id,substrate='steel',pose=pose(3,1.9,3),
        part_keys=('part:anchor-a','part:rod-a')),
        SupportInterfaceIntent(interface_key='interface:b',host_id=ceilings[1].id,substrate='concrete',pose=pose(3,2.4,3),
        part_keys=('part:anchor-b','part:rod-b')))
    intent=SupportIntent(intent_id='intent:trapeze',actor_id='synthetic-reviewer',assembly_type='trapeze',level_id=level,
        supported_object_ids=tuple(r.id for r in routes),parts=parts,interfaces=interfaces,basis=basis)
    return base,intent,catalog,rule

def selected(case):
    base,intent,catalog,rule=case
    return select_supports(upgrade_to_v1_3(base),intents=(intent,),catalog=catalog,rules=(rule,))


def test_router_shared_producer_ifc_glb_quantities_drawings_qa_proof(case,tmp_path):
    from oabm.exports.gltf import to_glb
    m=selected(case)
    assert len(m.support_assemblies)==1 and len(m.support_components)==7 and len(m.support_interfaces)==2
    assert len(m.support_assemblies[0].supported_object_ids)==2
    assert m.routes==case[0].routes and m.obstacles==case[0].obstacles
    ifc=to_ifc(m)
    assert len(ifc.by_type('IfcMechanicalFastener'))==2
    assert len(ifc.by_type('IfcDiscreteAccessory'))==5
    restored=from_ifc(ifc)
    assert round_trip_differences(m,restored)==()
    schema=json.loads((ROOT/'contracts/oabm-model-v1.3.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(m.to_dict())
    report=extract_quantities(restored)
    components=[q for q in report.items if q.category=='support_component']
    assert sum(q.quantity for q in components)==7
    assert not any(q.category in ('support_assembly','support_interface','support_outcome') for q in report.items)
    assert all(q.to_dict()['quantity_derivation']=='user' for q in components)
    for category in ('route_length','conductor_length'):
        assert sum(q.quantity for q in report.items if q.category==category)==pytest.approx(
            sum(q.quantity for q in extract_quantities(case[0]).items if q.category==category))
    assert any(w.code=='support_rules_unverified' for w in report.warnings)
    plan=generate_plan(restored,PlanSpec(id='plan:shared',level_id=m.levels[0].id,cut_height_m=3.1,bounds=Bounds2(min_x=0,min_y=0,max_x=6,max_y=5)))
    assert any(p.layer=='electrical:supports' for p in plan.primitives)
    assert any(p.layer=='electrical:support-interfaces' for p in plan.primitives)
    rows={row.source_id for s in generate_schedules(restored) for row in s.rows}
    assert {e.id for e in (*m.support_assemblies,*m.support_components,*m.support_interfaces,*m.support_selection_outcomes)} <= rows
    assert validate_lane_model(restored)
    assert {c.id for c in m.support_components}<={e.id for e in iter_entities(m)}
    path=tmp_path/'shared.glb';to_glb(restored,path)
    data=path.read_bytes();length,_=struct.unpack_from('<II',data,12);glb=json.loads(data[20:20+length])
    assembly_node=next(n for n in glb['nodes'] if n.get('name')==m.support_assemblies[0].id)
    assert len(assembly_node['children'])==9
    assert len([n for n in glb['nodes'] if n.get('extras',{}).get('canonical_type')=='support_component'])==7
    assert all(n['extras']['geometry_basis']=='catalog_exterior_envelope' for n in glb['nodes']
               if n.get('extras',{}).get('canonical_type')=='support_component')
    assert len(glb['asset']['extras']['support_interfaces'])==2

@pytest.mark.parametrize('change,reason', [('substrate','unknown_substrate'),('framed','unsupported_actual_substrate'),
    ('level','missing_level'),('host','missing_host'),('part_pose','missing_component_pose'),
    ('attachment_pose','missing_attachment_pose'),('basis','missing_rule_reference'),
    ('catalog_size','missing_catalog_envelope'),('catalog_category','missing_component_category')])
def test_atomic_unresolved_has_no_invented_hardware(case,change,reason,tmp_path):
    from oabm.exports.gltf import to_glb
    base,intent,catalog,rule=case
    if change in ('substrate','framed'):
        interface=replace(intent.interfaces[0],substrate=None if change=='substrate' else 'framed',host_id=base.walls[0].id)
        base=replace(base,walls=(replace(base.walls[0],construction='framed'),*base.walls[1:]))
        intent=replace(intent,interfaces=(interface,*intent.interfaces[1:]))
    elif change=='level': intent=replace(intent,level_id=None)
    elif change=='host': intent=replace(intent,interfaces=(replace(intent.interfaces[0],host_id=None),*intent.interfaces[1:]))
    elif change=='part_pose': intent=replace(intent,parts=(replace(intent.parts[0],pose=None),*intent.parts[1:]))
    elif change=='attachment_pose': intent=replace(intent,interfaces=(replace(intent.interfaces[0],pose=None),*intent.interfaces[1:]))
    elif change=='basis': intent=replace(intent,basis=None)
    elif change=='catalog_size': catalog=(replace(catalog[0],size=None),*catalog[1:])
    elif change=='catalog_category': catalog=(replace(catalog[0],category=None),*catalog[1:])
    m=selected((base,intent,catalog,rule))
    assert not m.support_assemblies and not m.support_components and not m.support_interfaces
    assert reason in m.support_selection_outcomes[0].reason_codes
    assert not any(q.category=='support_component' for q in extract_quantities(m).items)
    assert any(w.code=='unresolved_support_selection' for w in extract_quantities(m).warnings)
    assert round_trip_differences(m,from_ifc(to_ifc(m)))==()
    assert any(row.source_id==m.support_selection_outcomes[0].id for s in generate_schedules(m) for row in s.rows)
    to_glb(m,tmp_path/'unresolved.glb')

@pytest.mark.parametrize('owner',['route-equipment','route-device','device','equipment'])
def test_missing_supported_object_or_endpoint_level_is_explicit_unresolved(case,owner):
    base,intent,catalog,rule=case
    if owner in ('route-equipment','equipment'):
        base=replace(base,electrical_equipment=(replace(base.electrical_equipment[0],level_id=None),))
        if owner=='equipment': intent=replace(intent,supported_object_ids=(base.electrical_equipment[0].id,))
    else:
        base=replace(base,electrical_devices=(replace(base.electrical_devices[0],level_id=None),*base.electrical_devices[1:]))
        if owner=='device': intent=replace(intent,supported_object_ids=(base.electrical_devices[0].id,))
    m=selected((base,intent,catalog,rule))
    assert not m.support_components
    assert m.support_selection_outcomes[0].status=='unresolved'
    assert 'supported_object_level_unknown' in m.support_selection_outcomes[0].reason_codes
    # A hand-authored selected inventory cannot bypass the producer's same invariant.
    installed=selected(case)
    with pytest.raises(ContractError,match='known supported-object'):
        replace(installed,electrical_devices=base.electrical_devices,electrical_equipment=base.electrical_equipment)


def test_explicit_levels_cross_host_and_permission_conflicts_are_unresolved(case):
    from oabm.model import Level
    base,intent,catalog,rule=case
    base=replace(base,levels=(*base.levels,Level(id='level:upper',elevation_m=4)))
    base=replace(base,ceilings=(replace(base.ceilings[0],level_id='level:upper'),*base.ceilings[1:]))
    m=selected((base,intent,catalog,rule))
    assert not m.support_components
    assert 'multi_level_support_unsupported' in m.support_selection_outcomes[0].reason_codes
    base,intent,catalog,rule=case
    denied=replace(rule,permitted_substrates=('wood',),permitted_products=(catalog[0].product,))
    m=selected((base,intent,catalog,denied))
    assert not m.support_components
    assert {'caller_substrate_permission_conflict','caller_product_permission_conflict'}<=set(m.support_selection_outcomes[0].reason_codes)


def test_idempotence_permutation_stable_upsert_and_no_accumulated_history(case):
    base,intent,catalog,rule=case;m=selected(case)
    again=select_supports(m,intents=(intent,),catalog=catalog,rules=(rule,))
    assert again.to_json()==m.to_json()
    permuted=replace(intent,supported_object_ids=tuple(reversed(intent.supported_object_ids)),parts=tuple(reversed(intent.parts)),
        interfaces=tuple(replace(i,part_keys=tuple(reversed(i.part_keys))) for i in reversed(intent.interfaces)))
    reverse_rule=replace(rule,permitted_products=tuple(reversed(rule.permitted_products)),permitted_substrates=tuple(reversed(rule.permitted_substrates)))
    assert selected((base,permuted,tuple(reversed(catalog)),reverse_rule)).to_json()==m.to_json()
    updated_catalog=tuple(replace(c,size=Size3(x=.14,y=.7,z=.1)) if c.component_type=='rail' else c for c in catalog)
    updated=select_supports(m,intents=(intent,),catalog=updated_catalog,rules=(rule,))
    assert {c.id for c in updated.support_components}=={c.id for c in m.support_components}
    assert len(updated.support_selection_outcomes)==1
    unresolved=select_supports(updated,intents=(replace(intent,basis=None),),catalog=catalog,rules=(rule,))
    assert not unresolved.support_components and len(unresolved.support_selection_outcomes)==1


def test_catalog_scopes_and_low_confidence_host_evidence_are_not_widened(case):
    from oabm.model import provenance_applies_to
    base,intent,catalog,rule=case
    host=replace(base.ceilings[0],confidence=.6,provenance=(Provenance(source_kind='synthetic',source_id='fixture:weak-host',
        derivation='observed',confidence=.2,scope_paths=('level_id',)),))
    base=replace(base,ceilings=(host,*base.ceilings[1:]))
    scoped_catalog=tuple(replace(c,provenance=(replace(c.provenance[0],scope_paths=('size.x',)),)) for c in catalog)
    m=selected((base,intent,scoped_catalog,rule))
    interface=next(i for i in m.support_interfaces if i.host_id==host.id)
    inferred=next(p for p in interface.provenance if p.scope_paths==('level_id',))
    assert interface.confidence==inferred.confidence==.2
    assert inferred.derivation=='inferred' and inferred.source_element_id==host.id
    user=interface.provenance[0]
    assert not provenance_applies_to(user,('level_id',)) and user.confidence==1
    for component in m.support_components:
        catalog_record=component.provenance[1]
        assert catalog_record.scope_paths==('size.x',)
        assert not provenance_applies_to(catalog_record,('size.y','size.z','product','category'))
        assert not provenance_applies_to(component.provenance[0],('size','category','product','level_id'))

@pytest.mark.parametrize('collection,field,value',[
    ('support_components','assembly_id','assembly:missing'),('support_components','level_id','level:missing'),
    ('support_components','geometry_basis','exact_thread_profile'),('support_interfaces','host_id','wall:missing'),
    ('support_interfaces','component_ids',['component:missing']),('support_interfaces','substrate',None),
    ('support_assemblies','component_ids',['component:missing']),('support_assemblies','supported_object_ids',['route:missing']),
    ('support_selection_outcomes','selected_assembly_id','assembly:missing')])
def test_portable_dangling_or_malformed_support_references_fail(case,collection,field,value):
    doc=selected(case).to_dict();doc[collection][0][field]=value
    with pytest.raises(ContractError): BuildingModel.from_dict(doc)


def test_duplicate_part_interface_shared_owner_and_coincident_instance_refuse(case):
    base,intent,catalog,rule=case
    with pytest.raises(ContractError): replace(intent,parts=(intent.parts[0],intent.parts[0]))
    with pytest.raises(ContractError): replace(intent,interfaces=(intent.interfaces[0],intent.interfaces[0]))
    with pytest.raises(ContractError): replace(intent,supported_object_ids=intent.supported_object_ids*2)
    m=selected(case)
    with pytest.raises(ContractError): replace(m,support_components=(*m.support_components,replace(m.support_components[0],id='component:duplicate')))
    with pytest.raises(ContractError): replace(m,support_assemblies=(replace(m.support_assemblies[0],component_ids=m.support_assemblies[0].component_ids*2),))
    with pytest.raises(ContractError): select_supports(upgrade_to_v1_3(base),intents=(intent,intent),catalog=catalog,rules=(rule,))
    with pytest.raises(ContractError): select_supports(upgrade_to_v1_3(base),intents=(intent,),catalog=(*catalog,catalog[0]),rules=(rule,))
    with pytest.raises(ContractError): select_supports(upgrade_to_v1_3(base),intents=(intent,),catalog=catalog,rules=(rule,rule))
    bad=replace(intent,parts=(*intent.parts,replace(intent.parts[0],part_key='part:coincident')))
    with pytest.raises(ContractError,match='coincident'):
        selected((base,bad,catalog,rule))


def test_off_envelope_attachment_and_non_electrical_support_owned_ports_refuse(case):
    base,intent,catalog,rule=case
    with pytest.raises(ContractError,match='outside'):
        selected((base,replace(intent,interfaces=(replace(intent.interfaces[0],pose=pose(100,100,100)),*intent.interfaces[1:])),catalog,rule))
    m=selected(case)
    for owner in (*m.support_assemblies,*m.support_components,*m.support_interfaces,*m.support_selection_outcomes):
        port=Port(id='port:invalid-support',owner_id=owner.id,domain='power',role='sink',pose=pose(3,2,2),direction=Vector3(x=1,y=0,z=0))
        with pytest.raises(ContractError,match='non-electrical'):
            replace(m,ports=(*m.ports,port))

@pytest.mark.parametrize('kind',['support_component','support_interface'])
@pytest.mark.parametrize('edit',['remove','reassign'])
def test_native_storey_containment_must_match_explicit_component_interface_level(case,kind,edit):
    import ifcopenshell.api.spatial
    from oabm.model import Level
    m=selected(case);m=replace(m,levels=(*m.levels,Level(id='level:upper',elevation_m=4)))
    ifc=to_ifc(m)
    entity=m.support_components[0] if kind=='support_component' else m.support_interfaces[0]
    native=ifc.by_guid(canonical_id_to_ifc_guid(entity.id))
    if edit=='remove':
        ifcopenshell.api.spatial.unassign_container(ifc,products=[native])
    else:
        upper=ifc.by_guid(canonical_id_to_ifc_guid('level:upper'))
        ifcopenshell.api.spatial.assign_container(ifc,products=[native],relating_structure=upper)
    with pytest.raises(IfcAdapterError,match='spatial containment'):
        from_ifc(ifc)


def test_native_component_and_attachment_pose_edit_reads_real_geometry_and_scoped_history(case):
    import numpy as np
    import ifcopenshell.api.geometry
    import ifcopenshell.util.placement
    import ifcopenshell.util.element
    m=selected(case);ifc=to_ifc(m)
    interface=m.support_interfaces[0]
    moving=[next(c for c in m.support_components if c.id==cid) for cid in interface.component_ids]+[interface]
    for entity in moving:
        native=ifc.by_guid(canonical_id_to_ifc_guid(entity.id))
        before=ifcopenshell.util.element.get_psets(native)['OABM_Canonical']['CanonicalJson']
        matrix=np.asarray(ifcopenshell.util.placement.get_local_placement(native.ObjectPlacement)).copy();matrix[0,3]+=.05
        ifcopenshell.api.geometry.edit_object_placement(ifc,product=native,matrix=matrix,is_si=True,should_transform_children=False)
        assert ifcopenshell.util.element.get_psets(native)['OABM_Canonical']['CanonicalJson']==before
    restored=from_ifc(ifc)
    by_id={e.id:e for e in (*restored.support_components,*restored.support_interfaces)}
    for original in moving:
        edited=by_id[original.id]
        assert edited.pose.position.x==pytest.approx(original.pose.position.x+.05)
        assert edited.provenance[-1].derivation=='user' and edited.provenance[-1].scope_paths==('pose',)
    assert sum(q.quantity for q in extract_quantities(restored).items if q.category=='support_component')==7
    assert round_trip_differences(restored,from_ifc(to_ifc(restored)))==()

@pytest.mark.parametrize('edit',['body_dimensions','missing_body','component_type','category',
    'exact_fastener_diameter','group_member','interface_host','component_projection','interface_projection','assembly_projection'])
def test_native_geometry_grouping_host_and_owned_semantic_contradictions_refuse(case,edit):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    m=selected(case);ifc=to_ifc(m)
    fastener=next(c for c in m.support_components if c.category=='fastener')
    native=ifc.by_guid(canonical_id_to_ifc_guid(fastener.id))
    if edit=='body_dimensions':
        body=next(r for r in native.Representation.Representations if r.RepresentationIdentifier=='Body');body.Items[0].Depth*=2
    elif edit=='missing_body':
        native.Representation=None
    elif edit=='component_type': native.ObjectType='different-declared-component'
    elif edit=='category': native.PredefinedType='NOTDEFINED'
    elif edit=='exact_fastener_diameter': native.NominalDiameter=.006
    elif edit=='group_member':
        group=ifc.by_guid(canonical_id_to_ifc_guid(m.support_assemblies[0].id))
        relation=group.IsGroupedBy[0];relation.RelatedObjects=relation.RelatedObjects[:-1]
    elif edit=='interface_host':
        interface=ifc.by_guid(canonical_id_to_ifc_guid(m.support_interfaces[0].id))
        rel=next(r for r in interface.HasAssignments if r.is_a('IfcRelAssignsToProduct'))
        rel.RelatingProduct=ifc.by_guid(canonical_id_to_ifc_guid(m.walls[0].id))
    else:
        if edit=='interface_projection':
            native=ifc.by_guid(canonical_id_to_ifc_guid(m.support_interfaces[0].id));name='OABM_SupportInterface';key='ActualSubstrate'
        elif edit=='assembly_projection':
            native=ifc.by_guid(canonical_id_to_ifc_guid(m.support_assemblies[0].id));name='OABM_SupportAssembly';key='RuleId'
        else: name='OABM_SupportComponent';key='ProductId'
        pset=ifc.by_id(ifcopenshell.util.element.get_psets(native)[name]['id'])
        ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={key:'contradictory-native-edit'})
    with pytest.raises(IfcAdapterError): from_ifc(ifc)

@pytest.mark.parametrize('pset_name',['OABM_Canonical','OABM_SupportOutcome'])
@pytest.mark.parametrize('field,value',[('SelectionStatus','selected'),('UnresolvedReasons',''),
    ('SelectedAssemblyId','assembly:unrecorded'),('RequestedRuleId','unrecorded-rule'),('RequestedLevelId','level:unrecorded')])
def test_unresolved_annotations_compare_presence_and_values_of_all_owned_outcome_fields(case,pset_name,field,value):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    base,intent,catalog,rule=case
    m=selected((base,replace(intent,basis=None,level_id=None),catalog,rule));ifc=to_ifc(m)
    native=ifc.by_guid(canonical_id_to_ifc_guid(m.support_selection_outcomes[0].id))
    pset=ifc.by_id(ifcopenshell.util.element.get_psets(native)[pset_name]['id'])
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={field:value})
    with pytest.raises(IfcAdapterError,match='projection contradicts'):
        from_ifc(ifc)

@pytest.mark.parametrize('pset_name',['OABM_SupportAssembly','OABM_SupportComponent','OABM_SupportInterface','OABM_SupportOutcome'])
def test_plain_1_3_empty_arrays_refuse_fabricated_support_psets(case,pset_name):
    import ifcopenshell.api.pset
    base=upgrade_to_v1_3(case[0]);ifc=to_ifc(base)
    native=ifc.by_guid(canonical_id_to_ifc_guid(base.electrical_devices[0].id))
    pset=ifcopenshell.api.pset.add_pset(ifc,product=native,name=pset_name)
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'ProductId':'fabricated-part','ActualSubstrate':'steel'})
    with pytest.raises(IfcAdapterError,match='no matching typed'):
        from_ifc(ifc)


def test_unrelated_native_notes_are_allowed_on_support_projections(case):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    m=selected(case);ifc=to_ifc(m)
    for entity,name in ((m.support_assemblies[0],'OABM_SupportAssembly'),(m.support_components[0],'OABM_SupportComponent'),
        (m.support_interfaces[0],'OABM_SupportInterface'),(m.support_selection_outcomes[0],'OABM_SupportOutcome')):
        native=ifc.by_guid(canonical_id_to_ifc_guid(entity.id))
        pset=ifc.by_id(ifcopenshell.util.element.get_psets(native)[name]['id'])
        ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'CustomReviewNote':'synthetic unrelated note'})
    assert round_trip_differences(m,from_ifc(ifc))==()


def test_explicit_1_3_migration_preserves_previous_contract_and_rejects_downgrades(case):
    from oabm.model import upgrade_to_v1_1,upgrade_to_v1_2
    base=case[0]
    fields=('support_assemblies','support_components','support_interfaces','support_selection_outcomes')
    schema=json.loads((ROOT/'contracts/oabm-model-v1.3.schema.json').read_text())
    for version,old in (('v1',base),('v1.1',upgrade_to_v1_1(base)),('v1.2',upgrade_to_v1_2(base))):
        doc=old.to_dict()
        assert not set(fields)&set(doc)
        for field in fields:
            with pytest.raises(ContractError,match='unknown'):
                BuildingModel.from_dict({**doc,field:[]})
        migrated=upgrade_to_v1_3(old)
        assert {k:v for k,v in migrated.to_dict().items() if k not in fields and k!='schema_version'}=={
            **{k:v for k,v in doc.items() if k!='schema_version'},
            'electrical_boxes':doc.get('electrical_boxes',[]),'box_selection_outcomes':doc.get('box_selection_outcomes',[]),
            'raceway_selections':doc.get('raceway_selections',[]),'raceway_selection_outcomes':doc.get('raceway_selection_outcomes',[])}
        previous=json.loads((ROOT/f'contracts/oabm-model-{version}.schema.json').read_text())
        assert all(schema['$defs'][k]==v for k,v in previous['$defs'].items())
        assert upgrade_to_v1_3(migrated)==migrated
    doc=selected(case).to_dict()
    for field in fields:
        bad=dict(doc);del bad[field]
        with pytest.raises(ContractError): BuildingModel.from_dict(bad)
    for bad in ({**doc,'schema_version':'1.4.0'},{**doc,'support_products':[]}):
        with pytest.raises(ContractError): BuildingModel.from_dict(bad)
    for upgrade in (upgrade_to_v1_1,upgrade_to_v1_2):
        with pytest.raises(ContractError): upgrade(selected(case))
    with pytest.raises(ContractError): select_supports(base,intents=(),catalog=(),rules=())


def test_multiple_shared_assemblies_count_components_once_and_permutation_is_stable(case):
    base,intent,catalog,rule=case
    def shifted(p): return replace(p,position=replace(p.position,x=p.position.x+1))
    second=replace(intent,intent_id='intent:second-trapeze',
        parts=tuple(replace(p,pose=shifted(p.pose)) for p in reversed(intent.parts)),
        interfaces=tuple(replace(i,pose=shifted(i.pose),part_keys=tuple(reversed(i.part_keys))) for i in reversed(intent.interfaces)),
        supported_object_ids=tuple(reversed(intent.supported_object_ids)))
    m=select_supports(upgrade_to_v1_3(base),intents=(intent,second),catalog=catalog,rules=(rule,))
    again=select_supports(upgrade_to_v1_3(base),intents=(second,intent),catalog=tuple(reversed(catalog)),
        rules=(replace(rule,permitted_products=tuple(reversed(rule.permitted_products)),permitted_substrates=tuple(reversed(rule.permitted_substrates))),))
    assert m.to_json()==again.to_json()
    assert len(m.support_assemblies)==2 and len(m.support_components)==14
    assert sum(q.quantity for q in extract_quantities(m).items if q.category=='support_component')==14
    assert round_trip_differences(m,from_ifc(to_ifc(m)))==()
    first,second_assembly=m.support_assemblies
    bad=replace(second_assembly,component_ids=first.component_ids)
    with pytest.raises(ContractError): replace(m,support_assemblies=(first,bad))
    with pytest.raises(ContractError,match='coincident duplicate'):
        select_supports(upgrade_to_v1_3(base),intents=(intent,replace(intent,intent_id='intent:duplicate-location')),catalog=catalog,rules=(rule,))


@pytest.mark.parametrize('target',['system','segment'])
@pytest.mark.parametrize('name',['OABM_RacewaySelection','OABM_RacewayOutcome'])
def test_1_3_inherits_raceway_absent_state_checks(case,target,name):
    import ifcopenshell.api.pset
    m=upgrade_to_v1_3(case[0]);ifc=to_ifc(m);rid=m.routes[0].id
    native=ifc.by_guid(canonical_id_to_ifc_guid(rid if target=='system' else f'{rid}#segment:0'))
    pset=ifcopenshell.api.pset.add_pset(ifc,product=native,name=name)
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'ProductId':'invented','RuleId':'invented','SelectionStatus':'selected'})
    with pytest.raises(IfcAdapterError,match='no typed raceway'): from_ifc(ifc)


def test_support_components_do_not_become_router_hard_obstacles(case):
    base=case[0];m=selected(case)
    for route in base.routes:
        actual,fittings=route_between_ports(m,route.start_port_id,route.end_port_id,route.route_type,
            nominal_diameter_m=route.nominal_diameter_m)
        before,before_fittings=route_between_ports(base,route.start_port_id,route.end_port_id,route.route_type,
            nominal_diameter_m=route.nominal_diameter_m)
        assert actual==before and fittings==before_fittings

@pytest.mark.parametrize('rotation',[(0,0,0,1),(0,0,1,0),(0,0,-1,0),(0,0,0,-1)])
def test_coincident_product_envelopes_reject_box_symmetries(case,rotation):
    from oabm.model import Quaternion
    m=selected(case);rail=next(c for c in m.support_components if c.component_type=='rail')
    extra=replace(rail,id='support:duplicate',part_key='part:duplicate',pose=replace(rail.pose,rotation=Quaternion(x=rotation[0],y=rotation[1],z=rotation[2],w=rotation[3])))
    assembly=replace(m.support_assemblies[0],component_ids=(*m.support_assemblies[0].component_ids,extra.id))
    with pytest.raises(ContractError,match='coincident duplicate'):
        replace(m,support_assemblies=(assembly,),support_components=(*m.support_components,extra))


def test_noncoincident_rotation_is_not_collapsed_into_duplicate(case):
    import math
    from oabm.model import Quaternion
    m=selected(case);rail=next(c for c in m.support_components if c.component_type=='rail')
    extra=replace(rail,id='support:cross-rail',part_key='part:cross-rail',pose=replace(rail.pose,
        rotation=Quaternion(x=0,y=0,z=math.sin(math.pi/4),w=math.cos(math.pi/4))))
    assembly=replace(m.support_assemblies[0],component_ids=(*m.support_assemblies[0].component_ids,extra.id))
    assert len(replace(m,support_assemblies=(assembly,),support_components=(*m.support_components,extra)).support_components)==8


def test_shipped_1_2_consumer_fingerprints_unchanged(tmp_path):
    """Measured from public shipped #272 source 5cd3991, before 1.3 edits."""
    import hashlib
    from dataclasses import asdict
    from oabm.model import upgrade_to_v1_2
    from oabm.electrical import RacewayIntent,RacewayCatalogEntry,RacewayRule,select_raceways
    from oabm.exports.gltf import to_glb
    base=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json');route=base.routes[0]
    product=CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id='conduit-a')
    basis=RuleReference(authority='synthetic-company',revision='1',rule_id='explicit-product-a')
    prov=(Provenance(source_kind='synthetic',source_id='fixture:raceway',derivation='user'),)
    entry=RacewayCatalogEntry(product=product,route_type=route.route_type,wiring_method='raceway',
        designation_kind='trade_size',designation='synthetic trade A',nominal_diameter_m=route.nominal_diameter_m,provenance=prov)
    rule=RacewayRule(reference=basis,product=product,route_type=route.route_type,provenance=prov)
    intent=RacewayIntent(intent_id='intent:raceway-a',route_id=route.id,actor_id='synthetic-reviewer',product=product,basis=basis)
    m=select_raceways(upgrade_to_v1_2(base),intents=(intent,),catalog=(entry,),rules=(rule,))
    path=tmp_path/'model.glb';to_glb(m,path)
    values={'json':m.to_json().encode(),'ifc':to_ifc(m).to_string().encode(),'glb':path.read_bytes(),
        'quantities':json.dumps(asdict(extract_quantities(m)),sort_keys=True).encode(),
        'schedules':json.dumps([asdict(s) for s in generate_schedules(m)],sort_keys=True).encode()}
    assert {k:hashlib.sha256(v).hexdigest() for k,v in values.items()}=={
        'glb':'7586e91206ec525073336c4d7f6917843392d436d1dc05e8983b6671aa3bd5c8',
        'ifc':'c18049c72a602d039bb3cd281d35ba33a4c44a95445aee1d5c7319117f74e533',
        'json':'802e4bc87898d0f9371a8f38305c1a325c4f2035522b07af4e7652da9c766c28',
        'quantities':'ec478891986f485daec01df5b06278ebfeac10d0297eafc641c06246df2c0bf9',
        'schedules':'14e0b4d3da3bffbc09ebf264ac38712a9df990a8393c2977b950deb6cb1ac9f2'}
    migrated=upgrade_to_v1_3(m)
    assert migrated.raceway_selections==m.raceway_selections and migrated.raceway_selection_outcomes==m.raceway_selection_outcomes
    assert round_trip_differences(migrated,from_ifc(to_ifc(migrated)))==()


def test_glb_caller_groups_preserve_single_parent_shared_components(case,tmp_path):
    from oabm.exports.gltf import to_glb
    m=selected(case);path=tmp_path/'groups.glb'
    to_glb(m,path,groups={'public-review':(m.support_assemblies[0].id,m.support_components[0].id)})
    data=path.read_bytes();length=struct.unpack_from('<I',data,12)[0];doc=json.loads(data[20:20+length])
    parents={}
    for node in doc['nodes']:
        for child in node.get('children',[]): parents[child]=parents.get(child,0)+1
    assert all(count==1 for count in parents.values())
    assert len([n for n in doc['nodes'] if n.get('name') in {c.id for c in m.support_components}])==7


def test_convergence_preserves_empty_1_3_and_refuses_downstream_supports(case):
    from oabm.importers.pdf_convergence import converge_pdf_models,PdfConvergenceError
    from oabm.importers.pdf_electrical import ElectricalPdfImporter,PdfElectricalDocument,PdfPageTransform
    fixture=json.loads((ROOT/'fixtures/pdf_convergence/v1/simple-garage-convergence.json').read_text())
    architecture=BuildingModel.load(ROOT/fixture['architecture_model'])
    document=PdfElectricalDocument.from_dict(json.loads((ROOT/fixture['electrical_observations']).read_text()))
    electrical=ElectricalPdfImporter().import_document(document,page_transforms={1:PdfPageTransform(**fixture['page_transform'])})
    m=converge_pdf_models(architecture,upgrade_to_v1_3(electrical))
    assert m.schema_version=='1.3.0' and not m.support_components
    downstream=replace(selected(case),attributes={'pdf_architecture':{},'pdf_electrical':{}})
    with pytest.raises(PdfConvergenceError,match='downstream'): converge_pdf_models(downstream,electrical)


def test_unknown_host_level_is_not_taken_from_requested_assembly_level(case):
    base,intent,catalog,rule=case
    base=replace(base,ceilings=(replace(base.ceilings[0],level_id=None),base.ceilings[1]))
    m=selected((base,intent,catalog,rule))
    assert not m.support_components and not m.support_assemblies
    assert 'host_level_unknown' in m.support_selection_outcomes[0].reason_codes
