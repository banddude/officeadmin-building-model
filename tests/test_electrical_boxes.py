"""Public synthetic physical-box producer and consumer proofs."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.electrical import BoxCatalogEntry, BoxIntent, select_electrical_boxes
from oabm.model import (BuildingModel, CatalogProduct, ContractError, Point3, Pose,
                        Port, Provenance, Size3, Vector3, upgrade_to_v1_1,
                        provenance_applies_to)
from oabm.ifc import to_ifc, from_ifc, round_trip_differences
from oabm.quantities import extract_quantities
from oabm.drawings.generator import generate_schedules
from oabm.exports.gltf import _level_plan_bbox, to_glb

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def base():
    return BuildingModel.load(ROOT / 'fixtures/model/v1/garage-route.json')

@pytest.fixture
def entry():
    return BoxCatalogEntry(product=CatalogProduct(catalog_id='synthetic', catalog_revision='1',
        product_id='two-gang'), box_type='device_box', size=Size3(x=.3,y=.12,z=.5),
        listed_volume_m3=.01, provenance=(Provenance(source_kind='synthetic-catalog',
        source_id='fixture:box', derivation='observed'),))

def intent(base, entry, **changes):
    return replace(BoxIntent(intent_id='box-intent:garage', actor_id='synthetic-reviewer',
        occupant_ids=(base.electrical_devices[0].id,), product=entry.product,
        host_id='wall:south', mounting='flush', pose=Pose(position=Point3(x=5.2,y=-.01,z=1.2))), **changes)

def selected(base, entry, **changes):
    return select_electrical_boxes(upgrade_to_v1_1(base), intents=(intent(base,entry,**changes),), catalog=(entry,))

def test_explicit_migration_and_strict_versions(base, entry):
    old = base.to_dict()
    assert 'electrical_boxes' not in old
    with pytest.raises(ContractError):
        select_electrical_boxes(base, intents=(), catalog=())
    for key in ('electrical_boxes','box_selection_outcomes'):
        with pytest.raises(ContractError):
            BuildingModel.from_dict({**old,key:[]})
    promoted = upgrade_to_v1_1(base)
    assert {k:v for k,v in promoted.to_dict().items() if k not in
            ('schema_version','electrical_boxes','box_selection_outcomes')} == {
                k:v for k,v in old.items() if k != 'schema_version'}
    doc = selected(base,entry).to_dict()
    schema = json.loads((ROOT/'contracts/oabm-model-v1.1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(doc)
    assert BuildingModel.from_dict(doc).to_dict() == doc
    for malformed in ({**doc,'schema_version':'1.2.0'}, {**doc,'boxes':[]}):
        with pytest.raises(ContractError):
            BuildingModel.from_dict(malformed)
    del doc['electrical_boxes']
    with pytest.raises(ContractError):
        BuildingModel.from_dict(doc)

def test_independent_pose_capacity_and_repeatable_upsert(base,entry):
    model = selected(base,entry)
    box = model.electrical_boxes[0]
    assert model.electrical_devices == base.electrical_devices
    assert box.pose != base.electrical_devices[0].pose
    assert box.listed_volume_m3 == .01 != box.size.x*box.size.y*box.size.z
    assert select_electrical_boxes(model,intents=(intent(base,entry),),catalog=(entry,)).to_json() == model.to_json()
    larger = replace(entry,size=Size3(x=.4,y=.2,z=.6))
    updated = select_electrical_boxes(model,intents=(intent(base,larger),),catalog=(larger,))
    assert updated.electrical_boxes[0].id == box.id
    assert updated.box_selection_outcomes[0].id == model.box_selection_outcomes[0].id
    assert len(updated.box_selection_outcomes) == 1
    assert selected(base,replace(entry,listed_volume_m3=None)).electrical_boxes[0].listed_volume_m3 is None

@pytest.mark.parametrize('changes,reason', [({'host_id':None},'missing_host'),
    ({'pose':None},'missing_pose'),({'mounting':None},'missing_mounting'),
    ({'product':None},'missing_catalog_entry')])
def test_unresolved_does_not_invent_physical_geometry(base,entry,changes,reason,tmp_path):
    model=selected(base,entry,**changes)
    assert not model.electrical_boxes
    assert reason in model.box_selection_outcomes[0].reason_codes
    assert any(w.code=='unresolved_box_selection' for w in extract_quantities(model).warnings)
    assert not any(q.category=='electrical_box' for q in extract_quantities(model).items)
    assert round_trip_differences(model,from_ifc(to_ifc(model))) == ()
    assert generate_schedules(model)
    to_glb(model,tmp_path/'unresolved.glb')

def test_scoped_catalog_evidence_never_widens(base,entry):
    scoped=replace(entry,provenance=(replace(entry.provenance[0],scope_paths=('size.x',)),))
    box=selected(base,scoped).electrical_boxes[0]
    user,catalog,host_level=box.provenance
    assert catalog.scope_paths == ('size.x',)
    assert provenance_applies_to(catalog,('size.x',))
    for path in ('size.y','size.z','product','box_type','listed_volume_m3'):
        assert not provenance_applies_to(catalog,(path,))
        assert not provenance_applies_to(user,(path,))
    assert set(selected(base,entry).electrical_boxes[0].provenance[1].scope_paths) == {
        'size','product','box_type','listed_volume_m3'}
    assert host_level.derivation == 'inferred'
    assert host_level.scope_paths == ('level_id',)
    assert not provenance_applies_to(user,('level_id',))
    with pytest.raises(ContractError):
        replace(entry,provenance=(replace(entry.provenance[0],scope_paths=('pose',)),))

def test_resolved_to_unresolved_refuses_live_box_ports(base,entry):
    model=selected(base,entry)
    box=model.electrical_boxes[0]
    port=Port(id='port:box',owner_id=box.id,domain='electrical',role='terminal',
              pose=box.pose,direction=Vector3(x=0,y=1,z=0))
    model=replace(model,ports=(*model.ports,port))
    with pytest.raises(ContractError):
        select_electrical_boxes(model,intents=(intent(base,entry,pose=None),),catalog=(entry,))

def test_box_ifc_readback_and_reference_frame(base,entry,tmp_path):
    model=selected(base,entry)
    assert round_trip_differences(model,from_ifc(to_ifc(model))) == ()
    far=selected(base,entry,pose=Pose(position=Point3(x=20,y=20,z=1)))
    bbox=_level_plan_bbox(far,far.levels[0].id)
    assert bbox[2:] == pytest.approx((20.15,20.06))
    to_glb(far,tmp_path/'a.glb')
    to_glb(far,tmp_path/'b.glb')
    assert (tmp_path/'a.glb').read_bytes() == (tmp_path/'b.glb').read_bytes()

def test_native_placement_and_body_edit_policy(base,entry):
    import numpy as np
    import ifcopenshell.api.geometry
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid, IfcAdapterError
    model=selected(base,entry)
    box=model.electrical_boxes[0]
    port=Port(id='port:physical-box',owner_id=box.id,domain='electrical',role='terminal',
              pose=box.pose,direction=Vector3(x=0,y=1,z=0))
    model=replace(model,ports=(*model.ports,port))
    ifc=to_ifc(model)
    native=ifc.by_guid(canonical_id_to_ifc_guid(box.id))
    assert native.is_a('IfcJunctionBox')
    canonical=ifcopenshell.util.element.get_psets(native)['OABM_Canonical']['CanonicalJson']
    matrix=np.eye(4)
    matrix[:3,3]=(6.2,-.01,1.2)
    ifcopenshell.api.geometry.edit_object_placement(ifc,product=native,matrix=matrix,is_si=True,
                                                  should_transform_children=True)
    assert ifcopenshell.util.element.get_psets(native)['OABM_Canonical']['CanonicalJson'] == canonical
    restored=from_ifc(ifc)
    moved=restored.electrical_boxes[0]
    assert moved.pose.position.x == pytest.approx(6.2)
    assert moved.provenance[-1].derivation == 'user'
    assert moved.provenance[-1].scope_paths == ('pose',)
    assert next(p for p in restored.ports if p.id==port.id).pose.position.x == pytest.approx(6.2)
    assert moved.listed_volume_m3 == .01
    # Editing the native exterior cannot silently retain product sizing claims.
    body=next(r for r in native.Representation.Representations if r.RepresentationIdentifier=='Body')
    body.Items[0].Depth *= 2
    with pytest.raises(IfcAdapterError):
        from_ifc(ifc)

def test_ganged_counts_and_strict_occupant_links(base,entry):
    second=replace(base.electrical_devices[0],id='device:second')
    base=replace(base,electrical_devices=(*base.electrical_devices,second))
    i=intent(base,entry,occupant_ids=tuple(d.id for d in base.electrical_devices))
    model=select_electrical_boxes(upgrade_to_v1_1(base),intents=(i,),catalog=(entry,))
    quantities=extract_quantities(model)
    assert sum(q.quantity for q in quantities.items if q.category=='electrical_box') == 1
    assert sum(q.quantity for q in quantities.items if q.category=='device') == 2
    assert len(model.electrical_boxes[0].occupant_ids)==2
    with pytest.raises(ContractError):
        select_electrical_boxes(upgrade_to_v1_1(base),intents=(i,replace(i,intent_id='intent:other')),catalog=(entry,))
    with pytest.raises(ContractError):
        replace(model,box_selection_outcomes=(replace(model.box_selection_outcomes[0],
            occupant_ids=(second.id,)),))
    with pytest.raises(ContractError):
        replace(model,electrical_boxes=(model.electrical_boxes[0],replace(model.electrical_boxes[0],id='box:duplicate')))

def test_producer_route_ifc_quantities_drawings_and_qa(base,entry):
    from oabm.model import ElectricalEquipment
    from oabm.routing import route_between_ports
    from oabm.drawings import Bounds2, PlanSpec, generate_plan
    from oabm.qa.golden import iter_entities, validate_lane_model
    from oabm.qa.takeoff_comparison import device_scope_counts
    # The source port and box endpoint are explicit, outside wall geometry.
    model=selected(base,entry,pose=Pose(position=Point3(x=5.2,y=-.4,z=1.2)))
    box=model.electrical_boxes[0]
    source=ElectricalEquipment(id='equipment:explicit-source',equipment_type='panelboard',
        pose=Pose(position=Point3(x=2,y=-.4,z=1.2)),level_id=base.levels[0].id)
    ports=(Port(id='port:explicit-source',owner_id=source.id,domain='power',role='source',
        pose=source.pose,direction=Vector3(x=1,y=0,z=0)),
        Port(id='port:box-route',owner_id=box.id,domain='power',role='sink',
        pose=box.pose,direction=Vector3(x=-1,y=0,z=0)))
    model=replace(model,electrical_equipment=(*model.electrical_equipment,source),
                  ports=(*model.ports,*ports))
    route,fittings=route_between_ports(model,ports[0].id,ports[1].id,'emt',nominal_diameter_m=.02)
    assert route.centerline.points[0] == source.pose.position
    assert route.centerline.points[-1] == box.pose.position
    model=replace(model,routes=(*model.routes,route),route_fittings=(*model.route_fittings,*fittings))
    restored=from_ifc(to_ifc(model))
    assert round_trip_differences(model,restored)==()
    quantities=extract_quantities(restored)
    assert sum(q.quantity for q in quantities.items if q.category=='electrical_box')==1
    assert any(q.category=='route_length' for q in quantities.items)
    plan=generate_plan(restored,PlanSpec(id='plan:box',level_id=base.levels[0].id,
        bounds=Bounds2(min_x=0,min_y=-1,max_x=7,max_y=6)))
    assert any(p.layer=='electrical:boxes' and box.id in p.source_ids for p in plan.primitives)
    assert any(box.id==row.source_id for schedule in generate_schedules(restored) for row in schedule.rows)
    assert box.id in {entity.id for entity in iter_entities(restored)}
    assert validate_lane_model(restored)
    assert device_scope_counts(selected(base,entry))==device_scope_counts(base)

def test_missing_intent_and_unknown_catalog_size_are_explicit(base,entry):
    model=select_electrical_boxes(upgrade_to_v1_1(base),intents=(),catalog=(entry,))
    assert not model.electrical_boxes
    assert len(model.box_selection_outcomes)==len(base.electrical_devices)
    assert all(o.reason_codes==('missing_intent',) for o in model.box_selection_outcomes)
    unknown=selected(base,replace(entry,size=None))
    assert not unknown.electrical_boxes
    assert unknown.box_selection_outcomes[0].reason_codes==('missing_catalog_size',)

@pytest.mark.parametrize('changes',[{'host_id':'wall:missing'}, {'occupant_ids':('device:missing',)},
    {'occupant_ids':('device:evse','device:evse')}])
def test_invalid_intent_references_fail_closed(base,entry,changes):
    with pytest.raises(ContractError):
        selected(base,entry,**changes)

@pytest.mark.parametrize('field,value',[('occupant_ids',['device:missing']),
    ('host_id','wall:missing'),('level_id','level:missing')])
def test_portable_box_invalid_references(base,entry,field,value):
    doc=selected(base,entry).to_dict()
    doc['electrical_boxes'][0][field]=value
    with pytest.raises(ContractError,match='references missing id'):
        BuildingModel.from_dict(doc)

def test_box_duplicate_occupant_and_off_envelope_port(base,entry):
    model=selected(base,entry)
    box=model.electrical_boxes[0]
    with pytest.raises(ContractError):
        replace(box,occupant_ids=box.occupant_ids*2)
    with pytest.raises(ContractError):
        replace(model,ports=(*model.ports,Port(id='port:outside',owner_id=box.id,domain='power',role='sink',
          pose=Pose(position=Point3(x=100,y=100,z=100)),direction=Vector3(x=1,y=0,z=0))))
    for change in ({'host_id':base.walls[1].id}, {'level_id':'level:other'}):
        if 'level_id' in change:
            from oabm.model import Level
            changed=replace(base,levels=(*base.levels,Level(id='level:other',elevation_m=4)))
        else:
            changed=base
        changed=replace(changed,electrical_devices=(replace(changed.electrical_devices[0],**change),))
        with pytest.raises(ContractError):
            selected(changed,entry)

def test_permuted_multiple_intents_and_catalog_are_byte_identical(base,entry):
    second=replace(base.electrical_devices[0],id='device:second')
    base=replace(base,electrical_devices=(*base.electrical_devices,second))
    other=replace(entry,product=replace(entry.product,product_id='single-gang'))
    first=intent(base,entry)
    second_intent=replace(first,intent_id='intent:second',occupant_ids=(second.id,),product=other.product)
    model=upgrade_to_v1_1(base)
    a=select_electrical_boxes(model,intents=(first,second_intent),catalog=(entry,other))
    b=select_electrical_boxes(model,intents=(second_intent,first),catalog=(other,entry))
    assert a.to_json()==b.to_json()
    assert len(a.electrical_boxes)==len(a.box_selection_outcomes)==2

@pytest.mark.parametrize('target,version',[('project','1.0.0'),('box','1.0.0'),('project','9.0.0')])
def test_ifc_metadata_versions_fail_closed(base,entry,target,version):
    import ifcopenshell.api.pset
    import ifcopenshell.util.element
    from oabm.ifc import canonical_id_to_ifc_guid, IfcAdapterError
    model=selected(base,entry)
    ifc=to_ifc(model)
    entity=ifc.by_type('IfcProject')[0] if target=='project' else ifc.by_guid(canonical_id_to_ifc_guid(model.electrical_boxes[0].id))
    pset=ifc.by_id(ifcopenshell.util.element.get_psets(entity)['OABM_Canonical']['id'])
    ifcopenshell.api.pset.edit_pset(ifc,pset=pset,properties={'SchemaVersion':version})
    with pytest.raises(IfcAdapterError):
        from_ifc(ifc)

def test_convergence_refuses_physical_inputs_and_preserves_empty_version(base,entry):
    from oabm.importers.pdf_convergence import converge_pdf_models, PdfConvergenceError
    from oabm.importers.pdf_electrical import ElectricalPdfImporter,PdfElectricalDocument,PdfPageTransform
    case=json.loads((ROOT/'fixtures/pdf_convergence/v1/simple-garage-convergence.json').read_text())
    architecture=BuildingModel.load(ROOT/case['architecture_model'])
    document=PdfElectricalDocument.from_dict(json.loads((ROOT/case['electrical_observations']).read_text()))
    electrical=ElectricalPdfImporter().import_document(document,page_transforms={1:PdfPageTransform(**case['page_transform'])})
    promoted=converge_pdf_models(upgrade_to_v1_1(architecture),electrical)
    assert promoted.schema_version=='1.1.0'
    assert not promoted.electrical_boxes and not promoted.box_selection_outcomes
    unresolved=select_electrical_boxes(upgrade_to_v1_1(electrical),intents=(),catalog=())
    with pytest.raises(PdfConvergenceError,match='downstream'):
        converge_pdf_models(architecture,unresolved)


def test_golden_manifest_version_must_match_payload(base,entry,tmp_path):
    from oabm.qa.golden import GoldenCase,GoldenFixtureError,load_golden_model
    path=tmp_path/'model.json'
    path.write_text(selected(base,entry).to_json())
    with pytest.raises(GoldenFixtureError,match='version'):
        load_golden_model(GoldenCase(name='wrong-version',path=path,valid=True))
    assert load_golden_model(GoldenCase(name='version-1.1',path=path,valid=True,schema_version='1.1.0')).electrical_boxes
