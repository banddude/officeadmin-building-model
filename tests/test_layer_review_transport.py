"""Existing four-artifact transport with assembly-aware validation and saved sidecar."""
from dataclasses import replace
from pathlib import Path
import base64,json,sys
import pytest
from oabm.model import BuildingModel,LayerDefinition,LayerSelectionConfig,SavedPreset
from oabm.exports import build_layer_glb
from oabm.exports.layer_review import build_layer_review,read_layer_review
from oabm.exports.layer_memberships import verify_layer_delivery
from oabm.exports.review_bundle import ReviewBundle
from oabm.qa.confidence_review import build_confidence_review,ReviewConfig,_entity_collections,EvidenceReference,fingerprint_provenance
from oabm.quantities import extract_quantities
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
from assembly_layer_selections import synthetic_model,saved_config


def source_refs(model,confidence):
    index={entity.id:entity for rows in _entity_collections(model).values() for entity in rows}
    return {(record.source_kind,record.source_id):'synthetic:'+record.source_kind+':'+record.source_id
        for item in confidence.items for identity in item.entity_ids for record in index[identity].provenance}


@pytest.fixture(scope='module')
def transport():
    model=synthetic_model();config=saved_config(model)
    delivery=build_layer_glb(model,config,preset='preset:review',unassigned='error',level_ids=('level:proof',))
    snapshot=BuildingModel.from_json(delivery.source_model_json)
    confidence=build_confidence_review(snapshot,ReviewConfig(.9),building_assemblies=True)
    refs=source_refs(snapshot,confidence)
    packet=build_layer_review(delivery,review_config=ReviewConfig(.9),source_references=refs)
    return snapshot,delivery,confidence,refs,packet


def test_assembly_transport_preserves_existing_contracts_all_canonical_identities_and_quantities(transport):
    model,delivery,confidence,refs,packet=transport
    parsed,retained,bundle=read_layer_review(packet.review_file_json,packet.selection_manifest_json)
    assert parsed.to_dict()==model.to_dict()
    assert extract_quantities(parsed).to_json()==extract_quantities(model).to_json()
    assert retained==delivery
    assert ReviewBundle.from_dict(bundle.to_dict()).to_dict()==bundle.to_dict()
    file=json.loads(packet.review_file_json)
    assert set(file)=={'kind','version','bundle','artifacts'}
    assert file['kind']=='officeadmin.bim-review-file' and file['version']=='1.0.0'
    assert parsed.schema_version=='1.6.0'
    assert base64.b64decode(file['artifacts']['glb'])==delivery.glb_bytes
    assert base64.b64decode(file['artifacts']['confidence_review']).decode()==confidence.to_json(indent=None)
    assert {'building_assemblies','assembly_components'}<=set(confidence.collections)
    assert all(entity.id in {e.id for rows in _entity_collections(parsed).values() for e in rows}
        for entity in parsed.assembly_components)
    assert retained.to_dict()['unknown_level_hidden_ids']==['device:unknown']
    assert build_layer_review(delivery,review_config=ReviewConfig(.9),source_references=refs)==packet


def test_transport_rejects_altered_bytes_stale_selection_unknown_levels_and_partial_source(transport):
    model,delivery,_,_,packet=transport
    file=json.loads(packet.review_file_json)
    glb=bytearray(base64.b64decode(file['artifacts']['glb']));glb[-1]^=1
    file['artifacts']['glb']=base64.b64encode(glb).decode()
    with pytest.raises(ValueError,match='hash mismatch'):
        read_layer_review(json.dumps(file),packet.selection_manifest_json)
    selection=json.loads(packet.selection_manifest_json);selection['request']['level_ids']=['level:missing']
    with pytest.raises(ValueError):read_layer_review(packet.review_file_json,json.dumps(selection))
    selection=json.loads(packet.selection_manifest_json);selection['configuration']['layers'][0]['member_ids']=['missing:member']
    with pytest.raises(ValueError):read_layer_review(packet.review_file_json,json.dumps(selection))
    file=json.loads(packet.review_file_json);del file['artifacts']['source_reference_mapping']
    with pytest.raises(ValueError,match='four existing artifacts'):read_layer_review(json.dumps(file),packet.selection_manifest_json)
    with pytest.raises(ValueError,match='stale|mismatch'):
        verify_layer_delivery(replace(model,name='Stale source'),delivery)


def test_assembly_confidence_crop_evidence_survives_the_existing_transport(transport):
    model,delivery,_,_,_=transport
    component=next(c for c in model.assembly_components if c.provenance)
    record=component.provenance[0]
    evidence=(EvidenceReference(ref_id='crop:assembly',entity_id=component.id,source_kind=record.source_kind,
        source_id=record.source_id,page=record.page,fingerprint=fingerprint_provenance(record,entity_id=component.id)),)
    # A high threshold ensures the referenced component has a confidence item.
    confidence=build_confidence_review(model,ReviewConfig(1.),building_assemblies=True,evidence=evidence)
    packet=build_layer_review(delivery,review_config=ReviewConfig(1.),source_references=source_refs(model,confidence),evidence=evidence)
    _,_,bundle=read_layer_review(packet.review_file_json,packet.selection_manifest_json)
    assert any(item['review']['crops'] for item in bundle.to_dict()['items'])


@pytest.mark.parametrize('payload',[ '['*2000+'0'+']'*2000, '['+','.join('0' for _ in range(250001))+']' ])
def test_wrapper_depth_and_nodes_are_admitted_before_json_parse(monkeypatch,payload):
    import oabm.exports.layer_review as reader
    def forbidden(*args,**kwargs):pytest.fail('unadmitted wrapper reached JSON parser')
    monkeypatch.setattr(reader,'_strict_json_loads',forbidden)
    with pytest.raises(ValueError,match='resource_limit'):
        reader.read_layer_review(payload,'{}')


@pytest.mark.parametrize('key,label',[('confidence_review','confidence'),('source_reference_mapping','source mapping')])
@pytest.mark.parametrize('raw',[ ('['*2000+'0'+']'*2000).encode(), ('['+','.join('0' for _ in range(250001))+']').encode() ])
def test_artifact_json_depth_and_nodes_refuse_before_parse(transport,monkeypatch,key,label,raw):
    import hashlib
    import oabm.exports.layer_review as reader
    _,_,_,_,packet=transport
    file=json.loads(packet.review_file_json)
    file['artifacts'][key]=base64.b64encode(raw).decode()
    file['bundle']['artifacts'][key]['sha256']=hashlib.sha256(raw).hexdigest()
    # Source-map hashes are checked by the bundle schema against its inline
    # rows first, so verify pre-parser admission directly at this exact call.
    original=reader._strict_json_loads
    def checked(payload,actual_label):
        if actual_label==label:pytest.fail('unadmitted artifact reached JSON parser')
        return original(payload,actual_label)
    monkeypatch.setattr(reader,'_strict_json_loads',checked)
    if key=='source_reference_mapping':
        from oabm.routing.physical_verify_types import VerificationStop
        with pytest.raises(VerificationStop,match='source_json_(depth|node)_limit'):
            reader._admit_json(raw,label)
    else:
        with pytest.raises(ValueError,match='resource_limit'):
            reader.read_layer_review(json.dumps(file),packet.selection_manifest_json)


def test_recursion_and_invalid_utf8_are_normalized_refusals(monkeypatch):
    import oabm.exports.layer_review as reader
    def recursion(*args,**kwargs):raise RecursionError('synthetic parser limit')
    monkeypatch.setattr(reader,'_strict_json_loads',recursion)
    with pytest.raises(ValueError,match='recursion refused'):reader.read_layer_review('{}','{}')
    with pytest.raises(ValueError,match='source_json_encoding'):reader.read_layer_review('\ud800','{}')
