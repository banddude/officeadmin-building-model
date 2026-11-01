"""Opt-in, source-bound physical route revision evidence; no whole-model takeoff."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path

from oabm.model import BuildingModel, ContractError
from oabm.reports.physical_routes import _snapshot, _json, _length, _text_cell, generate_physical_route_schedule
from oabm.routing.physical_capture import bounded_source_json, SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop

KIND = 'physical_route_revision_comparison'


def _coefficient(length):
    value = length['pi_coefficient_ticks']
    return Fraction(value['numerator'], value['denominator'])


def _measure(rows):
    return {'length': _length(sum(row['length']['straight_ticks'] for row in rows),
                               sum((_coefficient(row['length']) for row in rows), Fraction())),
            'quarter_bend_count': sum(row['quarter_bend_count'] for row in rows)}


def _delta(before, after):
    return {'length': _length(after['length']['straight_ticks'] - before['length']['straight_ticks'],
                              _coefficient(after['length']) - _coefficient(before['length'])),
            'quarter_bend_count': after['quarter_bend_count'] - before['quarter_bend_count']}


def _path(route):
    """Traversal geometry without mutable owner/child IDs or evidence fields."""
    joints = {joint['id']: joint['position'] for joint in route['joints']}
    return [{'kind': segment['kind'], 'start': joints[segment['start_joint_id']],
             'end': joints[segment['end_joint_id']],
             **{key: segment[key] for key in ('plane_normal', 'turn') if key in segment}}
            for segment in route['segments']]


def _category(path):
    # Attribute dictionaries are opaque user metadata. Their keys do not acquire
    # canonical meaning when named confidence, provenance, id or route_id.
    if 'attributes' in path:
        return 'metadata'
    if 'provenance' in path or 'confidence' in path:
        return 'provenance'
    if path[-1] == 'id' or (path[0] in ('joints', 'segments', 'requirements') and
                            path[-1] in ('route_id', 'start_joint_id', 'end_joint_id')):
        return 'identity'
    if path[0] == 'context':
        return 'context'
    if path[0] in ('start_port_id', 'end_port_id', 'circuit_ids', 'level_id'):
        return 'connectivity'
    if path[:2] == ('requirements', 'outside_diameter_m'):
        return 'outside_diameter'
    if path[0] == 'requirements':
        return 'metadata' if path[1:2] == ('name',) else 'requirements'
    if path[0] in ('joints', 'segments'):
        if len(path) <= 2:
            return 'path_geometry'  # added/removed ordered geometry record
        geometry_fields = {'position'} if path[0] == 'joints' else {'kind', 'plane_normal', 'turn'}
        return 'path_geometry' if path[2] in geometry_fields else 'metadata'
    return 'metadata'


def _canonical_equal(before, after):
    # Python container equality conflates false/0 and true/1 recursively.
    # Canonical JSON preserves those source types, including inside metadata.
    # Spatial path/OD comparison intentionally retains numeric equality below.
    return _json(before) == _json(after)


def _changes(before, after, path=()):
    if _canonical_equal(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            if key in before and key in after:
                result.extend(_changes(before[key], after[key], (*path, key)))
            else:
                subpath = (*path, key)
                result.append({'path': list(subpath), 'category': _category(subpath),
                               'before_present': key in before, 'after_present': key in after,
                               'before': before.get(key), 'after': after.get(key)})
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [row for index, (a, b) in enumerate(zip(before, after))
                for row in _changes(a, b, (*path, str(index)))]
    return [{'path': list(path), 'category': _category(path),
             'before_present': True, 'after_present': True, 'before': before, 'after': after}]


@dataclass(frozen=True)
class PhysicalRouteRevisionComparison:
    _report_json: str

    def to_dict(self):
        return json.loads(self._report_json)

    def to_json(self):
        return self._report_json

    def validate_against_models(self, before, after):
        if generate_physical_route_comparison(before, after).to_json() != self.to_json():
            raise ContractError('physical route comparison is stale or changed')

    def routes_csv(self):
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(('route_id', 'status', 'before_kind', 'after_kind', 'before_name', 'after_name', 'path_changed',
                         'outside_diameter_changed', 'field_categories', 'delta_straight_ticks',
                         'delta_pi_ticks_numerator', 'delta_pi_ticks_denominator',
                         'delta_total_m_approx', 'delta_quarter_bend_count',
                         'before_source_sha256', 'after_source_sha256'))
        document = self.to_dict()
        for row in document['routes']:
            delta = row['delta']
            length = delta['length'] if delta else None
            writer.writerow((*(_text_cell(row[key]) for key in ('route_id', 'status', 'before_kind', 'after_kind', 'before_name', 'after_name')),
                             row['path_changed'], row['outside_diameter_changed'],
                             _text_cell(','.join(row['field_change_categories'])),
                             length['straight_ticks'] if length else '',
                             length['pi_coefficient_ticks']['numerator'] if length else '',
                             length['pi_coefficient_ticks']['denominator'] if length else '',
                             length['total_m_approx'] if length else '',
                             delta['quarter_bend_count'] if delta else '',
                             document['sources']['before']['sha256'], document['sources']['after']['sha256']))
        return output.getvalue()


def generate_physical_route_comparison(before, after):
    before, before_json = _snapshot(before)
    after, after_json = _snapshot(after)
    a, b = before.to_dict(), after.to_dict()
    for key in ('model_id', 'schema_version', 'coordinate_system'):
        if a[key] != b[key]:
            raise ContractError(f'physical route comparison requires matching complete {key}')
    schedules = [generate_physical_route_schedule(model).to_dict() for model in (before, after)]
    measures = [{row['route_id']: row for row in schedule['routes']} for schedule in schedules]
    sources = [{row['id']: row for row in source['routes']} for source in (a, b)]
    records, additions, removals = [], [], []
    def kind(route):
        return None if route is None else ('physical' if route.get('geometry_kind') == 'line_arc_v1' else 'legacy')
    for route_id in sorted(sources[0].keys() | sources[1].keys()):
        old, new = (source.get(route_id) for source in sources)
        old_kind, new_kind = kind(old), kind(new)
        old_measure, new_measure = (measure.get(route_id) for measure in measures)
        status = ('added' if old is None else 'removed' if new is None else
                  'unchanged' if _canonical_equal(old, new) else 'changed')
        comparable = old_kind == new_kind == 'physical'
        path_changed = _path(old) != _path(new) if comparable else None
        od_changed = old['requirements']['outside_diameter_m'] != new['requirements']['outside_diameter_m'] if comparable else None
        changes = _changes(old, new) if old is not None and new is not None else []
        # Gross replacement only when physical scope actually changes. Metadata,
        # child identity and request changes do not count the same conduit again.
        replaced = comparable and (path_changed or od_changed)
        if old_measure and (new_kind != 'physical' or replaced):
            removals.append(old_measure)
        if new_measure and (old_kind != 'physical' or replaced):
            additions.append(new_measure)
        empty = _measure([])
        delta = (_delta(old_measure or empty, new_measure or empty)
                 if (comparable or (old is None and new_measure is not None) or
                     (new is None and old_measure is not None)) else None)
        records.append({'route_id': route_id, 'status': status,
                        'before_kind': old_kind, 'after_kind': new_kind,
                        'before_name': old.get('name') if old else None,
                        'after_name': new.get('name') if new else None,
                        'physical_comparison_available': comparable,
                        'path_changed': path_changed, 'outside_diameter_changed': od_changed,
                        'field_changes': changes,
                        'field_change_categories': sorted({row['category'] for row in changes}),
                        'before_measurement': old_measure, 'after_measurement': new_measure,
                        'delta': delta,
                        'legacy_excluded': old_kind == 'legacy' or new_kind == 'legacy'})
    gross_added, gross_removed = _measure(additions), _measure(removals)
    document = {'kind': KIND, 'version': '1.0.0',
        'sources': {label: {'binding_profile': 'normalized-canonical-model-json-v1',
                            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'snapshot': snapshot}
                    for label, text, snapshot in (('before', before_json, a), ('after', after_json, b))},
        'scope': {'matching': 'stable canonical route ID only; no renamed-ID inference',
                  'measured': 'physical-route geometric inventory only',
                  'complete_takeoff': False, 'other_entities_compared': False,
                  'legacy_excluded': True},
        'classification_basis': 'Exact ordered segment kinds, positions, arc normal/turn define path; IDs and provenance do not. Supplied OD defines physical diameter. Field changes retain requirements, identity, connectivity, provenance, context and metadata separately.',
        'routes': records,
        'counts': {status: sum(row['status'] == status for row in records)
                   for status in ('added', 'removed', 'changed', 'unchanged')},
        'physical_inventory': {'gross_added': gross_added, 'gross_removed': gross_removed,
                               'net': _delta(gross_removed, gross_added),
                               'gross_added_route_ids': [row['route_id'] for row in additions],
                               'gross_removed_route_ids': [row['route_id'] for row in removals]},
        'limitations': ['Not a whole-model takeoff, pricing, change order or clearance verification.',
                       'Absent or excluded routes establish no completed takeoff scope; cross-kind replacement has no comparable route delta.',
                       'Changed context or obstacles does not establish a changed physical path or current route validity.',
                       'Gross additions/removals replace complete changed physical routes; they are not reusable segment or stock quantities.',
                       'Both normalized snapshots are retained; source authenticity and engineering approval are not established.']}
    text = _json(document)
    try:
        bounded_source_json(text)
    except VerificationStop as exc:
        raise ContractError(f'physical route comparison output {exc.status}: {exc.reason}') from exc
    return PhysicalRouteRevisionComparison(text)


class _TokenFloat(float):
    def __new__(cls, token):
        value = super().__new__(cls, token)
        value.token = token
        return value


def _token_json(value):
    # Preserve original source numeric tokens for canonical physical admission.
    # Ordinary decoding would round invalid nanometre coordinates first.
    if isinstance(value, _TokenFloat):
        return value.token
    if isinstance(value, dict):
        return '{' + ','.join(_json(key) + ':' + _token_json(item) for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_token_json(item) for item in value) + ']'
    return _json(value)


def validate_physical_route_comparison(payload):
    """Regenerate from both retained snapshots, checking all fields, not just hashes."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError('duplicate comparison JSON key')
            result[key] = value
        return result
    try:
        actual = json.loads(bounded_source_json(payload), object_pairs_hook=unique, parse_float=_TokenFloat)
        expected = generate_physical_route_comparison(
            BuildingModel.from_json(_token_json(actual['sources']['before']['snapshot'])),
            BuildingModel.from_json(_token_json(actual['sources']['after']['snapshot'])))
        if _json(actual) != expected.to_json():
            raise ContractError('physical route comparison is stale or changed')
        return expected
    except (VerificationStop, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ContractError('invalid physical route comparison JSON') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description='Compare canonical physical route snapshots without whole-model quantities.')
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('output', type=Path, help='Fresh output directory')
    args = parser.parse_args(argv)
    try:
        models = []
        for path in (args.before, args.after):
            with path.open('rb') as handle:
                models.append(BuildingModel.from_json(bounded_source_json(handle.read(SOURCE_BYTES + 1))))
        report = generate_physical_route_comparison(*models)
        data = report.to_dict()
        contents = {'comparison.json': report.to_json(), 'routes.csv': report.routes_csv(),
                    **{f'{label}.json': _json(data['sources'][label]['snapshot']) for label in ('before', 'after')}}
        prepared = {name: value.encode('utf-8') for name, value in contents.items()}
        args.output.mkdir(exist_ok=False)
        for name, value in prepared.items():
            (args.output / name).write_bytes(value)
        print(_json({'output_directory': str(args.output), 'counts': data['counts']}))
        return 0
    except (ContractError, VerificationStop, OSError, ValueError) as exc:
        parser.exit(2, f'physical route comparison: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
