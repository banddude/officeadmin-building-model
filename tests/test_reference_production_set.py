"""Full prepared sample inputs exercise the same production API, no raster QA."""
from pathlib import Path
import sys,json,hashlib
import pytest
from oabm.model import BuildingModel
from oabm.drawings.production_set import create_production_sheet_set
sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
from reference_production_set import prepare


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
