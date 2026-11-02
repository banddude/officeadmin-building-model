"""Full prepared sample inputs exercise the same production API, no raster QA."""
from pathlib import Path
import sys,json,hashlib
import pytest
from oabm.model import BuildingModel
from oabm.drawings.production_set import create_production_sheet_set
sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
from reference_production_set import prepare,generate


@pytest.mark.parametrize('variant,expected_equipment,expected_connections',[
    ('reference',8,7),('residential',4,3),('commercial',32,31)])
def test_complete_samples_share_model_fields_index_and_source_qualified_views(tmp_path,variant,expected_equipment,expected_connections):
    request=prepare(tmp_path/'inputs',variant=variant,pdf=False)
    source=(request.parent/'model.json').read_bytes();model=BuildingModel.from_json(source)
    assert len(model.electrical_equipment)==expected_equipment
    assert len(model.support_components)==2 and len(model.routes)==1
    root=create_production_sheet_set(request,output_directory=tmp_path/'run')
    manifest=json.loads((root/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['sheet_order'][0:2]==['G0.0','E1.0'] and manifest['sheet_order'][-1]=='ED1'
    assert [r['id'] for r in manifest['cover_index']]==manifest['sheet_order']
    cover=json.loads((root/'derived/cover-expanded.json').read_bytes())
    assert cover['sheets']==manifest['cover_index']
    assert (root/'inputs/model.json').read_bytes()==source
    topology=json.loads((root/'derived/single-line-topology-layout.json').read_bytes())['topology']
    assert topology['model_sha256']==hashlib.sha256(model.to_json(indent=None).encode()).hexdigest()
    assert len(topology['equipment'])==expected_equipment and len(topology['connections'])==expected_connections
    composition=json.loads((root/'derived/electrical-composition.json').read_bytes())
    feeder_rows=[row for page in composition['single_line']['sheets'] for row in page['feeder_grid_rows']]
    assert len(feeder_rows)==expected_connections
    assert {row['source_id'] for row in feeder_rows}=={c.id for c in model.circuits}
    rows=json.loads((root/'derived/schedule-rows.json').read_bytes())
    assert len(rows)==manifest['schedule_row_count']
    assert not rows if variant=='reference' else rows
    shared=json.loads((root/'company/delivery/resolved-sheets.json').read_bytes())
    assert all(row['fields']['project']=='SYNTHETIC '+variant.upper()+' PRODUCTION SET' for row in shared)
    site=next(row for row in shared if row['id']=='E1.0')
    audit=json.loads((root/'derived/site-audit.json').read_bytes())
    assert site['fields']['scale']==audit['presentation']['sheet_scale_label']=='AS NOTED'
    assert audit['presentation']['view_scales']['connection']==50
    assert (root/'inputs/site_graphics.json').read_bytes()==(request.parent/'site-graphics.json').read_bytes()
    requested=json.loads((root/'inputs/details.json').read_bytes())
    assert {sid for row in requested['details'] for sid in row['source_ids']}<={e.id for collection in (model.electrical_equipment,model.support_components,model.walls,model.ports,model.routes) for e in collection}
    for row in manifest['files']:assert hashlib.sha256((root/row['path']).read_bytes()).hexdigest()==row['sha256']


def test_reference_example_retains_independent_as_built_under_existing_checksum_tree(tmp_path):
    import xml.etree.ElementTree as ET
    from oabm.pipeline.as_built import replay_history,snapshot
    root=tmp_path/'reference-review'
    production=generate(root,variant='reference',pdf=False)
    production_manifest=json.loads((production/'PRODUCTION-MANIFEST.json').read_bytes())
    production_files={row['path'] for row in production_manifest['files']}
    for row in production_manifest['files']:
        raw=(production/row['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256']
        assert len(raw)==row['bytes']
    assert not any('as-built-example' in name for name in production_files)
    assert {path.relative_to(production).as_posix() for path in production.rglob('*') if path.is_file()} == (
        production_files | {'PRODUCTION-MANIFEST.json'})
    companion=root/'attempt/as-built-example/delivery'
    manifest=json.loads((companion/'MANIFEST.json').read_bytes())
    assert manifest['status']=='complete' and manifest['review_status']=='unreviewed'
    expected={'canonical-model.json','design-model.json','parent-model.json','history.json',
        'materials.json','materials.csv','parent-differences.json','design-differences.json',
        'drawings/plan-1.svg','drawings/panel-1.svg','inputs/history.json','inputs/transaction.json'}
    assert expected<=set(manifest['files'])
    for name,receipt in manifest['files'].items():
        path=companion/name
        raw=path.read_bytes()
        assert len(raw)==receipt['bytes']
        assert hashlib.sha256(raw).hexdigest()==receipt['sha256']
        assert receipt['revised_model_sha256']==manifest['revised_model_sha256']
        assert path.is_relative_to(root/'attempt')
    # The existing CI find command hashes all regular files below attempt;
    # the existing upload retains that entire enclosing review root.
    checksum_tree={path.relative_to(root).as_posix() for path in (root/'attempt').rglob('*') if path.is_file()}
    assert {f'attempt/as-built-example/delivery/{name}' for name in manifest['files']}<=checksum_tree
    assert 'attempt/as-built-example/delivery/MANIFEST.json' in checksum_tree
    assert 'attempt/AS-BUILT-COMPANION.txt' in checksum_tree
    history=json.loads((companion/'history.json').read_bytes())
    as_built=replay_history(history)
    as_built_json=(companion/'canonical-model.json').read_text()
    assert snapshot(as_built)==as_built_json
    assert hashlib.sha256(as_built_json.encode()).hexdigest()==manifest['revised_model_sha256']
    reference=BuildingModel.from_json((production/'inputs/model.json').read_bytes())
    assert as_built.model_id!=reference.model_id
    assert manifest['revised_model_sha256']!=production_manifest['model_sha256']
    assert (companion/'design-model.json').read_text()==history['design_json']
    materials=json.loads((companion/'materials.json').read_bytes())
    assert materials['model_sha256']==manifest['revised_model_sha256']
    assert sum(row['quantity'] for row in materials['installed_items'] if row['category']=='electrical_box')==2
    assert any(row['category']=='route_length' and row['item_type']=='cable' and row['unit']=='m'
               for row in materials['installed_items'])
    differences=json.loads((companion/'design-differences.json').read_bytes())
    assert differences['changed_count'] and differences['quantity_deltas']
    # Actual artifact inspection of run 41479090133 found a false -1 EVSE
    # credit here: the shared diff kept only the first provenance-split row.
    # Load the retained canonical snapshots, not a mocked quantity report.
    design=BuildingModel.from_json((companion/'design-model.json').read_bytes())
    assert {row.id for row in design.electrical_devices} == {row.id for row in as_built.electrical_devices}
    assert len(design.electrical_devices)==len(as_built.electrical_devices)==2
    assert not [row for row in differences['quantity_deltas'] if row['category']=='device']
    parent_differences=json.loads((companion/'parent-differences.json').read_bytes())
    assert not [row for row in parent_differences['quantity_deltas'] if row['category']=='device']
    assert sum(row['quantity'] for row in materials['installed_items'] if row['category']=='device')==2
    from oabm.qa import build_revision_diff
    assert build_revision_diff(design,as_built).to_dict()==differences
    assert not [row for row in build_revision_diff(as_built,design).quantity_deltas if row.category=='device']
    for name in ('drawings/plan-1.svg','drawings/panel-1.svg'):
        svg=ET.fromstring((companion/name).read_bytes())
        assert svg.findall('.//{http://www.w3.org/2000/svg}text')
    note=(root/'attempt/AS-BUILT-COMPANION.txt').read_text()
    assert 'Do not combine quantities, totals or source claims' in note
    assert 'Actual visual acceptance: PENDING' in note
    # Retained multi-story evidence is a third, explicitly separate model.
    three_story=root/'attempt/as-built-example/three-story-views/delivery'
    three_manifest=json.loads((three_story/'MANIFEST.json').read_bytes())
    three_model=BuildingModel.from_json((three_story/'canonical-model.json').read_bytes())
    assert len({reference.model_id,as_built.model_id,three_model.model_id})==3
    assert three_manifest['status']=='complete' and three_manifest['review_status']=='unreviewed'
    for name,receipt in three_manifest['files'].items():
        raw=(three_story/name).read_bytes()
        assert len(raw)==receipt['bytes'] and hashlib.sha256(raw).hexdigest()==receipt['sha256']
        assert (three_story/name).is_relative_to(root/'attempt')
    views=json.loads((three_story/'drawings/plans.json').read_bytes())['views']
    assert len(views)==3
    middle=next(view for view in views if dict(view['metadata'])['level_id']=='level:middle')
    marker,=[primitive for primitive in middle['primitives']
             if primitive['kind']=='symbol' and primitive['symbol']=='route-vertical-projection']
    assert marker['source_ids']==[three_model.routes[0].id]
    assert float(dict(marker['metadata'])['min_z_m'])==pytest.approx(3)
    assert float(dict(marker['metadata'])['max_z_m'])==pytest.approx(5.8)
    three_svgs=list((three_story/'drawings').glob('plan-*.svg'))
    assert len(three_svgs)==3
    for path in three_svgs:
        assert ET.fromstring(path.read_bytes()).findall('.//{http://www.w3.org/2000/svg}text')
    three_materials=json.loads((three_story/'materials.json').read_bytes())
    assert not three_materials['installed_items'] and three_materials['unresolved_items']
    three_differences=json.loads((three_story/'design-differences.json').read_bytes())
    assert not three_differences['quantity_deltas']
    with pytest.raises(FileExistsError):
        generate(root,variant='reference',pdf=False)


def test_reference_example_propagates_actual_companion_export_failure(tmp_path,monkeypatch):
    from oabm.pipeline import as_built_delivery
    def fail(*args,**kwargs):
        raise ValueError('synthetic companion drawing failure')
    monkeypatch.setattr(as_built_delivery,'_drawings',fail)
    root=tmp_path/'failed-review'
    with pytest.raises(ValueError,match='companion drawing failure'):
        generate(root,variant='reference',pdf=False)
    assert (root/'attempt/delivery/PRODUCTION-MANIFEST.json').is_file()
    assert not (root/'attempt/as-built-example/delivery/MANIFEST.json').exists()
    assert not (root/'attempt/AS-BUILT-COMPANION.txt').exists()
