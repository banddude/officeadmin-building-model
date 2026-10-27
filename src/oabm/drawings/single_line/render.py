"""Compact editable schematic bodies; shared company renderer owns stationery."""
from __future__ import annotations
import xml.etree.ElementTree as ET
from .layout import FONT, wrap_mm, backbone_geometry
from ..company_sheets import measure_sheet_text

NS='http://www.w3.org/2000/svg'
ET.register_namespace('',NS)


def render_body(topology,layout,page,sheet):
    root=ET.Element('{'+NS+'}g')
    left,top,right,bottom=sheet.drawing_bounds_mm
    equipment={e['id']:e for e in topology.equipment}
    edges={e['id']:e for e in topology.connections}
    refs={r['connection_id']:r for r in layout['connections']}
    occurrences=[]
    equipment_symbols=[]
    connection_paths=[]
    text_bounds=[]
    text_owner=None
    def node(kind,**attrs):
        return ET.SubElement(root,'{'+NS+'}'+kind,{k.replace('_','-'):str(v) for k,v in attrs.items()})
    def text(x,y,value,size=2.5,anchor='start'):
        n=node('text',x=x,y=y,font_size=size,text_anchor=anchor,font_family=FONT,fill='#000000')
        n.text=value
        occupied=measure_sheet_text(value,size,font_family=FONT)
        start=x-occupied*({'start':0,'middle':.5,'end':1}[anchor])
        text_bounds.append({'text':value,'font_size_mm':size,'equipment_id':text_owner,
                            'bounds_mm':(start,y-size,start+occupied,y+size*.3)})
        # Explicitly retain XML space semantics on the shared renderer boundary.
        return n
    def line(x1,y1,x2,y2,weight=.3):
        node('line',x1=x1,y1=y1,x2=x2,y2=y2,stroke='#000000',stroke_width=weight)
    def arrow(a,b):
        x,y=b;dx,dy=x-a[0],y-a[1]
        length=(dx*dx+dy*dy)**.5
        if not length:raise ValueError('single-line zero-length terminal arrow')
        ux,uy=dx/length,dy/length
        for side in (-1,1):line(x-2*ux+side*uy,y-2*uy-side*ux,x,y)
    def rectangle(x,y,w,h):
        node('rect',x=x,y=y,width=w,height=h,fill='#ffffff',stroke='#000000',stroke_width=.3)
    def symbol(kind,x,y):
        if kind in {'source','generator','meter'}:
            node('circle',cx=x,cy=y,r=3,fill='none',stroke='#000000',stroke_width=.3)
            text(x,y+.8,{'source':'S','generator':'G','meter':'M'}[kind],2.3,'middle')
        elif kind=='transformer':
            for dx in (-1.7,1.7):node('circle',cx=x+dx,cy=y,r=2.6,fill='none',stroke='#000000',stroke_width=.3)
        elif kind=='transfer_switch':
            line(x-4,y-3,x-1,y-3);line(x-4,y+3,x-1,y+3)
            line(x+1,y,x+4,y);line(x+1,y,x-1,y-1.5)
        elif kind=='disconnect':
            line(x-4,y,x-1,y);line(x+1,y,x+4,y);line(x-1,y,x+1,y-2)
        else:
            rectangle(x-3,y-4,6,8);line(x-1.5,y-2,x+1.5,y-2);line(x-1.5,y+2,x+1.5,y+2)
    def equipment_box(identity,x,y,w,h, *, caption=None,continued=False):
        nonlocal text_owner
        previous_owner=text_owner
        text_owner=identity
        e=equipment[identity]
        equipment_symbols.append({'equipment_id':identity,'bounds_mm':(x,y,x+w,y+h)})
        rectangle(x,y,w,h)
        if caption:
            if measure_sheet_text(caption,2.5,font_family=FONT)>w-4:
                raise ValueError(f'{identity}: terminal caption cannot fit its equipment box')
            text(x+2,y+4,caption,2.5)
        header_y=y+(9 if caption else 5)
        if measure_sheet_text(identity,2.5,font_family=FONT)>w-4:
            raise ValueError(f'{identity}: canonical ID cannot fit a schematic node; choose larger paper')
        text(x+2,header_y,identity)
        symbol(e['equipment_type'],x+7,y+h/2)
        names=wrap_mm(e['name'],w-18)
        if len(names)>4:
            raise ValueError(f'{identity}: equipment name exceeds available measured node label; use a larger sheet or a shorter explicit name')
        for i,value in enumerate(names):text(x+15,header_y+5+i*3.4,value,2.5)
        rated='UNKNOWN' if e['rated_voltage_v'] is None else f'{e["rated_voltage_v"]:g} V'
        text(x+2,y+h-7,'Rated '+rated,2.5)
        home=layout['equipment_home_sheets'][identity]
        label=('CONTINUED '+home) if continued or home!=page['id'] else e['equipment_type']
        if e['equipment_type']=='transfer_switch':label='STATE UNSPECIFIED; '+('FROM '+home if home!=page['id'] else 'TRANSFER')
        if not e['incoming'] and not e['outgoing']:label='ISOLATED / NO EXPLICIT CIRCUITS'
        if measure_sheet_text(label,2.3,font_family=FONT)>w-4:
            raise ValueError(f'{identity}: node status cannot fit; choose larger paper')
        text(x+2,y+h-2,label,2.3)
        text_owner=previous_owner
    def endpoints(key):
        ref=refs[key]
        for endpoint in ('source','load'):
            occurrences.append({'connection_id':key,'endpoint':endpoint,'sheet_id':page['id'],
                                'counterpart_sheet':page['id'],'tag':ref['tag']})
    ordinal=next(i for i,p in enumerate(layout['pages'],1) if p['id']==page['id'])
    text(left+3,top+6,'ELECTRICAL SINGLE-LINE / '+page['kind'].upper(),3.5)
    text(left+3,top+12,f'{page["id"]} | {ordinal}/{len(layout["pages"])} | NOT TO SCALE | Supplied facts only - no engineering approval.',2.5)
    text(left+3,top+18,'C-tags identify feeders. Arrow points to load. CONTINUED identifies the same canonical equipment.',2.5)
    # Full, connected small system or supply/transfer/transformer backbone.
    if page['nodes']:
        for geometry in backbone_geometry(page,edges,page['core_bounds']):
            key=geometry['connection_id'];edge=edges[key];ref=refs[key]
            points=geometry['points']
            connection_paths.append({'connection_id':key,'points_mm':points})
            for a,b in zip(points,points[1:]):line(*a,*b)
            arrow(points[-2],points[-1])
            voltage='V UNKNOWN' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
            lx,ly=geometry['label']
            text(lx,ly,ref['tag']+' / '+voltage,2.5)
            text(lx,ly+3.3,edge['load_role'],2.3)
            endpoints(key)
        for n in page['nodes']:
            equipment_box(n['equipment_id'],n['x']-n['width']/2,n['y'],n['width'],n['height'])
    # One equipment feeds its actual children. Chunking repeats the equipment
    # explicitly, instead of inserting a fabricated external distribution bus.
    for group in page['groups']:
        x,y,w=group['x'],group['y'],group['width']
        center=x+w/2
        parent=group['equipment_id']
        parent_width=min(130,w-8)
        equipment_box(parent,center-parent_width/2,y+3,parent_width,40,continued=True)
        count=len(group['connections'])
        pitch=w/max(1,count)
        for i,key in enumerate(group['connections']):
            edge,ref=edges[key],refs[key]
            xx=x+pitch*(i+.5)
            start=(center+(i-(count-1)/2)*5,y+43)
            end=(xx,y+96)
            line(*start,*end);arrow(start,end)
            connection_paths.append({'connection_id':key,'points_mm':[start,end]})
            voltage='V UNKNOWN' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
            child_width=min(100,pitch-5)
            equipment_box(edge['load_id'],xx-child_width/2,y+96,child_width,45,
                          caption=ref['tag']+' / '+voltage)
            endpoints(key)
        line(x+1,y+146,x+w-1,y+146,.15)
    for row in page.get('entries',[]):
        x,y=row['x'],row['y']
        line(x,y,x+row['width'],y,.15)
        for i,value in enumerate(row['lines']):text(x,y+3.3+i*3.4,value)
    # Compact on-sheet index replaces a compulsory standalone index sheet.
    index='SHEET INDEX: '+', '.join(p['id'] for p in layout['sheet_index'])
    lines=wrap_mm(index,right-left-6,size=2.3)
    if len(lines)>3:raise ValueError('single-line sheet index exceeds reserved footer; choose a larger sheet or bounded subset')
    for i,value in enumerate(lines):text(left+3,bottom-9+i*2.8,value,2.3)
    return ET.tostring(root,encoding='unicode'),{'sheet_id':page['id'],'kind':page['kind'],
                                             'terminal_occurrences':occurrences,'equipment_symbols':equipment_symbols,
                                             'connection_paths':connection_paths,'text_bounds':text_bounds}
