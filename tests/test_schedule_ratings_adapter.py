"""Synthetic shared-fact presentation regression tests."""
from dataclasses import replace
import pytest
from oabm.drawings.schedule_ratings import panel_ratings, circuit_ratings
from oabm.drawings.schedule_set import panel_schedule_drawing_set
from oabm.drawings.schedule_layout import draft_panel_schedule_pages
from oabm.drawings.company_sheets import resolve_company_sheet_set
from test_drafted_panel_schedules import source, ROOT


def test_supplied_ratings_render_without_inventing_main():
    model = source(2)
    panel = model.electrical_equipment[0]
    circuit = model.circuits[0]
    model = replace(model,
        electrical_equipment=(replace(panel, attributes={'single_line_drafting': {'bus_current_a': 100}}), *model.electrical_equipment[1:]),
        circuits=(replace(circuit, attributes={'single_line_drafting': {'protection': {'kind': 'breaker', 'rating_a': 60, 'poles': circuit.poles}}}), *model.circuits[1:]))
    report, drawing = panel_schedule_drawing_set(model)
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    pages = draft_panel_schedule_pages(report, drawing, sheet)
    text = ' '.join(p.text or '' for page in pages for p in page.drawing_set.views[0].primitives if p.kind == 'text')
    assert 'BUS: 100 A' in text and 'MAIN: TBD' in text and 'BREAKER 60 A' in text


def test_conflicts_and_invalid_shared_values_rejected():
    panel = {'id': 'equipment:test', 'attributes': {'single_line_drafting': {'bus_current_a': 100}}}
    assert panel_ratings(panel, {'bus': '100 A'})['bus'] == '100 A'
    with pytest.raises(ValueError, match='conflicts'):
        panel_ratings(panel, {'bus': '200 A'})
    panel['attributes']['single_line_drafting']['bus_current_a'] = -1
    with pytest.raises(ValueError, match='positive finite'):
        panel_ratings(panel, {})
    circuit = {'id': 'circuit:test', 'poles': 2, 'attributes': {'single_line_drafting': {'protection': {'kind': 'breaker', 'rating_a': 60, 'poles': 2}}}}
    assert circuit_ratings(circuit, {})['ocpd'] == 'BREAKER 60 A / 2P'
    assert circuit_ratings(circuit, {'ocpd': '60 A'})['ocpd'] == 'BREAKER 60 A / 2P'
    with pytest.raises(ValueError, match='conflicts'):
        circuit_ratings(circuit, {'ocpd': '20 A'})
    circuit['attributes']['single_line_drafting']['protection']['poles'] = 3
    with pytest.raises(ValueError, match='conflicts with canonical'):
        circuit_ratings(circuit, {})


def test_unknown_and_non_power_facts_are_not_invented():
    panel = {'id': 'equipment:test', 'attributes': {'single_line_drafting': {}}}
    assert panel_ratings(panel, {}) == {}
    circuit = {'id': 'circuit:test', 'poles': None, 'attributes': {'single_line_drafting': {}}}
    assert circuit_ratings(circuit, {}) == {}
    circuit['attributes']['single_line_drafting'] = {'connection_kind': 'neutral', 'protection': {'kind': 'breaker', 'rating_a': 60}}
    with pytest.raises(ValueError, match='neutral/grounding'):
        circuit_ratings(circuit, {})


def test_shared_facts_survive_full_packet_export(tmp_path):
    import json
    import pypdf
    from oabm.drawings.schedule_set import VERSION, create_panel_schedule_sheet_set
    model = source(4)
    panel, circuit = model.electrical_equipment[0], model.circuits[0]
    model = replace(model,
        electrical_equipment=(replace(panel, attributes={'single_line_drafting': {'bus_current_a': 100}}), *model.electrical_equipment[1:]),
        circuits=(replace(circuit, attributes={'single_line_drafting': {'protection': {'kind': 'breaker', 'rating_a': 60, 'poles': 1}}}), *model.circuits[1:]))
    path = tmp_path / 'model.json'; path.write_text(model.to_json())
    settings = tmp_path / 'settings.json'; settings.write_text(json.dumps({'version': VERSION, 'layout': {}}))
    out = create_panel_schedule_sheet_set(path, ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json', settings, output_directory=tmp_path / 'packet')
    text = ''.join(page.extract_text() for page in pypdf.PdfReader(out / 'sheet-set.pdf').pages)
    assert 'BUS: 100 A' in text and 'MAIN: TBD' in text and 'BREAKER 60 A' in text
