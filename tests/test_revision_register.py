"""Readable register preserves the canonical diff, binding and presentation boundaries."""
import csv
from dataclasses import replace
import io
import json
import math

import pytest

from oabm.model import ContractError
from oabm.reports.revision_register import (
    generate_revision_register, validate_revision_register, write_revision_register, main,
)
from test_physical_canonical_revisions import mixed_snapshots
from test_physical_route_revisions import detour
from physical_route_fixture import make_physical_model


def rows(report, name):
    return list(csv.DictReader(io.StringIO(report.files()[name])))


def test_actual_room_register_is_complete_deterministic_and_not_a_second_takeoff(tmp_path):
    before, after = mixed_snapshots()
    frozen = [model.to_json() for model in (before,after)]
    report = generate_revision_register(before,after)
    data = report.to_dict()
    assert data['diff']['changed_count'] == 53
    assert len(data['diff']['changes']) == 53
    changes = rows(report,'changes.csv')
    assert len({row['change_id'] for row in changes}) == 53
    assert all(row['before_source_sha256'] == data['sources']['before']['sha256'] for row in changes)
    quantities = rows(report,'quantity-deltas.csv')
    assert len(quantities) == len(data['diff']['quantity_deltas'])
    assert len({row['delta_id'] for row in quantities}) == len(quantities)
    physical = next(row for row in data['physical_measurements'] if row['route_id'] == 'route:physical')
    exact = physical['delta']['length']
    assert exact['straight_ticks'] == 100000000
    assert exact['pi_coefficient_ticks'] == {'numerator':100000000,'denominator':1}
    assert exact['total_m_approx'] == pytest.approx(.1+.1*math.pi)
    assert 'Route length changes at a glance' in report.files()['index.html']
    assert 'same delta IDs, not additional quantities' in report.files()['index.html']
    assert any(json.loads(row['delta_json']) == .5 for row in quantities if row['category'] == 'route_length')
    assert any(row['before_json'] == 'null' and row['status'] == 'added' for row in quantities)
    assert any(row['after_json'] == 'null' and row['status'] == 'removed' for row in quantities)
    assert [model.to_json() for model in (before,after)] == frozen
    assert validate_revision_register(report.to_json()).files() == report.files()
    report.validate_against_models(before,after)
    assert generate_revision_register(before,after).files() == report.files()
    out = write_revision_register(report,tmp_path/'register')
    assert {path.name:path.read_text() for path in out.iterdir()} == report.files()
    assert '53 changed objects' in (out/'index.html').read_text()


def test_equal_length_moved_path_remains_visible_without_quantity_change():
    report = generate_revision_register(detour(1),detour(-1))
    assert report.to_dict()['diff']['changes']
    assert not rows(report,'quantity-deltas.csv')
    assert report.to_dict()['physical_measurements'][0]['path_changed'] is True
    assert 'No changes in this section.' in report.files()['index.html']


@pytest.mark.parametrize('owner', ['route','joint','segment'])
def test_provenance_order_only_has_consistent_canonical_status_and_explicit_snapshot_evidence(owner):
    model = make_physical_model()
    route = model.routes[0]
    entity = route if owner == 'route' else route.joints[0] if owner == 'joint' else route.segments[0]
    records = (entity.provenance[0],replace(entity.provenance[0],method='second source'))
    def snapshot(provenance):
        revised = replace(entity,provenance=provenance)
        physical = revised if owner == 'route' else (
            replace(route,joints=(revised,*route.joints[1:])) if owner == 'joint' else
            replace(route,segments=(revised,*route.segments[1:])))
        return replace(model,routes=(physical,))
    before,after = snapshot(records),snapshot(tuple(reversed(records)))
    report = generate_revision_register(before,after)
    data = report.to_dict()
    assert data['diff']['changes'] == [] and data['diff']['quantity_deltas'] == []
    assert not rows(report,'changes.csv') and not rows(report,'quantity-deltas.csv')
    measured, = data['physical_measurements']
    assert measured['status'] == 'unchanged' and measured['change_classes'] == []
    assert measured['snapshot_status'] == 'changed'
    assert measured['snapshot_field_change_categories'] == ['provenance']
    assert measured['path_changed'] is False
    assert measured['delta']['length']['total_m_approx'] == 0
    assert data['sources']['before']['sha256'] != data['sources']['after']['sha256']
    html = report.files()['index.html']
    assert '0 changed objects' in html
    assert 'Canonical status' in html and 'Ordered snapshot status' in html
    assert 'provenance order alone' in html
    assert validate_revision_register(report.to_json()).to_json() == report.to_json()


def test_unchanged_and_negative_deltas_and_external_source_binding():
    before,after = detour(1),detour(-1)
    unchanged = generate_revision_register(before,before)
    assert not rows(unchanged,'changes.csv') and not rows(unchanged,'quantity-deltas.csv')
    with pytest.raises(ContractError,match='supplied snapshots'):
        unchanged.validate_against_models(before,after)
    from oabm.reports.revision_register import _csv
    assert list(csv.reader(io.StringIO(_csv(['number'],[[-.5]]))))[1] == ['-0.5']


@pytest.mark.parametrize('payload', ['{}','[]','{"sources":{},"sources":{}}','{"bad":NaN}'])
def test_malformed_register_refuses(payload):
    with pytest.raises(ContractError):
        validate_revision_register(payload)


@pytest.mark.parametrize('target', ['hash','diff','measurement','limitation','extra'])
def test_rehashed_or_changed_report_is_not_trusted(target):
    before,after = detour(1),detour(-1)
    data = generate_revision_register(before,after).to_dict()
    if target == 'hash':
        data['sources']['before']['sha256'] = '0'*64
    elif target == 'diff':
        data['diff']['changed_count'] = 0
    elif target == 'measurement':
        data['physical_measurements'][0]['path_changed'] = False
    elif target == 'limitation':
        data['limitations'] = []
    else:
        data['extra'] = 'unbound'
    with pytest.raises(ContractError):
        validate_revision_register(json.dumps(data))


def test_html_is_escaped_and_csv_text_cannot_become_formulas():
    before = detour(1)
    hostile = '=HYPERLINK("https://example.invalid")<script>alert(1)</script>'
    before = replace(before,routes=(replace(before.routes[0],name=hostile),))
    route = before.routes[0]
    after = replace(before,routes=(replace(route,name='<img src=x onerror=alert(1)>'),))
    report = generate_revision_register(before,after)
    html = report.files()['index.html']
    assert '<script>' not in html and '<img ' not in html
    assert '&lt;script&gt;' in html and '&lt;img ' in html
    # Exercise all text cells, including leading whitespace/BOM/CR.
    from oabm.reports.revision_register import _csv
    for value in ('=1','+2','-3','@SUM(1)',' \ufeff=1','\ttext','\rtext'):
        result = list(csv.reader(io.StringIO(_csv(['text'],[[value]]))))[1][0]
        assert result.startswith("'")


def test_fresh_directory_refusal_preserves_existing_and_symlink(tmp_path):
    report = generate_revision_register(detour(1),detour(-1))
    existing = tmp_path/'existing'; existing.mkdir()
    (existing/'keep').write_text('untouched')
    link = tmp_path/'link'; link.symlink_to(existing,target_is_directory=True)
    for path in (existing,link):
        with pytest.raises(FileExistsError):
            write_revision_register(report,path)
    assert list(existing.iterdir()) == [existing/'keep']
    assert (existing/'keep').read_text() == 'untouched'


def test_output_bounds_refuse_before_any_directory_is_created(tmp_path,monkeypatch):
    import oabm.reports.revision_register as module
    report = generate_revision_register(detour(1),detour(-1))
    monkeypatch.setattr(module,'PRESENTATION_BYTES',100)
    with pytest.raises(ContractError,match='no rows truncated'):
        write_revision_register(report,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_cli_reads_full_snapshots_and_writes_offline_files(tmp_path):
    before,after = detour(1),detour(-1)
    for name,model in (('before',before),('after',after)):
        (tmp_path/f'{name}.json').write_text(model.to_json())
    assert main([str(tmp_path/'before.json'),str(tmp_path/'after.json'),str(tmp_path/'out')]) == 0
    assert sorted(path.name for path in (tmp_path/'out').iterdir()) == ['changes.csv','index.html','quantity-deltas.csv','register.json']
