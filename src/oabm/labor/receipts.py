"""Proposed generic interchange for read-only exported standards/pricing receipts.

This is NOT an existing OA export format, network client or private pricing
implementation. An authenticated exporter must supply missing interface fields.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from fractions import Fraction

from oabm.model import ContractError
from .core import (_OutputBudget, _bounded_json, _cents, _detach, _hash,
                   _hours, _index, _instant, _json, _keys, _number, _text,
                   generate_labor_report)
from oabm.reports.physical_routes import _text_cell

MAX_INTEGER = 2**63 - 1


def _integer(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ContractError('receipt requires bounded nonnegative integer')
    return value


def _sum(values):
    result = sum(_integer(v) for v in values)
    return _integer(result)


def _rational(value, *, zero=False):
    # Engine effort is an exact rational; decimal-only receipts may not describe
    # thirds. Bounded numerator/denominator interchange preserves that effort.
    if type(value) is str and '/' in value:
        if len(value) > 256 or value.count('/') != 1:
            raise ContractError('receipt rational exceeds limit')
        numerator, denominator = value.split('/')
        result = _number(numerator, zero=zero) / _number(denominator)
        return result
    return _number(value, zero=zero)


def _receipt(row, extra):
    # Missing known receipt facts are unresolved, never inferred from a price.
    for key in ('revision', 'source', *extra.split()):
        row.setdefault(key, None)
    _keys(row, 'id target_id status missing_inputs revision source ' + extra)
    for key in ('id', 'target_id'):
        _text(row[key])
    if row['status'] not in ('available', 'unresolved'):
        raise ContractError('unknown external receipt status')
    if type(row['missing_inputs']) is not list:
        raise ContractError('receipt missing_inputs must be a list')
    for message in row['missing_inputs']:
        _text(message)
    if (row['status'] == 'available') != (not row['missing_inputs']):
        raise ContractError('receipt status disagrees with missing inputs')
    missing = list(row['missing_inputs'])
    for key in ('revision', 'source', *extra.split()):
        if row[key] is None:
            missing.append('external receipt ' + key + ' missing')
        elif key in ('revision', 'source', 'currency', 'price_basis'):
            _text(row[key])
    return missing


def _targets(rows, targets):
    result = {}
    for row in _index(rows).values():
        _text(row.get('target_id'))
        target = row['target_id']
        if target not in targets or target in result:
            raise ContractError('unknown or duplicated external receipt ownership')
        result[target] = row
    return result


def _labor_price(receipt, row, original):
    missing = _receipt(receipt, 'currency work_sha256 cost_bucket unit price_basis quantity unit_price_cents amount_cents priced_person_hours')
    if receipt['work_sha256'] is not None and receipt['work_sha256'] != _hash(original):
        raise ContractError('labor receipt does not bind the original work/crew selections')
    if missing:
        return None, missing
    if receipt['cost_bucket'] != 'labor':
        raise ContractError('external labor receipt must contain only the labor bucket')
    _text(receipt['currency'])
    hours = _rational(receipt['priced_person_hours'], zero=True)
    if row['adjusted_person_hours'] is None:
        return None, ['adjusted effort unavailable for external pricing']
    if hours != _rational(row['adjusted_person_hours'], zero=True):
        raise ContractError('external labor receipt prices different person-hours')
    quantity = _rational(receipt['quantity'], zero=True)
    unit_price = _integer(receipt['unit_price_cents'])
    amount = _integer(receipt['amount_cents'])
    if receipt['price_basis'] == 'one-hour-unit':
        if receipt['unit'] != 'person-hour' or quantity != hours:
            raise ContractError('one-hour receipt must price the exact person-hour quantity')
        expected = _cents(quantity * unit_price)
    elif receipt['price_basis'] == 'total-hours-in-one-unit':
        if receipt['unit'] != 'operation' or quantity != 1:
            raise ContractError('total-hours receipt requires one operation unit')
        expected = unit_price  # Already contains ALL hours. Never multiply again.
    else:
        raise ContractError('unsupported external labor price basis')
    if amount != expected:
        raise ContractError('labor receipt amount disagrees with its declared basis')
    return amount, []


def _rental_price(receipt, original):
    missing = _receipt(receipt, 'currency occurrence_sha256 cost_bucket unit price_basis quantity hire off_hire priced_elapsed_hours '
             'duration_basis durationDays tier_coverage_days mix coverageDays timeCents deliveryPickupCents '
             'fuelCents damageWaiverCents environmentalCents costCents markupBps markupCents sellCents rounding')
    if receipt['occurrence_sha256'] is not None and receipt['occurrence_sha256'] != _hash(original):
        raise ContractError('rental receipt does not bind the complete original occurrence')
    if missing:
        return None, missing
    if receipt['cost_bucket'] != 'rental':
        raise ContractError('external rental receipt must contain only the rental bucket')
    if receipt['rounding'] is None:
        return None, ['external provider markup rounding contract missing']
    if receipt['rounding'] != 'half-up-integer-cents':
        return None, ['external provider markup rounding contract unsupported']
    _text(receipt['currency'])
    if (receipt['price_basis'], receipt['unit'], _rational(receipt['quantity'])) != ('provider-occurrence-total', 'occurrence', 1):
        raise ContractError('rental receipt must price one whole occurrence')
    hire, off_hire = _instant(receipt['hire']), _instant(receipt['off_hire'])
    if (hire, off_hire) != (_instant(original['hire']), _instant(original['off_hire'])):
        raise ContractError('rental receipt hire period disagrees with occurrence')
    hours = _hours((hire, off_hire))
    if _rational(receipt['priced_elapsed_hours']) != hours:
        raise ContractError('rental receipt does not retain actual elapsed hire time')
    elapsed_days = hours / 24
    if receipt['duration_basis'] == 'elapsed-24-hour-days':
        days = elapsed_days
    elif receipt['duration_basis'] == 'started-24-hour-days':
        days = Fraction(-(-elapsed_days.numerator // elapsed_days.denominator))
    elif receipt['duration_basis'] == 'local-calendar-days':
        return None, ['local-calendar-day vendor boundary/timezone contract not admitted']
    else:
        raise ContractError('rental receipt requires explicit hire-based duration semantics')
    if _rational(receipt['durationDays']) != days:
        raise ContractError('rental durationDays disagrees with explicit hire-based duration')
    _keys(receipt['tier_coverage_days'], 'day week month')
    tiers = {unit: _rational(value) for unit, value in receipt['tier_coverage_days'].items()}
    if type(receipt['mix']) is not list or not receipt['mix']:
        raise ContractError('rental receipt requires normalized provider mix rows')
    coverage, times = Fraction(0), []
    for mix in receipt['mix']:
        _keys(mix, 'unit quantity coverageDays timeCents')
        _text(mix['unit'])
        if mix['unit'] not in tiers:
            raise ContractError('unsupported normalized rental mix unit')
        count = _integer(mix['quantity'])
        if _rational(mix['coverageDays'], zero=True) != count * tiers[mix['unit']]:
            raise ContractError('rental mix coverage disagrees with declared provider tier')
        coverage += count * tiers[mix['unit']]
        if count == 0 and mix['timeCents'] != 0:
            raise ContractError('zero rental tier quantity cannot carry a time amount')
        times.append(_integer(mix['timeCents']))
    if coverage != _rational(receipt['coverageDays']) or coverage < days:
        raise ContractError('rental coverage does not cover declared duration')
    time = _integer(receipt['timeCents'])
    if time != _sum(times):
        raise ContractError('rental mix time amounts disagree with timeCents')
    cost = _sum([time, receipt['deliveryPickupCents'], receipt['fuelCents'],
                 receipt['damageWaiverCents'], receipt['environmentalCents']])
    markup = _cents(Fraction(cost * _integer(receipt['markupBps']), 10_000))
    if (_integer(receipt['costCents']), _integer(receipt['markupCents']), _integer(receipt['sellCents'])) != (
            cost, _integer(markup), _sum([cost, markup])):
        raise ContractError('rental receipt cost/markup/sell arithmetic disagrees')
    return receipt['sellCents'], []


@dataclass(frozen=True)
class ReceiptedLaborReport:
    source_model_json: str
    request_json: str
    receipts_json: str
    report_json: str

    def to_dict(self):
        return json.loads(self.report_json)

    def to_json(self):
        return self.report_json

    def to_csv(self):
        doc = self.to_dict(); budget = _OutputBudget()
        output = io.StringIO(newline=''); writer = csv.writer(output, lineterminator='\n')
        def write(values):
            budget.charge(values)
            writer.writerow([_text_cell(_json(v) if type(v) in (dict, list) else v) for v in values])
        write(('kind', 'id', 'status', 'currency', 'cents', 'unresolved', 'receipt', 'source_sha256', 'request_sha256',
               'receipt_export_sha256', 'group_diagnostics'))
        for kind in ('work', 'rentals'):
            for row in doc[kind]:
                write((kind, row['id'], row['status'], row['currency'], row['cents'], row['unresolved'],
                       row['receipt'], doc['source_sha256'], doc['request_sha256'],
                       doc['receipt_export_sha256'], doc['group_diagnostics']))
        return output.getvalue()

    def validate_against(self, model, request, receipts):
        if self != generate_receipted_labor_report(model, request, receipts):
            raise ContractError('external labor report is stale or changed')


def generate_receipted_labor_report(model, request, receipts):
    """Consume an explicit exported envelope; no calls, OA writes or rate defaults."""
    req, export = _detach(request), _detach(receipts)
    request_json, receipts_json = _json(req), _json(export)
    receipt_export_sha256 = _hash(export)
    _keys(req, 'version source_sha256 groups policy operations rules rates work rentals')
    if type(export) is not dict:
        raise ContractError('external receipt envelope must be an object')
    export.setdefault('version', None)
    _keys(export, 'version source_sha256 request_sha256 provider standards labor_prices rental_prices')
    if export['version'] not in (None, 'external-labor-export-v1'):
        raise ContractError('unsupported proposed external labor interchange')
    envelope_issues = [] if export['version'] is not None else ['external interchange version missing']
    if type(export['provider']) is not dict:
        raise ContractError('external provider metadata must be an object')
    for key in ('id', 'revision', 'source'): export['provider'].setdefault(key, None)
    _keys(export['provider'], 'id revision source')
    for key, value in export['provider'].items():
        if value is None:
            envelope_issues.append('external provider ' + key + ' missing')
        else:
            _text(value)
    if export['request_sha256'] != _hash(req) or export['source_sha256'] != req.get('source_sha256'):
        raise ContractError('exported receipt envelope is stale or bound to another request')
    # The core still owns canonical admission, measurement and resource capacity.
    operations = _index(req.get('operations'))
    work = _index(req.get('work')); rentals = _index(req.get('rentals'))
    standards = _targets(export['standards'], operations)
    standard_issues = {}
    for identity, operation in operations.items():
        _keys(operation, 'id category item_type unit exclusive_family person_hours_per_unit')
        standard = standards.get(identity)
        if standard is None:
            operation['person_hours_per_unit'] = None
            standard_issues[identity] = ['external per-unit productivity standard missing']
            continue
        missing = _receipt(standard, 'unit person_hours_per_unit')
        if standard['unit'] is not None and standard['unit'] != operation['unit']:
            raise ContractError('external productivity standard unit disagrees with operation')
        if missing:
            operation['person_hours_per_unit'] = None
            standard_issues[identity] = missing
        else:
            _number(standard['person_hours_per_unit'])
            operation['person_hours_per_unit'] = standard['person_hours_per_unit']
    labor_prices = _targets(export['labor_prices'], work)
    rental_prices = _targets(export['rental_prices'], rentals)
    original = _detach(req)
    # Request-local rate IDs are not replaced with synthetic zero prices. The
    # core exposes pricing-only missing inputs separately for receipt replacement.
    req['rates'] = []
    for row in work.values():
        _keys(row, 'id contribution_id operation_id fraction phase mobilization evidence conditions height_basis crew windows')
        for member in _index(row['crew']).values():
            _keys(member, 'id role count selection rate_id')
            member['rate_id'] = None
    for row in rentals.values():
        _keys(row, 'id kind mobilization evidence capacity assignments hire off_hire rate_id billing_quantum_hours')
        row['rate_id'] = None
    engine = generate_labor_report(model, req)
    document = engine.to_dict()
    results = {'work': [], 'rentals': []}
    for kind, prices, owners in (('work', labor_prices, _index(original['work'])),
                                ('rentals', rental_prices, _index(original['rentals']))):
        for row in document[kind]:
            receipt = prices.get(row['id'])
            unknown = list(row['nonpricing_unresolved']) + envelope_issues
            if kind == 'work':
                unknown += standard_issues.get(row['operation']['id'], [])
            if receipt is None:
                amount, missing = None, ['external price receipt missing']
            elif kind == 'work':
                amount, missing = _labor_price(receipt, row, owners[row['id']])
            else:
                amount, missing = _rental_price(receipt, owners[row['id']])
            unknown += missing
            results[kind].append({'id': row['id'], 'status': 'unresolved' if unknown else 'provider-reported',
                'currency': None if receipt is None or receipt['status'] != 'available' else receipt['currency'],
                'cents': None if unknown else amount, 'unresolved': unknown, 'receipt': receipt})
    report = {'kind': 'receipted-labor-report-v1', 'source_sha256': document['source_sha256'],
        'request_sha256': export['request_sha256'], 'resolved_request_sha256': document['request_sha256'],
        'receipt_export_sha256': receipt_export_sha256,
        'unresolved': envelope_issues,
        'group_diagnostics': document['group_diagnostics'],
        'provider': export['provider'], 'standards': export['standards'], 'engine_report': document, **results,
        'limitations': ['Proposed generic interchange, not an existing OA export or live authenticated integration.',
            'Provider-reported amounts with arithmetic/binding checks, not independently verified vendor rates or wage eligibility.',
            'Native OA export field mapping, markup rounding declaration and complete unit/rule/rental receipt require exporter acceptance.',
            'Rental mix row normalization and tier coverage are explicit provider terms; no calendar month assumption or optimization.']}
    return ReceiptedLaborReport(engine.source_model_json, request_json, receipts_json, _bounded_json(report))


def read_labor_json(path):
    """Read a local request/export with a byte cap and duplicate-key refusal."""
    from pathlib import Path
    from .core import MAX_BYTES
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ContractError('labor export file exceeds input byte limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError('duplicate labor export JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ContractError('nonfinite labor export JSON constant')
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except ContractError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError('invalid labor export JSON') from exc
    return _detach(result)
