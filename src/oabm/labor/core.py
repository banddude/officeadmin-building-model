"""Detached, finite labor request evaluation. No company defaults or OA access.

Fractions preserve supplied decimal scope and effort; money rounds once per
priced line to integer cents. Occupancy is explicit, never inferred from hours.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction

from oabm.model import ContractError
from oabm.quantities import extract_quantities
from oabm.reports.physical_routes import _snapshot, _text_cell

MAX_BYTES = 2_000_000
MAX_NODES = 40_000


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _detach(value):
    # Bound structure before serialization, rejecting cycles and custom objects.
    budget = [MAX_NODES]
    bytes_left = [MAX_BYTES]
    active = set()
    def visit(node, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 24:
            raise ContractError('labor request exceeds structural limit')
        if type(node) in (dict, list):
            if id(node) in active:
                raise ContractError('cyclic labor request')
            active.add(id(node))
            if type(node) is dict:
                if any(type(k) is not str for k in node):
                    raise ContractError('labor keys must be strings')
                bytes_left[0] -= sum(len(k) * 12 + 3 for k in node)
                if bytes_left[0] < 0:
                    raise ContractError('labor keys exceed byte limit')
                result = {k: visit(v, depth + 1) for k, v in node.items()}
            else:
                result = [visit(v, depth + 1) for v in node]
            active.remove(id(node))
            return result
        if node is None or type(node) in (str, bool, int, float):
            if type(node) is int and node.bit_length() > 256:
                raise ContractError('labor integer exceeds limit')
            bytes_left[0] -= len(node) * 12 + 2 if type(node) is str else 80
            if bytes_left[0] < 0:
                raise ContractError('labor text exceeds limit')
            return node
        raise ContractError('labor request must contain plain JSON values')
    try:
        text = _json(visit(value))
        if len(text.encode()) > MAX_BYTES:
            raise ContractError('labor request exceeds byte limit')
        return json.loads(text)
    except ContractError:
        raise
    except (ValueError, OverflowError) as exc:
        raise ContractError('invalid finite labor JSON') from exc


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ContractError('expected fields: ' + names)


def _text(value):
    if type(value) is not str or not value.strip():
        raise ContractError('expected nonempty text')
    return value


def _number(value, *, zero=False):
    if type(value) not in (str, int, float):
        raise ContractError('expected finite decimal')
    text = str(value)
    if len(text) > 128 or not re.fullmatch(r'[+]?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]{1,3})?', text):
        raise ContractError('expected bounded finite decimal')
    if 'e' in text.lower() and abs(int(text.lower().split('e')[1])) > 100:
        raise ContractError('decimal exponent exceeds limit')
    try:
        result = Fraction(text)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ContractError('expected finite decimal') from exc
    if result < 0 or (not zero and result == 0):
        raise ContractError('expected positive decimal')
    return result


def _exact(value):
    return str(value.numerator) if value.denominator == 1 else f'{value.numerator}/{value.denominator}'


def _index(rows):
    if type(rows) is not list:
        raise ContractError('expected list')
    result = {}
    for row in rows:
        if type(row) is not dict:
            raise ContractError('expected object')
        key = _text(row.get('id'))
        if key in result:
            raise ContractError('duplicate id: ' + key)
        result[key] = row
    return result


def _instant(value):
    try:
        value = datetime.fromisoformat(_text(value))
    except ValueError as exc:
        raise ContractError('expected ISO timestamp with UTC offset') from exc
    if value.utcoffset() is None:
        raise ContractError('work windows require explicit UTC offsets')
    return value.astimezone(timezone.utc)


def _period(row):
    _keys(row, 'start end')
    start, end = _instant(row['start']), _instant(row['end'])
    if end <= start:
        raise ContractError('period end must follow start')
    return start, end


def _hours(period):
    delta = period[1] - period[0]
    return Fraction(delta.days * 86400 + delta.seconds, 3600) + Fraction(delta.microseconds, 3_600_000_000)


def _cents(amount):
    # Nonnegative half-up rounding, once per line.
    return (amount.numerator * 2 + amount.denominator) // (2 * amount.denominator)


def _inventory(model, groups):
    detached, source = _snapshot(model)
    rows = []
    def retain(item):
        basis = item.to_dict()
        rows.append({'id': _hash(basis), 'basis': basis})
        return True
    takeoff = extract_quantities(detached, groups=groups, contribution_filter=retain)
    if len({r['id'] for r in rows}) != len(rows):
        raise ContractError('indistinguishable original contributions require quantity-lane identity')
    return source, sorted(rows, key=lambda r: r['id']), [w.to_dict() for w in takeoff.warnings]


def contribution_inventory(model, *, groups=None):
    """Discover exact request selectors; these are original, unmerged scalars."""
    source, rows, warnings = _inventory(model, _detach(groups) if groups is not None else None)
    return {'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'contributions': rows, 'warnings': warnings}


@dataclass(frozen=True)
class LaborReport:
    source_model_json: str
    request_json: str
    report_json: str

    def to_dict(self):
        return json.loads(self.report_json)

    def to_json(self):
        return self.report_json

    def to_csv(self):
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(('kind', 'id', 'status', 'base_person_hours', 'adjusted_person_hours',
                         'occupancy_hours', 'currency', 'cents', 'unresolved', 'formula', 'source_sha256',
                         'contribution_id', 'operation_id', 'source_entity_ids', 'unit', 'group',
                         'assembly_key', 'variant', 'phase', 'mobilization', 'evidence', 'context', 'rate_receipts'))
        doc = self.to_dict()
        for kind, rows in (('work', doc['work']), ('rental', doc['rentals'])):
            for row in rows:
                basis = row.get('basis', {})
                context = row.get('occurrence', row)
                writer.writerow([_text_cell(v) for v in (kind, row['id'], row['status'],
                    row.get('base_person_hours'), row.get('adjusted_person_hours'), row.get('occupancy_hours'),
                    row.get('currency'), row.get('cents'), _json(row['unresolved']), _json(row['formula']),
                    doc['source_sha256'], row.get('contribution_id'), row.get('operation', {}).get('id'),
                    _json(basis.get('source_entity_ids', [])), basis.get('unit'), basis.get('group'),
                    basis.get('assembly_key'), _json(basis.get('variant', {})), row.get('phase'),
                    context.get('mobilization'), context.get('evidence'), _json(context), _json(doc['rates']))])
        return output.getvalue()

    def validate_against(self, model, request):
        expected = generate_labor_report(model, request)
        if self != expected:
            raise ContractError('labor report is stale or changed')


def generate_labor_report(model, request):
    """Evaluate an explicitly selected scope, never a whole-job completeness claim."""
    req = _detach(request)
    _keys(req, 'version source_sha256 groups policy operations rules rates work rentals')
    if req['version'] != 'labor-request-v1':
        raise ContractError('unsupported labor request')
    _keys(req['policy'], 'id revision source')
    for value in req['policy'].values():
        _text(value)
    source, inventory, warnings = _inventory(model, req['groups'])
    digest = hashlib.sha256(source.encode()).hexdigest()
    if req['source_sha256'] != digest:
        raise ContractError('stale labor source binding')
    contributions = {r['id']: r['basis'] for r in inventory}
    operations, rules, rates, work, rentals = (_index(req[k]) for k in ('operations', 'rules', 'rates', 'work', 'rentals'))
    for op in operations.values():
        _keys(op, 'id category item_type unit exclusive_family person_hours_per_unit')
        for key in ('id', 'category', 'item_type', 'unit', 'exclusive_family'):
            _text(op[key])
        if op['person_hours_per_unit'] is not None:
            _number(op['person_hours_per_unit'])
    # One rule per known condition. Multipliers combine by multiplication, once.
    conditions = {'occupied', 'hillside', 'no-truck', 'height'}
    rule_keys = set()
    for rule in rules.values():
        _keys(rule, 'id condition value multiplier resource_kind height_min_m height_max_m')
        if rule['condition'] not in conditions:
            raise ContractError('unknown condition rule')
        _text(rule['value'])
        key = (rule['condition'], rule['value'])
        if key in rule_keys:
            raise ContractError('ambiguous duplicated restriction: ' + rule['condition'])
        rule_keys.add(key)
        _number(rule['multiplier'])
        if rule['resource_kind'] is not None:
            _text(rule['resource_kind'])
        if rule['condition'] == 'height':
            lower = _number(rule['height_min_m'], zero=True)
            upper = _number(rule['height_max_m'])
            if lower >= upper:
                raise ContractError('height policy requires ordered relative band limits')
        elif rule['height_min_m'] is not None or rule['height_max_m'] is not None:
            raise ContractError('only height rules may specify height bands')
    for rate in rates.values():
        _keys(rate, 'id kind unit currency revision source cents selection role quantum_hours')
        if rate['kind'] not in ('labor', 'rental') or type(rate['cents']) is not int or rate['cents'] < 0:
            raise ContractError('invalid external rate')
        for key in ('id', 'unit', 'currency', 'revision', 'source', 'selection', 'role'):
            _text(rate[key])
        if rate['kind'] == 'labor' and rate['quantum_hours'] is not None:
            raise ContractError('labor rates cannot carry rental quantum')
        if rate['kind'] == 'rental':
            _number(rate['quantum_hours'])
    lines, allocations, exclusive, schedules = [], {}, {}, {}
    for identity, row in sorted(work.items()):
        _keys(row, 'id contribution_id operation_id fraction phase mobilization evidence conditions height_basis crew windows')
        _text(row['contribution_id']); _text(row['operation_id'])
        basis = contributions.get(row['contribution_id'])
        op = operations.get(row['operation_id'])
        if basis is None or op is None:
            raise ContractError('unknown contribution or operation')
        for key in ('phase', 'mobilization', 'evidence'):
            _text(row[key])
        if (op['category'], op['item_type'], op['unit']) != (basis['category'], basis['item_type'], basis['unit']):
            raise ContractError('ineligible measurement basis or incompatible unit')
        fraction = _number(row['fraction'])
        pair = (row['contribution_id'], op['id'])
        allocations[pair] = allocations.get(pair, Fraction(0)) + fraction
        if allocations[pair] > 1:
            raise ContractError('operation scope is allocated more than once')
        # Alternative measurement bases for one source cannot charge the same
        # installation. This deliberately refuses mixed-basis partial scopes.
        # A conductor-over-route contribution owns the PAIR, not each ID's
        # membership separately. Different conductors sharing a route and one
        # conductor traversing multiple routes are independent measured scope.
        key = (tuple(basis['source_entity_ids']), basis.get('group'), op['exclusive_family'])
        chosen = (row['contribution_id'], op['id'])
        if key in exclusive and exclusive[key] != chosen:
            raise ContractError('exclusive installation bases overlap')
        exclusive[key] = chosen
        unknown, formula, required = [], [], set()
        _keys(row['conditions'], 'occupied hillside no-truck height')
        if row['height_basis'] is None:
            unknown.append('height relative to work surface is unknown')
        else:
            _keys(row['height_basis'], 'work_surface evidence method min_height_m max_height_m')
            _text(row['height_basis']['work_surface'])
            _text(row['height_basis']['evidence'])
            if row['height_basis']['method'] not in ('measured-split', 'conservative-whole-operation'):
                raise ContractError('height bands require measured split or conservative whole-operation policy')
            if _number(row['height_basis']['min_height_m'], zero=True) > _number(row['height_basis']['max_height_m'], zero=True):
                raise ContractError('relative install heights must be ordered')
        multiplier = Fraction(1)
        for condition, value in sorted(row['conditions'].items()):
            if value is None:
                unknown.append('unknown condition: ' + condition)
                continue
            _text(value)
            matches = [r for r in rules.values() if (r['condition'], r['value']) == (condition, value)]
            if len(matches) > 1:
                raise ContractError('ambiguous duplicated restriction: ' + condition)
            if not matches:
                unknown.append('missing policy rule: ' + condition + '=' + value)
                continue
            rule = matches[0]
            if condition == 'height' and row['height_basis'] is not None:
                height = row['height_basis']
                low = _number(height['min_height_m'], zero=True)
                high = _number(height['max_height_m'], zero=True)
                floor = _number(rule['height_min_m'], zero=True)
                ceiling = _number(rule['height_max_m'])
                if high > ceiling or high < floor or (height['method'] == 'measured-split' and low < floor):
                    raise ContractError('height band crossing requires measured splits or declared conservative whole-operation policy')
            multiplier *= _number(rule['multiplier'])
            formula.append({'rule': rule['id'], 'multiplier': rule['multiplier']})
            if rule['resource_kind']:
                required.add(rule['resource_kind'])
        if type(row['windows']) is not list:
            raise ContractError('work windows must be a list')
        periods = sorted(_period(w) for w in row['windows'])
        if any(a[1] > b[0] for a, b in zip(periods, periods[1:])):
            raise ContractError('work windows overlap')
        if not periods:
            unknown.append('work windows missing')
        schedules[identity] = (periods, required)
        crew = _index(row['crew'])
        headcount, crew_price, currencies = 0, Fraction(0), set()
        for member in crew.values():
            _keys(member, 'id role count selection rate_id')
            if type(member['count']) is not int or member['count'] <= 0:
                raise ContractError('crew count must be positive integer')
            _text(member['role']); _text(member['selection'])
            if member['rate_id'] is not None:
                _text(member['rate_id'])
            headcount += member['count']
            rate = rates.get(member['rate_id'])
            if rate is None:
                unknown.append('missing labor rate: ' + member['id'])
            elif (rate['kind'], rate['unit'], rate['selection'], rate['role']) != ('labor', 'person-hour', member['selection'], member['role']):
                raise ContractError('incompatible labor rate selection')
            else:
                currencies.add(rate['currency'])
                crew_price += member['count'] * rate['cents']
        if len(currencies) > 1:
            raise ContractError('mixed crew currencies')
        if not headcount:
            unknown.append('crew missing')
        quantity = _number(basis['quantity'], zero=True) * fraction
        base = None if op['person_hours_per_unit'] is None else quantity * _number(op['person_hours_per_unit'])
        if base is None:
            unknown.append('productivity standard missing')
        adjusted = None if base is None or any('condition' in u or 'policy rule' in u or 'height relative' in u for u in unknown) else base * multiplier
        capacity = sum((_hours(p) for p in periods), Fraction(0))
        if adjusted is not None and headcount and adjusted > capacity * headcount:
            unknown.append('supplied windows have insufficient crew capacity')
        formula.insert(0, {'quantity': _exact(quantity), 'fraction': _exact(fraction),
                          'person_hours_per_unit': op['person_hours_per_unit'], 'combined_multiplier': _exact(multiplier)})
        price = None if unknown or adjusted is None else _cents(adjusted / headcount * crew_price)
        lines.append({'id': identity, 'contribution_id': row['contribution_id'], 'basis': basis,
            'operation': op, 'conditions': row['conditions'], 'height_basis': row['height_basis'],
            'phase': row['phase'], 'mobilization': row['mobilization'], 'evidence': row['evidence'],
            'crew': row['crew'], 'windows': row['windows'], 'base_person_hours': None if base is None else _exact(base),
            'adjusted_person_hours': None if adjusted is None else _exact(adjusted),
            'occupancy_hours': _exact(capacity), 'currency': next(iter(currencies), None),
            'cents': price, 'unresolved': unknown, 'formula': formula})
    rental_lines, coverage = [], set()
    for identity, row in sorted(rentals.items()):
        _keys(row, 'id kind mobilization evidence capacity assignments hire off_hire rate_id billing_quantum_hours')
        for key in ('kind', 'mobilization', 'evidence'):
            _text(row[key])
        if type(row['capacity']) is not int or row['capacity'] <= 0:
            raise ContractError('rental capacity must be positive integer')
        hire = _period({'start': row['hire'], 'end': row['off_hire']})
        if type(row['assignments']) is not list or not row['assignments']:
            raise ContractError('rental requires explicit work assignments')
        for assignment in row['assignments']:
            _text(assignment)
        if len(set(row['assignments'])) != len(row['assignments']):
            raise ContractError('duplicate rental assignment')
        events = []
        for work_id in row['assignments']:
            if work_id not in work:
                raise ContractError('unknown rental work assignment')
            if work[work_id]['mobilization'] != row['mobilization']:
                raise ContractError('rental occurrence cannot merge separate mobilizations')
            key = (work_id, row['kind'])
            if key in coverage:
                raise ContractError('ambiguous duplicated resource ownership')
            coverage.add(key)
            periods, _ = schedules[work_id]
            for start, end in periods:
                if start < hire[0] or end > hire[1]:
                    raise ContractError('resource hire period does not cover assigned work')
                events.extend(((start, 1), (end, -1)))
        concurrent, peak = 0, 0
        for _, delta in sorted(events):  # releases precede claims at same instant
            concurrent += delta
            peak = max(peak, concurrent)
        if peak > row['capacity']:
            raise ContractError('shared rental capacity exceeded')
        quantum = _number(row['billing_quantum_hours'])
        duration = _hours(hire)
        ratio = duration / quantum
        units = -(-ratio.numerator // ratio.denominator)
        if row['rate_id'] is not None:
            _text(row['rate_id'])
        rate = rates.get(row['rate_id'])
        unknown = []
        if rate is None:
            unknown.append('rental rate missing')
        elif (rate['kind'], rate['unit'], rate['role']) != ('rental', 'hire-quantum', row['kind']):
            raise ContractError('incompatible rental rate unit or resource kind')
        elif _number(rate['quantum_hours']) != quantum:
            raise ContractError('rental billing quantum disagrees with external rate')
        rental_lines.append({'id': identity, 'occurrence': row, 'occupancy_hours': _exact(duration),
            'peak_concurrent_assignments': peak, 'currency': None if rate is None else rate['currency'],
            'cents': None if unknown else rate['cents'] * units, 'unresolved': unknown,
            'formula': [{'hire_hours_including_retained_gaps': _exact(duration),
                         'billing_quantum_hours': _exact(quantum), 'billable_quanta': units,
                         'rate_id': row['rate_id']} ]})
    for line in lines:
        for kind in sorted(schedules[line['id']][1]):
            if (line['id'], kind) not in coverage:
                line['unresolved'].append('required resource occurrence missing: ' + kind)
        if line['unresolved']:
            line['cents'] = None
    for line in lines + rental_lines:
        line['status'] = 'unresolved' if line['unresolved'] else 'calculated'
    report = {'kind': 'labor-report-v1', 'source_sha256': digest, 'request_sha256': _hash(req),
        'policy': req['policy'], 'rates': sorted(rates.values(), key=lambda r: r['id']),
        'work': lines, 'rentals': rental_lines, 'quantity_warnings': warnings,
        'scope_allocations': [{'contribution_id': c, 'operation_id': o, 'fraction': _exact(f)}
                              for (c, o), f in sorted(allocations.items())],
        'limitations': ['Explicit selected scope only; unallocated inventory is not priced or declared complete.',
            'Caller supplies measured height-band splits or conservative whole-operation classification relative to a known work surface.',
            'No scheduler optimization, wage eligibility, engineering or code judgment.',
            'No OA connection, editable company standards UI or production pricing integration.',
            'External rental quantum contract only; vendor weekly/monthly tier selection is not implemented.']}
    return LaborReport(source, _json(req), _json(report))
