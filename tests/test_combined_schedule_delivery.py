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
