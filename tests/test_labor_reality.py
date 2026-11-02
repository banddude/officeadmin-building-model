"""Fictitious policy/rates only. Source acceptance cases, execution on hold."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.labor import contribution_inventory, generate_labor_report
from oabm.model import BuildingModel, ContractError, Point3

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/quantities/synthetic-route-model.json'


def request_for(model):
    inv = contribution_inventory(model)
    selected = [r for r in inv['contributions'] if r['basis']['category'] == 'device'][:2]
    operations, work = [], []
    for index, contribution in enumerate(selected):
        basis = contribution['basis']
        operation = {'id': f'install-{index}', 'category': basis['category'],
            'item_type': basis['item_type'], 'unit': basis['unit'],
            'exclusive_family': 'device-install', 'person_hours_per_unit': '2'}
        operations.append(operation)
        work.append({'id': f'work-{index}', 'contribution_id': contribution['id'],
            'operation_id': operation['id'], 'fraction': '1', 'phase': 'rough',
            'mobilization': 'visit-1', 'evidence': 'synthetic measured work scope',
            'conditions': {'occupied': 'yes', 'hillside': 'yes', 'no-truck': 'yes', 'height': 'high'},
            'height_basis': {'work_surface': 'synthetic-floor', 'evidence': 'measured relative height band',
                'method': 'measured-split', 'min_height_m': '4', 'max_height_m': '5'},
            'crew': [{'id': 'crew', 'role': 'installer', 'count': 2,
                      'selection': 'regular', 'rate_id': 'fictional-labor'}],
            'windows': [{'start': '2026-11-01T08:00:00-08:00', 'end': '2026-11-01T12:00:00-08:00'}]})
    return {'version': 'labor-request-v1', 'source_sha256': inv['source_sha256'], 'groups': None,
        'policy': {'id': 'fictional-only', 'revision': '1', 'source': 'synthetic test'},
        'operations': operations,
        'rules': [{'id': condition, 'condition': condition, 'value': value,
                   'multiplier': '2' if condition == 'occupied' else '1',
                   'resource_kind': 'lift' if condition == 'height' else None,
                   'height_min_m': '3' if condition == 'height' else None,
                   'height_max_m': '6' if condition == 'height' else None}
                  for condition, value in work[0]['conditions'].items()],
        'rates': [{'id': 'fictional-labor', 'kind': 'labor', 'unit': 'person-hour',
                   'currency': 'TEST', 'revision': '1', 'source': 'fictitious', 'cents': 100,
                   'selection': 'regular', 'role': 'installer', 'quantum_hours': None},
                  {'id': 'fictional-lift', 'kind': 'rental', 'unit': 'hire-quantum',
                   'currency': 'TEST', 'revision': '1', 'source': 'fictitious', 'cents': 300,
                   'selection': 'explicit-24h', 'role': 'lift', 'quantum_hours': '24'}],
        'work': work, 'rentals': [{'id': 'shared-lift', 'kind': 'lift', 'mobilization': 'visit-1',
            'evidence': 'synthetic hire contract', 'capacity': 2, 'assignments': [w['id'] for w in work],
            'hire': '2026-11-01T08:00:00-08:00', 'off_hire': '2026-11-02T09:00:00-08:00',
            'rate_id': 'fictional-lift', 'billing_quantum_hours': '24'}]}


def model():
    return BuildingModel.load(FIXTURE)


def test_shared_lift_effort_money_and_retained_overnight_are_separate():
    m = model(); req = request_for(m)
    original = deepcopy(req); source = m.to_json()
    report = generate_labor_report(m, req)
    doc = report.to_dict()
    assert all(w['base_person_hours'] == '2' and w['adjusted_person_hours'] == '4' for w in doc['work'])
    assert all(w['cents'] == 400 and w['occupancy_hours'] == '4' for w in doc['work'])
    assert len(doc['rentals']) == 1
    assert doc['rentals'][0]['occupancy_hours'] == '25'
    assert doc['rentals'][0]['cents'] == 600
    assert req == original and m.to_json() == source
    assert report.to_json() == generate_labor_report(m, req).to_json()
    report.validate_against(m, req)
    assert 'shared-lift' in report.to_csv()


@pytest.mark.parametrize('selection', ['regular', 'overtime', 'off-hours', 'prevailing'])
def test_explicit_rate_selection_has_no_legal_inference(selection):
    m = model(); req = request_for(m)
    req['rates'][0]['selection'] = selection
    for work in req['work']:
        work['crew'][0]['selection'] = selection
    assert generate_labor_report(m, req).to_dict()['work'][0]['cents'] == 400


@pytest.mark.parametrize('change,match', [
    ('overlap', 'allocated'), ('unit', 'incompatible'), ('rule', 'duplicated restriction'),
    ('capacity', 'capacity'), ('naive', 'offset'), ('height', 'height bands'),
    ('resource-owner', 'resource ownership'), ('mobilization', 'mobilizations'),
])
def test_refuses_ambiguous_requests(change, match):
    m = model(); req = request_for(m)
    if change == 'overlap':
        duplicate = deepcopy(req['work'][0]); duplicate['id'] = 'duplicate'; req['work'].append(duplicate)
    elif change == 'unit': req['operations'][0]['unit'] = 'm2'
    elif change == 'rule':
        duplicate = deepcopy(req['rules'][0]); duplicate['id'] = 'duplicate'; req['rules'].append(duplicate)
    elif change == 'capacity': req['rentals'][0]['capacity'] = 1
    elif change == 'naive': req['work'][0]['windows'][0]['start'] = '2026-11-01T08:00:00'
    elif change == 'height': req['work'][0]['height_basis']['method'] = 'average-endpoint'
    elif change == 'resource-owner':
        duplicate = deepcopy(req['rentals'][0]); duplicate['id'] = 'duplicate'; req['rentals'].append(duplicate)
    elif change == 'mobilization': req['work'][0]['mobilization'] = 'visit-2'
    with pytest.raises(ContractError, match=match): generate_labor_report(m, req)


@pytest.mark.parametrize('missing', ['rates', 'condition', 'height', 'productivity', 'resource'])
def test_unknown_inputs_remain_unresolved(missing):
    m = model(); req = request_for(m)
    if missing == 'rates': req['rates'] = []
    elif missing == 'condition': req['work'][0]['conditions']['hillside'] = None
    elif missing == 'height': req['work'][0]['height_basis'] = None
    elif missing == 'productivity': req['operations'][0]['person_hours_per_unit'] = None
    elif missing == 'resource': req['rentals'] = []
    row = generate_labor_report(m, req).to_dict()['work'][0]
    assert row['status'] == 'unresolved' and row['cents'] is None


def test_partial_measured_height_split_and_two_mobilizations():
    m = model(); req = request_for(m)
    first = req['work'][0]; first['fraction'] = '0.25'
    second = deepcopy(first); second.update(id='later', fraction='0.5', phase='finish', mobilization='visit-2')
    second['windows'] = [{'start': '2026-11-03T08:00:00-08:00', 'end': '2026-11-03T12:00:00-08:00'}]
    second['conditions']['height'] = 'low'
    second['height_basis'].update(min_height_m='1', max_height_m='2')
    req['rules'].append({'id': 'low', 'condition': 'height', 'value': 'low', 'multiplier': '1',
        'resource_kind': 'scaffold', 'height_min_m': '0', 'height_max_m': '3'})
    req['work'].append(second)
    rental = deepcopy(req['rentals'][0]); rental.update(id='second-hire', kind='scaffold', mobilization='visit-2',
        assignments=['later'], hire='2026-11-03T08:00:00-08:00', off_hire='2026-11-03T12:00:00-08:00')
    scaffold_rate = deepcopy(req['rates'][1]); scaffold_rate.update(id='scaffold', role='scaffold')
    req['rates'].append(scaffold_rate); rental['rate_id'] = 'scaffold'; req['rentals'].append(rental)
    doc = generate_labor_report(m, req).to_dict()
    assert len(doc['rentals']) == 2
    allocation = next(a for a in doc['scope_allocations'] if a['contribution_id'] == first['contribution_id'])
    assert allocation['fraction'] == '3/4'
    assert sum(w['cents'] for w in doc['work']) == 700


def test_exclusive_alternative_operation_rejected():
    m = model(); req = request_for(m)
    alternative = deepcopy(req['operations'][0]); alternative['id'] = 'alternative'; req['operations'].append(alternative)
    row = deepcopy(req['work'][0]); row.update(id='alternative', operation_id='alternative'); req['work'].append(row)
    with pytest.raises(ContractError, match='exclusive'): generate_labor_report(m, req)


def test_dst_offsets_are_elapsed_time_and_shutdown_gaps_retained():
    m = model(); req = request_for(m)
    for work in req['work']:
        work['windows'] = [{'start': '2026-11-01T01:00:00-07:00', 'end': '2026-11-01T01:00:00-08:00'},
                           {'start': '2026-11-01T03:00:00-08:00', 'end': '2026-11-01T04:00:00-08:00'}]
    req['rentals'][0].update(hire='2026-11-01T01:00:00-07:00', off_hire='2026-11-01T04:00:00-08:00')
    doc = generate_labor_report(m, req).to_dict()
    assert doc['work'][0]['occupancy_hours'] == '2'
    assert doc['rentals'][0]['occupancy_hours'] == '4'


def test_genuine_small_quantity_changes_and_stale_binding():
    m = model(); req = request_for(m)
    inventory = contribution_inventory(m)
    route = m.routes[0]
    # Insert a tiny kink, preserving endpoints and ports. No tolerance may
    # suppress this genuinely nonzero length change.
    points = route.centerline.points
    changed_point = Point3(x=(points[0].x + points[1].x) / 2,
        y=(points[0].y + points[1].y) / 2, z=(points[0].z + points[1].z) / 2 + 0.000001)
    changed = replace(m, routes=(replace(route, centerline=replace(route.centerline,
        points=(points[0], changed_point, *points[1:]))), *m.routes[1:]))
    after = contribution_inventory(changed)
    before_quantity = next(r['basis']['quantity'] for r in inventory['contributions']
        if r['basis']['category'] == 'route_length' and r['basis']['source_entity_ids'] == [route.id])
    after_quantity = next(r['basis']['quantity'] for r in after['contributions']
        if r['basis']['category'] == 'route_length' and r['basis']['source_entity_ids'] == [route.id])
    assert after_quantity > before_quantity
    with pytest.raises(ContractError, match='stale'): generate_labor_report(changed, req)


def test_bounded_detached_admission():
    m = model(); req = request_for(m)
    req['extra'] = req
    with pytest.raises(ContractError, match='cyclic'): generate_labor_report(m, req)


def test_conductor_route_pairs_do_not_conflict_with_conduit_or_each_other():
    m = model(); req = request_for(m)
    req['operations'] = []; req['work'] = []; req['rentals'] = []
    template = request_for(m)['work'][0]
    for index, contribution in enumerate(contribution_inventory(m)['contributions']):
        basis = contribution['basis']
        if basis['category'] not in ('conductor_length', 'route_length'):
            continue
        op = {'id': f'op-{index}', 'category': basis['category'], 'item_type': basis['item_type'],
              'unit': basis['unit'], 'exclusive_family': basis['category'], 'person_hours_per_unit': '0.01'}
        # Same operation for same eligible category/type across original route contributions.
        existing = next((o for o in req['operations'] if (o['category'], o['item_type']) ==
                         (op['category'], op['item_type'])), None)
        if existing is None: req['operations'].append(op)
        else: op = existing
        row = deepcopy(template); row.update(id=f'work-{index}', contribution_id=contribution['id'], operation_id=op['id'])
        req['work'].append(row)
    # Suppress resource requirement through explicit supplied rule, not a default.
    for rule in req['rules']: rule['resource_kind'] = None
    doc = generate_labor_report(m, req).to_dict()
    assert len(doc['work']) == len(req['work'])
    assert all(row['status'] == 'calculated' for row in doc['work'])


def test_rate_quantum_cannot_be_reinterpreted_by_request():
    m = model(); req = request_for(m)
    req['rentals'][0]['billing_quantum_hours'] = '1'
    with pytest.raises(ContractError, match='quantum'): generate_labor_report(m, req)


def test_conservative_whole_operation_height_and_resource_missing_rate():
    m = model(); req = request_for(m)
    req['work'][0]['height_basis']['method'] = 'conservative-whole-operation'
    req['rates'] = [r for r in req['rates'] if r['kind'] == 'labor']
    doc = generate_labor_report(m, req).to_dict()
    assert doc['work'][0]['adjusted_person_hours'] == '4'
    assert doc['rentals'][0]['status'] == 'unresolved'
    assert doc['rentals'][0]['cents'] is None


def test_missing_rules_and_insufficient_shutdown_window_capacity():
    m = model(); req = request_for(m)
    req['rules'] = [r for r in req['rules'] if r['condition'] != 'hillside']
    assert generate_labor_report(m, req).to_dict()['work'][0]['adjusted_person_hours'] is None
    req = request_for(m)
    req['work'][0]['windows'][0]['end'] = '2026-11-01T08:30:00-08:00'
    row = generate_labor_report(m, req).to_dict()['work'][0]
    assert row['cents'] is None and 'insufficient' in ' '.join(row['unresolved'])


def test_height_crossing_requires_split_or_conservative_whole_operation():
    m = model(); req = request_for(m)
    req['work'][0]['height_basis']['min_height_m'] = '1'
    with pytest.raises(ContractError, match='height band crossing'): generate_labor_report(m, req)
    req['work'][0]['height_basis']['method'] = 'conservative-whole-operation'
    with pytest.raises(ContractError, match='unclassified gaps'): generate_labor_report(m, req)
    req['rules'].append({'id': 'low-covered', 'condition': 'height', 'value': 'low-covered',
        'height_min_m': '0', 'height_max_m': '3', 'multiplier': '1', 'resource_kind': None})
    assert generate_labor_report(m, req).to_dict()['work'][0]['cents'] == 400


def test_shared_hardware_original_contribution_allocated_once():
    m = model(); req = request_for(m)
    fitting = next(r for r in contribution_inventory(m)['contributions'] if r['basis']['category'] == 'fitting')
    basis = fitting['basis']
    req['operations'] = [{'id': 'hardware', 'category': basis['category'], 'item_type': basis['item_type'],
        'unit': basis['unit'], 'exclusive_family': 'hardware-install', 'person_hours_per_unit': '2'}]
    row = deepcopy(req['work'][0]); row.update(contribution_id=fitting['id'], operation_id='hardware', fraction='0.5')
    second = deepcopy(row); second['id'] = 'second-half'
    req['work'] = [row, second]
    req['rentals'][0]['assignments'] = [row['id'], second['id']]
    doc = generate_labor_report(m, req).to_dict()
    assert len(doc['scope_allocations']) == 1 and doc['scope_allocations'][0]['fraction'] == '1'
    assert sum(int(w['base_person_hours']) for w in doc['work']) == 2
    assert all(w['basis'] == basis for w in doc['work'])


def test_group_typo_and_matched_group_diagnostics_survive_inventory_and_report():
    m = model(); req = request_for(m)
    req['groups'] = {'ALTERNATE': ['device:misspelled-member'], 'EMPTY': []}
    inv = contribution_inventory(m, groups=req['groups'])
    expected = {'unmatched_group_ids': 1, 'groups': {
        'ALTERNATE': {'entities': 0, 'items': 0}, 'EMPTY': {'entities': 0, 'items': 0}}}
    assert inv['group_diagnostics'] == expected
    # A misspelled alternate stays ungrouped in inherited takeoff, but its
    # unmatched ownership is now visible in both report formats.
    report = generate_labor_report(m, req)
    assert report.to_dict()['group_diagnostics'] == expected
    assert 'unmatched_group_ids' in report.to_csv()
    actual_id = inv['contributions'][0]['basis']['source_entity_ids'][0]
    matched = contribution_inventory(m, groups={'MATCHED': [actual_id]})
    assert matched['group_diagnostics']['unmatched_group_ids'] == 0
    assert matched['group_diagnostics']['groups']['MATCHED']['entities'] == 1


@pytest.mark.parametrize('groups', [[], '', 0, {'ALT': 'device:evse'}, {'ALT': None},
                                   {'ALT': [None]}, {'ALT': [[]]}, {'ALT': [7]}])
def test_malformed_group_shapes_refuse_at_labor_boundary(groups):
    m = model(); req = request_for(m); req['groups'] = groups
    with pytest.raises(ContractError): generate_labor_report(m, req)
    with pytest.raises(ContractError): contribution_inventory(m, groups=groups)


@pytest.mark.parametrize('condition', [[], {}, None, 4, True])
def test_malformed_rule_condition_is_contract_error(condition):
    m = model(); req = request_for(m); req['rules'][0]['condition'] = condition
    with pytest.raises(ContractError, match='text'): generate_labor_report(m, req)


@pytest.mark.parametrize('low_multiplier,low_resource', [('2', None), ('1', 'scaffold'), ('2', 'scaffold')])
def test_conservative_height_requires_effort_and_resource_dominance(low_multiplier, low_resource):
    m = model(); req = request_for(m)
    req['work'][0]['height_basis'].update(min_height_m='1', max_height_m='5', method='conservative-whole-operation')
    # Selected high band is multiplier one and requires no resource. The crossed
    # low band must not silently disappear just because the maximum is higher.
    high = next(r for r in req['rules'] if r['condition'] == 'height')
    high.update(multiplier='1', resource_kind=None)
    req['rules'].append({'id': 'low', 'condition': 'height', 'value': 'low',
        'height_min_m': '0', 'height_max_m': '3', 'multiplier': low_multiplier, 'resource_kind': low_resource})
    with pytest.raises(ContractError, match='does not dominate'): generate_labor_report(m, req)
    high.update(multiplier='2', resource_kind=low_resource)
    if low_resource:
        req['rentals'][0]['kind'] = low_resource
        req['rates'][1]['role'] = low_resource
    # The selected whole-operation rule now covers all crossed requirements.
    assert generate_labor_report(m, req).to_dict()['work'][0]['status'] == 'calculated'


def test_expanded_fractional_report_is_charged_before_report_serialization(monkeypatch):
    import oabm.labor.core as core
    m = model(); req = request_for(m)
    captured = core._inventory(m, None)
    template = deepcopy(req['work'][0]); template['fraction'] = '0.01'
    req['work'] = [dict(deepcopy(template), id=f'fraction-{i}') for i in range(100)]
    req['rentals'] = []
    monkeypatch.setattr(core, '_inventory', lambda model, groups: captured)
    # Establish a cap above a single original basis but below its repetition.
    budget = core._OutputBudget()
    budget.charge(captured[1][0]['basis'])
    one_basis_charge = core.MAX_OUTPUT_BYTES - budget.bytes_left
    monkeypatch.setattr(core, 'MAX_OUTPUT_BYTES', one_basis_charge * 10)
    original_json = core._json
    def guarded_json(value):
        assert not (type(value) is dict and value.get('kind') == 'labor-report-v1'), 'oversized report reached JSON serialization'
        return original_json(value)
    monkeypatch.setattr(core, '_json', guarded_json)
    with pytest.raises(ContractError, match='expanded labor output'): generate_labor_report(m, req)


def test_csv_repeated_rate_receipts_have_cumulative_preencoding_budget(monkeypatch):
    import oabm.labor.core as core
    m = model(); req = request_for(m)
    report = generate_labor_report(m, req)
    doc = report.to_dict()
    # A valid bounded report may still expand far more in CSV because each
    # work row carries receipts. Use many otherwise-unused fictitious receipts.
    doc['rates'] = [dict(doc['rates'][0], id=f'fictional-{i}', source='x' * 200) for i in range(20)]
    doc['work'] = [dict(deepcopy(doc['work'][0]), id=f'csv-{i}') for i in range(20)]
    report = replace(report, report_json=core._json(doc))
    budget = core._OutputBudget(); budget.charge(doc['rates'])
    receipt_charge = core.MAX_OUTPUT_BYTES - budget.bytes_left
    monkeypatch.setattr(core, 'MAX_OUTPUT_BYTES', receipt_charge * 3)
    original_json = core._json
    receipt_encodings = []
    def guarded_json(value):
        if value is doc['rates'] or (type(value) is list and value and type(value[0]) is dict and value[0].get('id') == 'fictional-0'):
            receipt_encodings.append(1)
        return original_json(value)
    monkeypatch.setattr(core, '_json', guarded_json)
    with pytest.raises(ContractError, match='expanded labor output'): report.to_csv()
    assert len(receipt_encodings) < 4


@pytest.mark.parametrize('lower_band_upper', ['0.5', '2', '2.999999999'])
def test_conservative_height_union_requires_entire_interval_without_gaps(lower_band_upper):
    m = model(); req = request_for(m)
    req['work'][0]['height_basis'].update(min_height_m='1', max_height_m='5', method='conservative-whole-operation')
    req['rules'].append({'id': 'lower', 'condition': 'height', 'value': 'lower',
        'height_min_m': '0', 'height_max_m': lower_band_upper, 'multiplier': '1', 'resource_kind': None})
    with pytest.raises(ContractError, match='unclassified gaps'): generate_labor_report(m, req)
    req['rules'][-1]['height_max_m'] = '3'
    assert generate_labor_report(m, req).to_dict()['work'][0]['status'] == 'calculated'


def test_all_overlapping_height_bands_still_participate_after_union_coverage():
    m = model(); req = request_for(m)
    req['work'][0]['height_basis'].update(min_height_m='1', max_height_m='5', method='conservative-whole-operation')
    req['rules'].extend([
        {'id': 'cover', 'condition': 'height', 'value': 'cover', 'height_min_m': '0', 'height_max_m': '3',
         'multiplier': '1', 'resource_kind': None},
        {'id': 'overlap', 'condition': 'height', 'value': 'overlap', 'height_min_m': '2', 'height_max_m': '4',
         'multiplier': '2', 'resource_kind': 'scaffold'}])
    # Coverage is complete; the overlapping middle rule remains a blocker.
    with pytest.raises(ContractError, match='does not dominate'): generate_labor_report(m, req)


@pytest.mark.parametrize('timestamp', ['0001-01-01T00:00:00+01:00', '9999-12-31T23:59:59-01:00'])
def test_utc_conversion_overflow_is_a_contract_refusal(timestamp):
    from oabm.labor.core import _instant
    with pytest.raises(ContractError, match='outside supported datetime range'): _instant(timestamp)


def receipt_export_for(m, request):
    """Proposed normalized export. Every amount here is explicitly fictitious."""
    from oabm.labor.core import _hash
    base = generate_labor_report(m, request).to_dict()
    common = {'status': 'available', 'missing_inputs': [], 'revision': 'fictional-v1', 'source': 'fictitious export'}
    labor = []
    for index, row in enumerate(request['work']):
        labor.append(dict(common, id=f'price-{index}', target_id=row['id'], currency='TEST',
            cost_bucket='labor',
            work_sha256=_hash(row), unit='person-hour' if index == 0 else 'operation',
            price_basis='one-hour-unit' if index == 0 else 'total-hours-in-one-unit',
            quantity='4' if index == 0 else '1', unit_price_cents=100 if index == 0 else 400,
            amount_cents=400, priced_person_hours='4'))
    occurrence = request['rentals'][0]
    rental = dict(common, id='rental-total', target_id=occurrence['id'], currency='TEST',
        cost_bucket='rental',
        occurrence_sha256=_hash(occurrence), unit='occurrence', price_basis='provider-occurrence-total',
        quantity='1', hire=occurrence['hire'], off_hire=occurrence['off_hire'], priced_elapsed_hours='25',
        duration_basis='started-24-hour-days', durationDays='2',
        tier_coverage_days={'day': '1', 'week': '7', 'month': '28'},
        mix=[{'unit': 'day', 'quantity': 2, 'coverageDays': '2', 'timeCents': 600}], coverageDays='2',
        timeCents=600, deliveryPickupCents=100, fuelCents=20, damageWaiverCents=84,
        environmentalCents=12, costCents=816, markupBps=2500, markupCents=204, sellCents=1020,
        rounding='half-up-integer-cents')
    return {'version': 'external-labor-export-v1', 'source_sha256': base['source_sha256'],
        'request_sha256': _hash(request), 'provider': {'id': 'fictional', 'revision': '1', 'source': 'fictitious'},
        'standards': [dict(common, id='standard-' + op['id'], target_id=op['id'],
                           unit=op['unit'], person_hours_per_unit='2') for op in request['operations']],
        'labor_prices': labor, 'rental_prices': [rental]}


def test_exported_receipts_apply_productivity_and_do_not_double_extend_hours_or_rental():
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m)
    for operation in req['operations']: operation['person_hours_per_unit'] = '999'
    export = receipt_export_for(m, req)
    original_request, original_export = deepcopy(req), deepcopy(export)
    report = generate_receipted_labor_report(m, req, export)
    doc = report.to_dict()
    assert [row['cents'] for row in doc['work']] == [400, 400]
    assert len(doc['rentals']) == 1 and doc['rentals'][0]['cents'] == 1020
    assert all(row['status'] == 'provider-reported' for row in doc['work'] + doc['rentals'])
    assert req == original_request and export == original_export
    assert doc['engine_report']['work'][0]['pricing_unresolved']
    assert doc['engine_report']['work'][0]['nonpricing_unresolved'] == []
    assert doc['engine_report']['work'][0]['operation']['person_hours_per_unit'] == '2'
    assert '1020' in report.to_csv()
    report.validate_against(m, req, export)


@pytest.mark.parametrize('missing', ['price_basis', 'currency', 'revision'])
def test_missing_external_price_contract_remains_unresolved(missing):
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    export['labor_prices'][0].pop(missing)
    row = generate_receipted_labor_report(m, req, export).to_dict()['work'][0]
    assert row['status'] == 'unresolved' and row['cents'] is None
    assert missing in ' '.join(row['unresolved'])


def test_missing_export_version_and_productivity_do_not_fall_back_to_request_defaults():
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    export.pop('version'); export['standards'] = []
    doc = generate_receipted_labor_report(m, req, export).to_dict()
    assert all(row['cents'] is None for row in doc['work'] + doc['rentals'])
    assert doc['engine_report']['work'][0]['base_person_hours'] is None
    assert 'version missing' in ' '.join(doc['unresolved'])


@pytest.mark.parametrize('field,value', [('costCents', 817), ('markupCents', 205), ('sellCents', 1021),
                                        ('timeCents', 601), ('coverageDays', '1'), ('durationDays', '1')])
def test_external_rental_arithmetic_and_duration_are_checked(field, value):
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    export['rental_prices'][0][field] = value
    with pytest.raises(ContractError): generate_receipted_labor_report(m, req, export)


def test_total_hours_receipt_cannot_be_multiplied_or_rebound_to_another_crew():
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    export['labor_prices'][1]['amount_cents'] = 1600
    with pytest.raises(ContractError, match='declared basis'): generate_receipted_labor_report(m, req, export)
    export = receipt_export_for(m, req); export['labor_prices'][0]['work_sha256'] = 'stale'
    with pytest.raises(ContractError, match='work/crew'): generate_receipted_labor_report(m, req, export)


def test_local_calendar_vendor_contract_is_not_silently_elapsed_24_hour_billing():
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    export['rental_prices'][0]['duration_basis'] = 'local-calendar-days'
    row = generate_receipted_labor_report(m, req, export).to_dict()['rentals'][0]
    assert row['cents'] is None and 'timezone contract' in ' '.join(row['unresolved'])


def test_external_receipt_missing_inputs_and_provider_tiers_are_preserved():
    from oabm.labor import generate_receipted_labor_report
    m = model(); req = request_for(m); export = receipt_export_for(m, req)
    rental = export['rental_prices'][0]
    rental.update(status='unresolved', missing_inputs=['vendor fee basis not exported'])
    doc = generate_receipted_labor_report(m, req, export).to_dict()
    assert doc['rentals'][0]['cents'] is None
    assert doc['rentals'][0]['receipt']['tier_coverage_days'] == {'day': '1', 'week': '7', 'month': '28'}
    assert 'vendor fee basis not exported' in doc['rentals'][0]['unresolved']


def test_bounded_local_export_reader_rejects_duplicate_keys(tmp_path):
    from oabm.labor import read_labor_json
    path = tmp_path / 'fictional-export.json'; path.write_text('{"version":1,"version":2}')
    with pytest.raises(ContractError, match='duplicate'): read_labor_json(path)


def test_export_reader_rejects_fifo_nonblocking_before_stream_read(tmp_path, monkeypatch):
    import os
    from oabm.pipeline import as_built_admission as admission
    from oabm.labor import read_labor_json
    if not hasattr(os, 'mkfifo'):
        pytest.skip('FIFO admission case requires POSIX')
    path = tmp_path / 'receipt.pipe'; os.mkfifo(path)
    opened, closed = [], []
    real_open, real_close = admission.os.open, admission.os.close
    def checked_open(path, flags):
        assert flags & os.O_NONBLOCK
        descriptor = real_open(path, flags); opened.append(descriptor)
        return descriptor
    def checked_close(descriptor):
        closed.append(descriptor); return real_close(descriptor)
    def must_not_read(*args, **kwargs):
        raise AssertionError('special file reached stream read')
    monkeypatch.setattr(admission.os, 'open', checked_open)
    monkeypatch.setattr(admission.os, 'close', checked_close)
    monkeypatch.setattr(admission.os, 'fdopen', must_not_read)
    with pytest.raises(ContractError, match='regular bounded input file'): read_labor_json(path)
    assert opened and closed == opened


def test_export_reader_rejects_device_descriptor_before_stream_read(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from oabm.pipeline import as_built_admission as admission
    from oabm.labor import read_labor_json
    path = tmp_path / 'fictional.json'; path.write_text('{}')
    # Exercise descriptor admission without opening an actual device.
    monkeypatch.setattr(admission.os, 'fstat', lambda descriptor: SimpleNamespace(st_mode=stat.S_IFCHR, st_size=0))
    def must_not_read(*args, **kwargs):
        raise AssertionError('device descriptor reached stream read')
    monkeypatch.setattr(admission.os, 'fdopen', must_not_read)
    with pytest.raises(ContractError, match='regular bounded input file'): read_labor_json(path)


def test_export_reader_shared_structure_scan_runs_before_json_parser(tmp_path, monkeypatch):
    from oabm.labor import read_labor_json
    import oabm.labor.receipts as receipts
    from oabm.routing.physical_capture import MAX_DEPTH, MAX_NODES
    from oabm.pipeline import as_built_admission  # Load shared reader before parser guard.
    def must_not_parse(*args, **kwargs):
        raise AssertionError('over-budget export reached JSON parser')
    monkeypatch.setattr(receipts.json, 'loads', must_not_parse)
    path = tmp_path / 'over-structure.json'
    for raw in ('[' * (MAX_DEPTH + 1) + ']' * (MAX_DEPTH + 1),
                '[' + ','.join('0' for _ in range(MAX_NODES + 1)) + ']',
                '{"number":' + '1' * 129 + '}'):
        path.write_text(raw)
        with pytest.raises(ContractError, match='admission'): read_labor_json(path)
