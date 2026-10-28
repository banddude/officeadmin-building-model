"""Public synthetic source-only RCP, using the shared sheet/PDF boundary."""
import hashlib
import json
from pathlib import Path
import copy
import subprocess
import sys

import pytest
from pypdf import PdfReader
from oabm.drawings.lighting_rcp import render_lighting_rcp_body, create_lighting_rcp_sheet_set
from oabm.drawings.company_sheets import resolve_company_sheet_set, _dependencies


def setup(tmp_path):
    raw=b'Synthetic source evidence; no real plans or project.\n'
    (tmp_path/'source.txt').write_bytes(raw)
    src={'id':'source1','path':'source.txt','sha256':hashlib.sha256(raw).hexdigest(),'qualification':'synthetic design intent'}
    request={'version':'supplied-lighting-rcp-v1','id':'demo','units':'m',
        'frame':{'id':'frame1','origin':[0,0],'x_axis':'right','y_axis':'up','qualification':'local drawing XY only'},
        'sources':[src],
        'context':[{'id':'wall1','points':[[0,0],[8,0],[8,5],[0,5]],'closed':True,'source_ids':['source1']}],
        'fixtures':[{'id':'head1','kind':'spot','symbol':'track_head','zone_id':'zone1','xy':[2,3], 'aim_xy':[2.5,2], 'source_ids':['source1']},
                    {'id':'light2','kind':'downlight','symbol':'downlight','zone_id':'zone1','xy':[5,3], 'aim_xy':None, 'source_ids':['source1']}],
        'tracks':[{'id':'track1','points':[[1,3],[4,3]],'head_ids':['head1'],'source_ids':['source1']}],
        'zones':[{'id':'zone1','label':'DISPLAY','source_ids':['source1']}],
        'notes':[{'id':'note1','text':'SUPPLIED DESIGN INTENT; VERIFY ON SITE','source_ids':['source1']}],
        'declared_counts':{'context':1,'fixtures':2,'tracks':1},
        'drawing':{'sheet_id':'E2','scale':50,'origin_mm':[20,20],'view_bounds':[-1,-1,9,6]},
        'physical_elevation_m':None,'ceiling_height_m':None}
    profile={'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC'},'template':{}}
    job={'version':'company-sheet-job-v1','set_id':'SYNTHETIC','date':'2026-01-01','revision':'A','formats':['svg','pdf'],
         'sheets':[{'id':'E2','title':'LIGHTING PLAN','fields':{'scale':'1:50'}}]}
    for name,data in [('request',request),('profile',profile),('job',job)]:
        (tmp_path/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
    sheet=resolve_company_sheet_set(tmp_path/'profile.json',tmp_path/'job.json').sheets[0]
    return request,sheet,{'source1':raw}


def test_complete_shared_delivery_keeps_input_and_source_bytes_and_no_3d(tmp_path):
    try:_dependencies()
    except ImportError as exc:pytest.skip(str(exc))
    request,sheet,sources=setup(tmp_path)
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    assert 'head1' in body and mapping['quantity_eligible'] is False
    fixture=next(x for x in mapping['items'] if x['id']=='head1' and x['role']=='fixture')
    assert fixture['xy']==[2,3] and fixture['source_ids']==['source1']
    assert next(x for x in mapping['items'] if x['role']=='aim')['aim_xy']==[2.5,2]
    assert mapping['tracks']==[{'id':'track1','head_ids':['head1']}]
    assert mapping['physical_elevation_m'] is None and mapping['ceiling_height_m'] is None
    a=create_lighting_rcp_sheet_set(tmp_path/'request.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'a')
    b=create_lighting_rcp_sheet_set(tmp_path/'request.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'b')
    assert {p.relative_to(a):p.read_bytes() for p in a.rglob('*') if p.is_file()} == {p.relative_to(b):p.read_bytes() for p in b.rglob('*') if p.is_file()}
    assert (a/'inputs/lighting-request.json').read_bytes()==(tmp_path/'request.json').read_bytes()
    assert (a/'inputs/sources/source1.bin').read_bytes()==sources['source1']
    manifest=json.loads((a/'LIGHTING-MANIFEST.json').read_bytes())
    assert not manifest['quantity_eligible'] and not manifest['canonical_model_created'] and not manifest['engineering_approval']
    for row in manifest['files']:
        assert hashlib.sha256((a/row['path']).read_bytes()).hexdigest()==row['sha256']
    shared=a/'company/delivery';pdf=PdfReader(shared/'sheet-set.pdf')
    assert len(pdf.pages)==1
    text=pdf.pages[0].extract_text()
    for expected in ('EXAMPLE ELECTRIC','LIGHTING PLAN','head1','zone1: DISPLAY','CEILING HEIGHT UNKNOWN','DESIGN COUNTS'):
        assert expected in text
    with pytest.raises(FileExistsError):create_lighting_rcp_sheet_set(tmp_path/'request.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'a')


@pytest.mark.parametrize('mutation,match',[
    (lambda r:r['fixtures'][0].update(aim_xy=[2,3]),'aim point'),
    (lambda r:r['fixtures'][0].update(zone_id='missing'),'unknown zone'),
    (lambda r:r['tracks'][0].update(head_ids=['missing']),'unknown head_ids'),
    (lambda r:r['context'][0].update(source_ids=['missing']),'unknown source IDs'),
    (lambda r:r['declared_counts'].update(fixtures=3),'declared_counts.fixtures'),
    (lambda r:r.update(physical_elevation_m=0),'requires unknown'),
    (lambda r:r['fixtures'][0].update(xy=[99,99]),'outside explicit view_bounds'),
    (lambda r:r['drawing'].update(scale=100),'scale must match'),
    (lambda r:r['sources'][0].update(sha256='0'*64),'SHA-256 mismatch'),
    (lambda r:r['fixtures'][1].update(id='head1'),'duplicate layout ID'),
    (lambda r:r['context'][0].update(points=[[0,0],[1,0],[0,0]]),'repeated closing'),
])
def test_invalid_input_is_actionable_and_creates_no_output(tmp_path,mutation,match):
    request,sheet,sources=setup(tmp_path);mutation(request)
    with pytest.raises(ValueError,match=match):render_lighting_rcp_body(request,sheet,source_bytes=sources)
    assert not (tmp_path/'delivery').exists()


def test_two_tracks_cannot_claim_same_head_and_missing_source_rejected(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['tracks'].append({'id':'track2','points':[[1,1],[2,1]],'head_ids':['head1'],'source_ids':['source1']})
    request['declared_counts']['tracks']=2
    with pytest.raises(ValueError,match='more than one track'):render_lighting_rcp_body(request,sheet,source_bytes=sources)
    request['tracks'].pop();request['declared_counts']['tracks']=1
    with pytest.raises(ValueError,match='missing.*SHA-256'):render_lighting_rcp_body(request,sheet,source_bytes={})


def test_cli_fresh_process_and_missing_source(tmp_path):
    setup(tmp_path);(tmp_path/'source.txt').unlink()
    p=subprocess.run([sys.executable,'-m','oabm.drawings.lighting_rcp',str(tmp_path/'request.json'),str(tmp_path/'profile.json'),str(tmp_path/'job.json'),str(tmp_path/'run')],capture_output=True,text=True)
    assert p.returncode==1 and 'source source1: cannot read' in p.stderr
    assert not (tmp_path/'run').exists()


def test_optional_annotations_are_source_bound_and_retained(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['annotations']=[{'id':'room1','text':'SYNTHETIC ROOM','xy':[1,4],'source_ids':['source1']},
                            {'id':'dim1','text':'8 METRES SUPPLIED','xy':[2,0.5],'source_ids':['source1']}]
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    rows=[row for row in mapping['items'] if row['role']=='annotation']
    assert [r['id'] for r in rows]==['room1','dim1']
    assert rows[0]['xy']==[1,4] and rows[0]['source_ids']==['source1']
    assert 'SYNTHETIC ROOM' in body and '8 METRES SUPPLIED' in body
    request['annotations'][0]['source_ids']=['unknown']
    with pytest.raises(ValueError,match='unknown source IDs'):render_lighting_rcp_body(request,sheet,source_bytes=sources)
    request['annotations'][0]['source_ids']=['source1'];request['annotations'][0]['xy']=[-99,4]
    with pytest.raises(ValueError,match='outside explicit view_bounds'):render_lighting_rcp_body(request,sheet,source_bytes=sources)
    request['annotations'][0]['xy']=[1,4];request['annotations'][0]['id']='head1'
    with pytest.raises(ValueError,match='duplicate layout ID'):render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_layout_id_is_preserved_and_ink_overflow_refuses(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['fixtures'][0]['id']='Fixture: original / 1'
    request['tracks'][0]['head_ids']=['Fixture: original / 1']
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    assert 'Fixture: original / 1' in body
    assert mapping['tracks'][0]['head_ids']==['Fixture: original / 1']
    request['annotations']=[{'id':'long-label','text':'X'*1000,'xy':[1,1],'source_ids':['source1']}]
    with pytest.raises(ValueError,match='exceeds drawing_bounds_mm'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_track_members_are_explicit_heads_and_legend_is_source_bound(tmp_path):
    request,sheet,sources=setup(tmp_path)
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    assert 'spot / TRACK HEAD' in body and 'downlight / DOWNLIGHT' in body
    legends=[item for item in mapping['items'] if item['role']=='legend_symbol']
    assert len(legends)==2 and all(row['source_ids']==['source1'] for row in legends)
    request['tracks'][0]['head_ids']=['light2']
    with pytest.raises(ValueError,match='must reference track_head'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_caller_side_column_wraps_without_losing_text_and_refuses_collisions(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['drawing']['notes_bounds_mm']=[250,20,350,160]
    request['notes'][0]['text']='A long supplied note requiring an explicitly reserved side column and preserving every word.'
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    lines=[item['text'] for item in mapping['items'] if item['id']=='note1' and item['role']=='note']
    assert len(lines)>1 and ''.join(lines)=='4. '+request['notes'][0]['text']
    assert all(row['source_ids']==['source1'] for row in mapping['items'] if row['id']=='note1')
    request['drawing']['notes_bounds_mm']=[210,20,350,160]  # touches view + 3 mm clearance
    with pytest.raises(ValueError,match='disjoint.*3 mm'):render_lighting_rcp_body(request,sheet,source_bytes=sources)
    request['drawing']['notes_bounds_mm']=[250,20,270,25]
    with pytest.raises(ValueError,match='notes_bounds_mm.*text does not fit'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)
    request['drawing']['notes_bounds_mm']=[-1,20,350,160]
    with pytest.raises(ValueError,match='within shared drawing area'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_bounded_1700_supplied_context_paths_are_retained_in_order(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['context']=[{'id':f'path-{i:04d}','points':[[0,i/1000],[1,i/1000]],'closed':False,'source_ids':['source1']} for i in range(1700)]
    request['declared_counts']['context']=1700
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    rows=[item for item in mapping['items'] if item['role']=='context']
    assert len(rows)==1700 and rows[0]['id']=='path-0000' and rows[-1]['id']=='path-1699'
    assert rows[-1]['input_points']==[[0,1.699],[1,1.699]]


def test_side_column_rejects_actual_view_ink_overflow_not_just_anchor(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['drawing']['notes_bounds_mm']=[250,20,350,160]
    request['annotations']=[{'id':'at-edge','text':'THIS LABEL WOULD ENCROACH','xy':[8.9,2], 'source_ids':['source1']}]
    with pytest.raises(ValueError,match='view ink exceeds view rectangle'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_cli_rejects_invalid_source_id_before_opening_path(tmp_path):
    request,_,_=setup(tmp_path)
    request['sources'][0]['id']=['bad']
    (tmp_path/'request.json').write_text(json.dumps(request))
    p=subprocess.run([sys.executable,'-m','oabm.drawings.lighting_rcp',str(tmp_path/'request.json'),str(tmp_path/'profile.json'),str(tmp_path/'job.json'),str(tmp_path/'run')],capture_output=True,text=True)
    assert p.returncode==1 and 'source.id' in p.stderr and 'Traceback' not in p.stderr
    assert not (tmp_path/'run').exists()


def test_vector_legend_numbered_notes_and_source_bound_track_tag(tmp_path):
    import xml.etree.ElementTree as ET
    request,sheet,sources=setup(tmp_path)
    request['fixtures'][0]['kind']='museum_station'
    request['drawing']['notes_bounds_mm']=[250,20,350,200]
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    assert 'GENERAL NOTES' in body and 'LIGHTING FIXTURE LEGEND' in body and 'LIGHTING ZONES' in body
    assert 'museum station / TRACK HEAD' in body and 'museum_station /' not in body
    assert '1. SOURCE REVIEW ONLY' in body and '4. SUPPLIED DESIGN INTENT' in body
    root=ET.fromstring(body)
    legend=next(row for row in mapping['items'] if row['role']=='legend_symbol' and row['kind']=='museum_station')
    node=list(root)[legend['body_child_index']]
    assert any(child.tag=='circle' for child in node) and any(child.tag=='line' for child in node)
    assert legend['source_ids']==['source1'] and legend['design_count']==1
    tag=next(row for row in mapping['items'] if row['role']=='track_label')
    assert tag['id']=='track1' and tag['source_ids']==['source1'] and tag['endpoint_xy'] in ([1,3],[4,3])
    assert list(root)[tag['body_child_index']].text=='track1'


def test_track_tag_that_cannot_fit_is_not_omitted(tmp_path):
    request,sheet,sources=setup(tmp_path)
    request['tracks'][0]['id']='W'*128
    with pytest.raises(ValueError,match='no bounded track-tag position'):
        render_lighting_rcp_body(request,sheet,source_bytes=sources)


def test_label_backplates_are_measured_source_bound_and_above_linework(tmp_path):
    import xml.etree.ElementTree as ET
    request,sheet,sources=setup(tmp_path)
    request['annotations']=[{'id':'room-label','text':'ROOM OVER LINE','xy':[2,3], 'source_ids':['source1']}]
    body,mapping=render_lighting_rcp_body(request,sheet,source_bytes=sources)
    root=list(ET.fromstring(body));linework=max(row['body_child_index'] for row in mapping['items'] if row['role'] in ('context','track','fixture','aim'))
    for role in ('fixture_label','annotation','track_label'):
        label=next(row for row in mapping['items'] if row['role']==role)
        plate=next(row for row in mapping['items'] if row['id']==label['id'] and row['role']==role+'_plate')
        assert plate['source_ids']==label['source_ids']==['source1']
        assert linework < plate['body_child_index'] < label['body_child_index']
        node=root[plate['body_child_index']]
        assert node.tag=='rect' and node.attrib['fill']=='#ffffff'
        l,t,r,b=plate['paper_bounds_mm']
        assert float(node.attrib['x'])==pytest.approx(l) and float(node.attrib['width'])==pytest.approx(r-l)
        assert r>l and b>t
    assert request['fixtures'][0]['xy']==[2,3] and request['context'][0]['points'][0]==[0,0]
