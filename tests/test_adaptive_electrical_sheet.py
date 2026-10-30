"""Content-driven choice reuses the real sheet producers and their guards."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pytest

from oabm.drawings.adaptive_electrical_sheet import plan_adaptive_electrical_sheets
from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.electrical_sheet import build_electrical_sheet_bodies
from oabm.drawings.production_set import create_production_sheet_set
from oabm.model import BuildingModel

sys.path.insert(0, str(Path(__file__).parents[1] / 'examples'))
from reference_production_set import prepare


def inputs(tmp_path, variant='residential', pdf=False):
    path = prepare(tmp_path / 'inputs', variant=variant, pdf=pdf)
    data = json.loads(path.read_text())
    sheet = next(s for s in resolve_company_sheet_set(path.parent / 'profile.json', path.parent / 'job.json').sheets if s.id == 'E3.1')
    l, t, r, b = sheet.drawing_bounds_mm
    height = b - t - 24
    candidate = {'id': 'shared-bottom', 'single_line_regions': {
        'diagram': [l, t, r - 178, t + height * .5],
        'notes': [r - 170, t, r, t + height * .5],
        'feeder_register': [l, t + height * .5 + 8, r, t + height * .69]},
        'panel_region_mm': [l, t + height * .69 + 8, r, b - 24]}
    config = data['single_line']['composition']
    config.update(version='electrical-sheet-regions-v3', panel_placement='auto', layout_candidates=[candidate])
    path.write_text(json.dumps(data))
    return path, sheet, config, BuildingModel.from_json((path.parent / 'model.json').read_bytes())


def plan(sheet, config, model):
    return plan_adaptive_electrical_sheets(model, sheet,
        single_line_regions=config['single_line_regions'], layout_candidates=config['layout_candidates'],
        panel_width_mm=config['panel_width_mm'], panel_policy=config['panel_policy'],
        reserved_sheet_ids=['G0.0', 'E1.0', 'ED1'])


def test_residential_selects_one_sheet_without_changing_facts(tmp_path):
    _, sheet, config, model = inputs(tmp_path)
    before = model.to_json()
    chosen, receipt = plan(sheet, config, model)
    assert chosen.sheet_ids == ('E3.1',)
    assert chosen.sheet_kinds == ('combined',)
    assert receipt['selected_candidate_id'] == 'shared-bottom'
    assert [a['sheet_count'] for a in receipt['attempts']] == [2, 1]
    assert receipt['selected_plan_fingerprint'] == chosen.fingerprint
    bodies, mapping = build_electrical_sheet_bodies(model, [sheet], plan=chosen)
    assert 'F003' in bodies['E3.1']
    assert {row['source_id'] for p in mapping['panel_pages'] for row in p['row_map']} == {'MAIN', 'F003'}
    assert model.to_json() == before


def test_no_panel_rows_keeps_baseline_on_equal_page_count(tmp_path):
    _, sheet, config, model = inputs(tmp_path, 'reference')
    chosen, receipt = plan(sheet, config, model)
    assert receipt['selected_candidate_id'] == 'continuation'
    assert chosen.sheet_ids == ('E3.1',)
    assert all(a.get('sheet_count', 1) >= 1 for a in receipt['attempts'])


def test_equal_compact_candidates_preserve_input_order(tmp_path):
    _, sheet, config, model = inputs(tmp_path)
    second = copy.deepcopy(config['layout_candidates'][0])
    second['id'] = 'another-shared-bottom'
    config['layout_candidates'].append(second)
    chosen, receipt = plan(sheet, config, model)
    assert receipt['selected_candidate_id'] == 'shared-bottom'
    assert [a['sheet_count'] for a in receipt['attempts']] == [2, 1, 1]


def test_commercial_does_not_force_shared_layout_when_it_saves_no_page(tmp_path):
    _, sheet, config, model = inputs(tmp_path, 'commercial')
    chosen, receipt = plan(sheet, config, model)
    assert receipt['selected_candidate_id'] == 'continuation'
    assert len(chosen.sld_layout['pages']) == 4
    assert len(chosen.sheet_ids) == 5
    assert [a['sheet_count'] for a in receipt['attempts']] == [5, 5]
    bodies, mapping = build_electrical_sheet_bodies(model,
        tuple(replace(sheet, id=identity) for identity in chosen.sheet_ids), plan=chosen)
    assert len(mapping['single_line']['topology']['equipment']) == 32
    assert len(mapping['single_line']['topology']['connections']) == 31
    assert all(f'P{n:02d}' in ''.join(bodies.values()) for n in range(1, 28))


def test_small_valid_region_rejected_without_losing_baseline(tmp_path):
    _, sheet, config, model = inputs(tmp_path)
    candidate = config['layout_candidates'][0]
    l, t, r, b = candidate['panel_region_mm']
    candidate['panel_region_mm'] = [l, b - 10, r, b]
    chosen, receipt = plan(sheet, config, model)
    assert chosen.sheet_ids == ('E3.1', 'E3.2')
    assert receipt['selected_candidate_id'] == 'continuation'
    assert receipt['attempts'][1]['status'] == 'content_does_not_fit'
    assert receipt['attempts'][1]['reason']


@pytest.mark.parametrize('problem', ['overlap', 'nan', 'duplicate', 'empty', 'too_many', 'extra', 'reserved_id'])
def test_invalid_presets_refuse_instead_of_silent_fallback(tmp_path, problem):
    _, sheet, config, model = inputs(tmp_path)
    candidates = config['layout_candidates']
    if problem == 'overlap': candidates[0]['panel_region_mm'] = candidates[0]['single_line_regions']['diagram']
    elif problem == 'nan': candidates[0]['panel_region_mm'][0] = float('nan')
    elif problem == 'duplicate': candidates.append(copy.deepcopy(candidates[0]))
    elif problem == 'empty': candidates.clear()
    elif problem == 'too_many': candidates *= 9
    elif problem == 'extra': candidates[0]['arbitrary'] = True
    elif problem == 'reserved_id': candidates[0]['id'] = 'CONTINUATION'
    with pytest.raises(ValueError): plan(sheet, config, model)


def test_invalid_baseline_is_not_bypassed_by_compact_layout(tmp_path):
    _, sheet, config, model = inputs(tmp_path)
    config['single_line_regions']['notes'] = config['single_line_regions']['diagram']
    with pytest.raises(ValueError): plan(sheet, config, model)


def test_adaptive_coordinator_reconciles_index_and_is_repeatable(tmp_path):
    path, _, _, _ = inputs(tmp_path)
    first = create_production_sheet_set(path, output_directory=tmp_path / 'first')
    second = create_production_sheet_set(path, output_directory=tmp_path / 'second')
    tree = lambda p: {str(f.relative_to(p)): f.read_bytes() for f in p.rglob('*') if f.is_file()}
    assert tree(first) == tree(second)
    manifest = json.loads((first / 'PRODUCTION-MANIFEST.json').read_text())
    assert manifest['sheet_order'] == ['G0.0', 'E1.0', 'E3.1', 'ED1']
    assert [s['id'] for s in manifest['cover_index']] == manifest['sheet_order']
    receipt = json.loads((first / 'derived/electrical-layout-selection.json').read_text())
    assert manifest['electrical_composition']['layout_selection'] == receipt
    assert receipt['selected_candidate_id'] == 'shared-bottom'
    for row in manifest['files']:
        assert hashlib.sha256((first / row['path']).read_bytes()).hexdigest() == row['sha256']


def test_adaptive_actual_pdf_has_four_pages_and_no_stale_continuation(tmp_path):
    from oabm.drawings.sheet_pdf import _dependencies
    try: _dependencies()
    except ImportError as exc: pytest.skip(str(exc))
    from pypdf import PdfReader
    path, _, _, _ = inputs(tmp_path, pdf=True)
    output = create_production_sheet_set(path, output_directory=tmp_path / 'output')
    reader = PdfReader(output / 'company/delivery/sheet-set.pdf')
    assert len(reader.pages) == 4
    assert all(abs(float(p.mediabox.width) - 2592) < .01 and abs(float(p.mediabox.height) - 1728) < .01 for p in reader.pages)
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'E3.2' not in text
    assert all(source in text for source in ('F001', 'F002', 'F003'))
