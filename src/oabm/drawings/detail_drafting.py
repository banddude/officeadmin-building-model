"""Technical detail presentation of explicit projected canonical facts only."""
from __future__ import annotations
import html
import math
import re
from .company_sheets import measure_sheet_text, validate_sheet_text


def render_drafted_detail(detail, *, drawing_bounds_mm, audit=None):
    left,top,right,bottom=drawing_bounds_mm
    if not all(math.isfinite(v) for v in drawing_bounds_mm) or left>=right or top>=bottom:
        raise ValueError('invalid detail slot bounds')
    spec,view=detail.spec,detail.view
    ppu=1000/view.scale
    width,height=view.bounds.width*ppu,view.bounds.height*ppu
    gx,gy=left+5,top+25
    if gx+width>right-5:
        raise ValueError('detail geometry exceeds slot at supplied scale; enlarge slot')
    if measure_sheet_text(spec.number,3.5,font_weight=700)>6:
        raise ValueError('detail number exceeds circled heading; supply a shorter number')
    out=['<g>']; boxes=[]; labels=[]; leader_bindings={}; dimension_boxes=[]; dimension_witnesses=[]
    def point(p):return gx+(p.x-view.bounds.min_x)*ppu,gy+(view.bounds.max_y-p.y)*ppu
    def check_box(box):
        if box[0]<left+.1 or box[1]<top+.1 or box[2]>right-.1 or box[3]>bottom-.1:
            raise ValueError('drafted detail exceeds slot bounds; enlarge slot or revise explicit labels')
    def text(x,y,value,size=2.8,anchor='start',weight=400):
        validate_sheet_text(value)
        w=measure_sheet_text(value,size,font_weight=weight)
        sx=x-w*{'start':0,'middle':.5,'end':1}[anchor]
        box=(sx-.1,y-size-.1,sx+w+.1,y+size*.3+.1)
        check_box(box)
        if any(box[0]<b[2]+.5 and b[0]<box[2]+.5 and box[1]<b[3]+.5 and b[1]<box[3]+.5 for b in boxes):
            raise ValueError(f'detail label overlap: {value!r}; revise explicit callout positions')
        boxes.append(box)
        labels.append((value,box))
        out.append(f'<text x="{x:g}" y="{y:g}" font-family="Liberation Sans" font-size="{size:g}" font-weight="{weight}" text-anchor="{anchor}" fill="#000000">{html.escape(value)}</text>')
    def line(points,weight=.18,closed=False):
        for x,y in points:check_box((x-weight/2,y-weight/2,x+weight/2,y+weight/2))
        coords=' '.join(f'{x:g},{y:g}' for x,y in points)
        tag='polygon' if closed else 'polyline'
        out.append(f'<{tag} points="{coords}" fill="none" stroke="#000000" stroke-width="{weight:g}"/>')
    # Circled numbered heading and separate scale, with no copied title block.
    out.append(f'<circle cx="{left+5:g}" cy="{top+7:g}" r="4" fill="none" stroke="#000000" stroke-width="0.35"/>')
    text(left+5,top+8.4,spec.number,3.5,'middle',700)
    text(left+12,top+8.5,spec.title,3.8,weight=700)
    line(((left+1,top+13),(right-1,top+13)),.3)
    text(left+12,top+18.5,f'SCALE 1:{view.scale:g}  |  DIMENSIONS IN METRES',2.5)
    source_points={}; source_segments={}; port_sources=set()
    for primitive in view.primitives:
        pts=tuple(map(point,primitive.points))
        for sid in primitive.source_ids:source_points.setdefault(sid,[]).extend(pts)
        if primitive.kind=='symbol' and primitive.symbol=='supplied_connection_location':
            port_sources.update(primitive.source_ids)
            x,y=pts[0];check_box((x-1.2,y-1.2,x+1.2,y+1.2))
            out.append(f'<circle cx="{x:g}" cy="{y:g}" r="1.2" fill="none" stroke="#000000" stroke-width="0.25"/>')
            line(((x-2,y),(x+2,y)),.13);line(((x,y-2),(x,y+2)),.13)
        elif primitive.kind in {'line','polyline','polygon'}:
            weight=.6 if primitive.style.weight=='heavy' or primitive.style.stroke=='cut' else (.4 if primitive.layer=='electrical:routes' else .25)
            line(pts,weight,primitive.closed or primitive.kind=='polygon')
            edges=list(zip(pts,pts[1:]))
            if primitive.closed or primitive.kind=='polygon':edges.append((pts[-1],pts[0]))
            for sid in primitive.source_ids:source_segments.setdefault(sid,[]).extend(edges)
        else:raise ValueError('unsupported drafted detail primitive')
    def closest_on_segment(p,a,b):
        dx,dy=b[0]-a[0],b[1]-a[1]
        t=0 if dx*dx+dy*dy==0 else max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy)))
        return a[0]+t*dx,a[1]+t*dy
    def distance(a,b):return (a[0]-b[0])**2+(a[1]-b[1])**2
    leader_paths=[]
    def crosses(a,b,c,d):
        # Reject actual crossings, including an endpoint in another leader's
        # interior. Shared source endpoints may intentionally coincide.
        if a in (c,d) or b in (c,d):return False
        def orient(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
        def on(p,q,r):return min(p[0],r[0])-1e-8<=q[0]<=max(p[0],r[0])+1e-8 and min(p[1],r[1])-1e-8<=q[1]<=max(p[1],r[1])+1e-8
        o1,o2,o3,o4=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        return (o1*o2<0 and o3*o4<0) or any(abs(o)<1e-8 and on(p,q,r) for o,p,q,r in ((o1,a,c,b),(o2,a,d,b),(o3,c,a,d),(o4,c,b,d)))
    # Local elbow leaders terminate on actual projected edges, not object
    # centres. A port remains its supplied point. No source geometry changes.
    for callout in sorted(spec.callouts,key=lambda c:c.source_id):
        x,y=left+callout.x_mm,top+callout.y_mm
        label=f'{callout.source_id}: {callout.text}'
        text(x,y,label,anchor=callout.anchor)
        occupied=measure_sheet_text(label,2.8)
        tx0=x-occupied if callout.anchor=='end' else x;tx1=tx0+occupied
        target=((tx0+tx1)/2,y-1.4)
        candidates=[closest_on_segment(target,a,b) for a,b in source_segments.get(callout.source_id,())]
        if not candidates:candidates=source_points[callout.source_id]
        end=min(candidates,key=lambda p:(distance(p,target),p))
        pts=source_points[callout.source_id]
        x0,x1=min(p[0] for p in pts),max(p[0] for p in pts)
        y0,y1=min(p[1] for p in pts),max(p[1] for p in pts)
        if y<y0:
            start=(max(tx0,min(tx1,end[0])),y+1.8)
            ax=end[0]+(4 if start[0]>end[0] else -4) if callout.source_id in port_sources else end[0]
            approach=(ax,end[1]-4)
            path=(start,(ax,start[1]),approach,end)
        elif y-2.8>y1:
            start=(max(tx0,min(tx1,end[0])),y-3.8)
            ax=end[0]+(4 if start[0]>end[0] else -4) if callout.source_id in port_sources else end[0]
            approach=(ax,end[1]+4)
            path=(start,(ax,start[1]),approach,end)
        else:
            right_side=tx0>x1
            start=(tx0 if right_side else tx1,y+1.8)
            ax=end[0]+(4 if right_side else -4)
            path=(start,(ax,start[1]),(ax,end[1]),end)
        path=tuple(p for i,p in enumerate(path) if i==0 or p!=path[i-1])
        if any(crosses(a,b,c,d) for a,b in zip(path,path[1:]) for old in leader_paths for c,d in zip(old,old[1:])):
            raise ValueError(f'detail leader crossing for {callout.source_id}; revise explicit local callout positions')
        leader_paths.append(path)
        leader_bindings[callout.source_id]=path
        line(path,.13)
    dimension_lanes={d.source_id:d for d in spec.dimension_lanes}
    for sid in sorted(spec.dimension_source_ids):
        pts=source_points[sid];x0,x1=min(p[0] for p in pts),max(p[0] for p in pts)
        y0,y1=min(p[1] for p in pts),max(p[1] for p in pts)
        if x1-x0<.01 or y1-y0<.01:raise ValueError('dimension source has no supported projected width/height')
        line(((x0,y0-2),(x0,y0-7),(x1,y0-7),(x1,y0-2)),.13)
        text((x0+x1)/2,y0-9,f'{(x1-x0)/ppu:g} m',anchor='middle')
        lane=dimension_lanes.get(sid)
        side=lane.vertical_side if lane is not None else 'right'
        gap=lane.gap_mm if lane is not None else 7
        edge=x0 if side=='left' else x1
        sign=-1 if side=='left' else 1
        lane_x=edge+sign*gap
        witness=((edge+sign*2,y0),(lane_x,y0),(lane_x,y1),(edge+sign*2,y1))
        line(witness,.13)
        text(lane_x+sign*2,(y0+y1)/2,f'{(y1-y0)/ppu:g} m',anchor='end' if side=='left' else 'start')
        dimension_boxes.append((sid,boxes[-1]))
        dimension_witnesses.append((sid,side,witness))
    y=gy+height+8 if spec.notes_y_mm is None else top+spec.notes_y_mm
    if y<gy+height+8:
        raise ValueError('notes block overlaps the explicit figure canvas; revise notes_y_mm')
    line(((left+2,y-5),(right-2,y-5)),.25)
    text(left+2,y,'DETAIL NOTES / EXPLICIT LIMITS',3.1,weight=700)
    y+=7
    # Word wrap preserves every supplied character. Long words fail rather
    # than changing fonts, hiding notes, or silently reducing view scale.
    paragraphs=(f'MOUNTING: {spec.mounting_spec}',f'CONNECTION: {spec.connection_spec}',*spec.notes,
                'PORT MARKERS SHOW SUPPLIED LOCATIONS, NOT TERMINAL HARDWARE.',
                'SUPPORTS ARE EXTERIOR ENVELOPES. NO ENGINEERING APPROVAL.')
    for paragraph in paragraphs:
        validate_sheet_text(paragraph)
        current=''
        for token in re.findall(r'\s+|\S+',paragraph):
            if measure_sheet_text(token,2.8)>right-left-4:
                raise ValueError('detail note token exceeds slot width; supply a larger slot')
            if current and measure_sheet_text(current+token,2.8)>right-left-4:
                text(left+2,y,current);y+=4.8;current=''
            current+=token
        if current:text(left+2,y,current);y+=4.8
        y+=1.8
    if audit is not None:
        audit.update(leaders=leader_bindings,dimension_text_boxes=dimension_boxes,dimension_witnesses=dimension_witnesses,source_points=source_points,labels=labels,note_baseline_mm=(gy+height+8 if spec.notes_y_mm is None else top+spec.notes_y_mm))
    out.append('</g>')
    return '\n'.join(out)
