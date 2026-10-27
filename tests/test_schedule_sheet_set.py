"""Integration against the actual shared company renderer, never a fake frame."""
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.drawings.schedule_layout import ScheduleLayout, paginate_schedules
from oabm.drawings.schedule_set import VERSION, create_panel_schedule_sheet_set, panel_schedule_drawing_set

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('panel_example', ROOT / 'examples/panel_schedule_sheets.py')
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def inputs(tmp_path, formats=None, count=90):
    profile = {'version': 'company-sheet-profile-v1', 'fields': {'company': 'SYNTHETIC COMPANY'}, 'template': {}}
    job = {'version': 'company-sheet-job-v1', 'set_id': 'synthetic-schedules',
        'date': '2026-10-27', 'revision': 'R1', 'formats': formats or ['svg'],
        'fields': {'project': 'SYNTHETIC PANEL FACTS', 'drawn_by': 'TEST'},
        'sheets': [{'id': 'E6', 'title': 'PANEL SCHEDULES'}]}
    settings = {'version': VERSION, 'layout': {}}
    files = []
    for name, value in [('model', example.synthetic_two_panel_model(count).to_dict()),
                        ('profile', profile), ('job', job), ('settings', settings)]:
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps(value))
        files.append(path)
    return files


def test_two_panel_shared_frame_row_coverage_unknown_conflicts_and_totals(tmp_path):
    files = inputs(tmp_path)
    before = [p.read_bytes() for p in files]
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'first')
    second = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'second')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest == json.loads((second / 'SCHEDULE-MANIFEST.json').read_text())
    assert [p.read_bytes() for p in files] == before
    assert manifest['row_count'] == 92  # 90 canonical circuits + 2 supplied-load summaries
    assert len(manifest['sheet_order']) > 4  # panel A is large enough to paginate
    assert len(manifest['panel_totals']) == 2
    report, drawing = panel_schedule_drawing_set(example.synthetic_two_panel_model())
    circuit_rows = [r for r in manifest['rows'] if r['source_id'].startswith('circuit:')]
    assert {r['source_id'] for r in circuit_rows} == {c.id for c in example.synthetic_two_panel_model().circuits}
    assert len(circuit_rows) == 90
    assert any('unknown' in cell for r in circuit_rows for cell in r['cells'])
    assert any('duplicate label; circuits:' in cell for r in circuit_rows for cell in r['cells'])
    assert [(p['panel_id'], p['known_connected_va']) for p in manifest['panel_totals']] == [
        (p.panel.id, p.known_connected_va) for p in report.panels]
    svgs = sorted((out / 'sheets').glob('*.svg'))
    assert len(svgs) == len(manifest['sheet_order'])
    for svg, page in zip(svgs, manifest['schedule_pages']):
        root = ET.fromstring(svg.read_text())
        text = ''.join(root.itertext())
        assert 'SYNTHETIC COMPANY' in text
        assert 'Schedule page' in text
        assert f"{page['schedule_page']}/{page['schedule_pages']}" in text
        assert len(root.findall('{http://www.w3.org/2000/svg}rect')) == 1  # one shared frame
    assert (out / 'schedule-source/model.json').read_bytes() == before[0]
    assert (out / 'schedule-source/drawing-schedules.json').exists()
    for row in manifest['files']:
        import hashlib
        assert hashlib.sha256((out / row['path']).read_bytes()).hexdigest() == row['sha256']


def test_actual_vector_pdf_repeatability_page_count_dimensions_and_text(tmp_path):
    pytest.importorskip('cairosvg')
    import pypdf
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    first = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'pdf1')
    second = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'pdf2')
    data = (first / 'sheet-set.pdf').read_bytes()
    assert data == (second / 'sheet-set.pdf').read_bytes()
    manifest = json.loads((first / 'SCHEDULE-MANIFEST.json').read_text())
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) == len(manifest['sheet_order'])
    extracted = []
    for page, sheet_id in zip(reader.pages, manifest['sheet_order']):
        assert not list(page.images)
        assert abs(float(page.mediabox.width) - 914.4 * 72 / 25.4) < .01
        assert abs(float(page.mediabox.height) - 609.6 * 72 / 25.4) < .01
        text = page.extract_text()
        assert sheet_id in text and 'Schedule page' in text
        extracted.append(text)
    assert 'Known connected subtotal' in ''.join(extracted)
    assert 'unknown' in ''.join(extracted)


def test_failure_never_publishes_partial_success(tmp_path, monkeypatch):
    from oabm.drawings import company_sheets
    files = inputs(tmp_path)
    def failed(*args, **kwargs):
        raise ValueError('synthetic converter failure')
    monkeypatch.setattr(company_sheets, 'create_company_sheet_set', failed)
    with pytest.raises(ValueError, match='synthetic converter failure'):
        create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'bad')
    assert not (tmp_path / 'bad/delivery').exists()
    assert json.loads((tmp_path / 'bad/failure.json').read_text())['stage'] == 'shared company sheet production'


def test_invalid_overflow_never_creates_success_directory(tmp_path):
    files = inputs(tmp_path)
    files[3].write_text(json.dumps({'version': VERSION, 'layout': {'column_widths_mm': {'number': 1}}}))
    with pytest.raises(ValueError, match='too narrow'):
        create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_portable_snapshot_preserves_relative_logo_bytes(tmp_path):
    from PIL import Image
    files = inputs(tmp_path, count=12)
    Image.new('RGB', (12, 12), '#ffffff').save(tmp_path / 'synthetic.png')
    profile = json.loads(files[1].read_text())
    profile['logos'] = {'icon': 'synthetic.png'}
    files[1].write_text(json.dumps(profile))
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'logos')
    retained = out / 'schedule-source'
    assets = list((retained / 'assets').glob('*.png'))
    assert len(assets) == 1 and assets[0].read_bytes() == (tmp_path / 'synthetic.png').read_bytes()
    from oabm.drawings.company_sheets import resolve_company_sheet_set
    restored = resolve_company_sheet_set(retained / 'portable-profile.json', retained / 'expanded-job.json')
    assert len(restored.sheets) == len(list((out / 'sheets').glob('*.svg')))
    assert restored.sheets[0].logos[0][1] == assets[0].read_bytes()


def test_empty_panels_are_visible_without_invented_circuits(tmp_path):
    files = inputs(tmp_path, count=12)
    model = example.synthetic_two_panel_model(12)
    files[0].write_text(replace(model, circuits=()).to_json())
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'empty')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest['row_count'] == 2  # the two actual panel summaries only
    assert len(manifest['sheet_order']) == 4
    assert {row['source_id'] for row in manifest['rows']} == {p.id for p in model.electrical_equipment}
    assert 'No listed circuits' in ''.join(p.read_text() for p in (out / 'sheets').glob('*.svg'))


def test_missing_actual_shared_text_is_refused_before_final_delivery(tmp_path, monkeypatch):
    from oabm.drawings import company_sheets
    files = inputs(tmp_path, count=12)
    original = company_sheets.create_company_sheet_set
    def omit_text(*args, **kwargs):
        output = original(*args, **kwargs)
        svg = sorted((output / 'sheets').glob('*.svg'))[0]
        root = ET.fromstring(svg.read_bytes())
        group = root.find('{http://www.w3.org/2000/svg}g')
        group.remove(group.find('{http://www.w3.org/2000/svg}text'))
        svg.write_bytes(ET.tostring(root))
        return output
    monkeypatch.setattr(company_sheets, 'create_company_sheet_set', omit_text)
    with pytest.raises(ValueError, match='schedule text missing'):
        create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'omitted')
    assert not (tmp_path / 'omitted/delivery').exists()


def test_settings_schema_and_actionable_invalid_input(tmp_path):
    from jsonschema import Draft202012Validator
    files = inputs(tmp_path, count=12)
    Draft202012Validator(json.loads((ROOT / 'contracts/schedule-sheet-settings-v1.schema.json').read_text())).validate(
        json.loads(files[3].read_text()))
    files[3].write_text(json.dumps({'version': VERSION, 'layout': {'line_height_mm': 1}}))
    with pytest.raises(ValueError, match='at least 1.5'):
        create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_unsupported_label_glyph_fails_without_source_mutation(tmp_path):
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    model = example.synthetic_two_panel_model(12)
    files[0].write_text(replace(model, circuits=(replace(model.circuits[0], circuit_number='A\u00a0B'),
                                             *model.circuits[1:])).to_json())
    original = files[0].read_bytes()
    with pytest.raises(ValueError, match='printable ASCII'):
        create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'unsupported')
    assert files[0].read_bytes() == original and not (tmp_path / 'unsupported').exists()


def test_supported_single_internal_label_space_survives_actual_pdf_runs_and_source(tmp_path):
    import pypdf
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    model = example.synthetic_two_panel_model(12)
    files[0].write_text(replace(model, circuits=(replace(model.circuits[0], circuit_number='A B'),
                                             *model.circuits[1:])).to_json())
    original = files[0].read_bytes()
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'single-space')
    assert (out / 'schedule-source/model.json').read_bytes() == original
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    assert manifest['verification']['pdf']['status'] == 'all_projected_nonspace_glyph_runs_exact'
    runs = []
    for page in pypdf.PdfReader(out / 'sheet-set.pdf').pages:
        page.extract_text(visitor_text=lambda text, *unused: runs.append(text.rstrip('\r\n')))
    assert 'A B' in runs
    assert any('>A B</' in p.read_text() for p in (out / 'sheets').glob('*.svg'))


def test_supported_space_has_distinct_actual_pdf_pixels_from_no_space(tmp_path):
    pdfium = pytest.importorskip('pypdfium2')
    from PIL import ImageChops
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    model = example.synthetic_two_panel_model(12)
    outputs = []
    for label, folder in [('A B', 'space'), ('AB', 'no-space')]:
        files[0].write_text(replace(model, circuits=(replace(model.circuits[0], circuit_number=label),
                                                   *model.circuits[1:])).to_json())
        outputs.append(create_panel_schedule_sheet_set(*files, output_directory=tmp_path / folder))
    crops = []
    for out in outputs:
        manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
        row = next(r for r in manifest['rows'] if r['source_id'] == model.circuits[0].id)
        left, top, right, bottom = row['bounds_mm']
        width = (right - left) / len(row['cells'])
        box_mm = (left + width + .5, top + .5, left + 2 * width - .5, bottom - .5)
        with pdfium.PdfDocument(out / 'sheet-set.pdf') as pdf:
            image = pdf[0].render(scale=2).to_pil().convert('RGB')
            crops.append(image.crop(tuple(round(v * 72 / 25.4 * 2) for v in box_mm)))
    assert ImageChops.difference(*crops).getbbox() is not None


def test_distinct_opaque_spaces_preserved_in_source_svg_and_actual_pdf_pixels(tmp_path):
    pdfium = pytest.importorskip('pypdfium2')
    from PIL import Image, ImageChops
    labels = ['A B', 'A  B', ' A B ']
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    model = example.synthetic_two_panel_model(12)
    files[0].write_text(replace(model, circuits=tuple(replace(c, circuit_number=labels[i]) if i < 3 else c
                                                   for i, c in enumerate(model.circuits))).to_json())
    original = files[0].read_bytes()
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'all-spaces')
    assert files[0].read_bytes() == (out / 'schedule-source/model.json').read_bytes() == original
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    rows = [next(r for r in manifest['rows'] if r['source_id'] == c.id) for c in model.circuits[:3]]
    assert [r['cells'][1] for r in rows] == labels
    assert [''.join(r['wrapped_cells'][1]) for r in rows] == labels
    root = ET.fromstring(sorted((out / 'sheets').glob('*.svg'))[0].read_bytes())
    text_nodes = root.findall('.//{http://www.w3.org/2000/svg}text')
    for label in labels:
        assert any(n.text == label and n.get('{http://www.w3.org/XML/1998/namespace}space') == 'preserve' for n in text_nodes)
    boxes = []
    with pdfium.PdfDocument(out / 'sheet-set.pdf') as pdf:
        image = pdf[0].render(scale=3).to_pil().convert('RGB')
        for row in rows:
            left, top, right, bottom = row['bounds_mm']
            width = (right - left) / len(row['cells'])
            mm = (left + width + .5, top + .5, left + 2 * width - .5, min(top + 7, bottom - .5))
            crop = image.crop(tuple(round(v * 72 / 25.4 * 3) for v in mm))
            boxes.append(ImageChops.difference(crop, Image.new('RGB', crop.size, 'white')).getbbox())
    assert all(boxes)
    assert boxes[1][2] - boxes[1][0] > boxes[0][2] - boxes[0][0]  # extra internal space has a wider actual gap
    assert boxes[2][0] > boxes[0][0]  # supplied leading space moves the glyphs


def test_narrow_column_wrap_boundary_spaces_remain_distinct_in_actual_pdf(tmp_path):
    pdfium = pytest.importorskip('pypdfium2')
    from PIL import ImageChops
    labels = ['ABC DEF', 'ABC DE F']
    files = inputs(tmp_path, ['svg', 'pdf'], 12)
    model = example.synthetic_two_panel_model(12)
    files[0].write_text(replace(model, circuits=tuple(replace(c, circuit_number=labels[i]) if i < 2 else c
                                                   for i, c in enumerate(model.circuits))).to_json())
    files[3].write_text(json.dumps({'version': VERSION, 'layout': {'column_widths_mm': {'number': 10.5}}}))
    out = create_panel_schedule_sheet_set(*files, output_directory=tmp_path / 'narrow-spaces')
    manifest = json.loads((out / 'SCHEDULE-MANIFEST.json').read_text())
    rows = [next(r for r in manifest['rows'] if r['source_id'] == c.id) for c in model.circuits[:2]]
    wrapped = [r['wrapped_cells'][1] for r in rows]
    assert [''.join(lines) for lines in wrapped] == labels
    assert wrapped[0] != wrapped[1]
    primitives = json.loads((out / 'schedule-source' / f"{rows[0]['sheet_id']}.drawing.json").read_text())['views'][0]['primitives']
    for row, lines in zip(rows, wrapped):
        text = [p['text'] for p in primitives if p['id'] in row['primitive_ids']]
        # The first cell precedes the number column; all its exact wrapped number
        # runs must remain present in row order, including boundary spaces.
        position = 0
        for line in lines:
            position = text.index(line, position) + 1
    crops = []
    maximum_lines = max(map(len, wrapped))
    with pdfium.PdfDocument(out / 'sheet-set.pdf') as pdf:
        image = pdf[0].render(scale=3).to_pil().convert('RGB')
        for row in rows:
            left, top, right, bottom = row['bounds_mm']
            first_width = (right - left - 10.5) / (len(row['cells']) - 1)
            mm = (left + first_width + .5, top + .5, left + first_width + 10.5 - .5,
                  top + 2 * 1.5 + maximum_lines * 4 - .5)
            crops.append(image.crop(tuple(round(v * 72 / 25.4 * 3) for v in mm)))
    assert ImageChops.difference(*crops).getbbox() is not None
