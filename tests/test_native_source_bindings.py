"""Independent synthetic identity bridge; never customer fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid
import numpy as np
import pytest

from oabm.model import BuildingModel, Level, ElectricalDevice, Pose, Point3, Provenance
from oabm.ifc import to_ifc, canonical_id_to_ifc_guid
from oabm.native_bonsai import NativeRuntime, export_native_ifc, validate_ifc_request
from oabm.native_bonsai.extensions import source_snapshot, verify_source_snapshot
from oabm.native_bonsai.source_bindings import fingerprint, binding_contract


def fixture(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    guid = ifcopenshell.guid.compress('a4f2b0b0aaba432b88c11324978f1456')
    level_guid = canonical_id_to_ifc_guid('level:ground')
    def evidence(g):
        return (Provenance(source_kind='ifc', source_id='synthetic-source', source_element_id=g,
                           method='explicit-source-interpretation', confidence=.8, derivation='inferred'),)
    model = BuildingModel(model_id='model:bound-source', name='Synthetic native source',
        levels=(Level(id='level:ground', elevation_m=0, provenance=evidence(level_guid)),),
        electrical_devices=(ElectricalDevice(id='device:light', name='Synthetic light', device_type='luminaire',
            level_id='level:ground', pose=Pose(position=Point3(x=0,y=0,z=2)), provenance=evidence(guid)),))
    native = to_ifc(model)
    light = native.by_guid(canonical_id_to_ifc_guid('device:light'))
    light.GlobalId = guid
    boiler = ifcopenshell.api.run('root.create_entity', native, ifc_class='IfcBoiler', name='Synthetic native boiler')
    boiler.GlobalId = ifcopenshell.guid.compress('f783ceee48014652885c3ecbf72c4552')
    body = next(c for c in native.by_type('IfcGeometricRepresentationSubContext') if c.ContextIdentifier=='Body')
    rep = ifcopenshell.api.run('geometry.add_wall_representation', native, context=body, length=.4, thickness=.3, height=.8)
    ifcopenshell.api.run('geometry.assign_representation', native, product=boiler, representation=rep)
    matrix=np.eye(4);matrix[:3,3]=[1,0,0]
    ifcopenshell.api.run('geometry.edit_object_placement', native, product=boiler, matrix=matrix, is_si=True)
    ifcopenshell.api.run('spatial.assign_container', native, products=[boiler], relating_structure=native.by_type('IfcBuildingStorey')[0])
    path=tmp_path/'source.ifc';native.write(str(path));mp=tmp_path/'model.json';model.dump(mp)
    entities={e['id']:e for rows in model.to_dict().values() if isinstance(rows,list) for e in rows if isinstance(e,dict) and 'id' in e}
    request={'version':'native-bonsai-ifc-v2','source_contract':{
        'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'bindings':[{'canonical_id':cid,'source_guid':guid if cid=='device:light' else level_guid,
            'canonical_sha256':fingerprint(e),'qualification':'Original native geometry retained; source interpretation requires review.'} for cid,e in entities.items()],
        'preserve_products':[{'source_guid':boiler.GlobalId,'ifc_class':'IfcBoiler','qualification':'Native mechanical product; not canonical electrical equipment.'}]},
        'acknowledged_body_omissions':['device:light'],'acknowledged_native_body_omissions':[],
        'views':[{'id':'plan','target_view':'PLAN_VIEW','level_id':'level:ground','center_m':[0,0],
            'cut_height_m':1.2,'width_m':5,'height_m':4,'scale_denominator':48,'exclude_ids':[],
            'annotations':[{'source_id':'device:light','kind':'luminaire','text':'L1','label_offset_mm':[3,0]}],
            'native_annotations':[{'source_guid':boiler.GlobalId,'text':'NATIVE BOILER','label_offset_mm':[3,0]}]}]}
    rp=tmp_path/'request.json';rp.write_text(json.dumps(request))
    return model,native,request,mp,path,rp


def test_complete_source_bindings_do_not_rewrite_ids_or_change_legacy_guard(tmp_path):
    model,native,request,*_=fixture(tmp_path)
    before=source_snapshot(native)
    validate_ifc_request(request,model,native)
    verify_source_snapshot(before,native)
    legacy=copy.deepcopy(request);legacy['version']='native-bonsai-ifc-v1';legacy.pop('source_contract')
    with pytest.raises(ValueError):validate_ifc_request(legacy,model,native)


@pytest.mark.parametrize('mutate',[
    lambda r:r['source_contract']['bindings'].pop(),
    lambda r:r['source_contract']['bindings'].append(copy.deepcopy(r['source_contract']['bindings'][0])),
    lambda r:r['source_contract']['bindings'][0].update(canonical_sha256='0'*64),
    lambda r:r['source_contract']['bindings'][0].update(source_guid='bad'),
    lambda r:r['source_contract']['bindings'][0].update(qualification=''),
    lambda r:r['source_contract']['preserve_products'][0].update(ifc_class='IfcElectricAppliance'),
    lambda r:r['source_contract']['preserve_products'].clear(),
    lambda r:r['source_contract'].update(arbitrary='not allowed'),
])
def test_source_contract_refuses_incomplete_or_changed_claims(tmp_path,mutate):
    model,native,request,*_=fixture(tmp_path);mutate(request)
    with pytest.raises(ValueError):validate_ifc_request(request,model,native)


def test_changed_source_hash_fails_before_runtime_or_output(tmp_path):
    model,native,request,mp,source,rp=fixture(tmp_path/'inputs')
    source.write_text(source.read_text()+'\n')
    with pytest.raises(ValueError,match='SHA256'):
        export_native_ifc(mp,source,rp,tmp_path/'attempt',NativeRuntime(Path('/not/a/runtime'),Path('/not/packages')))
    assert not (tmp_path/'attempt').exists()


def test_recomputed_fingerprint_cannot_move_a_bodyless_label_off_source(tmp_path):
    from dataclasses import replace
    model,native,request,*_=fixture(tmp_path)
    light=replace(model.electrical_devices[0],pose=Pose(position=Point3(x=20,y=0,z=2)))
    changed=replace(model,electrical_devices=(light,))
    request['source_contract']['bindings'][1]['canonical_sha256']=fingerprint(changed.to_dict()['electrical_devices'][0])
    with pytest.raises(ValueError,match='anchor differs'):validate_ifc_request(request,changed,native)


def test_spatial_objects_cannot_own_native_plumbing_ports(tmp_path):
    model,native,request,*_=fixture(tmp_path)
    port=ifcopenshell.api.run('root.create_entity',native,ifc_class='IfcDistributionPort')
    port.PredefinedType='PIPE';port.SystemType='DOMESTICCOLDWATER'
    ifcopenshell.api.run('nest.assign_object',native,related_objects=[port],relating_object=native.by_type('IfcBuildingStorey')[0])
    with pytest.raises(ValueError,match='plumbing owner'):validate_ifc_request(request,model,native)


def test_fingerprint_preserves_numeric_equality_without_large_integer_rounding():
    assert fingerprint({'x':2})==fingerprint({'x':2.0})
    assert fingerprint({'x':2**70})!=fingerprint({'x':2**70+1})
    assert fingerprint({'x':True})!=fingerprint({'x':1})


def test_every_bodyless_physical_source_requires_exact_acknowledgment(tmp_path):
    from dataclasses import replace
    from oabm.model import Wall,Polyline3D
    model,native,request,*_=fixture(tmp_path)
    wall=ifcopenshell.api.run('root.create_entity',native,ifc_class='IfcWall')
    ifcopenshell.api.run('geometry.edit_object_placement',native,product=wall,matrix=np.eye(4),is_si=True)
    canonical=Wall(id='wall:bodyless',level_id='level:ground',centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=2,y=0,z=0))),height_m=2,thickness_m=.1,
        provenance=(Provenance(source_kind='ifc',source_id='synthetic',source_element_id=wall.GlobalId,method='explicit source',confidence=.8,derivation='inferred'),))
    model=replace(model,walls=(canonical,));entity=model.to_dict()['walls'][0]
    request['source_contract']['bindings'].append({'canonical_id':canonical.id,'source_guid':wall.GlobalId,'canonical_sha256':fingerprint(entity),'qualification':'Native source wall has no geometry; physical extent unknown.'})
    with pytest.raises(ValueError,match='every bodyless'):validate_ifc_request(request,model,native)
    request['acknowledged_body_omissions'].append(canonical.id)
    validate_ifc_request(request,model,native)


@pytest.mark.parametrize('native_form',[True,False])
def test_cross_identity_exclusions_also_exclude_linked_annotations(tmp_path,native_form):
    model,native,request,*_=fixture(tmp_path);view=request['views'][0]
    guid=next(x['source_guid'] for x in request['source_contract']['bindings'] if x['canonical_id']=='device:light')
    if native_form:
        view['native_exclude_guids']=[guid]
    else:
        view['exclude_ids']=['device:light'];view['annotations']=[]
        view['native_annotations']=[{'source_guid':guid,'text':'L1','label_offset_mm':[3,0]}]
    with pytest.raises(ValueError,match='either identity form'):validate_ifc_request(request,model,native)


def test_native_label_anchor_uses_visible_source_body_not_zero_origin(tmp_path):
    from oabm.native_bonsai.source_bindings import native_label_anchor
    _,native,request,*_=fixture(tmp_path)
    boiler=native.by_guid(request['source_contract']['preserve_products'][0]['source_guid'])
    # Bake the old world placement into the swept solid and reset placement.
    solid=boiler.Representation.Representations[0].Items[0]
    solid.Position.Location.Coordinates=(1.,0.,0.)
    ifcopenshell.api.run('geometry.edit_object_placement',native,product=boiler,matrix=np.eye(4),is_si=True)
    anchor,basis=native_label_anchor(boiler)
    assert basis=='native_body_bounds_center'
    assert anchor.tolist()==pytest.approx([1.2,.15,.4])


@pytest.mark.skipif(not(os.getenv('OABM_TEST_BLENDER') and os.getenv('OABM_TEST_BONSAI_PYTHON')),reason='explicit installed native runtime required')
def test_actual_native_bound_identity_graph_and_product_labels(tmp_path):
    model,native,request,mp,source,rp=fixture(tmp_path/'inputs');before=source_snapshot(native)
    result=export_native_ifc(mp,source,rp,tmp_path/'attempt',NativeRuntime(Path(os.environ['OABM_TEST_BLENDER']),Path(os.environ['OABM_TEST_BONSAI_PYTHON'])))
    receipt=json.loads((result/'receipt.json').read_text())
    assert receipt['canonical_roundtrip']['performed'] is False
    assert receipt['native_source_preservation']['verified'] is True
    saved=ifcopenshell.open(str(result/'model.ifc'));verify_source_snapshot(before,saved,allow_drawing_context_additions=True)
    assert receipt['canonical_identity']['device:light']['ifc_guid']==request['source_contract']['bindings'][1]['source_guid']
    text=(result/receipt['views']['plan']['svg']).read_text();assert 'NATIVE BOILER' in text and 'L1' in text
