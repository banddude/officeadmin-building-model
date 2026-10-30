"""Synthetic plan-style circuit schedule proofs; no raster or customer data."""
import importlib.util
import io
import json
from dataclasses import replace
from pathlib import Path
import xml.etree.ElementTree as ET

import pypdf
import pytest

from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.schedule_layout import ScheduleLayout, draft_panel_schedule_pages
from oabm.drawings.schedule_set import VERSION, create_panel_schedule_sheet_set, panel_schedule_drawing_set

ROOT = Path(__file__).resolve().parents[1]
module = importlib.util.spec_from_file_location('draft_example', ROOT / 'examples/panel_schedule_sheets.py')
example = importlib.util.module_from_spec(module)
module.loader.exec_module(example)


def source(count=12, positioned=False):
    model = example.synthetic_two_panel_model(count)
    circuits = []
    for i, circuit in enumerate(model.circuits):
        fields = {'description': ('A  B' if i == 0 else ' A B ' if i == 1 else f'Synthetic equipment {i}'),
                  'ocpd': '20 A' if i % 2 == 0 else 'TBD'}
        if positioned and i < count - 3:
            fields.update(side='left' if i % 2 == 0 else 'right', row=i // 2 + 1)
        if i == 0:
            fields['phase_va'] = {'A': 0.0, 'B': 0.0, 'C': 0.0}
        circuits.append(replace(circuit, attributes={'panel_schedule': fields}))
    return replace(model, circuits=tuple(circuits))


def inputs(tmp_path, *, count=12, positioned=False, layout=None):
    model = tmp_path / 'model.json'; model.write_text(source(count, positioned).to_json())
    settings = tmp_path / 'settings.json'; settings.write_text(json.dumps({'version': VERSION, 'layout': layout or {}}))
    return (model, ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json', settings)


def test_unpositioned_packet_has_no_fictitious_bank_and_preserves_source_coverage(tmp_path):
    paths = inputs(tmp_path)
    before = paths[0].read_bytes()
    out = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'drafted')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest['drafting_grammar'] == 'paired-panel-card-v2'
    assert manifest['row_count'] == 14
    section_ids = {sid for section in manifest['schedule_pages'] for sid in section['included_schedule_ids']}
    assert {row['schedule_id'] for row in manifest['rows']} <= section_ids
    assert paths[0].read_bytes() == (out / 'schedule-source/model.json').read_bytes() == before
    svgs = [ET.fromstring(path.read_bytes()) for path in sorted((out / 'sheets').glob('*.svg'))]
    svg = svgs[0]
    texts = [n.text or '' for page in svgs for n in page.findall('.//{http://www.w3.org/2000/svg}text')]
    assert texts.count('CKT') >= 2 and texts.count('LOAD / DESCRIPTION') >= 2
    assert 'ROW / CKT' not in texts
    assert 'SUPPLIED CONNECTED-LOAD SUMMARY' in texts
    assert 'KNOWN CONNECTED SUBTOTAL' in texts
    assert any('A  B' in v for v in texts) and any(v.startswith(' A B ') for v in texts)
    assert any('LABEL CONFLICT' in v for v in texts)
    assert not any('UNASSIGNED' == v for v in texts)
    assert any('CIRCUIT POSITIONS NOT SUPPLIED' in v for v in texts)
    weights = {n.get('stroke-width') for n in svg.findall('.//{http://www.w3.org/2000/svg}polyline')}
    assert {'0.13', '0.25', '0.5'} <= weights
    assert {'400', '600'} <= {n.get('font-weight') for n in svg.findall('.//{http://www.w3.org/2000/svg}text')}
    assert len({n.get('font-size') for n in svg.findall('.//{http://www.w3.org/2000/svg}text')}) >= 2
    pdf = pypdf.PdfReader(out / 'sheet-set.pdf')
    assert len(pdf.pages) == len(manifest['sheet_order'])
    assert all(not list(page.images) for page in pdf.pages)
    text = ''.join(p.extract_text() for p in pdf.pages)
    assert 'NOT ESTABLISHED' in text and 'MAIN: TBD' in text
    # Data-ledger implementation columns are retained as source, not printed as
    # an operational table on the plan.
    assert 'Actual source port' not in text and 'Circuit source confidence' not in text
    circuits = [r for r in manifest['rows'] if r['source_id'].startswith('circuit:')]
    assert len(circuits) == 12 and all(r['position_status'] == 'not_supplied_display_order_only' for r in circuits)
    assert all(r['display_side']=='unpositioned' and r['supplied_side'] is None and r['supplied_row'] is None for r in circuits)


def test_dense_explicit_pairing_preserves_every_circuit_and_does_not_infer_spares(tmp_path):
    paths = inputs(tmp_path, count=90, positioned=True)
    first = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'one')
    second = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'two')
    manifest = json.loads((first / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest == json.loads((second / 'SCHEDULE-MANIFEST.json').read_text())
    assert (first / 'sheet-set.pdf').read_bytes() == (second / 'sheet-set.pdf').read_bytes()
    assert manifest['row_count'] == 92
    rows = [r for r in manifest['rows'] if r['source_id'].startswith('circuit:')]
    assert len({r['source_id'] for r in rows}) == 90
    left, right = [next(r for r in rows if r['source_id'] == f'circuit:synthetic-{i:03d}') for i in (0, 1)]
    assert left['position_status'] == right['position_status'] == 'supplied'
    assert left['bounds_mm'][1:4:2] == right['bounds_mm'][1:4:2]
    assert left['bounds_mm'][0] < right['bounds_mm'][0]
    assert manifest['panel_totals'][0]['all_listed_loads_known'] is False
    assert set(r['source_id'] for r in rows) == {c.id for c in source(90, True).circuits}


def test_explicit_region_reserves_space_for_single_line_and_has_no_titleblock():
    model = source()
    report, drawing = panel_schedule_drawing_set(model)
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    region = (420, 230, 810, 590)
    pages = draft_panel_schedule_pages(report, drawing, sheet, region_bounds_mm=region)
    for page in pages:
        for row in page.row_map:
            box = row['bounds_mm']
            assert region[0] <= box[0] <= box[2] <= region[2]
            assert region[1] <= box[1] <= box[3] <= region[3]
        body = page.body_svg(ScheduleLayout())
        assert 'data-titleblock' not in body
        assert 'font-weight="600"' in body


def test_duplicate_explicit_positions_are_not_silently_reassigned(tmp_path):
    model = source(positioned=True)
    circuits = list(model.circuits)
    fields = dict(circuits[1].attributes['panel_schedule']); fields.update(side='left', row=1)
    circuits[1] = replace(circuits[1], attributes={'panel_schedule': fields})
    paths = inputs(tmp_path)
    paths[0].write_text(replace(model, circuits=tuple(circuits)).to_json())
    with pytest.raises(ValueError, match='duplicate explicit panel schedule position'):
        create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_long_description_uses_more_rows_or_pages_without_text_reduction(tmp_path):
    paths = inputs(tmp_path, count=30)
    model = source(30)
    fields = dict(model.circuits[0].attributes['panel_schedule']); fields['description'] = 'SUPPLIED DESCRIPTION ' * 30
    model = replace(model, circuits=(replace(model.circuits[0], attributes={'panel_schedule': fields}), *model.circuits[1:]))
    paths[0].write_text(model.to_json())
    out = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'long', drafted_width_mm=300)
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest['row_count'] == 32
    assert manifest['layout']['text_height_mm'] == 2.5
    report = json.loads((out / 'schedule-source/panel-facts.json').read_text())
    assert report['panels'][0]['circuits'][0]['circuit']['entity']['attributes']['panel_schedule']['description'] == fields['description']


def test_phase_source_conflicts_are_visible_without_inventing_allocation(tmp_path):
    paths = inputs(tmp_path)
    model = source()
    fields = dict(model.circuits[0].attributes['panel_schedule']); fields['phase_va'] = {'A': 1.0, 'B': 0.0, 'C': 0.0}
    model = replace(model, circuits=(replace(model.circuits[0], attributes={'panel_schedule': fields}), *model.circuits[1:]))
    paths[0].write_text(model.to_json())
    out = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'phase-conflict')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    row = next(r for r in manifest['rows'] if r['source_id'] == model.circuits[0].id)
    assert row['phase_cells'] == ['1.0', '0.0', '0.0']
    assert any('PHASE/LOAD CONFLICT' in c for c in row['display_cells'])
    unknown = next(r for r in manifest['rows'] if r['source_id'] == model.circuits[1].id)
    assert unknown['phase_cells'] == ['TBD', 'TBD', 'TBD']
    assert manifest['panel_totals'][0]['known_connected_va'] == 350.0


def test_region_overflow_is_actionable_and_unpublished(tmp_path):
    paths = inputs(tmp_path)
    with pytest.raises(ValueError, match='leave no circuit rows'):
        create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'tiny', drafted_region_mm=(20, 20, 400, 65))
    assert not (tmp_path / 'tiny').exists()


def test_noncontiguous_supplied_positions_are_labelled_and_gap_marked_in_pdf(tmp_path):
    paths = inputs(tmp_path, count=6)
    model = source(6)
    circuits = list(model.circuits)
    for i, (side, row) in enumerate([('left', 1), ('right', 5), ('left', 9)]):
        fields = dict(circuits[i].attributes['panel_schedule']); fields.update(side=side, row=row)
        circuits[i] = replace(circuits[i], attributes={'panel_schedule': fields})
    paths[0].write_text(replace(model, circuits=tuple(circuits)).to_json())
    out = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'positions')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    rows = [next(r for r in manifest['rows'] if r['source_id'] == c.id) for c in circuits[:3]]
    assert [(r['supplied_side'], r['supplied_row'], r['display_side']) for r in rows] == [
        ('left', 1, 'left'), ('right', 5, 'right'), ('left', 9, 'left')]
    assert manifest['row_count'] == 8  # No placeholder circuit or spare identities added.
    gaps = [gap for section in manifest['schedule_pages'] for gap in section['position_gaps']]
    assert [(g['first_row'], g['last_row']) for g in gaps] == [(2, 4), (6, 8)]
    assert all(g['status'] == 'not_supplied_no_spare_capacity_inferred' for g in gaps)
    text = ''.join(page.extract_text() for page in pypdf.PdfReader(out / 'sheet-set.pdf').pages)
    assert 'ROWS 2-4 NOT SUPPLIED - NO SPARE CAPACITY INFERRED' in text
    assert 'ROWS 6-8 NOT SUPPLIED - NO SPARE CAPACITY INFERRED' in text
    assert '1 / duplicate' in text and '5 / duplicate' in text and '9 / 3' in text
    # Actual vector text has the same position labels; source opaque labels are
    # retained in full canonical cells, not replaced by generated row numbers.
    assert rows[0]['cells'][1] == rows[1]['cells'][1] == 'duplicate supplied label'
    assert rows[2]['cells'][1] == '3'
    assert rows[0]['bounds_mm'][1] < rows[1]['bounds_mm'][1] < rows[2]['bounds_mm'][1]


def test_unassigned_positions_have_no_supplied_side_or_row(tmp_path):
    paths = inputs(tmp_path, count=6)
    out = create_panel_schedule_sheet_set(*paths, output_directory=tmp_path / 'unassigned')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    circuits = [r for r in manifest['rows'] if r['source_id'].startswith('circuit:')]
    assert all(r['supplied_side'] is None and r['supplied_row'] is None for r in circuits)
    assert all(r['position_status'] == 'not_supplied_display_order_only' for r in circuits)


def test_selection_empty_only_report_has_no_filtered_pages_and_report_stays_whole():
    from oabm.drawings.schedule_layout import draft_panel_schedule_selection
    report, drawing = panel_schedule_drawing_set(replace(source(6), circuits=()))
    before = report.to_json()
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    assert draft_panel_schedule_pages(report, drawing, sheet, include_empty_panels=False, include_unassigned=False) == ()
    audit = draft_panel_schedule_selection(report, include_empty_panels=False, include_unassigned=False)
    assert not audit['selected_schedule_ids'] and len(audit['excluded_schedule_ids']) == 4
    assert all(g['reason'] == 'empty_panel_excluded' for g in audit['groups'])
    assert report.to_json() == before and len(report.panels) == 2


def test_selection_keeps_actual_panel_circuits_and_explicitly_accounts_for_exclusions():
    from oabm.drawings.schedule_layout import draft_panel_schedule_selection
    model = source(12)
    # The second source is explicitly non-panel equipment; keep its circuits in
    # the validated report for the coordinator's feeder register, not branch cards.
    second = replace(model.electrical_equipment[1], equipment_type='transformer')
    model = replace(model, electrical_equipment=(model.electrical_equipment[0], second))
    report, drawing = panel_schedule_drawing_set(model)
    before = report.to_json()
    panel = report.panels[0].panel.id
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    options = dict(panel_ids=[panel], include_empty_panels=False, include_unassigned=False)
    pages = draft_panel_schedule_pages(report, drawing, sheet, **options)
    rows = [row for page in pages for row in page.row_map]
    assert {r['source_id'] for r in rows} == {panel, *(fact.circuit.id for fact in report.panels[0].circuits)}
    audit = draft_panel_schedule_selection(report, **options)
    drawn_schedules = {sid for p in pages for section in p.sections for sid in section['included_schedule_ids']}
    assert drawn_schedules == set(audit['selected_schedule_ids'])
    assert len(audit['excluded_schedule_ids']) == 1
    assert audit['groups'][-1]['reason'] == 'unassigned_excluded'
    assert audit['groups'][-1]['circuit_ids'] == [fact.circuit.id for fact in report.unassigned_circuits]
    assert len(report.unassigned_circuits) == 3 and report.to_json() == before


def test_selection_defaults_are_identical_to_explicit_legacy_all_groups():
    report, drawing = panel_schedule_drawing_set(source(6))
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    implicit = draft_panel_schedule_pages(report, drawing, sheet)
    explicit = draft_panel_schedule_pages(report, drawing, sheet, panel_ids=None, include_empty_panels=True, include_unassigned=True)
    assert [p.body_svg(ScheduleLayout()) for p in implicit] == [p.body_svg(ScheduleLayout()) for p in explicit]
    assert [p.row_map for p in implicit] == [p.row_map for p in explicit]
    assert draft_panel_schedule_pages(report, drawing, sheet, panel_ids=[], include_unassigned=False) == ()
    with pytest.raises(ValueError, match='unknown report panel'):
        draft_panel_schedule_pages(report, drawing, sheet, panel_ids=['equipment:not-in-report'])


def _flat_pdf_text(path):
    return ''.join(''.join((page.extract_text() or '').split()) for page in pypdf.PdfReader(path).pages)


def test_duplicate_panel_names_and_conflicting_circuits_remain_visible_in_pdf_and_maps(tmp_path):
    model=source(90,True)
    model=replace(model,electrical_equipment=tuple(replace(e,name='Distribution panel') for e in model.electrical_equipment))
    paths=inputs(tmp_path,count=90,positioned=True);paths[0].write_text(model.to_json())
    out=create_panel_schedule_sheet_set(*paths,output_directory=tmp_path/'identity',drafted_width_mm=300)
    manifest=json.loads((out/'SCHEDULE-MANIFEST.json').read_bytes())
    text=_flat_pdf_text(out/'sheet-set.pdf')
    for panel in model.electrical_equipment:
        assert panel.id in text
    assert text.count('Distributionpanel')>=len(model.electrical_equipment)
    for cid in ('circuit:synthetic-000','circuit:synthetic-001'):
        assert cid in text
        row=next(row for row in manifest['rows'] if row['source_id']==cid)
        assert row['label_conflict_circuit_ids']==['circuit:synthetic-000','circuit:synthetic-001']
        assert any(cid in cell and 'LABEL CONFLICT' in cell for cell in row['display_cells'])
    # Every continuation section repeats its own full panel identity and name.
    page_files=sorted((out/'schedule-source').glob('*.drawing.json'))
    assert page_files
    for section in manifest['schedule_pages']:
        sid=section['sheet_id'];panel_id=section['panel_id']
        drawing=json.loads(next(p for p in page_files if p.name.startswith(sid+'.')).read_bytes())
        titles=[primitive['text'] for primitive in drawing['views'][0]['primitives']
                if primitive['kind']=='text' and ['role','schedule_title'] in primitive['metadata'] and panel_id in primitive['source_ids']]
        full=''.join(titles)
        assert panel_id in full and 'Distribution panel' in full


def test_unsupported_owners_and_missing_labels_print_actual_ids_and_source_bindings(tmp_path):
    model=source(6)
    # This explicitly classifies one supplied source as unsupported; no panel is invented.
    second=replace(model.electrical_equipment[1],equipment_type='transformer')
    model=replace(model,electrical_equipment=(model.electrical_equipment[0],second))
    circuits=list(model.circuits)
    # Two separate, same-description rows have missing opaque labels.
    for i in (0,1):circuits[i]=replace(circuits[i],circuit_number=None,attributes={'panel_schedule':{'description':'SAME SUPPLIED LOAD'}})
    model=replace(model,circuits=tuple(circuits))
    paths=inputs(tmp_path,count=6);paths[0].write_text(model.to_json())
    report,_=panel_schedule_drawing_set(model)
    out=create_panel_schedule_sheet_set(*paths,output_directory=tmp_path/'unassigned-visible')
    manifest=json.loads((out/'SCHEDULE-MANIFEST.json').read_bytes());text=_flat_pdf_text(out/'sheet-set.pdf')
    for circuit in circuits[:2]:
        assert circuit.id in text
        row=next(row for row in manifest['rows'] if row['source_id']==circuit.id)
        assert any('LABEL NOT SUPPLIED' in cell and circuit.id in cell for cell in row['display_cells'])
        assert 'TBD' in row['display_cells'][0]
    for fact in report.unassigned_circuits:
        assert fact.circuit.id in text and fact.source_owner.id in text and fact.source_port.id in text
        row=next(row for row in manifest['rows'] if row['source_id']==fact.circuit.id)
        assert row['source_owner_id']==fact.source_owner.id and row['source_port_id']==fact.source_port.id
        assert any(fact.circuit.id in cell and fact.source_owner.id in cell for cell in row['display_cells'])
        drawing=json.loads((out/'schedule-source'/f"{row['sheet_id']}.drawing.json").read_bytes())
        by_id={primitive['id']:primitive for primitive in drawing['views'][0]['primitives']}
        assert any({fact.circuit.id,fact.source_owner.id,fact.source_port.id}<=set(by_id[pid]['source_ids']) for pid in row['primitive_ids'])


def test_long_source_ids_wrap_without_loss_in_heading_conflicts_and_pdf(tmp_path):
    model=source(6)
    old_id=model.electrical_equipment[0].id;long_panel='equipment:'+'P'*100
    equipment=tuple(replace(e,id=long_panel,name='Long supplied panel name for visual identification') if e.id==old_id else e for e in model.electrical_equipment)
    ports=tuple(replace(p,owner_id=long_panel) if p.owner_id==old_id else p for p in model.ports)
    long_ids=['circuit:'+'A'*98,'circuit:'+'B'*98]
    circuits=tuple(replace(c,id=long_ids[i]) if i<2 else c for i,c in enumerate(model.circuits))
    model=replace(model,electrical_equipment=equipment,ports=ports,circuits=circuits)
    paths=inputs(tmp_path,count=6);paths[0].write_text(model.to_json())
    out=create_panel_schedule_sheet_set(*paths,output_directory=tmp_path/'long-identities',drafted_width_mm=300)
    text=_flat_pdf_text(out/'sheet-set.pdf')
    for identity in (long_panel,*long_ids):assert identity in text
    manifest=json.loads((out/'SCHEDULE-MANIFEST.json').read_bytes())
    for cid in long_ids:
        row=next(row for row in manifest['rows'] if row['source_id']==cid)
        assert row['label_conflict_circuit_ids']==long_ids
        assert any(cid in cell for cell in row['display_cells'])


def test_mixed_positions_are_separate_unpositioned_section(tmp_path):
    model = source(12, positioned=True)
    report, drawing = panel_schedule_drawing_set(model)
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    pages = draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(packing='compact'))
    rows = [row for page in pages for row in page.row_map if row['source_id'].startswith('circuit:')]
    assert len(rows) == len(model.circuits)
    assert {r['source_id'] for r in rows} == {c.id for c in model.circuits}
    for row in rows:
        if row['supplied_side'] is None:
            assert row['display_side'] == 'unpositioned'
            assert row['position_status'] == 'not_supplied_display_order_only'
            assert 'TBD /' not in row['display_cells'][0]
        else:
            assert row['display_side'] == row['supplied_side']
    body = ''.join(page.body_svg(ScheduleLayout()) for page in pages)
    assert 'UNPOSITIONED CIRCUITS' in body
    assert 'CIRCUIT POSITIONS NOT SUPPLIED' in body


def test_drafted_layout_controls_are_not_silently_ignored(tmp_path):
    model = source()
    report, drawing = panel_schedule_drawing_set(model)
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    with pytest.raises(ValueError, match='do not support column_widths_mm'):
        draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(column_widths_mm={'number': 9999}))
    one = draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(packing='one_table_per_sheet'))
    compact = draft_panel_schedule_pages(report, drawing, sheet, layout=ScheduleLayout(packing='compact'))
    assert len(one) > len(compact)
    assert all(len(page.sections) == 1 for page in one)


def test_mixed_positions_for_unsupported_owner_remain_separate():
    model = source(6)
    second = replace(model.electrical_equipment[1], equipment_type='transformer')
    model = replace(model, electrical_equipment=(model.electrical_equipment[0], second))
    report, _ = panel_schedule_drawing_set(model)
    target = report.unassigned_circuits[0].circuit.id
    circuits = tuple(replace(c, attributes={'panel_schedule': {'side': 'right', 'row': 2}}) if c.id == target else c for c in model.circuits)
    report, drawing = panel_schedule_drawing_set(replace(model, circuits=circuits))
    sheet = resolve_company_sheet_set(ROOT / 'fixtures/company_sheets/v1/profile.json', ROOT / 'fixtures/schedule_sheets/v1/job.json').sheets[0]
    pages = draft_panel_schedule_pages(report, drawing, sheet)
    rows = [r for p in pages for r in p.row_map if r['source_id'] in {f.circuit.id for f in report.unassigned_circuits}]
    assert len(rows) == len(report.unassigned_circuits)
    assert next(r for r in rows if r['source_id'] == target)['display_side'] == 'right'
    assert all(r['display_side'] == 'unpositioned' for r in rows if r['source_id'] != target)
