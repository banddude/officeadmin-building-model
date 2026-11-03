"""Retain source-bound labor/receipt reports; default inputs are all fictitious.

No OA connection or private source. Run only after the coordinated execution hold
is released. Generated outputs still require review and do not close issue #234.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import html
import json
import os
from pathlib import Path
import string
import tempfile

from oabm.labor import (contribution_inventory, generate_receipted_labor_report,
                        read_labor_json)
from oabm.labor.core import _hash
from oabm.model import BuildingModel, ElectricalDevice, Level, Point3, Pose, Provenance


def fictional_inputs():
    """Two independent original EA contributions, one shared lift occurrence."""
    evidence = (Provenance(source_kind='synthetic', source_id='fictional:labor-demo',
        method='explicit fictitious model and work conditions', derivation='user'),)
    model = BuildingModel(model_id='model:fictional-labor-demo',
        name='FICTIONAL labor receipt demonstration',
        levels=(Level(id='level:work-floor', elevation_m=0., provenance=evidence),),
        electrical_devices=tuple(ElectricalDevice(id=f'device:{name}',
            device_type='fictional-luminaire', level_id='level:work-floor',
            pose=Pose(position=Point3(x=float(index), y=0., z=4. + index)), provenance=evidence)
            for index, name in enumerate(('a', 'b'))))
    inventory = contribution_inventory(model)
    selectors = {row['basis']['source_entity_ids'][0]: row['id'] for row in inventory['contributions']
                 if row['basis']['category'] == 'device'}
    work = []
    for index, name in enumerate(('a', 'b')):
        work.append({'id': f'work:{name}', 'contribution_id': selectors[f'device:{name}'],
            'operation_id': 'install-fictional-luminaire', 'fraction': '1', 'phase': 'rough',
            'mobilization': 'visit:one', 'evidence': 'Fictitious explicit installation scope; not field evidence.',
            'conditions': {'occupied': 'yes', 'hillside': 'no', 'no-truck': 'no', 'height': 'high'},
            'height_basis': {'work_surface': 'level:work-floor; supplied flat floor at Z=0 m',
                'evidence': 'Fictitious measured relative install height, not route endpoints.',
                'method': 'measured-split', 'min_height_m': str(4 + index), 'max_height_m': str(4 + index)},
            'crew': [{'id': 'crew:composition', 'role': 'fictional-installer', 'count': 2,
                      'selection': 'explicit-regular', 'rate_id': None}],
            'windows': [{'start': '2026-11-01T08:00:00-08:00', 'end': '2026-11-01T12:00:00-08:00'}]})
    request = {'version': 'labor-request-v1', 'source_sha256': inventory['source_sha256'], 'groups': None,
        'policy': {'id': 'fictional-policy', 'revision': '1', 'source': 'FICTITIOUS DEMONSTRATION ONLY'},
        'operations': [{'id': 'install-fictional-luminaire', 'category': 'device', 'item_type': 'fictional-luminaire',
            'unit': 'ea', 'exclusive_family': 'fixture-install', 'person_hours_per_unit': None}],
        'rules': [{'id': f'rule:{condition}', 'condition': condition, 'value': value,
            'multiplier': '2' if condition == 'occupied' else '1',
            'resource_kind': 'lift' if condition == 'height' else None,
            'height_min_m': '3' if condition == 'height' else None,
            'height_max_m': '6' if condition == 'height' else None}
            for condition, value in work[0]['conditions'].items()],
        'rates': [], 'work': work, 'rentals': [{'id': 'rental:shared-lift', 'kind': 'lift',
            'mobilization': 'visit:one', 'evidence': 'Fictitious explicit hire contract; retained overnight.',
            'capacity': 2, 'assignments': [row['id'] for row in work],
            'hire': '2026-11-01T08:00:00-08:00', 'off_hire': '2026-11-02T09:00:00-08:00',
            'rate_id': None, 'billing_quantum_hours': '24'}]}
    common = {'status': 'available', 'missing_inputs': [], 'revision': 'fictional-v1', 'source': 'FICTITIOUS EXPORTED RECEIPT'}
    labor_prices = [dict(common, id=f'price:{name}', target_id=row['id'], currency='TEST',
        work_sha256=_hash(row), cost_bucket='labor', unit='person-hour' if index == 0 else 'operation',
        price_basis='one-hour-unit' if index == 0 else 'total-hours-in-one-unit',
        quantity='4' if index == 0 else '1', unit_price_cents=100 if index == 0 else 400,
        amount_cents=400, priced_person_hours='4') for index, (name, row) in enumerate(zip(('a', 'b'), work))]
    occurrence = request['rentals'][0]
    rental_price = dict(common, id='price:shared-lift', target_id=occurrence['id'], currency='TEST',
        occurrence_sha256=_hash(occurrence), cost_bucket='rental', unit='occurrence',
        price_basis='provider-occurrence-total', quantity='1', hire=occurrence['hire'], off_hire=occurrence['off_hire'],
        priced_elapsed_hours='25', duration_basis='started-24-hour-days', durationDays='2',
        tier_coverage_days={'day': '1', 'week': '7', 'month': '28'},
        mix=[{'unit': 'day', 'quantity': 2, 'coverageDays': '2', 'timeCents': 600}], coverageDays='2',
        timeCents=600, deliveryPickupCents=100, fuelCents=20, damageWaiverCents=84, environmentalCents=12,
        costCents=816, markupBps=2500, markupCents=204, sellCents=1020, rounding='half-up-integer-cents')
    receipts = {'version': 'external-labor-export-v1', 'source_sha256': inventory['source_sha256'],
        'request_sha256': _hash(request), 'provider': {'id': 'fictional-provider', 'revision': '1', 'source': 'FICTITIOUS'},
        'standards': [dict(common, id='standard:fixture', target_id='install-fictional-luminaire',
                           unit='ea', person_hours_per_unit='2')],
        'labor_prices': labor_prices, 'rental_prices': [rental_price]}
    missing = deepcopy(receipts)
    missing['labor_prices'][0].update(status='unresolved', missing_inputs=['Fictitious crew rate not supplied'],
                                     unit_price_cents=None, amount_cents=None)
    return model, request, receipts, missing


def _pretty(document):
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + '\n'


def _cell(value):
    # Markdown summary is presentation only; source-bound JSON retains originals.
    text = str(value).replace('\r', ' ').replace('\n', ' ')
    # Escape Markdown first, then encode HTML, so generated numeric entities
    # (for example an apostrophe's &#x27;) are never escaped a second time.
    # HTML delimiters/quotes are handled by html.escape rather than backslashes.
    controls = set(string.punctuation) - {'&', ';', '<', '>', '"', "'"}
    escaped = ''.join('\\' + char if char in controls else char for char in text)
    return html.escape(escaped)


def retain_delivery(output, model, request, cases, *, fictional=False):
    """Fresh standalone delivery, kept outside any sealed production manifest."""
    root = Path(output).absolute()
    if root.exists() or root.is_symlink():
        raise FileExistsError('fresh labor output directory required')
    contents, summaries = {}, {}
    source = None
    for name, receipts in cases:
        if name not in ('complete', 'missing-input', 'supplied'):
            raise ValueError('unsupported example case name')
        if name in summaries:
            raise ValueError('duplicate example case name')
        report = generate_receipted_labor_report(model, request, receipts)
        doc = report.to_dict()
        if source is not None and source != report.source_model_json:
            raise ValueError('example cases must share one exact canonical source')
        source = report.source_model_json
        contents[f'{name}/request.json'] = report.request_json + '\n'
        contents[f'{name}/receipts.json'] = report.receipts_json + '\n'
        contents[f'{name}/labor.json'] = report.to_json() + '\n'
        contents[f'{name}/labor.csv'] = report.to_csv()
        summaries[name] = {'work': [{**{key: row[key] for key in ('id', 'status', 'currency', 'cents', 'unresolved')},
            'base_person_hours': engine['base_person_hours'], 'adjusted_person_hours': engine['adjusted_person_hours'],
            'price_basis': None if row['receipt'] is None else row['receipt'].get('price_basis'),
            'receipt': None if row['receipt'] is None else {key: row['receipt'][key] for key in ('id', 'revision', 'source')}}
            for row, engine in zip(doc['work'], doc['engine_report']['work'])],
            'rentals': [{**{key: row[key] for key in ('id', 'status', 'currency', 'cents', 'unresolved')},
                'occupancy_hours': engine['occupancy_hours'],
                'assigned_work': engine['occurrence']['assignments'],
                'receipt': None if row['receipt'] is None else {key: row['receipt'][key] for key in ('id', 'revision', 'source')}}
                for row, engine in zip(doc['rentals'], doc['engine_report']['rentals'])],
            'source_sha256': doc['source_sha256'], 'request_sha256': doc['request_sha256'],
            'receipt_export_sha256': doc['receipt_export_sha256'], 'unresolved': doc['unresolved'], 'provider': doc['provider']}
    if source is None:
        raise ValueError('at least one receipt case is required')
    if fictional:
        complete, missing = summaries['complete'], summaries['missing-input']
        if ([r['cents'] for r in complete['work']] != [400, 400] or
                [r['adjusted_person_hours'] for r in complete['work']] != ['4', '4'] or
                len(complete['rentals']) != 1 or complete['rentals'][0]['cents'] != 1020 or
                complete['rentals'][0]['occupancy_hours'] != '25' or
                missing['work'][0]['cents'] is not None or missing['work'][0]['status'] != 'unresolved'):
            raise ValueError('fictional labor demonstration does not match declared acceptance values')
    contents['model.json'] = source + '\n'
    summary = {'kind': 'labor-demonstration-summary-v1', 'fictional': fictional,
        'review_status': 'generated; actual output review pending', 'cases': summaries,
        'limitations': ['Proposed exported-receipt interchange; no live authenticated OA integration.',
            'Independent selected-scope labor model; do not combine with production-model quantities.',
            'Provider-reported prices; no wage eligibility, engineering or vendor rate verification.']}
    contents['SUMMARY.json'] = _pretty(summary)
    lines = ['# Labor receipt demonstration', '',
        'FICTIONAL DATA ONLY.' if fictional else 'Supplied exported receipts; source and provider claims require review.',
        'Generated output review is pending. This is not live OA integration.', '']
    for name, case in summaries.items():
        lines += [f'## {_cell(name)}', '',
                  f"Provider: {_cell(case['provider']['id'])}; revision: {_cell(case['provider']['revision'])}; source: {_cell(case['provider']['source'])}.", '',
                  '| Work | Base person-hours | Adjusted person-hours | Price basis | Amount | Status |',
                  '| --- | --- | --- | --- | --- | --- |']
        notes = []
        for row in case['work']:
            amount = 'UNPRICED' if row['cents'] is None else f"{row['cents']} cents {row['currency']}"
            lines.append('| ' + ' | '.join(_cell(v) for v in (row['id'], row['base_person_hours'],
                row['adjusted_person_hours'], row['price_basis'], amount, row['status'])) + ' |')
            for message in row['unresolved']: notes.append(f"Unresolved {_cell(row['id'])}: {_cell(message)}")
            if row['receipt'] is not None:
                notes.append(f"{_cell(row['id'])} receipt: {_cell(row['receipt']['id'])}; revision {_cell(row['receipt']['revision'])}; source {_cell(row['receipt']['source'])}.")
        for note in notes: lines += ['', note]
        for row in case['rentals']:
            amount = 'UNPRICED' if row['cents'] is None else f"{row['cents']} cents {_cell(row['currency'])}"
            lines += ['', f"Rental {_cell(row['id'])}: {amount}, charged once; {_cell(row['occupancy_hours'])} elapsed hire hours, "
                      f"shared by {_cell(', '.join(row['assigned_work']))}. Status: {_cell(row['status'])}."]
            lines += [f'Unresolved: {_cell(message)}' for message in row['unresolved']]
            if row['receipt'] is not None:
                lines += [f"Receipt: {_cell(row['receipt']['id'])}; revision {_cell(row['receipt']['revision'])}; source {_cell(row['receipt']['source'])}."]
        lines += ['', *[f'Envelope unresolved: {_cell(message)}' for message in case['unresolved']]]
        lines += ['', f"Source SHA256: `{case['source_sha256']}`", '']
    contents['SUMMARY.md'] = '\n'.join(lines) + '\n'
    contents['README.txt'] = ('Independent labor receipt demonstration. Read SUMMARY.md and SUMMARY.json.\n'
        'Each case retains the exact source-bound request, proposed external receipt, JSON and CSV report.\n'
        'The same source model is retained once. Unknown price inputs remain UNPRICED, never zero certainty.\n'
        'Do not combine this model or amounts with any production-sheet or as-built companion.\n'
        'Generated output review, authenticated export integration and issue #234 acceptance remain pending.\n')
    manifest = {'kind': 'labor-demonstration-manifest-v1', 'fictional': fictional,
        'source_sha256': next(iter(summaries.values()))['source_sha256'],
        'review_status': summary['review_status'], 'files': {name: {'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'bytes': len(text.encode('utf-8'))} for name, text in sorted(contents.items())}}
    # Match the existing as-built staged/fresh-attempt publication pattern.
    # All writes finish in a temporary sibling before reserving the final root.
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.labor-', dir=root.parent) as temporary:
        stage = Path(temporary) / 'delivery'; stage.mkdir()
        for name, text in contents.items():
            path = stage / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        (stage / 'MANIFEST.json').write_text(_pretty(manifest), encoding='utf-8')
        root.mkdir()  # Exclusive reservation; never overwrite another attempt.
        try:
            os.rename(stage, root / 'delivery')
        except BaseException:
            root.rmdir()  # Only our still-empty reservation; no recursive cleanup.
            raise
    return manifest


def create_synthetic_delivery(output):
    model, request, complete, missing = fictional_inputs()
    return retain_delivery(output, model, request, [('complete', complete), ('missing-input', missing)], fictional=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='fresh output directory')
    parser.add_argument('--model', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    supplied = (args.model, args.request, args.receipts)
    if any(supplied) and not all(supplied):
        parser.error('--model, --request and --receipts must be supplied together')
    if all(supplied):
        model = BuildingModel.from_dict(read_labor_json(args.model))
        manifest = retain_delivery(args.output, model, read_labor_json(args.request),
                                   [('supplied', read_labor_json(args.receipts))])
    else:
        manifest = create_synthetic_delivery(args.output)
    print(_pretty(manifest), end='')


if __name__ == '__main__':
    main()
