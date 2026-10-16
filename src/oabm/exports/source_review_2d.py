"""Source-registered 2D review views; never accepted construction geometry."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from oabm.model import ContractError
from oabm.proposals.contracts import Portable, digest, fingerprint, text, _finite
from oabm.proposals.symbols import CATEGORIES
from oabm.proposals.takeoff import SourceImage, _validate_rendered_images
from oabm.importers.pdf_electrical.symbol_stage import build_symbol_review

VERSION = 'source-review-2d-v1'
WORK_STATUS = ('unresolved','new','existing','reuse_or_relocation','removed','not_in_contract','conflict')


def _point(value, label):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ContractError(label+' requires two source coordinates')
    return tuple(_finite(v,label) for v in value)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceReviewFrame(Portable):
    """Declared page-local XY convention, with no physical elevation field."""
    frame_id: str
    background: SourceImage
    origin_pt: tuple[float,float]
    metres_per_point: float
    rotation_radians: float
    label: str
    relative_level_label: str
    rationale: str
    source_citations: tuple[dict,...]

    def __post_init__(self):
        for key in ('frame_id','label','relative_level_label','rationale'):
            text(getattr(self,key),key,maximum=4096)
        if not isinstance(self.background,SourceImage):
            raise ContractError('frame requires a typed source image')
        bg=self.background
        if bg.crop_px != (0,0,bg.source.width_px,bg.source.height_px):
            raise ContractError('review background must be a full displayed source page')
        object.__setattr__(self,'origin_pt',_point(self.origin_pt,'view origin'))
        if max(abs(v) for v in self.origin_pt)>1_000_000_000:
            raise ContractError('view origin exceeds coordinate budget')
        scale=_finite(self.metres_per_point,'view scale')
        if not 0 < scale <= 100:
            raise ContractError('view scale must be positive and bounded')
        object.__setattr__(self,'metres_per_point',scale)
        object.__setattr__(self,'rotation_radians',_finite(self.rotation_radians,'view rotation'))
        _citation_shape(self.source_citations)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceReviewAssertion(Portable):
    """Separate source interpretation; even user derivation grants no BIM acceptance."""
    proposal_id: str
    proposal_fingerprint: str
    reviewer_id: str
    derivation: str
    decision: str
    category: str
    type_proposal: str | None
    work_status: str
    bid_scope: str
    rationale: str
    source_citations: tuple[dict,...]
    label: str | None = None

    def __post_init__(self):
        if self.label is not None:
            text(self.label,'source review label')
        for key in ('proposal_id','reviewer_id','bid_scope','rationale'):
            text(getattr(self,key),key,maximum=4096)
        fingerprint(self.proposal_fingerprint,'reviewed proposal fingerprint')
        if self.derivation not in ('inferred','user'):
            raise ContractError('source review requires explicit reviewer derivation')
        if self.decision not in ('source_supported','corrected','unknown','split_required','deferred'):
            raise ContractError('unsupported source-only review decision')
        if self.category not in CATEGORIES or self.work_status not in WORK_STATUS:
            raise ContractError('unsupported source category or work status')
        if self.type_proposal is not None:
            text(self.type_proposal,'reviewed type')
        _citation_shape(self.source_citations)


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingSourceSplit(Portable):
    """Unlocated review child, not a device or guessed replacement geometry."""
    child_id: str
    parent_proposal_id: str
    parent_proposal_fingerprint: str
    description: str
    source_citations: tuple[dict,...]

    def __post_init__(self):
        for key in ('child_id','parent_proposal_id','description'):
            text(getattr(self,key),key,maximum=4096)
        fingerprint(self.parent_proposal_fingerprint,'split parent fingerprint')
        _citation_shape(self.source_citations)


def _citation_shape(citations):
    if not isinstance(citations,tuple) or not 1 <= len(citations) <= 32:
        raise ContractError('source review needs bounded source citations')
    for citation in citations:
        if not isinstance(citation,dict):
            raise ContractError('source citation must be a mapping')
        text(citation.get('source_id'),'citation source')
        if 'document_sha256' in citation:
            fingerprint(citation['document_sha256'],'citation PDF digest')
        page=citation.get('page')
        if isinstance(page,bool) or not isinstance(page,int) or page<1:
            raise ContractError('source citation page must be positive')
        # Reject nonportable values and oversized payloads before HTML serialization.
        try:
            raw=json.dumps(citation,allow_nan=False)
        except (TypeError,ValueError,RecursionError) as exc:
            raise ContractError('source citation must be finite portable JSON') from exc
        if len(raw)>32768:
            raise ContractError('source citation exceeds review budget')


def _key(source):
    return source.source_id,source.document_sha256,source.page


def _xy(frame,point):
    x=(point[0]-frame.origin_pt[0])*frame.metres_per_point
    y=(point[1]-frame.origin_pt[1])*frame.metres_per_point
    c,s=math.cos(frame.rotation_radians),math.sin(frame.rotation_radians)
    return [c*x-s*y,s*x+c*y]


@dataclass(frozen=True, slots=True)
class SourceReview2DExport:
    manifest_json: str
    html: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.manifest_json.encode()).hexdigest()


def export_source_review_2d(*, project_id: str, title: str, runs: Sequence,
                           documents: Mapping[str,bytes], images: Mapping[str,bytes],
                           journal_paths: Mapping, frames: Sequence[SourceReviewFrame],
                           background_images: Mapping[str,bytes],
                           assertions: Sequence[SourceReviewAssertion]=(),
                           pending_splits: Sequence[PendingSourceSplit]=(),
                           expected_review_fingerprint: str | None=None) -> SourceReview2DExport:
    """Rebuild source/journal trust, derive XY and export an offline review viewer.

    Every page has its own named frame. Equal coordinates or a declared relative
    zero do not establish inter-sheet registration, physical Z or level elevation.
    """
    text(project_id,'project identity');text(title,'review title')
    if not runs or len(runs)>24:
        raise ContractError('2D review requires at most24 explicit runs')
    review=build_symbol_review(runs,documents=documents,images=images,journal_paths=journal_paths)
    if expected_review_fingerprint is not None and review.fingerprint!=expected_review_fingerprint:
        raise ContractError('stale source review fingerprint')
    if not frames or len(frames)>24 or any(not isinstance(f,SourceReviewFrame) for f in frames):
        raise ContractError('review requires bounded typed page frames')
    frames=tuple(SourceReviewFrame.from_dict(f.to_dict()) for f in frames)
    if len({f.frame_id for f in frames})!=len(frames) or len({_key(f.background.source) for f in frames})!=len(frames):
        raise ContractError('page-local review frames must be unique')
    if len({f.background.id for f in frames})!=len(frames):
        raise ContractError('background image IDs must be distinct')
    page_keys={_key(im.source) for run in runs for im in run.request.images
               if im.id in run.request.target_image_ids}
    if {_key(f.background.source) for f in frames}!=page_keys:
        raise ContractError('frames must exactly cover observed source pages')
    _validate_rendered_images(tuple(f.background for f in frames),background_images,
        {f.background.source.document_sha256:documents[f.background.source.document_sha256] for f in frames})
    aliases={}
    for run in runs:
        for im in run.request.images:
            aliases.setdefault((im.source.source_id,im.source.page),set()).add(im.source.document_sha256)
    def check_citations(citations):
        for citation in citations:
            sources=aliases.get((citation['source_id'],citation['page']),set())
            if len(sources)!=1 or (citation.get('document_sha256') is not None and citation['document_sha256'] not in sources):
                raise ContractError('citation must bind to an unambiguous supplied source page')
    for frame in frames:
        check_citations(frame.source_citations)
    proposals={p['proposal_id']:p for p in review.proposals}
    assertion_map={}
    if len(assertions)>len(proposals) or any(not isinstance(a,SourceReviewAssertion) for a in assertions):
        raise ContractError('invalid source assertion list')
    assertions=tuple(SourceReviewAssertion.from_dict(a.to_dict()) for a in assertions)
    for a in assertions:
        if a.proposal_id in assertion_map or a.proposal_id not in proposals:
            raise ContractError('source assertion references duplicate or absent proposal')
        if digest(proposals[a.proposal_id])!=a.proposal_fingerprint:
            raise ContractError('stale source assertion proposal binding')
        check_citations(a.source_citations);assertion_map[a.proposal_id]=a
    splits=[];split_ids=set();parents={}
    if len(pending_splits)>1024:
        raise ContractError('pending split review budget exceeded')
    for child in pending_splits:
        if not isinstance(child,PendingSourceSplit) or child.child_id in split_ids or child.child_id in proposals:
            raise ContractError('split children require unique review identities')
        child=PendingSourceSplit.from_dict(child.to_dict())
        parent=proposals.get(child.parent_proposal_id);a=assertion_map.get(child.parent_proposal_id)
        if parent is None or child.parent_proposal_fingerprint!=digest(parent) or a is None or a.decision!='split_required':
            raise ContractError('split requires exact parent and split review assertion')
        check_citations(child.source_citations);split_ids.add(child.child_id)
        parents[child.parent_proposal_id]=parents.get(child.parent_proposal_id,0)+1
        splits.append({**child.to_dict(),'anchor_pt':None,'local_xy_m':None,'bounds_pt':None,
                       'physical_elevation_m':None,'canonical_accepted':False,'countable':False})
    for a in assertions:
        if a.decision=='split_required' and parents.get(a.proposal_id,0)<2:
            raise ContractError('split-required parent needs at least two pending child records')
    frame_map={_key(f.background.source):f for f in frames};rows=[]
    for p in review.proposals:
        frame=frame_map[(p['source_id'],p['document_sha256'],p['page'])];a=assertion_map.get(p['proposal_id'])
        x0,y0,x1,y1=p['bounds_pt']
        rows.append({'proposal_id':p['proposal_id'],'source_mark_id':p['source_mark_id'],
          'proposal_fingerprint':digest(p),'frame_id':frame.frame_id,'page':p['page'],
          'source_observation':copy.deepcopy(p),'source_review':a.to_dict() if a else None,
          'local_anchor_xy_m':_xy(frame,p['anchor_pt']) if p['anchor_pt'] is not None else None,
          'local_polyline_xy_m':[_xy(frame,point) for point in p['polyline_pt']],
          'local_bounds_polygon_xy_m':[_xy(frame,point) for point in ((x0,y0),(x1,y0),(x1,y1),(x0,y1))],
          'physical_elevation_m':None,'mounting_height_m':None,'canonical_level_id':None,
          'canonical_accepted':False,'quantity_eligible':False,'canonical_action':'defer',
          'deferral_reasons':['source-only review view','physical placement not supplied']})
    pages=[];data_images={}
    for f in sorted(frames,key=lambda f:f.frame_id):
        bg=f.background
        pages.append({'frame_id':f.frame_id,'label':f.label,'source':bg.source.to_dict(),
          'background':bg.to_dict(),'origin_display_bottom_pt':list(f.origin_pt),
          'metres_per_point':f.metres_per_point,'rotation_radians':f.rotation_radians,
          'relative_level_label':f.relative_level_label,'relative_level_zero':0,
          'zero_role':'declared diagram convention; not physical elevation',
          'physical_elevation_m':None,'canonical_level_id':None,
          'registration_status':'page_local_convention_only','inter_page_registration':None,
          'rationale':f.rationale,'source_citations':copy.deepcopy(f.source_citations)})
        data_images[f.frame_id]='data:image/png;base64,'+base64.b64encode(background_images[bg.id]).decode()
    manifest={'version':VERSION,'project_id':project_id,'title':title,'symbol_review_fingerprint':review.fingerprint,
      'pages':pages,'observations':rows,'pending_splits':sorted(splits,key=lambda v:v['child_id']),
      'coverage':copy.deepcopy(review.coverage),'duplicate_candidates':review.to_dict()['duplicate_candidates'],
      'canonical_instances_created':0,'accepted_quantity_claims':False,
      'unknowns':['No physical elevation or mounting/ceiling height is assigned.',
                  'Page-local XY choices do not establish inter-page construction registration.',
                  'Source-only reviews do not authorize canonical placement or quantities.']}
    manifest_json=json.dumps(manifest,sort_keys=True,indent=2,allow_nan=False)
    payload=json.dumps({'manifest':manifest,'backgrounds':data_images},sort_keys=True,allow_nan=False)
    for char,escaped in (('<','\\u003c'),('>','\\u003e'),('&','\\u0026'),('\u2028','\\u2028'),('\u2029','\\u2029')):
        payload=payload.replace(char,escaped)
    return SourceReview2DExport(manifest_json,_HTML.replace('__REVIEW_DATA__',payload))


_HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Source review 2D</title>
<style>*{box-sizing:border-box}body{margin:0;background:#edf0f3;color:#17212c;font:14px system-ui}header{padding:14px 20px;background:#172b3a;color:#fff}h1{font-size:19px;margin:0 0 5px}header p{margin:0;color:#c6d4dd}.layout{display:grid;grid-template-columns:310px 1fr;height:calc(100vh - 83px)}aside{padding:14px;background:white;overflow:auto}main{position:relative;overflow:hidden}label{display:block;margin:10px 0 4px;color:#45586a}input,select,button{font:inherit;padding:8px;border:1px solid #c1cad2;border-radius:5px}input,select{width:100%}button{background:white;cursor:pointer}.tools{display:flex;gap:5px;padding:8px;position:absolute;top:0;left:0;z-index:2;background:#ffffffd9}.tools button{padding:5px 10px}svg{width:100%;height:100%;touch-action:none;cursor:grab}.mark{fill:#c026d320;stroke:#b91c86;stroke-width:1.3;vector-effect:non-scaling-stroke;cursor:pointer}.mark[data-unknown="true"]{fill:#d9770630;stroke:#be6a00}.mark[data-nic="true"]{fill:#64748b10;stroke:#64748b}.mark.selected{stroke:#0075ff;stroke-width:4}.item{width:100%;text-align:left;margin:3px 0;font-size:12px}.muted{color:#657381;font-size:12px}.details{max-height:42vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border-top:1px solid #dce1e5;margin-top:12px;padding-top:12px;font-size:12px}.badge{padding:6px;background:#fff0c5;margin:12px 0;color:#5e480a}@media(max-width:700px){.layout{grid-template-columns:220px 1fr}aside{padding:8px}}</style>
<header><h1 id="title"></h1><p>Source-registered 2D review · relative zero is a convention · physical heights unknown · no accepted construction geometry</p></header>
<div class="layout"><aside><label for="sheet">Source sheet / local frame</label><select id="sheet"></select><p class="muted" id="frame"></p><label for="search">Find proposal or source text</label><input id="search" placeholder="Search ID, type, source description"><label for="scope">Source work status</label><select id="scope"><option value="">All observations</option><option>new</option><option>existing</option><option>reuse_or_relocation</option><option>not_in_contract</option><option>unresolved</option><option>conflict</option></select><div class="badge" id="count"></div><div id="details" class="details">Select a mark to inspect original evidence, review corrections and local XY.</div><div id="list"></div></aside><main><div class="tools"><button id="observed">Fit observations</button><button id="fit">Fit sheet</button><button id="plus">+</button><button id="minus">−</button><button id="marks">Toggle marks</button></div><svg id="drawing" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Source sheet with pending observation overlays"></svg></main></div>
<script type="application/json" id="review-data">__REVIEW_DATA__</script><script>
'use strict';const D=JSON.parse(document.getElementById('review-data').textContent),M=D.manifest,$=id=>document.getElementById(id),N='http://www.w3.org/2000/svg';let page,box,drag=null,showMarks=true,selected=null;
$('title').textContent=M.title;document.title=M.title+' — source review';M.pages.forEach(p=>{let o=document.createElement('option');o.value=p.frame_id;o.textContent=p.label;$('sheet').append(o)});
const work=r=>r.source_review?.work_status||'unresolved',type=r=>r.source_review?(r.source_review.type_proposal||'unknown'):(r.source_observation.type_proposal||'unknown');
function node(name,attrs){let n=document.createElementNS(N,name);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));return n}
function view(){ $('drawing').setAttribute('viewBox',box.join(' ')) }
function fit(){box=[0,0,page.source.width_pt,page.source.height_pt];view()}
function fitObserved(){let marks=[...$('drawing').querySelectorAll('.mark')];if(!marks.length)return fit();let x0=Math.min(...marks.map(n=>+n.getAttribute('x'))),y0=Math.min(...marks.map(n=>+n.getAttribute('y'))),x1=Math.max(...marks.map(n=>+n.getAttribute('x') + +n.getAttribute('width'))),y1=Math.max(...marks.map(n=>+n.getAttribute('y') + +n.getAttribute('height')));x0=Math.max(0,x0-60);y0=Math.max(0,y0-60);x1=Math.min(page.source.width_pt,x1+60);y1=Math.min(page.source.height_pt,y1+60);box=[x0,y0,x1-x0,y1-y0];view()}
function select(r){selected=r.proposal_id;document.querySelectorAll('.mark').forEach(n=>n.classList.toggle('selected',n.dataset.id===selected));$('details').scrollTop=0;$('details').textContent=(r.source_review?.label||r.proposal_id)+'\nType: '+type(r)+' | Work: '+work(r)+'\nLocal XY (m): '+JSON.stringify(r.local_anchor_xy_m)+'\nPhysical elevation / mounting height: unknown\nCanonical action: defer\n\nFull source and review evidence:\n'+JSON.stringify({proposal_id:r.proposal_id,source_mark_id:r.source_mark_id,original:r.source_observation,source_review:r.source_review,local_xy_m:r.local_anchor_xy_m,local_polyline_xy_m:r.local_polyline_xy_m,coordinate_frame:r.frame_id,physical_elevation_m:null,mounting_height_m:null,canonical_action:'defer',quantity_eligible:false,pending_children:M.pending_splits.filter(c=>c.parent_proposal_id===r.proposal_id)},null,2)}
function draw(){page=M.pages.find(p=>p.frame_id===$('sheet').value);$('frame').textContent=page.relative_level_label+'; local zero=0 by convention. '+page.registration_status+'. '+page.metres_per_point+' m/source point.';let svg=$('drawing');svg.replaceChildren();svg.append(node('image',{href:D.backgrounds[page.frame_id],width:page.source.width_pt,height:page.source.height_pt}));let q=$('search').value.toLowerCase(),s=$('scope').value,rows=M.observations.filter(r=>r.frame_id===page.frame_id&&(!s||work(r)===s)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));$('list').replaceChildren();rows.forEach(r=>{let p=r.source_observation,[x0,y0,x1,y1]=p.bounds_pt,n=node('rect',{x:x0,y:page.source.height_pt-y1,width:x1-x0,height:y1-y0,class:'mark','data-id':r.proposal_id,'data-unknown':type(r)==='unknown','data-nic':work(r)==='not_in_contract'});n.style.display=showMarks?'':'none';n.addEventListener('click',e=>{e.stopPropagation();select(r)});let title=node('title',{});title.textContent=r.proposal_id+' · '+type(r)+' · '+work(r);n.append(title);svg.append(n);let b=document.createElement('button');b.className='item';b.textContent=type(r)+' · '+work(r)+' · '+(r.source_review?.label||r.proposal_id.slice(-10));b.addEventListener('click',()=>{select(r);let w=Math.max(100,(x1-x0)*8),h=w*svg.clientHeight/svg.clientWidth;box=[(x0+x1-w)/2,page.source.height_pt-(y0+y1)/2-h/2,w,h];view()});$('list').append(b)});let splits=M.pending_splits.filter(c=>M.observations.find(r=>r.proposal_id===c.parent_proposal_id)?.frame_id===page.frame_id);$('count').textContent=rows.length+' visible pending observations · '+splits.length+' unlocated split children · 0 canonical accepted';splits.forEach(c=>{let b=document.createElement('button');b.className='item';b.textContent='Unlocated child: '+c.description;b.addEventListener('click',()=>{$('details').textContent=JSON.stringify(c,null,2)});$('list').append(b)});fitObserved()}
function zoom(f,cx=.5,cy=.5){box=[box[0]+box[2]*cx*(1-f),box[1]+box[3]*cy*(1-f),box[2]*f,box[3]*f];view()}
$('sheet').onchange=draw;$('search').oninput=draw;$('scope').onchange=draw;$('fit').onclick=fit;$('observed').onclick=fitObserved;$('plus').onclick=()=>zoom(.75);$('minus').onclick=()=>zoom(1.33);$('marks').onclick=()=>{showMarks=!showMarks;document.querySelectorAll('.mark').forEach(n=>n.style.display=showMarks?'':'none')};let svg=$('drawing');svg.addEventListener('wheel',e=>{e.preventDefault();let r=svg.getBoundingClientRect();zoom(e.deltaY>0?1.15:.87,(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height)},{passive:false});svg.addEventListener('pointerdown',e=>{if(e.target.closest('.mark'))return;drag={x:e.clientX,y:e.clientY,box:[...box]};svg.setPointerCapture(e.pointerId)});svg.addEventListener('pointermove',e=>{if(!drag)return;box=[drag.box[0]-(e.clientX-drag.x)*drag.box[2]/svg.clientWidth,drag.box[1]-(e.clientY-drag.y)*drag.box[3]/svg.clientHeight,drag.box[2],drag.box[3]];view()});svg.addEventListener('pointerup',()=>drag=null);draw();
</script></html>'''
