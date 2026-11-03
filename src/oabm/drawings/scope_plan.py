"""Dedicated SVG scope styles; metadata alone is never used as visible styling."""
from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET

SVG = 'http://www.w3.org/2000/svg'
# Shape plus literal token distinguish classes even in monochrome output.
STYLES = {
    'new': ('N', '#006b38', 'INSTALL - new'),
    'existing_to_remain': ('E', '#666666', 'RETAIN - existing context'),
    'removed': ('D', '#b00020', 'DEMOLISH - removed'),
    'relocated': ('R', '#005ea8', 'RELOCATE - supplied position only'),
    'unresolved': ('?', '#9c4500', 'UNRESOLVED - no work assigned'),
}


def _node(parent, tag, **attributes):
    return ET.SubElement(parent, '{' + SVG + '}' + tag,
                         {k.replace('_', '-'): str(v) for k, v in attributes.items()})


def _text(parent, x, y, value, *, size=13, **attrs):
    node = _node(parent, 'text', x=x, y=y, font_size=size, fill='black',
                 font_family='DejaVu Sans', **attrs)
    node.text = value
    return node


def _mark(parent, status, x, y):
    token, color, _ = STYLES[status]
    if status == 'unresolved':
        _node(parent, 'polygon', points=f'{x},{y-13} {x+13},{y} {x},{y+13} {x-13},{y}',
              fill='white', stroke=color, stroke_width=2.5)
    else:
        node = _node(parent, 'rect', x=x-11, y=y-11, width=22, height=22,
            fill='white', stroke=color, stroke_width=2)
        if status == 'relocated':
            node.set('stroke-dasharray', '4 2')
        if status == 'removed':
            for a, b in ((-1, 1), (1, -1)):
                _node(parent, 'line', x1=x-16, y1=y+16*a, x2=x+16, y2=y+16*b,
                      stroke=color, stroke_width=2.5)
        if status == 'new':
            _node(parent, 'rect', x=x-15, y=y-15, width=30, height=30,
                  fill='none', stroke=color, stroke_width=2)
    _text(parent, x, y+5, token, size=14, text_anchor='middle')


def scope_plan_svg(raw, view, *, plan, pixels_per_model_unit, objects, ledger):
    root = ET.fromstring(raw)
    width, height = map(float, root.attrib['viewBox'].split()[2:])
    width = max(width, 1000)
    root.set('viewBox', f'0 0 {width} {height+290}')
    root.set('width', str(width)); root.set('height', str(height+290))
    rows = {r.source.id: r for r in ledger.rows}
    # Base review backend alone decides whether real geometry was projected.
    shown = {identity for p in view['primitives'] if p['kind'] != 'text'
             for identity in p['source_ids'] if identity in rows}
    for node in root.iter():
        ids = node.get('data-source-ids', '').split()
        matched = [identity for identity in ids if identity in rows]
        if len(matched) == 1:
            status = rows[matched[0]].effective_status
            node.set('data-scope-status', status)
            node.set('style', f'color:{STYLES[status][1]}')
            if node.tag.rsplit('}', 1)[-1] != 'text':
                node.set('stroke-width', '2' if status in ('new', 'removed', 'unresolved') else '1')
                if status in ('removed', 'relocated'):
                    node.set('stroke-dasharray', '5 3')
    # Marks anchor to supplied canonical poses only. Offset text is annotation.
    overlay = _node(root, 'g', id='scope-marks', transform='translate(0 64)')
    marked = []
    for obj in sorted(objects, key=lambda o: o.id):
        p = obj.pose.position
        if obj.id not in shown or not (plan.bounds_m[0] <= p.x <= plan.bounds_m[2]
                                      and plan.bounds_m[1] <= p.y <= plan.bounds_m[3]):
            continue
        x = (p.x-plan.bounds_m[0])*pixels_per_model_unit
        y = (plan.bounds_m[3]-p.y)*pixels_per_model_unit
        status = rows[obj.id].effective_status
        group = _node(overlay, 'g', data_source_ids=obj.id, data_scope_status=status,
                      data_anchor_model_x=p.x, data_anchor_model_y=p.y)
        _mark(group, status, x, y)
        _text(group, x+20, y-19, obj.id, size=12)
        title = _node(group, 'title')
        title.text = STYLES[status][2] + '; ' + (rows[obj.id].diagnostic_reason or rows[obj.id].derivation)
        marked.append(obj.id)
    legend = _node(root, 'g', id='scope-legend')
    _node(legend, 'rect', x=0, y=height, width=width, height=290, fill='white')
    _text(legend, 20, height+24, 'OBJECT SCOPE REVIEW - symbols are annotations, not physical dimensions', size=15)
    for i, status in enumerate(STYLES):
        y = height+54+i*32
        _mark(legend, status, 35, y)
        count = sum(r.effective_status == status for r in ledger.rows)
        _text(legend, 62, y+5, f'{STYLES[status][2]} | model count {count}')
    _text(legend, 20, height+226, 'Relocated: origin/destination geometry unavailable; no arrows or extra positions supplied.')
    _text(legend, 20, height+248, 'Scope applies only to devices/equipment. Circuits/raceways remain unclassified context.')
    _text(legend, 20, height+270, 'No safe-off, pricing, topology or permit-readiness determination. See scope schedules and ledger JSON.')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), {
        'view_id': plan.id, 'marked_ids': marked,
        'unmarked_ids': sorted(rows.keys()-set(marked)),
        'unmarked_meaning': 'No backend geometry or supplied anchor outside requested window; never repositioned.'}


def scope_schedule_svg(schedules, ledger):
    # Full existing table cells, wrapped losslessly; no shortened IDs/evidence.
    width = 2200
    root = ET.Element('{' + SVG + '}svg', {'viewBox': f'0 0 {width} 1'})
    y = 28
    _text(root, 16, y, 'INCOMPLETE SCOPE REVIEW - NOT FOR PERMIT OR CONSTRUCTION', size=18)
    y += 28
    _text(root, 16, y, 'Model binding: ' + ledger.source_model_fingerprint)
    for schedule_index, schedule in enumerate(schedules):
        y += 34
        _text(root, 16, y, schedule.title, size=16)
        col_width = (width-32)/len(schedule.columns)
        y += 26
        for i, column in enumerate(schedule.columns):
            for j, line in enumerate(textwrap.wrap(column.title, width=int(col_width/8))):
                _text(root, 16+i*col_width, y+j*17, line)
        y += 44
        for row_index, row in enumerate(schedule.rows):
            source_ids = (row.source_id,) if schedule_index == 0 else ledger.groups[row_index].object_ids
            group = _node(root, 'g', data_source_ids=' '.join(source_ids),
                          data_schedule_row_id=row.source_id, data_schedule_id=schedule.id)
            lines = [textwrap.wrap(cell, width=int(col_width/8), replace_whitespace=False,
                       drop_whitespace=False) or [''] for cell in row.cells]
            count = max(map(len, lines))
            for i, cell in enumerate(lines):
                for j, line in enumerate(cell):
                    _text(group, 16+i*col_width, y+j*17, line, size=12)
            y += count*17+12
            _node(root, 'line', x1=16, x2=width-16, y1=y-5, y2=y-5, stroke='#888888')
    y += 24
    root.set('viewBox', f'0 0 {width} {y}')
    root.set('width', str(width)); root.set('height', str(y))
    background = ET.Element('{' + SVG + '}rect', {'width': str(width), 'height': str(y), 'fill': 'white'})
    root.insert(0, background)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
