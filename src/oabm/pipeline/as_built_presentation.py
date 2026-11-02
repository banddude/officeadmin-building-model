"""Measured physical-sheet callouts for as-built review, never model edits."""
from __future__ import annotations

from dataclasses import replace
import math

from oabm.drawings import Bounds2, DrawingPrimitive, LineStyle, Point2, SheetSpec
from oabm.drawings.company_sheets import measure_sheet_text
from oabm.drawings.generator import _offset_point
from oabm.drawings.label_layout import _inflate, _overlap, _segment_hits_box, _segments_close
from oabm.drawings.schedule_layout import ScheduleLayout, wrap_cell

FONT = 'DejaVu Sans'
TEXT_MM = 3.0
SYMBOL_RADIUS_MM = 1.5
ROUTE_COLOR = '#0064a8'
ROUTE_STYLE = LineStyle(stroke='route', weight='heavy', pattern='dash')


def _primitive(identity, kind, source, points, *, text=None, layer='annotations:labels', style=None, metadata=()):
    return DrawingPrimitive(identity, kind, layer, (source,), tuple(points), text=text,
                            style=style or LineStyle(stroke='annotation', weight='thin'), metadata=metadata)


def _box(anchor, text, scale, *, centered=False):
    unit = scale / 1000
    width = measure_sheet_text(text, TEXT_MM, font_family=FONT) * unit
    height = TEXT_MM * unit
    left = anchor.x - (width / 2 if centered else 0)
    return (left - .15*height, anchor.y - .3*height, left + width + .15*height, anchor.y + height)


def _owners(model):
    result = {}
    for collection, prefix in (('electrical_equipment','E'), ('electrical_devices','D'),
            ('electrical_boxes','B'), ('routes','R'), ('walls','W'), ('openings','O'), ('spaces','S')):
        for owner in getattr(model, collection):
            result[owner.id] = (owner, 'P' if getattr(owner,'equipment_type',None)=='panelboard' else prefix)
    return result


def _layout(model, view, scale):
    """Use actual sheet font widths, millimetre symbols and dimension gaps.

    Only geometric collision helpers are shared with the SVG-view label module;
    its pixel text estimates, fixed symbol size and placement pass are not used.
    """
    unit, gap = scale / 1000, 1.25 * scale / 1000  # 1mm clear plus maximum half-stroke
    owners = _owners(model)
    originals = [p for p in view.primitives if p.kind=='text' and p.layer in {'annotations:labels','annotations:routes'}]
    original_ids = {p.id for p in originals}
    geometry = [replace(p,style=ROUTE_STYLE) if p.layer=='electrical:routes' else p
                for p in view.primitives if p.id not in original_ids]
    visible = {identity for p in geometry for identity in p.source_ids}
    texts = {}
    for label in originals:
        for identity in label.source_ids:
            texts.setdefault(identity,[]).append(label.text or '')
    for identity in visible:
        if identity in owners and owners[identity][1] in {'P','E','D','B','R'}:
            owner = owners[identity][0]
            texts.setdefault(identity,[owner.name or getattr(owner,'device_type',None) or
                getattr(owner,'box_type',None) or getattr(owner,'equipment_type',None) or getattr(owner,'route_type','')])
            if owners[identity][1]=='R' and f'{owner.route_type} route' not in texts[identity]:
                texts[identity].append(f'{owner.route_type} route')
    if len(texts)>128:
        raise ValueError('as-built presentation supports at most 128 callout owners per plan')
    if sum(len(text) for labels in texts.values() for text in labels)>32768 or any(
            len(text)>4096 for labels in texts.values() for text in labels):
        raise ValueError('as-built full-label text exceeds bounded legend capacity')
    counts, entries = {}, []
    for identity in sorted(texts):
        owner,prefix = owners.get(identity,(None,'X'))
        counts[prefix]=counts.get(prefix,0)+1
        tag=f'{prefix}{counts[prefix]}'
        entries.append({'owner_id':identity,'tag':tag,'labels':list(dict.fromkeys(texts[identity])),
            'original_labels':[{'id':p.id,'text':p.text,'source_ids':list(p.source_ids),
                                'anchor_m':[p.points[0].x,p.points[0].y]}
                               for p in originals if identity in p.source_ids]})

    # Put dimensions outside the geometry, with separate wall/opening rails.
    center=Point2((view.bounds.min_x+view.bounds.max_x)/2,(view.bounds.min_y+view.bounds.max_y)/2)
    dimensions=[]
    for dimension in view.dimensions:
        dx,dy=dimension.end.x-dimension.start.x,dimension.end.y-dimension.start.y
        length=math.hypot(dx,dy)
        if not length:
            raise ValueError('as-built dimension has no projected span')
        middle=Point2((dimension.start.x+dimension.end.x)/2,(dimension.start.y+dimension.end.y)/2)
        toward=(-dy*(center.x-middle.x)+dx*(center.y-middle.y))/length
        offset=(9 if owners.get(dimension.source_ids[0],(None,''))[1]=='W' else 5)*unit
        dimensions.append(replace(dimension,offset_m=-offset if toward>=0 else offset))

    boxes,segments=[],[]
    for p in geometry:
        if p.kind=='symbol':
            a=p.points[0];radius=SYMBOL_RADIUS_MM*unit
            boxes.append(((a.x-radius,a.y-radius,a.x+radius,a.y+radius),p.source_ids))
        elif p.kind=='text':
            boxes.append((_box(p.points[0],p.text or '',scale),p.source_ids))
        elif p.kind=='quarter_arc':
            boxes.append(((min(a.x for a in p.points),min(a.y for a in p.points),
                           max(a.x for a in p.points),max(a.y for a in p.points)),p.source_ids))
        else:
            points=p.points+((p.points[0],) if p.closed or p.kind=='polygon' else ())
            segments.extend((a,b,p.source_ids) for a,b in zip(points,points[1:]))
            if p.closed and p.layer in {'electrical:equipment','electrical:devices','electrical:boxes','architecture:openings'}:
                boxes.append(((min(a.x for a in points),min(a.y for a in points),
                               max(a.x for a in points),max(a.y for a in points)),p.source_ids))
    dimension_boxes=[]
    for d in dimensions:
        a,b=_offset_point(d.start,d.end,d.offset_m),_offset_point(d.end,d.start,-d.offset_m)
        anchor=Point2((a.x+b.x)/2,(a.y+b.y)/2+.8*unit)
        rectangle=_box(anchor,d.text,scale,centered=True)
        dimension_boxes.append(rectangle);boxes.append((rectangle,()))
        segments.extend(((a,b,d.source_ids),(d.start,a,d.source_ids),(d.end,b,d.source_ids)))
        for point in (a,b):
            radius=1.5*unit
            boxes.append(((point.x-radius,point.y-radius,point.x+radius,point.y+radius),()))

    # Reserve actual dimension text/arrow extents before placing callouts or
    # choosing paper. This expands presentation bounds, never source geometry.
    plot_bounds=Bounds2(min([view.bounds.min_x]+[b[0]-gap for b,_ in boxes]),
        min([view.bounds.min_y]+[b[1]-gap for b,_ in boxes]),
        max([view.bounds.max_x]+[b[2]+gap for b,_ in boxes]),
        max([view.bounds.max_y]+[b[3]+gap for b,_ in boxes]))

    placed,occupied,unplaced=[],[],[]
    for entry in entries:
        identity,tag=entry['owner_id'],entry['tag']
        owner=owners.get(identity,(None,''))[0]
        linked={identity}
        if getattr(owner,'host_id',None):
            linked.add(owner.host_id)
        linked.update(getattr(owner,'occupant_ids',()))
        linked.update(box.id for box in model.electrical_boxes if identity in box.occupant_ids)
        symbols=[p for p in geometry if p.kind=='symbol' and identity in p.source_ids]
        lines=[(a,b) for a,b,ids in segments if identity in ids]
        arcs=[p for p in geometry if p.kind=='quarter_arc' and identity in p.source_ids]
        label=next((p for p in originals if identity in p.source_ids),None)
        if symbols:
            origin=symbols[0].points[0]
        elif lines:
            a,b=max(lines,key=lambda pair:math.hypot(pair[1].x-pair[0].x,pair[1].y-pair[0].y))
            origin=Point2((a.x+b.x)/2,(a.y+b.y)/2)
        elif arcs:
            origin=arcs[0].points[0]  # actual canonical arc endpoint, never a radial chord
        elif label:
            origin=label.points[0]
        else:
            unplaced.append(identity);entry.update(callout_status='legend_only',reason='no_source_anchor');continue
        selected=None
        width=measure_sheet_text(tag,TEXT_MM,font_family=FONT)*unit
        for radius_mm in (4,7,10,14,20,28):
            radius=radius_mm*unit
            for dx,dy in ((radius,radius),(-radius-width,radius),(radius,-radius),(-radius-width,-radius),
                          (-width/2,radius),(-width/2,-radius)):
                anchor=Point2(origin.x+dx,origin.y+dy)
                rectangle=_box(anchor,tag,scale)
                if rectangle[0]<plot_bounds.min_x or rectangle[1]<plot_bounds.min_y or rectangle[2]>plot_bounds.max_x or rectangle[3]>plot_bounds.max_y:
                    continue
                expanded=_inflate(rectangle,gap)
                if any(_overlap(expanded,b) for b,_ in boxes) or any(_overlap(expanded,b) for b in occupied):
                    continue
                if any(_segment_hits_box(a,b,expanded) for a,b,_ in segments):
                    continue
                end=Point2(max(rectangle[0],min(origin.x,rectangle[2])),max(rectangle[1],min(origin.y,rectangle[3])))
                distance=math.hypot(end.x-origin.x,end.y-origin.y)
                trim=SYMBOL_RADIUS_MM*unit if symbols else 0
                start=Point2(origin.x+(end.x-origin.x)*min(1,trim/distance),
                             origin.y+(end.y-origin.y)*min(1,trim/distance)) if distance else origin
                if any(not linked.intersection(ids) and _segment_hits_box(start,end,_inflate(b,gap)) for b,ids in boxes):
                    continue
                if any(_segment_hits_box(start,end,_inflate(b,gap)) for b in occupied):
                    continue
                if any(not linked.intersection(ids) and _segments_close(start,end,a,b,gap) for a,b,ids in segments):
                    continue
                selected=(anchor,rectangle,start,end);break
            if selected:
                break
        if selected is None:
            unplaced.append(identity);entry.update(callout_status='legend_only',reason='no_clear_finite_candidate');continue
        anchor,rectangle,start,end=selected
        entry.update(callout_status='placed',text_box_m=list(rectangle),anchor_m=[origin.x,origin.y])
        occupied.append(rectangle)
        style=LineStyle(stroke='route' if owners.get(identity,(None,''))[1]=='R' else 'annotation',weight='thin')
        placed.append(_primitive(f'as-built-tag:{identity}','text',identity,(anchor,),text=tag,style=style))
        if start!=end:
            placed.append(_primitive(f'as-built-leader:{identity}','line',identity,(start,end),style=style,
                layer='annotations:callout-leaders',metadata=(('owner_id',identity),)))
            segments.append((start,end,(identity,)))

    # The measured margin legend retains every full label and canonical owner,
    # including any callout for which no collision-free candidate was found.
    legend_width=84.0
    layout=ScheduleLayout(text_height_mm=TEXT_MM,line_height_mm=4.5,padding_mm=1)
    legend_x=plot_bounds.max_x+8*unit
    y=plot_bounds.max_y-4*unit
    level_id=dict(view.metadata)['level_id']
    level=next(level for level in model.levels if level.id==level_id)
    source=level.id
    legend=[]
    identity_lines=[]
    for value in (view.title, f'Level ID: {level.id}', f'Elevation: {level.elevation_m:g} m'):
        identity_lines.extend(wrap_cell(value,legend_width,layout))
    if len(identity_lines)>64:
        raise ValueError('as-built level identity exceeds bounded measured heading capacity')
    for line in identity_lines:
        legend.append(_primitive(f'{view.id}:level-heading:{len(legend)}','text',source,
            (Point2(legend_x,y),),text=line))
        y-=4.5*unit
    y-=3*unit
    for text in ('CALLOUTS / SOURCE IDS','Blue dashed: supplied route','Location cross: not product CAD'):
        legend.append(_primitive(f'{view.id}:legend-header:{len(legend)}','text',source,(Point2(legend_x,y),),text=text))
        y-=5*unit
    route_source=next((entry['owner_id'] for entry in entries if owners.get(entry['owner_id'],(None,''))[1]=='R'),None)
    if route_source is not None:
        legend.append(_primitive(f'{view.id}:route-key','line',route_source,
            (Point2(legend_x,y),Point2(legend_x+18*unit,y)),style=ROUTE_STYLE))
    y-=6*unit
    for entry in entries:
        text=f'{entry["tag"]} | {"; ".join(entry["labels"])} | {entry["owner_id"]}'
        if entry['callout_status']=='legend_only':
            text+=' | callout needs review'
        entry['legend_lines']=list(wrap_cell(text,legend_width,layout))
        for line in entry['legend_lines']:
            legend.append(_primitive(f'{view.id}:legend:{len(legend)}','text',entry['owner_id'],
                (Point2(legend_x,y),),text=line))
            y-=4.5*unit
        y-=2*unit
    if sum(len(entry['legend_lines']) for entry in entries)>256:
        raise ValueError('as-built legend exceeds 256 measured lines')
    bounds=Bounds2(plot_bounds.min_x,min(plot_bounds.min_y,y-2*unit),
                   legend_x+legend_width*unit,plot_bounds.max_y)
    return replace(view,scale=scale,bounds=bounds,primitives=tuple(geometry+placed+legend),dimensions=tuple(dimensions)), {
        'text_height_mm':TEXT_MM,'font_family':FONT,'symbol_radius_mm':SYMBOL_RADIUS_MM,
        'level_id':level.id,'level_elevation_m':level.elevation_m,'view_title':view.title,
        'printed_level_heading_lines':identity_lines,
        'entries':entries,'unplaced_callout_owner_ids':unplaced,
        'dimension_text_boxes_m':[list(box) for box in dimension_boxes],
        'geometry_unchanged':True,'review_status':'needs_review' if unplaced else 'unreviewed'}


def prepare_plan(model, view, ordinal, revision):
    """Choose from finite standard paper/scale candidates; never rescale geometry."""
    candidates=((25,((297,210),(420,297))), (50,((297,210),(420,297),(594,420))),
                (100,((420,297),(594,420),(841,594),(1189,841))))
    for scale,papers in candidates:
        projected,audit=_layout(model,view,scale)
        for width,height in papers:
            if projected.bounds.width*1000/scale+24<=width-10 and projected.bounds.height*1000/scale+24<=height-40:
                sheet=SheetSpec(f'AB-{ordinal}',width,height,titleblock_height_mm=30,
                    title='As-built field review plan',revision=revision,titleblock_fields=(('Scale',f'1:{scale}'),))
                audit.update(scale=scale,paper_mm=[width,height])
                return projected,sheet,audit
    raise ValueError('as-built plan exceeds finite supported paper/scale candidates')
