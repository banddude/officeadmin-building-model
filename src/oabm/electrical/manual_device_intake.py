"""Admit explicitly positioned, individually supplied devices, never infer them."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
import math
import re
from importlib.resources import files
from jsonschema import Draft202012Validator, ValidationError
from oabm.electrical.sizing_provider import _portable_json
from oabm.model import BuildingModel, ContractError, ElectricalDevice, Port, Pose, Point3, Quaternion, Vector3, Provenance
from oabm.model.model import validate_model, _entity_collections
from oabm.model.common import _validate_id
from oabm.model import PhysicalRoute
from oabm.reports.source_coverage import model_fingerprint
from oabm.routing.physical_capture import capture, SOURCE_BYTES, CANDIDATE_BYTES
from oabm.routing.physical_verify_types import VerificationStop

_CONTRACTS=files('oabm.electrical._manual_intake_contracts')
_REQUEST_SCHEMA=Draft202012Validator(json.loads(_CONTRACTS.joinpath('manual-device-request-v1.schema.json').read_text()))
_REPORT_SCHEMA=Draft202012Validator(json.loads(_CONTRACTS.joinpath('manual-device-report-v1.schema.json').read_text()))

_KEYS={'source_item_id','quantity','device_id','device_type','level_id','device_pose','port_id','port_pose','port_direction','confidence'}
_REQUIRED=_KEYS


def _vec(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise ValueError('invalid '+label)
    if any(isinstance(value[k],bool) or not isinstance(value[k],(int,float)) or not math.isfinite(value[k]) for k in keys):raise ValueError('invalid '+label)
    return {k:value[k] for k in keys}


def _pose(value):
    if not isinstance(value,dict) or set(value)!={'position','rotation'}:raise ValueError('invalid explicit pose')
    point=_vec(value['position'],('x','y','z'),'position')
    rotation=_vec(value['rotation'],('x','y','z','w'),'rotation')
    return Pose(position=Point3(**point),rotation=Quaternion(**rotation))


def manual_intake_request_sha256(*, request_id, source_sha256, base_model_sha256, frame_id, items):
    """Bind normalized user-supplied request content; the digest does not authenticate it."""
    try:
        rows=capture(items,byte_limit=CANDIDATE_BYTES)
        if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('source_item_id'),str) for row in rows):
            raise ValueError('invalid intake items')
        rows.sort(key=lambda row:row['source_item_id'])
        request=_portable_json({'request_id':request_id,'source_sha256':source_sha256,'base_model_sha256':base_model_sha256,'frame_id':frame_id,'items':rows})
        _REQUEST_SCHEMA.validate(request)
        return hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    except (VerificationStop,ValidationError,TypeError,ValueError,RecursionError) as exc:
        raise ValueError('invalid intake request') from exc


def admit_manual_devices(model, *, request_id, source_sha256, base_model_sha256, frame_id, items, request_sha256):
    """Return (detached model, report), never mutate or infer missing placement.

    Source hash and frame are caller assertions, not authenticated evidence or
    proof of site registration. Existing IDs are never replaced. Every admitted
    row supplies one explicitly positioned device and an explicit load port.
    """
    if not isinstance(model,BuildingModel):raise TypeError('expected a canonical model')
    if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>512:raise ValueError('request id required')
    if not isinstance(source_sha256,str) or re.fullmatch(r'[0-9a-f]{64}',source_sha256) is None:raise ValueError('source hash required')
    try:
        base=capture(model,byte_limit=SOURCE_BYTES)
        rows=capture(items,byte_limit=CANDIDATE_BYTES)
    except VerificationStop as exc:raise ValueError('invalid or oversized intake') from exc
    validate_model(base)
    base_hash=model_fingerprint(base)
    expected=manual_intake_request_sha256(request_id=request_id,source_sha256=source_sha256,base_model_sha256=base_model_sha256,frame_id=frame_id,items=rows)
    if not isinstance(request_sha256,str) or request_sha256!=expected:raise ValueError('stale intake request binding')
    if not isinstance(base_model_sha256,str) or base_model_sha256!=base_hash:raise ValueError('stale base model binding')
    if frame_id!=base.coordinate_system.frame_id:raise ValueError('model frame mismatch')
    if not isinstance(rows,list) or not rows or len(rows)>2000:raise ValueError('intake outside supported size')
    if any(not isinstance(row,dict) or set(row)-_KEYS for row in rows):raise ValueError('invalid item fields')
    ids=[row.get('source_item_id') for row in rows]
    if any(not isinstance(sid,str) or not sid.strip() or len(sid)>512 for sid in ids) or len(set(ids))!=len(ids):raise ValueError('source item ids must be nonempty and unique')
    rows.sort(key=lambda row:row['source_item_id'])
    owners=[entity for collection in _entity_collections(base) for entity in collection]
    for route in base.routes:
        if isinstance(route,PhysicalRoute):owners.extend((*route.joints,*route.segments,route.requirements,route.context.allowed_domain))
    occupied={base.model_id,*(owner.id for owner in owners)}
    requested=[]
    for row in rows:
        for key in ('device_id','port_id'):
            identity=row.get(key)
            if identity is not None:
                _validate_id(identity,key)
                if identity in occupied or identity in requested:raise ValueError('canonical id collision')
                requested.append(identity)
    previously_admitted={record.source_element_id for owner in owners for record in owner.provenance
        if record.source_kind=='manual-takeoff' and record.attributes.get('source_sha256_asserted')==source_sha256}
    if any(sid in previously_admitted for sid in ids):raise ValueError('source item already admitted')
    devices=[];ports=[];decisions=[]
    levels={level.id for level in base.levels}
    for row in rows:
        sid=row['source_item_id']
        for key in ('device_id','device_type','level_id','port_id'):
            if key in row and row[key] is not None and (not isinstance(row[key],str) or not row[key].strip()):raise ValueError('invalid explicit device identity')
        if 'level_id' in row and row['level_id'] is not None and row['level_id'] not in levels:raise ValueError('device level not in model')
        quantity=row.get('quantity')
        if quantity is not None and (isinstance(quantity,bool) or not isinstance(quantity,int) or quantity<1):raise ValueError('quantity must be a positive integer')
        confidence=row.get('confidence')
        if confidence is not None and (isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not 0<=confidence<=1):raise ValueError('invalid explicit confidence')
        device_pose=_pose(row['device_pose']) if row.get('device_pose') is not None else None
        port_pose=_pose(row['port_pose']) if row.get('port_pose') is not None else None
        direction=Vector3(**_vec(row['port_direction'],('x','y','z'),'direction')) if row.get('port_direction') is not None else None
        if direction is not None and direction.magnitude<=1e-9:raise ValueError('invalid explicit port direction')
        missing=sorted(k for k in _REQUIRED if k not in row or row[k] is None)
        if missing:
            decisions.append({'source_item_id':sid,'status':'unresolved','reason':'missing_explicit_fields','missing_fields':missing});continue
        if quantity!=1:
            decisions.append({'source_item_id':sid,'status':'unresolved','reason':'individual_placement_required','missing_fields':[]});continue
        provenance=(Provenance(source_kind='manual-takeoff',source_id=request_id,source_element_id=sid,
            method='explicit-user-supplied',confidence=confidence,derivation='user',
            attributes={'source_sha256_asserted':source_sha256,'frame_id_asserted':frame_id}),)
        devices.append(ElectricalDevice(id=row['device_id'],device_type=row['device_type'],level_id=row['level_id'],
            pose=device_pose,confidence=confidence,provenance=provenance))
        ports.append(Port(id=row['port_id'],owner_id=row['device_id'],domain='electrical',role='load',
            pose=port_pose,direction=direction,confidence=confidence,provenance=provenance))
        decisions.append({'source_item_id':sid,'status':'admitted','device_id':row['device_id'],'load_port_id':row['port_id']})
    candidate=replace(base,electrical_devices=(*base.electrical_devices,*devices),ports=(*base.ports,*ports))
    validate_model(candidate)
    report={'version':'manual-device-intake-v1','request_id':request_id,'source_sha256_asserted':source_sha256,
        'base_model_sha256':base_hash,'candidate_model_sha256':model_fingerprint(candidate),
        'request_sha256':request_sha256,'frame_id_asserted':frame_id,'items':decisions,
        'applied_to_original':False,'source_authentication':'not_verified','registration':'not_verified',
        'limitations':['Source hash and frame are user assertions, not authenticated or registration proof.',
            'No panel, circuit, grouping, route, field condition or code compliance is inferred.',
            'Incomplete and quantity-only items are unresolved, not expanded into guessed devices.',
            'Device type labels are caller-supplied and not verified against a catalog.']}
    try:_REPORT_SCHEMA.validate(report)
    except ValidationError as exc:raise ValueError('invalid intake report') from exc
    return candidate,report
