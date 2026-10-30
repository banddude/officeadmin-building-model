"""Explicit finite fit selection against real company paper, never source edits."""
import copy
import json
import math
from pathlib import Path

import pytest

from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.site_plan import render_site_body,create_site_plan

ROOT=Path(__file__).parents[1]


def request(shape='wide',units='m'):
    data=json.loads((ROOT/'examples/synthetic-site-plan.json').read_text())
    settings=data['drawing']
    settings.update(scale=None,scale_mode='fit',allowed_scales=[200,1000,5000,200000])
    for feature in data['features']:
        for point in feature['points']:
            if shape=='wide':point[0]*=20
            if shape=='tall':point[1]*=20
            if shape=='extreme':point[0]*=1000;point[1]*=.01
            if shape=='rotated':
                x,y=point
                angle=math.radians(33)
                point[:]=[20*(x*math.cos(angle)-y*math.sin(angle)),20*(x*math.sin(angle)+y*math.cos(angle))]
            if units=='ft':point[:]=[coordinate/.3048 for coordinate in point]
    data['units']=units
    return data


def sheet():
    return resolve_company_sheet_set(ROOT/'examples/synthetic-site-company-profile.json',ROOT/'examples/synthetic-site-company-job.json').sheets[0]


@pytest.mark.parametrize('shape',['wide','tall','rotated','extreme'])
@pytest.mark.parametrize('units',['m','ft'])
def test_explicit_fit_preserves_points_and_has_exact_transform(shape,units):
    data=request(shape,units)
    original=copy.deepcopy(data)
    body,drawings,composed,audit,mapping=render_site_body(data,sheet())
    presentation=audit['presentation']
    scale=presentation['selected_scale']
    assert scale in data['drawing']['allowed_scales']
    assert presentation['rejected_candidates']
    assert presentation['rotation_degrees']==0
    assert presentation['north_degrees']==15
    assert presentation['label_height_mm']==2.5
    assert data==original
    assert audit['mapping'][0]['input_points']==data['features'][0]['points']
    world=presentation['world_to_sheet_mm']['matrix']
    placed=next(v for v in composed.to_dict()['views'] if v['view_id']=='site-plan')
    geometry={p['id']:p for p in placed['primitives'] if p['id'].startswith('geometry:')}
    for feature in data['features']:
        if not feature['points']:continue
        for point,out in zip(feature['points'],geometry['geometry:'+feature['id']]['points_mm']):
            assert out['x']==pytest.approx(world[0][0]*point[0]+world[0][2],abs=1e-7)
            assert out['y']==pytest.approx(world[1][1]*point[1]+world[1][2],abs=1e-7)
    legend=next(v for v in drawings.views if v.id=='site-legend')
    bar=next(p for p in legend.primitives if p.id=='scale-bar')
    assert abs(bar.points[1].x-bar.points[0].x)*1000/scale==pytest.approx(20)
    assert next(p for p in legend.primitives if p.id=='scale-label').text==f'0 to {20*scale/1000:g} m'
    north=next(p for p in legend.primitives if p.id=='north')
    direction=(north.points[1].x-north.points[0].x,north.points[1].y-north.points[0].y)
    assert math.atan2(direction[0],direction[1])==pytest.approx(math.radians(15),abs=1e-8)
    assert 'Scale 1:'+f'{scale:g}' in body
    assert mapping['presentation']==presentation
    # Every surviving/fallback feature label retains source identity; zero point
    # intents still have text only, never new geometry.
    for source in ('empty-a','future-b'):
        assert any(item['source_ids']==[source] for item in mapping['items'])
        assert not any(p.kind!='text' and source in p.source_ids for view in drawings.views for p in view.primitives)


def test_fixed_scale_failure_is_not_silently_fitted():
    data=request()
    data['drawing'].update(scale_mode='fixed',scale=200)
    data['drawing'].pop('allowed_scales')
    with pytest.raises(ValueError,match='exceeds'):
        render_site_body(data,sheet())
    assert data['drawing']['scale']==200


def test_failed_fit_has_actionable_candidate_reasons():
    data=request('extreme')
    data['drawing']['allowed_scales']=[200,500]
    with pytest.raises(ValueError,match='Add an explicitly permitted larger denominator') as error:
        render_site_body(data,sheet())
    assert 'Candidates:' in str(error.value)
    assert '200.0' in str(error.value) and '500.0' in str(error.value)


@pytest.mark.parametrize('values',[[],[200,200],[0,200],[True,200]])
def test_invalid_scale_choices(values):
    data=request()
    data['drawing']['allowed_scales']=values
    with pytest.raises(ValueError):render_site_body(data,sheet())


def test_fit_does_not_override_a_fixed_scale_number():
    data=request()
    data['drawing']['scale']=200
    with pytest.raises(ValueError,match='scale=null'):
        render_site_body(data,sheet())


def test_fit_schema_and_collision_fallback():
    import jsonschema
    data=request('extreme')
    jsonschema.Draft202012Validator(json.loads((ROOT/'contracts/supplied-site-plan-v1.schema.json').read_text())).validate(data)
    _,drawings,_,audit,mapping=render_site_body(data,sheet())
    assert audit['unplaced_labels']
    for missing in audit['unplaced_labels']:
        assert any(p.text==missing['text']+' ('+missing['source_ids'][0]+')' and list(p.source_ids)==missing['source_ids']
                   for view in drawings.views for p in view.primitives)


def test_fit_real_company_pdf_prints_selected_scale_and_retains_original_job(tmp_path):
    from oabm.drawings.sheet_pdf import _dependencies
    try:_,_,pypdf=_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    path=tmp_path/'wide.json'
    data=request()
    path.write_text(json.dumps(data))
    delivery=create_site_plan(path,ROOT/'examples/synthetic-site-company-profile.json',ROOT/'examples/synthetic-site-company-job.json',output_directory=tmp_path/'fit')
    manifest=json.loads((delivery/'MANIFEST.json').read_text())
    selected=manifest['scale']
    assert selected==5000
    assert (delivery/'source-job.json').read_bytes()==(ROOT/'examples/synthetic-site-company-job.json').read_bytes()
    shared=delivery/manifest['shared_delivery']
    effective=json.loads((shared/'inputs/job.json').read_text())
    assert effective['sheets'][0]['fields']['scale']=='1:5000'
    assert (shared/'sheet-set.pdf').is_file()
    text=pypdf.PdfReader(shared/'sheet-set.pdf').pages[0].extract_text()
    assert 'Scale 1:5000' in text and '1:5000' in text
    assert 'Unrouted A [empty] (empty-a)' in text
    assert manifest['presentation']['world_to_sheet_mm']['matrix']


def a3_inputs(tmp_path):
    profile=json.loads((ROOT/'examples/synthetic-site-company-profile.json').read_text())
    profile['template'].update(width_mm=420,height_mm=297,rail_width_mm=65)
    pp=tmp_path/'profile.json';pp.write_text(json.dumps(profile))
    job=ROOT/'examples/synthetic-site-company-job.json'
    data=json.loads((ROOT/'examples/synthetic-site-plan.json').read_text())
    data['features']=data['features'][:1]
    data['features'][0]['points']=[[0,0],[5,0],[5,5],[0,5]]
    data['drawing'].update(placement_mm=[10,10],padding_mm=5,legend_width_mm=95)
    return data,pp,job


def test_a3_tiny_site_reserves_independent_wrapped_legend_and_real_pdf(tmp_path):
    data,pp,jp=a3_inputs(tmp_path)
    qualification='Synthetic supplied coordinates; complete original qualification with preserved  spaces. '*8
    data['features'][0]['evidence']['qualification']=qualification
    original=copy.deepcopy(data)
    sheet=resolve_company_sheet_set(pp,jp).sheets[0]
    body,drawings,composed,audit,source_map=render_site_body(data,sheet)
    site=next(v for v in composed.to_dict()['views'] if v['view_id']=='site-plan')
    assert site['bounds_mm'][3]-site['bounds_mm'][1]==pytest.approx(35)
    assert audit['legend_layout']['height_mm']>35
    assert audit['legend_layout']['geometry_independent']
    lines=[p for v in drawings.views for p in v.primitives if dict(p.metadata).get('full_text')==qualification]
    assert len(lines)>1
    assert ''.join(p.text for p in sorted(lines,key=lambda p:int(dict(p.metadata)['line_index'])))==qualification
    assert all(p.source_ids==('building-a',) for p in lines)
    assert data==original
    path=tmp_path/'a3.json';path.write_text(json.dumps(data))
    from oabm.drawings.sheet_pdf import _dependencies
    try:_,_,pypdf=_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    out=create_site_plan(path,pp,jp,output_directory=tmp_path/'a3-output')
    manifest=json.loads((out/'MANIFEST.json').read_text())
    pdf=pypdf.PdfReader(out/manifest['shared_delivery']/'sheet-set.pdf')
    assert float(pdf.pages[0].mediabox.width)==pytest.approx(420*72/25.4,abs=.01)
    assert 'complete original qualification' in pdf.pages[0].extract_text()


def test_fit_packs_many_fallback_facts_into_allowed_a3_columns(tmp_path):
    data,pp,jp=a3_inputs(tmp_path)
    seed=copy.deepcopy(json.loads((ROOT/'examples/synthetic-site-plan.json').read_text())['features'][4])
    for i in range(100):
        feature=copy.deepcopy(seed)
        feature.update(id=f'gear-{i}',label=f'Gear {i}',points=[[2,2]])
        data['features'].append(feature)
    data['drawing'].update(scale=None,scale_mode='fit',allowed_scales=[200],allowed_legend_columns=[1,2])
    _,drawings,_,audit,mapping=render_site_body(data,resolve_company_sheet_set(pp,jp).sheets[0])
    assert audit['presentation']['legend_columns']==2
    assert audit['presentation']['selected_scale']==200
    assert audit['legend_layout']['height_mm']<277
    assert any(row['legend_columns']==1 for row in audit['presentation']['rejected_candidates'])
    for i in range(100):
        source=f'gear-{i}'
        assert any(source in item['source_ids'] for item in mapping['items'])
        if any(source in row['source_ids'] for row in audit['unplaced_labels']):
            assert any(p.text==f'Gear {i} ({source})' and p.source_ids==(source,) for v in drawings.views for p in v.primitives)
    data['drawing']['allowed_legend_columns']=[1]
    with pytest.raises(ValueError,match='No allowed scale fits'):
        render_site_body(data,resolve_company_sheet_set(pp,jp).sheets[0])


def test_thin_overview_reports_limits_and_selected_detail_preserves_full_features():
    data=request('extreme')
    original=copy.deepcopy(data)
    body,_,_,audit,_=render_site_body(data,sheet())
    assert audit['presentation']['presentation_limits']['status']=='requires_detail_review'
    assert audit['presentation']['presentation_limits']['geometry_span_mm'][1]<3
    assert 'Thin extent: selected detail advised' in body
    data['drawing'].update(presentation_role='detail',detail_feature_ids=['gear-a','charger-a'])
    body,drawings,_,detail,mapping=render_site_body(data,sheet())
    assert detail['coverage']['selected_feature_ids']==['gear-a','charger-a']
    assert 'building-a' in detail['coverage']['excluded_feature_ids']
    assert not detail['coverage']['all_supplied_geometry_shown']
    assert detail['coverage']['cropping'] is False
    assert 'Detail - selected features only' in body
    for feature in detail['mapping']:
        assert feature['input_points']==next(row['points'] for row in original['features'] if row['id']==feature['source_id'])
    assert all(item['source_ids'][0] in {'gear-a','charger-a','site-settings:synthetic-site'} for item in mapping['items'])
    assert data['features']==original['features']


def test_detail_scope_is_explicit_and_rejects_unknown_features():
    data=request()
    data['drawing']['detail_feature_ids']=['gear-a']
    with pytest.raises(ValueError,match='presentation_role=detail'):
        render_site_body(data,sheet())
    data['drawing'].update(presentation_role='detail',detail_feature_ids=['missing'])
    with pytest.raises(ValueError,match='unknown supplied'):
        render_site_body(data,sheet())
