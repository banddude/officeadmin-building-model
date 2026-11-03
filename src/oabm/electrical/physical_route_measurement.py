"""Explicit geometric measurements only; no routing or electrical certification."""
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import math

from oabm.model import BuildingModel, PhysicalRoute, physical_segment_facts
from oabm.model.physical_routes import point_ticks
from oabm.electrical.sizing_provider import _portable_json
from oabm.routing.physical_capture import capture, bounded_source_json, SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop

# A strict upper bound on pi, not a floating approximation. All route coordinates
# use nanometre ticks; one international foot is exactly 304,800,000 such ticks.
_PI_UPPER = Fraction(314159265358979323846264338328, 10**29)
_TICKS_PER_FOOT = 304_800_000
_MICROFEET = 1_000_000


def _capture_physical_measurement(model, circuit_id):
    """Measure one stored source-to-load route without asserting it is safe.

    The returned feet value rounds the geometric upper bound upward to a
    microfoot, then one floating ULP upward. It excludes leads, slack and all
    installation allowances. No provider is invoked and no model is changed.
    """
    if not isinstance(model, BuildingModel):
        raise TypeError('expected a canonical BuildingModel')
    try:
        captured = capture(model, byte_limit=SOURCE_BYTES)
        snapshot = bounded_source_json(captured.to_json(indent=None))
        detached = BuildingModel.from_json(snapshot)
        # Normalize typed model fields before hashing. Constructor-created
        # integer coordinates and parsed floats must have the same identity.
        snapshot = bounded_source_json(detached.to_json(indent=None))
        if BuildingModel.from_json(snapshot).to_json(indent=None) != snapshot:
            raise ValueError('normalized model snapshot is not stable')
    except VerificationStop as exc:
        raise ValueError('invalid or oversized model snapshot') from exc
    circuits = [c for c in detached.circuits if c.id == circuit_id]
    if len(circuits) != 1 or len(circuits[0].load_port_ids) != 1 or len(circuits[0].route_ids) != 1:
        raise ValueError('a single circuit, load and route are required')
    circuit = circuits[0]
    routes = [r for r in detached.routes if r.id == circuit.route_ids[0]]
    if len(routes) != 1 or not isinstance(routes[0], PhysicalRoute):
        raise ValueError('a single stored physical route is required')
    route = routes[0]
    if route.start_port_id != circuit.source_port_id or route.end_port_id != circuit.load_port_ids[0]:
        raise ValueError('route does not explicitly connect this source and load')
    facts = physical_segment_facts(route)
    straight = sum(row.get('length_ticks', 0) for row in facts)
    radius_sum = sum(row.get('radius_ticks', 0) for row in facts)
    upper_ticks = Fraction(straight) + Fraction(radius_sum, 2) * _PI_UPPER
    if upper_ticks <= 0:
        raise ValueError('route length unavailable')
    rounded_microfeet = math.ceil(upper_ticks * _MICROFEET / _TICKS_PER_FOOT)
    feet = math.nextafter(rounded_microfeet / _MICROFEET, math.inf)
    geometry = []
    for segment, fact in zip(route.segments, facts):
        geometry.append({'segment_id':segment.id,'kind':segment.kind,
                         'start_joint_id':segment.start_joint_id,'end_joint_id':segment.end_joint_id,
                         'length_ticks':fact.get('length_ticks'), 'radius_ticks':fact.get('radius_ticks'),
                         'confidence':segment.confidence,'provenance':[asdict(p) for p in segment.provenance],
                         'plane_normal':asdict(segment.plane_normal) if segment.kind=='quarter_arc' else None,
                         'turn':segment.turn if segment.kind=='quarter_arc' else None})
    result = {
        'version':'physical-route-measurement-v1','purpose':'geometric_measurement_only',
        'model_id':detached.model_id,'model_sha256':hashlib.sha256(snapshot.encode('utf-8')).hexdigest(),
        'model_hash_scope':'normalized_typed_model_json_utf8_v1',
        'circuit_id':circuit.id,'route_id':route.id,'route_type':route.route_type,
        'frame_id':route.context.coordinate_frame_id,'start_port_id':route.start_port_id,'end_port_id':route.end_port_id,
        'declared_source_model_sha256':route.context.source_model_sha256,
        'declared_source_model_schema_version':route.context.source_model_schema_version,
        'declared_request_sha256':route.context.request_sha256,
        'tick_unit':'nanometre','straight_ticks':straight,'quarter_arc_radius_ticks_sum':radius_sum,
        'rounding_profile':{'version':'pi-upper-microfoot-nextafter-v1',
                            'pi_upper_decimal':'3.14159265358979323846264338328',
                            'ticks_per_foot':_TICKS_PER_FOOT,'microfeet_per_foot':_MICROFEET},
        'one_way_centerline_feet_upper':feet,'geometry':geometry,
        'joints':[{'id':j.id,'position':asdict(j.position),'position_ticks':list(point_ticks(j.position)),'confidence':j.confidence,'provenance':[asdict(p) for p in j.provenance]} for j in route.joints],
        'route_confidence':route.confidence,'route_provenance':[asdict(p) for p in route.provenance],
        'requirements_id':route.requirements.id,
        'requirements_profile':route.requirements.profile,
        'centerline_radius_m':route.requirements.centerline_radius_m,
        'requirements_confidence':route.requirements.confidence,
        'requirements_provenance':[asdict(p) for p in route.requirements.provenance],
        'physical_outside_diameter_m':route.requirements.outside_diameter_m,
        'source_context_status':'unverified','clearance_status':'unverified','applied':False,
        'limitations':['One-way geometric centerline only; excludes leads, slack and installation allowances.',
                       'Source context and obstacle clearance have not been verified.',
                       'Outside diameter is not a nominal trade size; fit and code compliance are not established.'],
    }
    # Enforce JSON portability for evidence; this is correlation, not authentication.
    portable = _portable_json(result)
    if len(json.dumps(portable, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('measurement evidence exceeds resource limits')
    return detached, portable


def measure_single_physical_route(model, circuit_id):
    """Return a non-certifying measurement from a bounded detached snapshot."""
    _, measurement = _capture_physical_measurement(model, circuit_id)
    return measurement


def main(argv=None):
    """Write a local geometric measurement JSON; never certify or apply it."""
    import argparse
    import os
    import stat
    import sys
    parser = argparse.ArgumentParser(description='Measure one stored physical route; no clearance or electrical certification.')
    parser.add_argument('--model', required=True, help='Canonical model JSON file')
    parser.add_argument('--circuit', required=True, help='Circuit ID')
    args = parser.parse_args(argv)
    try:
        fd = os.open(args.model, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('model input must be a regular file')
            raw = stream.read(SOURCE_BYTES + 1)
        source = bounded_source_json(raw)
        model = BuildingModel.from_json(source)
        result = measure_single_physical_route(model, args.circuit)
    except (OSError, ValueError, TypeError, VerificationStop) as exc:
        print('measurement failed: invalid, unsupported or oversized input', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
