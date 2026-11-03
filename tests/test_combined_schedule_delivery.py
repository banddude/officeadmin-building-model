"""Cloud-only acceptance: one source, all inventories, real vector delivery."""
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from oabm.model import ContractError
from oabm.drawings.generator import generate_schedules
from oabm.drawings.schedule_set import (combined_schedule_drawing_set,
    create_combined_schedule_sheet_set, verify_combined_schedule_delivery)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('combined_example', ROOT / 'examples/combined_schedule_delivery.py')
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_inventory_panel_overlap_unknowns_and_provenance():
    model = example.synthetic_model()
    before = model.to_json()
    report, drawing = combined_schedule_drawing_set(model)
    schedules = {s.id: s for s in drawing.schedules}
    ordinary = generate_schedules(model)
    assert all(schedules[s.id] == s for s in ordinary)
    assert {'schedule:rooms', 'schedule:openings', 'schedule:equipment',
            'schedule:devices', 'schedule:circuits', 'schedule:electrical-boxes',
            'schedule:box-selections'} <= schedules.keys()
    assert len(schedules['schedule:electrical-boxes'].rows) == 1
    assert len(schedules['schedule:box-selections'].rows) == 2
    assert len(report.panels) == 2
    panel_a = next(p for p in report.panels if p.panel.id == model.electrical_equipment[0].id)
    assert panel_a.known_connected_va == 1800
    assert panel_a.unknown_load_circuit_ids == ('circuit:example-1',)
    assert panel_a.label_conflicts
    text = drawing.to_json()
    assert 'unknown' in text and 'duplicate label; circuits:' in text and 'missing_product' in text
    index = {s.canonical_id: s for s in drawing.source_index}
    assert all(r.source_id in index for s in drawing.schedules for r in s.rows)
    assert index[model.electrical_devices[0].id].provenance
    assert model.to_json() == before


def test_actual_pdf_svg_delivery_and_regeneration(tmp_path):
    import pypdf
    model = example.synthetic_model()
    manifests = []
    drawings = []
    for name, source in [('before', model), ('after', example.revised_model(model))]:
        inputs = example.write_inputs(tmp_path / name / 'inputs', source)
        before = [p.read_bytes() for p in inputs]
        delivery = create_combined_schedule_sheet_set(*inputs, output_directory=tmp_path / name / 'output')
        manifest = verify_combined_schedule_delivery(delivery, model_path=inputs[0])
        manifests.append(manifest)
        drawing = json.loads((delivery / 'schedule-source/drawing-schedules.json').read_text())
        drawings.append(drawing)
        assert [p.read_bytes() for p in inputs] == before
        assert manifest['source_model_sha256'] == hashlib.sha256(before[0]).hexdigest()
        assert manifest['verification']['svg'] == 'all_projected_text_exact'
        assert manifest['verification']['pdf']['page_count'] == len(manifest['sheet_order'])
        assert len(list((delivery / 'sheets').glob('*.svg'))) == len(manifest['sheet_order'])
        pdf = pypdf.PdfReader(delivery / 'sheet-set.pdf')
        text = '\n'.join(p.extract_text() for p in pdf.pages)
        assert all(not list(p.images) for p in pdf.pages)
        assert 'FICTIONAL COMPANY' in text and 'unknown' in text
        for panel in source.electrical_equipment:
            # Individual text may wrap in narrow fact columns; ignore whitespace.
            assert ''.join(panel.id.split()) in ''.join(text.split())
        assert manifest['overlap_policy'] and len(manifest['panel_totals']) == 2
        assert all(r['entity']['id'] == r['source_id'] and r['entity']['provenance']
                   for r in manifest['row_sources'])
    assert manifests[0]['source_model_fingerprint'] != manifests[1]['source_model_fingerprint']
    old = {s['id']: s for s in drawings[0]['schedules']}
    new = {s['id']: s for s in drawings[1]['schedules']}
    assert old.keys() == new.keys()
    assert old['schedule:devices'] != new['schedule:devices']
    assert old['schedule:circuits'] != new['schedule:circuits']
    for identity in ('schedule:rooms', 'schedule:openings', 'schedule:equipment',
                     'schedule:electrical-boxes', 'schedule:box-selections'):
        assert old[identity] == new[identity]
    first = tmp_path / 'before/output/delivery'
    with pytest.raises(ValueError, match='regenerate'):
        verify_combined_schedule_delivery(first, model_path=tmp_path / 'after/inputs/model.json')
    # Tampering cannot pass file receipts, even with structurally valid JSON.
    (first / 'schedule-source/panel-facts.json').write_text('{}')
    with pytest.raises(ValueError, match='file changed'):
        verify_combined_schedule_delivery(first)


def test_stale_report_broken_references_and_units_refused(tmp_path):
    model = example.synthetic_model()
    report, _ = combined_schedule_drawing_set(model)
    with pytest.raises(ContractError, match='does not match'):
        combined_schedule_drawing_set(example.revised_model(model), report=report)
    with pytest.raises(ContractError):
        combined_schedule_drawing_set(replace(model, electrical_devices=(
            replace(model.electrical_devices[0], space_id='space:missing'), *model.electrical_devices[1:])))
    files = example.write_inputs(tmp_path / 'invalid', model)
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / 'contracts/combined-schedule-sheet-settings-v1.schema.json').read_text())
    Draft202012Validator(schema).validate(json.loads(files[3].read_text()))
    document = json.loads(files[0].read_text())
    document['circuits'][0]['source_port_id'] = 'port:missing'
    files[0].write_text(json.dumps(document))
    with pytest.raises(ContractError):
        create_combined_schedule_sheet_set(*files, output_directory=tmp_path / 'refused')
    assert not (tmp_path / 'refused/delivery').exists()
    document = model.to_dict()
    document['coordinate_system']['length_unit'] = 'ft'
    files[0].write_text(json.dumps(document))
    with pytest.raises(ContractError):
        create_combined_schedule_sheet_set(*files, output_directory=tmp_path / 'bad-units')


def test_site_inventory_explicitly_refused():
    from test_site_model_integration import site_model
    with pytest.raises(ContractError, match='site'):
        combined_schedule_drawing_set(site_model())


def test_combined_requires_pdf_and_svg(tmp_path):
    files = example.write_inputs(tmp_path / 'inputs', example.synthetic_model())
    job = json.loads(files[2].read_text())
    job['formats'] = ['svg']
    files[2].write_text(json.dumps(job))
    with pytest.raises(ValueError, match='both svg and pdf'):
        create_combined_schedule_sheet_set(*files, output_directory=tmp_path / 'no-pdf')


def test_empty_panel_kept_and_assembly_row_sources_indexed():
    from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
    model = example.synthetic_model()
    report, drawing = combined_schedule_drawing_set(replace(model, circuits=()))
    assert len(report.panels) == 2
    assert sum(not s.rows for s in drawing.schedules) == 2
    _, drawing = combined_schedule_drawing_set(synthetic_enclosed_room_assembly())
    index = {s.canonical_id for s in drawing.source_index}
    assert all(r.source_id in index for s in drawing.schedules for r in s.rows)


@pytest.fixture(scope='module')
def verified_delivery(tmp_path_factory):
    root = tmp_path_factory.mktemp('combined-receipt-proof')
    inputs = example.write_inputs(root / 'inputs', example.synthetic_model())
    return create_combined_schedule_sheet_set(*inputs, output_directory=root / 'output')


@pytest.mark.parametrize('field', [
    'panel_totals', 'sheet_order', 'schedule_pages', 'sheet_id', 'page',
    'primitive_ids', 'bounds_mm', 'wrapped_cells', 'layout',
    'status', 'engineering_approval', 'verification',
])
def test_derived_manifest_receipts_recomputed(verified_delivery, tmp_path, field):
    import shutil
    delivery = tmp_path / 'delivery'
    shutil.copytree(verified_delivery, delivery)
    path = delivery / 'SCHEDULE-MANIFEST.json'
    manifest = json.loads(path.read_text())
    if field == 'panel_totals':
        manifest[field][0]['known_connected_va'] = 999999
    elif field == 'sheet_order':
        manifest[field] = ['nonexistent-sheet']
    elif field == 'schedule_pages':
        manifest[field][0]['sheet_id'] = 'nonexistent-sheet'
    elif field == 'layout':
        manifest[field]['text_height_mm'] = 999
    elif field == 'status':
        manifest[field] = 'engineered'
    elif field == 'engineering_approval':
        manifest[field] = True
    elif field == 'verification':
        manifest[field]['pdf']['page_count'] = 999
    else:
        bad = {'sheet_id': 'nonexistent-sheet', 'page': 999,
               'primitive_ids': ['nonexistent-primitive'],
               'bounds_mm': [0, 0, 1, 1], 'wrapped_cells': [['invented']]}
        manifest['rows'][0][field] = bad[field]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='receipt mismatch'):
        verify_combined_schedule_delivery(delivery)


@pytest.mark.parametrize('target', ['drawing', 'body', 'rendered'])
def test_rehashed_page_mutations_refused(verified_delivery, tmp_path, target):
    import shutil
    import xml.etree.ElementTree as ET
    delivery = tmp_path / 'delivery'
    shutil.copytree(verified_delivery, delivery)
    path = delivery / 'SCHEDULE-MANIFEST.json'
    manifest = json.loads(path.read_text())
    sheet = manifest['sheet_order'][0]
    if target == 'drawing':
        changed = delivery / 'schedule-source' / f'{sheet}.drawing.json'
        data = json.loads(changed.read_text())
        data['views'][0]['primitives'][0]['points'][0]['x'] += .001
        changed.write_text(json.dumps(data))
    elif target == 'body':
        changed = delivery / 'schedule-source' / f'{sheet}.body.svg'
        changed.write_text(changed.read_text().replace('x="', 'x="1', 1))
    else:
        changed = delivery / 'sheets' / f'001-{sheet}.svg'
        root = ET.fromstring(changed.read_bytes())
        text = root.find('{http://www.w3.org/2000/svg}g/{http://www.w3.org/2000/svg}text')
        text.set('x', str(float(text.get('x')) + 10))
        changed.write_bytes(ET.tostring(root))
    relative = changed.relative_to(delivery).as_posix()
    receipt = next(row for row in manifest['files'] if row['path'] == relative)
    raw = changed.read_bytes()
    receipt.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='differs from regenerated pagination'):
        verify_combined_schedule_delivery(delivery)


def test_architecture_and_assembly_deliveries_keep_all_row_sources(tmp_path):
    from oabm.model import BuildingModel
    from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
    cases = [('architecture', BuildingModel.from_json(
        (ROOT / 'fixtures/model/v1/minimal-room.json').read_bytes())),
        ('assembly', synthetic_enclosed_room_assembly())]
    for name, model in cases:
        inputs = example.write_inputs(tmp_path / name / 'inputs', model)
        delivery = create_combined_schedule_sheet_set(*inputs, output_directory=tmp_path / name / 'output')
        manifest = verify_combined_schedule_delivery(delivery, model_path=inputs[0])
        assert manifest['row_count'] > 0
        assert not manifest['panel_totals']
        assert len(manifest['row_sources']) == manifest['row_count']
        if name == 'assembly':
            assert {'building_assemblies', 'assembly_components'} <= {
                row['collection'] for row in manifest['row_sources']}


def test_multi_page_row_maps_and_portable_logo_replay(tmp_path):
    from PIL import Image
    model = example.synthetic_model()
    model = replace(model, circuits=tuple(replace(model.circuits[0],
        id=f'circuit:many-{i:03}', circuit_number=f'L{i:03}',
        load_va=None if i % 7 == 0 else 1800.0) for i in range(72)) + (model.circuits[2],))
    inputs = example.write_inputs(tmp_path / 'inputs', model)
    Image.new('RGB', (16, 16), '#336699').save(inputs[1].parent / 'logo.png')
    profile = json.loads(inputs[1].read_text())
    profile['logos'] = {'icon': 'logo.png'}
    inputs[1].write_text(json.dumps(profile))
    delivery = create_combined_schedule_sheet_set(*inputs, output_directory=tmp_path / 'output')
    # Delivery replay must not reopen original logo paths or mutable inputs.
    inputs[0].parent.rename(tmp_path / 'original-inputs-moved')
    manifest = verify_combined_schedule_delivery(delivery)
    assert len(manifest['sheet_order']) >= 3
    assert len(manifest['rows']) == manifest['row_count'] == len(manifest['row_sources'])
    assert {row['sheet_id'] for row in manifest['rows']} == set(manifest['sheet_order'])
    assert all(row['primitive_ids'] and row['wrapped_cells'] for row in manifest['rows'])


def test_combined_delivery_is_explicit_and_legacy_review_contract_unchanged():
    from oabm.pipeline.review_drawings import generate_review_drawings, ReviewDrawingConfig, VERSION
    from oabm.drawings.panel_schedules import panel_report_to_drawing_schedules
    model = example.synthetic_model()
    original = model.to_json()
    report, combined = combined_schedule_drawing_set(model)
    expected_panel_ids = [s.id for s in panel_report_to_drawing_schedules(report)]
    payloads, _ = generate_review_drawings(model,
        ReviewDrawingConfig(version=VERSION, plans=(), pixels_per_model_unit=40.0))
    legacy = json.loads(payloads['drawing_set'])['drawing_set']['schedules']
    assert [s['id'] for s in legacy] == expected_panel_ids
    ordinary_ids = {s.id for s in generate_schedules(model)}
    assert not ordinary_ids.intersection(expected_panel_ids)
    assert ordinary_ids.union(expected_panel_ids) == {s.id for s in combined.schedules}
    assert model.to_json() == original
