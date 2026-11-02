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


@pytest.mark.parametrize('reference_only,coincident',[(False,False),(True,False),(True,True)])
def test_dense_site_measurements_are_complete_in_measured_legend(tmp_path,reference_only,coincident):
    from oabm.drawings.company_sheets import resolve_company_sheet_set,render_company_sheet_svg
    from oabm.drawings.model_utility import render_model_site_body
    before,_=example.prepare(tmp_path/'inputs')
    request=json.loads(before.read_text());root=before.parent
    model=BuildingModel.from_json((root/'model.json').read_bytes())
    if reference_only:
        from dataclasses import replace
        from oabm.model import SiteRequirement
        model=replace(model,site_requirements=tuple(replace(row,kind='horizontal_reference_distance',minimum_m=None)
            if row.kind=='minimum_horizontal_separation' else row for row in model.site_requirements))
        model=replace(model,site_requirements=(*model.site_requirements,
            SiteRequirement(id='requirement:separate-clearance',kind='minimum_horizontal_separation',
                source_id='site:gas',target_id='service:meter-main',reference_basis='envelope_to_envelope',minimum_m=.5)))
        if coincident:
            # Distinct source owners may have the same XY. This is not a
            # nonzero line, clearance threshold or reason to lose the record.
            anchor=next(row.plan_position for row in model.service_nodes if row.id=='service:meter-main')
            model=replace(model,site_features=tuple(replace(row,geometry=replace(row.geometry,points=(anchor,)))
                if row.kind=='gas_location' else row for row in model.site_features))
    original=model.to_json()
    sheet=next(row for row in resolve_company_sheet_set(root/'profile.json',root/'job.json').sheets if row.id=='U1')
    body,receipt=render_model_site_body(model,sheet,source_files={key:root/path for key,path in request['sources'].items()},drawing=request['site']['drawing'])
    # Actual shared renderer verifies finite page bounds and measured ink.
    import xml.etree.ElementTree as ET
    rendered=render_company_sheet_svg(sheet,body_svg=body)
    # site_plan._company_body_node intentionally strips IDs/data attributes;
    # its exact separate map points to child paths in the normalized body.
    # company_sheets._body_group preserves text coordinates/content while
    # validating ink and normalizing fonts. Match that actual public contract.
    body_root=ET.fromstring(body)
    rendered_text=[node for node in ET.fromstring(rendered).iter() if node.tag.endswith('}text')]
    legend_items=[row for row in receipt['source_map']['items'] if row['source_item_id'].startswith('legend:')]
    assert legend_items
    legend_nodes=[]
    for item in legend_items:
        expected=body_root
        for index in receipt['source_map']['renderer_svg_id_to_body_child_path'][item['svg_id']]:
            expected=expected[index]
        assert expected.tag.endswith('}text')
        matches=[node for node in rendered_text
                 if (float(node.get('x')),float(node.get('y')),''.join(node.itertext()))==
                    (float(expected.get('x')),float(expected.get('y')),''.join(expected.itertext()))]
        assert len(matches)==1
        legend_nodes.append(matches[0])
    ordered=sorted(legend_nodes,key=lambda node:(float(node.get('y')),float(node.get('x'))))
    assert len({(node.get('x'),node.get('y')) for node in ordered})==len(ordered)
    visible=' '.join(' '.join(''.join(node.itertext()) for node in ordered).split())
    bindings=receipt['projection']['dimension_bindings']
    assert len(bindings)==5
    assert sorted(row['value_m'] for row in bindings)==pytest.approx(sorted([1.2,1.2,1.2,1.2,0. if coincident else 2.]))
    legend=[primitive for view in receipt['drawing']['views'] for primitive in view['primitives'] if primitive['id'].startswith('legend:')]
    notes=' '.join(dict(row['metadata']).get('full_text','') for row in legend)
    for row in bindings:
        assert row['presentation']=='measured_legend' and len(row['endpoints'])==2
        assert row['dimension_id'] in notes and row['source_id'] in notes and row['target_id'] in notes
    assert 'SURFACE EDGE' in notes
    if reference_only:
        assert 'REFERENCE ANCHORS ONLY' in notes and 'min 0.5 m' not in notes
        assert all('meets_supplied_horizontal_minimum' not in row for row in receipt['projection']['measurement_requirements'])
        assert 'SUPPLIED REQUIREMENT: minimum 0.5 m; actual clearance UNRESOLVED; no pass claim.' in notes
        assert 'SUPPLIED REQUIREMENT: minimum 0.5 m;' in visible
        assert 'actual clearance UNRESOLVED; no pass claim.' in visible
        reference_row=next(row for row in receipt['projection']['measurement_requirements'] if row['kind']=='horizontal_reference_distance')
        assert reference_row['minimum_m'] is None
        if coincident:
            zero,= [row for row in bindings if row['value_m']==0]
            assert zero['source_id']!=zero['target_id'] and zero['endpoints'][0]==zero['endpoints'][1]
            assert '0.000 m REFERENCE ANCHORS ONLY' in notes
    else:assert 'min 0.5 m' in notes
    assert 'SUPPLIED REQUIREMENT: width 0.8 m, depth 0.8 m; actual working clearance UNRESOLVED; no pass claim.' in notes
    assert 'SUPPLIED REQUIREMENT: width 0.8 m, depth 0.8 m;' in visible
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
    import as_built_quick_edit as as_built
    import as_built_physical_route as physical
    stub=lambda *args,**kwargs:{'revised_model_sha256':'a'*64}
    monkeypatch.setattr(as_built,'create_synthetic_delivery',stub)
    monkeypatch.setattr(as_built,'create_three_story_delivery',stub)
    monkeypatch.setattr(physical,'create_physical_field_delivery',stub)
    events=[]
    root=tmp_path/'review';delivery=root/'attempt'/'delivery'
    monkeypatch.setattr(command,'prepare',lambda path,**kw: events.append(('prepare',path)) or 'request')
    def primary(request,**kw):
        kw['output_directory'].mkdir(parents=True)
        events.append(('primary',request));return delivery
    monkeypatch.setattr(command,'create_production_sheet_set',primary)
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
    import as_built_quick_edit as as_built
    import as_built_physical_route as physical
    stub=lambda *args,**kwargs:{'revised_model_sha256':'a'*64}
    monkeypatch.setattr(as_built,'create_synthetic_delivery',stub)
    monkeypatch.setattr(as_built,'create_three_story_delivery',stub)
    monkeypatch.setattr(physical,'create_physical_field_delivery',stub)
    events=[]
    monkeypatch.setattr(command,'prepare',lambda *args,**kwargs:'request')
    def primary(*args,**kwargs):
        events.append('primary')
        if failure=='primary':raise ValueError('primary failed')
        kwargs['output_directory'].mkdir(parents=True)
        return tmp_path/'delivery'
    def companion(*args,**kwargs):
        events.append('companion');raise ValueError('companion failed')
    monkeypatch.setattr(command,'create_production_sheet_set',primary)
    monkeypatch.setattr(utility,'create_example',companion)
    with pytest.raises(ValueError,match=failure+' failed'):
        command.main([str(tmp_path/'review')])
    assert events==(['primary'] if failure=='primary' else ['primary','companion'])
    assert capsys.readouterr().out==''
