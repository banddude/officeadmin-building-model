"""Packing defaults preserve the presentation-specific legacy behavior."""
import json
import shutil
from pathlib import Path
import pytest
from oabm.drawings.schedule_set import VERSION, create_panel_schedule_sheet_set, main
from oabm.drawings import production_set
from test_drafted_panel_schedules import inputs


def packet_count(out):
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    return len(manifest['sheet_order']), manifest['layout']['packing']


@pytest.mark.parametrize('settings', [{}, {'text_height_mm': 2.5}])
def test_standalone_drafted_omission_and_explicit_packing(tmp_path, settings):
    paths = inputs(tmp_path, count=12, layout=settings)
    implicit = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path/'implicit')
    paths[3].write_text(json.dumps({'version': VERSION, 'layout': {**settings, 'packing': 'compact'}}))
    compact = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path/'compact')
    paths[3].write_text(json.dumps({'version': VERSION, 'layout': {**settings, 'packing': 'one_table_per_sheet'}}))
    one = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path/'one')
    assert packet_count(implicit) == packet_count(compact)
    assert packet_count(implicit)[1] == 'compact'
    assert packet_count(one)[0] > packet_count(compact)[0]


@pytest.mark.parametrize('settings', [{}, {'text_height_mm': 2.5}])
def test_cli_drafted_omission_and_explicit_packing(tmp_path, settings):
    paths = inputs(tmp_path, count=12, layout=settings)
    assert main([*(str(p) for p in paths), str(tmp_path/'implicit')]) == 0
    paths[3].write_text(json.dumps({'version': VERSION, 'layout': {**settings, 'packing': 'one_table_per_sheet'}}))
    assert main([*(str(p) for p in paths), str(tmp_path/'one')]) == 0
    # CLI emits the delivery subdirectory.
    assert packet_count(tmp_path/'implicit'/'delivery')[1] == 'compact'
    assert packet_count(tmp_path/'implicit'/'delivery')[0] < packet_count(tmp_path/'one'/'delivery')[0]


@pytest.mark.parametrize('settings', [{}, {'text_height_mm': 2.5}])
def test_production_ledger_omission_is_one_table_per_sheet(tmp_path, settings):
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/production_set/compact'
    folder = tmp_path/'inputs'; shutil.copytree(fixture, folder)
    (folder/'settings.json').write_text(json.dumps({'version': VERSION, 'layout': settings}))
    out = production_set.create_production_sheet_set(folder/'production.json', output_directory=tmp_path/'out')
    layout = json.loads((out/'derived/schedule-layout.json').read_text())
    sections = json.loads((out/'derived/schedule-sections.json').read_text())
    assert layout['packing'] == 'one_table_per_sheet'
    assert len({section['sheet_id'] for section in sections}) == len(sections)


def test_direct_drafted_producer_omitted_layout_is_compact():
    from oabm.drawings.schedule_layout import ScheduleLayout, draft_panel_schedule_pages
    from oabm.drawings.schedule_set import panel_schedule_drawing_set
    from oabm.drawings.company_sheets import resolve_company_sheet_set
    from test_drafted_panel_schedules import source, ROOT
    report, drawing = panel_schedule_drawing_set(source())
    sheet = resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json', ROOT/'fixtures/schedule_sheets/v1/job.json').sheets[0]
    implicit = draft_panel_schedule_pages(report, drawing, sheet)
    compact = draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(packing='compact'))
    one = draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(packing='one_table_per_sheet'))
    assert len(implicit) == len(compact) < len(one)
