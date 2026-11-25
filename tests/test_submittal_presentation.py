"""Opt-in utility-submittal presentation: same geometry/topology, plain sheets."""
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.drawings.company_sheets import resolve_company_sheet_set,render_company_sheet_svg
from oabm.drawings.site_plan import render_site_body,_feet_inches
from oabm.drawings.single_line import project_topology,plan_pages
from oabm.drawings.single_line.production import build_single_line_bodies
from oabm.drawings.single_line.drafting_facts import submittal_presentation,status_tag
from oabm.model import BuildingModel
from test_site_drafting import rich_site,saved

ROOT=Path(__file__).parents[1]


def _submittal(graphics,data):
    graphics=copy.deepcopy(graphics);graphics['detail_views']=[];graphics['label_feature_ids']=[]
    graphics['submittal']={'legend':[{'label':'Panel','sample':'symbol','symbol':'site-panel'},{'label':'Proposed feeder','sample':'heavy'}],
        'annotations':[{'text':'WORKSHOP','at':[20,20],'source_ids':['workshop'],'size':4,'rotate':90},
                       {'text':'PANEL-S','at':[10,25],'leader_to':[6,16],'source_ids':['main-panel']}],
        'dimension_units':'ft','scale_bar_ft':[0,10,20,40]}
    graphics['dimensions']=[{'id':'w','view_id':'overview','start_xy':[0,0],'end_xy':[44,0],'offset_mm':-6,'source_ids':['workshop']}]
    return graphics


def test_site_submittal_omits_internal_rows_and_prints_feet(tmp_path):
    data,graphics,paths=saved(tmp_path)
    sheet=resolve_company_sheet_set(paths[2],paths[3]).sheets[0]
    body=render_site_body(data,sheet,graphics=_submittal(graphics,data))[0]
    for phrase in ('SOURCE / PRESENTATION BASIS','coordinate-derived','No survey, engineering','generic mark','supplied status'):
        assert phrase not in body
    assert "144'-4\"" in body and 'LEGEND' in body and 'rotate(-90 ' in body and "40'" in body
    render_company_sheet_svg(sheet,body_svg=body)
    review=render_site_body(data,sheet,graphics=graphics)[0]
    assert 'No survey, engineering' in review


def test_feet_inches_rounds_to_nearest_inch():
    assert _feet_inches(6.096)=="20'-0\"" and _feet_inches(.9144)=="3'-0\""


def test_single_line_submittal_has_no_unknown_jargon(tmp_path):
    model=BuildingModel.from_json((ROOT/'examples/single_line/residential.model.json').read_bytes())
    profile=ROOT/'fixtures/company_sheets/v1/profile.json';job=ROOT/'fixtures/company_sheets/v1/job.json'
    template=resolve_company_sheet_set(profile,job).sheets[0]
    with submittal_presentation():
        assert status_tag('unknown')==''
        layout=plan_pages(project_topology(model),template.drawing_bounds_mm,first_sheet_id='E-1')
        sheets=tuple(replace(template,id=page['id']) for page in layout['pages'])
        bodies,_=build_single_line_bodies(model,sheets,layout=layout)
    text=''.join(bodies.values())
    for phrase in ('Routes: UNKNOWN','BUS A ?','(?)','no engineering approval','Transfer state is unspecified','C001 |'):
        assert phrase not in text
    assert status_tag('unknown')=='(?)'


def test_body_rotation_is_quarter_turn_only():
    sheet=resolve_company_sheet_set(ROOT/'fixtures/company_sheets/v1/profile.json',ROOT/'fixtures/company_sheets/v1/job.json').sheets[0]
    l,t,r,b=sheet.drawing_bounds_mm;x,y=l+40,t+40
    ok=f'<g xmlns="http://www.w3.org/2000/svg"><text x="{x}" y="{y}" font-size="3" transform="rotate(-90 {x} {y})">AB</text></g>'
    render_company_sheet_svg(sheet,body_svg=ok)
    with pytest.raises(ValueError,match='rotate'):
        render_company_sheet_svg(sheet,body_svg=ok.replace('rotate(-90','rotate(-45'))
