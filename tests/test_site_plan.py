"""Public-safe supplied-geometry regression and physical-sheet integration."""
import copy
import json
from pathlib import Path

import pytest

from oabm.drawings.site_plan import prepare_site_plan, render_site_plan
from oabm.drawings.sheets import SheetSpec

ROOT=Path(__file__).parents[1]

@pytest.fixture
def request_data():
    return json.loads((ROOT/'examples/synthetic-site-plan.json').read_text())

@pytest.fixture
def spec():
    return SheetSpec('E1',650,420,10,60,'Synthetic site plan','A',(('Company','Synthetic Company'),))


def test_repeatable_no_mutation_source_mapping_and_scale(request_data,spec):
    original=copy.deepcopy(request_data)
    d,c,s,a=render_site_plan(request_data,spec)
    assert request_data==original
    assert render_site_plan(request_data,spec)[2].svg==s.svg
    assert len(s.manifest()['source_index'])==len(request_data['features'])+1
    sheet=c.to_dict()
    view=next(v for v in sheet['views'] if v['view_id']=='site-plan')
    geometry={p['id']:p for p in view['primitives'] if p['id'].startswith('geometry:')}
    assert geometry['geometry:gear-a']['points_mm'][0]=={'x':177.,'y':177.}
    building=geometry['geometry:building-a']['points_mm']
    assert building[1]['x']-building[0]['x']==125 # 25m at 1:200
    assert geometry['geometry:future-a']['style']['pattern']=='dash'
    assert geometry['geometry:route-a']['style']['pattern']=='solid'
    assert 'geometry:empty-a' not in geometry
    assert 'geometry:future-b' not in geometry
    assert a['route_states'][-2:]==[
        {'id':'empty-a','status':'empty','has_geometry':False},
        {'id':'future-b','status':'future','has_geometry':False}]
    assert a['unplaced_labels'] # crowded route labels retained, source points unchanged
    for row in a['mapping']:
        assert row['input_points']==next(r['points'] for r in original['features'] if r['id']==row['source_id'])
    assert 'Future A [future] (future-a)' in s.svg
    assert 'data-titleblock-row' in s.svg and 'Synthetic Company' in s.svg


def test_explicit_units_equivalent_geometry(request_data,spec):
    baseline=render_site_plan(request_data,spec)[1].to_dict()['views']
    request_data['units']='ft'
    for row in request_data['features']:
        row['points']=[[x/.3048,y/.3048] for x,y in row['points']]
    converted=render_site_plan(request_data,spec)[1].to_dict()['views']
    assert next(v for v in baseline if v['view_id']=='site-plan')==next(v for v in converted if v['view_id']=='site-plan')


@pytest.mark.parametrize('mutation,match',[
    (lambda r:r.update(units='degrees'),'units'),
    (lambda r:r['frame'].update(axes='EPSG:4326'),'frame.axes'),
    (lambda r:r['features'][0].update(frame_id='other'),'incompatible frame'),
    (lambda r:r['features'].append(copy.deepcopy(r['features'][0])),'duplicate'),
    (lambda r:r['features'][0].update(points=[[0,0],[1,1],[0,1],[1,0]]),'self-intersects'),
    (lambda r:r['features'][0].update(points=[[0,0],[1,0],[2,0]]),'backtracks|area'),
    (lambda r:r['features'][0]['points'].append(r['features'][0]['points'][0]),'repeats'),
    (lambda r:r['features'][0]['points'][0].__setitem__(0,float('nan')),'finite'),
    (lambda r:r['features'][0]['points'][0].__setitem__(0,True),'finite'),
    (lambda r:r['features'][0].pop('evidence'),'required'),
    (lambda r:r['features'][0]['evidence'].update(confidence=1.2),'confidence'),
    (lambda r:r['features'][4].update(points=[[1,2],[2,3]]),'equipment'),
    (lambda r:r['features'][-2].update(points=[[1,1],[2,2]]),'empty route'),
    (lambda r:r['features'][6].update(route_status='inferred'),'status'),
    (lambda r:r['drawing'].update(scale=0),'positive'),
    (lambda r:r['drawing'].update(north_degrees=360),'north'),
    (lambda r:r['drawing'].update(padding_mm=1),'reserve'),
    (lambda r:r['drawing'].update(placement_mm=[-1,15]),'non-negative'),
])
def test_invalid(request_data,spec,mutation,match):
    mutation(request_data)
    with pytest.raises(ValueError,match=match):
        render_site_plan(request_data,spec)


def test_actual_geometry_overflow_no_fit(request_data,spec):
    request_data['features'][0]['points'][1][0]=1000
    with pytest.raises(ValueError,match='exceeds available'):
        render_site_plan(request_data,spec)


def test_label_overflow_does_not_relocate_equipment(request_data,spec):
    request_data['features'][4]['label']='Long equipment name '*40
    drawings,_,_,_=render_site_plan(request_data,spec)
    full=request_data['features'][4]['label']+' (gear-a)'
    lines=[p for view in drawings.views for p in view.primitives if dict(p.metadata).get('full_text')==full]
    assert len(lines)>1
    assert ''.join(p.text for p in sorted(lines,key=lambda p:int(dict(p.metadata)['line_index'])))==full
    assert all(p.source_ids==('gear-a',) for p in lines)
    assert request_data['features'][4]['points']==[[27,15]]


def test_unknown_north_and_no_geometry(request_data,spec):
    request_data['drawing']['north_degrees']=None
    assert 'North: not supplied' in render_site_plan(request_data,spec)[2].svg
    request_data['features']=request_data['features'][-2:]
    with pytest.raises(ValueError,match='actual geometry'):
        prepare_site_plan(request_data)


def test_company_body_bounds_no_duplicate_branding(request_data):
    from types import SimpleNamespace
    from oabm.drawings.site_plan import render_site_body
    sheet=SimpleNamespace(id='E1',title='Site',width_mm=650,height_mm=420,drawing_bounds_mm=(10,10,500,350))
    body,drawings,composed,audit,source_map=render_site_body(request_data,sheet)
    assert body.startswith('<g ')
    assert 'data-titleblock-row' not in body
    assert 'Revision:' not in body
    assert source_map['body_sha256']
    sheet.drawing_bounds_mm=(10,10,450,350)
    with pytest.raises(ValueError,match='company drawing_bounds'):
        render_site_body(request_data,sheet)


def test_portable_request_schema(request_data):
    import jsonschema
    schema=json.loads((ROOT/'contracts/supplied-site-plan-v1.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(request_data)
    request_data['drawing']['scale']=True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(request_data)


def test_adjacent_backtracking_rejected(request_data,spec):
    request_data['features'][1]['points']=[[0,0],[2,0],[1,0]]
    with pytest.raises(ValueError,match='backtracks'):
        render_site_plan(request_data,spec)


def test_real_company_pipeline_retains_input_and_vector_pdf(tmp_path,monkeypatch):
    company=pytest.importorskip('oabm.drawings.company_sheets')
    from oabm.drawings.site_plan import create_site_plan
    from oabm.drawings.sheet_pdf import _dependencies
    try:
        _,_,pypdf=_dependencies()
    except ImportError as exc:
        pytest.skip(str(exc))
    actual=company.create_company_sheet_set
    calls=[]
    def invoke(*args,**kwargs):
        calls.append(kwargs['bodies'])
        return actual(*args,**kwargs)
    monkeypatch.setattr(company,'create_company_sheet_set',invoke)
    path=create_site_plan(ROOT/'examples/synthetic-site-plan.json',
                          ROOT/'examples/synthetic-site-company-profile.json',
                          ROOT/'examples/synthetic-site-company-job.json',output_directory=tmp_path/'output')
    assert len(calls)==1 and set(calls[0])=={'E1'}
    manifest=json.loads((path/'MANIFEST.json').read_text())
    shared=path/manifest['shared_delivery']
    svg=(shared/'sheets/001-E1.svg').read_text()
    assert svg.count('Synthetic Company')==1
    assert 'Future A [future] (future-a)' in svg
    assert (path/'site-input.json').read_bytes()==(ROOT/'examples/synthetic-site-plan.json').read_bytes()
    pdf=pypdf.PdfReader(shared/'sheet-set.pdf',strict=True)
    assert len(pdf.pages)==1
    assert float(pdf.pages[0].mediabox.width)==pytest.approx(650*72/25.4,abs=.01)
    assert not list(pdf.pages[0].images)
    text=pdf.pages[0].extract_text()
    assert 'Synthetic Company' in text and 'Building A' in text and '1:200' in text
    assert 'Unrouted A [empty] (empty-a)' in text
    assert 'Future B [future] (future-b)' in text
    import hashlib
    for row in manifest['files']:
        assert hashlib.sha256((path/row['path']).read_bytes()).hexdigest()==row['sha256']
    with pytest.raises(FileExistsError):
        create_site_plan(ROOT/'examples/synthetic-site-plan.json',ROOT/'examples/synthetic-site-company-profile.json',
                         ROOT/'examples/synthetic-site-company-job.json',output_directory=tmp_path/'output')


def test_company_scale_mismatch_refused(tmp_path):
    pytest.importorskip('oabm.drawings.company_sheets')
    from oabm.drawings.site_plan import create_site_plan
    job=json.loads((ROOT/'examples/synthetic-site-company-job.json').read_text())
    job['fields']['scale']='1:100'
    jp=tmp_path/'job.json'
    jp.write_text(json.dumps(job))
    with pytest.raises(ValueError,match='scale field'):
        create_site_plan(ROOT/'examples/synthetic-site-plan.json',ROOT/'examples/synthetic-site-company-profile.json',
                         jp,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_shared_failure_never_publishes_completed_site(tmp_path,monkeypatch):
    company=pytest.importorskip('oabm.drawings.company_sheets')
    from oabm.drawings.site_plan import create_site_plan
    def fail(*args,**kwargs):
        raise ValueError('synthetic shared failure')
    monkeypatch.setattr(company,'create_company_sheet_set',fail)
    output=tmp_path/'failed'
    with pytest.raises(ValueError,match='synthetic shared failure'):
        create_site_plan(ROOT/'examples/synthetic-site-plan.json',ROOT/'examples/synthetic-site-company-profile.json',
                         ROOT/'examples/synthetic-site-company-job.json',output_directory=output)
    assert (output/'.pending/site-input.json').exists()
    assert (output/'failure.json').exists()
    assert not (output/'delivery').exists()


def test_dashed_body_has_source_bound_explicit_strokes(request_data):
    from types import SimpleNamespace
    from oabm.drawings.site_plan import render_site_body
    import xml.etree.ElementTree as ET
    sheet=SimpleNamespace(id='E1',title='Site',width_mm=650,height_mm=420,drawing_bounds_mm=(10,10,540,410))
    body,_,_,_,source_map=render_site_body(request_data,sheet)
    root=ET.fromstring(body)
    item=next(row for row in source_map['items'] if row['source_item_id']=='geometry:future-a')
    node=root[source_map['renderer_svg_id_to_body_child_path'][item['svg_id']][0]]
    assert node.tag.endswith('g')
    assert len(node)>1
    assert all(n.tag.endswith('line') for n in node)
    assert item['source_ids']==['future-a']
    assert 'stroke-dasharray' not in body
    assert 'data-titleblock' not in body


def test_fallback_and_empty_intent_text_keeps_feature_identity(request_data):
    from types import SimpleNamespace
    from oabm.drawings.site_plan import render_site_body
    import xml.etree.ElementTree as ET
    original=copy.deepcopy(request_data)
    sheet=SimpleNamespace(id='E1',title='Site',width_mm=650,height_mm=420,drawing_bounds_mm=(10,10,540,410))
    body,drawings,_,audit,source_map=render_site_body(request_data,sheet)
    root=ET.fromstring(body)
    items={item['source_item_id']:item for item in source_map['items']}
    expected={
        'Route A (route-a)':'route-a',
        'Future A [future] (future-a)':'future-a',
        'Unrouted A [empty] (empty-a)':'empty-a',
        'Future B [future] (future-b)':'future-b',
    }
    legend=next(view for view in drawings.views if view.id=='site-legend')
    for text,source_id in expected.items():
        primitive=next(p for p in legend.primitives if p.text==text)
        assert primitive.source_ids==(source_id,)
        item=items[primitive.id]
        assert item['source_ids']==[source_id]
        node=root[source_map['renderer_svg_id_to_body_child_path'][item['svg_id']][0]]
        assert node.text==text
        mapping=next(row for row in audit['mapping'] if row['source_id']==source_id)
        assert primitive.id in mapping['primitive_ids']
    for source_id in ('empty-a','future-b'):
        notice=next(p for p in legend.primitives if p.text=='Intent only: no geometry' and p.source_ids==(source_id,))
        assert items[notice.id]['source_ids']==[source_id]
        assert not any(p.kind!='text' and source_id in p.source_ids for v in drawings.views for p in v.primitives)
    assert request_data==original


def test_uses_shared_font_metrics_for_body_text(request_data,monkeypatch):
    from types import SimpleNamespace
    import oabm.drawings.site_plan as site
    actual=site.measure_sheet_text
    calls=[]
    def measure(text,size,**kwargs):
        calls.append(text)
        return actual(text,size,**kwargs)
    monkeypatch.setattr(site,'measure_sheet_text',measure)
    sheet=SimpleNamespace(id='E1',title='Site',width_mm=650,height_mm=420,drawing_bounds_mm=(10,10,540,410))
    _,_,_,_,mapping=site.render_site_body(request_data,sheet)
    assert 'Unrouted A [empty] (empty-a)' in calls
    assert 'Future B [future] (future-b)' in calls
    assert mapping['measured_text']
    assert all(row['width_mm']>0 for row in mapping['measured_text'].values())
    assert mapping['text_measurement_helper']=='oabm.drawings.company_sheets.measure_sheet_text'


def test_covered_omega_label_is_preserved_without_substitution(request_data,spec):
    request_data['features'][4]['label']='Gear Ω';original=copy.deepcopy(request_data)
    _,_,rendered,_=render_site_plan(request_data,spec)
    assert 'Gear Ω' in rendered.svg and request_data==original


def test_actual_missing_site_glyph_is_refused(request_data,spec):
    from oabm.drawings.company_sheets import _font
    coverage=_font('DejaVu Sans',400)[0]._oabm_codepoints
    glyph=next(c for c in ('⌀','🧑','𓀀') if ord(c) not in coverage)
    request_data['features'][4]['label']='Gear '+glyph
    with pytest.raises(ValueError,match=r'glyph U\+'):
        render_site_plan(request_data,spec)


def test_intent_label_whitespace_is_preserved(request_data):
    from types import SimpleNamespace
    from oabm.drawings.site_plan import render_site_body
    import xml.etree.ElementTree as ET
    request_data['features'][-2]['label']='  Unrouted  A  '
    sheet=SimpleNamespace(id='E1',title='Site',width_mm=650,height_mm=420,drawing_bounds_mm=(10,10,540,410))
    body,_,_,_,mapping=render_site_body(request_data,sheet)
    root=ET.fromstring(body)
    texts=[node.text for node in root if node.tag.endswith('text')]
    assert '  Unrouted  A   [empty] (empty-a)' in texts
