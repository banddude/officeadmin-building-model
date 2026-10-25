"""Read-only spacing screen over explicit, model-bound support stations.

No support placement, hardware sizing, load capacity or code approval is inferred.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re

from oabm.model import BuildingModel, ContractError, Point3, Provenance, RuleReference
from oabm.model.common import _validate_id
from oabm.model.supports import component_contains_point
from .panel_schedules import SourceSnapshot, _scope_exists


def _number(value, label, *, zero=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and (value >= 0 if zero else value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ContractError(f"support coverage: invalid {label}")


def _evidence(records, fields, owner):
    if not isinstance(records, tuple) or not records or any(
            not isinstance(p, Provenance) or p.derivation is None for p in records):
        raise ContractError("support coverage requires explicit input provenance")
    if any(path.split('.')[0] not in fields or not _scope_exists(owner, path)
           for p in records for path in (p.scope_paths or ())):
        raise ContractError("support coverage evidence names an unknown input-owner field")


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportSpacingRule:
    route_id: str
    route_type: str
    basis: RuleReference
    max_span_m: float
    max_end_distance_m: float
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        _validate_id(self.route_id, "route_id")
        if not isinstance(self.route_type, str) or not self.route_type.strip() or not isinstance(self.basis, RuleReference):
            raise ContractError("support coverage requires explicit route type and rule reference")
        _number(self.max_span_m, "max_span_m")
        _number(self.max_end_distance_m, "max_end_distance_m", zero=True)
        _evidence(self.provenance, {'route_id', 'route_type', 'basis', 'max_span_m', 'max_end_distance_m'}, self)


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportStation:
    route_id: str
    component_id: str
    distance_m: float
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        _validate_id(self.route_id, "route_id")
        _validate_id(self.component_id, "component_id")
        _number(self.distance_m, "station distance", zero=True)
        _evidence(self.provenance, {'route_id', 'component_id', 'distance_m'}, self)


def support_model_fingerprint(model):
    """Exact canonical serialization binding, not an authenticity signature."""
    if not isinstance(model, BuildingModel):
        raise ContractError("support coverage requires a canonical model")
    return hashlib.sha256(model.to_json().encode()).hexdigest()


def _segments(route):
    segments = []
    total = 0.0
    for a, b in zip(route.centerline.points, route.centerline.points[1:]):
        length = math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        _number(length, "route segment length")
        total += length
        _number(total, "route length")
        segments.append((a, b, length))
    return segments, total


def _point_at(segments, distance):
    remaining = distance
    for index, (a, b, length) in enumerate(segments):
        if remaining <= length or index == len(segments)-1:
            fraction = min(1.0, max(0.0, remaining / length))
            return Point3(x=a.x+(b.x-a.x)*fraction, y=a.y+(b.y-a.y)*fraction, z=a.z+(b.z-a.z)*fraction)
        remaining -= length
    raise ContractError("route has no segments")


def report_support_coverage(model, *, expected_model_sha256, rules, stations):
    """Screen every route without modifying canonical objects or quantities.

    A caller-declared station must lie inside its associated support component's
    catalog envelope. This is only a necessary geometric screen, not proof of
    physical contact, fastening, loading or compliance. Unknown input stays open.
    """
    if not isinstance(model, BuildingModel):
        raise ContractError("support coverage requires a canonical model")
    # Pin the actual caller serialization before the typed decoder normalizes
    # equivalent numeric spellings (for example an integer coordinate to float).
    source_json = model.to_json()
    fingerprint = hashlib.sha256(source_json.encode()).hexdigest()
    snapshot = BuildingModel.from_dict(json.loads(source_json))
    if not isinstance(expected_model_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_model_sha256) or expected_model_sha256 != fingerprint:
        raise ContractError("support coverage input model binding is absent or stale")
    rules, stations = tuple(rules), tuple(stations)
    if any(not isinstance(r, SupportSpacingRule) for r in rules) or any(not isinstance(s, SupportStation) for s in stations):
        raise ContractError("support coverage inputs must be typed")
    routes = {r.id: r for r in snapshot.routes}
    components = {c.id: c for c in snapshot.support_components}
    assemblies = {a.id: a for a in snapshot.support_assemblies}
    if len({r.route_id for r in rules}) != len(rules):
        raise ContractError("duplicate route spacing rule")
    if len({(s.route_id, s.component_id) for s in stations}) != len(stations):
        raise ContractError("one support component cannot supply duplicate stations on a route")
    if any(r.route_id not in routes for r in rules) or any(s.route_id not in routes or s.component_id not in components for s in stations):
        raise ContractError("support coverage input has a missing canonical reference")
    by_rule = {r.route_id: r for r in rules}
    sources = {}

    def retain(collection, entity):
        sources[(collection, entity.id)] = asdict(SourceSnapshot.capture(collection, entity))

    results = []
    for route in sorted(routes.values(), key=lambda r: r.id):
        retain('routes', route)
        segments, length = _segments(route)
        rule = by_rule.get(route.id)
        reasons, accepted, rejected, gaps = set(), [], [], []
        if rule is None:
            reasons.add('spacing_rule_missing')
        elif rule.route_type != route.route_type:
            reasons.add('spacing_rule_route_type_conflict')
        pending = [o for o in snapshot.support_selection_outcomes if o.status == 'unresolved' and route.id in o.supported_object_ids]
        for outcome in pending:
            retain('support_selection_outcomes', outcome)
            reasons.add('support_selection_unresolved')
        for station in sorted((s for s in stations if s.route_id == route.id), key=lambda s: (s.distance_m, s.component_id)):
            if station.distance_m > length + 1e-9:
                raise ContractError("support station lies beyond its route")
            component = components[station.component_id]
            assembly = assemblies[component.assembly_id]
            retain('support_components', component)
            retain('support_assemblies', assembly)
            for interface in snapshot.support_interfaces:
                if interface.assembly_id == assembly.id:
                    retain('support_interfaces', interface)
            point = _point_at(segments, station.distance_m)
            reason = None
            if component.category != 'support':
                reason = 'fastener_is_not_support_station'
            elif route.id not in assembly.supported_object_ids:
                reason = 'component_assembly_does_not_support_route'
            elif not component_contains_point(component, point):
                reason = 'station_outside_support_envelope'
            if reason:
                reasons.add(reason)
                rejected.append({'station': asdict(station), 'reason': reason})
            else:
                accepted.append({'station': asdict(station), 'point': asdict(point)})
        positions = sorted({row['station']['distance_m'] for row in accepted})
        if not positions:
            reasons.add('support_stations_missing')
        if rule is not None and rule.route_type == route.route_type and positions:
            intervals = [(0.0, positions[0], rule.max_end_distance_m, 'start_distance')]
            intervals.extend((a, b, rule.max_span_m, 'between_supports') for a, b in zip(positions, positions[1:]))
            intervals.append((positions[-1], length, rule.max_end_distance_m, 'end_distance'))
            for start, end, limit, kind in intervals:
                if end-start > limit + 1e-9:
                    gaps.append({'from_m': start, 'to_m': end, 'length_m': end-start, 'limit_m': limit, 'kind': kind})
        status = 'gaps_found' if gaps else 'unresolved' if reasons else 'within_supplied_limits'
        results.append({'route_id': route.id, 'route_length_m': length, 'status': status,
                        'rule': asdict(rule) if rule is not None else None,
                        'accepted_stations': accepted, 'rejected_stations': rejected,
                        'reason_codes': sorted(reasons), 'gaps': gaps,
                        'unlocated_model_span_m': length if not positions else None})
    status = 'gaps_found' if any(r['gaps'] for r in results) else 'unresolved' if not results or any(r['status'] == 'unresolved' for r in results) else 'within_supplied_limits'
    report = {'version': 'support-coverage-v1', 'model_sha256': fingerprint, 'status': status,
              'routes': results, 'source_snapshots': [sources[k] for k in sorted(sources)],
              'geometry_screen': 'declared_centerline_station_inside_catalog_envelope',
              'physical_contact_verified': False, 'attachment_capacity_verified': False,
              'code_compliance_verified': False, 'installation_completeness_verified': False}
    return json.loads(json.dumps(report, sort_keys=True, allow_nan=False))
