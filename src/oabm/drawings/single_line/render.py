"""Electrical construction one-line bodies using bounded vector primitives."""
from __future__ import annotations
import math
import xml.etree.ElementTree as ET
from .layout import FONT,wrap_mm,backbone_geometry,symbol_lane,fan_geometry,_segments_cross,_paper_clear_segment
from .drafting_facts import status_tag,protection_label
from ..company_sheets import measure_sheet_text

NS='http://www.w3.org/2000/svg'
ET.register_namespace('',NS)


def render_body(topology,layout,page,sheet):
    root=ET.Element('{'+NS+'}g')
    left,top,right,bottom=page.get('diagram_bounds',sheet.drawing_bounds_mm)
    equipment={e['id']:e for e in topology.equipment}
    edges={e['id']:e for e in topology.connections}
    refs={r['connection_id']:r for r in layout['connections']}
    terminals=[];symbols=[];paths=[];labels=[];glyphs=[];seen=set();owner=None;symbol_lanes={};source_buses=[]
    def node(kind,**attrs):
        return ET.SubElement(root,'{'+NS+'}'+kind,{k.replace('_','-'):str(v) for k,v in attrs.items()})
    def text(x,y,value,size=2.5,anchor='start'):
        n=node('text',x=x,y=y,font_size=size,text_anchor=anchor,font_family=FONT,fill='#000000');n.text=value
        width=measure_sheet_text(value,size,font_family=FONT)
        start=x-width*{'start':0,'middle':.5,'end':1}[anchor]
        labels.append({'text':value,'font_size_mm':size,'equipment_id':owner,'bounds_mm':(start,y-size,start+width,y+size*.3)})
    def line(x1,y1,x2,y2,weight=.22):
        node('line',x1=x1,y1=y1,x2=x2,y2=y2,stroke='#000000',stroke_width=weight)
    def poly(points,weight=.25):
        node('polyline',points=' '.join(f'{x},{y}' for x,y in points),fill='none',stroke='#000000',stroke_width=weight)
    def circle(x,y,r,weight=.25):
        node('circle',cx=x,cy=y,r=r,fill='none',stroke='#000000',stroke_width=weight)
    def dash(a,b):
        length=math.dist(a,b)
        if not length:return
        for start in range(math.ceil(length/2.5)):
            lo=start*2.5;hi=min(length,lo+1.5)
            if lo<length:line(a[0]+(b[0]-a[0])*lo/length,a[1]+(b[1]-a[1])*lo/length,
                              a[0]+(b[0]-a[0])*hi/length,a[1]+(b[1]-a[1])*hi/length,.13)
    def outline(x,y,w,h):
        for a,b in [((x,y),(x+w,y)),((x+w,y),(x+w,y+h)),((x+w,y+h),(x,y+h)),((x,y+h),(x,y))]:dash(a,b)
    def section(number,title,x,y,width):
        circle(x+4,y+6,3.7);text(x+4,y+7.1,str(number),3.1,'middle')
        text(x+11,y+7.2,title,3.4);line(x+.2,y+12,x+width-.2,y+12,.3)
    def record_glyph(kind,source_id,bounds,path):
        glyphs.append({'kind':kind,'source_id':source_id,'source_path':path,'bounds_mm':bounds})
    def terminal_bar(identity,network_kind,a,b):
        glyphs.append({'kind':'terminal_bar','source_id':identity,'network_kind':network_kind,'segments_mm':[(a,b)],'source_path':'explicit network terminal'})
    def device(kind,x,y,ux,uy,source_id):
        def p(u,v=0):return (x+u*ux-v*uy,y+u*uy+v*ux)
        def stroke(a,b):line(*p(*a),*p(*b),.28)
        if kind=='breaker':
            stroke((-6,0),(-3,0));stroke((3,0),(6,0))
            poly([p(-3*math.cos(i*math.pi/12),3*math.sin(i*math.pi/12)) for i in range(13)],.32)
            circle(*p(-3),.45);circle(*p(3),.45)
        elif kind=='fuse':
            stroke((-6,0),(-3,0));stroke((3,0),(6,0))
            poly([p(-3,-1.5),p(3,-1.5),p(3,1.5),p(-3,1.5),p(-3,-1.5)],.28)
            stroke((-3,0),(3,0))
        elif kind=='disconnect':
            stroke((-6,0),(-2,0));stroke((2,0),(6,0));stroke((-2,0),(2,2.5))
            circle(*p(-2),.45);circle(*p(2),.45)
        elif kind=='fused_disconnect':
            device('fuse',*p(-4),ux,uy,source_id);device('disconnect',*p(5),ux,uy,source_id)
            stroke((-11,0),(-10,0));stroke((10,0),(11,0))
        record_glyph(kind,source_id,(x-11,y-11,x+11,y+11),'attributes.single_line_drafting.protection.kind')
    def lane_key(identity,bounds):return (identity,tuple(round(v,7) for v in bounds))
    def equipment_symbol(identity,x,y,w,h,continued=False,lane=None):
        nonlocal owner
        previous=owner;owner=identity
        e=equipment[identity];facts=e['drafting'];kind=e['equipment_type']
        side=bool(lane)
        reserved=symbol_lane((x,y,x+w,y+h),wide=lane=='side-wide') if side else None
        sx=reserved['symbol_left'] if side else x;sw=reserved['symbol_right']-sx if side else w
        axis=reserved['axis_x'] if side else x+w/2
        text_width=reserved['text_width'] if side else w-5
        text_left=reserved['text_left'] if side else x
        symbol_lanes[lane_key(identity,(x,y,x+w,y+h))]={'side':side,'axis':axis,'symbol_left':sx,'symbol_right':sx+sw,'wide':lane=='side-wide'}
        symbols.append({'equipment_id':identity,'bounds_mm':(x,y,x+w,y+h),
            'label_lane':(text_left,y,text_left+text_width+3,y+h) if side else None,'axis_x':axis,
            'enclosure_stroke_width_mm':.13,'enclosure_bounds_mm':(sx,y+5,sx+sw,y+h-.5) if kind in {'panelboard','switchboard','disconnect','transfer_switch'} else None})
        same_sheet_context=identity in seen and layout['equipment_home_sheets'][identity]==page['id']
        continued=layout['equipment_home_sheets'][identity]!=page['id']
        symbols[-1]['continued']=bool(continued)
        symbols[-1]['same_sheet_context']=same_sheet_context
        seen.add(identity)
        text(text_left+2,y+3.5,status_tag(facts['status'])+' '+identity,2.8)
        names=wrap_mm(e['name'],text_width)
        if len(names)>3:raise ValueError(f'{identity}: equipment name exceeds three measured lines; select a larger diagram area')
        for i,value in enumerate(names):text(text_left+2,y+9+i*3.4,value)
        bus_kinds={'panelboard','switchboard','disconnect','transfer_switch'}
        if kind in bus_kinds:
            outline(sx,y+5,sw,h-5.5)
            volts='V ?' if e['rated_voltage_v'] is None else f'{e["rated_voltage_v"]:g} V'
            current='BUS A ?' if facts['bus_current_a'] is None else f'{facts["bus_current_a"]:g} A BUS'
            rating=wrap_mm(current+' / '+volts,text_width,size=2.3)
            if len(rating)>2:raise ValueError(f'{identity}: bus rating needs a wider label lane')
            for i,value in enumerate(rating):text(text_left+2,y+(22 if side else 20)+i*3,value,2.3)
            if kind=='transfer_switch':
                line(axis-8,y+21,axis-3,y+21);line(axis-8,y+27,axis-3,y+27)
                line(axis+2,y+24,axis+8,y+24);line(axis+2,y+24,axis-2,y+22)
                record_glyph('transfer_unspecified',identity,(axis-8,y+20,axis+8,y+28),'equipment_type')
                if lane=='side-wide':
                    line(axis+8,y+24,axis+8,y+31)
                    line(sx+6,y+31,sx+sw-6,y+31,.65)
                    terminal_bar(identity,'power',(sx+6,y+31),(sx+sw-6,y+31))
                    record_glyph('bus',identity,(sx+6,y+30.6,sx+sw-6,y+31.4),'equipment_type')
            else:
                line(sx+6,y+23,sx+sw-6,y+23,.65)
                terminal_bar(identity,'power',(sx+6,y+23),(sx+sw-6,y+23))
                record_glyph('bus',identity,(sx+6,y+22.6,sx+sw-6,y+23.4),'equipment_type')
            kinds={edge['drafting']['connection_kind'] for edge in topology.connections if identity in {edge['source_id'],edge['load_id']}}
            if 'neutral' in kinds:
                line(sx+4,y+28,sx+19,y+28,.45);text(sx+4,y+26,'N',2.3)
                terminal_bar(identity,'neutral',(sx+4,y+28),(sx+19,y+28))
            if 'grounding' in kinds:
                line(sx+sw-19,y+32,sx+sw-4,y+32,.45);text(sx+sw-18,y+30,'G',2.3)
                terminal_bar(identity,'grounding',(sx+sw-19,y+32),(sx+sw-4,y+32))
        elif kind=='transformer':
            for winding_side in (-1,1):
                base=axis+winding_side*2
                for turn in range(3):
                    cy=y+20+turn*4
                    poly([(base+winding_side*3*math.sin(i*math.pi/12),cy-2*math.cos(i*math.pi/12)) for i in range(13)],.3)
            poly([(axis,y+16),(axis-2,y+16),(axis-2,y+18)],.3)
            poly([(axis+2,y+30),(axis,y+30)],.3)
            if lane=='side-wide':
                line(axis,y+30,axis,y+34)
                line(sx+6,y+34,sx+sw-6,y+34,.65)
                terminal_bar(identity,'power',(sx+6,y+34),(sx+sw-6,y+34))
                record_glyph('bus',identity,(sx+6,y+33.6,sx+sw-6,y+34.4),'equipment_type')
            record_glyph('transformer_windings',identity,(axis-5,y+16,axis+5,y+30),'equipment_type')
        elif kind in {'source','generator','meter'}:
            circle(axis,y+24,5,.32)
            text(axis,y+25,{'source':'S','generator':'G','meter':'M'}[kind],3,'middle')
            record_glyph(kind,identity,(axis-5,y+19,axis+5,y+29),'equipment_type')
            if lane=='side-wide':
                line(axis,y+29,axis,y+34)
                line(sx+6,y+34,sx+sw-6,y+34,.65)
                terminal_bar(identity,'power',(sx+6,y+34),(sx+sw-6,y+34))
        elif kind=='ground_electrode':
            line(axis,y+18,axis,y+25)
            terminal_bar(identity,'grounding',(axis,y+18),(axis,y+25))
            for delta,half in ((25,7),(28,4.5),(31,2)):
                line(axis-half,y+delta,axis+half,y+delta,.3)
                terminal_bar(identity,'grounding',(axis-half,y+delta),(axis+half,y+delta))
            record_glyph('ground_electrode',identity,(axis-7,y+18,axis+7,y+31),'equipment_type')
        elif kind in {'neutral_bus','ground_bus'}:
            line(sx+5,y+23,sx+sw-5,y+23,.65);text(axis+8,y+20,'N' if kind=='neutral_bus' else 'G',2.8)
            terminal_bar(identity,'neutral' if kind=='neutral_bus' else 'grounding',(sx+5,y+23),(sx+sw-5,y+23))
            record_glyph(kind,identity,(sx+5,y+20,sx+sw-5,y+24),'equipment_type')
        else:device(kind,axis,y+24,0,1,identity)
        footer=[]
        if kind=='transfer_switch':footer.append('STATE UNSPECIFIED')
        if continued:footer.append('CONTINUED '+layout['equipment_home_sheets'][identity])
        elif same_sheet_context:footer.append('SAME EQUIPMENT '+identity)
        elif facts['neutral_ground_bond']!='unknown':footer.append('N/G '+facts['neutral_ground_bond'].upper())
        if not e['incoming'] and not e['outgoing']:footer.append('ISOLATED / NO EXPLICIT CIRCUITS')
        if len(footer)>2:raise ValueError(f'{identity}: electrical status needs a larger symbol area')
        for i,value in enumerate(footer):
            if measure_sheet_text(value,2.3,font_family=FONT)>(text_width if side else w-4):raise ValueError(f'{identity}: electrical status exceeds symbol width')
            text(text_left+2,y+h-4-(len(footer)-1-i)*3.5,value,2.3)
        owner=previous
    def terminal(identity,bounds,boundary,kind,source,role=None):
        x,y,r,b=bounds;w=r-x;h=b-y;e=equipment[identity]
        lane=symbol_lanes.get(lane_key(identity,bounds),{})
        if lane.get('side'):
            axis=lane['axis'];device=e['equipment_type']
            if kind=='neutral' and device in {'panelboard','switchboard','disconnect','transfer_switch'}:return (lane['symbol_left']+11.5,y+28)
            if kind=='grounding' and device in {'panelboard','switchboard','disconnect','transfer_switch'}:return (lane['symbol_right']-11.5,y+32)
            if device in {'source','generator','meter'}:
                if source and lane['wide']:return (min(lane['symbol_right']-6,max(lane['symbol_left']+6,boundary[0])),y+34)
                return (axis,y+(29 if source else 19))
            if device=='transformer':
                if source and lane['wide']:return (min(lane['symbol_right']-6,max(lane['symbol_left']+6,boundary[0])),y+34)
                return (axis,y+(30 if source else 16))
            if device=='ground_electrode':return (axis,y+18)
            if device=='transfer_switch':
                if source and lane['wide']:return (min(lane['symbol_right']-6,max(lane['symbol_left']+6,boundary[0])),y+31)
                return (axis+(8 if source else -8),y+(24 if source else 21 if role=='normal' else 27))
            return (min(lane['symbol_right']-6,max(lane['symbol_left']+6,boundary[0])),y+23) if source or lane['wide'] else (axis,y+23)
        if kind=='neutral' and e['equipment_type'] in {'panelboard','switchboard','disconnect','transfer_switch'}:return (x+11.5,y+28)
        if kind=='grounding' and e['equipment_type'] in {'panelboard','switchboard','disconnect','transfer_switch'}:return (r-11.5,y+32)
        t=e['equipment_type']
        if t=='transfer_switch':return (x+w/2+(8 if source else -8),y+(24 if source else 21 if role=='normal' else 27))
        if t=='transformer':return (x+w/2,y+(30 if source else 16))
        if t in {'source','generator','meter'}:return (x+w/2,y+(29 if source else 19))
        if t=='ground_electrode':return (x+w/2,y+18)
        return (min(r-6,max(x+6,boundary[0])),y+23)
    def feeder(key,points,source_bounds,target_bounds,*,private_start=None):
        edge=edges[key];kind=edge['drafting']['connection_kind'];protection=edge['drafting']['protection']
        start=terminal(edge['source_id'],source_bounds,points[0],kind,True,edge['source_role'])
        end=terminal(edge['load_id'],target_bounds,points[-1],kind,False,edge['load_role'])
        def internal_lead(terminal,boundary,bounds,identity):
            # Reserve the entire header and footer for text. Only side rails
            # traverse these bands; the horizontal join lies in the bus lane.
            x,y,r,b=bounds
            if symbol_lanes.get(lane_key(identity,bounds),{}).get('side'):
                return [terminal,boundary] if terminal[0]==boundary[0] or terminal[1]==boundary[1] else [terminal,(boundary[0],terminal[1]),boundary]
            if abs(boundary[1]-y)<1e-6:
                rail=r-.8 if equipment[identity]['equipment_type'] in {'neutral_bus','ground_bus'} else x+.8
                return [terminal,(rail,terminal[1]),(rail,y),boundary]
            if abs(boundary[1]-b)<1e-6:
                return [terminal,(r-.8,terminal[1]),(r-.8,b),boundary]
            return [terminal,(boundary[0],terminal[1]),boundary]
        leading=internal_lead(start,points[0],source_bounds,edge['source_id'])
        external_start=len(leading)-1
        external_end=external_start+len(points)-1
        leading.extend(points[1:])
        leading.extend(reversed(internal_lead(end,points[-1],target_bounds,edge['load_id'])[:-1]))
        # Insert supplied protection in an external straight feeder run; missing facts
        # produce a plain conductor, never a default breaker.
        placed=False
        for segment_index,(a,b) in enumerate(zip(leading,leading[1:])):
            distance=math.dist(a,b)
            needed=22 if protection and protection['kind']=='fused_disconnect' else 12
            if protection and not placed and external_start<=segment_index<external_end and (private_start is None or segment_index>=external_start+private_start) and distance>=needed+2:
                ux,uy=(b[0]-a[0])/distance,(b[1]-a[1])/distance
                center=((a[0]+b[0])/2,(a[1]+b[1])/2);half=needed/2
                before=(center[0]-half*ux,center[1]-half*uy);after=(center[0]+half*ux,center[1]+half*uy)
                line(*a,*before);device(protection['kind'],*center,ux,uy,edge['circuit_id']);line(*after,*b)
                placed=True
            else:line(*a,*b)
        if protection and not placed:raise ValueError(f'{key}: supplied protection cannot fit its conductor lead')
        paths.append({'connection_id':key,'points_mm':leading})
        for endpoint in ('source','load'):
            terminals.append({'connection_id':key,'endpoint':endpoint,'sheet_id':page['id'],'counterpart_sheet':page['id'],'tag':refs[key]['tag']})
    section(1,'ELECTRICAL REFERENCES' if page['kind']=='reference-index' else 'FEEDER SCHEDULE CONTINUED' if page['kind']=='facts-register' else 'ONE-LINE NOTES CONTINUED' if page['kind']=='notes-continuation' else 'POWER ONE-LINE',left+2,top+1,right-left-4)
    header='NOT TO SCALE / '+page.get('navigation_header','REFERENCES UNRESOLVED')
    header_lines=wrap_mm(header,right-left-26,size=2.5)
    if len(header_lines)>2:raise ValueError('actual reference links exceed the reserved header; shorten IDs or allocate a notes region')
    for i,value in enumerate(header_lines):text(left+13,top+19+i*3.4,value,2.5)
    positions={n['equipment_id']:n for n in page['nodes']}
    for n in page['nodes']:equipment_symbol(n['equipment_id'],n['x']-n['width']/2,n['y'],n['width'],n['height'],lane=n.get('drafting_lane'))
    if positions:
        for geometry in backbone_geometry(page,edges,page['core_bounds']):
            key=geometry['connection_id'];edge=edges[key]
            def bounds(identity):
                n=positions[identity];return (n['x']-n['width']/2,n['y'],n['x']+n['width']/2,n['y']+n['height'])
            feeder(key,geometry['points'],bounds(edge['source_id']),bounds(edge['load_id']))
            x,y=geometry['label']
            for i,value in enumerate(edge['drawing_labels']):text(x,y+i*3.4,value)
    for group in page['groups']:
        x,y,w=group['x'],group['y'],group['width'];height=group['height'];keys=group['connections']
        if group.get('kind')=='chain':
            ids=[group['equipment_id']]+[edges[key]['load_id'] for key in keys];gap=34
            box_width=(w-gap*(len(ids)-1))/len(ids)
            boxes=[]
            for i,identity in enumerate(ids):
                bx=x+i*(box_width+gap);equipment_symbol(identity,bx,y+13,box_width,40)
                boxes.append((bx,y+13,bx+box_width,y+53))
            for i,key in enumerate(keys):
                a=(boxes[i][2],y+36);b=(boxes[i+1][0],y+36)
                feeder(key,[a,b],boxes[i],boxes[i+1])
                text((a[0]+b[0])/2,y+9,refs[key]['tag'],2.5,'middle')
                reference='REG '+(refs[key].get('register_sheet') or 'DELEGATED')
                if measure_sheet_text(reference,2.3,font_family=FONT)>gap-2:reference='REG INDEX'
                text((a[0]+b[0])/2,y+47,reference,2.3,'middle')
        else:
            parent=group['equipment_id']
            parent_bounds,children,bus_left,bus_right=fan_geometry(group)
            home=group['source_home_bounds_mm']
            if parent not in seen:
                equipment_symbol(parent,*home[:2],home[2]-home[0],home[3]-home[1],continued=False,lane=group['source_home_lane'])
            if not keys:continue
            bus_path=group['source_bus_path_mm'];bus_y=group['bus_row_mm'][1]
            bus_points=[*bus_path,(bus_left,bus_y),(bus_right,bus_y)]
            for a,b in zip(bus_path,bus_path[1:]):line(*a,*b,.65)
            line(bus_left,bus_y,bus_right,bus_y,.65)
            source_buses.append({'equipment_id':parent,'terminal_key':group['source_terminal_key'],'stroke_width_mm':.65,'connection_ids':keys,'path_mm':bus_path,'row_mm':group['bus_row_mm']})
            text(parent_bounds[0]+2,bus_y-5,group['source_bus_caption'],2.3)
            for i,key in enumerate(keys):
                child=group['target_home_bounds_mm'][key];cw=child[2]-child[0];target=edges[key]['load_id']
                if target not in seen:equipment_symbol(target,child[0],child[1],cw,child[3]-child[1],lane=group['target_home_lanes'][key])
                row_path=group['feeder_paths_mm'][key]
                feeder(key,row_path,home,child,private_start=group['private_branch_index'][key])
                for j,value in enumerate(edges[key]['drawing_labels'][:2]):
                    if measure_sheet_text(value,2.5,font_family=FONT)>cw-8:
                        value=refs[key]['tag']+' / REG '+(refs[key].get('register_sheet') or 'DELEGATED')
                    label_lane=symbol_lane(children[i])
                    if measure_sheet_text(value,2.5,font_family=FONT)>label_lane['text_width']:value=refs[key]['tag'] if j==0 else 'REG '+(refs[key].get('register_sheet') or 'DELEGATED')
                    if measure_sheet_text(value,2.5,font_family=FONT)>label_lane['text_width']:value=refs[key]['tag'] if j==0 else 'REG INDEX'
                    text(label_lane['text_left']+2,y+50+j*3.4,value)
    if page.get('notes_bounds'):
        nl,nt,nr,nb=page['notes_bounds'];line(nl+.2,nt+.2,nl+.2,nb-.2,.22);section(2,'ONE-LINE NOTES / REFERENCES',nl,nt+1,nr-nl)
        for i,value in enumerate(page['note_lines']):text(nl+2,nt+23+i*4.6,value,3.2)
    for i,value in enumerate(page.get('reference_lines',[])):text(left+3,top+30+i*4.6,value,3.2)
    if page.get('schedule_bounds'):
        sl,st,sr,sb=page['schedule_bounds'];section(3,'FEEDER SCHEDULE',sl+2,st,sr-sl-4)
    for grid in page.get('feeder_grids',[]):
        root.append(ET.fromstring(grid['body_svg']))
    for row in page.get('entries',[]):
        x,y=row['x'],row['y'];line(x,y,x+row['width'],y,.13)
        for i,value in enumerate(row['lines']):text(x,y+3.3+i*3.4,value)
    index='SHEET INDEX: '+', '.join(p['id'] for p in layout['sheet_index'])
    for i,value in enumerate(wrap_mm(index,right-left-6,size=2.3)):
        if i>=3:raise ValueError('electrical sheet index requires more footer space')
        text(left+3,bottom-9+i*2.8,value,2.3)
    # Fail closed even if a future layout regression misses a bar crossing.
    for path in paths:
        network_kind=edges[path['connection_id']]['drafting']['connection_kind']
        for bar in glyphs:
            if bar['kind']!='terminal_bar' or bar['network_kind']==network_kind:continue
            if any(_segments_cross(a,b,c,d) for a,b in zip(path['points_mm'],path['points_mm'][1:]) for c,d in bar['segments_mm']):
                raise ValueError(f'{path["connection_id"]}: drawn {network_kind} conductor intersects {bar["network_kind"]} terminal on {bar["source_id"]}; diagram refused')
    for i,bus in enumerate(source_buses):
        segments=[*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
        boxes=[]
        for symbol in symbols:
            if symbol['equipment_id']==bus['equipment_id'] or not symbol['enclosure_bounds_mm']:continue
            l,t,r,b=symbol['enclosure_bounds_mm'];pad=(bus['stroke_width_mm']+symbol['enclosure_stroke_width_mm'])/2
            boxes.append((l-pad,t-pad,r+pad,b+pad))
        other_segments=[]
        for other in source_buses[:i]:
            if other['terminal_key']==bus['terminal_key']:continue
            other_segments.extend(zip(other['path_mm'],other['path_mm'][1:]));other_segments.append((other['row_mm'][:2],other['row_mm'][2:]))
        if not all(_paper_clear_segment(a,b,boxes,other_segments) for a,b in segments):
            raise ValueError(f'{bus["equipment_id"]}: shared bus ink intersects unrelated enclosure or terminal network; diagram refused')
    return ET.tostring(root,encoding='unicode'),{'sheet_id':page['id'],'kind':page['kind'],'terminal_occurrences':terminals,
        'equipment_symbols':symbols,'source_buses':source_buses,'connection_paths':paths,'text_bounds':labels,'electrical_symbols':glyphs,'drafting_style':'electrical-bus-v1',
        'feeder_references':page.get('feeder_references',[]),'terminal_references':page.get('terminal_references',[]),
        'feeder_grid_rows':[row for grid in page.get('feeder_grids',[]) for row in grid['row_map']],'feeder_grid_engine':[grid['engine'] for grid in page.get('feeder_grids',[])]}
