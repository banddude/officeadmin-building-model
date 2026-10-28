"""Rich synthetic drafting proof. No historical/customer geometry or facts."""
import copy
import json
from pathlib import Path

import pytest

from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.site_plan import render_site_body,create_site_plan


def rich_site():
    rows=[]
    def feature(key,kind,points,label,closed=False,status=None):
        row={'id':key,'kind':kind,'points':points,'label':label,'closed':closed,'frame_id':'synthetic-campus',
             'evidence':{'source_id':'synthetic-campus-coordinate-ledger','reference':'supplied synthetic '+key,
                         'qualification':'Synthetic coordinate example only; not survey or construction information.','confidence':1.0}}
        if status:row['route_status']=status
        rows.append(row)
    feature('workshop','building',[[0,0],[44,0],[44,28],[34,28],[34,45],[0,45]],'EXISTING WORKSHOP',True)
    feature('annex','building',[[72,34],[94,34],[94,60],[72,60]],'PROPOSED SERVICE ANNEX',True)
    feature('curb','curb',[[-8,-10],[112,-10],[116,-6],[116,66],[112,70],[-8,70],[-12,66],[-12,-6]],'CURB EDGE',True)
    feature('aisle-south','parking',[[-5,-9],[109,-9]],'SOUTH PARKING EDGE')
    feature('aisle-east','parking',[[112,-5],[112,62]],'EAST PARKING EDGE')
    for i in range(32):
        x=-5+i*3.5
        feature(f'stall-s-{i}','parking',[[x,-8],[x,-1]],'PARKING STRIPE')
    for i in range(21):
        y=round(-4+i*3.1,3)
        feature(f'stall-e-{i}','parking',[[98,y],[111,y]],'PARKING STRIPE')
    for i in range(13):
        y=round(2+i*3.6,3)
        feature(f'stall-w-{i}','parking',[[-10,y],[-1,y]],'PARKING STRIPE')
    feature('island-north','landscape',[[98,64],[111,64],[113,66],[111,68],[98,68]],'LANDSCAPE',True)
    feature('island-west','landscape',[[-10,54],[-2,54],[-2,66],[-7,66],[-10,62]],'LANDSCAPE',True)
    for key,point,label in [('main-panel',[6,16],'PANEL-S'),('jb-1',[63,34],'JB-1'),('jb-2',[72,34],'JB-2'),
                            ('annex-panel',[72,39],'PANEL-A'),('control',[88,44],'CONTROL')]:
        feature(key,'equipment',[point],label)
    feature('existing-service','route',[[-6,4],[6,4],[6,16]],'EXISTING ROUTE',status='supplied')
    feature('proposed-feeder','route',[[6,16],[44,16],[44,29],[63,29],[63,34]],'PROPOSED FEEDER',status='supplied')
    feature('span','route',[[63,34],[72,34]],'SUPPLIED SPAN',status='supplied')
    feature('annex-drop','route',[[72,34],[72,39]],'PANEL APPROACH',status='supplied')
    feature('future-control','route',[[72,39],[80,39],[80,44],[88,44]],'FUTURE CONTROL ROUTE',status='future')
    feature('future-intent','route',[],'FUTURE CONNECTION INTENT',status='future')
    request={'version':'supplied-site-plan-v1','id':'synthetic-campus','units':'m',
        'frame':{'id':'synthetic-campus','axes':'local-xy,+x-right,+y-up'},'features':rows,
        'drawing':{'scale':None,'scale_mode':'fit','allowed_scales':[200,250,300,500],
                   'placement_mm':[15,15],'padding_mm':12,'legend_width_mm':105,'north_degrees':0}}
    statuses={row['id']:'existing' for row in rows}
    for key in ('annex','jb-1','jb-2','annex-panel','control','proposed-feeder','span','annex-drop'):statuses[key]='proposed'
    for key in ('future-control','future-intent'):statuses[key]='future'
    graphics={'version':'site-drafting-v1','overview_bounds_mm':[15,15,570,395],'notes_bounds_mm':[585,15,805,586],
        'label_feature_ids':['workshop','annex','main-panel','jb-1','jb-2','annex-panel','control','island-north'],
        'work_status':statuses,'route_patterns':{'span':'dash'},
        'symbols':{'main-panel':'site-panel','jb-1':'site-junction','jb-2':'site-junction','annex-panel':'site-panel','control':'site-control'},
        'detail_views':[{'id':'connection','number':'2','title':'CONNECTION AREA ENLARGEMENT','bounds_mm':[15,411,570,586],
                         'feature_ids':['jb-1','jb-2','annex-panel','span','annex-drop'],'scale':50,'focus':True}],
        'notes':[{'text':'The heavy solid line follows the supplied proposed feeder path from PANEL-S to JB-1.','source_ids':['proposed-feeder','main-panel','jb-1']},
                 {'text':'The heavy dashed segment between JB-1 and JB-2 is the supplied overhead plan projection. No elevation or support design is included.','source_ids':['span','jb-1','jb-2']},
                 {'text':'The thin gray dashed path is existing. The remaining dashed control route is a supplied future intent.','source_ids':['existing-service','future-control']},
                 {'text':'View 2 enlarges the supplied connection region. Marks are generic; equipment footprints and physical clearances are not represented.','source_ids':['jb-1','jb-2','annex-panel']}],
        'dimensions':[{'id':'connection-distance','view_id':'connection','start_feature_id':'jb-1','start_point_index':0,
                       'end_feature_id':'jb-2','end_point_index':0,'offset_mm':-7}]}
    profile={'version':'company-sheet-profile-v1','fields':{'company':'Synthetic Drafting Company','license':'SYNTHETIC'},
        'template':{'width_mm':914.4,'height_mm':609.6,'margin_mm':7.055555556,'rail_width_mm':87.136111111,'rail_inset_mm':6.35},'logos':{}}
    job={'version':'company-sheet-job-v1','set_id':'synthetic-drafted-site','fields':{'project':'Synthetic site study','scale':'AS NOTED','drawn_by':'EXAMPLE','issue':'SYNTHETIC DRAFT'},
         'date':'2026-10-27','revision':'A','formats':['svg','pdf'],'sheets':[{'id':'E2','title':'SITE AND CONNECTION PLAN'}]}
    return request,graphics,profile,job


def saved(tmp_path):
    data,graphics,profile,job=rich_site()
    paths=[]
    for name,value in [('site',data),('graphics',graphics),('profile',profile),('job',job)]:
        p=tmp_path/(name+'.json');p.write_text(json.dumps(value,indent=2)+'\n');paths.append(p)
    return data,graphics,paths


def test_rich_drafting_retains_coordinates_and_graphic_meaning(tmp_path):
    data,graphics,paths=saved(tmp_path)
    original=copy.deepcopy(data)
    sheet=resolve_company_sheet_set(paths[2],paths[3]).sheets[0]
    body,drawings,composed,audit,mapping=render_site_body(data,sheet,graphics=graphics)
    assert data==original
    assert 'SITE PLAN' in body and 'SITE PLAN NOTES' in body and 'CONNECTION AREA ENLARGEMENT' in body
    assert '9.00 m PLAN' in body
    assert audit['presentation']['selected_scale'] in data['drawing']['allowed_scales']
    assert audit['drafting']['source_geometry_modified'] is False
    assert audit['drafting']['historical_reference_geometry_used'] is False
    assert audit['drafting']['views']['connection']['scale']==50
    assert len(drawings.views)==2
    overview=next(v for v in drawings.views if v.id=='overview')
    primitives={p.id:p for p in overview.primitives}
    assert primitives['geometry:proposed-feeder'].style.weight=='heavy'
    assert primitives['geometry:proposed-feeder'].style.pattern=='solid'
    assert primitives['geometry:existing-service'].style.stroke=='space'
    assert primitives['geometry:existing-service'].style.pattern=='dash'
    assert primitives['geometry:span'].style.pattern=='dash'
    assert primitives['geometry:jb-1'].symbol=='site-junction'
    for row in original['features']:
        if row['points']:
            assert [[p.x,p.y] for p in primitives['geometry:'+row['id']].points]==row['points']
    assert 'stall-s-0' in audit['drafting']['explicitly_unlabelled_feature_ids']
    assert any(item['kind']=='detail-focus' and set(item['source_ids'])==set(graphics['detail_views'][0]['feature_ids']) for item in mapping['items'])
    assert any(item['kind']=='dimension' and set(item['source_ids'])=={'jb-1','jb-2'} for item in mapping['items'])
    assert 'FUTURE CONNECTION INTENT [future] - no geometry' in body
    assert audit['presentation']['sheet_scale_label']=='AS NOTED'
    assert audit['drafting']['coverage_by_feature']['future-intent']['state']=='intent_only_no_geometry'
    import xml.etree.ElementTree as ET
    root=ET.fromstring(body)
    for key,path in mapping['renderer_svg_id_to_body_child_path'].items():
        node=root
        for index in path:node=node[index]
        assert node.tag
    for item in mapping['items']:
        if 'svg_label_id' in item:assert item['svg_label_id'] in mapping['renderer_svg_id_to_body_child_path']
    assert 'data-titleblock-row' not in body
    assert render_site_body(data,sheet,graphics=graphics)[0]==body
    json.dumps(audit,allow_nan=False) # no circular or nonfinite diagnostics


def test_drafted_pdf_uses_actual_shared_pipeline(tmp_path):
    data,graphics,paths=saved(tmp_path)
    try:
        from oabm.drawings.sheet_pdf import _dependencies
        _,_,pypdf=_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    out=create_site_plan(paths[0],paths[2],paths[3],graphics_path=paths[1],output_directory=tmp_path/'drafted')
    manifest=json.loads((out/'MANIFEST.json').read_text())
    assert (out/'site-graphics.json').read_bytes()==paths[1].read_bytes()
    pdf=pypdf.PdfReader(out/manifest['shared_delivery']/'sheet-set.pdf')
    text=pdf.pages[0].extract_text()
    assert 'CONNECTION AREA ENLARGEMENT' in text and '9.00 m PLAN' in text
    assert text.count('Synthetic Drafting Company')==1
    assert 'AS NOTED' in text
    assert json.loads((out/manifest['shared_delivery']/'inputs/job.json').read_text())['sheets'][0]['fields']['scale']=='AS NOTED'
    assert not list(pdf.pages[0].images)


@pytest.mark.parametrize('mutation,match',[
    (lambda g:g['detail_views'][0].update(feature_ids=['missing']),'feature'),
    (lambda g:g['work_status'].update(span='invented'),'work_status'),
    (lambda g:g['route_patterns'].update(**{'future-control':'solid'}),'future'),
    (lambda g:g['notes'][0].update(source_ids=['missing']),'source'),
    (lambda g:g['dimensions'][0].update(end_point_index=9),'endpoint'),
    (lambda g:g.update(notes_bounds_mm=[500,15,805,586]),'overlap'),
])
def test_invalid_drafting_supplement_is_refused(tmp_path,mutation,match):
    data,graphics,paths=saved(tmp_path)
    mutation(graphics)
    with pytest.raises(ValueError,match=match):
        render_site_body(data,resolve_company_sheet_set(paths[2],paths[3]).sheets[0],graphics=graphics)


def test_focus_annotation_never_crosses_jb2_label_or_masks_future_route(tmp_path):
    """Regression from independent 504c1f8 PDF inspection, in physical mm."""
    import xml.etree.ElementTree as ET
    from oabm.drawings.label_layout import _segment_hits_box,_point_segment_distance
    from oabm.drawings.model import Point2
    from oabm.drawings.company_sheets import measure_sheet_text
    data,graphics,paths=saved(tmp_path)
    original=copy.deepcopy(data)
    body,drawings,composed,audit,mapping=render_site_body(data,resolve_company_sheet_set(paths[2],paths[3]).sheets[0],graphics=graphics)
    root=ET.fromstring(body)
    def node(item):
        out=root
        for index in mapping['renderer_svg_id_to_body_child_path'][item['svg_id']]:out=out[index]
        return out
    label=next(row for row in mapping['items'] if row['view_id']=='overview' and row['source_item_id']=='label:jb-2')
    text=node(label)
    x,y=float(text.get('x')),float(text.get('y'))
    # The former x369/y182..185 dash crosses the actual placed JB-2 text.
    label_box=(x-.3,y-2.5,x+measure_sheet_text(text.text,2.5,font_family='DejaVu Sans')+.3,y+.75)
    assert x==pytest.approx(368.25) and y==pytest.approx(183.375)
    assert _segment_hits_box(Point2(369,182),Point2(369,185),label_box)
    focus=next(row for row in mapping['items'] if row['kind']=='detail-focus')
    for dash in node(focus):
        a=Point2(float(dash.get('x1')),float(dash.get('y1')))
        b=Point2(float(dash.get('x2')),float(dash.get('y2')))
        assert not _segment_hits_box(a,b,label_box)
    bubble=next(row for row in mapping['items'] if row['kind']=='detail-callout')
    circle=node(bubble)
    assert circle.get('fill')=='none'
    center=Point2(float(circle.get('cx')),float(circle.get('cy')))
    radius=float(circle.get('r'))
    overview=next(v for v in composed.to_dict()['views'] if v['view_id']=='overview')
    future=next(p for p in overview['primitives'] if p['id']=='geometry:future-control')
    points=[Point2(p['x'],p['y']) for p in future['points_mm']]
    # Previous white-filled bubble (374,168,r3.5) hid this supplied route y167.
    assert points[0].y==pytest.approx(167)
    assert _point_segment_distance(Point2(374,168),points[0],points[1])<3.5
    assert min(_point_segment_distance(center,a,b) for a,b in zip(points,points[1:]))>radius+1
    diagnostics=audit['drafting']['focus_annotations']['connection']
    assert diagnostics['omitted_collision_segments']>0
    assert diagnostics['retained_segments']>0
    assert diagnostics['state']=='placed'
    assert diagnostics['bubble_fill']=='none'
    assert data==original
    # Source vertices and scales remain exactly as independently verified.
    assert audit['presentation']['view_scales']=={'overview':250.,'connection':50.}
    actual=next(v for v in drawings.views if v.id=='overview')
    assert [[p.x,p.y] for p in next(p for p in actual.primitives if p.id=='geometry:future-control').points]==original['features'][-2]['points']


def test_focus_annotation_reports_unplaced_when_all_candidates_are_blocked():
    import xml.etree.ElementTree as ET
    from oabm.drawings.site_plan import _place_detail_focus
    ns='{http://www.w3.org/2000/svg}'
    group=ET.Element(ns+'g')
    ET.SubElement(group,ns+'line',{'x1':'10','y1':'10','x2':'20','y2':'10'})
    center,retained,diagnostics=_place_detail_focus(group,(10,10,20,20),(0,0,30,30),[],[(-100,-100,100,100)])
    assert center is None and not retained and not list(group)
    assert diagnostics['state']=='partially_unplaced'
    assert diagnostics['source_geometry_modified'] is False
