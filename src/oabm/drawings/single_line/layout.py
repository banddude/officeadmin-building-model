"""Topology-aware compact schematic pagination, in paper millimetres."""
from __future__ import annotations
import math
from .topology import Topology
from ..company_sheets import measure_sheet_text, validate_sheet_text

FONT='DejaVu Sans'
TEXT_MM=2.5


def wrap_mm(value, width_mm, *, size=TEXT_MM):
    """Measured wrapping that preserves every supplied character, including spaces."""
    validate_sheet_text(value)
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
    node_width=min(110,(right-left)/(maximum_row+1)-10)
    if node_width<64 or bottom-top<(maximum+1)*54:
        return None
    nodes=[]
    pitch=min(88,(bottom-top-44)/max(1,maximum))
    for rank in range(maximum+1):
        row=[e for e in selected if ranks[e['id']]==rank]
        for i,e in enumerate(row):
            nodes.append({'equipment_id':e['id'],'x':left+(right-left)*(i+1)/(len(row)+1),
                          'y':top+rank*pitch,'width':node_width,'height':40})
    return {'nodes':nodes,'connections':[e['id'] for e in edges],'core_bounds':bounds}


def _facts(topology,tags):
    rows=[]
    for edge in topology.connections:
        voltage='V UNKNOWN' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
        values=[f'{tags[edge["id"]]} | Circuit {edge["circuit_id"]} | FROM {edge["source_id"]} / {edge["source_port_id"]} ({edge["source_role"]}) -> {edge["load_id"]} / {edge["load_port_id"]} ({edge["load_role"]})',
                f'{voltage}; poles {edge["poles"] if edge["poles"] is not None else "UNKNOWN"}; phase {edge["phase"] or "UNKNOWN"}; Conductors: {edge["conductor_specification"]}; Routes: '+(', '.join(edge['route_ids']) or 'UNKNOWN / not supplied')]
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
        height=len(lines)*3.4+4
        if y+height>bottom:return output,rows[i:]
        output.append({**row,'x':left+3,'y':y,'width':right-left-6,'lines':lines,'height':height})
        y+=height
    return output,[]


def plan_pages(topology: Topology,drawing_bounds_mm):
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
    core_width=w*.53 if small else min(290,w*.34)
    core=_core(topology,core_ids,(left+8,top+28,left+core_width-8,bottom-20)) if core_ids else None
    if small and core:
        pages.append({'id':'E601','kind':'single-line',**core,'groups':[],
                      'facts_bounds':(left+core_width+5,top+28,right-3,bottom-17)})
    elif core and len(core_ids)<=8:
        pages.append({'id':'E601','kind':'distribution',**core,'groups':[]})
    covered=set(pages[0]['connections']) if pages else set()
    # Fill one page with connected parent/children groups, retaining topology
    # order. A larger continuation sheet uses its full width automatically.
    page=pages[0] if pages and not small else None
    group_left=left+core_width+7 if page else left+3
    group_top=top+27
    group_bottom=bottom-17
    group_height=148
    y=group_top
    edges={e['id']:e for e in topology.connections}
    rendered_core_ids={n['equipment_id'] for p in pages for n in p['nodes']}
    for equipment in topology.equipment:
        remaining=[key for key in equipment['outgoing'] if key not in covered]
        # Isolated objects remain visible symbols, not merely a warning count.
        isolated=not equipment['incoming'] and not equipment['outgoing'] and equipment['id'] not in rendered_core_ids
        while remaining or isolated:
            if page is None or y+group_height>group_bottom:
                page={'id':f'E{601+len(pages)}','kind':'distribution-continuation','nodes':[],'connections':[],'groups':[]}
                pages.append(page);group_left=left+3;y=group_top
            width=right-group_left-3
            capacity=max(1,int(width//74))
            selected=remaining[:capacity]
            remaining=remaining[capacity:]
            page['groups'].append({'equipment_id':equipment['id'],'connections':selected,
                                   'x':group_left,'y':y,'width':width,'height':group_height})
            y+=group_height
            isolated=False
    # Equipment already in a small core needs no redundant terminal-detail grid.
    if not pages:
        raise ValueError('single-line cannot fit equipment topology in these drawing bounds')
    facts=_facts(topology,tags)
    if small and pages[0].get('facts_bounds'):
        placed,facts=_place_facts(facts,pages[0]['facts_bounds'])
        pages[0]['entries']=placed
    if facts and not small and pages[-1]['groups']:
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
        page={'id':f'E{601+len(pages)}','kind':'facts-register','nodes':[],'connections':[],'groups':[],'entries':[]}
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
    if set(homes)!=all_ids or set(locations)!=set(edges) or set(register)!=set(edges):
        raise ValueError('single-line pagination omitted equipment, connection or supplied register facts')
    refs=[{'connection_id':e['id'],'tag':tags[e['id']],
           'source_sheet':locations[e['id']],'load_sheet':locations[e['id']],
           'register_sheet':register[e['id']]} for e in topology.connections]
    return {'coordinate_system':'paper-mm,top-left,+x-right,+y-down','scale':'not-to-scale',
            'pages':pages,'equipment_home_sheets':homes,'equipment_occurrences':occurrences,
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


def backbone_geometry(page,edges,bounds):
    positions={n['equipment_id']:n for n in page['nodes']}
    result=[]
    for key in page['connections']:
        edge=edges[key]
        start,end=positions[edge['source_id']],positions[edge['load_id']]
        outgoing=[k for k in page['connections'] if edges[k]['source_id']==edge['source_id']]
        incoming=[k for k in page['connections'] if edges[k]['load_id']==edge['load_id']]
        ax=start['x']+(outgoing.index(key)-(len(outgoing)-1)/2)*6
        bx=end['x']+(incoming.index(key)-(len(incoming)-1)/2)*6
        ay,by=start['y']+start['height'],end['y']
        boxes=[(n['x']-n['width']/2-2,n['y']-2,n['x']+n['width']/2+2,n['y']+n['height']+2) for n in page['nodes'] if n['equipment_id'] not in {edge['source_id'],edge['load_id']}]
        points=[(ax,ay),(bx,by)];label=((ax+bx)/2+3,(ay+by)/2)
        if any(_crosses_box(points[0],points[1],box) for box in boxes):
            gutter=bounds[0]+3+len(result)*2
            bx,by=end['x']-end['width']/2,end['y']+end['height']/2
            points=[(ax,ay),(ax,ay+6),(gutter,ay+6),(gutter,by),(bx,by)]
            label=(gutter+3,(ay+by)/2)
        if any(_crosses_box(a,b,box) for a,b in zip(points,points[1:]) for box in boxes):
            raise ValueError(f'{key}: backbone cannot route clear of equipment; choose larger paper')
        result.append({'connection_id':key,'points':points,'label':label})
    return result
