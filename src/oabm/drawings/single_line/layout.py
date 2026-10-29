"""Topology-aware compact schematic pagination, in paper millimetres."""
from __future__ import annotations
import math
import re
import heapq
import json
from functools import lru_cache
from .topology import Topology
from ..company_sheets import measure_sheet_text, validate_sheet_text

FONT='DejaVu Sans'
TEXT_MM=3.2
SMALL_TEXT_MM=2.8
TEXT_LINE_MM=4.3


def wrap_mm(value, width_mm, *, size=TEXT_MM):
    """Measured wrapping that preserves every supplied character, including spaces."""
    validate_sheet_text(value,font_family=FONT)
    if width_mm<=0:raise ValueError('single-line text cell has no width')
    if value=='':return ['']
    result=[]
    remaining=value
    while remaining:
        low,high=0,len(remaining)
        while low<high:
            middle=(low+high+1)//2
            if measure_sheet_text(remaining[:middle],size,font_family=FONT)<=width_mm:low=middle
            else:high=middle-1
        if not low:raise ValueError('single-line text cell cannot fit supplied glyph')
        end=low
        if end<len(remaining):
            space=remaining.rfind(' ',0,end)
            if space>=0:end=space+1
        result.append(remaining[:end])
        remaining=remaining[end:]
    return result


def wrap(value,width):
    """Compatibility character-capacity wrapper; actual layout uses paper widths."""
    return wrap_mm(value,width*1.75)


def symbol_lane(bounds, *, wide=False):
    """Reserve a measured label column before choosing electrical terminals."""
    l,t,r,b=bounds;w=r-l
    text_width=min(80,w-42)
    if text_width<20:raise ValueError('equipment side lane needs at least 62 mm width')
    text_left=l if wide else r-18-22-text_width
    symbol_left=l+text_width+8 if wide else r-32
    symbol_right=r-2 if wide else r-4
    return {'text_left':text_left,'text_width':text_width,'symbol_left':symbol_left,
            'symbol_right':symbol_right,'axis_x':symbol_left+14 if wide else r-18,'wide':wide}


def _core(topology, ids, bounds):
    left,top,right,bottom=bounds
    selected=[e for e in topology.equipment if e['id'] in ids]
    edges=[e for e in topology.connections if e['source_id'] in ids and e['load_id'] in ids]
    ranks={e['id']:0 for e in selected}
    for equipment in selected:
        parents=[e['source_id'] for e in edges if e['load_id']==equipment['id']]
        if parents:ranks[equipment['id']]=max(ranks[p] for p in parents)+1
    maximum=max(ranks.values(),default=0)
    maximum_row=max((list(ranks.values()).count(r) for r in ranks.values()),default=1)
    node_width=min(190,right-left-30) if maximum_row==1 else min(150,(right-left)/(maximum_row+1)-10)
    if node_width<64 or bottom-top<(maximum+1)*54+16:
        return None
    nodes=[]
    pitch=min(68 if any(edge.get('drafting',{}).get('protection',{} ) and edge['drafting']['protection']['kind']=='fused_disconnect' for edge in edges) else 56,(bottom-top-60)/max(1,maximum))
    for rank in range(maximum+1):
        row=[e for e in selected if ranks[e['id']]==rank]
        def terminal_order(e):
            kinds={edge['drafting']['connection_kind'] for edge in edges if edge['load_id']==e['id']}
            return (0 if kinds=={'neutral'} else 2 if kinds=={'grounding'} else 1,e['id'])
        row.sort(key=terminal_order)
        for i,e in enumerate(row):
            xx=left+(right-left)*(i+1)/(len(row)+1)
            parents=[edge['source_id'] for edge in edges if edge['load_id']==e['id']]
            outgoing=sum(edge['source_id']==e['id'] for edge in topology.connections)
            bus_width=min(right-left-30,max(node_width,80*outgoing+20)) if len(row)==1 and e['equipment_type'] in {'panelboard','switchboard','transfer_switch'} else node_width
            if len(row)==1 and len(parents)==1 and outgoing<=1:
                parent=next((node for node in nodes if node['equipment_id']==parents[0]),None)
                if parent is not None:
                    lane=symbol_lane((0,0,bus_width,40))
                    xx=parent['axis_x']-lane['axis_x']+bus_width/2
            nodes.append({'equipment_id':e['id'],'x':xx,
                          'y':top+12+rank*pitch,'width':bus_width,'height':40,'equipment_type':e['equipment_type'],'network_kinds':sorted({edge['drafting']['connection_kind'] for edge in topology.connections if e['id'] in {edge['source_id'],edge['load_id']}}),
                          'drafting_lane':'side-wide' if outgoing>1 and e['equipment_type'] in {'panelboard','switchboard','transfer_switch'} else 'side',
                          'axis_x':symbol_lane((xx-bus_width/2,top+12+rank*pitch,xx+bus_width/2,top+52+rank*pitch),wide=outgoing>1 and e['equipment_type'] in {'panelboard','switchboard','transfer_switch'})['axis_x']})
    by_id={e['id']:e for e in selected}
    for node in nodes:
        lane=symbol_lane((0,0,node['width'],node['height']),wide=node['drafting_lane']=='side-wide')
        limit=3 if by_id[node['equipment_id']]['equipment_type'] in {'panelboard','switchboard','disconnect','transfer_switch'} else 4
        if len(wrap_mm(by_id[node['equipment_id']]['name'],lane['text_width']))>limit:
            return None
    return {'nodes':nodes,'connections':[e['id'] for e in edges],'core_bounds':bounds}


def _facts(topology,tags):
    rows=[]
    for edge in topology.connections:
        voltage='V UNKNOWN' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
        values=[f'{tags[edge["id"]]} | Circuit {edge["circuit_id"]} | FROM {edge["source_id"]} / {edge["source_port_id"]} ({edge["source_role"]}) -> {edge["load_id"]} / {edge["load_port_id"]} ({edge["load_role"]})',
                f'{voltage}; poles {edge["poles"] if edge["poles"] is not None else "UNKNOWN"}; phase {edge["phase"] or "UNKNOWN"}; Conductors: {edge["conductor_specification"]}; Routes: '+(', '.join(edge['route_ids']) or 'UNKNOWN / not supplied')]
        values.extend(edge['drafting']['notes'])
        rows.append({'connection_id':edge['id'],'values':values})
    for equipment in topology.equipment:
        if equipment['supplied_notes']:
            rows.append({'equipment_id':equipment['id'],
                         'values':[f'Equipment {equipment["id"]} / supplied specifications',*equipment['supplied_notes']]})
    notes=['SCHEMATIC - NOT TO SCALE. Supplied facts only; no engineering approval.',
           'C-tags identify explicit directed circuit/load pairs. No unmarked line crossings are junctions.',
           'CONTINUED equipment references the same canonical object; full IDs and supplied specifications appear in this register.',
           'Transfer state is unspecified; normal/alternate inputs do not assert source paralleling.',
           'Transformer winding voltages are connection labels. Missing grounding/protection/sizes are not designed.',
           *topology.diagnostics]
    rows.append({'note_id':'notes','values':notes})
    return rows


def _place_facts(rows,bounds):
    left,top,right,bottom=bounds
    output=[]
    y=top
    for i,row in enumerate(rows):
        lines=[line for value in row['values'] for line in wrap_mm(value,right-left-6)]
        height=len(lines)*TEXT_LINE_MM+4
        if y+height>bottom:return output,rows[i:]
        output.append({**row,'x':left+3,'y':y,'width':right-left-6,'lines':lines,'height':height})
        y+=height
    return output,[]


def _plan_topology_pages(topology: Topology,drawing_bounds_mm, *, diagram_only=False, first_sheet_id='E601', reserved_sheet_ids=()):
    """Honor the caller's base ID, retaining numeric prefix and zero padding.

    Numeric suffixes increment (SL009 -> SL010); otherwise continuations append
    -2, -3, etc. Reserved-ID collisions fail rather than silently renumbering.
    """
    def valid_id(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',value):
            raise ValueError('single-line sheet ID requires 1-64 ASCII letters/digits/periods/underscores/hyphens')
        return value
    valid_id(first_sheet_id)
    if isinstance(reserved_sheet_ids,str):
        raise ValueError('reserved_sheet_ids must be a sequence of sheet IDs, not one string')
    reserved=[valid_id(value).casefold() for value in reserved_sheet_ids]
    if len(set(reserved))!=len(reserved):
        raise ValueError('reserved sheet IDs must be unique ignoring case')
    numeric=re.fullmatch(r'(.*?)([0-9]+)',first_sheet_id)
    def sheet_id(ordinal):
        if ordinal==0:identity=first_sheet_id
        elif numeric:
            prefix,digits=numeric.groups()
            identity=prefix+str(int(digits)+ordinal).zfill(len(digits))
        else:identity=first_sheet_id+'-'+str(ordinal+1)
        valid_id(identity)
        if identity.casefold() in reserved:
            raise ValueError(f'single-line sheet ID {identity!r} collides with a reserved sheet; supply a different base ID')
        return identity
    left,top,right,bottom=drawing_bounds_mm
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in drawing_bounds_mm):
        raise ValueError('drawing bounds must be finite millimetres')
    w,h=right-left,bottom-top
    if left<0 or top<0 or w<210 or h<168:
        raise ValueError('single-line requires at least 210 x 168 mm drawing bounds')
    pages=[]
    tags={e['id']:f'C{i:03d}' for i,e in enumerate(topology.connections,1)}
    all_ids={e['id'] for e in topology.equipment}
    core_ids=all_ids if len(all_ids)<=8 else {e['id'] for e in topology.equipment if e['equipment_type']!='panelboard'}
    small=len(all_ids)<=8 and w>=300 and h>=250
    core_width=w*(1 if diagram_only and small else .53) if small else w*.34
    edges={e['id']:e for e in topology.connections}
    core=None
    if core_ids and len(core_ids)<=8:
        for fraction in (((1.0,) if diagram_only else (.53,)) if small else (.34,.42,.50,.58)):
            trial_width=w*fraction
            if not small and w-trial_width<210:continue
            candidate=_core(topology,core_ids,(left+8,top+28,left+trial_width-8,bottom-20))
            if candidate is None:continue
            try:backbone_geometry(candidate,edges,candidate['core_bounds'])
            except ValueError:continue
            core=candidate;core_width=trial_width;break
    if small and core:
        pages.append({'id':sheet_id(0),'kind':'single-line',**core,'groups':[],
                      'facts_bounds':None if diagram_only else (left+core_width+5,top+28,right-3,bottom-17)})
    elif core and len(core_ids)<=8:
        pages.append({'id':sheet_id(0),'kind':'distribution',**core,'groups':[]})
    covered=set(pages[0]['connections']) if pages else set()
    # Shelf-pack compact branch groups. Degree-one runs become connected chain
    # strips instead of paying a full parent/child fan row for every feeder.
    page=pages[0] if pages and not small else None
    group_left=left+core_width+7 if page else left+3
    group_top=top+27;group_bottom=bottom-17
    if page and diagram_only:
        branch_owners={edges[key]['source_id'] for key in edges if key not in covered}
        source_rows=[node['y']-3 for node in page['nodes'] if node['equipment_id'] in branch_owners]
        if source_rows:group_top=max(group_top,min(source_rows))
    x=group_left;y=group_top;row_height=0
    rendered_core_ids={n['equipment_id'] for p in pages for n in p['nodes']}
    equipment_by_id={e['id']:e for e in topology.equipment}
    for equipment in topology.equipment:
        isolated=not equipment['incoming'] and not equipment['outgoing'] and equipment['id'] not in rendered_core_ids
        while True:
            remaining=[key for key in equipment['outgoing'] if key not in covered]
            if diagram_only and remaining:
                terminal_signature=_bus_key(edges[remaining[0]])
                remaining=[key for key in remaining if _bus_key(edges[key])==terminal_signature]
            if not remaining and not isolated:break
            if page is None:
                group_top=top+27
                page={'id':sheet_id(len(pages)),'kind':'distribution-continuation','nodes':[],'connections':[],'groups':[]}
                pages.append(page);group_left=left+3;x=group_left;y=group_top;row_height=0
            available=right-group_left-3
            kind='fan';height=120 if diagram_only else 148
            # Split a short-sheet fan into strips; never retry it unchanged.
            if remaining and ((len(remaining)==1 and not (diagram_only and edges[remaining[0]]['drafting']['connection_kind']!='power')) or height>group_bottom-group_top):
                capacity=max(2,int((available+34)//134))
                selected=[];current=remaining[0]
                while len(selected)<capacity-1:
                    selected.append(current)
                    target=equipment_by_id[edges[current]['load_id']]
                    following=[key for key in target['outgoing'] if key not in covered and key not in selected]
                    if len(target['incoming'])!=1 or len(following)!=1:break
                    current=following[0]
                width=min(available,(len(selected)+1)*100+len(selected)*34)
                kind='chain';height=66
            elif remaining:
                width=min(available,max(260,108+len(remaining)*78))
                selected=remaining[:max(1,int((width-104)//78))]
            else:
                width=min(available,160);selected=[];height=56
            if height>group_bottom-group_top:
                raise ValueError(f'{equipment["id"]}: minimum {kind} group needs {height:g} mm drawing height after header/footer; only {group_bottom-group_top:g} mm available; select taller paper')
            if x+width>right-3+1e-8:
                x=group_left;y+=row_height;row_height=0
            if y+height>group_bottom:
                if not page['groups']:
                    raise ValueError(f'{equipment["id"]}: {kind} group cannot fit a fresh sheet; select taller paper')
                page=None;continue
            page['groups'].append({'kind':kind,'equipment_id':equipment['id'],'connections':selected,
                                   'x':x,'y':y,'width':width,'height':height})
            covered.update(selected)
            x+=width+5;row_height=max(row_height,height)
            isolated=False
    # Equipment already in a small core needs no redundant terminal-detail grid.
    if not pages:
        raise ValueError('single-line cannot fit equipment topology in these drawing bounds')
    facts=[] if diagram_only else _facts(topology,tags)
    if small and pages[0].get('facts_bounds'):
        placed,facts=_place_facts(facts,pages[0]['facts_bounds'])
        pages[0]['entries']=placed
    if facts and pages[-1]['groups']:
        # Use the unused lower band of the last branch sheet if the COMPLETE
        # register fits. Do not scatter a partial table among unrelated pages.
        start=max(g['y']+g['height'] for g in pages[-1]['groups'])+7
        remaining=facts
        placed=[]
        for col in range(2):
            entries,remaining=_place_facts(remaining,(left+col*w/2+2,start,left+(col+1)*w/2-4,bottom-17))
            placed.extend(entries)
        if not remaining:
            pages[-1]['entries']=placed
            facts=[]
    # At most two measured, full-height register columns. Notes share these
    # columns; no compulsory notes, index, or duplicate detail sheets exist.
    while facts:
        page={'id':sheet_id(len(pages)),'kind':'facts-register','nodes':[],'connections':[],'groups':[],'entries':[]}
        pages.append(page)
        column_width=w/2-4
        before=len(facts)
        for col in range(2):
            placed,facts=_place_facts(facts,(left+col*(w/2)+2,top+28,left+col*(w/2)+column_width,bottom-17))
            page['entries'].extend(placed)
        if len(facts)==before:
            raise ValueError(f'{facts[0].get("connection_id",facts[0].get("equipment_id","notes"))}: supplied facts cannot fit a register column; select larger paper or shorten explicit text')
    homes={};occurrences=[];locations={};register={}
    for page in pages:
        ids=[n['equipment_id'] for n in page['nodes']]
        for group in page['groups']:
            ids.append(group['equipment_id'])
            ids.extend(edges[key]['load_id'] for key in group['connections'])
        for key in ids:
            homes.setdefault(key,page['id'])
            occurrences.append({'equipment_id':key,'sheet_id':page['id']})
        shown=page['connections']+[key for g in page['groups'] for key in g['connections']]
        for key in shown:
            if key in locations:raise ValueError(f'{key}: connection duplicated in overview')
            locations[key]=page['id']
        for row in page.get('entries',[]):
            if 'connection_id' in row:register[row['connection_id']]=page['id']
    if set(homes)!=all_ids or set(locations)!=set(edges) or (not diagram_only and set(register)!=set(edges)):
        raise ValueError('single-line pagination omitted equipment, connection or supplied register facts')
    refs=[{'connection_id':e['id'],'tag':tags[e['id']],
           'source_sheet':locations[e['id']],'load_sheet':locations[e['id']],
           'register_sheet':register.get(e['id'],locations[e['id']])} for e in topology.connections]
    return {'coordinate_system':'paper-mm,top-left,+x-right,+y-down','scale':'not-to-scale',
            'first_sheet_id':first_sheet_id,'pages':pages,'equipment_home_sheets':homes,'equipment_occurrences':occurrences,
            'sheet_index':[{'id':p['id'],'kind':p['kind']} for p in pages],'connections':refs}


def _crosses_box(a,b,box):
    lo,hi=0.0,1.0
    for start,end,low,high in ((a[0],b[0],box[0],box[2]),(a[1],b[1],box[1],box[3])):
        delta=end-start
        if abs(delta)<1e-10:
            if start<low or start>high:return False
        else:
            one,two=(low-start)/delta,(high-start)/delta
            lo=max(lo,min(one,two));hi=min(hi,max(one,two))
            if lo>hi:return False
    return True


def _segments_cross(a,b,c,d):
    def orient(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    def on(p,q,r):return min(p[0],q[0])-1e-8<=r[0]<=max(p[0],q[0])+1e-8 and min(p[1],q[1])-1e-8<=r[1]<=max(p[1],q[1])+1e-8
    one,two,three,four=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
    if one*two<0 and three*four<0:return True
    return any(abs(value)<1e-8 and on(p,q,r) for value,p,q,r in ((one,a,b,c),(two,a,b,d),(three,c,d,a),(four,c,d,b)))


def _clear_segment(a,b,boxes,segments=()):
    return not any(_crosses_box(a,b,box) for box in boxes) and not any(_segments_cross(a,b,c,d) for c,d in segments)


def _label_location(points,edge,bounds,boxes,segments,labels):
    voltage='V UNKNOWN' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
    label_lines=edge.get('drawing_labels')
    width=max([measure_sheet_text(line,TEXT_MM,font_family=FONT) for line in label_lines] or [0])+1 if label_lines else max(measure_sheet_text('C000 / '+voltage,TEXT_MM,font_family=FONT),
              measure_sheet_text(edge['load_role'],SMALL_TEXT_MM,font_family=FONT))+1
    candidates=[]
    for a,b in sorted(zip(points,points[1:]),key=lambda pair:-math.dist(*pair)):
        for portion in (.5,.25,.75):
            x=a[0]+portion*(b[0]-a[0]);y=a[1]+portion*(b[1]-a[1])
            if abs(a[0]-b[0])<1e-8:candidates.extend(((x+3,y),(x-width-3,y)))
            else:candidates.extend(((x-width/2,y-7),(x-width/2,y+5)))
    for x,y in candidates:
        box=(x-.1,y-TEXT_MM-.1,x+width,y+(len(label_lines)-1)*TEXT_LINE_MM+TEXT_MM*.3 if label_lines else y+5.1)
        if box[0]<bounds[0]+1 or box[1]<bounds[1]+1 or box[2]>bounds[2]-1 or box[3]>bounds[3]-1:continue
        if any(not (box[2]<r[0] or r[2]<box[0] or box[3]<r[1] or r[3]<box[1]) for r in boxes+labels):continue
        if any(_crosses_box(a,b,box) for a,b in segments+list(zip(points,points[1:]))):continue
        return (x,y),box
    return None


def _channel_path(start,end,bounds,boxes,segments,*,gaps=(3,7)):
    """Finite orthogonal visibility grid, distance/bend cost and stable ties."""
    xs={start[0],end[0],bounds[0]+3,bounds[2]-3}
    ys={start[1],end[1],bounds[1]+3,bounds[3]-3}
    for l,t,r,b in boxes:
        for gap in gaps:xs.update((l-gap,r+gap));ys.update((t-gap,b+gap))
    # Existing segment ends expose additional channels around occupied wires.
    for a,b in segments:
        gap=gaps[0]
        xs.update((a[0]-gap,a[0]+gap,b[0]-gap,b[0]+gap))
        ys.update((a[1]-gap,a[1]+gap,b[1]-gap,b[1]+gap))
    xs=sorted(x for x in xs if bounds[0]+1<=x<=bounds[2]-1)
    ys=sorted(y for y in ys if bounds[1]+1<=y<=bounds[3]-1)
    if len(xs)*len(ys)>20000:return None
    xi={x:i for i,x in enumerate(xs)};yi={y:i for i,y in enumerate(ys)}
    if start[0] not in xi or start[1] not in yi or end[0] not in xi or end[1] not in yi:return None
    initial=(xi[start[0]],yi[start[1]],-1)
    queue=[(0,0,initial)];cost={initial:0};previous={};serial=0;finish=None
    expansions=0
    while queue:
        expansions+=1
        if expansions>12000:return None
        value,_,state=heapq.heappop(queue)
        if value!=cost[state]:continue
        i,j,direction=state
        if (xs[i],ys[j])==end:finish=state;break
        for ni,nj,axis in ((i-1,j,0),(i+1,j,0),(i,j-1,1),(i,j+1,1)):
            if not (0<=ni<len(xs) and 0<=nj<len(ys)):continue
            a,b=(xs[i],ys[j]),(xs[ni],ys[nj])
            if not _clear_segment(a,b,boxes,segments):continue
            total=value+math.dist(a,b)+(7 if direction not in {-1,axis} else 0)
            next_state=(ni,nj,axis)
            if total<cost.get(next_state,float('inf')):
                cost[next_state]=total;previous[next_state]=state;serial+=1
                heapq.heappush(queue,(total,serial,next_state))
    if finish is None:return None
    result=[]
    while True:
        result.append((xs[finish[0]],ys[finish[1]]))
        if finish==initial:break
        finish=previous[finish]
    result.reverse()
    return result


def _simplify_path(points):
    result=[]
    for point in points:
        if result and point==result[-1]:continue
        if len(result)>1 and abs((result[-1][0]-result[-2][0])*(point[1]-result[-1][1])-(result[-1][1]-result[-2][1])*(point[0]-result[-1][0]))<1e-8:
            result[-1]=point
        else:result.append(point)
    return result


def _port_options(node,offset,source,kind='power',role=None):
    x,y,w,h=node['x'],node['y'],node['width'],node['height']
    l,r=x-w/2,x+w/2
    lane=symbol_lane((l,y,r,y+h),wide=node.get('drafting_lane')=='side-wide') if node.get('drafting_lane') else None
    axis=lane['axis_x'] if lane else x
    if source and kind in {'neutral','grounding'} and node.get('equipment_type') in _BUS_EQUIPMENT:
        yy=y+(28 if kind=='neutral' else 32)
        xx=l if kind=='neutral' else r
        return [((xx,yy),(xx+(-5 if kind=='neutral' else 5),yy))]
    if not source and kind in {'neutral','grounding'} and node.get('equipment_type') in _BUS_EQUIPMENT:
        yy=y+(28 if kind=='neutral' else 32);xx=l if kind=='neutral' else r
        return [((xx,yy),(xx+(-5 if kind=='neutral' else 5),yy))]
    if node.get('equipment_type')=='transfer_switch' and kind=='power':
        if not source:
            return [((axis-8,y),(axis-8,y-5))] if role=='normal' else [((l,y+27),(l-5,y+27)),((l,y+27),(l-1.5,y+27))]
        xx=max(lane['symbol_left']+6,min(r-6,axis+offset)) if lane and lane['wide'] else axis+8
        return [((xx,y+h),(xx,y+h+5)),((r,y+(31 if lane and lane['wide'] else 24)),(r+5,y+(31 if lane and lane['wide'] else 24)))]
    if node.get('equipment_type')=='transformer' and kind=='power':
        yy=y+h if source else y
        return [((axis,yy),(axis,yy+(5 if source else -5)))]
    xx=axis+offset
    choices=[((xx,y+h),(xx,y+h+5)),((l,y+23),(l-5,y+23)),((r,y+23),(r+5,y+23)),((xx,y),(xx,y-5))]
    if source and kind=='power' and node.get('equipment_type') in _BUS_EQUIPMENT and set(node.get('network_kinds',()))&{'neutral','grounding'}:
        bars=_auxiliary_bars((l,y,r,y+h),node.get('drafting_lane'))
        phase=(max(lane['symbol_left']+6,min(lane['symbol_right']-6,xx)),y+23) if lane else (xx,y+23)
        choices=[choice for choice in choices if not any(_segments_cross(phase,choice[0],*bars[aux]) for aux in node['network_kinds'] if aux in bars)]
    return choices if source else [choices[3],choices[2],choices[1],choices[0]]


@lru_cache(maxsize=128)
def _cached_backbone_geometry(payload):
    document=json.loads(payload);page,edges,bounds=document['page'],document['edges'],document['bounds']
    positions={n['equipment_id']:n for n in page['nodes']}
    raw_boxes={key:(n['x']-n['width']/2,n['y'],n['x']+n['width']/2,n['y']+n['height']) for key,n in positions.items()}
    boxes=[(l-.6,t-.6,r+.6,b+.6) for l,t,r,b in raw_boxes.values()]
    original=page['connections']
    span=lambda key:positions[edges[key]['load_id']]['y']-positions[edges[key]['source_id']]['y']
    orders=[original,sorted(original,key=lambda key:(-span(key),key)),list(reversed(original))]
    for order in orders:
        routed={};segments=[];labels=[]
        for key in order:
            edge=edges[key];start,end=positions[edge['source_id']],positions[edge['load_id']]
            outgoing=[k for k in original if edges[k]['source_id']==edge['source_id']]
            incoming=[k for k in original if edges[k]['load_id']==edge['load_id']]
            source_offset=(outgoing.index(key)-(len(outgoing)-1)/2)*6
            if start.get('equipment_type') in {'panelboard','switchboard','transfer_switch'}:
                source_axis=start.get('axis_x',start['x'])
                target_axis=end.get('axis_x',end['x'])
                lane=symbol_lane(raw_boxes[edge['source_id']],wide=start.get('drafting_lane')=='side-wide')
                source_offset=max(lane['symbol_left']+6,min(raw_boxes[edge['source_id']][2]-6,target_axis))-source_axis
            load_offset=(incoming.index(key)-(len(incoming)-1)/2)*6
            if end.get('drafting_lane')=='side-wide' and end.get('equipment_type') in {'panelboard','switchboard'}:
                lane=symbol_lane(raw_boxes[edge['load_id']],wide=True)
                load_offset=max(lane['symbol_left']+6,min(raw_boxes[edge['load_id']][2]-6,start.get('axis_x',start['x'])))-end['axis_x']
            kind=edge.get('drafting',{}).get('connection_kind','power')
            source_options=_port_options(start,source_offset,True,kind,edge['source_role'])
            target_options=_port_options(end,load_offset,False,kind,edge['load_role'])
            a=source_options[0][0]
            b=target_options[0][0]
            others=[(l-1,t-1,r+1,b+1) for owner,(l,t,r,b) in raw_boxes.items() if owner not in {edge['source_id'],edge['load_id']}]
            points=None;label=None
            if (a[0]==b[0] or a[1]==b[1]) and _clear_segment(a,b,others,segments):
                placed=_label_location([a,b],edge,bounds,boxes,segments,labels)
                if placed:points=[a,b];label=placed
            if points is None:
                for source,escape in source_options:
                    if not _clear_segment(source,escape,others,segments):continue
                    for target,approach in target_options:
                        if not _clear_segment(target,approach,others,segments):continue
                        route=_channel_path(escape,approach,bounds,boxes+labels,segments)
                        if not route:continue
                        candidate=_simplify_path([source,*route,target])
                        placed=_label_location(candidate,edge,bounds,boxes,segments,labels)
                        if placed:points=candidate;label=placed;break
                    if points is not None:break
            if points is None:break
            anchor,box=label
            routed[key]={'connection_id':key,'points':points,'label':anchor,'label_bounds_mm':box}
            segments.extend(zip(points,points[1:]));labels.append(box)
        if len(routed)==len(original):return json.dumps([routed[key] for key in original])
    return None  # Cache deterministic channel exhaustion as well as successes.


def backbone_geometry(page,edges,bounds):
    # Only geometric/core inputs participate in the cache, not changing sheet IDs.
    core={key:page[key] for key in ('nodes','connections')}
    selected={key:edges[key] for key in page['connections']}
    raw=_cached_backbone_geometry(json.dumps({'page':core,'edges':selected,'bounds':bounds},sort_keys=True))
    if raw is None:raise ValueError('bounded backbone channels exhausted; reflow or continue source groups')
    return json.loads(raw)


def _topology_fingerprint(topology):
    import hashlib
    return hashlib.sha256(json.dumps(topology.to_dict(),sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def _plan_fingerprint(layout):
    import hashlib
    value={key:val for key,val in layout.items() if key!='plan_fingerprint'}
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def _regions(bounds,regions):
    l,t,r,b=bounds
    if not isinstance(regions,dict) or 'diagram' not in regions or set(regions)-{'diagram','notes','feeder_register'}:
        raise ValueError('regions: require diagram; optional notes and feeder_register rectangles only')
    result={}
    for name,rect in regions.items():
        if not isinstance(rect,(tuple,list)) or len(rect)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in rect):
            raise ValueError(f'regions.{name}: supply four finite paper-mm coordinates')
        x,y,right,bottom=rect
        if x>=right or y>=bottom or x<l or y<t or right>r or bottom>b:
            raise ValueError(f'regions.{name}: positive rectangle must lie inside actual drawing bounds')
        result[name]=tuple(rect)
    for i,(name,rect) in enumerate(result.items()):
        for other,second in list(result.items())[i+1:]:
            if max(rect[0],second[0])<min(rect[2],second[2]) and max(rect[1],second[1])<min(rect[3],second[3]):
                raise ValueError(f'regions: {name} overlaps {other}')
    return result


def fan_geometry(group):
    x,y,w=group['x'],group['y'],group['width']
    parent=(x+5,y+3,x+w-5,y+43)
    lane=symbol_lane(parent,wide=True)
    left=lane['symbol_left']+6;right=parent[2]-8
    pitch=(right-left)/max(1,len(group['connections']))
    top=y+group['height']-52
    children=[(left+i*pitch,top,left+(i+1)*pitch-4,top+45) for i in range(len(group['connections']))]
    return parent,children,left,right


PAPER_EPS_MM=1e-6  # Below 1/100000 of the narrowest 0.13-mm drawn stroke.


# Largest external bus stroke is 0.65 mm. Two unrelated buses require
# 0.65 mm centreline separation plus 0.25 mm of visible white clearance.
PAPER_BUS_CLEARANCE_MM=.90


def _paper_clear_segment(a,b,boxes,segments=()):
    ink_boxes=[(min(c[0],d[0])-PAPER_BUS_CLEARANCE_MM,min(c[1],d[1])-PAPER_BUS_CLEARANCE_MM,
                max(c[0],d[0])+PAPER_BUS_CLEARANCE_MM,max(c[1],d[1])+PAPER_BUS_CLEARANCE_MM) for c,d in segments]
    return _clear_segment(a,b,[*boxes,*ink_boxes],segments)


def _paper_route(start,end,bounds,boxes,segments):
    boxes=[*boxes,*[(min(c[0],d[0])-PAPER_BUS_CLEARANCE_MM,min(c[1],d[1])-PAPER_BUS_CLEARANCE_MM,
                       max(c[0],d[0])+PAPER_BUS_CLEARANCE_MM,max(c[1],d[1])+PAPER_BUS_CLEARANCE_MM) for c,d in segments]]
    candidates=([start,end],) if abs(start[0]-end[0])<PAPER_EPS_MM or abs(start[1]-end[1])<PAPER_EPS_MM else ([start,(end[0],start[1]),end],[start,(start[0],end[1]),end])
    for route in candidates:
        if all(_clear_segment(a,b,boxes,segments) for a,b in zip(route,route[1:])):return _simplify_path(route)
    rounded=lambda p:tuple(round(v,7) for v in p)
    route=_channel_path(rounded(start),rounded(end),rounded(bounds),[rounded(box) for box in boxes],[(rounded(a),rounded(b)) for a,b in segments],gaps=(1.5,))
    if route is None:return None
    # Grid deduplication must not create tiny sloping joins to exact bus axes.
    def snap(value,axis):
        for point in (start,end):
            if abs(value-point[axis])<PAPER_EPS_MM:return point[axis]
        return value
    return _simplify_path([(snap(x,0),snap(y,1)) for x,y in route])


_BUS_EQUIPMENT={'panelboard','switchboard','disconnect','transfer_switch'}


def _bus_key(edge):
    return (edge['source_id'],edge['drafting']['connection_kind'],edge['source_role'])


def chain_feeder_geometry(group, edges, equipment):
    """Return the exact non-side-lane chain conductors used by the renderer."""
    keys=group['connections'];ids=[group['equipment_id']]+[edges[k]['load_id'] for k in keys]
    gap=34;width=(group['width']-gap*(len(ids)-1))/len(ids)
    boxes=[]
    for i,identity in enumerate(ids):
        x=group['x']+i*(width+gap);y=group['y']+13
        boxes.append((x,y,x+width,y+40))
    def terminal(identity,box,boundary,kind,source,role):
        x,y,r,b=box;axis=(x+r)/2;t=equipment[identity]['equipment_type']
        if kind=='neutral' and t in _BUS_EQUIPMENT:return (x+11.5,y+28)
        if kind=='grounding' and t in _BUS_EQUIPMENT:return (r-11.5,y+32)
        if t=='transfer_switch':return (axis+(8 if source else -8),y+(24 if source else 21 if role=='normal' else 27))
        if t=='transformer':return (axis,y+(30 if source else 16))
        if t in {'source','generator','meter'}:return (axis,y+(29 if source else 19))
        if t=='ground_electrode':return (axis,y+18)
        return (min(r-6,max(x+6,boundary[0])),y+23)
    result=[]
    for i,key in enumerate(keys):
        edge=edges[key];a=(boxes[i][2],group['y']+36);b=(boxes[i+1][0],group['y']+36)
        kind=edge['drafting']['connection_kind']
        start=terminal(ids[i],boxes[i],a,kind,True,edge['source_role'])
        end=terminal(ids[i+1],boxes[i+1],b,kind,False,edge['load_role'])
        points=[start,(a[0],start[1]),a,b,(b[0],end[1]),end]
        result.append({'connection_id':key,'points':points,'source_bounds':boxes[i],
                       'target_bounds':boxes[i+1]})
    return boxes,result


def _validate_terminal_roles(topology):
    """Refuse roles this grammar cannot represent separately on one bar.

    Different roles do not themselves establish internal connectivity. Apply
    the existing fan guard equally to core, chain and precomputed layouts.
    """
    roles={}
    equipment={e['id']:e for e in topology.equipment}
    for edge in topology.connections:
        for endpoint in ('source','load'):
            owner=edge[endpoint+'_id'];kind=edge['drafting']['connection_kind']
            # Transfer normal/alternate are deliberately separate contacts.
            if endpoint=='load' and kind=='power' and equipment[owner]['equipment_type']=='transfer_switch':continue
            key=(owner,kind,endpoint)
            roles.setdefault(key,set()).add(edge[endpoint+'_role'])
    for (owner,kind,endpoint),values in sorted(roles.items()):
        if len(values)>1:
            raise ValueError(f'{owner}: distinct {kind} {endpoint} roles cannot share the single represented terminal bar; supply explicit separate bus/winding equipment')


def _bus_caption(key,home):
    owner,kind,role=key
    return owner+' '+{'power':'LOAD','neutral':'NEUTRAL','grounding':'GROUND'}[kind]+' BUS / '+home


def _auxiliary_bars(bounds,lane):
    l,t,r,b=bounds
    layout=symbol_lane(bounds,wide=lane=='side-wide') if lane else {'symbol_left':l,'symbol_right':r}
    sx,sr=layout['symbol_left'],layout['symbol_right']
    return {'neutral':((sx+4,t+28),(sx+19,t+28)),
            'grounding':((sr-19,t+32),(sr-4,t+32))}


class _BusLayoutError(ValueError):
    def __init__(self,owner,message):
        super().__init__(message);self.owner=owner


def _refresh_page_bindings(result,topology,first_sheet_id,reserved_sheet_ids):
    numeric=re.fullmatch(r'(.*?)([0-9]+)',first_sheet_id)
    reserved={key.casefold() for key in reserved_sheet_ids}
    homes={};occurrences=[];locations={};edges={e['id']:e for e in topology.connections}
    for ordinal,page in enumerate(result['pages']):
        identity=first_sheet_id if ordinal==0 else numeric.group(1)+str(int(numeric.group(2))+ordinal).zfill(len(numeric.group(2))) if numeric else first_sheet_id+'-'+str(ordinal+1)
        if len(identity)>64 or identity.casefold() in reserved:raise ValueError(f'single-line continuation ID {identity!r} exceeds capacity or collides with a reserved sheet')
        page['id']=identity
        ids=[node['equipment_id'] for node in page['nodes']]
        for group in page['groups']:
            ids.append(group['equipment_id']);ids.extend(edges[key]['load_id'] for key in group['connections'])
        for key in ids:homes.setdefault(key,identity);occurrences.append({'equipment_id':key,'sheet_id':identity})
        for key in page['connections']+[key for g in page['groups'] for key in g['connections']]:
            if key in locations:raise ValueError(f'{key}: reflow duplicated a supplied connection')
            locations[key]=identity
    if set(homes)!={e['id'] for e in topology.equipment} or set(locations)!=set(edges):raise ValueError('bus reflow omitted source equipment or connections')
    result['equipment_home_sheets']=homes;result['equipment_occurrences']=occurrences
    result['sheet_index']=[{'id':page['id'],'kind':page['kind']} for page in result['pages']]
    for row in result['connections']:
        row['source_sheet']=locations[row['connection_id']];row['load_sheet']=locations[row['connection_id']]
    return result


def _reflow_bus_pages(result,bounds,isolated):
    """Shelf-pack source groups; conflicting input owners get explicit continuations."""
    left,top,right,bottom=bounds;available=right-left-6
    overview=[];normal=[];separate={owner:[] for owner in isolated}
    for page in result['pages']:
        if page['nodes']:overview.append({key:page[key] for key in ('nodes','connections','core_bounds')} | {'kind':'distribution-overview','groups':[],'entries':[]})
        for group in page['groups']:
            base={key:group[key] for key in ('kind','equipment_id','connections','height')}
            (separate[group['equipment_id']] if group['equipment_id'] in isolated and group.get('kind')!='chain' else normal).append(base)
    def pack(groups):
        pages=[];page=None;x=left+3;y=top+27;row_height=0
        for base in groups:
            group=dict(base);count=len(group['connections'])
            width=min(available,(count+1)*100+count*34) if group['kind']=='chain' else min(available,max(260,108+count*78))
            if group['height']>bottom-top-44:raise ValueError('source continuation cannot fit minimum group height')
            if page is None:page={'kind':'distribution-continuation','nodes':[],'connections':[],'groups':[],'entries':[]};pages.append(page);x=left+3;y=top+27;row_height=0
            if x+width>right-3+PAPER_EPS_MM:x=left+3;y+=row_height;row_height=0
            if y+group['height']>bottom-17:
                page={'kind':'distribution-continuation','nodes':[],'connections':[],'groups':[],'entries':[]};pages.append(page);x=left+3;y=top+27;row_height=0
            group.update(x=x,y=y,width=width);page['groups'].append(group)
            x+=width+5;row_height=max(row_height,group['height'])
        return pages
    result['pages']=[*overview,*pack(normal),*[page for owner in sorted(separate) for page in pack(separate[owner])]]
    return result


def _prepare_bus_layout(result,topology,bounds,first_sheet_id,reserved_sheet_ids):
    # Finite alternatives: initial packing, separate core from branches, then
    # isolate at most one additional source owner per retry. Never page-retry
    # unchanged groups or fabricate a route through an occupied terminal.
    isolated=set();limit=2+len({g['equipment_id'] for p in result['pages'] for g in p['groups']})
    normal_inputs={e['source_id'] for e in topology.connections if e['load_role']=='normal'}
    reflows=0
    for attempt in range(limit):
        _refresh_page_bindings(result,topology,first_sheet_id,reserved_sheet_ids)
        try:_consolidate_buses(result,topology,bounds);result['bus_layout_prepared']=True;result['bus_reflows']=reflows;result['bus_reflow_sources']=sorted(isolated);return result
        except _BusLayoutError as error:
            if any(page['nodes'] and page['groups'] for page in result['pages']):
                _reflow_bus_pages(result,bounds,isolated);reflows+=1;continue
            available={g['equipment_id'] for p in result['pages'] for g in p['groups'] if g.get('kind')!='chain'}
            candidates=sorted((normal_inputs & available)-isolated)
            owner=candidates[0] if candidates else error.owner
            if owner in isolated:
                remaining=sorted(available-isolated)
                if not remaining:raise
                owner=remaining[0]
            isolated.add(owner);_reflow_bus_pages(result,bounds,isolated);reflows+=1
    raise ValueError('bounded bus reflow alternatives exhausted; enlarge diagram or provide a separate source sheet')


def _consolidate_buses(result,topology,bounds):
    """Plan one equipment occurrence per sheet and explicit common source buses."""
    equipment={e['id']:e for e in topology.equipment};edges={e['id']:e for e in topology.connections}
    for page in result['pages']:
        homes={n['equipment_id']:(n['x']-n['width']/2,n['y'],n['x']+n['width']/2,n['y']+n['height']) for n in page['nodes']}
        lanes={n['equipment_id']:n.get('drafting_lane') for n in page['nodes']}
        rows={}
        repeated_chain_boxes=[];chain_segments=[]
        for index,group in enumerate(page['groups']):
            if group.get('kind')=='chain':
                ids=[group['equipment_id']]+[edges[key]['load_id'] for key in group['connections']]
                boxes,geometry=chain_feeder_geometry(group,edges,equipment)
                for identity,box in zip(ids,boxes):
                    if identity in homes and homes[identity]!=box:repeated_chain_boxes.append((identity,box))
                    homes.setdefault(identity,box);lanes.setdefault(identity,None)
                for row in geometry:
                    chain_segments.extend((row['connection_id'],i,a,b) for i,(a,b) in enumerate(zip(row['points'],row['points'][1:])) if a!=b)
                continue
            parent,children,left,right=fan_geometry(group)
            owner=group['equipment_id']
            homes.setdefault(owner,parent);lanes.setdefault(owner,'side-wide')
            if not group['connections']:
                group.update(source_home_bounds_mm=homes[owner],source_home_lane=lanes[owner]);continue
            signatures={_bus_key(edges[key]) for key in group['connections']}
            if len(signatures)!=1:raise ValueError(f'{owner}: mixed terminal networks require separate source-bus groups')
            terminal_key=next(iter(signatures));network_kind=terminal_key[1]
            device=equipment[owner]['equipment_type']
            offset=23 if device in {'neutral_bus','ground_bus'} else 18 if device=='ground_electrode' else 28 if network_kind=='neutral' else 32 if network_kind=='grounding' else 31 if device=='transfer_switch' else 34 if device in {'transformer','source','generator','meter'} else 23
            bus_y=parent[1]+offset
            rows.setdefault(terminal_key,[]).append((group,left,right,bus_y))
            for key,child_bounds in zip(group['connections'],children):
                target=edges[key]['load_id'];homes.setdefault(target,child_bounds);lanes.setdefault(target,'side')
        core_geometry=backbone_geometry(page,edges,page['core_bounds']) if page['nodes'] else []
        routed_bus_segments=[];routed_private_segments=[]
        for terminal_key,items in rows.items():
            owner,network_kind,source_role=terminal_key
            signatures={key for key in rows if key[0]==owner and key[1]==network_kind}
            if len(signatures)>1:raise ValueError(f'{owner}: distinct {network_kind} source roles cannot share the single represented terminal bar; supply explicit separate bus/winding equipment')
            volts={edges[key]['voltage_v'] for group,_,_,_ in items for key in group['connections'] if edges[key]['voltage_v'] is not None}
            if len(volts)>1:raise ValueError(f'{owner}: distinct supplied voltages cannot share one terminal bus; supply explicit separate winding/bus topology')
            if equipment[owner]['equipment_type']=='transfer_switch' and source_role in {'normal','alternate'}:raise ValueError(f'{owner}: sourcing from a transfer input terminal is unsupported; no load-bus connection is inferred')
            if network_kind!='power' and equipment[owner]['equipment_type'] not in (_BUS_EQUIPMENT|({'neutral_bus'} if network_kind=='neutral' else {'ground_bus','ground_electrode'})):raise ValueError(f'{owner}: auxiliary source terminal on this equipment type is unsupported; supply an explicit neutral/ground bus')
            home=homes[owner];kind=equipment[owner]['equipment_type'];wide=lanes[owner]=='side-wide'
            source_y=home[1]+(31 if kind=='transfer_switch' and wide else 24 if kind=='transfer_switch' else 34 if kind in {'transformer','source','generator','meter'} and wide else 30 if kind=='transformer' else 29 if kind in {'source','generator','meter'} else 23)
            start_x=home[2]-8 if wide else home[2]-18 if kind in {'transformer','source','generator','meter'} else home[2]-10
            source_lane=symbol_lane(home,wide=wide) if lanes[owner] else {'symbol_left':home[0],'symbol_right':home[2],'axis_x':(home[0]+home[2])/2}
            if network_kind!='power':
                if kind in {'neutral_bus','ground_bus'}:
                    bar=((source_lane['symbol_left']+5,home[1]+23),(source_lane['symbol_right']-5,home[1]+23))
                elif kind=='ground_electrode':bar=((source_lane['axis_x'],home[1]+18),)*2
                else:bar=_auxiliary_bars(home,lanes[owner])[network_kind]
                start_x=(bar[0][0]+bar[1][0])/2;source_y=bar[0][1]
            start=(start_x,source_y)
            trunk=max([home[2]+3]+[right for _,_,right,_ in items])
            boxes=[(l-PAPER_BUS_CLEARANCE_MM,t-PAPER_BUS_CLEARANCE_MM,r+PAPER_BUS_CLEARANCE_MM,b+PAPER_BUS_CLEARANCE_MM) for identity,(l,t,r,b) in [*homes.items(),*repeated_chain_boxes] if identity!=owner]
            # Unrelated endpoint leads lie wholly in their clearance-padded
            # equipment box; retain the inter-box span and every lead in the
            # current owner's box, where distinct terminals matter.
            foreign=[(a,b) for key,index,a,b in chain_segments
                     if (index==2 or (index<2 and edges[key]['source_id']==owner) or (index>2 and edges[key]['load_id']==owner))
                     and not (index<2 and _bus_key(edges[key])==terminal_key)]
            foreign.extend([*routed_bus_segments,*routed_private_segments])
            for path in core_geometry:
                if _bus_key(edges[path['connection_id']])!=terminal_key:foreign.extend(zip(path['points'],path['points'][1:]))
            for other,other_rows in rows.items():
                if other==terminal_key:continue
                for other_group,l,r,yy in other_rows:
                    foreign.append(((l,yy),(r,yy)))
                    parent,_,_,_=fan_geometry(other_group)
                    caption=_bus_caption(other,result['equipment_home_sheets'][other[0]])
                    boxes.append((parent[0]+1.8,yy-5-SMALL_TEXT_MM,parent[0]+2.2+measure_sheet_text(caption,SMALL_TEXT_MM,font_family=FONT),yy-5+SMALL_TEXT_MM*.3))
            lane=source_lane
            # Other networks on the same equipment are real conductive bars,
            # including while traversing the equipment's own envelope.
            actual_kinds={edge['drafting']['connection_kind'] for edge in topology.connections if owner in {edge['source_id'],edge['load_id']}}
            aux_bars=_auxiliary_bars(home,lanes[owner])
            for other_kind,bar in aux_bars.items():
                if kind in _BUS_EQUIPMENT and other_kind!=network_kind and other_kind in actual_kinds:foreign.append(bar)
            if kind in _BUS_EQUIPMENT and network_kind!='power' and 'power' in actual_kinds:
                phase_y=home[1]+(31 if kind=='transfer_switch' and wide else 24 if kind=='transfer_switch' else 23)
                foreign.append(((lane['symbol_left']+6,phase_y),(lane['symbol_right']-6,phase_y)))
            exit_options=[(start,(lane['axis_x'],home[1]-3))] if kind=='ground_electrode' else [(start,(home[0]-3,source_y))] if network_kind=='neutral' else [(start,(home[2]+3,source_y))]
            join_xs=[lane['axis_x'],lane['symbol_left']+6,lane['symbol_right']-6] if wide else [start_x] if kind=='transfer_switch' else [lane['axis_x'],start_x]
            if network_kind!='power':join_xs=[start_x] if kind in {'neutral_bus','ground_bus','ground_electrode'} else [start_x,aux_bars[network_kind][0][0]+1,aux_bars[network_kind][1][0]-1]
            exit_options.extend(((xx,source_y),(xx,home[3]+3)) for xx in join_xs)
            for group,left,right,bus_y in items:
                # The complete drawn row is conductor ink even beyond the last
                # private feeder branch. It must clear every unrelated envelope.
                if not _paper_clear_segment((left,bus_y),(trunk,bus_y),boxes,foreign):
                    raise _BusLayoutError(owner,f'{owner}: shared bus row intersects unrelated equipment; continue this source')
                goal=(trunk,bus_y);route=None;selected_start=None
                for join,escape in exit_options:
                    if not _paper_clear_segment(join,escape,boxes,foreign):continue
                    route_boxes=[*boxes,(home[0]-.6,home[1]-.6,home[2]+.6,home[3]+.6),
                                 (bounds[0],bounds[1],bounds[2],bounds[1]+25),
                                 (bounds[0],bounds[3]-16,bounds[2],bounds[3])]
                    route=_paper_route(escape,goal,bounds,route_boxes,foreign)
                    if route is not None:selected_start=join;break
                if route is None:raise _BusLayoutError(owner,f'{owner}: no clear source-bus channel; enlarge the diagram or continue this source')
                group['source_home_bounds_mm']=home
                group['source_home_lane']=lanes[owner]
                group['source_terminal_key']=terminal_key
                group['source_bus_caption']=_bus_caption(terminal_key,result['equipment_home_sheets'][owner])
                group['source_bus_path_mm']=_simplify_path([selected_start,*route])
                group['bus_row_mm']=(left,bus_y,trunk,bus_y)
            own_bus=[]
            for group,_,_,_ in items:
                own_bus.extend(zip(group['source_bus_path_mm'],group['source_bus_path_mm'][1:]))
                row=group['bus_row_mm'];own_bus.append((row[:2],row[2:]))
            private_for_owner=[]
            for group,left,right,bus_y in items:
                group['feeder_paths_mm']={};group['private_branch_index']={}
                group['target_home_bounds_mm']={};group['target_home_lanes']={}
                for key in group['connections']:
                    edge=edges[key];target=edge['load_id'];target_home=homes[target]
                    l,t,r,b=target_home
                    target_node={'x':(l+r)/2,'y':t,'width':r-l,'height':b-t,'equipment_type':equipment[target]['equipment_type'],'drafting_lane':lanes[target]}
                    ports=_port_options(target_node,0,False,edge['drafting']['connection_kind'],edge['load_role'])
                    found=None
                    for port,approach in ports:
                        branches=[approach[0],r+3,l-3,trunk]
                        for branch_x in dict.fromkeys(branches):
                            if not left-6<=branch_x<=trunk:continue
                            branch=(branch_x,bus_y)
                            direction=1 if approach[1]>=bus_y else -1
                            escape=(branch_x,bus_y+direction*3)
                            private_boxes=[(ll-PAPER_BUS_CLEARANCE_MM,tt-PAPER_BUS_CLEARANCE_MM,rr+PAPER_BUS_CLEARANCE_MM,bb+PAPER_BUS_CLEARANCE_MM) for identity,(ll,tt,rr,bb) in [*homes.items(),*repeated_chain_boxes] if identity!=owner]
                            owner_labels=symbol_lane(home,wide=wide)
                            private_boxes.extend([(owner_labels['text_left'],home[1],owner_labels['text_left']+owner_labels['text_width']+3,home[3]),
                                (bounds[0],bounds[1],bounds[2],bounds[1]+25),(bounds[0],bounds[3]-16,bounds[2],bounds[3])])
                            # The final approach may enter its target box, so
                            # retain every exact chain lead for private routes.
                            avoid=[*foreign,*private_for_owner,*[(a,b) for other,index,a,b in chain_segments if not (index<2 and _bus_key(edges[other])==terminal_key)]]
                            if not _paper_clear_segment(branch,escape,private_boxes,avoid):continue
                            body=_paper_route(escape,approach,bounds,private_boxes,[*avoid,*own_bus])
                            if body is None:continue
                            # The final approach crosses only its own equipment boundary.
                            other_boxes=[box for identity,box in [*homes.items(),*repeated_chain_boxes] if identity not in {owner,target}]
                            if not _paper_clear_segment(approach,port,other_boxes,avoid):continue
                            private=_simplify_path([branch,*body,port])
                            found=(branch,private);break
                        if found:break
                    if found is None:raise _BusLayoutError(owner,f'{key}: no clear route to the actual {target} {edge["load_role"]} terminal; continue this source')
                    branch,private=found
                    group['bus_row_mm']=(min(group['bus_row_mm'][0],branch[0]),bus_y,trunk,bus_y)
                    # Preserve the obstacle-checked route even when its ends
                    # happen to share a Y coordinate; a straight shortcut can
                    # pass through the very obstacle the route avoided.
                    prefix=[*group['source_bus_path_mm'],branch]
                    group['private_branch_index'][key]=len(prefix)-1
                    group['feeder_paths_mm'][key]=[*prefix,*private[1:]]
                    group['target_home_bounds_mm'][key]=target_home;group['target_home_lanes'][key]=lanes[target]
                    private_for_owner.extend(zip(private,private[1:]))
            for group,_,_,_ in items:
                row=group['bus_row_mm']
                if not _paper_clear_segment(row[:2],row[2:],boxes,foreign):
                    raise _BusLayoutError(owner,f'{owner}: extended shared bus row intersects unrelated equipment; continue this source')
            routed_private_segments.extend(private_for_owner)
            for group,_,_,_ in items:
                routed_bus_segments.extend(zip(group['source_bus_path_mm'],group['source_bus_path_mm'][1:]))
                row=group['bus_row_mm'];routed_bus_segments.append((row[:2],row[2:]))
    return result


def _resolve_navigation(result,topology,bounds,regions,reserved_sheet_ids=()):
    """Bind references only after every diagram and register row is paginated."""
    from .feeder_grid import resolve_register_footer
    pages=result['pages'];refs={row['connection_id']:row for row in result['connections']}
    edges={edge['id']:edge for edge in topology.connections};homes=result['equipment_home_sheets']
    register_pages=[page['id'] for page in pages if page.get('feeder_grids') or any(row.get('connection_id') for row in page.get('entries',[]))]
    for page in pages:
        if page['id'] not in register_pages:continue
        i=register_pages.index(page['id'])
        for grid in page.get('feeder_grids',[]):
            resolve_register_footer(grid,page['id'],register_pages[i-1] if i else None,register_pages[i+1] if i+1<len(register_pages) else None)
    index_entries={};index_consumers=[]
    original_pages=list(pages)
    for page in original_pages:
        shown=page['connections']+[key for group in page['groups'] for key in group['connections']]
        registered=[row['connection_id'] for grid in page.get('feeder_grids',[]) for row in grid['row_map']]+[row['connection_id'] for row in page.get('entries',[]) if row.get('connection_id')]
        known=set(shown)|set(registered)
        feeder_refs=[];lines=[]
        for key in sorted(known,key=lambda key:refs[key]['tag']):
            row=refs[key];destination=row.get('register_sheet')
            feeder_refs.append({'connection_id':key,'tag':row['tag'],'register_sheet':destination,'diagram_sheet':row['source_sheet'],'register_status':row.get('register_status','drawn')})
            if key in shown:
                lines.append(row['tag']+' REGISTER '+(destination or 'DELEGATED'))
            else:lines.append(row['tag']+' FEEDER '+row['source_sheet']+' / REGISTER '+(destination or 'DELEGATED'))
        symbols={node['equipment_id'] for node in page['nodes']}
        for group in page['groups']:
            symbols.add(group['equipment_id']);symbols.update(edges[key]['load_id'] for key in group['connections'])
        terminal_refs=[]
        for identity in sorted(symbols):
            for key,edge in edges.items():
                endpoint='source' if edge['source_id']==identity else 'load' if edge['load_id']==identity else None
                if endpoint is None:continue
                drawn=refs[key][endpoint+'_sheet'];home=homes[identity]
                direction='forward' if drawn!=page['id'] else 'return' if home!=page['id'] else None
                if direction is None:continue
                destination=drawn if direction=='forward' else home
                role=edge[endpoint+'_role'];tag=refs[key]['tag']
                record={'equipment_id':identity,'connection_id':key,'tag':tag,'endpoint':endpoint,'role':role,'direction':direction,'from_sheet':page['id'],'to_sheet':destination,'home_sheet':home,'terminal_sheet':drawn}
                terminal_refs.append(record)
                label=identity+' '+role.upper()+(' OUT ' if endpoint=='source' else ' IN ')+tag+' -> '+('HOME ' if direction=='return' else '')+destination
                lines.append(label)
        page['feeder_references']=feeder_refs;page['terminal_references']=terminal_refs
        if not lines:
            page['navigation_header']='GENERAL ONE-LINE NOTES' if page['kind']=='notes-continuation' else 'NO FEEDERS ON THIS SHEET'
            continue
        nav=['FEEDER / TERMINAL LINKS',*lines]
        notes=page.get('notes_bounds')
        if notes:
            l,t,r,b=notes;capacity=int((b-t-30)/4.6)
            wrapped=[part for value in nav for part in wrap_mm(value,r-l-10,size=3.2)]
            old=page.get('note_lines',[])
            if len(wrapped)+1+len(old)<=capacity:
                page['note_lines']=[*wrapped,'',*old];page['navigation_header']='FEEDER / TERMINAL LINKS AT RIGHT';continue
        # Keep all supplied notes intact; use a bounded shared reference index
        # if this sheet has no reference column or the column is full.
        destinations={row['register_sheet'] for row in feeder_refs if row['connection_id'] in shown}
        if len(destinations)==1:
            target=next(iter(destinations));header='ALL FEEDER TAGS: REGISTER '+(target or 'DELEGATED')
        else:header=' / '.join(row['tag']+' REG '+(row['register_sheet'] or 'DELEGATED') for row in feeder_refs if row['connection_id'] in shown)
        page['navigation_header']=header or 'FEEDER REGISTER INDEX'
        header_fits=len(wrap_mm(page['navigation_header'],regions['diagram'][2]-regions['diagram'][0]-26,size=TEXT_MM))<=2
        needs_index=bool(terminal_refs) or not header_fits or bool(registered and not shown and any(refs[key]['source_sheet']!=page['id'] for key in registered))
        if not needs_index:continue
        index_consumers.append(page)
        for row in feeder_refs:
            key=('feeder',row['connection_id'])
            index_entries[key]=row['tag']+' REGISTER '+(row['register_sheet'] or 'DELEGATED')+' / FEEDER '+row['diagram_sheet']
        for row in terminal_refs:
            key=('terminal',row['equipment_id'],row['connection_id'],row['endpoint'])
            index_entries[key]=row['equipment_id']+' '+row['role'].upper()+(' OUT ' if row['endpoint']=='source' else ' IN ')+row['tag']+': HOME '+row['home_sheet']+' <-> TERMINAL '+row['terminal_sheet']
    if index_entries:
        l,t,r,b=regions['diagram'];capacity=int((b-t-48)/4.6)
        if capacity<1:raise ValueError('diagram region cannot hold a reference-index line')
        remaining=[line for value in index_entries.values() for line in wrap_mm(value,r-l-10,size=3.2)]
        numeric=re.fullmatch(r'(.*?)([0-9]+)',result['first_sheet_id'])
        added=[];reserved={key.casefold() for key in reserved_sheet_ids}
        while remaining:
            ordinal=len(pages);base=result['first_sheet_id']
            identity=numeric.group(1)+str(int(numeric.group(2))+ordinal).zfill(len(numeric.group(2))) if numeric else base+'-'+str(ordinal+1)
            if len(identity)>64 or identity.casefold() in reserved:raise ValueError(f'reference sheet {identity!r} exceeds ID capacity or collides with a reserved sheet')
            page={'id':identity,'kind':'reference-index','nodes':[],'connections':[],'groups':[],'entries':[],
                  'reference_lines':remaining[:capacity],'drafting_layout':True,'diagram_bounds':regions['diagram'],
                  'effective_regions':{'diagram':regions['diagram']},'navigation_header':'TERMINAL / REGISTER INDEX',
                  'feeder_references':[],'terminal_references':[]}
            pages.append(page);added.append(identity);remaining=remaining[capacity:]
        for page in index_consumers:
            page['reference_index_sheets']=added
            page['navigation_header']='FEEDER / TERMINAL INDEX: '+added[0]
        for i,identity in enumerate(added):
            page=next(page for page in pages if page['id']==identity)
            links=[]
            if i:links.append('PREV '+added[i-1])
            if i+1<len(added):links.append('NEXT '+added[i+1])
            page['navigation_header']='REFERENCE INDEX '+str(i+1)+'/'+str(len(added))+(' / '+' / '.join(links) if links else '')
    result['register_page_order']=register_pages
    result['sheet_index']=[{'id':page['id'],'kind':page['kind']} for page in pages]
    result['navigation_version']='terminal-register-v1'
    return result


def _seal_layout(result,topology,bounds,regions,delegated=False,reserved_sheet_ids=()):
    if not result.get('bus_layout_prepared'):_consolidate_buses(result,topology,regions['diagram'])
    result['typography_version']='paper-type-v2'
    result['drawing_bounds_mm']=tuple(bounds)
    result['effective_regions']=regions
    result['topology_fingerprint']=_topology_fingerprint(topology)
    result['source_model_sha256']=topology.model_sha256
    result['delegated_feeder_connection_ids']=[edge['id'] for edge in topology.connections] if delegated else []
    if delegated:
        for row in result['connections']:
            row['register_sheet']=None;row['register_status']='delegated_to_composer'
    if not result.get('navigation_version'):_resolve_navigation(result,topology,bounds,regions,reserved_sheet_ids)
    for page in result['pages']:
        page.setdefault('effective_regions',regions)
        page.setdefault('diagram_bounds',regions['diagram'])
    result['plan_fingerprint']=_plan_fingerprint(result)
    return result


def plan_pages(topology: Topology,drawing_bounds_mm, *, first_sheet_id='E601',reserved_sheet_ids=(),regions=None):
    """Plan one-line content in explicit nonoverlapping global paper regions.

    Omitted notes/register in an explicit region request are delegated, not
    silently invented or placed over the coordinator's panel tables.
    """
    _validate_terminal_roles(topology)
    from .drafting_facts import protection_label
    left,top,right,bottom=drawing_bounds_mm
    width,height=right-left,bottom-top
    explicit=regions is not None
    composed=explicit or (width>=600 and height>=450)
    if not composed:
        result=_plan_topology_pages(topology,drawing_bounds_mm,first_sheet_id=first_sheet_id,reserved_sheet_ids=reserved_sheet_ids)
        for page in result['pages']:page['drafting_layout']=True
        return _seal_layout(result,topology,drawing_bounds_mm,{'diagram':tuple(drawing_bounds_mm)},reserved_sheet_ids=reserved_sheet_ids)
    if not explicit:
        regions={'diagram':(left,top,right-178,bottom-142),
                 'notes':(right-170,top,right,bottom-16),
                 'feeder_register':(left,bottom-130,right-178,bottom-16)}
    effective=_regions(drawing_bounds_mm,regions)
    result=_plan_topology_pages(topology,effective['diagram'],diagram_only=True,first_sheet_id=first_sheet_id,reserved_sheet_ids=reserved_sheet_ids)
    result=_prepare_bus_layout(result,topology,effective['diagram'],first_sheet_id,reserved_sheet_ids)
    pages=result['pages'];refs={row['connection_id']:row for row in result['connections']}
    tags={key:row['tag'] for key,row in refs.items()}
    rows=_facts(topology,tags)
    notes=[value for row in rows if 'connection_id' not in row for value in row['values']]
    notes[:0]=['(E) EXISTING; (N) NEW; (?) STATUS NOT SUPPLIED.',
              'Protection symbols, neutral and grounding links are drawn only from supplied facts.',
              'Knife symbols identify device type; operating state is not supplied.',
              'Dashed outlines identify equipment; heavy strokes identify schematic bus terminals.']
    note_lines=[];overflow_notes=[];note_capacity=0
    if 'notes' in effective:
        nl,nt,nr,nb=effective['notes']
        note_capacity=int((nb-nt-30)/4.6)
        if note_capacity<1:raise ValueError('notes region cannot fit heading and one readable line; enlarge the region')
        all_lines=[line for note in notes for line in wrap_mm(note,nr-nl-10,size=3.2)]
        note_lines=all_lines[:note_capacity];overflow_notes=all_lines[note_capacity:]
    register_region=effective.get('feeder_register')
    pending=[]
    if register_region:
        from .feeder_grid import paginate_feeder_grid
    for page in pages:
        page['drafting_layout']=True;page['entries']=[]
        page['diagram_bounds']=effective['diagram']
        if 'notes' in effective:page['notes_bounds']=effective['notes'];page['note_lines']=note_lines
        if not register_region:continue
        page['schedule_bounds']=register_region
        shown=page['connections']+[key for group in page['groups'] for key in group['connections']]
        sl,st,sr,sb=register_region
        partitions=[shown] if len(shown)<=8 else [shown[:(len(shown)+1)//2],shown[(len(shown)+1)//2:]]
        cells=[]
        for col,selection in enumerate(partitions):
            if not selection:continue
            step=(sr-sl)/len(partitions)
            bounds=(sl+col*step,st+14,sl+(col+1)*step-2,sb)
            grids=paginate_feeder_grid(topology,selection,bounds,sheet_id=page['id'],width_mm=right+left,height_mm=bottom+top)
            if grids:cells.append(grids[0]);pending.extend(key for grid in grids[1:] for key in grid['connection_ids'])
        if cells:
            page['feeder_grids']=cells
    numeric=re.fullmatch(r'(.*?)([0-9]+)',first_sheet_id)
    def following_id(ordinal):
        if numeric:
            prefix,digits=numeric.groups();value=prefix+str(int(digits)+ordinal).zfill(len(digits))
        else:value=first_sheet_id+'-'+str(ordinal+1)
        if len(value)>64 or value.casefold() in {key.casefold() for key in reserved_sheet_ids}:
            raise ValueError(f'single-line continuation ID {value!r} exceeds capacity or collides with a reserved sheet')
        return value
    while pending:
        identity=following_id(len(pages))
        page={'id':identity,'kind':'facts-register','nodes':[],'connections':[],'groups':[],'entries':[],
              'drafting_layout':True,'diagram_bounds':effective['diagram'],'effective_regions':dict(effective)}
        pages.append(page)
        if 'notes' in effective:page['notes_bounds']=effective['notes'];page['note_lines']=note_lines
        region=register_region if explicit else (left+2,top+25,right-178,bottom-16)
        if explicit:page['effective_regions']['feeder_register']=region
        else:
            page['effective_regions']={'diagram':(left,top,right-178,bottom)}
            if 'notes' in effective:page['effective_regions']['notes']=effective['notes']
            page['diagram_bounds']=page['effective_regions']['diagram']
        grids=paginate_feeder_grid(topology,pending,region,sheet_id=identity,width_mm=right+left,height_mm=bottom+top)
        if not grids or not grids[0]['connection_ids']:raise ValueError('supplied feeder row cannot fit a fresh shared-grid continuation')
        page['feeder_grids']=[grids[0]]
        consumed=set(grids[0]['connection_ids']);pending=[key for key in pending if key not in consumed]
    while overflow_notes:
        page={'id':following_id(len(pages)),'kind':'notes-continuation','nodes':[],'connections':[],'groups':[],'entries':[],
              'drafting_layout':True,'diagram_bounds':effective['diagram'],'effective_regions':dict(effective)}
        pages.append(page)
        page['notes_bounds']=effective['notes'];page['note_lines']=overflow_notes[:note_capacity]
        overflow_notes=overflow_notes[note_capacity:]
    for page in pages:
        for grid in page.get('feeder_grids',[]):
            for row in grid['row_map']:refs[row['connection_id']]['register_sheet']=page['id']
    result['sheet_index']=[{'id':page['id'],'kind':page['kind']} for page in pages]
    result['composition']='electrical-bus-drafting-v2'
    return _seal_layout(result,topology,drawing_bounds_mm,effective,explicit and not register_region,reserved_sheet_ids=reserved_sheet_ids)


def validate_precomputed_layout(layout,topology,drawing_bounds_mm):
    """Validate exact source/content binding and complete drawn topology."""
    _validate_terminal_roles(topology)
    if not isinstance(layout,dict) or layout.get('topology_fingerprint')!=_topology_fingerprint(topology):
        raise ValueError('precomputed layout topology fingerprint differs from the supplied model')
    if layout.get('plan_fingerprint')!=_plan_fingerprint(layout):
        raise ValueError('precomputed layout content changed; obtain a fresh plan')
    if layout.get('typography_version')!='paper-type-v2':
        raise ValueError('precomputed layout typography differs; obtain a fresh plan')
    if tuple(layout.get('drawing_bounds_mm',()))!=tuple(drawing_bounds_mm):
        raise ValueError('precomputed layout drawing bounds differ from resolved shared sheets')
    if layout.get('navigation_version')!='terminal-register-v1':raise ValueError('precomputed layout has unresolved navigation; obtain a fresh plan')
    _regions(drawing_bounds_mm,layout.get('effective_regions'))
    known={e['id'] for e in topology.equipment};edges={e['id']:e for e in topology.connections}
    seen=[];equipment=set();ids=[]
    for page in layout.get('pages',[]):
        identity=page['id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',identity):raise ValueError('precomputed layout has invalid sheet ID')
        ids.append(identity.casefold());_regions(drawing_bounds_mm,page['effective_regions'])
        nodes={node['equipment_id'] for node in page['nodes']}
        if not nodes<=known:raise ValueError('precomputed layout references unknown canonical equipment')
        equipment.update(nodes)
        for key in page['connections']:
            if key not in edges or not {edges[key]['source_id'],edges[key]['load_id']}<=nodes:
                raise ValueError('precomputed core has a missing/misbound canonical connection')
            seen.append(key)
        for group in page['groups']:
            owner=group['equipment_id'];equipment.add(owner)
            if owner not in known:raise ValueError('precomputed group references unknown equipment')
            for key in group['connections']:
                if key not in edges or edges[key]['source_id']!=owner:raise ValueError('precomputed group changed canonical connection endpoints')
                equipment.add(edges[key]['load_id']);seen.append(key)
                if group.get('kind')=='chain':owner=edges[key]['load_id']
    if len(set(ids))!=len(ids) or len(seen)!=len(set(seen)) or set(seen)!=set(edges) or equipment!=known:
        raise ValueError('precomputed layout duplicates or omits source equipment/connections')
    return json.loads(json.dumps(layout))
