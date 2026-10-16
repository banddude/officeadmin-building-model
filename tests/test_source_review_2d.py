"""2D review cases use independently authored synthetic PDF/journal fixtures."""
import hashlib
import json
import math
from dataclasses import replace

import pytest

from oabm.model import ContractError
from oabm.proposals.contracts import digest
from oabm.proposals.takeoff import render_source_image
from oabm.importers.pdf_electrical.symbol_stage import build_symbol_review
from oabm.exports.source_review_2d import (SourceReviewFrame,SourceReviewAssertion,
    PendingSourceSplit,export_source_review_2d)
from test_symbol_proposals import fixture,run


def case(tmp_path,output=None):
    tmp_path.mkdir(parents=True,exist_ok=True)
    request,images,documents,default=fixture()
    result=run(tmp_path,output=output or default)
    source=request.images[0].source;document=documents[source.document_sha256]
    background,pixels=render_source_image(document,source_id=source.source_id,page=source.page,
        scale=1,crop_px=(0,0,200,100),image_id='source-page',role='floor_context')
    cite={'source_id':source.source_id,'page':source.page,'locator':'Synthetic scale and plan.'}
    frame=SourceReviewFrame(frame_id='sheet-local',background=background,origin_pt=(10,20),
        metres_per_point=.5,rotation_radians=math.pi/2,label='Synthetic floor',
        relative_level_label='Local reference',rationale='Declared diagram frame only.',source_citations=(cite,))
    args=dict(project_id='synthetic-project',title='Synthetic source review',runs=(result,),
        documents=documents,images=images,journal_paths={request.fingerprint:tmp_path/'attempts.jsonl'},
        frames=(frame,),background_images={background.id:pixels})
    return args,frame,cite


def export(args,**changes):
    return export_source_review_2d(**{**args,**changes})


def assertion(row,cite,**changes):
    values=dict(proposal_id=row['proposal_id'],proposal_fingerprint=digest(row),
        reviewer_id='synthetic-agent',derivation='inferred',decision='corrected',category='switch',
        type_proposal='synthetic-switch',work_status='existing',bid_scope='existing_baseline',
        rationale='Independent source glyph review.',source_citations=(cite,))
    return SourceReviewAssertion(**{**values,**changes})


def test_xy_convention_is_explicit_without_invented_elevation(tmp_path):
    args,frame,_=case(tmp_path);before=args['runs'][0].to_json();result=export(args)
    data=json.loads(result.manifest_json);row,=data['observations'];page,=data['pages']
    assert row['local_anchor_xy_m']==pytest.approx((-23,30))
    for point,expected in zip(row['local_bounds_polygon_xy_m'],[(-19,25),(-19,35),(-27,35),(-27,25)]):
        assert point==pytest.approx(expected)
    assert page['origin_display_bottom_pt']==[10,20]
    assert page['relative_level_zero']==0 and page['physical_elevation_m'] is None
    assert page['registration_status']=='page_local_convention_only'
    assert page['inter_page_registration'] is None
    assert row['physical_elevation_m'] is row['mounting_height_m'] is row['canonical_level_id'] is None
    assert not row['canonical_accepted'] and not row['quantity_eligible']
    assert data['canonical_instances_created']==0 and row['canonical_action']=='defer'
    assert args['runs'][0].to_json()==before
    assert result==export(args)
    assert result.fingerprint==hashlib.sha256(result.manifest_json.encode()).hexdigest()


def test_review_corrections_preserve_original_confidence_provenance_and_geometry(tmp_path):
    args,_,cite=case(tmp_path);original=json.loads(export(args).manifest_json)['observations'][0]['source_observation']
    a=assertion(original,cite)
    data=json.loads(export(args,assertions=(a,)).manifest_json);row,=data['observations']
    assert row['source_observation']==original
    assert row['source_review']['category']=='switch'
    assert row['source_review']['derivation']=='inferred'
    assert row['source_observation']['confidence']==.95
    assert row['source_observation']['provenance'][0]['derivation']=='inferred'
    assert row['canonical_action']=='defer'


def test_pending_split_children_never_inherit_parent_anchor_or_become_devices(tmp_path):
    args,_,cite=case(tmp_path);p=json.loads(export(args).manifest_json)['observations'][0]['source_observation']
    a=assertion(p,cite,decision='split_required',category='unknown',type_proposal=None,work_status='unresolved')
    children=tuple(PendingSourceSplit(child_id='child-'+str(i),parent_proposal_id=p['proposal_id'],
        parent_proposal_fingerprint=digest(p),description='Unresolved glyph '+str(i),source_citations=(cite,)) for i in (1,2))
    data=json.loads(export(args,assertions=(a,),pending_splits=children).manifest_json)
    assert len(data['observations'])==1 and len(data['pending_splits'])==2
    assert all(c['local_xy_m'] is c['anchor_pt'] is c['bounds_pt'] is None for c in data['pending_splits'])
    assert all(not c['canonical_accepted'] and not c['countable'] for c in data['pending_splits'])
    with pytest.raises(ContractError,match='at least two'):
        export(args,assertions=(a,),pending_splits=children[:1])


@pytest.mark.parametrize('kind',['review','proposal','citation','frame','background'])
def test_stale_bindings_missing_frame_and_altered_background_fail(tmp_path,kind):
    args,frame,cite=case(tmp_path);p=json.loads(export(args).manifest_json)['observations'][0]['source_observation']
    if kind=='review':changes={'expected_review_fingerprint':'0'*64}
    elif kind=='proposal':changes={'assertions':(assertion(p,cite,proposal_fingerprint='0'*64),)}
    elif kind=='citation':changes={'assertions':(assertion(p,{**cite,'page':99}),)}
    elif kind=='frame':changes={'frames':(replace(frame,background=replace(frame.background,source=replace(frame.background.source,page=99))),)}
    else:changes={'background_images':{frame.background.id:b'not an image'}}
    with pytest.raises(ContractError):export(args,**changes)


def test_modified_journal_is_not_trusted_by_a_valid_manifest(tmp_path):
    args,_,_=case(tmp_path);path=next(iter(args['journal_paths'].values()));path.write_text(path.read_text().replace('strong-actual','forged-actual'))
    with pytest.raises(ContractError):export(args)


def test_linear_source_observation_preserves_ordered_2d_path(tmp_path):
    _,_,_,output=fixture();mark=replace(output.marks[0],category='linear_symbol',anchor=None,polyline=((.1,.2),(.3,.4)))
    args,_,_=case(tmp_path,output=replace(output,marks=(mark,)));row,=json.loads(export(args).manifest_json)['observations']
    assert row['local_anchor_xy_m'] is None
    assert len(row['local_polyline_xy_m'])==2
    assert row['local_polyline_xy_m'][0]==pytest.approx((-27,25))
    assert row['local_polyline_xy_m'][1]==pytest.approx((-19,35))


def test_offline_html_embeds_images_and_escapes_script_breakout(tmp_path):
    args,_,_=case(tmp_path);result=export(args,title='</script><script>bad()</script>')
    assert '</script><script>bad()' not in result.html
    assert '\\u003c/script' in result.html
    assert 'data:image/png;base64,' in result.html
    assert 'src="http' not in result.html and 'href="http' not in result.html
    assert 'pointermove' in result.html and 'source_review' in result.html


@pytest.mark.parametrize('changes',[{'metres_per_point':0},{'rotation_radians':float('nan')},{'origin_pt':(1,2,3)}])
def test_invalid_frame_choices_refuse(tmp_path,changes):
    _,frame,_=case(tmp_path)
    with pytest.raises(ContractError):replace(frame,**changes)


def test_empty_observed_page_remains_navigable_with_coverage(tmp_path):
    _,_,_,output=fixture();args,_,_=case(tmp_path,output=replace(output,marks=()))
    data=json.loads(export(args).manifest_json)
    assert data['observations']==[] and len(data['pages'])==1
    assert len(data['coverage'])==1 and data['canonical_instances_created']==0


def test_same_source_document_pages_keep_independent_frames_and_no_merge(tmp_path):
    args,frame,cite=case(tmp_path/'second')
    request,_,documents,output=fixture();document=documents[request.images[0].source.document_sha256]
    refs=[];pixels={}
    for im in request.images:
        ref,payload=render_source_image(document,source_id=im.source.source_id,page=1,scale=2,
            crop_px=im.crop_px,image_id=im.id+'-first',role=im.role)
        refs.append(ref);pixels[ref.id]=payload
    request=replace(request,images=tuple(refs),target_image_ids=('field-first',),
        contexts=tuple(replace(c,image_ids=tuple(key+'-first' for key in c.image_ids)) for c in request.contexts))
    output=replace(output,marks=(replace(output.marks[0],image_id='field-first',
        evidence_image_ids=tuple(key+'-first' for key in output.marks[0].evidence_image_ids)),),scanned_image_ids=('field-first',))
    (tmp_path/'first').mkdir()
    result=run(tmp_path/'first',request=request,output=output,images=pixels,documents=documents)
    bg,bg_pixels=render_source_image(document,source_id=refs[0].source.source_id,page=1,scale=1,
        crop_px=(0,0,200,100),image_id='first-background',role='floor_context')
    other=replace(frame,frame_id='other-page-local',background=bg,source_citations=({**cite,'page':1},))
    artifact=export(args,runs=(*args['runs'],result),images={**args['images'],**pixels},
        journal_paths={**args['journal_paths'],request.fingerprint:tmp_path/'first/attempts.jsonl'},
        frames=(frame,other),background_images={**args['background_images'],bg.id:bg_pixels})
    data=json.loads(artifact.manifest_json)
    assert len(data['observations'])==2 and len(data['pages'])==2
    assert len({r['proposal_id'] for r in data['observations']})==2
    assert {r['frame_id'] for r in data['observations']}=={'sheet-local','other-page-local'}
    assert all(p['inter_page_registration'] is None for p in data['pages'])


def test_browser_filter_selection_and_pending_children(tmp_path):
    """Execute the shipped UI script against a small synthetic DOM."""
    import re
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for executable UI regression')
    args,_,_=case(tmp_path)
    artifact=export(args)
    data=json.loads(artifact.manifest_json)
    row=data['observations'][0]
    row['source_review']={'work_status':'removed','type_proposal':'test'}
    data['pages'].append({**data['pages'][0],'frame_id':'empty'})
    data['pending_splits']=[{'child_id':'child-a','parent_proposal_id':row['proposal_id'],
                             'description':'Unlocated alpha'}]
    payload={'manifest':data,'backgrounds':{p['frame_id']:'' for p in data['pages']}}
    script=re.findall(r'<script>(.*?)</script>',artifact.html,re.S)[0]
    harness=r"""
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.value='';this.dataset={};this.style={};this.attrs={};this.events={};this.textContent='';this.classes=new Set();this.clientWidth=800;this.clientHeight=600;this.classList={toggle:(k,on)=>on?this.classes.add(k):this.classes.delete(k)}}
 append(n){this.children.push(n);if(this===elements.sheet&&!this.value&&n.value)this.value=n.value}
 replaceChildren(){this.children=[]}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.classes=new Set(v.split(' '));if(k==='data-id')this.dataset.id=v}
 getAttribute(k){return this.attrs[k]}
 addEventListener(k,f){this.events[k]=f}
 querySelectorAll(){return this.children.filter(n=>n.classes.has('mark'))}
 click(){this.events.click?.({stopPropagation(){}})}
}
const elements={};const document={getElementById:id=>elements[id]??=new Element(),createElement:()=>new Element(),createElementNS:()=>new Element(),querySelectorAll:()=>elements.drawing.querySelectorAll()};
elements['review-data']=new Element();elements['review-data'].textContent=PAYLOAD;
"""
    checks=r"""
assert(M.work_statuses.includes('removed'));
assert(elements.scope.children.some(n=>n.value==='removed'));
elements.list.children[0].click();
assert(elements.drawing.querySelectorAll().some(n=>n.classes.has('selected')));
elements.scope.value='removed';elements.scope.onchange();
assert(elements.drawing.querySelectorAll().some(n=>n.classes.has('selected')));
assert(elements.details.textContent.includes('Full source'));
elements.sheet.value='empty';elements.sheet.onchange();
assert(elements.details.textContent.startsWith('Select a visible'));
assert.equal(elements.drawing.querySelectorAll().length,0);
elements.sheet.value=M.pages[0].frame_id;elements.sheet.onchange();
elements.search.value='child-a';elements.search.oninput();
assert.equal(elements.list.children.length,1);
elements.list.children[0].click();assert(elements.details.textContent.includes('child-a'));
elements.search.value='alpha';elements.search.oninput();assert.equal(elements.list.children.length,1);
elements.scope.value='existing';elements.scope.onchange();
assert.equal(elements.list.children.length,0);
assert(elements.details.textContent.startsWith('Select a visible'));
elements.scope.value='removed';elements.search.value='no-match';elements.search.oninput();
assert.equal(elements.list.children.length,0);
elements.search.value='';elements.search.oninput();
elements.list.children[0].click();
elements.scope.value='existing';elements.scope.onchange();
assert(elements.details.textContent.startsWith('Select a visible'));
"""
    result=subprocess.run([node,'-e',harness.replace('PAYLOAD',json.dumps(json.dumps(payload)))+
                           script+checks],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
