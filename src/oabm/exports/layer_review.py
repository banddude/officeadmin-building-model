"""Assembly-aware existing review transport plus the existing selection sidecar.

No new canonical or review-file schema. Current OA assembly/control admission
is separate. Re-reading verifies both files against the same exact source/GLB.
"""
from dataclasses import dataclass
import base64
import json
from functools import wraps

from oabm.model import BuildingModel, LayerSelectionConfig
from oabm.qa.confidence_review import ReviewConfig, ConfidenceReviewManifest, build_confidence_review
from .layers import LayerGlbDelivery
from .layer_memberships import verify_layer_delivery
from .review_bundle import (ReviewBundle,build_review_bundle,_strict_json_loads,
    _canonical_json_bytes)

MAX_REVIEW_BYTES=20*1024*1024
KEYS=('canonical_model','glb','confidence_review','source_reference_mapping')


def _resource_boundary(function):
    @wraps(function)
    def admitted(*args,**kwargs):
        from oabm.routing.physical_verify_types import VerificationStop
        try:
            return function(*args,**kwargs)
        except VerificationStop as exc:
            raise ValueError(f'layer review {exc.status}: {exc.reason}') from exc
        except RecursionError as exc:
            raise ValueError('layer review recursion refused') from exc
    return admitted


def _admit_json(payload,label,*,byte_limit=None):
    """Bound UTF-8, nesting and token/node count before the JSON parser."""
    from oabm.routing.physical_capture import bounded_source_json
    text=bounded_source_json(payload)
    if type(text) is not str:raise ValueError(f'{label} must be JSON text')
    if byte_limit is not None:
        size=0
        for offset in range(0,len(text),4096):
            size+=len(text[offset:offset+4096].encode('utf-8'))
            if size>byte_limit:raise ValueError(f'{label} exceeds byte limit')
    return _strict_json_loads(text,label)


@dataclass(frozen=True,slots=True)
class LayerReviewDelivery:
    review_file_json: str
    selection_manifest_json: str


@_resource_boundary
def build_layer_review(delivery, *, review_config, source_references, evidence=None,
    model_reference='model.json',glb_reference='model.glb',confidence_reference='confidence.json',
    source_reference_mapping_reference='source-map.json'):
    if type(delivery) is not LayerGlbDelivery or type(review_config) is not ReviewConfig:
        raise ValueError('layer review requires a source-bound GLB delivery and explicit ReviewConfig')
    model=BuildingModel.from_dict(_admit_json(delivery.source_model_json,'canonical model'))
    verify_layer_delivery(model,delivery)
    request=delivery.to_dict()['request']
    confidence=build_confidence_review(model,review_config,physical_routes=request['physical_routes'],building_assemblies=True,evidence=evidence)
    bundle=build_review_bundle(model,glb_bytes=delivery.glb_bytes,model_reference=model_reference,
        glb_reference=glb_reference,confidence_manifest=confidence,confidence_reference=confidence_reference,
        source_reference_mapping_reference=source_reference_mapping_reference,source_references=source_references,
        glb_options=delivery.to_dict()['export_options'],building_assemblies=True)
    source_rows=bundle.to_dict()['artifacts']['source_reference_mapping']['items']
    artifacts=dict(zip(KEYS,(delivery.source_model_json.encode(),delivery.glb_bytes,
        confidence.to_json(indent=None).encode(),_canonical_json_bytes(source_rows)),strict=True))
    transport={'kind':'officeadmin.bim-review-file','version':'1.0.0','bundle':bundle.to_dict(),
        'artifacts':{key:base64.b64encode(value).decode('ascii') for key,value in artifacts.items()}}
    text=json.dumps(transport,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    _admit_json(text,'layer review file',byte_limit=MAX_REVIEW_BYTES)
    return LayerReviewDelivery(text,delivery.manifest_json)


@_resource_boundary
def read_layer_review(review_file_json,selection_manifest_json):
    """Reject stale, tampered, mismatched or partial existing review artifacts."""
    if type(review_file_json) is not str:raise ValueError('layer review file must be JSON text')
    transport=_admit_json(review_file_json,'layer review file',byte_limit=MAX_REVIEW_BYTES)
    if not isinstance(transport,dict) or set(transport)!={'kind','version','bundle','artifacts'}:
        raise ValueError('invalid existing review-file wrapper')
    if transport['kind']!='officeadmin.bim-review-file' or transport['version']!='1.0.0':
        raise ValueError('unsupported review-file wrapper')
    if not isinstance(transport['artifacts'],dict) or set(transport['artifacts'])!=set(KEYS):
        raise ValueError('review file requires exactly four existing artifacts')
    try:
        artifacts={key:base64.b64decode(value,validate=True) for key,value in transport['artifacts'].items()}
    except (TypeError,ValueError) as exc:raise ValueError('invalid review artifact encoding') from exc
    bundle=ReviewBundle.from_dict(transport['bundle'])
    import hashlib
    for key,data in artifacts.items():
        if hashlib.sha256(data).hexdigest()!=transport['bundle']['artifacts'][key]['sha256']:
            raise ValueError(f'{key} review artifact hash mismatch')
    model=BuildingModel.from_dict(_admit_json(artifacts['canonical_model'],'canonical model'))
    selection=_admit_json(selection_manifest_json,'layer selection sidecar')
    config=LayerSelectionConfig.from_dict(selection['configuration'])
    delivery=LayerGlbDelivery(source_model_json=artifacts['canonical_model'].decode(),config_json=config.to_json(indent=None),
        manifest_json=json.dumps(selection,sort_keys=True,indent=2,allow_nan=False),glb_bytes=artifacts['glb'])
    verify_layer_delivery(model,delivery)
    confidence=ConfidenceReviewManifest.from_dict(_admit_json(artifacts['confidence_review'],'confidence'))
    source_rows=_admit_json(artifacts['source_reference_mapping'],'source mapping')
    if source_rows!=bundle.to_dict()['artifacts']['source_reference_mapping']['items']:
        raise ValueError('review source mapping disagrees with bundle')
    source_refs={(row['source_kind'],row['source_id']):row['reference'] for row in source_rows}
    args=bundle.to_dict()['artifacts']
    # Evidence bindings must also reproduce; do not silently drop crop evidence.
    verified=build_review_bundle(model,glb_bytes=artifacts['glb'],model_reference=args['canonical_model']['reference'],
        glb_reference=args['glb']['reference'],confidence_manifest=confidence,
        confidence_reference=args['confidence_review']['reference'],
        source_reference_mapping_reference=args['source_reference_mapping']['reference'],source_references=source_refs,
        glb_options=selection['export_options'],building_assemblies=True)
    if verified.to_dict()!=bundle.to_dict():raise ValueError('review bundle does not match retained canonical artifacts')
    from oabm.qa.confidence_review import EvidenceReference
    crops={crop['ref_id']:crop for item in confidence.items for crop in item.crops}
    expected=build_layer_review(delivery,review_config=ReviewConfig(confidence.confidence_threshold),source_references=source_refs,
        evidence=tuple(EvidenceReference(**crops[key]) for key in sorted(crops)),
        model_reference=args['canonical_model']['reference'],glb_reference=args['glb']['reference'],
        confidence_reference=args['confidence_review']['reference'],
        source_reference_mapping_reference=args['source_reference_mapping']['reference'])
    if _admit_json(expected.review_file_json,'expected review',byte_limit=MAX_REVIEW_BYTES)!=transport:
        raise ValueError('review transport differs from trusted source-bound regeneration')
    return model,delivery,bundle
