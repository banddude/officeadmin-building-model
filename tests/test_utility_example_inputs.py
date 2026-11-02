"""Cheap fixture admission; the companion's PDF execution stays in its example."""
import hashlib
import importlib.util
import json
from pathlib import Path

from oabm.model import BuildingModel

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('synthetic_utility_example',ROOT/'examples/utility_service_set.py')
example=importlib.util.module_from_spec(spec);spec.loader.exec_module(example)


def test_companion_inputs_are_independent_synthetic_canonical_facts(tmp_path):
    before,after=example.prepare(tmp_path/'inputs')
    first=json.loads(before.read_text());second=json.loads(after.read_text())
    model=BuildingModel.from_json((before.parent/'model.json').read_bytes())
    assert model.model_id=='model:synthetic-utility-example' and model.schema_version=='1.8.0'
    assert not model.electrical_equipment and not model.ports and not model.routes
    assert all(row.specification_status=='unresolved' for row in model.service_connections)
    assert 'field_edit' not in first and second['field_edit']['x_m']==4.
    raw=(before.parent/'reference-plan.svg').read_bytes()
    assert all(record.sha256==hashlib.sha256(raw).hexdigest() for feature in model.site_features for record in feature.evidence)
    assert first['single_line']['service_only'] is True
    profile=json.loads((before.parent/'load-inputs.json').read_text())
    assert profile['general_demand']['first_va']==8000 and profile['general_demand']['remainder_factor']==.5
    assert {row['va'] for row in profile['components']}=={18000,1500,600}
