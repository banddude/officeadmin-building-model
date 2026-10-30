"""Known synthetic drafting hierarchy and physical-layout proofs, without rasterization."""
from dataclasses import replace
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from oabm.drawings.cover_sheet import paginate_cover
from oabm.drawings.company_sheets import resolve_company_sheet_set, render_company_sheet_svg

ROOT=Path(__file__).parents[1]
SVG='{http://www.w3.org/2000/svg}'


def request():
    return json.loads((ROOT/'examples/cover-sheet/input.json').read_text())


def test_sparse_cover_has_hierarchy_numbered_open_regions_and_table():
    config=request()
    sheet=resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json',ROOT/'examples/cover-sheet/job.json').sheets[0]
    plan=paginate_cover(config,sheet.drawing_bounds_mm)
    assert len(plan.pages)==1
    body=ET.fromstring(plan.pages[0].body_svg)
    layout=plan.pages[0].layout
    assert layout['design']=='drafting-cover-v3'
    assert layout['project_heading_height_mm']>=2*layout['text_height_mm']
    assert layout['heading_height_mm']>layout['text_height_mm']
    assert len(list(body.iter(SVG+'circle')))>=5
    assert not list(body.iter(SVG+'rect'))
    texts=list(body.iter(SVG+'text'))
    assert any(n.text==config['project']['name'] and n.get('font-weight')=='600' for n in texts)
    assert {'PROJECT','SCOPE','CONTACTS','SHEET INDEX','SHEET','TITLE'}<={n.text for n in texts}
    panels={p['title']:p for p in layout['panels']}
    assert 0<panels['Scope']['bounds_mm'][1]-panels['Project']['bounds_mm'][1]<80
    assert panels['Project']['bounds_mm'][1]==panels['Contacts']['bounds_mm'][1]
    assert 0<panels['Sheet index']['bounds_mm'][1]-panels['Contacts']['bounds_mm'][1]<80
    assert 'Location map' not in panels
    assert layout['gutter_mm']>=25
    render_company_sheet_svg(sheet,body_svg=plan.pages[0].body_svg)


@pytest.mark.parametrize('paper',[(210,297),(420,297),(594,841),(914.4,609.6)])
def test_drafting_body_fits_resolved_paper_without_text_shrink(paper,tmp_path):
    config=request()
    job=json.loads((ROOT/'examples/cover-sheet/job.json').read_text())
    job['template']={'width_mm':paper[0],'height_mm':paper[1],
                     'rail_width_mm':max(87.136111111,87.136111111*paper[1]/609.6)}
    path=tmp_path/'job.json';path.write_text(json.dumps(job))
    sheet=resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json',path).sheets[0]
    plan=paginate_cover(config,sheet.drawing_bounds_mm)
    sizes={p.layout['text_height_mm'] for p in plan.pages}
    assert len(sizes)==1 and next(iter(sizes))>=2.5
    for p in plan.pages:
        render_company_sheet_svg(replace(sheet,id=p.id,title=p.title),body_svg=p.body_svg)
        for region in p.layout['panels']:
            l,t,r,b=region['bounds_mm'];sl,st,sr,sb=sheet.drawing_bounds_mm
            assert sl<=l<r<=sr and st<=t<b<=sb


def test_supplied_map_is_balanced_right_region_with_exact_geometry():
    config=request()
    config['location_map']={'caption':'Supplied location sketch','width':100,'height':60,
                            'segments':[[0,0,100,60],[0,60,100,0]]}
    bounds=(10,10,800,590)
    plan=paginate_cover(config,bounds)
    assert len(plan.pages)==1
    regions={p['title']:p for p in plan.pages[0].layout['panels']}
    assert regions['Location map']['bounds_mm'][0]>bounds[2]/2
    assert regions['Location map diagram']['bounds_mm'][0]==regions['Location map']['bounds_mm'][0]
    scale=regions['Location map diagram']['scale']
    assert scale>0
    # Only the explicitly supplied two oblique map segments appear.
    root=ET.fromstring(plan.pages[0].body_svg)
    oblique=[n for n in root.iter(SVG+'line') if n.get('x1')!=n.get('x2') and n.get('y1')!=n.get('y2')]
    assert len(oblique)==len(config['location_map']['segments'])


def test_configured_typography_survives_long_content_continuations():
    config=request();config['scope']=[f'Supplied scope item {i}. '+('Full supplied content. '*8) for i in range(120)]
    config['pagination']={'text_height_mm':4.5,'continuation_id_prefix':'X'}
    plan=paginate_cover(config,(10,10,800,590))
    assert len(plan.pages)>1
    assert {p.layout['text_height_mm'] for p in plan.pages}=={4.5}
    actual={}
    for p in plan.pages:
        for region in p.layout['panels']:
            for span in region['source_spans']:
                assert len(actual.get(span['source'],''))==span['offset']
                actual[span['source']]=actual.get(span['source'],'')+span['text']
    assert actual==plan.pages[0].layout['source_text']


def test_long_cover_uses_both_columns_on_continuations():
    config=request();config['contacts']=[{'role':'Designer','name':f'CONTACT {i}','email':f'p{i}@example.invalid'} for i in range(160)]
    bounds=(10,10,800,590)
    plan=paginate_cover(config,bounds)
    assert len(plan.pages)>1
    regions=plan.pages[1].layout['panels']
    assert any(r['bounds_mm'][0]<400 for r in regions)
    assert any(r['bounds_mm'][0]>400 for r in regions)
    assert {p.layout['text_height_mm'] for p in plan.pages}=={plan.pages[0].layout['text_height_mm']}


def test_caller_project_labels_and_values_are_preserved():
    config=request();config['project']['AHJ_REF']='SUPPLIED LABEL VALUE'
    plan=paginate_cover(config,(10,10,800,590))
    texts=list(ET.fromstring(plan.pages[0].body_svg).iter(SVG+'text'))
    assert any(n.text=='AHJ_REF: ' and n.get('font-weight')=='600' for n in texts)
    assert any(n.text=='SUPPLIED LABEL VALUE' for n in texts)
    assert plan.pages[0].layout['source_text']['project.AHJ_REF']=='AHJ_REF: SUPPLIED LABEL VALUE'


def rich_map():
    return {'caption':'Explicitly supplied synthetic location sketch','width':100,'height':60,
            'segments':[[0,14,100,14],[0,18,100,18],[0,44,100,44],[0,48,100,48],
                        [18,0,18,60],[22,0,22,60],[72,0,72,60],[76,0,76,60]],
            'labels':[{'x':45,'y':14,'text':'SYNTHETIC ACCESS WAY A'},
                      {'x':45,'y':44,'text':'SYNTHETIC ACCESS WAY B'},
                      {'x':20,'y':30,'text':'SYNTHETIC ENTRY'}],
            'site_marker':{'x':60,'y':32,'label':'SYNTHETIC PROJECT SITE'}}


def test_index_row_rules_and_hanging_scope_bullets():
    config=request();config['scope']=['SUPPLIED_BULLET_START '+('caller text '*65)]
    plan=paginate_cover(config,(10,10,800,590))
    root=ET.fromstring(plan.pages[0].body_svg)
    rules=[n for n in root.iter(SVG+'line') if n.get('stroke-width')=='0.13']
    assert len(rules)==len(config['sheets'])
    scope=next(r for r in plan.pages[0].layout['panels'] if r['title']=='Scope')
    texts=[n for n in root.iter(SVG+'text') if n.text in scope['lines']]
    assert len(scope['lines'])>1 and len(texts)==len(scope['lines'])
    xs={float(n.get('x')) for n in texts}
    assert len(xs)==1 and next(iter(xs))>scope['bounds_mm'][0]+4
    bullets=[n for n in root.iter(SVG+'circle') if n.get('r')=='0.45']
    assert any(float(n.get('cx'))+.45<next(iter(xs)) for n in bullets)


def test_supplied_rich_map_labels_marker_and_full_coverage():
    config=request();config['location_map']=rich_map()
    sheet=resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json',ROOT/'examples/cover-sheet/job.json').sheets[0]
    plan=paginate_cover(config,sheet.drawing_bounds_mm)
    source={}
    for p in plan.pages:
        render_company_sheet_svg(replace(sheet,id=p.id,title=p.title),body_svg=p.body_svg)
        for panel in p.layout['panels']:
            for span in panel['source_spans']:
                assert len(source.get(span['source'],''))==span['offset']
                source[span['source']]=source.get(span['source'],'')+span['text']
    assert source==plan.pages[0].layout['source_text']
    diagram=next(r for p in plan.pages for r in p.layout['panels'] if r['title']=='Location map diagram')
    assert diagram['supplied_segment_count']==len(config['location_map']['segments'])
    placed={r['key']:r for r in diagram['map_labels']}
    assert placed['SITE']['source_point']==[60,32]
    assert placed['SITE']['source_label']=='SYNTHETIC PROJECT SITE'
    assert {r['status'] for r in placed.values()}=={'placed'}
    for i,row in enumerate(config['location_map']['labels']):
        assert source[f'location_map.labels[{i}].text']==f"L{i+1}  {row['text']}"
        assert placed[f'L{i+1}']['source_point']==[row['x'],row['y']]
    assert source['location_map.site_marker.label']=='SITE  SYNTHETIC PROJECT SITE'


def test_long_map_label_keeps_full_legend_and_bounded_reference():
    config=request();config['location_map']=rich_map()
    supplied='Long caller label '+('retain exact full supplied text '*100)
    config['location_map']['labels'][0]['text']=supplied
    plan=paginate_cover(config,(10,10,800,590))
    diagram=next(r for p in plan.pages for r in p.layout['panels'] if r['title']=='Location map diagram')
    label=next(r for r in diagram['map_labels'] if r['key']=='L1')
    assert label['status']=='legend_reference' and label['source_label']==supplied
    spans=[span for p in plan.pages for r in p.layout['panels'] for span in r['source_spans']
           if span['source']=='location_map.labels[0].text']
    assert ''.join(span['text'] for span in spans)=='L1  '+supplied


@pytest.mark.parametrize('invalid',['missing_marker_label','outside_label','outside_site'])
def test_invalid_map_facts_are_refused(invalid):
    from oabm.drawings.cover_sheet import validate_cover
    config=request();config['location_map']=rich_map()
    if invalid=='missing_marker_label':del config['location_map']['site_marker']['label']
    elif invalid=='outside_label':config['location_map']['labels'][0]['x']=101
    else:config['location_map']['site_marker']['y']=-1
    with pytest.raises(ValueError):validate_cover(config)


def test_short_contact_cards_and_paragraphs_stay_in_one_column():
    config=request()
    config['contacts']=[{'role':'Designer','name':f'CONTACT {i}','email':f'p{i}@example.invalid'} for i in range(100)]
    config['scope']=[f'SUPPLIED PARAGRAPH {i}. '+('Explicit supplied description. '*5) for i in range(100)]
    config['notes']=[f'Supplied note {i}.' for i in range(100)]
    plan=paginate_cover(config,(10,10,800,590))
    locations={}
    for p in plan.pages:
        for region in p.layout['panels']:
            for span in region['source_spans']:
                if span['source'].startswith(('contacts[','scope[')):
                    locations.setdefault(span['source'],set()).add((p.id,region['bounds_mm'][0]))
    assert len(locations)==200
    assert all(len(value)==1 for value in locations.values())


def test_optional_reference_sections_preserve_supplied_facts_and_paginate():
    config=request()
    sections={'code_basis':'Caller code reference', 'existing_system':'Caller existing system fact',
              'linework_conventions':'Dashed: caller convention', 'key_abbreviations':'EX: caller abbreviation'}
    for key,value in sections.items():config[key]=[value+' '+str(i) for i in range(80)]
    plan=paginate_cover(config,(10,10,400,280))
    assert len(plan.pages)>1
    source={}
    for page in plan.pages:
        source.update(page.layout.get('source_text',{}))
    # Coverage records retain every source row across the page sequence.
    encoded=json.dumps([p.layout for p in plan.pages])
    for key in sections:
        assert f'{key}[79]' in encoded
    with pytest.raises(ValueError):
        paginate_cover(dict(config,code_basis='unsourced scalar'),(10,10,400,280))
