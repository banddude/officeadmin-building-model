"""Explicit, opt-in circuit sizing provider boundary. Never applies a design."""
import hashlib, json, math
from importlib.resources import files
from dataclasses import asdict
from jsonschema import Draft202012Validator, ValidationError
from oabm.model import BuildingModel, Circuit, Route

_SCHEMAS = files('oabm.electrical._sizing_contracts')
_REQUEST = Draft202012Validator(json.loads(_SCHEMAS.joinpath('sizing-request-v1.schema.json').read_text()))
_RESPONSE = Draft202012Validator(json.loads(_SCHEMAS.joinpath('sizing-result-v1.schema.json').read_text()))

def _portable_json(value):
    """Normalize JSON numbers for Python/JavaScript round trips; reject unsafe integers."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError('integer is outside the interoperable JSON range')
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 2**53 - 1:
            raise ValueError('number is outside the interoperable JSON range')
        return int(value) if value.is_integer() else value
    if isinstance(value, (tuple, list)):
        return [_portable_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key:_portable_json(item) for key,item in value.items()}
    raise ValueError('value is not interoperable JSON')

def request_payload(body):
    """Exact UTF-8 JSON text to be echoed, not a cross-language canonicalization rule."""
    return json.dumps(_portable_json(body), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

def request_digest(body):
    """Correlation digest of the exact payload text, not authentication."""
    return hashlib.sha256(request_payload(body).encode()).hexdigest()

def request_from_canonical(circuit, design_inputs):
    """Map a canonical Circuit only; caller must validate its model references."""
    if not isinstance(circuit, Circuit):
        raise TypeError('expected a canonical Circuit')
    cid = circuit.id
    if not isinstance(cid, str) or not cid:
        raise ValueError('canonical circuit ID required')
    data = dict(design_inputs)
    if 'source_evidence' in data:
        raise ValueError('source evidence must come from the model')
    data['source_evidence'] = json.loads(json.dumps({'confidence':circuit.confidence, 'provenance':[asdict(p) for p in circuit.provenance]}, allow_nan=False))
    poles = circuit.poles
    if poles is not None and 'hot_conductor_count' in data and poles != data['hot_conductor_count']:
        raise ValueError('canonical poles cannot be reconciled with hot conductor count')
    phase = circuit.phase
    if phase is not None:
        normalized = {'single':1, '1':1, '1ph':1, 'three':3, '3':3, '3ph':3}.get(str(phase).lower())
        if normalized is None or ('phase' in data and normalized != data['phase']):
            raise ValueError('canonical phase is ambiguous or conflicts with sizing phase')
        data.setdefault('phase', normalized)
    for canonical, provider in [('voltage_v', 'voltage_v'), ('load_va', 'load_va')]:
        value = getattr(circuit, canonical)
        if value is not None:
            if provider in data and data[provider] != value:
                raise ValueError('conflicting ' + provider)
            data[provider] = value
    return cid, data

def request_from_model(model, circuit_id, design_inputs):
    if not isinstance(model, BuildingModel):
        raise TypeError('expected a canonical BuildingModel')
    snapshot = model.to_json(indent=None)
    detached = BuildingModel.from_json(snapshot)
    found = [c for c in detached.circuits if c.id == circuit_id]
    if len(found) != 1:
        raise ValueError('circuit not found in validated snapshot')
    cid, inputs = request_from_canonical(found[0], design_inputs)
    if len(found[0].load_port_ids) != 1 or len(found[0].route_ids) > 1:
        raise ValueError('multi-load or multi-route circuit mapping is unsupported')
    mapped_routes = set(found[0].route_ids)
    if any(set(conductor.route_ids) - mapped_routes for conductor in detached.conductors if conductor.circuit_id == cid):
        raise ValueError('a conductor extends beyond the mapped circuit route')
    if any(r.id in found[0].route_ids and not isinstance(r, Route) for r in detached.routes):
        raise ValueError('physical-route sizing is not supported by this prototype')
    if inputs.get('sole_circuit_in_raceway') is True:
        route_ids = set(found[0].route_ids)
        shared_circuit = any(route_ids.intersection(other.route_ids) for other in detached.circuits if other.id != cid)
        shared_conductor = any(route_ids.intersection(other.route_ids) for other in detached.conductors if other.circuit_id != cid)
        if shared_circuit or shared_conductor:
            raise ValueError('canonical route is shared by another circuit')
    if found[0].route_ids:
        measured_route, bound_route_id = single_route_length_ft(detached, cid)
        bound_route = next(r for r in detached.routes if r.id == bound_route_id)
        requested_type = inputs.get('conduit_type')
        if requested_type is not None and (not isinstance(requested_type, str) or bound_route.route_type.lower() != requested_type.lower()):
            raise ValueError('requested conduit type differs from the canonical route')
        if inputs.get('length_basis') == 'user_supplied' and isinstance(inputs.get('length_ft'), (int, float)) and not isinstance(inputs['length_ft'], bool) and inputs['length_ft'] + 1e-6 < measured_route:
            raise ValueError('user-supplied length is shorter than the mapped route')
    if inputs.get('length_basis') == 'model_centerline':
        measured, route_id = single_route_length_ft(detached, cid)
        supplied = inputs.get('length_ft')
        if inputs.get('source_route_id') != route_id or not isinstance(supplied, (int, float)) or isinstance(supplied, bool) or not math.isclose(supplied, measured, abs_tol=1e-6):
            raise ValueError('route length or route identity does not match the canonical snapshot')
        route = next(r for r in detached.routes if r.id == route_id)
        if 'length_evidence' in inputs:
            raise ValueError('route evidence must come from the model')
        inputs['length_evidence'] = json.loads(json.dumps({'confidence':route.confidence, 'provenance':[asdict(p) for p in route.provenance]}, allow_nan=False))
    elif 'length_evidence' in inputs or 'source_route_id' in inputs:
        raise ValueError('model route reference or evidence requires model centerline basis')
    return detached.model_id, hashlib.sha256(snapshot.encode()).hexdigest(), cid, inputs

def single_route_length_ft(model, circuit_id):
    """A strict single source-to-load polyline only; no slack or branch guessing."""
    if not isinstance(model, BuildingModel):
        raise TypeError('expected a canonical BuildingModel')
    detached = BuildingModel.from_json(model.to_json(indent=None))
    circuits = [c for c in detached.circuits if c.id == circuit_id]
    if len(circuits) != 1 or len(circuits[0].load_port_ids) != 1 or len(circuits[0].route_ids) != 1:
        raise ValueError('a single circuit, load and route are required')
    c = circuits[0]
    routes = [r for r in detached.routes if r.id == c.route_ids[0]]
    if len(routes) != 1 or not isinstance(routes[0], Route):
        raise ValueError('only a legacy polyline route is supported')
    r = routes[0]
    if r.start_port_id != c.source_port_id or r.end_port_id != c.load_port_ids[0]:
        raise ValueError('route does not explicitly connect this source and load')
    pts = r.centerline.points
    length_m = math.fsum(math.dist((a.x,a.y,a.z), (b.x,b.y,b.z)) for a,b in zip(pts, pts[1:]))
    if not math.isfinite(length_m) or length_m <= 0:
        raise ValueError('route length unavailable')
    return length_m / 0.3048, r.id

def _size_circuit(model_id, model_sha256, circuit_id, solver_input, provider):
    solver_input = _portable_json(solver_input)
    body = {'version': 'sizing-request-v1', 'purpose':'proposed_only', 'model_id':model_id, 'model_sha256':model_sha256, 'circuit_id': circuit_id, 'inputs': solver_input}
    payload = request_payload(body)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    request = {**body, 'request_digest': digest, 'request_payload':payload}
    errors = list(_REQUEST.iter_errors(request))
    if errors:
        missing = [e.message for e in errors if e.validator == 'required']
        if not any(k in solver_input for k in ('load_amps', 'load_va')):
            missing.append('load_amps or load_va is required')
        if missing and all(e.validator in ('required', 'anyOf') for e in errors):
            return {'version':'sizing-result-v1', 'purpose':'proposed_only', 'model_id':model_id, 'model_sha256':model_sha256, 'circuit_id':circuit_id, 'request_digest':digest, 'request_payload':payload, 'inputs':solver_input, 'status':'unresolved', 'missing':missing}
        locations = sorted({'.'.join(str(part) for part in e.absolute_path) or 'request' for e in errors})
        raise ValueError('invalid sizing request at: ' + ', '.join(locations))
    if solver_input['neutral_is_ccc'] and not solver_input['neutral']:
        raise ValueError('a missing neutral cannot be current carrying')
    if (solver_input['phase'] == 3 and solver_input['hot_conductor_count'] != 3) or (solver_input['phase'] == 1 and solver_input['hot_conductor_count'] not in (1, 2)):
        raise ValueError('hot conductor count conflicts with phase')
    minimum_ccc = solver_input['hot_conductor_count'] + int(solver_input['neutral'] and solver_input['neutral_is_ccc'])
    if solver_input['current_carrying_count'] < minimum_ccc:
        raise ValueError('current-carrying count is below the stated conductors')
    if solver_input['ambient_c'] < -273.15:
        raise ValueError('ambient temperature is below absolute zero')
    if solver_input.get('load_va') is not None and solver_input.get('load_amps') is not None:
        multiplier = math.sqrt(3) if solver_input['phase'] == 3 else 1
        if not math.isclose(solver_input['load_va'], solver_input['load_amps'] * solver_input['voltage_v'] * multiplier, rel_tol=1e-6, abs_tol=0.01):
            raise ValueError('load amperes and volt-amperes conflict')
    unsupported = (
        solver_input['parallel_sets'] != 1
        or not solver_input['sole_circuit_in_raceway']
        or solver_input['current_carrying_count'] != minimum_ccc
        or (solver_input['phase'] == 3 and not solver_input['balanced_three_phase'])
        or (solver_input['phase'] == 1 and solver_input['hot_conductor_count'] == 1 and not solver_input['neutral'])
        or (solver_input['phase'] == 1 and solver_input['hot_conductor_count'] == 2 and solver_input['neutral'])
        or (solver_input['neutral'] and not solver_input['neutral_same_size_as_phase'])
        or (solver_input['egc'] and not solver_input['egc_same_insulation_as_phase'])
        or solver_input['location_condition'] != 'dry'
        or solver_input['temp_rating_c'] > {'thhn':90, 'xhhw':75, 'thw':75, 'rhw':75}[solver_input['insulation']]
        or solver_input['ambient_c'] > 85
        or solver_input['ambient_c'] >= solver_input['temp_rating_c']
    )
    if unsupported:
        return {'version':'sizing-result-v1', 'purpose':'proposed_only', 'model_id':model_id, 'model_sha256':model_sha256, 'circuit_id':circuit_id, 'request_digest':digest, 'request_payload':payload, 'inputs':solver_input, 'status':'unsupported', 'limitations':['This boundary does not represent all required details for the selected circuit arrangement.']}
    # Copy the request: an in-process provider must not mutate the caller's
    # inputs or the evidence used to verify its response.
    request_copy = json.loads(json.dumps(request, allow_nan=False))
    result = provider(request_copy)
    if not isinstance(result, dict) or 'target_mapping' in result:
        raise ValueError('invalid or pre-mapped provider response')
    result = json.loads(json.dumps(result, allow_nan=False))
    try:
        _RESPONSE.validate(result)
    except ValidationError:
        raise ValueError('invalid provider response') from None
    if 'provider' not in result:
        raise ValueError('provider must identify itself and its version')
    echoed_body = {'version':'sizing-request-v1', 'purpose':result.get('purpose'), 'model_id':result.get('model_id'), 'model_sha256':result.get('model_sha256'), 'circuit_id':result.get('circuit_id'), 'inputs':result.get('inputs')}
    if result.get('request_digest') != digest or result.get('request_payload') != payload or request_payload(echoed_body) != payload:
        raise ValueError('provider returned a mismatched circuit or request')
    if result['status'] != 'partial' and any((result.get('result') or {}).get(k) is not None for k in ('wire_size', 'conduit_trade_size', 'voltage_drop_percent')):
        raise ValueError('non-partial response cannot include proposed sizes or voltage drop')
    if result['status'] == 'partial' and (not (result.get('result') or {}).get('wire_size') or not (result.get('result') or {}).get('conduit_trade_size')):
        raise ValueError('partial response requires explicit proposed sizes')
    if result['status'] == 'partial':
        outcome = result['result']
        if solver_input['material'] == 'al' and outcome['wire_size'] == '14':
            raise ValueError('provider returned unsupported aluminum conductor size')
        drop = outcome.get('voltage_drop_percent')
        multiplier = math.sqrt(3) if solver_input['phase'] == 3 else 1
        expected_operating = solver_input.get('load_amps') or solver_input['load_va'] / (solver_input['voltage_v'] * multiplier)
        expected_design = expected_operating * (1.25 if solver_input['continuous'] else 1)
        operating = outcome.get('operating_amps')
        design = outcome.get('design_amps')
        if result.get('missing') or outcome.get('violations') or outcome.get('unsupported_reason') is not None or drop is None or drop < 0 or drop > solver_input['vd_limit_pct'] or operating is None or not math.isclose(operating, expected_operating, rel_tol=1e-6, abs_tol=1e-6) or design is None or not math.isclose(design, expected_design, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError('partial response has unresolved violations or voltage drop')
    result.setdefault('limitations', []).append('Proposal only; provider output and input facts have not been independently verified or applied.')
    return result

def size_model_circuit(model, circuit_id, design_inputs, provider):
    """Capture the model, invoke the provider, and map a proposal without applying it."""
    if not isinstance(model, BuildingModel):
        raise TypeError('expected a canonical BuildingModel')
    detached = BuildingModel.from_json(model.to_json(indent=None))
    if not callable(provider):
        raise TypeError('provider must be callable')
    response = _size_circuit(*request_from_model(detached, circuit_id, design_inputs), provider)
    circuit = next(c for c in detached.circuits if c.id == circuit_id)
    existing = [
        {'id':c.id, 'role':c.role, 'material':c.material, 'size':c.size, 'insulation':c.insulation, 'count':c.count, 'route_ids':list(c.route_ids), 'confidence':c.confidence, 'provenance':json.loads(json.dumps([asdict(p) for p in c.provenance], allow_nan=False))}
        for c in detached.conductors if c.circuit_id == circuit_id
    ]
    existing_routes = [{'id':r.id, 'route_type':r.route_type, 'nominal_diameter_m':r.nominal_diameter_m} for r in detached.routes if r.id in circuit.route_ids and isinstance(r, Route)]
    outcome = response.get('result') or {}
    response['target_mapping'] = {
        'circuit_id':circuit_id, 'route_ids':list(circuit.route_ids), 'existing_routes':existing_routes, 'existing_conductors':existing,
        'proposed_wire_size':outcome.get('wire_size'), 'proposed_conduit_trade_size':outcome.get('conduit_trade_size'),
        'proposed_wire_scope':(['line'] + (['neutral'] if response['inputs'].get('neutral') and response['inputs'].get('neutral_same_size_as_phase') else [])) if outcome.get('wire_size') else [],
        'ground_size_supplied':response['inputs'].get('egc_size') if response['inputs'].get('egc') else None, 'ground_size_verified':False,
        'applied':False,
    }
    if not circuit.route_ids:
        response.setdefault('limitations', []).append('No route is mapped; any conduit suggestion is circuit-level only.')
    _RESPONSE.validate(response)
    return response

