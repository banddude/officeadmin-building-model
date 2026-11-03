"""Explicit manual-intake to preliminary design; no saved review or approval."""
from __future__ import annotations
from dataclasses import fields
import hashlib
import json
from importlib.resources import files
from jsonschema import Draft202012Validator,ValidationError
from oabm.model import BuildingModel, Box3D, Size3, reject_physical_routes
from oabm.model.model import _entity_collections
from oabm.electrical.manual_device_intake import admit_manual_devices, _pose, _vec
from oabm.reports.source_coverage import model_fingerprint
from oabm.routing.design import CircuitDesignRules, design_proposed_circuits
from oabm.routing.router import AllowedRouteDomain, RoutingOptions
from oabm.routing.physical_capture import capture, CANDIDATE_BYTES
from oabm.routing.physical_verify_types import VerificationStop
from oabm.electrical.sizing_provider import _portable_json


_CONTRACTS=files('oabm.electrical._manual_design_contracts')
_DECISION_SCHEMA=Draft202012Validator(json.loads(_CONTRACTS.joinpath('manual-design-decision-v1.schema.json').read_text()))
_REPORT_SCHEMA=Draft202012Validator(json.loads(_CONTRACTS.joinpath('manual-design-report-v1.schema.json').read_text()))

def manual_design_decision_sha256(decision):
    """Hash normalized caller decision content; this is not authentication."""
    try:
        value=_portable_json(capture(decision,byte_limit=CANDIDATE_BYTES))
        _DECISION_SCHEMA.validate(value)
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    except (VerificationStop,ValidationError,TypeError,ValueError,RecursionError) as exc:
        raise ValueError('invalid manual design decision') from exc


def design_manual_intake(base:BuildingModel, *, intake_request:dict, decision:dict, decision_sha256:str):
    """Recompute intake, then call the preliminary router with explicit inputs.

    The source, registration and decision are unverified caller assertions.
    This is not the PDF-reviewed saved pipeline or engineering approval.
    """
    try:
        intake=capture(intake_request,byte_limit=CANDIDATE_BYTES)
        choice=_portable_json(capture(decision,byte_limit=CANDIDATE_BYTES))
    except (VerificationStop,TypeError,ValueError,RecursionError) as exc:raise ValueError('invalid or oversized manual design input') from exc
    expected=manual_design_decision_sha256(choice)
    if not isinstance(decision_sha256,str) or decision_sha256!=expected:raise ValueError('stale manual decision binding')
    if not isinstance(intake,dict) or set(intake)!={'request_id','source_sha256','base_model_sha256','frame_id','items','request_sha256'}:
        raise ValueError('invalid manual intake request fields')
    candidate, receipt=admit_manual_devices(base,**intake)
    keys={'decision_id','candidate_model_sha256','intake_request_sha256','equipment_id','source_port_id','groups','load_ports','rules','routing_controls','allowed_domain'}
    if not isinstance(choice,dict) or set(choice)!=keys:raise ValueError('invalid manual design decision fields')
    if not isinstance(choice['decision_id'],str) or not choice['decision_id'].strip() or len(choice['decision_id'])>512:raise ValueError('decision id required')
    if choice['candidate_model_sha256']!=receipt['candidate_model_sha256'] or choice['intake_request_sha256']!=receipt['request_sha256']:
        raise ValueError('stale manual design binding')
    admitted={row['device_id']:row['load_port_id'] for row in receipt['items'] if row['status']=='admitted'}
    if len(admitted)>4:raise ValueError('manual design supports at most four devices per request')
    if sum(len(collection) for collection in _entity_collections(candidate))>512:raise ValueError('manual design model exceeds the bounded entity limit')
    reject_physical_routes(candidate,'manual preliminary design')
    groups=choice['groups']; mapping=choice['load_ports']
    if not admitted or not isinstance(groups,list) or not groups or any(not isinstance(group,list) or not group or any(not isinstance(x,str) for x in group) for group in groups):
        raise ValueError('explicit nonempty device groups required')
    flattened=[device for group in groups for device in group]
    if len(flattened)!=len(set(flattened)) or set(flattened)!=set(admitted) or mapping!=admitted:
        raise ValueError('groups and load ports must exactly cover admitted devices')
    rules=choice['rules'];controls=choice['routing_controls']
    if not isinstance(rules,dict) or set(rules)!={f.name for f in fields(CircuitDesignRules)}:raise ValueError('all circuit rules must be explicit')
    if not isinstance(controls,dict) or set(controls)!={f.name for f in fields(RoutingOptions)}:raise ValueError('all routing controls must be explicit')
    domain_data=choice['allowed_domain']
    if not isinstance(domain_data,dict) or set(domain_data)!={'id','kind','volumes','source_ids'} or not isinstance(domain_data['volumes'],list) or not isinstance(domain_data['source_ids'],list):
        raise ValueError('explicit allowed route domain required')
    boxes=[]
    for volume in domain_data['volumes']:
        if not isinstance(volume,dict) or set(volume)!={'pose','size'}:raise ValueError('invalid explicit domain volume')
        boxes.append(Box3D(pose=_pose(volume['pose']),size=Size3(**_vec(volume['size'],('x','y','z'),'domain size'))))
    domain=AllowedRouteDomain(id=domain_data['id'],kind=domain_data['kind'],volumes=tuple(boxes),source_ids=tuple(domain_data['source_ids']))
    source=next((port for port in candidate.ports if port.id==choice['source_port_id']),None)
    if source is None or source.owner_id!=choice['equipment_id'] or source.domain!='electrical' or source.role!='source':
        raise ValueError('invalid explicit panel source port')
    # The underlying router may replace its managed design. Never permit that
    # side effect here: a panel with any source circuit or owned design is held.
    source_ids={port.id for port in candidate.ports if port.owner_id==choice['equipment_id']}
    if any((port.id in source_ids and port.connected_port_ids) or any(target in source_ids for target in port.connected_port_ids) for port in candidate.ports):
        raise ValueError('panel already has port connections; replacement is not supported')
    if any(route.start_port_id in source_ids or route.end_port_id in source_ids for route in candidate.routes):
        raise ValueError('panel already has connected routes; replacement is not supported')
    for circuit in candidate.circuits:
        marker=circuit.attributes.get('design')
        if circuit.source_port_id in source_ids or (isinstance(marker,dict) and marker.get('equipment_id')==choice['equipment_id']):
            raise ValueError('panel already has circuiting; replacement is not supported')
    rule_obj=CircuitDesignRules(**rules);control_obj=RoutingOptions(**controls)
    result=design_proposed_circuits(candidate,equipment_id=choice['equipment_id'],device_ids=tuple(flattened),rules=rule_obj,
        user_groups=tuple(tuple(group) for group in groups),user_input_id=choice['decision_id'],source_port_id=choice['source_port_id'],
        load_port_ids=mapping,routing_options=control_obj,allowed_domain=domain)
    before=candidate.to_dict();after=result.to_dict()
    for collection in ('circuits','routes','conductors'):
        old={row['id']:row for row in before[collection]};new={row['id']:row for row in after[collection]}
        if any(identity not in new or json.dumps(new[identity],sort_keys=True,allow_nan=False)!=json.dumps(row,sort_keys=True,allow_nan=False) for identity,row in old.items()):
            raise ValueError('pre-existing design changed unexpectedly')
    new_circuits=sorted({row['id'] for row in after['circuits']}-{row['id'] for row in before['circuits']})
    new_routes=sorted({row['id'] for row in after['routes']}-{row['id'] for row in before['routes']})
    if not new_circuits or not new_routes:raise ValueError('preliminary design produced no circuit or route')
    report={'version':'manual-preliminary-design-v1','decision_id':choice['decision_id'],
        'decision_sha256':decision_sha256,
        'base_model_sha256':receipt['base_model_sha256'],'intake_request_sha256':receipt['request_sha256'],
        'candidate_model_sha256':receipt['candidate_model_sha256'],'designed_model_sha256':model_fingerprint(result),
        'unresolved_items':[row for row in receipt['items'] if row['status']=='unresolved'],
        'device_ids':flattened,'equipment_id':choice['equipment_id'],'source_port_id':choice['source_port_id'],
        'new_circuit_ids':new_circuits,'new_route_ids':new_routes,'source_observed':False,'applied_to_original':False,'decision_authentication':'not_verified',
        'limitations':['Preliminary user-supplied design, not source observation or engineering approval.',
            'Source authenticity, registration, inventory completeness, clearance, electrical sizing and code compliance are not verified.',
            'Existing panel circuiting is refused; no automatic replacement is allowed.']}
    try:_REPORT_SCHEMA.validate(report)
    except ValidationError as exc:raise ValueError('invalid manual design report') from exc
    return result,report
