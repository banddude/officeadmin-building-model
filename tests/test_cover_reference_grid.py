"""Reusable reference layout, complete source coverage, and safe map placement."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.drawings.cover_sheet import paginate_cover, validate_cover
from oabm.drawings.company_sheets import resolve_company_sheet_set, render_company_sheet_svg
from test_cover_pagination import assert_complete

ROOT = Path(__file__).parents[1]


def request():
    config = json.loads((ROOT/'examples/cover-sheet/input.json').read_text())
    config.update(layout='reference-grid', pagination={'text_height_mm': 3.5},
                  code_basis=['Supplied jurisdiction basis pending verification.'],
                  existing_system=['Supplied existing equipment description.'],
                  linework_conventions=['Solid: proposed work.', 'Dashed: existing work.'],
                  key_abbreviations=['PNL: panel.'],
                  location_map={'caption': 'Synthetic vicinity diagram, not a survey.',
                                'width': 100, 'height': 60,
                                'segments': [[0,20,100,20], [30,0,30,60]]})
    return config


def sheet():
    return resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json',
                                     ROOT/'examples/cover-sheet/job.json').sheets[0]


def verify(plan, resolved):
    assert_complete(plan)
    for page in plan.pages:
        render_company_sheet_svg(replace(resolved,id=page.id,title=page.title), body_svg=page.body_svg,
                                 images=page.images)
        panels=page.layout['panels']
        for i,a in enumerate(panels):
            l,t,r,b=a['bounds_mm']
            sl,st,sr,sb=resolved.drawing_bounds_mm
            assert sl<=l<r<=sr and st<=t<b<=sb
            for other in panels[i+1:]:
                x,y,rr,bb=other['bounds_mm']
                assert r<=x or rr<=l or b<=y or bb<=t, (a['title'],other['title'])


def test_reference_grid_sections_align_and_use_shared_frame():
    config=request();resolved=sheet()
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    assert plan==paginate_cover(config,resolved.drawing_bounds_mm)
    assert len(plan.pages)==1
    verify(plan,resolved)
    p={r['title']:r['bounds_mm'] for r in plan.pages[0].layout['panels']}
    assert p['Project'][1]==p['Contacts'][1]==p['Sheet index'][1]
    assert p['Project'][2]<p['Contacts'][0]<p['Sheet index'][0]
    assert p['Scope'][1]==p['Code basis'][1]==p['Existing system'][1]
    assert p['Job notes'][1]==p['Location map'][1]
    assert p['Linework conventions'][1]>p['Job notes'][3]
    assert plan.pages[0].layout['design']=='drafting-cover-reference-grid-v2'


@pytest.mark.parametrize('field',['scope','contacts','notes','code_basis','existing_system','key_abbreviations','sheets'])
def test_grid_overflow_retains_every_fact_and_index(field):
    config=request();resolved=sheet()
    if field=='contacts':
        config[field]=[{'role':'Designer','name':f'SYNTHETIC PERSON {i}','email':f'{i}@example.invalid'} for i in range(50)]
    elif field=='sheets':
        config[field]+=[{'id':f'X{i}', 'title':f'Synthetic sheet {i}'} for i in range(80)]
    else:
        config[field]=[f'Supplied fact {i}. '+('Preserve caller wording. '*5) for i in range(100 if field=='notes' else 30)]
    original=json.dumps(config,sort_keys=True)
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    assert len(plan.pages)>1
    verify(plan,resolved)
    assert {p.layout['text_height_mm'] for p in plan.pages}=={3.5}
    assert json.dumps(config,sort_keys=True)==original


def test_small_sheet_uses_readable_flow_without_shrinking():
    config=request()
    plan=paginate_cover(config,(10,10,390,280))
    assert {p.layout['design'] for p in plan.pages}=={'drafting-cover-v3'}
    assert {p.layout['text_height_mm'] for p in plan.pages}=={3.5}
    assert_complete(plan)


def test_unknown_layout_is_refused():
    with pytest.raises(ValueError,match='layout'):
        validate_cover(dict(request(),layout='guess'))


@pytest.mark.parametrize('long_caption',[False,True])
def test_apple_map_stays_uncropped_qualified_and_clear_of_text(tmp_path,long_caption):
    from oabm.drawings.cover_maps import acquire_cover_map
    from test_cover_maps import ADDRESS, Client, NOW
    config=request();config.pop('location_map')
    snapshot=acquire_cover_map(ADDRESS,cache_directory=tmp_path/'cache',client=Client(),now=NOW)
    if long_caption:
        # Presentation stress only. No new acquisition or accepted address is asserted.
        snapshot=replace(snapshot,attribution='Retain attribution. '*1000)
    resolved=sheet()
    plan=paginate_cover(config,resolved.drawing_bounds_mm,map_snapshot=snapshot)
    verify(plan,resolved)
    images=[i for p in plan.pages for i in p.images]
    assert len(images)==1 and images[0].png_bytes==snapshot.png_bytes
    records=[r for p in plan.pages for r in p.layout['panels'] if r['title']=='Apple map image']
    assert records[0]['uncropped'] is True
    assert records[0]['marker_status']=='not_placed_unverified_geographic_image_bounds'
    assert any('VICINITY ONLY' in p.body_svg for p in plan.pages)


def test_complete_cli_example_uses_grid_and_final_index(tmp_path,sheet_pdf_runtime):
    import sys
    from pypdf import PdfReader
    from oabm.drawings.production_set import create_production_sheet_set
    sys.path.insert(0,str(ROOT/'examples'))
    from reference_production_set import prepare
    path=prepare(tmp_path/'inputs',adaptive_layout=True,reference_cover=True)
    original=(path.parent/'model.json').read_bytes()
    result=create_production_sheet_set(path,output_directory=tmp_path/'run')
    pdf=PdfReader(result/'company/delivery/sheet-set.pdf')
    assert len(pdf.pages)==4
    text=' '.join(pdf.pages[0].extract_text().split())
    for value in ('RESPONSIBLE PARTIES','SCOPE OF WORK','CODE BASIS','EXISTING SYSTEM',
                  'GENERAL NOTES','LINEWORK CONVENTIONS','VICINITY MAP','SYNTHETIC SITE'):
        assert value in text
    for identity in ('G0.0','E1.0','E3.1','ED1'):
        assert identity in text
    assert original==(path.parent/'model.json').read_bytes()


def test_sparse_cover_compacts_bands_and_reserves_map_space():
    config=request();resolved=sheet()
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    panels={p['title']:p for p in plan.pages[0].layout['panels']}
    top=panels['Project']['bounds_mm'][1]
    assert panels['Scope']['bounds_mm'][1]-top<100
    assert panels['Location map']['bounds_mm'][1]<resolved.drawing_bounds_mm[3]*.45
    verify(plan,resolved)


def test_explicit_linework_samples_and_contact_license_preserve_facts():
    import xml.etree.ElementTree as ET
    config=request();resolved=sheet()
    config['contacts'][0]['license']='SYNTHETIC LICENSE 123'
    config['linework_samples']=[{'label':'PROPOSED ROUTE','pattern':'solid','weight_mm':.7},
                                {'label':'OVERHEAD ROUTE','pattern':'dash'},
                                {'label':'EXISTING CONTEXT','pattern':'solid','tone':'gray'},
                                {'label':'PROVISIONAL','pattern':'dot'}]
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    verify(plan,resolved)
    root=ET.fromstring(plan.pages[0].body_svg)
    lines=[x for x in root.iter() if x.tag.endswith('}line')]
    assert any(x.get('stroke')=='#777777' for x in lines)
    assert any(x.get('stroke-width')=='0.7' for x in lines)
    assert 'SYNTHETIC LICENSE 123' in ''.join(root.itertext())
    assert plan.pages[0].layout['source_text']['linework_samples[0].label']=='PROPOSED ROUTE'


@pytest.mark.parametrize('sample',[{'label':'x','pattern':'unknown'},
    {'label':'x','pattern':'solid','tone':'url(bad)'},
    {'label':'x','pattern':'solid','weight_mm':False},
    {'label':'x','pattern':'solid','weight_mm':float('nan')}])
def test_invalid_line_sample_refuses(sample):
    with pytest.raises(ValueError):validate_cover(dict(request(),linework_samples=[sample]))


def test_balanced_note_columns_keep_complete_source_and_no_extra_number_bullets():
    import xml.etree.ElementTree as ET
    config=request();resolved=sheet()
    config['notes']=[f'{i}. '+('Caller note detail. '*12) for i in range(1,9)]
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    verify(plan,resolved)
    notes=next(p for p in plan.pages[0].layout['panels'] if p['title']=='Job notes')
    assert {r['column'] for r in notes['source_spans']}=={0,1}
    root=ET.fromstring(plan.pages[0].body_svg)
    bullets=[n for n in root.iter() if n.tag.endswith('}circle') and n.get('r')=='0.45']
    assert len(bullets)==len(config['scope'])


def test_single_long_note_flows_across_columns_and_pages_without_loss():
    config=request();resolved=sheet()
    config['notes']=['1. '+('Retain this supplied long note. '*600)]
    plan=paginate_cover(config,resolved.drawing_bounds_mm)
    assert len(plan.pages)>1
    verify(plan,resolved)
    notes=[p for page in plan.pages for p in page.layout['panels'] if p['title']=='Job notes']
    assert all({r['column'] for r in panel['source_spans']} <= {0,1} for panel in notes)


def test_line_samples_survive_narrow_sheet_flow():
    config=request()
    config['linework_samples']=[{'label':'Supplied route legend. '*8,'pattern':'dash-dot'}]
    plan=paginate_cover(config,(10,10,390,280))
    assert_complete(plan)
    assert any('line_sample' in row for page in plan.pages for panel in page.layout['panels'] for row in panel['source_spans'])
