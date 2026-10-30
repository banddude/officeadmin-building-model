"""Source-bound supplied 2D lighting/RCP presentation, never a physical BIM model.

Caller-supplied XY and source snapshots are presented without assigning elevation,
installation facts or quantities. Shared company sheets own the frame/PDF output.
"""
from __future__ import annotations

import argparse
import html
import math
import os
from pathlib import Path
import re

from .blank_set import _decode, _identifier, _json, _sha, _text, _runtime
from .company_sheets import (_body_group, _font, measure_sheet_text, validate_sheet_text,
                             resolve_company_sheet_set, create_company_sheet_set)

VERSION = 'supplied-lighting-rcp-v1'
_FACTORS = {'m': 1.0, 'mm': .001, 'ft': .3048}
_SYMBOLS = {'track_head', 'downlight', 'linear', 'generic'}


def _keys(obj, keys, label):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError(f'{label}: exact keys required: {", ".join(keys)}')


def _number(value, label):
    if type(value) not in (int, float): raise ValueError(f'{label}: finite number required')
    try:value=float(value)
    except OverflowError as exc:raise ValueError(f'{label}: finite number required') from exc
    if not math.isfinite(value):raise ValueError(f'{label}: finite number required')
    return value


def _point(value, label):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f'{label}: explicit [x,y] required')
    return tuple(_number(x, label) for x in value)


def _rows(value, label, *, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{label}: list of at most {maximum} required')
    return value


def _layout_id(value, label):
    _text(value,label)
    if len(value)>128:raise ValueError(f'{label}: at most 128 characters')
    validate_sheet_text(value)
    return value


def _source_refs(value, known, label):
    if not isinstance(value, list) or not value or any(not isinstance(x,str) for x in value):
        raise ValueError(f'{label}: nonempty source_ids list required')
    if len(set(value)) != len(value) or set(value) - known:
        raise ValueError(f'{label}: duplicate or unknown source IDs')
    return value


def render_lighting_rcp_body(request, sheet, *, source_bytes):
    """Return (bounded SVG group, source map) from explicit 2D supplied facts.

    `source_bytes` maps every declared source ID to exact bytes; hashes are
    verified, but neither those bytes nor this drawing become physical BIM.
    """
    required=('version','id','units','frame','sources','context','fixtures','tracks','zones','notes',
              'declared_counts','drawing','physical_elevation_m','ceiling_height_m')
    _keys(request, required + (('annotations',) if isinstance(request,dict) and 'annotations' in request else ()), 'lighting request')
    if request['version'] != VERSION: raise ValueError('lighting request: unsupported version')
    _identifier(request['id'], 'lighting id')
    if not isinstance(request['units'],str) or request['units'] not in _FACTORS: raise ValueError('units: use m, mm or ft')
    factor = _FACTORS[request['units']]
    if request['physical_elevation_m'] is not None or request['ceiling_height_m'] is not None:
        raise ValueError('v1 requires unknown physical_elevation_m and ceiling_height_m as null')
    frame = request['frame']
    _keys(frame, ('id','origin','x_axis','y_axis','qualification'), 'frame')
    _identifier(frame['id'], 'frame.id'); _point(frame['origin'], 'frame.origin')
    if frame['x_axis'] != 'right' or frame['y_axis'] != 'up':
        raise ValueError('frame: explicitly use x_axis=right and y_axis=up')
    _text(frame['qualification'], 'frame.qualification')
    sources = _rows(request['sources'], 'sources', maximum=64)
    if not sources: raise ValueError('sources: at least one source required')
    known = set()
    if not isinstance(source_bytes, dict): raise ValueError('source_bytes: mapping required')
    for source in sources:
        _keys(source, ('id','path','sha256','qualification'), 'source')
        identity = _identifier(source['id'], 'source.id')
        if identity in known: raise ValueError(f'source: duplicate ID {identity}')
        known.add(identity); _text(source['path'], f'{identity}.path'); _text(source['qualification'], f'{identity}.qualification')
        if not isinstance(source['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',source['sha256']):
            raise ValueError(f'{identity}: lowercase SHA-256 required')
        data = source_bytes.get(identity)
        if not isinstance(data,bytes) or not data or len(data)>64*1024*1024 or _sha(data)!=source['sha256']:
            raise ValueError(f'{identity}: source bytes missing, oversized or SHA-256 mismatch')
    if set(source_bytes) != known: raise ValueError('source_bytes: exactly the declared source IDs required')
    if sum(map(len,source_bytes.values()))>256*1024*1024: raise ValueError('sources: total source budget exceeded')
    lists = {name:_rows(request[name],name) for name in ('context','fixtures','tracks','zones','notes')}
    lists['annotations']=_rows(request.get('annotations',[]),'annotations')
    counts=request['declared_counts']; _keys(counts, ('context','fixtures','tracks'), 'declared_counts')
    for name in counts:
        if type(counts[name]) is not int or counts[name] != len(lists[name]):
            raise ValueError(f'declared_counts.{name}: does not match supplied {name} list')
    seen=set()
    for name, rows in lists.items():
        for row in rows:
            if not isinstance(row,dict): raise ValueError(f'{name}: rows must be objects')
            identity=_layout_id(row.get('id'), f'{name}.id')
            if identity in seen: raise ValueError(f'duplicate layout ID {identity}')
            seen.add(identity)
            _source_refs(row.get('source_ids'), known, identity)
    zones={}
    for row in lists['zones']:
        _keys(row, ('id','label','source_ids'), 'zone')
        zones[row['id']]=_text(row['label'], f"{row['id']}.label")
    fixtures={}
    for row in lists['fixtures']:
        _keys(row, ('id','kind','symbol','zone_id','xy','aim_xy','source_ids'), 'fixture')
        _text(row['kind'], f"{row['id']}.kind")
        if not isinstance(row['symbol'],str) or row['symbol'] not in _SYMBOLS: raise ValueError(f"{row['id']}: unsupported fixture symbol")
        if row['zone_id'] is not None and (not isinstance(row['zone_id'],str) or row['zone_id'] not in zones): raise ValueError(f"{row['id']}: unknown zone_id")
        xy=_point(row['xy'], f"{row['id']}.xy")
        if row['aim_xy'] is not None and _point(row['aim_xy'], f"{row['id']}.aim_xy")==xy:
            raise ValueError(f"{row['id']}: aim point must differ from fixture XY")
        fixtures[row['id']]=row
    used_heads=set()
    for row in lists['tracks']:
        _keys(row, ('id','points','head_ids','source_ids'), 'track')
        _polyline(row['points'],row['id'],False)
        ids=row['head_ids']
        if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids) or set(ids)-set(fixtures):
            raise ValueError(f"{row['id']}: duplicate or unknown head_ids")
        if used_heads.intersection(ids): raise ValueError(f"{row['id']}: head belongs to more than one track")
        if any(fixtures[head]['symbol']!='track_head' for head in ids):
            raise ValueError(f"{row['id']}: head_ids must reference track_head fixtures")
        used_heads.update(ids)
    if sum(len(row.get('points',[])) for row in lists['context']+lists['tracks'] if isinstance(row.get('points'),list))>100000:
        raise ValueError('context/tracks exceed total point budget')
    for row in lists['context']:
        _keys(row, ('id','points','closed','source_ids'), 'context')
        if type(row['closed']) is not bool: raise ValueError(f"{row['id']}: closed must be boolean")
        _polyline(row['points'],row['id'],row['closed'])
    for row in lists['notes']:
        _keys(row, ('id','text','source_ids'), 'note'); _text(row['text'], f"{row['id']}.text")
    for row in lists['annotations']:
        _keys(row, ('id','text','xy','source_ids'), 'annotation'); _text(row['text'], f"{row['id']}.text")
        _point(row['xy'], f"{row['id']}.xy")
    drawing=request['drawing']
    drawing_keys=('sheet_id','scale','origin_mm','view_bounds')
    _keys(drawing, drawing_keys + (('notes_bounds_mm',) if isinstance(drawing,dict) and 'notes_bounds_mm' in drawing else ()), 'drawing')
    if drawing['sheet_id'] != sheet.id: raise ValueError('drawing.sheet_id differs from shared sheet')
    scale=_number(drawing['scale'], 'drawing.scale')
    if scale<=0: raise ValueError('drawing.scale must be positive')
    if sheet.fields.get('scale')!=f'1:{scale:g}': raise ValueError('shared sheet scale must match drawing scale as 1:N')
    x0,y0=_point(drawing['origin_mm'], 'drawing.origin_mm')
    bounds=drawing['view_bounds']
    if not isinstance(bounds,list) or len(bounds)!=4: raise ValueError('drawing.view_bounds: explicit [min_x,min_y,max_x,max_y] required')
    xmin,ymin,xmax,ymax=(_number(v,'drawing.view_bounds') for v in bounds)
    if xmin>=xmax or ymin>=ymax: raise ValueError('drawing.view_bounds must have positive area')
    ppu=1000*factor/scale
    left,top,right,bottom=sheet.drawing_bounds_mm
    if x0<left or y0<top or x0+(xmax-xmin)*ppu>right or y0+(ymax-ymin)*ppu>bottom:
        raise ValueError('drawing view_bounds exceed shared drawing area at requested scale/origin')
    notes_bounds=drawing.get('notes_bounds_mm')
    if notes_bounds is not None:
        if not isinstance(notes_bounds,list) or len(notes_bounds)!=4:raise ValueError('notes_bounds_mm: explicit [left,top,right,bottom] required')
        notes_bounds=tuple(_number(v,'notes_bounds_mm') for v in notes_bounds)
        nl,nt,nr,nb=notes_bounds
        if not (left<=nl<nr<=right and top<=nt<nb<=bottom):raise ValueError('notes_bounds_mm must be a positive rectangle within shared drawing area')
        view_right=x0+(xmax-xmin)*ppu;view_bottom=y0+(ymax-ymin)*ppu;gap=3.0
        if not (nr+gap<=x0 or view_right+gap<=nl or nb+gap<=y0 or view_bottom+gap<=nt):
            raise ValueError('notes_bounds_mm must be disjoint from view_bounds with 3 mm clearance')
    def point(value, label):
        x,y=_point(value,label)
        if not (xmin<=x<=xmax and ymin<=y<=ymax): raise ValueError(f'{label}: point outside explicit view_bounds')
        return x0+(x-xmin)*ppu,y0+(ymax-y)*ppu
    parts=['<g>']; mapping=[]
    def add(identity, role, source_ids, markup, **data):
        index=len(parts)-1;parts.append(markup)
        mapping.append({'id':identity,'role':role,'source_ids':source_ids,'body_child_index':index,**data})
    def poly(row, role, width):
        coords=[point(p,row['id']) for p in row['points']]
        points=' '.join(f'{x:.12g},{y:.12g}' for x,y in coords)
        tag='polygon' if row.get('closed',False) else 'polyline'
        add(row['id'],role,row['source_ids'],f'<{tag} points="{points}" fill="none" stroke="#000000" stroke-width="{width}" stroke-linejoin="round"/>',input_points=row['points'])
    for row in lists['context']:poly(row,'context',.13)
    for row in lists['tracks']:poly(row,'track',.5)
    pending_labels=[]
    for row in lists['fixtures']:
        x,y=point(row['xy'],row['id']); kind=row['symbol']
        mark=_fixture_symbol(x,y,kind)
        add(row['id'],'fixture',row['source_ids'],mark,xy=row['xy'],kind=row['kind'],symbol=kind,zone_id=row['zone_id'])
        if row['aim_xy'] is not None:
            ax,ay=point(row['aim_xy'],row['id']+'.aim_xy')
            add(row['id'],'aim',row['source_ids'],f'<g><line x1="{x:.12g}" y1="{y:.12g}" x2="{ax:.12g}" y2="{ay:.12g}" stroke="#000000" stroke-width="0.18"/><circle cx="{ax:.12g}" cy="{ay:.12g}" r="0.8" fill="none" stroke="#000000" stroke-width="0.18"/></g>',aim_xy=row['aim_xy'])
        value=validate_sheet_text(row['id'],font_family='Liberation Sans')
        # Clear the actual paper mark, including its stroke, with a modest gap.
        # A linear mark is wider than the circular/square marks; the plate's
        # font-bearing/padding extends left of its text anchor.
        mark_right=x+(3.125 if kind=='linear' else 2.125)
        _,_,origin_box=_label_markup(0,0,value)
        label_x=mark_right+.3-origin_box[0]
        plate,markup,box=_label_markup(label_x,y-1,value)
        pending_labels.append((row['id'],'fixture_label',row['source_ids'],plate,markup,box,{}))
    for row in lists['annotations']:
        x,y=point(row['xy'],row['id']+'.xy')
        value=validate_sheet_text(row['text'],font_family='Liberation Sans')
        plate,markup,box=_label_markup(x,y,value)
        pending_labels.append((row['id'],'annotation',row['source_ids'],plate,markup,box,{'xy':row['xy'],'text':row['text']}))
    # Track IDs are presentation tags near their supplied endpoints.
    # Never silently drop a tag: try finite paper-space candidates while avoiding
    # fixture/annotation text and other track tags; linework still needs review.
    occupied=[]
    for row in lists['fixtures']:
        x,y=point(row['xy'],row['id'])
        occupied.append((x-3.5,y-3.5,x+3.5,y+3.5))
    occupied.extend(row[5] for row in pending_labels)
    view_box=(x0,y0,x0+(xmax-xmin)*ppu,y0+(ymax-ymin)*ppu)
    def overlap(a,b):return a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3]
    for row in lists['tracks']:
        value=validate_sheet_text(row['id'],font_family='Liberation Sans')
        width=measure_sheet_text(value,3.2);selected=None
        endpoints=(row['points'][0],row['points'][-1])
        for endpoint in endpoints:
            x,y=point(endpoint,row['id'])
            for gap in (3,7,12,18):
                for xx,yy in ((x+gap,y-gap),(x+gap,y+gap+3.2),(x-gap-width,y-gap),(x-gap-width,y+gap+3.2),
                              (x-width/2,y-gap),(x-width/2,y+gap+3.2)):
                    plate,markup,box=_label_markup(xx,yy,value)
                    if any(overlap(box,other) for other in occupied):continue
                    try:_body_group('<g>'+plate+markup+'</g>',view_box)
                    except ValueError:continue
                    selected=(box,plate,markup,xx,yy,endpoint);break
                if selected is not None:break
            if selected is not None:break
        if selected is None:raise ValueError(f"{row['id']}: no bounded track-tag position near either endpoint; enlarge view or revise layout")
        box,plate,markup,xx,yy,endpoint=selected;occupied.append(box)
        add(row['id'],'track_label_plate',row['source_ids'],plate,paper_bounds_mm=list(box))
        add(row['id'],'track_label',row['source_ids'],markup,endpoint_xy=endpoint,paper_anchor_mm=[xx,yy])
    for identity,role,refs,plate,markup,box,metadata in pending_labels:
        add(identity,role+'_plate',refs,plate,paper_bounds_mm=list(box))
        add(identity,role,refs,markup,**metadata)
    # With a side column, keep *actual* geometry, symbol and annotation ink
    # inside the allocated view, not just their anchors, so it cannot intrude
    # into the caller's notes column.
    if notes_bounds is not None:
        try:_body_group(''.join(parts)+'</g>',(x0,y0,x0+(xmax-xmin)*ppu,y0+(ymax-ymin)*ppu))
        except ValueError as exc:raise ValueError('view ink exceeds view rectangle; pad view_bounds or revise explicit placement before using notes_bounds_mm') from exc
    # Caller may reserve an explicit side column; otherwise notes sit below the view.
    text_x=(notes_bounds[0]+1) if notes_bounds is not None else x0
    text_y=(notes_bounds[1]+4.2) if notes_bounds is not None else y0+(ymax-ymin)*ppu+6
    count_refs=list(dict.fromkeys(source_id for row in lists['fixtures']+lists['tracks'] for source_id in row['source_ids']))
    def text_line(identity,role,value,refs,*,indent=0,weight=400,line_number=0):
        nonlocal text_y
        value=validate_sheet_text(value,font_family='Liberation Sans',font_weight=weight)
        at_x=text_x+indent
        available=(notes_bounds[2]-at_x-1) if notes_bounds is not None else (right-at_x-1)
        lines=_wrap_notes(value,available,weight=weight)
        for part_number,line in enumerate(lines):
            markup=f'<text x="{at_x:.12g}" y="{text_y:.12g}" font-family="Liberation Sans" font-size="3.2" font-weight="{weight}" fill="#000000">{html.escape(line)}</text>'
            if notes_bounds is not None:
                try:_body_group('<g>'+markup+'</g>',notes_bounds)
                except ValueError as exc:raise ValueError('notes_bounds_mm: text does not fit; enlarge the supplied column or revise its contents') from exc
            add(identity,role,refs,markup,text=line,line_number=line_number,part_number=part_number)
            text_y+=5
    def heading(title):
        nonlocal text_y
        text_line('heading','heading',title,[],weight=600)
        text_y+=1
    heading('GENERAL NOTES')
    general=[('qualification','SOURCE REVIEW ONLY; NOT PHYSICAL BIM OR ENGINEERING APPROVAL.',[]),
             ('heights','MOUNTING ELEVATION AND CEILING HEIGHT UNKNOWN.',[]),
             ('counts',f"DESIGN COUNTS: {len(fixtures)} FIXTURES; {len(lists['tracks'])} TRACKS. NOT INSTALLED QUANTITIES.",count_refs)]
    general += [(row['id'],row['text'],row['source_ids']) for row in lists['notes']]
    for number,(identity,value,refs) in enumerate(general,1):
        text_line(identity,'note',f'{number}. {value}',refs,line_number=number)
    text_y+=2
    heading('LIGHTING FIXTURE LEGEND')
    legend={}
    for row in lists['fixtures']:
        key=(row['symbol'],row['kind'])
        info=legend.setdefault(key,{'refs':[],'count':0});info['count']+=1
        for source_id in row['source_ids']:
            if source_id not in info['refs']:info['refs'].append(source_id)
    for (symbol,kind),info in legend.items():
        mark=_fixture_symbol(text_x+4,text_y-1,symbol)
        if notes_bounds is not None:
            try:_body_group('<g>'+mark+'</g>',notes_bounds)
            except ValueError as exc:raise ValueError('notes_bounds_mm: fixture legend does not fit; enlarge column') from exc
        add('legend','legend_symbol',info['refs'],mark,symbol=symbol,kind=kind,design_count=info['count'])
        display_kind=kind.replace('_',' ')
        display_symbol={'track_head':'TRACK HEAD','downlight':'DOWNLIGHT','linear':'LINEAR','generic':'GENERIC'}[symbol]
        text_line('legend','legend_label',f"{display_kind} / {display_symbol} - DESIGN COUNT {info['count']}",info['refs'],indent=10)
    if lists['zones']:
        text_y+=2;heading('LIGHTING ZONES')
        for row in lists['zones']:
            members=[f for f in lists['fixtures'] if f['zone_id']==row['id']]
            refs=list(dict.fromkeys(row['source_ids']+[source for f in members for source in f['source_ids']]))
            text_line(row['id'],'zone_summary',f"{row['id']}: {row['label']} - DESIGN COUNT {len(members)}",refs)
    parts.append('</g>');body='\n'.join(parts)
    _body_group(body,sheet.drawing_bounds_mm)
    source_map={'version':VERSION,'layout_id':request['id'],'frame':frame,'units':request['units'],
                'drawing':drawing,'items':mapping,'tracks':[{'id':r['id'],'head_ids':r['head_ids']} for r in lists['tracks']],
                'sources':sources,'design_counts':counts,'physical_elevation_m':None,'ceiling_height_m':None,
                'canonical_model_created':False,'quantity_eligible':False,'engineering_approval':False,
                'limitations':['Source XY design intent only; source registration and design facts are not independently verified.',
                    'Fixed paper symbols are not physical fixture dimensions. Aims are supplied points, not optical calculations.',
                    'No physical Z, host, code, installation, quantities or engineering approval is inferred.',
                    'White text backplates mask source linework visually without changing retained input geometry.',
                    'Independent visual review of labels, linework and source evidence is required.']}
    return body,source_map


def _label_markup(x,y,value):
    """Measured, modestly padded paper-space plate plus source-preserving text."""
    value=validate_sheet_text(value,font_family='Liberation Sans')
    size=3.2;pad=.4;safety=size*.01
    font,_,_,_=_font('Liberation Sans',400)
    advance=font.getlength(value)*size/1000
    ink=tuple(v*size/1000 for v in font.getbbox(value,anchor='ls'))
    bounds=(x+min(0,ink[0])-safety-pad,y+min(-size,ink[1])-pad,
            x+max(advance,ink[2])+safety+pad,y+max(size*.3,ink[3])+pad)
    l,t,r,b=bounds
    plate=f'<rect x="{l:.12g}" y="{t:.12g}" width="{r-l:.12g}" height="{b-t:.12g}" fill="#ffffff"/>'
    text=f'<text x="{x:.12g}" y="{y:.12g}" font-family="Liberation Sans" font-size="3.2" fill="#000000">{html.escape(value)}</text>'
    return plate,text,bounds


def _fixture_symbol(x,y,kind):
    """Generic paper-mm symbol, identical in view and legend."""
    if kind=='track_head':
        return f'<g><circle cx="{x:.12g}" cy="{y:.12g}" r="2" fill="none" stroke="#000000" stroke-width="0.25"/><line x1="{x:.12g}" y1="{y-2:.12g}" x2="{x:.12g}" y2="{y+2:.12g}" stroke="#000000" stroke-width="0.25"/></g>'
    if kind=='downlight':
        return f'<circle cx="{x:.12g}" cy="{y:.12g}" r="2" fill="none" stroke="#000000" stroke-width="0.25"/>'
    if kind=='linear':
        return f'<rect x="{x-3:.12g}" y="{y-1:.12g}" width="6" height="2" fill="none" stroke="#000000" stroke-width="0.25"/>'
    return f'<rect x="{x-2:.12g}" y="{y-2:.12g}" width="4" height="4" fill="none" stroke="#000000" stroke-width="0.25"/>'


def _wrap_notes(value, width, *, weight=400):
    # Preserve every source character; prefer word boundaries but never drop or
    # truncate text. Text and ink are independently checked against the column.
    if width<=0:raise ValueError('notes_bounds_mm: column too narrow')
    lines=[];remaining=value
    while remaining:
        lo,hi=1,len(remaining);fit=0
        while lo<=hi:
            mid=(lo+hi)//2
            if measure_sheet_text(remaining[:mid],3.2,font_weight=weight)<=width:fit=mid;lo=mid+1
            else:hi=mid-1
        if not fit:raise ValueError('notes_bounds_mm: column too narrow for text')
        if fit<len(remaining):
            split=remaining.rfind(' ',0,fit)
            if split>0:fit=split+1
        lines.append(remaining[:fit]);remaining=remaining[fit:]
    return lines


def _polyline(value, label, closed):
    if not isinstance(value,list) or len(value)<(3 if closed else 2) or len(value)>10000:
        raise ValueError(f'{label}: insufficient or excessive path points')
    points=[_point(p,label) for p in value]
    if any(a==b for a,b in zip(points,points[1:])) or closed and points[0]==points[-1]:
        raise ValueError(f'{label}: zero-length edge or repeated closing point')
    if closed and len(set(points))<3:
        raise ValueError(f'{label}: closed path requires at least three distinct vertices')


def create_lighting_rcp_sheet_set(request_path, profile_path, job_path, *, output_directory):
    """Validate snapshots, then publish one shared-company sheet and receipts."""
    request_path=Path(request_path);raw=request_path.read_bytes();request=_decode(raw)
    if not isinstance(request,dict):raise ValueError('lighting request: object required')
    source_rows=_rows(request.get('sources'),'sources',maximum=64)
    sources={}
    for row in source_rows:
        if not isinstance(row,dict):raise ValueError('source: object required')
        identity=_identifier(row.get('id'),'source.id')
        if identity in sources:raise ValueError(f'source: duplicate ID {identity}')
        _text(row.get('path'),'source.path')
        try:sources[identity]=(request_path.parent/row['path']).read_bytes()
        except OSError as exc:raise ValueError(f"source {row.get('id')}: cannot read {row['path']}: {exc.strerror or exc}") from exc
    resolved=resolve_company_sheet_set(profile_path,job_path)
    if len(resolved.sheets)!=1:raise ValueError('lighting job must contain exactly one sheet')
    body,source_map=render_lighting_rcp_body(request,resolved.sheets[0],source_bytes=sources)
    output=Path(output_directory).absolute();output.parent.mkdir(parents=True,exist_ok=True);output.mkdir()
    pending=output/'.pending';pending.mkdir();stage='source snapshots'
    try:
        inputs=pending/'inputs';inputs.mkdir()
        (inputs/'lighting-request.json').write_bytes(raw)
        source_dir=inputs/'sources';source_dir.mkdir()
        for key,data in sources.items():(source_dir/(key+'.bin')).write_bytes(data)
        (pending/'source-map.json').write_bytes(_json(source_map))
        stage='shared sheet production'
        shared=create_company_sheet_set(profile_path,job_path,output_directory=pending/'company',bodies={resolved.sheets[0].id:body})
        if ((shared/'inputs/profile.json').read_bytes()!=resolved.profile_json or
            (shared/'inputs/job.json').read_bytes()!=resolved.job_json or
            resolve_company_sheet_set(profile_path,job_path)!=resolved):
            raise ValueError('profile, job or logo changed during lighting sheet production')
        files=[{'path':p.relative_to(pending).as_posix(),'bytes':p.stat().st_size,'sha256':_sha(p.read_bytes())}
               for p in sorted(pending.rglob('*')) if p.is_file()]
        for item in files:
            if _sha((pending/item['path']).read_bytes())!=item['sha256']:raise ValueError('lighting artifact changed before publication')
        manifest={'version':VERSION,'status':'source_layout_review','engineering_approval':False,
                  'canonical_model_created':False,'quantity_eligible':False,'shared_delivery':'company/delivery',
                  'source_snapshots':{key:'inputs/sources/'+key+'.bin' for key in sources},'files':files,'runtime':_runtime(),
                  'limitations':source_map['limitations']}
        (pending/'LIGHTING-MANIFEST.json').write_bytes(_json(manifest))
        os.rename(pending,output/'delivery');return output/'delivery'
    except Exception as exc:
        try:(output/'failure.json').write_bytes(_json({'stage':stage,'error_type':type(exc).__name__}))
        except OSError:pass
        raise ValueError(f'lighting sheet failed at {stage}: {exc}; attempt retained at {output}') from exc


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('request','profile','job','output_directory'):parser.add_argument(name)
    args=parser.parse_args(argv)
    try:result=create_lighting_rcp_sheet_set(args.request,args.profile,args.job,output_directory=args.output_directory)
    except (ValueError,OSError,ImportError) as exc:parser.exit(1,str(exc)+'\n')
    print(result);return 0


if __name__=='__main__':raise SystemExit(main())
