"""Synthetic explicit-product decisions; these are not verified sizing tables."""
import json
import math
import struct
from dataclasses import replace
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from oabm.model import (BuildingModel,CatalogProduct,ContractError,Provenance,RuleReference,
    upgrade_to_v1_1,upgrade_to_v1_2)
from oabm.electrical import RacewayCatalogEntry,RacewayIntent,RacewayRule,select_raceways
from oabm.ifc import to_ifc,from_ifc,round_trip_differences,IfcAdapterError
from oabm.quantities import extract_quantities
from oabm.drawings import generate_schedules
from oabm.qa.golden import validate_lane_model,iter_entities
from oabm.model import provenance_applies_to
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def base():
    return BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')

@pytest.fixture
def inputs(base):
    product=CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id='conduit-a')
    basis=RuleReference(authority='synthetic-company',revision='1',rule_id='explicit-product-a')
    prov=(Provenance(source_kind='synthetic',source_id='fixture:raceway',derivation='user'),)
    route=base.routes[0]
    entry=RacewayCatalogEntry(product=product,route_type=route.route_type,wiring_method='raceway',
        designation_kind='trade_size',designation='synthetic trade A',nominal_diameter_m=route.nominal_diameter_m,
        provenance=prov)
    rule=RacewayRule(reference=basis,product=product,route_type=route.route_type,provenance=prov)
    intent=RacewayIntent(intent_id='intent:raceway-a',route_id=route.id,actor_id='synthetic-reviewer',product=product,basis=basis)
    return intent,entry,rule

def selected(base,inputs):
    i,e,r=inputs
    return select_raceways(upgrade_to_v1_2(base),intents=(i,),catalog=(e,),rules=(r,))

def test_explicit_version_migration_legacy_readers_and_schema(base,inputs):
    one=base.to_dict();eleven=upgrade_to_v1_1(base).to_dict()
    for legacy in (one,eleven):
        assert 'raceway_selections' not in legacy
        assert BuildingModel.from_dict(legacy).to_dict()==legacy
        for field in ('raceway_selections','raceway_selection_outcomes'):
            with pytest.raises(ContractError,match='unknown'):
                BuildingModel.from_dict({**legacy,field:[]})
    m=selected(base,inputs);doc=m.to_dict()
    schema=json.loads((ROOT/'contracts/oabm-model-v1.2.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(doc)
    assert BuildingModel.from_dict(doc).to_dict()==doc
    assert m.routes==base.routes
    with pytest.raises(ContractError):
        upgrade_to_v1_1(m)
    with pytest.raises(ContractError):
        select_raceways(base,intents=(),catalog=(),rules=())
    for field in ('raceway_selections','raceway_selection_outcomes','electrical_boxes','box_selection_outcomes'):
        bad={**doc};del bad[field]
        with pytest.raises(ContractError):
            BuildingModel.from_dict(bad)
    with pytest.raises(ContractError):
        BuildingModel.from_dict({**doc,'schema_version':'1.4.0'})
    with pytest.raises(ContractError):
        BuildingModel.from_dict({**doc,'raceway_products':[]})
    # All prior portable definition semantics are unchanged in the new schema.
    for version in ('v1','v1.1'):
        previous=json.loads((ROOT/f'contracts/oabm-model-{version}.schema.json').read_text())
        assert all(schema['$defs'][k]==v for k,v in previous['$defs'].items())

@pytest.mark.parametrize('target,changes,reason',[('intent',{'product':None},'missing_catalog_entry'),
    ('intent',{'basis':None},'missing_rule_reference'),('intent',{'scope':'mixed_run'},'mixed_run_unsupported'),
    ('intent',{'scope':'partial_route'},'partial_route_unsupported'),
    ('entry',{'designation':None},'missing_product_designation'),
    ('entry',{'nominal_diameter_m':None},'catalog_nominal_unknown'),
    ('entry',{'nominal_diameter_m':.123},'nominal_diameter_conflict'),
    ('entry',{'route_type':'pvc'},'product_method_conflict'),
    ('rule',{'route_type':'pvc'},'caller_rule_conflict')])
def test_unresolved_conflicting_missing_or_mixed_requests(base,inputs,target,changes,reason):
    i,e,r=inputs
    values={'intent':i,'entry':e,'rule':r};values[target]=replace(values[target],**changes)
    m=selected(base,(values['intent'],values['entry'],values['rule']))
    assert m.routes==base.routes
    assert not m.raceway_selections
    assert reason in m.raceway_selection_outcomes[0].reason_codes
    assert any(w.code=='unresolved_raceway_selection' for w in extract_quantities(m).warnings)
    assert round_trip_differences(m,from_ifc(to_ifc(m)))==()
    assert any(row.source_id==m.raceway_selection_outcomes[0].id for s in generate_schedules(m) for row in s.rows)

def test_missing_intent_and_catalog_cable_cannot_replace_conduit(base,inputs):
    m=select_raceways(upgrade_to_v1_2(base),intents=(),catalog=(),rules=())
    assert all(o.reason_codes==('missing_intent',) for o in m.raceway_selection_outcomes)
    i,e,r=inputs
    cable=replace(e,wiring_method='cable',designation_kind='listed_assembly',designation='synthetic listed cable A')
    m=selected(base,(i,cable,r))
    assert not m.raceway_selections
    assert 'product_method_conflict' in m.raceway_selection_outcomes[0].reason_codes


def test_idempotence_stable_upsert_and_no_catalog_scope_widening(base,inputs):
    i,e,r=inputs;m=selected(base,inputs)
    assert select_raceways(m,intents=(i,),catalog=(e,),rules=(r,)).to_json()==m.to_json()
    changed=replace(e,designation='synthetic trade renamed')
    updated=select_raceways(m,intents=(i,),catalog=(changed,),rules=(r,))
    assert updated.raceway_selections[0].id==m.raceway_selections[0].id
    assert len(updated.raceway_selection_outcomes)==1
    scoped=replace(e,provenance=(replace(e.provenance[0],scope_paths=('designation',)),))
    record=selected(base,(i,scoped,r)).raceway_selections[0].provenance[1]
    assert record.scope_paths==('designation',)
    assert not provenance_applies_to(record,('product','nominal_diameter_m','wiring_method'))
    with pytest.raises(ContractError):
        replace(e,provenance=(replace(e.provenance[0],scope_paths=('basis',)),))
    # User intent must not qualify catalog identity/diameter or reference content.
    user=m.raceway_selections[0].provenance[0]
    assert not provenance_applies_to(user,('product','nominal_diameter_m','designation','basis'))

@pytest.mark.parametrize('collection,field,value',[('raceway_selections','route_id','route:missing'),
    ('raceway_selections','route_type','cable'),('raceway_selections','nominal_diameter_m',.123),
    ('raceway_selection_outcomes','selected_selection_id','selection:missing'),
    ('raceway_selection_outcomes','route_id','route:missing')])
def test_portable_invalid_reference_and_conflict_rejection(base,inputs,collection,field,value):
    doc=selected(base,inputs).to_dict();doc[collection][0][field]=value
    with pytest.raises(ContractError):
        BuildingModel.from_dict(doc)


def test_duplicate_requests_records_and_unknown_catalog_inputs_refuse(base,inputs):
    i,e,r=inputs;m=upgrade_to_v1_2(base)
    for kw in ({'intents':(i,i),'catalog':(e,),'rules':(r,)},
               {'intents':(i,),'catalog':(e,e),'rules':(r,)},
               {'intents':(i,),'catalog':(e,),'rules':(r,r)}):
        with pytest.raises(ContractError):
            select_raceways(m,**kw)
    with pytest.raises(ContractError):
        selected(base,(replace(i,route_id='route:missing'),e,r))
    m=selected(base,inputs)
    with pytest.raises(ContractError):
        replace(m,raceway_selections=(m.raceway_selections[0],replace(m.raceway_selections[0],id='selection:duplicate')))
    with pytest.raises(ContractError):
        replace(m,raceway_selection_outcomes=(m.raceway_selection_outcomes[0],replace(m.raceway_selection_outcomes[0],id='outcome:duplicate')))


def test_physical_length_once_and_conductor_traversal_unchanged(base,inputs):
    m=selected(base,inputs)
    before=extract_quantities(base);after=extract_quantities(m)
    for category in ('route_length','conductor_length'):
        assert sum(q.quantity for q in before.items if q.category==category)==pytest.approx(
            sum(q.quantity for q in after.items if q.category==category))
    line=next(q for q in after.items if q.category=='route_length')
    assert dict(line.variant)['product_id']=='conduit-a'
    assert any(w.code=='raceway_code_unverified' for w in after.warnings)
    assert validate_lane_model(m)
    assert m.raceway_selections[0].id in {e.id for e in iter_entities(m)}

def test_native_route_axis_readback_retains_identity_and_product_basis(base,inputs):
    import numpy as np
    import ifcopenshell.api.geometry
    import ifcopenshell.util.placement
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    m=selected(base,inputs);ifc=to_ifc(m)
    evse=ifc.by_guid(canonical_id_to_ifc_guid(base.electrical_devices[0].id))
    matrix=np.asarray(ifcopenshell.util.placement.get_local_placement(evse.ObjectPlacement)).copy()
    matrix[2,3]+=.15
    ifcopenshell.api.geometry.edit_object_placement(ifc,product=evse,matrix=matrix,is_si=True,should_transform_children=False)
    segment=ifc.by_guid(canonical_id_to_ifc_guid(f'{m.routes[0].id}#segment:2'))
    axis=next(r for r in segment.Representation.Representations if r.RepresentationIdentifier=='Axis')
    axis.Items[0].Points[-1].Coordinates=(5.2,.2,1.35)
    route_system=ifc.by_guid(canonical_id_to_ifc_guid(m.routes[0].id))
    before=ifcopenshell.util.element.get_psets(route_system)['OABM_Canonical']['CanonicalJson']
    restored=from_ifc(ifc)
    assert restored.routes[0].centerline.points[-1].z==pytest.approx(1.35)
    assert restored.raceway_selections==m.raceway_selections
    assert restored.raceway_selection_outcomes==m.raceway_selection_outcomes
    assert restored.routes[0].nominal_diameter_m==m.routes[0].nominal_diameter_m
    assert ifcopenshell.util.element.get_psets(route_system)['OABM_Canonical']['CanonicalJson']==before

@pytest.mark.parametrize('edit',['native_method','native_diameter','product_property','rule_property'])
def test_native_selected_product_contradictions_refuse(base,inputs,edit):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    m=selected(base,inputs);ifc=to_ifc(m)
    segment=ifc.by_guid(canonical_id_to_ifc_guid(f'{m.routes[0].id}#segment:0'))
    if edit=='native_method':
        segment.PredefinedType='CABLETRAYSEGMENT'
    elif edit=='native_diameter':
        body=next(r for r in segment.Representation.Representations if r.RepresentationIdentifier=='Body')
        body.Items[0].Radius*=2
    else:
        pset=ifc.by_id(ifcopenshell.util.element.get_psets(segment)['OABM_RacewaySelection']['id'])
        ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={
            'ProductId' if edit=='product_property' else 'RuleId':'conflicting-native-edit'})
    with pytest.raises(IfcAdapterError,match='contradicts'):
        from_ifc(ifc)


def test_permutation_multiple_routes_products_and_outcomes(base,inputs):
    i,e,r=inputs
    route=replace(base.routes[0],id='route:second',fitting_ids=())
    base=replace(base,routes=(*base.routes,route))
    p=replace(e.product,product_id='conduit-b')
    b=replace(r.reference,rule_id='explicit-b')
    second=replace(i,intent_id='intent:second',route_id=route.id,product=p,basis=b)
    entry=replace(e,product=p);rule=replace(r,product=p,reference=b)
    m=upgrade_to_v1_2(base)
    a=select_raceways(m,intents=(i,second),catalog=(e,entry),rules=(r,rule))
    b=select_raceways(m,intents=(second,i),catalog=(entry,e),rules=(rule,r))
    assert a.to_json()==b.to_json()
    assert len(a.raceway_selections)==2


def test_router_producer_consumer_proof_and_no_duplicate_geometry(base,inputs,tmp_path):
    from oabm.routing import route_between_ports
    from oabm.exports.gltf import to_glb
    i,e,r=inputs
    route,fittings=route_between_ports(base,base.routes[0].start_port_id,base.routes[0].end_port_id,
                                      'emt',nominal_diameter_m=base.routes[0].nominal_diameter_m)
    base=replace(base,routes=(route,),route_fittings=fittings,
        circuits=tuple(replace(c,route_ids=(route.id,)) for c in base.circuits),
        conductors=tuple(replace(c,route_ids=(route.id,)) for c in base.conductors))
    m=selected(base,(replace(i,route_id=route.id),e,r))
    restored=from_ifc(to_ifc(m))
    assert round_trip_differences(m,restored)==()
    assert restored.routes==base.routes
    to_glb(upgrade_to_v1_2(base),tmp_path/'before.glb')
    to_glb(restored,tmp_path/'after.glb')
    def glb(path):
        data=path.read_bytes();length,kind=struct.unpack_from('<II',data,12)
        return json.loads(data[20:20+length])
    before=glb(tmp_path/'before.glb');after=glb(tmp_path/'after.glb')
    assert len(after['meshes'])==len(before['meshes'])
    route_node=next(n for n in after['nodes'] if n.get('name')==route.id)
    assert route_node['extras']['raceway_selections'][0]['product']['product_id']==e.product.product_id
    assert after['asset']['extras']['raceway_selection_outcomes'][0]['status']=='selected'
    assert any(row.source_id==m.raceway_selections[0].id for s in generate_schedules(restored) for row in s.rows)
    assert validate_lane_model(restored)
    assert sum(q.quantity for q in extract_quantities(restored).items if q.category=='route_length')==pytest.approx(
        sum(q.quantity for q in extract_quantities(base).items if q.category=='route_length'))


def test_cable_selection_is_native_cable_and_separate_nominal_claim(base,inputs):
    i,e,r=inputs
    # The caller supplies a separate cable route; no conduit is silently converted.
    route=replace(base.routes[0],route_type='cable',nominal_diameter_m=None)
    base=replace(base,routes=(route,))
    e=replace(e,route_type='cable',wiring_method='cable',designation_kind='listed_assembly',
        designation='synthetic cable assembly A',nominal_diameter_m=.015)
    r=replace(r,route_type='cable')
    m=selected(base,(i,e,r))
    assert m.routes[0].nominal_diameter_m is None
    assert m.raceway_selections[0].nominal_diameter_m==.015
    ifc=to_ifc(m)
    assert len([s for s in ifc.by_type('IfcCableSegment') if s.Name and 'segment' in s.Name])==3
    assert round_trip_differences(m,from_ifc(ifc))==()


def test_migration_preserves_shipped_box_inventory(base,inputs):
    from oabm.model import Pose,Point3,Size3,upgrade_to_v1_3
    from oabm.electrical import BoxIntent,BoxCatalogEntry,select_electrical_boxes
    product=inputs[1].product
    entry=BoxCatalogEntry(product=product,box_type='synthetic-device-box',size=Size3(x=.3,y=.12,z=.5),
        provenance=inputs[1].provenance)
    intent=BoxIntent(intent_id='intent:box',occupant_ids=(base.electrical_devices[0].id,),
        actor_id='synthetic-reviewer',product=product,host_id='wall:south',mounting='flush',
        pose=Pose(position=Point3(x=5.2,y=-.01,z=1.2)))
    old=select_electrical_boxes(upgrade_to_v1_1(base),intents=(intent,),catalog=(entry,))
    migrated=upgrade_to_v1_2(old)
    assert migrated.electrical_boxes==old.electrical_boxes
    assert migrated.box_selection_outcomes==old.box_selection_outcomes
    assert selected(migrated,inputs).electrical_boxes==old.electrical_boxes
    assert round_trip_differences(migrated,from_ifc(to_ifc(migrated)))==()
    with_raceways=selected(migrated,inputs)
    latest=upgrade_to_v1_3(with_raceways)
    assert latest.electrical_boxes==old.electrical_boxes
    assert latest.box_selection_outcomes==old.box_selection_outcomes
    assert latest.raceway_selections==with_raceways.raceway_selections
    assert latest.raceway_selection_outcomes==with_raceways.raceway_selection_outcomes
    assert round_trip_differences(latest,from_ifc(to_ifc(latest)))==()

@pytest.mark.parametrize('scope',['whole_route','mixed_run','partial_route'])
def test_unresolved_ifc_has_readable_uncertainty_on_annotations_and_native_runs(base,inputs,scope):
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    i,e,r=inputs
    m=selected(base,(replace(i,scope=scope,product=None),e,r));ifc=to_ifc(m)
    outcome=m.raceway_selection_outcomes[0]
    annotation=ifc.by_guid(canonical_id_to_ifc_guid(outcome.id))
    props=ifcopenshell.util.element.get_psets(annotation)['OABM_Canonical']
    assert props['RouteId']==outcome.route_id
    assert props['SelectionStatus']=='unresolved' and props['Scope']==scope
    assert 'missing_catalog_entry' in props['UnresolvedReasons']
    assert props['RequestedRuleId']==r.reference.rule_id
    for native_id in (outcome.route_id,f'{outcome.route_id}#segment:0'):
        native=ifc.by_guid(canonical_id_to_ifc_guid(native_id))
        props=ifcopenshell.util.element.get_psets(native)['OABM_RacewayOutcome']
        assert props['SelectionStatus']=='unresolved' and props['Scope']==scope
        assert 'missing_catalog_entry' in props['UnresolvedReasons']
        assert props['CodeVerificationPerformed'] is False
        assert 'OABM_RacewaySelection' not in ifcopenshell.util.element.get_psets(native)
    assert round_trip_differences(m,from_ifc(ifc))==()

@pytest.mark.parametrize('version,expected',[('1.0.0',{
    'json':'a4f924f0c10c051a7bc13ab05a0674e5a0cd1185f151b49079a248a1b7d6ae53',
    'ifc':'f1ad62fd405527c27b2a482a9d26ac6e74a55c1eea392dbd80e2e263edf9c3cc',
    'glb':'608e6bd8be077a6f71767b8821a05c1257432e1f447fc9ac754a4ed1409ca591',
    'quantities':'7e039fa4d1eed71aa1106f87c2f062073fb47d44029efd840f9097573f0dfaa0',
    'schedules':'d568cbd96f610052bbfef9cdd10550aa8a0967b4325d8a635019f3d1a99772f6'}),
    ('1.1.0',{'json':'68d05c585c9a2f9e5575742061252f3a8ee797f5f556b545d85b368d9a8320f7',
    'ifc':'435b6053a6b52e71df2d7ca53bba16d163778422e16286c8c2c7197438a6c5d0',
    'glb':'8d1507482617d3f8e7bd87e10596b9fafea903f6cc276f8411a1dbb469fb8bc8',
    'quantities':'9628f8173e929e9827f2c3b7841ed1f08d89c0f9794a815a65fafd9a216002d7',
    'schedules':'834dc1b275f2d57da2f2473be82a352cc66c5bd8782d44dae239769a584885f3'})])
def test_shipped_1_0_and_1_1_consumer_bytes_unchanged(base,tmp_path,version,expected):
    """Fingerprints measured from shipped #265 source before the 1.2 edit."""
    import hashlib
    from dataclasses import asdict
    from oabm.model import Pose,Point3,Size3
    from oabm.electrical import BoxIntent,BoxCatalogEntry,select_electrical_boxes
    from oabm.exports.gltf import to_glb
    m=base
    if version=='1.1.0':
        p=CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id='box-a')
        e=BoxCatalogEntry(product=p,box_type='synthetic-box',size=Size3(x=.3,y=.12,z=.5),
            provenance=(Provenance(source_kind='synthetic',source_id='fixture:box',derivation='user'),))
        i=BoxIntent(intent_id='intent:box',occupant_ids=(base.electrical_devices[0].id,),
            actor_id='synthetic-reviewer',product=p,host_id='wall:south',mounting='flush',
            pose=Pose(position=Point3(x=5.2,y=-.01,z=1.2)))
        m=select_electrical_boxes(upgrade_to_v1_1(base),intents=(i,),catalog=(e,))
    path=tmp_path/'model.glb';to_glb(m,path)
    values={'json':m.to_json().encode(),'ifc':to_ifc(m).to_string().encode(),'glb':path.read_bytes(),
        'quantities':json.dumps(asdict(extract_quantities(m)),sort_keys=True).encode(),
        'schedules':json.dumps([asdict(s) for s in generate_schedules(m)],sort_keys=True).encode()}
    assert {k:hashlib.sha256(v).hexdigest() for k,v in values.items()}==expected


def test_convergence_carries_empty_1_2_and_refuses_downstream_selections(base,inputs):
    from oabm.importers.pdf_convergence import converge_pdf_models,PdfConvergenceError
    from oabm.importers.pdf_electrical import ElectricalPdfImporter,PdfElectricalDocument,PdfPageTransform
    case=json.loads((ROOT/'fixtures/pdf_convergence/v1/simple-garage-convergence.json').read_text())
    architecture=BuildingModel.load(ROOT/case['architecture_model'])
    document=PdfElectricalDocument.from_dict(json.loads((ROOT/case['electrical_observations']).read_text()))
    electrical=ElectricalPdfImporter().import_document(document,page_transforms={1:PdfPageTransform(**case['page_transform'])})
    m=converge_pdf_models(architecture,upgrade_to_v1_2(electrical))
    assert m.schema_version=='1.2.0'
    assert not m.raceway_selections and not m.raceway_selection_outcomes
    downstream=replace(selected(base,inputs),attributes={'pdf_architecture':{},'pdf_electrical':{}})
    with pytest.raises(PdfConvergenceError,match='downstream'):
        converge_pdf_models(downstream,electrical)

@pytest.mark.parametrize('value',[False,-1,float('nan'),'0.021'])
def test_catalog_nominal_must_be_positive_finite_number(inputs,value):
    with pytest.raises(ContractError):
        replace(inputs[1],nominal_diameter_m=value)


def test_unknown_route_method_and_conflicting_rule_product_are_unresolved(base,inputs):
    i,e,r=inputs
    route=replace(base.routes[0],route_type='unregistered-method')
    unknown=selected(replace(base,routes=(route,)),(i,replace(e,route_type=route.route_type),
                                                   replace(r,route_type=route.route_type)))
    assert not unknown.raceway_selections
    assert 'product_method_conflict' in unknown.raceway_selection_outcomes[0].reason_codes
    wrong=replace(r,product=replace(r.product,product_id='other-product'))
    conflict=selected(base,(i,e,wrong))
    assert not conflict.raceway_selections
    assert 'caller_rule_conflict' in conflict.raceway_selection_outcomes[0].reason_codes

@pytest.mark.parametrize('kind,field,value',[
    ('selection','ProductId','other-native-product'),
    ('selection','RuleId','other-native-rule'),
    ('selection','Designation','other-native-trade-size'),
    ('outcome','SelectionStatus','selected'),
    ('outcome','Scope','mixed_run'),
    ('outcome','UnresolvedReasons',''),
    ('requested-outcome','RequestedProductId','other-native-request'),
    ('requested-outcome','RequestedRuleId','other-native-rule')])
def test_annotation_only_product_basis_and_uncertainty_edits_refuse(base,inputs,kind,field,value):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    if kind=='outcome':
        m=select_raceways(upgrade_to_v1_2(base),intents=(),catalog=(),rules=())
    else:
        m=selected(base,inputs)
    entity=m.raceway_selections[0] if kind=='selection' else m.raceway_selection_outcomes[0]
    ifc=to_ifc(m)
    annotation=ifc.by_guid(canonical_id_to_ifc_guid(entity.id))
    properties=ifcopenshell.util.element.get_psets(annotation)['OABM_Canonical']
    shadow=properties['CanonicalJson']
    pset=ifc.by_id(properties['id'])
    edits={field:value}
    if field=='SelectionStatus':
        edits['UnresolvedReasons']=''
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties=edits)
    assert ifcopenshell.util.element.get_psets(annotation)['OABM_Canonical']['CanonicalJson']==shadow
    with pytest.raises(IfcAdapterError,match='annotation contradicts'):
        from_ifc(ifc)

@pytest.mark.parametrize('target',['annotation','system','segment'])
@pytest.mark.parametrize('field',['RequestedProductId','RequestedRuleId'])
def test_absent_optional_managed_claims_cannot_be_added_to_unresolved_native_outcome(base,target,field):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    m=select_raceways(upgrade_to_v1_2(base),intents=(),catalog=(),rules=());ifc=to_ifc(m)
    outcome=m.raceway_selection_outcomes[0]
    native_id=outcome.id if target=='annotation' else outcome.route_id if target=='system' else f'{outcome.route_id}#segment:0'
    native=ifc.by_guid(canonical_id_to_ifc_guid(native_id))
    pset_name='OABM_Canonical' if target=='annotation' else 'OABM_RacewayOutcome'
    properties=ifcopenshell.util.element.get_psets(native)[pset_name]
    ifcopenshell.api.pset.edit_pset(ifc,pset=ifc.by_id(properties['id']),properties={field:'unrecorded-native-claim'})
    with pytest.raises(IfcAdapterError,match='contradicts'):
        from_ifc(ifc)

@pytest.mark.parametrize('target',['system','segment'])
@pytest.mark.parametrize('producer,pset_name',[(False,'OABM_RacewaySelection'),
    (False,'OABM_RacewayOutcome'),(True,'OABM_RacewaySelection')])
def test_unrecorded_managed_native_selection_or_outcome_psets_refuse(base,producer,target,pset_name):
    import ifcopenshell.api.pset
    from oabm.ifc import canonical_id_to_ifc_guid
    m=upgrade_to_v1_2(base)
    if producer:
        m=select_raceways(m,intents=(),catalog=(),rules=())
    ifc=to_ifc(m);route_id=m.routes[0].id
    native=ifc.by_guid(canonical_id_to_ifc_guid(route_id if target=='system' else f'{route_id}#segment:0'))
    pset=ifcopenshell.api.pset.add_pset(ifc,product=native,name=pset_name)
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'ProductId':'unrecorded-product',
        'RuleId':'unrecorded-rule','SelectionStatus':'selected'})
    with pytest.raises(IfcAdapterError,match='no typed raceway'):
        from_ifc(ifc)

@pytest.mark.parametrize('target',['annotation','system','segment'])
def test_unrelated_native_custom_notes_do_not_conflict_with_managed_uncertainty(base,target):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid
    m=select_raceways(upgrade_to_v1_2(base),intents=(),catalog=(),rules=());ifc=to_ifc(m)
    outcome=m.raceway_selection_outcomes[0]
    native_id=outcome.id if target=='annotation' else outcome.route_id if target=='system' else f'{outcome.route_id}#segment:0'
    native=ifc.by_guid(canonical_id_to_ifc_guid(native_id))
    pset_name='OABM_Canonical' if target=='annotation' else 'OABM_RacewayOutcome'
    pset=ifc.by_id(ifcopenshell.util.element.get_psets(native)[pset_name]['id'])
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'CustomReviewNote':'synthetic unrelated note'})
    assert round_trip_differences(m,from_ifc(ifc))==()
