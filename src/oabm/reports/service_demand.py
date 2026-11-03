"""Explicit supplied demand arithmetic, separate from canonical connected VA."""
from __future__ import annotations
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
import re

from oabm.model import ContractError
from oabm.model.common import _validate_id
from oabm.reports.physical_routes import _snapshot,_json
from oabm.reports.source_files import hash_source_file
from oabm.routing.physical_capture import capture,REPORT_BYTES,bounded_source_json


def _keys(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):
        raise ContractError(f'{label}: exact fields required: {sorted(keys)}')


def _decimal(value,label):
    if type(value) not in (int,float):raise ContractError(f'{label}: explicit finite nonnegative number required')
    result=Decimal(str(value))
    if not result.is_finite() or result<0:raise ContractError(f'{label}: explicit finite nonnegative number required')
    return Fraction(result)


def calculate_service_demand(model,request,*,source_files):
    """Calculate only the supplied first-block/remainder demand scenario.

    Factors and grouping are caller inputs with evidence, not code interpretation.
    Supplied decimal numbers become exact rational arithmetic. Exact values use
    integer or numerator/denominator strings alongside finite display numbers.
    """
    model,raw=_snapshot(model)
    try:request=capture(request,byte_limit=REPORT_BYTES)
    except Exception as exc:
        from oabm.routing.physical_verify_types import VerificationStop
        if isinstance(exc,VerificationStop):raise ContractError(f'demand request admission refused: {exc.reason}') from exc
        raise
    _keys(request,{'kind','source_model_sha256','service_node_id','voltage_v','sources','components','general_demand','sensitivity'},'service demand')
    if request['kind']!='supplied_service_demand_v1':raise ContractError('unsupported supplied demand request')
    digest=hashlib.sha256(raw.encode()).hexdigest()
    if request['source_model_sha256']!=digest:raise ContractError('service demand is stale against canonical model')
    nodes={node.id:node for node in model.service_nodes}
    _validate_id(request['service_node_id'],'demand service_node_id')
    if request['service_node_id'] not in nodes or nodes[request['service_node_id']].equipment_type!='meter_main':
        raise ContractError('demand must bind to a canonical meter_main service node')
    voltage=_decimal(request['voltage_v'],'voltage_v')
    if voltage<=0:raise ContractError('demand voltage must be positive')
    service=nodes[request['service_node_id']]
    if service.equipment_id is not None:
        owner=next(item for item in model.electrical_equipment if item.id==service.equipment_id)
        if owner.rated_voltage_v is not None and _decimal(owner.rated_voltage_v,'canonical service voltage')!=voltage:
            raise ContractError('demand voltage contradicts known canonical service owner voltage')
    sources=request['sources']
    if not isinstance(source_files,dict):raise ContractError('explicit demand source-file mapping required')
    if not isinstance(sources,dict) or not sources:raise ContractError('demand requires supplied source digests')
    for identity,expected in sources.items():
        _validate_id(identity,'demand source id')
        if not isinstance(expected,str) or re.fullmatch('[0-9a-f]{64}',expected) is None:
            raise ContractError('demand source requires lowercase SHA256')
        if identity not in source_files:raise ContractError(f'missing demand source bytes for {identity}')
        if hash_source_file(source_files[identity])!=expected:raise ContractError('demand source hash mismatch')
    components=request['components']
    if not isinstance(components,list) or not components or len(components)>1000:
        raise ContractError('demand requires 1..1000 components; no rows truncated')
    seen=set();sums={group:Fraction(0) for group in ('general','hvac','floor_heat')}
    for row in components:
        _keys(row,{'id','name','group','va','source_id','basis','assumption'},'demand component')
        _validate_id(row['id'],'demand component id')
        _validate_id(row['source_id'],'demand component source_id')
        if row['id'] in seen:raise ContractError('duplicate demand component id')
        seen.add(row['id'])
        for field in ('name','basis'):
            if not isinstance(row[field],str) or not row[field].strip():raise ContractError('component name/basis must be supplied')
        if not isinstance(row['group'],str) or row['group'] not in sums or row['source_id'] not in sources:
            raise ContractError('component must reference a supplied demand group/source')
        if type(row['assumption']) is not bool:raise ContractError('component assumption must be explicit boolean')
        sums[row['group']]+=_decimal(row['va'],'component va')
    if {row['group'] for row in components}!=set(sums):
        raise ContractError('each demand group needs an explicit supplied component, including any source-confirmed zero')
    policy=request['general_demand']
    _keys(policy,{'first_va','remainder_factor','basis','source_id'},'general demand policy')
    _validate_id(policy['source_id'],'demand policy source_id')
    if not isinstance(policy['basis'],str) or not policy['basis'].strip() or policy['source_id'] not in sources:
        raise ContractError('demand factor policy requires supplied source/basis')
    first=_decimal(policy['first_va'],'first_va');factor=_decimal(policy['remainder_factor'],'remainder_factor')
    if factor>1:raise ContractError('supplied remainder factor must be within0..1')
    general=min(sums['general'],first)+max(Fraction(0),sums['general']-first)*factor
    total=general+sums['hvac']+sums['floor_heat']
    scenario=request['sensitivity']
    _keys(scenario,{'additional_va','basis','source_id'},'demand sensitivity')
    _validate_id(scenario['source_id'],'demand sensitivity source_id')
    if not isinstance(scenario['basis'],str) or not scenario['basis'].strip() or scenario['source_id'] not in sources:
        raise ContractError('demand sensitivity requires source/basis')
    sensitivity=_decimal(scenario['additional_va'],'sensitivity additional_va')
    try:display=(float(total/voltage),float((total+sensitivity)/voltage))
    except OverflowError as exc:raise ContractError('demand display exceeds finite numerical range') from exc
    if any(not math.isfinite(value) for value in display):raise ContractError('demand display exceeds finite numerical range')
    report={'kind':'supplied_service_demand_report','version':'1.0.0','source_model_sha256':digest,
            'source_request_sha256':hashlib.sha256(_json(request).encode()).hexdigest(),
            'service_node_id':request['service_node_id'],'inputs':request,
            'general_connected_va_exact':str(sums['general']),'general_demand_va_exact':str(general),
            'no_supplemental_heat_va_exact':str(total),'no_supplemental_heat_a_exact':str(total/voltage),
            'sensitivity_va_exact':str(total+sensitivity),'sensitivity_a_exact':str((total+sensitivity)/voltage),
            'no_supplemental_heat_a':display[0],'sensitivity_a':display[1],
            'arithmetic':{'voltage_v_exact':str(voltage),'first_threshold_va_exact':str(first),
                'remainder_factor_exact':str(factor),'first_applied_va_exact':str(min(sums['general'],first)),
                'remainder_connected_va_exact':str(max(Fraction(0),sums['general']-first)),
                'remainder_demand_va_exact':str(max(Fraction(0),sums['general']-first)*factor),
                'group_connected_va_exact':{key:str(value) for key,value in sums.items()},
                'sensitivity_additional_va_exact':str(sensitivity)},
            'connected_circuit_values_modified':False,'engineering_approval':False,
            'limitations':['Supplied demand arithmetic only; grouping, factors and assumptions are not an automatic NEC compliance decision.',
                           'Demand differs from connected VA; no circuit loads, feeder sizes or selected equipment are inferred.']}
    if len(_json(report).encode())>REPORT_BYTES:raise ContractError('service demand report exceeds byte admission; no rows truncated')
    return report


def bind_service_demand_inputs(model,profile,*,source_files):
    """Build a new derived request from an explicitly unbound supplied profile.

    This does not accept or rebind an old protected request. A field edit can
    regenerate a new report from the same separately source-verified load inputs.
    """
    _keys(profile,{'kind','service_node_id','voltage_v','sources','components','general_demand','sensitivity'},'supplied demand inputs')
    if profile['kind']!='supplied_service_demand_inputs_v1':raise ContractError('unbound supplied demand input profile required')
    _,raw=_snapshot(model)
    request={**profile,'kind':'supplied_service_demand_v1','source_model_sha256':hashlib.sha256(raw.encode()).hexdigest()}
    return calculate_service_demand(model,request,source_files=source_files)


def validate_service_demand_report(model,payload,*,source_files):
    """Regenerate all retained values, not merely accept a source-hash shape."""
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ContractError('duplicate service demand JSON key')
            result[key]=value
        return result
    from oabm.routing.physical_verify_types import VerificationStop
    try:
        if not isinstance(payload,(str,bytes)) or len(payload.encode() if isinstance(payload,str) else payload)>REPORT_BYTES:
            raise ContractError('service demand report exceeds admission')
        actual=json.loads(bounded_source_json(payload),object_pairs_hook=unique)
        expected=calculate_service_demand(model,actual['inputs'],source_files=source_files)
        if _json(actual)!=_json(expected):raise ContractError('service demand report is stale or changed')
        return expected
    except (VerificationStop,KeyError,TypeError,ValueError,OverflowError,RecursionError) as exc:
        raise ContractError('invalid or stale service demand report') from exc
