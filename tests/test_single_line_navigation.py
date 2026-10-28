"""Actual sheet/terminal destinations, resolved after complete pagination."""
from pathlib import Path
from types import SimpleNamespace
import json,runpy
import xml.etree.ElementTree as ET
import pytest
from oabm.drawings.single_line import project_topology,plan_pages,create_single_line_set
from oabm.drawings.single_line.production import build_single_line_bodies
from oabm.drawings.company_sheets import resolve_company_sheet_set

FACTORY=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
BOUNDS=(7.055555556,7.055555556,820.208333333,602.544444444)

def build(model):
    plan=plan_pages(project_topology(model),BOUNDS,first_sheet_id='SL-020')
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in plan['pages']],layout=plan)
    return bodies,doc

def visible(body):return ' '.join(' '.join(ET.fromstring(body).itertext()).split())

def shared_packet(tmp_path,model):
    (tmp_path/'model.json').write_text(model.to_json())
    (tmp_path/'profile.json').write_text(json.dumps({'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC','scale':'NOT TO SCALE'},'template':{}}))
    job={'version':'company-sheet-job-v1','set_id':'navigation-proof','date':'2026-10-27','revision':'A','formats':['svg','pdf'],'sheets':[{'id':'SL-020','title':'SINGLE LINE'}]}
    (tmp_path/'job.json').write_text(json.dumps(job))
    template=resolve_company_sheet_set(tmp_path/'profile.json',tmp_path/'job.json').sheets[0]
    plan=plan_pages(project_topology(model),template.drawing_bounds_mm,first_sheet_id=template.id)
    job['sheets']=[{'id':p['id'],'title':'SINGLE LINE'} for p in plan['pages']]
    (tmp_path/'job.json').write_text(json.dumps(job))
    delivery=create_single_line_set(tmp_path/'model.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'packet')
    return delivery,json.loads((delivery/'topology-layout.json').read_text())

@pytest.mark.parametrize('shape,tags',[('two-transformers',{'C004','C032','C005','C013','C014'}),('shallow',{'C004'}),('deep',{'C012'})])
def test_actual_spilled_register_destinations_in_svg_and_pdf(tmp_path,shape,tags):
    from pypdf import PdfReader
    model=FACTORY['make_variant_model'](shape)
    bodies,doc=build(model)
    for row in doc['layout']['connections']:
        if row['tag'] not in tags:continue
        assert row['register_sheet']=='SL-023'
        assert row['source_sheet']!=row['register_sheet']
        assert row['tag']+' REGISTER '+row['register_sheet'] in visible(bodies[row['source_sheet']])
    delivery,actual=shared_packet(tmp_path,model)
    pdf=PdfReader(delivery/'company-sheets/delivery/sheet-set.pdf')
    index={page['id']:i for i,page in enumerate(actual['layout']['pages'])}
    for row in actual['layout']['connections']:
        if row['tag'] in tags:
            text=' '.join(pdf.pages[index[row['source_sheet']]].extract_text().split())
            assert row['tag']+' REGISTER '+row['register_sheet'] in text
    for page in actual['layout']['pages']:
        for grid in page.get('feeder_grids',[]):
            text=visible(grid['body_svg'])
            assert 'Schedule page' not in text
            assert 'REGISTER '+page['id'] in text
            if grid['next_register_sheet']:assert 'NEXT '+grid['next_register_sheet'] in text
            if grid['previous_register_sheet']:assert 'PREV '+grid['previous_register_sheet'] in text


def test_two_transfer_normal_terminal_has_forward_and_return_svg_pdf_links(tmp_path):
    from pypdf import PdfReader
    model=FACTORY['make_variant_model']('multi-transfer',transfer_count=2)
    bodies,doc=build(model)
    edge=next(e for e in doc['topology']['connections'] if e['load_id']=='ATS' and e['load_role']=='normal')
    row=next(r for r in doc['layout']['connections'] if r['connection_id']==edge['id'])
    home=doc['layout']['equipment_home_sheets']['ATS'];terminal=row['load_sheet']
    assert home=='SL-020' and terminal=='SL-023'
    forward='ATS NORMAL IN '+row['tag']+' -> '+terminal
    backward='ATS NORMAL IN '+row['tag']+' -> HOME '+home
    assert forward in visible(bodies[home]) and backward in visible(bodies[terminal])
    for page in doc['layout']['pages']:
        assert 'CONTINUED '+page['id'] not in visible(bodies[page['id']])
        assert all(link['from_sheet']!=link['to_sheet'] for link in page['terminal_references'])
    delivery,actual=shared_packet(tmp_path,model)
    pdf=PdfReader(delivery/'company-sheets/delivery/sheet-set.pdf')
    index={p['id']:i for i,p in enumerate(actual['layout']['pages'])}
    assert forward in ' '.join(pdf.pages[index[home]].extract_text().split())
    assert backward in ' '.join(pdf.pages[index[terminal]].extract_text().split())
