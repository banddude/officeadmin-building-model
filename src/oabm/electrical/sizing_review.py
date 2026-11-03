"""Local-only preview of a proposed sizing change; never applies a design."""
from dataclasses import asdict
import hashlib
import json
import re
from importlib.resources import files
from jsonschema import Draft202012Validator, ValidationError

from oabm.model import BuildingModel, Route
from oabm.electrical.sizing_provider import request_payload, size_model_circuit

_RESPONSE = Draft202012Validator(json.loads(files('oabm.electrical._sizing_contracts').joinpath('sizing-result-v1.schema.json').read_text()))
_REQUEST = Draft202012Validator(json.loads(files('oabm.electrical._sizing_contracts').joinpath('sizing-request-v1.schema.json').read_text()))
_PREVIEW = Draft202012Validator(json.loads(files('oabm.electrical._sizing_contracts').joinpath('sizing-preview-v1.schema.json').read_text()))

def _validate(validator, value, label):
    try:
        validator.validate(value)
    except ValidationError:
        raise ValueError('invalid ' + label) from None

_AWG = {'14','12','10','8','6','4','3','2','1','1/0','2/0','3/0','4/0'}
_KCMIL = {'250','300','350','400','500','600','700','750','900','1000'}

def _size(value):
    if not isinstance(value, str): return None
    m = re.fullmatch(r'\s*(#)?\s*(\d+(?:/0)?)\s*(AWG|KCMIL)?\s*', value, re.I)
    if not m: return None
    mark, number, suffix = m.groups()
    if number in _AWG and (suffix is None or suffix.upper() == 'AWG'): return number
    if number in _KCMIL and not mark and (suffix is None or suffix.upper() == 'KCMIL'): return number
    return None

def _material(value):
    if not isinstance(value, str): return None
    return {'cu':'cu','copper':'cu','al':'al','aluminum':'al','aluminium':'al'}.get(value.strip().lower())

def preview(model, proposal):
    if not isinstance(model, BuildingModel): raise TypeError('expected a canonical model')
    proposal = json.loads(json.dumps(proposal, allow_nan=False))
    _validate(_RESPONSE, proposal, 'sizing proposal')
    snapshot = model.to_json(indent=None)
    detached = BuildingModel.from_json(snapshot)
    sha = hashlib.sha256(snapshot.encode()).hexdigest()
    if proposal.get('model_id') != detached.model_id or proposal.get('model_sha256') != sha:
        raise ValueError('proposal is stale or from another model')
    request = {k:proposal[k] for k in ('version','purpose','model_id','model_sha256','circuit_id','inputs')}
    request['version'] = 'sizing-request-v1'
    payload = request_payload(request)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    if proposal.get('request_digest') != digest or proposal.get('request_payload') != payload: raise ValueError('request binding mismatch')
    _validate(_REQUEST, {**request,'request_digest':digest,'request_payload':payload}, 'sizing request')
    if proposal.get('status') != 'partial' or proposal.get('purpose') != 'proposed_only':
        raise ValueError('only an unapplied partial proposal can be previewed')
    matches = [c for c in detached.circuits if c.id == proposal['circuit_id']]
    if len(matches) != 1: raise ValueError('circuit missing')
    circuit = matches[0]
    mapping = proposal.get('target_mapping') or {}
    if mapping.get('applied') is not False or mapping.get('circuit_id') != circuit.id or mapping.get('route_ids') != list(circuit.route_ids):
        raise ValueError('target mapping mismatch')
    expected_conductors = [
        {'id':c.id,'role':c.role,'material':c.material,'size':c.size,'insulation':c.insulation,'count':c.count,'route_ids':list(c.route_ids),'confidence':c.confidence,'provenance':json.loads(json.dumps([asdict(p) for p in c.provenance],allow_nan=False))}
        for c in detached.conductors if c.circuit_id == circuit.id
    ]
    expected_routes = [{'id':r.id,'route_type':r.route_type,'nominal_diameter_m':r.nominal_diameter_m} for r in detached.routes if r.id in circuit.route_ids and isinstance(r,Route)]
    if mapping.get('existing_conductors') != expected_conductors or mapping.get('existing_routes') != expected_routes:
        raise ValueError('mapped source facts differ from model')
    result = proposal.get('result') or {}
    if not proposal.get('provider') or result.get('violations') or result.get('unsupported_reason') is not None or result.get('voltage_drop_percent') is None or result['voltage_drop_percent'] < 0 or result['voltage_drop_percent'] > proposal['inputs']['vd_limit_pct']:
        raise ValueError('proposal contains unresolved provider checks')
    if mapping.get('proposed_wire_size') != result.get('wire_size') or mapping.get('proposed_conduit_trade_size') != result.get('conduit_trade_size'):
        raise ValueError('mapped proposal differs from provider output')
    scope = ['line'] + (['neutral'] if proposal['inputs'].get('neutral') and proposal['inputs'].get('neutral_same_size_as_phase') else [])
    if mapping.get('proposed_wire_scope') != scope or mapping.get('ground_size_verified') is not False:
        raise ValueError('proposed conductor scope or ground status differs')
    if mapping.get('ground_size_supplied') != (proposal['inputs'].get('egc_size') if proposal['inputs'].get('egc') else None):
        raise ValueError('ground input mismatch')
    # Replay the boundary's validation against the detached source. This checks
    # consistency, not provider authenticity: the result remains editable.
    design_inputs = {k:v for k,v in proposal['inputs'].items() if k not in ('source_evidence','length_evidence')}
    provider_response = {k:v for k,v in proposal.items() if k != 'target_mapping'}
    replay = size_model_circuit(detached, circuit.id, design_inputs, lambda _: provider_response)
    if replay.get('status') != 'partial' or replay.get('target_mapping') != mapping:
        raise ValueError('proposal no longer satisfies the sizing boundary')
    suggested = result.get('wire_size')
    if _size(suggested) is None: raise ValueError('unrecognized proposed wire size')
    comparisons = []
    for conductor in expected_conductors:
        if conductor['role'] not in scope: continue
        previous = _size(conductor['size'])
        previous_material = _material(conductor['material'])
        proposed_material = proposal['inputs']['material']
        membership = 'matched' if circuit.route_ids and conductor['route_ids'] == list(circuit.route_ids) else 'unknown'
        comparisons.append({'conductor_id':conductor['id'],'role':conductor['role'],'count':conductor['count'],'route_membership':membership,'existing_size':conductor['size'],'proposed_size':suggested,'size_comparison':'unknown' if previous is None else 'same' if previous == suggested else 'different','existing_material':conductor['material'],'proposed_material':proposed_material,'material_comparison':'unknown' if previous_material is None else 'same' if previous_material == proposed_material else 'different','existing_insulation':conductor['insulation'],'proposed_insulation':proposal['inputs']['insulation']})
    represented = {row['role'] for row in comparisons}
    missing_roles = [role for role in scope if role not in represented]
    excluded = [{'conductor_id':c['id'],'role':c['role'],'count':c['count'],'route_membership':'matched' if circuit.route_ids and c['route_ids'] == list(circuit.route_ids) else 'unknown','existing_size':c['size'],'existing_material':c['material']} for c in expected_conductors if c['role'] not in scope]
    ground_roles = {'equipment-ground','ground','egc'}
    grounds = [row for row in excluded if row['role'] in ground_roles]
    unmatched_roles = sorted({row['role'] for row in excluded if row['role'] not in ground_roles})
    ground_requested = proposal['inputs']['egc']
    ground_count = sum(row['count'] for row in grounds)
    ground_mapping_status = ('present_unverified' if ground_count == 1 else 'missing' if ground_count == 0 else 'count_mismatch') if ground_requested else ('existing_not_in_proposal' if grounds else 'not_requested')
    supplied_ground = proposal['inputs'].get('egc_size') if ground_requested else None
    ground_size_comparison = None
    if ground_count == 1:
        old_ground = _size(grounds[0]['existing_size'])
        new_ground = _size(supplied_ground)
        ground_size_comparison = 'unknown' if old_ground is None or new_ground is None else 'same' if old_ground == new_ground else 'different'
    expected_counts = {'line':proposal['inputs']['hot_conductor_count'], **({'neutral':1} if 'neutral' in scope else {})}
    observed_counts = {role:sum(row['count'] for row in comparisons if row['role']==role) for role in scope}
    counts_match = expected_counts == observed_counts
    topology_gap = bool(unmatched_roles) or ground_mapping_status in ('missing','count_mismatch','existing_not_in_proposal') or (ground_requested and ground_size_comparison == 'unknown')
    route_gap = not circuit.route_ids or any(row['route_membership'] == 'unknown' for row in comparisons + excluded)
    unresolved = missing_roles or topology_gap or route_gap or not counts_match or any(row['size_comparison'] == 'unknown' or row['material_comparison'] == 'unknown' for row in comparisons)
    different = ground_size_comparison == 'different' or any(row['size_comparison'] == 'different' or row['material_comparison'] == 'different' for row in comparisons)
    comparison_status = 'unresolved' if unresolved else 'differences_found' if different else 'matches_in_scope'
    output = {'version':'sizing-preview-v1','model_id':detached.model_id,'model_sha256':sha,'circuit_id':circuit.id,'request_digest':digest,'provider':proposal['provider'],'inputs':proposal['inputs'],'route_ids':list(circuit.route_ids),'conductors':comparisons,'excluded_conductors':excluded,'unmatched_roles':unmatched_roles,'ground_mapping_status':ground_mapping_status,'ground_count':ground_count,'ground_size_comparison':ground_size_comparison,'comparison_scope':'line_neutral_size_material_and_ground_size_only','comparison_status':comparison_status,'missing_roles':missing_roles,'expected_counts':expected_counts,'observed_counts':observed_counts,'existing_routes':expected_routes,'proposed_conduit_trade_size':result.get('conduit_trade_size'),'provider_output_authentication':'not_verified','applied':False,'limitations':['Provider output is editable and unauthenticated; request binding does not authenticate the result.','Comparison is not engineering verification or authorization to apply the proposal.','Insulation equivalence, ground adequacy and existing conduit equivalence are not assessed.',*proposal.get('limitations',[])]}
    _validate(_PREVIEW, output, 'sizing preview')
    return output
