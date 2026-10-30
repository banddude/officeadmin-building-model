"""Network feeder rows adapted to the schedule owner's shared grid API.

No panel slots, circuit pairing, connected-load totals, demand or protective
ratings are calculated here. This adapter formats exact supplied feeder facts.
"""
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from dataclasses import replace
from .layout import wrap_mm
from ..model import DrawingSet,DrawingSchedule,ScheduleColumn,ScheduleRow,SourceReference,SourceProvenance
from .drafting_facts import protection_label,status_tag


def paginate_feeder_grid(topology,connection_ids,bounds_mm, *, sheet_id,width_mm,height_mm):
    try:
        from ..schedule_layout import ScheduleLayout,paginate_schedules
    except ImportError as error:
        raise ImportError('Electrical feeder grid requires the shared schedule_layout module from the integration/schedule owner; no independent grid fallback is provided') from error
    edges={edge['id']:edge for edge in topology.connections}
    tags={edge['id']:f'C{i:03d}' for i,edge in enumerate(topology.connections,1)}
    count={}
    for edge in topology.connections:count[edge['circuit_id']]=count.get(edge['circuit_id'],0)+1
    columns=tuple(ScheduleColumn(key,label) for key,label in (
        ('tag','ID / FEEDER'),('equipment','FROM / TO'),('voltage','V / POLES / PHASE'),
        ('protection','PROTECTION'),('conductors','CONDUCTORS'),('notes','NOTES')))
    rows=[];bindings={}
    for identity in connection_ids:
        edge=edges[identity]
        source=edge['circuit_id'] if count[edge['circuit_id']]==1 else edge['load_port_id']
        if source in bindings:raise ValueError(f'{identity}: shared grid requires distinct canonical row sources; resolve duplicate load-port assignment')
        bindings[source]=identity
        facts=edge['drafting'];kind=facts['connection_kind']
        voltage=(f'{edge["voltage_v"]:g} V' if edge['voltage_v'] is not None else 'V UNKNOWN') if kind=='power' else kind.upper()
        if kind=='power':voltage+=f' / {edge["poles"] if edge["poles"] is not None else "?"}P / {edge["phase"] or "?"}'
        notes='; '.join([*facts['notes'], 'ROUTE '+(', '.join(edge['route_ids']) or 'UNKNOWN / not supplied')])
        rows.append(ScheduleRow(source,(tags[identity]+' / '+edge['circuit_id'],
            status_tag(facts['status'])+' '+edge['source_id']+' / '+edge['source_port_id']+' ('+edge['source_role']+') -> '+edge['load_id']+' / '+edge['load_port_id']+' ('+edge['load_role']+')'+('\nSUPPLIED LABEL: '+edge['circuit_number'] if edge.get('circuit_number') is not None else ''),voltage,
            protection_label(facts) if kind=='power' else 'NOT A PHASE FEEDER',edge['conductor_specification'],notes)))
    sources=[]
    for entry in topology.source_index:
        entity=entry['entity']
        records=tuple(SourceProvenance(p['source_kind'],p['source_id'],p.get('source_element_id'),p.get('page'),p.get('method'),p.get('confidence',1),p.get('derivation')) for p in entity.get('provenance',[]))
        sources.append(SourceReference(entity['id'],entity.get('name'),entity.get('confidence',1),records))
    drawing=DrawingSet('single-line:'+topology.model_sha256,(),(DrawingSchedule('single-line-feeders','SUPPLIED FEEDER FACTS',columns,tuple(rows)),),tuple(sources))
    template=SimpleNamespace(id=sheet_id,title='FEEDER SCHEDULE',width_mm=width_mm,height_mm=height_mm,drawing_bounds_mm=bounds_mm)
    usable=bounds_mm[2]-bounds_mm[0]-4.01
    fractions=(.09,.29,.12,.17,.23,.10)
    layout=ScheduleLayout(text_height_mm=3.2,line_height_mm=5,padding_mm=1.5,
        column_widths_mm={column.key:usable*fraction for column,fraction in zip(columns,fractions)})
    # Keep shared table ownership; pre-wrap values losslessly at word boundaries
    # because the shared generic grid currently wraps by glyph capacity.
    wrapped_rows=tuple(replace(row,cells=tuple('\n'.join(part for line in value.split('\n') for part in wrap_mm(line,layout.column_widths_mm[column.key]-2*layout.padding_mm,size=layout.text_height_mm)) for column,value in zip(columns,row.cells))) for row in rows)
    drawing=replace(drawing,schedules=(replace(drawing.schedules[0],rows=wrapped_rows),))
    pages=paginate_schedules(drawing,template,layout=layout)
    result=[]
    for page in pages:
        maps=[{**row,'connection_id':bindings[row['source_id']]} for row in page.row_map]
        view=page.drawing_set.views[0]
        footer=[primitive for primitive in view.primitives if dict(primitive.metadata).get('role')=='page_reference' and primitive.kind=='text']
        if not footer:raise ValueError('shared schedule renderer did not expose its reserved page-reference footer')
        footer_points=[(page.placement.x_mm+p.points[0].x*1000,page.placement.y_mm+(view.bounds.max_y-p.points[0].y)*1000) for p in footer]
        result.append({'body_svg':page.body_svg(layout),'row_map':maps,
                       'footer_points_mm':footer_points,'footer_width_mm':usable,
                       'footer_text_height_mm':layout.text_height_mm,'footer_line_height_mm':layout.line_height_mm,
                       'connection_ids':[row['connection_id'] for row in maps],
                       'engine':'oabm.drawings.schedule_layout.paginate_schedules'})
    return result


def resolve_register_footer(grid,sheet_id,previous_sheet,next_sheet):
    """Fill the shared paginator's existing reserved footer with final IDs."""
    root=ET.fromstring(grid['body_svg']);points=grid['footer_points_mm'];template=None
    for node in list(root):
        if node.tag.split('}')[-1]!='text':continue
        xy=(float(node.get('x')),float(node.get('y')))
        if any(abs(xy[0]-p[0])<1e-6 and abs(xy[1]-p[1])<1e-6 for p in points):
            if template is None:template=dict(node.attrib)
            root.remove(node)
    if template is None:raise ValueError('cannot locate the shared register footer text slots')
    labels=['REGISTER '+sheet_id]
    links=[]
    if previous_sheet:links.append('PREV '+previous_sheet)
    if next_sheet:links.append('NEXT '+next_sheet)
    if links:labels.append(' / '.join(links))
    lines=[line for label in labels for line in wrap_mm(label,grid['footer_width_mm']-3,size=grid['footer_text_height_mm'])]
    if len(lines)>2:raise ValueError('actual register-sheet links exceed the shared footer capacity; shorten IDs or enlarge the register region')
    x,y=points[0]
    for i,value in enumerate(lines):
        attrs={**template,'x':str(x),'y':str(y+i*grid['footer_line_height_mm'])}
        node=ET.SubElement(root,'{http://www.w3.org/2000/svg}text',attrs);node.text=value
    grid['body_svg']=ET.tostring(root,encoding='unicode')
    grid['register_sheet']=sheet_id;grid['previous_register_sheet']=previous_sheet;grid['next_register_sheet']=next_sheet
    grid['footer_lines']=lines
