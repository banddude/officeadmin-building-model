"""Synthetic consumed-input disclosures; independent from capture provenance."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.importers.roomplan import RoomPlanImportOptions, import_captured_room, load_captured_room
from oabm.model import BuildingModel

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/roomplan/captured-room-3d.json'


def source():
    return json.loads(FIXTURE.read_text())


def inputs(model):
    return model.attributes['roomplan']['import_inputs']


def test_document_identity_wins_but_caller_source_can_describe_provenance():
    data = source()
    result = import_captured_room(data, source_id='fixture:caller-description')
    record = inputs(result)
    assert record['identity'] == {'value': data['identifier'], 'origin': 'document.identifier'}
    assert record['provenance_source_id'] == {'value': 'fixture:caller-description', 'origin': 'argument.source_id'}
    assert record['name']['origin'] == 'generated_from_identity'


def test_bundle_identity_and_explicit_name_are_recorded_without_inventing_them():
    data = source(); del data['identifier']
    result = import_captured_room(data, source_id='fixture:bundle', provenance_source_id='fixture:file', name='Synthetic room')
    assert inputs(result)['identity'] == {'value': 'fixture:bundle', 'origin': 'argument.source_id'}
    assert inputs(result)['provenance_source_id'] == {'value': 'fixture:file', 'origin': 'argument.provenance_source_id'}
    assert inputs(result)['name'] == {'value': 'Synthetic room', 'origin': 'argument.name'}


def test_omission_empty_options_and_explicit_same_default_remain_distinguishable():
    implicit = import_captured_room(source())
    empty = import_captured_room(source(), options=RoomPlanImportOptions())
    explicit = import_captured_room(source(), options=RoomPlanImportOptions(wall_surface_thickness_m=.001))
    assert not inputs(implicit)['options_argument_provided']
    assert inputs(empty)['options_argument_provided']
    assert not inputs(empty)['options']['wall_surface_thickness_m']['provided']
    entry = inputs(explicit)['options']['wall_surface_thickness_m']
    assert entry == {'value': .001, 'default': .001, 'provided': True, 'differs_from_default': False}
    assert [w.thickness_m for w in implicit.walls] == [w.thickness_m for w in explicit.walls]
    assert [w.id for w in implicit.walls] == [w.id for w in explicit.walls]


@pytest.mark.parametrize('name,value', [
    ('wall_surface_thickness_m', .12), ('floor_surface_thickness_m', .08),
    ('opening_surface_depth_m', .05), ('object_min_dimension_m', .002),
    ('orphan_opening_host_tolerance_m', .8), ('orphan_opening_host_ambiguity_m', .02),
])
def test_each_option_records_its_effective_value_and_override(name, value):
    model = import_captured_room(source(), options=RoomPlanImportOptions(**{name: value}))
    records = inputs(model)['options']
    assert len(records) == 6
    assert records[name]['provided'] and records[name]['differs_from_default']
    assert records[name]['value'] == value
    assert sum(r['provided'] for r in records.values()) == 1


def test_entity_trace_reaches_actual_canonical_fields_and_model_records():
    data = source(); data['objects'][0]['dimensions'][0] = 0
    model = import_captured_room(data, options=RoomPlanImportOptions(wall_surface_thickness_m=.1, object_min_dimension_m=.02))
    for entity, key, target in [
        (model.walls[0], 'wall_surface_thickness_m', 'thickness_m'),
        (model.slabs[0], 'floor_surface_thickness_m', 'thickness_m'),
        (model.obstacles[0], 'object_min_dimension_m', 'geometry.size.x'),
    ]:
        record = entity.attributes['roomplan']['import_inputs'][key]
        assert target in record['target_paths']
        assert record['value'] == inputs(model)['options'][key]['value']
        assert record['model_record_path'] == 'attributes.roomplan.import_inputs.options.' + key
    for opening in model.openings:
        record = opening.attributes['roomplan']['import_inputs']['opening_surface_depth_m']
        assert record['target_paths'] == ['size.y']
        assert record['rule'].startswith('max(')
        if opening.attributes['roomplan']['host_inferred']:
            for key in ['orphan_opening_host_tolerance_m', 'orphan_opening_host_ambiguity_m']:
                assert opening.attributes['roomplan']['import_inputs'][key]['target_paths'] == ['host_id']
        else:
            assert 'orphan_opening_host_tolerance_m' not in opening.attributes['roomplan']['import_inputs']


def test_measured_dimensions_do_not_claim_fallback_option_use():
    data = source()
    for wall in data['walls']: wall['dimensions'][2] = .15
    data['floors'][0]['dimensions'][2] = .2
    for collection in ['doors','windows','openings']:
        for opening in data[collection]: opening['dimensions'][2] = .12
    model = import_captured_room(data, options=RoomPlanImportOptions(wall_surface_thickness_m=.8))
    for entity in [*model.walls, *model.slabs, *model.obstacles]:
        assert 'import_inputs' not in entity.attributes['roomplan']
    for opening in model.openings:
        assert 'opening_surface_depth_m' not in opening.attributes['roomplan'].get('import_inputs', {})


def test_load_wrapper_labels_provenance_argument_not_capture_identity(tmp_path):
    path = tmp_path/'synthetic.json';path.write_text(json.dumps(source()))
    model = load_captured_room(path)
    assert inputs(model)['identity']['origin'] == 'document.identifier'
    assert inputs(model)['provenance_source_id']['value'] == str(path)
    assert inputs(model)['provenance_source_id']['origin'] == 'argument.provenance_source_id'


def test_explicit_generated_name_differs_from_omitting_name():
    base = import_captured_room(source())
    named = import_captured_room(source(), name=base.name)
    assert base.name == named.name
    assert inputs(base)['name']['origin'] != inputs(named)['name']['origin']


def test_input_records_are_deterministic_detached_and_json_round_trip():
    data = source(); before = copy.deepcopy(data)
    options = RoomPlanImportOptions(.02)
    first = import_captured_room(data, options=options)
    data['walls'].reverse()
    second = import_captured_room(data, options=options)
    assert first.to_json() == second.to_json()
    assert BuildingModel.from_json(first.to_json()).to_dict() == first.to_dict()
    copied = options.input_records(); copied['wall_surface_thickness_m']['value'] = 99
    assert options.wall_surface_thickness_m == .02
    data['walls'].reverse(); assert data == before


def test_dataclass_replace_reports_its_constructor_arguments_honestly():
    # replace supplies every init field to the new constructor. The record makes
    # that concrete claim, not a claim about which knob a human intended to turn.
    options = replace(RoomPlanImportOptions(), wall_surface_thickness_m=.1)
    records = options.input_records()
    assert all(r['provided'] for r in records.values())
    assert sum(r['differs_from_default'] for r in records.values()) == 1


def test_copy_pickle_and_asdict_preserve_configuration_compatibility():
    import pickle
    from dataclasses import asdict
    options = RoomPlanImportOptions(wall_surface_thickness_m=.001)
    for copied in [copy.copy(options), copy.deepcopy(options), pickle.loads(pickle.dumps(options))]:
        assert copied.input_records() == options.input_records()
    assert len(asdict(options)) == 6
    assert not any(k.startswith('_') for k in asdict(options))
    rebuilt = RoomPlanImportOptions(**asdict(options))
    assert rebuilt == options
    assert all(v['provided'] for v in rebuilt.input_records().values())


def test_legacy_options_without_constructor_evidence_report_unknown():
    options = RoomPlanImportOptions()
    object.__delattr__(options, '_supplied_fields')  # legacy six-field pickle state
    assert all(r['provided'] is None for r in options.input_records().values())
    assert all(not r['differs_from_default'] for r in options.input_records().values())
    assert inputs(import_captured_room(source(), options=options))['options_argument_provided']


def test_default_import_changes_only_input_disclosures_against_prechange_baseline():
    import hashlib
    # SHA from the existing public synthetic capture imported at 815be70,
    # before this feature. It covers geometry, IDs, confidence and provenance.
    data = import_captured_room(source(), source_id='fixture:input-disclosure-baseline').to_dict()
    data['attributes']['roomplan'].pop('import_inputs')
    for collection in data.values():
        if not isinstance(collection, list): continue
        for entity in collection:
            if isinstance(entity, dict):
                entity.get('attributes', {}).get('roomplan', {}).pop('import_inputs', None)
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == 'ef7373ea07f1fe9311f1f60aa047dbea275d0b208dac6b65c278f55b002b8533'
