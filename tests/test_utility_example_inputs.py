"""Cheap fixture admission; the companion's PDF execution stays in its example."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

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
    assert first['site']['drawing']['scale']==25.
    assert all(record.scale_denominator==100. for feature in model.site_features for record in feature.evidence)


@pytest.mark.parametrize('reference_only',[False,True])
def test_dense_site_measurements_are_complete_in_measured_legend(tmp_path,reference_only):
    from oabm.drawings.company_sheets import resolve_company_sheet_set,render_company_sheet_svg
    from oabm.drawings.model_utility import render_model_site_body
    before,_=example.prepare(tmp_path/'inputs')
    request=json.loads(before.read_text());root=before.parent
    model=BuildingModel.from_json((root/'model.json').read_bytes())
    if reference_only:
        from dataclasses import replace
        model=replace(model,site_requirements=tuple(replace(row,kind='horizontal_reference_distance',minimum_m=None)
            if row.kind=='minimum_horizontal_separation' else row for row in model.site_requirements))
    original=model.to_json()
    sheet=next(row for row in resolve_company_sheet_set(root/'profile.json',root/'job.json').sheets if row.id=='U1')
    body,receipt=render_model_site_body(model,sheet,source_files={key:root/path for key,path in request['sources'].items()},drawing=request['site']['drawing'])
    # Actual shared renderer verifies finite page bounds and measured ink.
    render_company_sheet_svg(sheet,body_svg=body)
    bindings=receipt['projection']['dimension_bindings']
    assert len(bindings)==5
    assert sorted(row['value_m'] for row in bindings)==pytest.approx([1.2,1.2,1.2,1.2,2.])
    legend=[primitive for view in receipt['drawing']['views'] for primitive in view['primitives'] if primitive['id'].startswith('legend:')]
    notes=' '.join(dict(row['metadata']).get('full_text','') for row in legend)
    for row in bindings:
        assert row['presentation']=='measured_legend' and len(row['endpoints'])==2
        assert row['dimension_id'] in notes and row['source_id'] in notes and row['target_id'] in notes
    assert 'SURFACE EDGE' in notes
    if reference_only:
        assert 'REFERENCE ANCHORS ONLY' in notes and 'min 0.5 m' not in notes
        assert all('meets_supplied_horizontal_minimum' not in row for row in receipt['projection']['measurement_requirements'])
    else:assert 'min 0.5 m' in notes
    assert all(not view['dimensions'] for view in receipt['drawing']['views'])
    assert model.to_json()==original


def test_companion_model_identity_is_separate_from_primary():
    from reference_production_set import source_model
    primary,_=source_model('reference')
    companion=example._model('a'*64)
    assert primary.model_id != companion.model_id
    assert not ({row.id for row in primary.electrical_equipment} & {row.id for row in companion.service_nodes})


def test_companion_receipt_checks_bindings_and_retains_identity(tmp_path,monkeypatch):
    """Stub rendering only; exercise the receipt/readback checks, not PDF validity."""
    def delivery(request,*,output_directory):
        output_directory.mkdir();(output_directory/'derived').mkdir();(output_directory/'inputs').mkdir()
        edited='field-edit' in request.name
        binding=('b' if edited else 'a')*64
        (output_directory/'inputs/model.json').write_bytes((request.parent/'model.json').read_bytes())
        records={'site-projection':{'projection':{'model_sha256':binding,'measurement_requirements':[{'distance_m':1 if edited else 2}]}},
                 'load-report':{'source_model_sha256':binding},'single-line':{'topology':{'model_sha256':binding}}}
        for name,data in records.items():(output_directory/'derived'/f'{name}.json').write_text(json.dumps(data))
        return output_directory
    monkeypatch.setattr(example,'create_utility_delivery',delivery)
    root=example.create_example(tmp_path/'example')
    receipt=json.loads((root/'EXAMPLE-ACCEPTANCE.json').read_text())
    assert receipt['model_id']=='model:synthetic-utility-example'
    assert receipt['identities_preserved'] and receipt['document_source_bindings_verified']
    assert receipt['baseline_distance_m']==2 and receipt['edited_distance_m']==1
    assert not receipt['engineering_approval'] and not receipt['private_project_acceptance']


@pytest.mark.parametrize('variant', ['reference','residential','commercial'])
def test_reference_cli_hook_order_and_success_output(tmp_path,monkeypatch,capsys,variant):
    import reference_production_set as command
    import utility_service_set as utility
    events=[]
    root=tmp_path/'review';delivery=root/'attempt'/'delivery'
    monkeypatch.setattr(command,'prepare',lambda path,**kw: events.append(('prepare',path)) or 'request')
    monkeypatch.setattr(command,'create_production_sheet_set',lambda request,**kw: events.append(('primary',request)) or delivery)
    def companion(path):
        assert capsys.readouterr().out==''
        events.append(('companion',path))
    monkeypatch.setattr(utility,'create_example',companion)
    assert command.main([str(root),'--variant',variant])==delivery
    assert [event[0] for event in events]==(['prepare','primary','companion'] if variant=='reference' else ['prepare','primary'])
    if variant=='reference':assert events[-1][1]==root/'inputs'/'model-utility-example'
    assert capsys.readouterr().out==str(delivery)+'\n'


@pytest.mark.parametrize('failure', ['primary','companion'])
def test_reference_cli_propagates_failure_without_success_path(tmp_path,monkeypatch,capsys,failure):
    import reference_production_set as command
    import utility_service_set as utility
    events=[]
    monkeypatch.setattr(command,'prepare',lambda *args,**kwargs:'request')
    def primary(*args,**kwargs):
        events.append('primary')
        if failure=='primary':raise ValueError('primary failed')
        return tmp_path/'delivery'
    def companion(*args,**kwargs):
        events.append('companion');raise ValueError('companion failed')
    monkeypatch.setattr(command,'create_production_sheet_set',primary)
    monkeypatch.setattr(utility,'create_example',companion)
    with pytest.raises(ValueError,match=failure+' failed'):
        command.main([str(tmp_path/'review')])
    assert events==(['primary'] if failure=='primary' else ['primary','companion'])
    assert capsys.readouterr().out==''
