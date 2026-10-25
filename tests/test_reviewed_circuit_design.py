"""Independent drawn source marks -> admitted placements -> explicit design graph."""
from dataclasses import asdict,replace
import hashlib,io,json
from pathlib import Path
import subprocess,sys
from unittest.mock import patch

import pytest
from pypdf import PdfReader,PdfWriter
from pypdf.generic import DecodedStreamObject,NameObject

from oabm.model import (BuildingModel,ElectricalEquipment,Port,Pose,Point3,Vector3,Provenance,
    RuleReference,Obstacle,Box3D,Size3,ContractError)
from oabm.importers.pdf_electrical.symbol_stage import build_symbol_review,SymbolDecision
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview,PlacementEvidence
from oabm.proposals.contracts import digest
from oabm.routing import RoutingOptions
from oabm.pipeline.reviewed_circuit_design import ReviewedCircuitRequest,ReviewedLoadPort,VERSION
from oabm.pipeline.reviewed_electrical import deliver_reviewed_electrical,ReviewedElectricalDeliveryError
from oabm.pipeline.saved_electrical import persist_saved_electrical,CIRCUIT_VERSION,SavedDeliveryError
from test_reviewed_electrical_pipeline import inputs
import test_symbol_proposals as symbols
import test_takeoff_proposals as takeoff


def drawn_source():
    reader=PdfReader(io.BytesIO(takeoff.source_pdf()))
    writer=PdfWriter();writer.append(reader)
    page=writer.pages[1]
    content=page.get_contents().get_data()+b'\n0 0 0 RG 1 w 106 30 8 8 re S 126 62 8 8 re S\n'
    stream=DecodedStreamObject();stream.set_data(content);page[NameObject('/Contents')]=writer._add_object(stream)
    buffer=io.BytesIO();writer.write(buffer);return buffer.getvalue()


def case(tmp_path,*,height=1.2,blocked=False,select_deferred=False):
    document=drawn_source()
    with patch.object(takeoff,'source_pdf',lambda *a,**kw:document),patch.object(symbols,'source_pdf',lambda *a,**kw:document):
        model,old,args=inputs(tmp_path/'initial',include_ifc=True)
        request,images,documents,output=symbols.fixture()
        second=replace(output.marks[0],bounds=(.55,.65,.65,.75),anchor=(.6,.7),instance_label='drawn-square-b',source_mark='Square centered at source point 110,34.')
        third=replace(output.marks[0],bounds=(.75,.25,.85,.35),anchor=(.8,.3),instance_label='drawn-square-c',source_mark='Square centered at source point 130,66.')
        (tmp_path/'batch').mkdir()
        run=symbols.run(tmp_path/'batch',request=request,images=images,documents=documents,output=replace(output,marks=(*output.marks,second,third)))
    journals={request.fingerprint:tmp_path/'batch/attempts.jsonl'}
    review=build_symbol_review((run,),documents=documents,images=images,journal_paths=journals)
    old_decision=args['decisions'][0];old_facts=args['assertion_review'].assertions
    decisions=[];facts=[];selected=[];deferred=[]
    for row in review.proposals:
        if row['anchor_pt'][0]>120:
            decisions.append(SymbolDecision(manifest_fingerprint=review.fingerprint,proposal_id=row['proposal_id'],
                proposal_fingerprint=digest(row),reviewer_id='synthetic-reviewer',reviewer_derivation='user',
                rationale='Height and physical product association remain unresolved.',action='defer'))
            deferred.append(row['proposal_id']);continue
        selected.append(row['proposal_id'])
        h=height if row['anchor_pt'][0]<100 else 1.2
        decisions.append(replace(old_decision,manifest_fingerprint=review.fingerprint,proposal_id=row['proposal_id'],
            proposal_fingerprint=digest(row),placement=replace(old_decision.placement,anchor_pt=tuple(row['anchor_pt']),height_above_level_m=h)))
        for fact in old_facts:
            fact=replace(fact,assertion_id=row['proposal_id']+':'+fact.field,proposal_id=row['proposal_id'],proposal_fingerprint=digest(row))
            if fact.field=='anchor':fact=replace(fact,value=tuple(row['anchor_pt']),evidence=(replace(fact.evidence[0],bounds_pt=tuple(row['bounds_pt'])),))
            if fact.field=='elevation':fact=replace(fact,value=h)
            facts.append(fact)
    prov=(Provenance(source_kind='synthetic',source_id='fixture:supplied-panel',derivation='observed',confidence=.75),)
    panel=ElectricalEquipment(id='equipment:supplied-panel',equipment_type='panelboard',level_id='floor',
        pose=Pose(position=Point3(x=1.,y=1.,z=4.2)),provenance=prov)
    source=Port(id='port:supplied-source',owner_id=panel.id,domain='electrical',role='source',pose=panel.pose,
        direction=Vector3(x=1.,y=0.,z=0.),provenance=prov)
    obstacles=() if not blocked else (Obstacle(id='obstacle:source-blocked',name='Synthetic blocked source envelope',obstacle_type='hard',
        geometry=Box3D(pose=source.pose,size=Size3(x=2.,y=2.,z=2.))),)
    model=BuildingModel.from_dict(replace(model,electrical_equipment=(panel,),ports=(source,),obstacles=obstacles).to_dict())
    prior=args['assertion_review']
    assertion=PlacementAssertionReview.capture(model,review,decisions=tuple(decisions),registrations=args['registrations'],
        canonical_datum_id=prior.canonical_datum_id,reviewer_id=prior.reviewer_id,reviewer_derivation='user',
        rationale='Explicit synthetic placement assertions.',assertions=tuple(facts))
    payload=b'{"design":"preliminary synthetic circuit grouping and connector offsets","engineering":"not verified"}'
    sha=hashlib.sha256(payload).hexdigest()
    evidence=PlacementEvidence(source_id='fixture:design-decision',document_sha256=sha,kind='project_decision',
        locator='Synthetic project decision record.',provenance=Provenance(source_kind='project-design',source_id='fixture:design-decision',derivation='user',confidence=.8))
    chosen=tuple(sorted(selected+deferred if select_deferred else selected))
    design=ReviewedCircuitRequest.capture(model,review,assertion,request_id='design:explicit-fixture',
        equipment_id=panel.id,source_port_id=source.id,groups=tuple((p,) for p in chosen),
        load_ports=tuple(ReviewedLoadPort(id='port:designed-load-'+str(i),proposal_id=p,
            offset_canonical_m=(0.,0.,0.),direction=(-1.,0.,0.)) for i,p in enumerate(chosen)),
        rules={'max_devices_per_circuit':1,'assumed_voltage_v':48.,'assumed_poles':2,'route_type':'pvc',
            'nominal_diameter_m':.03,'conductor_material':'copper','conductor_size':'10 AWG',
            'insulated_conductor_count':3,'include_ground':True},
        routing_controls=asdict(RoutingOptions(bend_penalty_m=.4,max_bends=8,port_stub_m=0.,clearance_m=.02,
            search_margin_m=.5,corridor_tolerance_m=.03,bundle_alignment_tolerance_m=.02,
            preferred_corridor_discount=0.,surface_path_discount=0.,glazed_wall_penalty=7.,soft_obstacle_penalty_factor=4.,
            vertical_cost_factor=1.,coordinate_precision=9)),
        rule_references=(RuleReference(authority='Synthetic project design',revision='r1',rule_id='fixture.explicit'),),
        evidence=(evidence,),reviewer_id='synthetic-designer',rationale='Explicit preliminary fixture design; capacity and compliance unresolved.',
        inventory_group='designed-graph')
    args.update(runs=(run,),documents=documents,images=images,journal_paths=journals,decisions=tuple(decisions),
        assertion_review=assertion,evidence_documents={**args['evidence_documents'],sha:payload},
        proposal_groups={'source-inventory':tuple(r['proposal_id'] for r in review.proposals)},canonical_groups={'supplied-panel':(panel.id,)})
    args['artifact_references']={**args['artifact_references'],'circuit_design':'synthetic/circuit-design.json'}
    args['source_references'].update({('synthetic','fixture:supplied-panel'):'evidence/panel',
        ('reviewed-circuit-design',design.request_id):'evidence/design',('user-override',model.model_id):'evidence/groups',
        ('router',model.model_id):'evidence/router'})
    return model,review,args,design


def deliver(value,**changes):
    model,review,args,design=value
    return deliver_reviewed_electrical(model,review,**{**args,'circuit_design':design,**changes})


def save_inputs(path,value):
    path.mkdir(parents=True)
    model,review,args,design=value
    def put(name,value):
        data=value if isinstance(value,bytes) else json.dumps(value,sort_keys=True).encode()
        p=path/name;p.write_bytes(data);return {'path':name,'sha256':hashlib.sha256(data).hexdigest()}
    doc={'version':CIRCUIT_VERSION,'delivery_id':args['delivery_id'],'model':put('base.json',model.to_dict()),
        'review':put('review.json',review.to_dict()),'circuit_design':put('design.json',design.to_dict())}
    for key in ('runs','decisions','registrations'):doc[key]=put(key+'.json',[r.to_dict() for r in args[key]])
    for key in ('assertion_review','scope_policy','review_config'):doc[key]=put(key+'.json',args[key].to_dict())
    for key in ('documents','images','evidence_documents'):
        doc[key]={identity:put(key+str(i),data) for i,(identity,data) in enumerate(args[key].items())}
    doc['journals']={identity:put('journal'+str(i),Path(p).read_bytes()) for i,(identity,p) in enumerate(args['journal_paths'].items())}
    doc['source_references']=[{'source_kind':k,'source_id':s,'reference':v} for (k,s),v in args['source_references'].items()]
    for key in ('proposal_groups','canonical_groups','glb_options','include_ifc','coverage_report'):doc[key]=args[key]
    manifest=path/'inputs.json';manifest.write_text(json.dumps(doc));return manifest


def test_explicit_supplied_panel_ports_rules_and_hand_computed_lengths(tmp_path):
    value=case(tmp_path);before=value[0].to_json();result=deliver(value)
    assert value[0].to_json()==before
    assert result.completion['counts']['dispositions']=={'accept':2,'defer':1}
    assert len(result.model.circuits)==2 and len(result.model.routes)==2 and len(result.model.conductors)==4
    assert result.model.electrical_equipment==value[0].electrical_equipment
    assert 'placement' not in result.model.electrical_equipment[0].attributes
    assert next(p for p in result.model.ports if p.id=='port:supplied-source')==value[0].ports[0]
    assert all(c.voltage_v==48. and c.poles==2 and c.load_va is None for c in result.model.circuits)
    assert all(r.route_type=='pvc' and r.nominal_diameter_m==.03 for r in result.model.routes)
    assert all(c.size=='10 AWG' for c in result.model.conductors)
    quantity=json.loads(result.artifacts['quantities'])
    assert sum(r['quantity'] for r in quantity['items'] if r['category']=='route_length')==pytest.approx(30.)
    assert sum(r['quantity'] for r in quantity['items'] if r['category']=='conductor_length')==pytest.approx(120.)
    assert all(r['group']=='designed-graph' for r in quantity['items'] if r['category'] in ('route_length','conductor_length','fitting'))
    assert all(r['geometry_status']=='inferred' and r['design_status']=='user-directed' for r in quantity['items'] if r['category']=='route_length')
    assert not result.completion['circuit_design']['buildable'] and not any(result.completion['claims'].values())
    assert result.completion['source_coverage']['status']=='unavailable'
    assert all(p.direction==Vector3(x=-1.,y=0.,z=0.) for p in result.model.ports if p.role=='load')


def test_actual_saved_entrypoint_repeat_ifc_and_reviewed_recomputation(tmp_path):
    value=case(tmp_path/'source');manifest=save_inputs(tmp_path/'saved',value)
    first=subprocess.run([sys.executable,'-m','oabm.pipeline.saved_electrical',str(manifest),
        '--output-root',str(tmp_path/'out'),'--run-id','a'],capture_output=True,text=True)
    assert first.returncode==0,first.stderr
    a=Path(first.stdout.strip());b=persist_saved_electrical(manifest,output_root=tmp_path/'out',run_id='b')
    assert (a/'model.json').read_bytes()==(b/'model.json').read_bytes()
    assert (a/'quantities.json').read_bytes()==(b/'quantities.json').read_bytes()
    from oabm.ifc.adapter import from_ifc
    restored=from_ifc(a/'model.ifc');original=BuildingModel.from_json((a/'model.json').read_text())
    assert {r.id for r in restored.routes}=={r.id for r in original.routes}
    assert all(c.attributes['design']['source_observed'] is False for c in restored.circuits)
    changed=case(tmp_path/'changed-source',height=2.2)
    moved=persist_saved_electrical(save_inputs(tmp_path/'changed-inputs',changed),output_root=tmp_path/'out',run_id='changed')
    new=BuildingModel.from_json((moved/'model.json').read_text())
    assert {d.id for d in new.electrical_devices}=={d.id for d in original.electrical_devices}
    assert {r.id for r in new.routes}=={r.id for r in original.routes}
    quantities=json.loads((moved/'quantities.json').read_bytes())
    assert sum(r['quantity'] for r in quantities['items'] if r['category']=='route_length')==pytest.approx(31.)
    assert (moved/'model.json').read_bytes()!=(a/'model.json').read_bytes()
    for final in (a,b,moved):
        marker=json.loads((final/'COMPLETED.json').read_bytes())
        for row in marker['files']:
            data=(final/row['path']).read_bytes();assert len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256']


@pytest.mark.parametrize('change,reason',[
    ({'rules':None},'missing_circuit_rules'),({'routing_controls':None},'missing_routing_controls'),
    ({'source_port_id':None},'source_port_unavailable'),({'equipment_id':None},'source_equipment_unavailable'),
    ({'groups':None},'missing_circuit_groups'),({'load_ports':()},'missing_load_port:'),
    ({'evidence':()},'missing_design_decision_evidence'),({'rule_references':()},'missing_design_rule_references')])
def test_missing_inputs_defer_without_partial_graph(tmp_path,change,reason):
    value=case(tmp_path);result=deliver(value,circuit_design=replace(value[3],**change))
    assert not result.model.routes and not result.model.circuits and not result.model.conductors
    assert result.model.ports==value[0].ports
    assert any(r.startswith(reason) for r in result.completion['circuit_design']['reasons'])
    assert result.completion['delivery_ready']


def test_deferred_selected_proposal_and_failed_route_are_atomic(tmp_path):
    for name,params in [('selected',{'select_deferred':True}),('blocked',{'blocked':True})]:
        value=case(tmp_path/name,**params);result=deliver(value)
        assert result.model.ports==value[0].ports and not result.model.circuits and not result.model.routes
        assert not result.completion['circuit_design']['created_ids']
        assert result.completion['circuit_design']['status']=='deferred'


@pytest.mark.parametrize('field,key,bad',[
    ('rules','assumed_voltage_v',float('nan')),
    ('rules','assumed_poles',True),
    ('rules','include_ground',1),
    ('rules','conductor_material',''),
    ('rules','nominal_diameter_m',-1.),
    ('routing_controls','clearance_m',float('inf')),
    ('routing_controls','coordinate_precision',True),
    ('routing_controls','preferred_corridor_discount',1.),
])
def test_invalid_explicit_profiles_fail_closed(tmp_path,field,key,bad):
    value=case(tmp_path);profile={**getattr(value[3],field),key:bad}
    with pytest.raises((ReviewedElectricalDeliveryError,ContractError)):
        deliver(value,circuit_design=replace(value[3],**{field:profile}))
    assert not value[0].circuits and len(value[0].ports)==1


def test_partial_profile_defers_instead_of_using_engine_default(tmp_path):
    value=case(tmp_path);profile=dict(value[3].rules);del profile['assumed_voltage_v']
    result=deliver(value,circuit_design=replace(value[3],rules=profile))
    assert result.completion['circuit_design']['reasons']==['missing_circuit_rules_field:assumed_voltage_v']
    assert result.model.ports==value[0].ports and not result.model.circuits


@pytest.mark.parametrize('fault',['stale','evidence','unknown-proposal','port-collision'])
def test_broken_review_bindings_abort_before_output(tmp_path,fault):
    value=case(tmp_path);request=value[3];changes={}
    if fault=='stale':request=replace(request,assertion_review_fingerprint='0'*64)
    elif fault=='evidence':
        evidence=request.evidence[0]
        changes['evidence_documents']={**value[2]['evidence_documents'],evidence.document_sha256:b'tampered'}
    elif fault=='unknown-proposal':request=replace(request,groups=(('unknown',),),load_ports=())
    else:request=replace(request,load_ports=(replace(request.load_ports[0],id='port:supplied-source'),*request.load_ports[1:]))
    with pytest.raises(ReviewedElectricalDeliveryError):deliver(value,circuit_design=request,**changes)
    assert not value[0].circuits and len(value[0].ports)==1


def test_disabled_stage_preserves_existing_artifacts(tmp_path):
    value=case(tmp_path);model,review,args,_=value
    args={**args,'artifact_references':{k:v for k,v in args['artifact_references'].items() if k!='circuit_design'}}
    omitted=deliver_reviewed_electrical(model,review,**args)
    disabled=deliver_reviewed_electrical(model,review,**args,circuit_design=None)
    assert omitted.artifacts==disabled.artifacts
    assert omitted.completion==disabled.completion
    assert 'circuit_design' not in disabled.completion['stages']


def test_saved_invalid_binding_publishes_no_delivery(tmp_path):
    value=case(tmp_path);value=(*value[:3],replace(value[3],model_fingerprint='0'*64))
    manifest=save_inputs(tmp_path/'saved-invalid',value)
    with pytest.raises(SavedDeliveryError) as rejected:
        persist_saved_electrical(manifest,output_root=tmp_path/'out',run_id='invalid')
    assert isinstance(rejected.value.__cause__,ReviewedElectricalDeliveryError)
    assert rejected.value.__cause__.stage=='circuit_design'
    assert 'stale reviewed circuit request binding' in str(rejected.value.__cause__)
    assert not list((tmp_path/'out').rglob('COMPLETED.json'))
    assert not list((tmp_path/'out').rglob('model.json'))


def test_designed_connector_offset_does_not_move_observed_device(tmp_path):
    value=case(tmp_path)
    request=replace(value[3],load_ports=tuple(replace(p,offset_canonical_m=(0.,0.,.5)) for p in value[3].load_ports))
    result=deliver(value,circuit_design=request)
    devices={d.id:d for d in result.model.electrical_devices}
    for port in result.model.ports:
        if port.role=='load':
            assert port.pose.position.z==pytest.approx(devices[port.owner_id].pose.position.z+.5)
            assert port.provenance[-1].attributes['role']=='designed'
    assert all(d.pose.position.z==pytest.approx(4.2) for d in devices.values())
    quantities=json.loads(result.artifacts['quantities'])
    assert sum(r['quantity'] for r in quantities['items'] if r['category']=='route_length')==pytest.approx(31.)


@pytest.mark.parametrize('scope',[('pose.position',),('equipment_type',),('level_id',)])
def test_panel_field_evidence_is_owner_relative_in_saved_design(tmp_path,scope):
    model,review,args,request=case(tmp_path/'source')
    panel=replace(model.electrical_equipment[0],provenance=tuple(replace(p,scope_paths=scope)
        for p in model.electrical_equipment[0].provenance))
    model=BuildingModel.from_dict(replace(model,electrical_equipment=(panel,)).to_dict())
    prior=args['assertion_review']
    assertion=PlacementAssertionReview.capture(model,review,decisions=args['decisions'],registrations=args['registrations'],
        canonical_datum_id=prior.canonical_datum_id,reviewer_id=prior.reviewer_id,
        reviewer_derivation=prior.reviewer_derivation,rationale=prior.rationale,assertions=prior.assertions)
    request=replace(request,model_fingerprint=digest(model.to_dict()),assertion_review_fingerprint=assertion.fingerprint)
    value=(model,review,{**args,'assertion_review':assertion},request)
    final=persist_saved_electrical(save_inputs(tmp_path/'saved',value),output_root=tmp_path/'out',run_id='qualified-panel')
    result=BuildingModel.from_json((final/'model.json').read_text())
    assert result.electrical_equipment==(panel,)
    source_record=model.to_dict()['electrical_equipment'][0]['provenance'][0]
    for entity in (*result.routes,*result.route_fittings):
        references=[p for p in entity.provenance if p.method=='reviewed-circuit-owner-dependency']
        assert len(references)==1
        ref=references[0]
        assert ref.derivation=='inferred' and ref.scope_paths is None
        assert ref.confidence==panel.provenance[0].confidence
        assert ref.attributes['source_owner']=={'model_id':model.model_id,'collection':'electrical_equipment','canonical_id':panel.id}
        assert ref.attributes['source_record']==source_record
        assert not ref.attributes['source_observed']
    from oabm.ifc.adapter import from_ifc
    restored=from_ifc(final/'model.ifc')
    assert restored.electrical_equipment[0].provenance==panel.provenance
    assert {e.id:e.provenance for e in restored.routes}=={e.id:e.provenance for e in result.routes}


@pytest.mark.parametrize('field,key,missing,bad',[
    ('rules','assumed_poles','assumed_voltage_v',True),
    ('rules','include_ground','conductor_size',1),
    ('routing_controls','max_bends','clearance_m',True),
    ('rules','assumed_poles','assumed_voltage_v',0),
    ('routing_controls','max_bends','clearance_m',-1),
    ('routing_controls','coordinate_precision','clearance_m',13),
])
def test_supplied_partial_profile_values_are_validated_before_missing_fields(tmp_path,field,key,missing,bad):
    value=case(tmp_path);profile=dict(getattr(value[3],field));del profile[missing];profile[key]=bad
    with pytest.raises(ContractError):deliver(value,circuit_design=replace(value[3],**{field:profile}))
