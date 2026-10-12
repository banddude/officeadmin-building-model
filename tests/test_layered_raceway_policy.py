"""Public caller-policy data is not a verified electrical-code baseline."""
import json
import struct
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.electrical import (RacewayPolicy, RacewayPolicyRule, compile_raceway_policy,
    RacewayIntent, RacewayCatalogEntry, select_raceways)
from oabm.model import (BuildingModel, CatalogProduct, ContractError, Provenance,
    RuleReference, upgrade_to_v1_2)

ROOT=Path(__file__).resolve().parents[1]
PRODUCTS=tuple(CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id=f'product-{k}') for k in 'abc')


def record(identity,layer,products,*,enabled=True,confidence=.7):
    return RacewayPolicyRule(reference=RuleReference(authority='synthetic-policy',revision='1',rule_id=identity),
        layer=layer,enabled=enabled,explanation=f'Synthetic permission {identity}',actor_id='synthetic-reviewer',
        route_type='emt',permitted_products=tuple(products),provenance=(Provenance(source_kind='synthetic',
            source_id='fixture:layered-policy',source_element_id=identity,derivation='observed',confidence=confidence,
            scope_paths=('permitted_products',),attributes={'source_note':'public synthetic policy fact'}),))


def policy(*rules):
    return RacewayPolicy(policy_id='policy:synthetic',version='1.0.0',catalog_products=PRODUCTS,rules=tuple(rules))


def evaluate(p): return compile_raceway_policy(p,route_types=('emt',)).evaluations[0]


def test_baseline_only_company_and_job_narrowing():
    a,b,c=PRODUCTS
    baseline=record('rule:baseline','baseline',(a,b,c))
    assert evaluate(policy(baseline)).effective_products==(a,b,c)
    company=record('rule:company','company',(a,b))
    job=record('rule:job','job',(a,))
    result=evaluate(policy(baseline,company,job))
    assert result.status=='effective' and result.effective_products==(a,)
    assert len(result.rules)==1 and result.rules[0].product==a
    assert result.contributing_references==(baseline.reference,company.reference,job.reference)
    assert all(p.derivation=='inferred' and p.scope_paths==('basis',) and p.confidence==.7 for p in result.rules[0].provenance)
    assert [p.attributes['source_rule']['explanation'] for p in result.rules[0].provenance]==[
        baseline.explanation,company.explanation,job.explanation]
    for edge in result.rules[0].provenance:
        original=edge.attributes['source_rule']['provenance'][0]
        assert original['derivation']=='observed' and original['scope_paths']==['permitted_products']
        assert edge.attributes['code_verification_performed'] is False


@pytest.mark.parametrize('layer',['company','job'])
def test_attempted_widening_reports_exact_prior_layer_blockers(layer):
    a,b,c=PRODUCTS
    baseline=record('rule:baseline','baseline',(a,b))
    other=record('rule:baseline-other','baseline',(a,c))
    company=record('rule:company','company',(a,))
    invalid=record('rule:invalid',layer,(a,b,c))
    p=policy(baseline,other,invalid) if layer=='company' else policy(baseline,company,invalid)
    result=evaluate(p)
    assert result.status=='unresolved' and not result.rules and not result.effective_products
    diagnostic=result.diagnostics[0]
    assert diagnostic.code=='permission_widening' and diagnostic.references==(invalid.reference,)
    blockers={b.product:b.blocking_references for b in diagnostic.blocked_products}
    assert blockers[c]==((baseline.reference,company.reference) if layer=='job' else (baseline.reference,))
    assert blockers[b]==((company.reference,) if layer=='job' else (other.reference,))
    assert invalid in compile_raceway_policy(p,route_types=('emt',)).source_rules


def test_within_layer_intersection_is_order_independent_and_job_cannot_relax_company():
    a,b,c=PRODUCTS
    baseline=record('rule:baseline','baseline',(a,b,c))
    left=record('rule:left','company',(a,b));right=record('rule:right','company',(b,c))
    first=compile_raceway_policy(policy(baseline,left,right),route_types=('emt',))
    second=compile_raceway_policy(policy(right,baseline,left),route_types=('emt',))
    assert first.to_dict()==second.to_dict() and first.evaluations[0].effective_products==(b,)
    result=evaluate(policy(baseline,left,right,record('rule:job','job',(a,b))))
    assert result.diagnostics[0].blocked_products[0].blocking_references==(right.reference,)


def test_disabled_optional_records_missing_baseline_and_empty_intersection():
    a,b,c=PRODUCTS
    baseline=record('rule:baseline','baseline',(a,b))
    disabled=record('rule:disabled','company',(c,),enabled=False)
    result=compile_raceway_policy(policy(disabled,baseline),route_types=('emt',))
    assert result.evaluations[0].effective_products==(a,b) and disabled in result.source_rules
    assert evaluate(policy(disabled)).diagnostics[0].code=='missing_baseline'
    result=evaluate(policy(baseline,record('rule:left','company',(a,)),record('rule:right','company',(b,))))
    assert result.diagnostics[0].code=='empty_effective_permissions' and not result.rules
    assert evaluate(policy(record('rule:empty','baseline',()))).diagnostics[0].code=='empty_effective_permissions'
    with pytest.raises(ContractError,match='cannot be disabled'): replace(baseline,enabled=False)


def test_portable_schema_determinism_revisions_content_and_source_scopes():
    a,b,c=PRODUCTS
    first=record('rule:baseline','baseline',(b,a),confidence=1)
    second=record('rule:company','company',(a,))
    p=policy(second,first)
    doc=p.to_dict();schema=json.loads((ROOT/'contracts/raceway-policy-v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(doc)
    assert RacewayPolicy.from_json(p.to_json()).to_json()==p.to_json()
    compiled=compile_raceway_policy(p,route_types=('emt',))
    assert compiled.to_dict()==compile_raceway_policy(RacewayPolicy.from_dict(doc),route_types=('emt',)).to_dict()
    permuted=policy(replace(first,permitted_products=(a,b)),second)
    assert compiled.to_dict()==compile_raceway_policy(permuted,route_types=('emt',)).to_dict()
    reference=compiled.rules[0].reference
    for change in (replace(second,reference=replace(second.reference,revision='2')),
                   replace(second,explanation='Changed synthetic rationale'),
                   replace(second,provenance=(replace(second.provenance[0],attributes={'new_context':'public'}),))):
        assert compile_raceway_policy(policy(first,change),route_types=('emt',)).rules[0].reference!=reference
    # A disabled record is audited but cannot influence effective basis/permissions.
    assert compile_raceway_policy(policy(first,second,record('rule:disabled','job',(c,),enabled=False)),route_types=('emt',)).rules==compiled.rules


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(version='9.0.0'),lambda d:d.update(predicate='wet'),
    lambda d:d['rules'][0].update(predicate={'location':'outdoors'}),
    lambda d:d['rules'][0].update(enabled=1),lambda d:d['rules'][0].update(enabled=False),
    lambda d:d['rules'][0].update(layer='code'),lambda d:d['rules'][0].update(route_type='unknown'),
    lambda d:d['rules'][0]['reference'].update(revision=True),
    lambda d:d['rules'][0]['reference'].update(rule_id='bad id'),
    lambda d:d['rules'][0]['permitted_products'].append(d['rules'][0]['permitted_products'][0]),
    lambda d:d['rules'].append(d['rules'][0]),lambda d:d['catalog_products'].clear(),
    lambda d:d['catalog_products'].append(d['catalog_products'][0]),
    lambda d:d['rules'][0]['provenance'][0].update(confidence=True),
    lambda d:d['rules'][0]['provenance'][0].update(confidence=float('nan')),
    lambda d:d['rules'][0]['provenance'][0].update(page=False),
    lambda d:d['rules'][0]['provenance'][0].update(scope_paths=['size.x']),
    lambda d:d['rules'][0]['provenance'][0].update(scope_paths=[]),
    lambda d:d['rules'][0]['provenance'][0].update(source_id=True),
])
def test_strict_portable_malformed_policy_rejected(mutation):
    doc=policy(record('rule:baseline','baseline',PRODUCTS)).to_dict();mutation(doc)
    with pytest.raises(ContractError): RacewayPolicy.from_dict(doc)


def test_constructor_boundaries_duplicate_revision_and_strict_json():
    r=record('rule:baseline','baseline',PRODUCTS)
    for changes in ({'enabled':1},{'permitted_products':list(PRODUCTS)},{'provenance':()},
                    {'actor_id':True},{'route_type':'EMT'}):
        with pytest.raises(ContractError): replace(r,**changes)
    with pytest.raises(ContractError,match='duplicate policy rule identity'):
        policy(r,replace(r,reference=replace(r.reference,revision='2')))
    with pytest.raises(ContractError): compile_raceway_policy(policy(r),route_types=('emt','emt'))
    with pytest.raises(ContractError): compile_raceway_policy(policy(r),route_types=('unknown',))
    with pytest.raises(ContractError): RacewayPolicy.from_json('{"version":"1.0.0","version":"1.0.0"}')
    with pytest.raises(ContractError): RacewayPolicy.from_json('{"bad":NaN}')


@pytest.fixture
def imported_route():
    from oabm.importers.roomplan import load_captured_room
    from oabm.model import (ElectricalEquipment,ElectricalDevice,Point3,Pose,Size3,Port,Vector3,Circuit,Conductor)
    from oabm.routing import route_between_ports
    imported=load_captured_room(ROOT/'fixtures/roomplan/captured-room-3d.json',source_id='fixture:policy-roomplan')
    prov=(Provenance(source_kind='synthetic',source_id='fixture:policy-electrical',derivation='user'),)
    panel=ElectricalEquipment(id='equipment:policy-panel',equipment_type='panelboard',
        pose=Pose(position=Point3(x=-1.6,y=0,z=3.8)),size=Size3(x=.4,y=.2,z=.6),
        level_id=imported.levels[0].id,space_id=imported.spaces[0].id,provenance=prov)
    load=ElectricalDevice(id='device:policy-load',device_type='synthetic-load',
        pose=Pose(position=Point3(x=1.6,y=0,z=3.8)),size=Size3(x=.3,y=.2,z=.5),
        level_id=imported.levels[0].id,space_id=imported.spaces[0].id,provenance=prov)
    ports=(Port(id='port:policy-source',owner_id=panel.id,domain='electrical',role='source',
        pose=Pose(position=Point3(x=-1.4,y=0,z=3.8)),direction=Vector3(x=1,y=0,z=0),provenance=prov),
        Port(id='port:policy-load',owner_id=load.id,domain='electrical',role='sink',
        pose=Pose(position=Point3(x=1.4,y=0,z=3.8)),direction=Vector3(x=-1,y=0,z=0),provenance=prov))
    base=replace(imported,electrical_equipment=(panel,),electrical_devices=(load,),ports=ports)
    route,fittings=route_between_ports(base,ports[0].id,ports[1].id,'emt',nominal_diameter_m=.021)
    circuit=Circuit(id='circuit:policy',source_port_id=ports[0].id,load_port_ids=(ports[1].id,),route_ids=(route.id,),provenance=prov)
    conductor=Conductor(id='conductor:policy',circuit_id=circuit.id,role='phase',count=2,route_ids=(route.id,),provenance=prov)
    base=replace(base,routes=(route,),route_fittings=fittings,circuits=(circuit,),conductors=(conductor,))
    return base


def test_actual_importer_router_compiler_selector_and_all_consumers(imported_route,tmp_path):
    import ifcopenshell.util.element
    from oabm.ifc import to_ifc,from_ifc,round_trip_differences,canonical_id_to_ifc_guid
    from oabm.exports.gltf import to_glb
    from oabm.drawings import generate_schedules,generate_plan,PlanSpec,Bounds2
    from oabm.quantities import extract_quantities
    from oabm.qa import validate_lane_model
    base=imported_route;before=base.to_json()
    a,b,c=PRODUCTS
    p=RacewayPolicy.load(ROOT/'fixtures/policies/v1/layered-raceway-policy.json')
    compilation=compile_raceway_policy(p,route_types=(base.routes[0].route_type,));compiled=compilation.rules[0]
    catalog=RacewayCatalogEntry(product=a,route_type='emt',wiring_method='raceway',designation_kind='trade_size',
        designation='synthetic finite product A',nominal_diameter_m=.021,provenance=(Provenance(
            source_kind='synthetic',source_id='fixture:policy-catalog',derivation='user'),))
    intent=RacewayIntent(intent_id='intent:policy',actor_id='synthetic-reviewer',route_id=base.routes[0].id,
                         product=a,basis=compiled.reference)
    upgraded=upgrade_to_v1_2(base)
    selected=select_raceways(upgraded,intents=(intent,),catalog=(catalog,),rules=compilation.rules)
    assert base.to_json()==before and selected.routes==base.routes and selected.conductors==base.conductors
    assert selected.walls==base.walls and selected.obstacles==base.obstacles and len(selected.routes)==1
    assert selected.raceway_selections[0].basis==compiled.reference
    assert selected.raceway_selections[0].confidence==.4
    assert base.provenance[0].source_kind=='roomplan' and base.routes[0].provenance[0].source_kind=='router'
    assert BuildingModel.from_json(selected.to_json()).to_dict()==selected.to_dict()
    ifc=to_ifc(selected);restored=from_ifc(ifc)
    assert round_trip_differences(selected,restored)==()
    for identity in (base.routes[0].id,f'{base.routes[0].id}#segment:0'):
        native=ifc.by_guid(canonical_id_to_ifc_guid(identity))
        props=ifcopenshell.util.element.get_psets(native)['OABM_RacewaySelection']
        assert props['RuleId']==compiled.reference.rule_id and props['RuleRevision']==compiled.reference.revision
        assert props['CodeVerificationPerformed'] is False
    sources=[p.attributes['source_rule'] for p in restored.raceway_selections[0].provenance if p.source_kind=='layered-raceway-policy']
    assert {s['reference']['rule_id'] for s in sources}=={'rule:baseline','rule:company','rule:job'}
    assert all(s['provenance'][0]['scope_paths']==['permitted_products'] for s in sources)
    paths=[tmp_path/'before.glb',tmp_path/'after.glb']
    for model,path in zip((upgraded,restored),paths): to_glb(model,path)
    def glb(path):
        data=path.read_bytes();length=struct.unpack_from('<I',data,12)[0];return json.loads(data[20:20+length])
    first,last=map(glb,paths)
    assert len(first['meshes'])==len(last['meshes'])
    route_node=next(n for n in last['nodes'] if n.get('name')==base.routes[0].id)
    assert route_node['extras']['raceway_selections'][0]['basis']['rule_id']==compiled.reference.rule_id
    schedules=generate_schedules(restored)
    assert compiled.reference.rule_id in json.dumps([asdict(s) for s in schedules])
    spec=PlanSpec(id='plan:policy',level_id=base.levels[0].id,cut_height_m=.6,
        bounds=Bounds2(min_x=-2.5,min_y=-2,max_x=2.5,max_y=2))
    assert generate_plan(restored,spec).primitives==generate_plan(upgraded,spec).primitives
    original=extract_quantities(upgraded);report=extract_quantities(restored)
    for category in ('route_length','conductor_length'):
        assert sum(q.quantity for q in report.items if q.category==category)==pytest.approx(
            sum(q.quantity for q in original.items if q.category==category))
    route_item=next(q for q in report.items if q.category=='route_length')
    assert dict(route_item.variant)['rule_id']==compiled.reference.rule_id
    assert validate_lane_model(restored)
    # Explicit selection stays idempotent and never becomes automatic.
    assert select_raceways(selected,intents=(intent,),catalog=(catalog,),rules=compilation.rules).to_json()==selected.to_json()
    blocked=compile_raceway_policy(policy(*p.rules,record('rule:widen','job',(c,))),route_types=('emt',))
    assert blocked.evaluations[0].diagnostics[0].code=='permission_widening'
    unresolved=select_raceways(upgraded,intents=(intent,),catalog=(catalog,),rules=blocked.rules)
    assert not unresolved.raceway_selections and 'missing_rule_reference' in unresolved.raceway_selection_outcomes[0].reason_codes
    assert unresolved.routes==base.routes


def test_multiple_types_baselines_permissions_and_evidence_permutations():
    a,b,c=PRODUCTS
    baseline=record('rule:baseline','baseline',(a,b,c))
    second=record('rule:baseline-other','baseline',(a,b))
    company=record('rule:company','company',(a,b))
    job=record('rule:job','job',(b,))
    pvc=replace(record('rule:pvc','baseline',(c,)),route_type='pvc')
    p=policy(baseline,second,company,job,pvc)
    first=compile_raceway_policy(p,route_types=('pvc','emt','cable'))
    reversed_p=replace(p,catalog_products=tuple(reversed(p.catalog_products)),rules=tuple(reversed(p.rules)))
    second_result=compile_raceway_policy(reversed_p,route_types=('cable','emt','pvc'))
    assert first.to_dict()==second_result.to_dict()
    assert [e.route_type for e in first.evaluations]==['cable','emt','pvc']
    assert first.evaluations[0].diagnostics[0].code=='missing_baseline'
    assert [(r.route_type,r.product) for r in first.rules]==[('emt',b),('pvc',c)]
    evidence=replace(baseline.provenance[0],source_id='fixture:another-source',scope_paths=('reference.revision',),confidence=.2)
    with_two=replace(baseline,provenance=(*baseline.provenance,evidence))
    compiled=compile_raceway_policy(policy(with_two),route_types=('emt',))
    reversed_sources=replace(with_two,provenance=tuple(reversed(with_two.provenance)))
    assert compiled.to_dict()==compile_raceway_policy(policy(reversed_sources),route_types=('emt',)).to_dict()
    assert all(r.provenance[0].confidence==.2 for r in compiled.rules)
    scopes={tuple(s['scope_paths']) for s in compiled.rules[0].provenance[0].attributes['source_rule']['provenance']}
    assert scopes=={('permitted_products',),('reference.revision',)}


@pytest.mark.parametrize('number',[True,False,float('inf'),float('-inf'),float('nan'),-1,1.1])
def test_policy_numeric_evidence_rejected_consistently(number):
    r=record('rule:baseline','baseline',PRODUCTS)
    with pytest.raises(ContractError): replace(r.provenance[0],confidence=number)
    doc=policy(r).to_dict();doc['rules'][0]['provenance'][0]['confidence']=number
    with pytest.raises(ContractError): RacewayPolicy.from_dict(doc)


def test_snapshot_source_facts_and_absent_implicit_baseline():
    r=record('rule:baseline','baseline',PRODUCTS)
    p=policy(r);compiled=compile_raceway_policy(p,route_types=('emt',))
    snapshot=compiled.to_dict()
    r.provenance[0].attributes['source_note']='changed caller data after compilation'
    assert compiled.to_dict()==snapshot
    assert not compile_raceway_policy(policy(),route_types=('emt',)).rules
    assert compile_raceway_policy(policy(),route_types=()).evaluations==()
    with pytest.raises(ContractError): RacewayPolicy.from_dict({True:'not a JSON object key'})
    with pytest.raises(ContractError): replace(r,provenance=(replace(r.provenance[0],source_id=True),))
