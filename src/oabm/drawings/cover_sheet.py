"""Supplied project facts to a bounded cover body on shared company stationery."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path

from .blank_set import _decode, _json, _text as _single_line, _identifier
from .sheet_svg import _xml, _fmt
from .company_sheets import measure_sheet_text, validate_sheet_text, SheetImageAsset
from .cover_maps import CoverMapSnapshot, acquire_cover_map

VERSION = 'cover-sheet-v1'
SUPPORTED_FONT = 'DejaVu Sans'
def _text(value, label):
    _single_line(value, label)
    try:
        return validate_sheet_text(value,font_family=SUPPORTED_FONT)
    except ValueError as exc:
        raise ValueError(f'{label}: {exc}; source text was not changed') from exc


@lru_cache(maxsize=8192)
def _wrap_text(value, width_mm, size_mm, family, weight=400):
    """Wrap with the shared measured ink/advance contract, preserving all text."""
    def fits(text):
        return measure_sheet_text(text, size_mm, font_family=family, font_weight=weight) <= width_mm
    lines = []
    while value:
        if not fits(value[0]):
            raise CoverCapacityError('body: insufficient width; one supported glyph exceeds panel width')
        low, high = 1, 2
        while high < len(value) and fits(value[:high]):
            low, high = high, high * 2
        high = min(high, len(value))
        while low < high:
            middle = (low + high + 1) // 2
            if fits(value[:middle]):
                low = middle
            else:
                high = middle - 1
        count = low
        if count < len(value):
            split = value.rfind(' ', 0, count)
            if split > 0:
                count = split + 1
        lines.append(value[:count])
        value = value[count:]
    return tuple(lines)


class CoverCapacityError(ValueError):
    """All content is retained in the request; capacity failures publish nothing."""


def validate_cover(config):
    required = {'version', 'id', 'project', 'scope', 'sheets', 'contacts'}
    optional = {'layout', 'notes', 'location_map', 'pagination', 'code_basis', 'existing_system', 'linework_conventions', 'key_abbreviations'}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - optional:
        raise ValueError('cover: required version, id, project, scope, sheets, contacts; optional notes/location_map/pagination')
    if config['version'] != VERSION:
        raise ValueError('unsupported cover version')
    if config.get('layout', 'flow') not in ('flow', 'reference-grid'):
        raise ValueError('layout: flow or reference-grid required')
    _identifier(config['id'], 'id')
    project = config['project']
    if not isinstance(project, dict) or not {'name', 'location'} <= project.keys():
        raise ValueError('project: explicit name and location required')
    for key, value in project.items():
        _text(key, 'project field')
        _text(value, 'project.' + key)
    for key in ('scope', 'sheets', 'contacts'):
        if not isinstance(config[key], list) or not config[key]:
            raise ValueError(key + ': supply a nonempty ordered list')
    for i, value in enumerate(config['scope']):
        _text(value, f'scope[{i}]')
    seen = set()
    for row in config['sheets']:
        if not isinstance(row, dict) or set(row) != {'id', 'title'}:
            raise ValueError('sheets: each row needs id and title')
        _identifier(row['id'], 'sheet id')
        _text(row['title'], 'sheet title')
        if row['id'].casefold() in seen:
            raise ValueError('duplicate sheet id')
        seen.add(row['id'].casefold())
    if config['id'].casefold() not in seen:
        raise ValueError('sheet index must include the cover id')
    for row in config['contacts']:
        if not isinstance(row, dict) or not {'role', 'name'} <= row.keys() or row.keys() - {'role', 'name', 'phone', 'email', 'address'}:
            raise ValueError('contacts: role/name required; phone/email/address optional')
        for key, value in row.items():
            _text(value, 'contact.' + key)
    for section in ('code_basis', 'existing_system', 'linework_conventions', 'key_abbreviations'):
        if section in config:
            if not isinstance(config[section], list):
                raise ValueError(section + ': expected ordered list of supplied facts')
            for i, value in enumerate(config[section]):
                _text(value, f'{section}[{i}]')
    if 'notes' in config:
        if not isinstance(config['notes'], list):
            raise ValueError('notes: expected list')
        for value in config['notes']:
            _text(value, 'note')
    if 'pagination' in config:
        options = config['pagination']
        if not isinstance(options, dict) or options.keys() - {'continuation_ids', 'continuation_id_prefix', 'continuation_title', 'text_height_mm'}:
            raise ValueError('pagination: continuation_ids, continuation_id_prefix, continuation_title, text_height_mm only')
        if 'continuation_ids' in options:
            if not isinstance(options['continuation_ids'], list):
                raise ValueError('pagination.continuation_ids: expected ordered list')
            ids = options['continuation_ids']
            for identity in ids:
                _identifier(identity, 'continuation id')
            if len({v.casefold() for v in ids}) != len(ids):
                raise ValueError('duplicate continuation id')
        if 'continuation_id_prefix' in options:
            _identifier(options['continuation_id_prefix'], 'continuation_id_prefix')
        if 'continuation_title' in options:
            _text(options['continuation_title'], 'continuation_title')
        size = options.get('text_height_mm', 3.0)
        if type(size) not in (float, int) or not math.isfinite(size) or size < 2.5:
            raise ValueError('pagination.text_height_mm: finite size of at least 2.5 mm required')
    if 'location_map' in config:
        m = config['location_map']
        if not isinstance(m, dict) or not {'caption','width','height','segments'} <= m.keys() or m.keys()-{'caption','width','height','segments','labels','site_marker'}:
            raise ValueError('location_map: caption, width, height, segments required')
        _text(m['caption'], 'map caption')
        for key in ('width', 'height'):
            if type(m[key]) not in (int, float) or not math.isfinite(m[key]) or m[key] <= 0:
                raise ValueError('map dimensions must be finite positive numbers')
        if not isinstance(m['segments'], list) or not m['segments']:
            raise ValueError('map: supply explicit vector segments')
        for segment in m['segments']:
            if not isinstance(segment, list) or len(segment) != 4:
                raise ValueError('map segments: [x1,y1,x2,y2] required')
            for i, value in enumerate(segment):
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= m['width' if i % 2 == 0 else 'height']:
                    raise ValueError('map segment outside supplied extent')
        def map_point(row, label):
            for key, maximum in (('x',m['width']),('y',m['height'])):
                value=row[key]
                if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=maximum:
                    raise ValueError(label+': coordinates outside supplied map extent')
        if 'labels' in m:
            if not isinstance(m['labels'],list):
                raise ValueError('location_map.labels: expected ordered list')
            for i,row in enumerate(m['labels']):
                if not isinstance(row,dict) or set(row)!={'x','y','text'}:
                    raise ValueError('map label: x, y and text required')
                _text(row['text'],f'location_map.labels[{i}].text')
                map_point(row,'map label')
        if 'site_marker' in m:
            row=m['site_marker']
            if not isinstance(row,dict) or set(row)!={'x','y','label'}:
                raise ValueError('site_marker: x, y and explicit label required')
            _text(row['label'],'location_map.site_marker.label')
            map_point(row,'site marker')
    return json.loads(_json(config))


@dataclass(frozen=True)
class CoverBody:
    svg_elements: str
    layout: dict
    images: tuple[SheetImageAsset, ...] = ()


def compose_cover_body(config, bounds_mm, *, font_family='DejaVu Sans', text_height_mm=3.0):
    """Compose one drafting-style cover body; use paginate_cover for long input."""
    config = validate_cover(config)
    if len(bounds_mm) != 4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds_mm):
        raise ValueError('body bounds require four finite millimetre coordinates')
    left,top,right,bottom = bounds_mm
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError('body bounds must be a positive rectangle')
    if type(text_height_mm) not in (int,float) or not math.isfinite(text_height_mm) or text_height_mm < 2.5:
        raise ValueError('text height must be at least 2.5 mm')
    if font_family != SUPPORTED_FONT:
        raise ValueError('cover body supports only DejaVu Sans with shared measured text layout')
    pages = _flow_cover(config,bounds_mm,text_height_mm)
    if len(pages) != 1:
        raise CoverCapacityError(f'cover needs {len(pages)} pages; use paginate_cover for complete output')
    return pages[0]


@dataclass(frozen=True)
class CoverPage:
    id: str
    title: str
    body_svg: str
    layout: dict
    images: tuple[SheetImageAsset, ...] = ()


@dataclass(frozen=True)
class CoverPagination:
    pages: tuple[CoverPage, ...]
    sheet_index: tuple[dict, ...]
    iterations: int


def _flow_cover(config, bounds, size, map_snapshot=None):
    """Drafting hierarchy, open regions and exact source spans across pages."""
    left,top,right,bottom = bounds
    inset=max(3.0,size*2.5)
    left,top,right,bottom = left+.25+inset,top+.25,right-.25-inset,bottom-.25
    pad = 4.0
    gutter = max(10.0,(right-left)*.045)
    width = (right-left-gutter)/2
    text_width = width-2*pad
    line_height = size*1.6
    heading_size = size*1.25
    radius = size*.9
    if text_width <= 0:
        raise CoverCapacityError('body: insufficient width for drafting regions')
    def text(value,x,y,*,height=size,weight=400,anchor='start'):
        return f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-family="{SUPPORTED_FONT}" font-size="{_fmt(height)}" font-weight="{weight}" text-anchor="{anchor}" fill="#000000" xml:space="preserve">{_xml(value)}</text>'
    def rule(x,y,length,weight=.25):
        return f'<line x1="{_fmt(x)}" y1="{_fmt(y)}" x2="{_fmt(x+length)}" y2="{_fmt(y)}" stroke="#000000" stroke-width="{_fmt(weight)}"/>'
    title_size = max(size*2,min(10.5,(right-left)*.015))
    title_lines = _wrap_text(config['project']['name'],right-left-2*pad,title_size,SUPPORTED_FONT,600)
    # A very long supplied name remains complete in Project details; a generic
    # banner avoids shrinking or clipping that fact merely to decorate a page.
    if len(title_lines)>3:
        title_lines = ['PROJECT COVER']
    title_y = top+max(6,size*3)+title_size
    header_elements = [text(value,left+pad,title_y+i*title_size*1.3,height=title_size,weight=600)
                       for i,value in enumerate(title_lines)]
    subtitle_y = title_y+(len(title_lines)-1)*title_size*1.3+size*2.2
    location_lines = _wrap_text(config['project']['location'],right-left-2*pad,size,SUPPORTED_FONT)
    if len(location_lines)<=2:
        header_elements.extend(text(value,left+pad,subtitle_y+i*line_height) for i,value in enumerate(location_lines))
        subtitle_y += (len(location_lines)-1)*line_height
    header_bottom = subtitle_y+size*2
    header_elements.append(rule(left,header_bottom,right-left,.45))
    first_top = header_bottom+size*4
    continuation_top = top+size*5
    if first_top+size*12 >= bottom:
        raise CoverCapacityError('body: insufficient width/height for heading and drafting regions')
    grid = config.get('layout') == 'reference-grid'
    # A reusable proportioned cover, derived only from the shared drawing area.
    # Narrow sheets retain the readable flow arrangement, rather than shrinking.
    grid = grid and right-left >= 500 and bottom-first_top >= 300
    grid_regions = {}
    if grid:
        w, h = right-left, bottom-first_top
        gap = max(8., size*3)
        def region(x0, y0, x1, y1):
            return (left+w*x0+(gap/2 if x0 else 0), first_top+h*y0,
                    left+w*x1-(gap/2 if x1<1 else 0), first_top+h*y1-gap)
        grid_regions = {
            'Project': region(0,0,.23,.24),
            'Contacts': region(.23,0,.52,.24),
            'Sheet index': region(.52,0,1,.19),
            'Key abbreviations': region(.52,.19,1,.29),
            'Scope': region(0,.29,.48,.46),
            'Code basis': region(.48,.29,.74,.46),
            'Existing system': region(.74,.29,1,.46),
            'Job notes': region(0,.49,.48,.79),
            'Linework conventions': region(0,.81,.48,1),
            'Location map': region(.48,.49,1,1),
        }
    standard_width = width
    # Row fragments are wrapped to the smaller of first-page and continuation
    # widths. This preserves source offsets unchanged when flowing onward.
    def set_width(title, index=0):
        nonlocal width, text_width, project_label_width, table_index
        width = grid_regions[title][2]-grid_regions[title][0] if grid and index==0 else standard_width
        text_width = min(width, standard_width)-2*pad
        project_label_width=min(text_width*.35,size*9)
        table_index=id_width < text_width*.45

    project_labels = {'name':'Project','location':'Location','number':'Project no.'}
    project_keys=[k for k in ('name','location','number') if k in config['project']]+sorted(set(config['project'])-{'name','location','number'})
    sections = [
        ('Project',1,[(f'project.{k}',project_labels.get(k,k)+': '+config['project'][k]) for k in project_keys],0),
        ('Contacts',2,[(f'contacts[{i}]','') for i in range(len(config['contacts']))],1),
        ('Scope',3,[(f'scope[{i}]',v) for i,v in enumerate(config['scope'])],0),
        ('Sheet index',4,[(f'sheets[{i}]',f"{r['id']}  {r['title']}") for i,r in enumerate(config['sheets'])],1)]
    if config.get('notes'):
        sections.append(('Job notes',5,[(f'notes[{i}]',v) for i,v in enumerate(config['notes'])],0))
    for key, label, number, column in (
        ('code_basis', 'Code basis', 7, 0),
        ('existing_system', 'Existing system', 8, 0),
        ('linework_conventions', 'Linework conventions', 9, 1),
        ('key_abbreviations', 'Key abbreviations', 10, 1)):
        if config.get(key):
            sections.append((label, number, [(f'{key}[{i}]', value) for i, value in enumerate(config[key])], column))
    if 'location_map' in config:
        map_facts=[('location_map.caption',config['location_map']['caption'])]
        map_facts.extend((f'location_map.labels[{i}].text',f"L{i+1}  {row['text']}") for i,row in enumerate(config['location_map'].get('labels',[])))
        if 'site_marker' in config['location_map']:
            map_facts.append(('location_map.site_marker.label','SITE  '+config['location_map']['site_marker']['label']))
        sections.append(('Location map',6,map_facts,1))
    if map_snapshot is not None:
        sections.append(('Location map',6,[
            ('location_map.provider','Apple Maps - VICINITY ONLY'),
            ('location_map.resolved_address','Resolved address: '+map_snapshot.provenance['resolved_address']),
            ('location_map.attribution',map_snapshot.attribution),
            ('location_map.marker_status','Site marker pending verified geographic image bounds.')],1))
    if grid:
        numbering={'Project':1,'Contacts':2,'Sheet index':3,'Scope':4,'Code basis':5,
                   'Existing system':6,'Job notes':7,'Linework conventions':8,'Location map':9,'Key abbreviations':10}
        sections=[(title,numbering[title],facts,column) for title,_,facts,column in sections]
    id_width = max(size*10,max(measure_sheet_text(r['id'],size,font_family=SUPPORTED_FONT,font_weight=600)+size*2 for r in config['sheets']))
    table_index = id_width < text_width*.45
    project_label_width=min(text_width*.35,size*9)
    bullet_indent=size*1.1
    source_text,prepared = {},[]
    for title,number,facts,column in sections:
        set_width(title)
        rows=[]
        for source,value in facts:
            offset=0
            if source.startswith('contacts['):
                entry=config['contacts'][int(source[9:-1])]
                parts=[(entry['role']+': '+entry['name'],600)]
                parts.extend(((('Tel: ' if key=='phone' else 'Address: ' if key=='address' else '')+entry[key]),400)
                             for key in ('phone','email','address') if key in entry)
                value='\n'.join(part[0] for part in parts)
                for part_index,(part_value,weight) in enumerate(parts):
                    fragments=_wrap_text(part_value,text_width,size,SUPPORTED_FONT,weight)
                    for i,fragment in enumerate(fragments):
                        row_text=fragment+('\n' if i==len(fragments)-1 and part_index<len(parts)-1 else '')
                        rows.append({'source':source,'offset':offset,'text':row_text,'display':fragment,'weight':weight})
                        offset+=len(row_text)
                rows[-1]['after_gap']=line_height*.35
            elif source.startswith('project.'):
                key=source[8:];label=project_labels.get(key,key)+': '
                supplied=config['project'][key]
                if measure_sheet_text(label,size,font_family=SUPPORTED_FONT,font_weight=600)<=project_label_width:
                    for i,fragment in enumerate(_wrap_text(supplied,text_width-project_label_width,size,SUPPORTED_FONT)):
                        row_text=(label if i==0 else '')+fragment
                        rows.append({'source':source,'offset':offset,'text':row_text,
                                     'project_label':label if i==0 else '', 'project_value':fragment})
                        offset+=len(row_text)
                else:
                    for fragment in _wrap_text(label,text_width,size,SUPPORTED_FONT,600):
                        rows.append({'source':source,'offset':offset,'text':fragment,'weight':600})
                        offset+=len(fragment)
                    for fragment in _wrap_text(supplied,text_width,size,SUPPORTED_FONT):
                        rows.append({'source':source,'offset':offset,'text':fragment})
                        offset+=len(fragment)
            elif source.startswith('sheets[') and table_index:
                entry=config['sheets'][int(source[7:-1])]
                fragments=_wrap_text(entry['title'],text_width-id_width,size,SUPPORTED_FONT)
                for i,fragment in enumerate(fragments):
                    row_text=(entry['id']+'  ' if i==0 else '')+fragment
                    rows.append({'source':source,'offset':offset,'text':row_text,
                                 'index_id':entry['id'] if i==0 else '', 'index_title':fragment})
                    offset+=len(row_text)
            else:
                indent=bullet_indent if source.startswith(('scope[','notes[')) else 0
                for fragment in _wrap_text(value,text_width-indent,size,SUPPORTED_FONT):
                    rows.append({'source':source,'offset':offset,'text':fragment,'indent':indent})
                    offset+=len(fragment)
            source_text[source]=value
        prepared.append((title,number,rows,column))
    def region_heading(title,part,x,y,number):
        display_title=({'Project':'Project / site','Contacts':'Responsible parties',
                        'Scope':'Scope of work','Job notes':'General notes','Location map':'Vicinity map'}.get(title,title)
                       if grid else title)
        label=display_title.upper()+(' / CONT.' if part else '')
        available=text_width-2*radius-3
        if available <= 0:
            raise CoverCapacityError('body: insufficient width for numbered headings')
        lines=_wrap_text(label,available,heading_size,SUPPORTED_FONT,600)
        h=max(2*radius+2,len(lines)*heading_size*1.3+2)+pad
        elements=[f'<circle cx="{_fmt(x+pad+radius)}" cy="{_fmt(y+radius+1)}" r="{_fmt(radius)}" fill="none" stroke="#000000" stroke-width="0.35"/>',
                  text(str(number),x+pad+radius,y+radius+1+size*.34,anchor='middle')]
        elements.extend(text(value,x+pad+2*radius+3,y+heading_size+1+i*heading_size*1.3,height=heading_size,weight=600)
                        for i,value in enumerate(lines))
        elements.append(rule(x,y+h-size*.7,width,.25))
        return h,elements
    pages=[]
    def page(index):
        while len(pages)<=index:
            n=len(pages);y=first_top if n==0 else continuation_top
            pages.append({'elements':list(header_elements) if n==0 else [],'panels':[], 'columns':[y,y], 'images':[]})
        return pages[index]
    last_locations={}
    grid_cursors={}
    def place(title,number,rows,part,index,column):
        set_width(title,index)
        p=page(index);y=p['columns'][column]
        x=left+column*(standard_width+gutter)
        limit=bottom
        if grid and index==0:
            x,y,_,limit=grid_regions[title]
        h,decor=region_heading(title,part,x,y,number)
        if title=='Sheet index' and table_index:
            decor.extend([text('SHEET',x+pad,y+h+size,height=size*.85,weight=600),
                          text('TITLE',x+pad+id_width,y+h+size,height=size*.85,weight=600),
                          rule(x,y+h+size*1.5,width,.15)])
            h+=line_height*1.3
        available=limit-y-h-pad
        # Keep a contact card or paragraph together when it fits a fresh
        # column. Truly oversized facts still flow with exact source offsets.
        first_source=rows[0]['source']
        first_rows=[]
        for row in rows:
            if row['source']!=first_source:break
            first_rows.append(row)
        required=sum(line_height+row.get('after_gap',0) for row in first_rows)
        fresh_h,_=region_heading(title,part,left,continuation_top,number)
        if title=='Sheet index' and table_index:fresh_h+=line_height*1.3
        fresh_available=bottom-continuation_top-fresh_h-pad
        if required<=fresh_available+1e-9 and required>available+1e-9:
            return 0
        used,count=0.,0
        for row in rows:
            advance=line_height+row.get('after_gap',0)
            if used+advance>available:break
            used+=advance;count+=1
        if count<1:return 0
        if count<len(rows):
            boundary=count
            while boundary>0 and rows[boundary-1]['source']==rows[count]['source']:
                boundary-=1
            if boundary:count=boundary
        selected=rows[:count]
        p['elements'].extend(decor)
        advance=0.
        for i,row in enumerate(selected):
            baseline=y+h+size+advance
            if 'index_title' in row:
                if row['index_id']:
                    p['elements'].append(text(row['index_id']+'  ',x+pad,baseline,weight=600))
                p['elements'].append(text(row['index_title'],x+pad+id_width,baseline))
                if i==len(selected)-1 or selected[i+1]['source']!=row['source']:
                    p['elements'].append(rule(x+pad,baseline+size*.55,width-2*pad,.13))
            elif 'project_value' in row:
                if row['project_label']:
                    p['elements'].append(text(row['project_label'],x+pad,baseline,weight=600))
                p['elements'].append(text(row['project_value'],x+pad+project_label_width,baseline))
            else:
                if row['offset']==0 and row['source'].startswith(('scope[','notes[')):
                    p['elements'].append(f'<circle cx="{_fmt(x+pad+size*.25)}" cy="{_fmt(baseline-size*.35)}" r="0.45" fill="#000000" stroke="#000000" stroke-width="0.1"/>')
                p['elements'].append(text(row.get('display',row['text']),x+pad+row.get('indent',0),baseline,weight=row.get('weight',400)))
            advance+=line_height+row.get('after_gap',0)
        total_h=h+advance+pad
        p['panels'].append({'title':title,'number':number,'part':part+1,
                            'bounds_mm':[x,y,x+width,y+total_h],
                            'lines':[r['text'] for r in selected],'source_spans':selected})
        p['columns'][column]=y+total_h+size*2
        last_locations[title]=(index,column)
        if grid and index==0:grid_cursors[title]=y+total_h+size*2
        return count
    overflow=[]
    # First-page regions retain their architectural cover arrangement.
    for title,number,rows,column in prepared:
        count=place(title,number,rows,0,0,column)
        if count<len(rows):
            overflow.append((title,number,rows[count:],1 if count else 0))
    # Continuations read down the left column, then down the right. A long
    # section can use both columns at constant typography before adding pages.
    flow_page,flow_column=1,0
    for title,number,rows,part in sorted(overflow,key=lambda item:item[1]):
        cursor=0
        while cursor<len(rows):
            count=place(title,number,rows[cursor:],part,flow_page,flow_column)
            if not count:
                set_width(title,flow_page)
                h,_=region_heading(title,part,left,continuation_top,number)
                if title=='Sheet index' and table_index:
                    h+=line_height*1.3
                if bottom-continuation_top-h-pad < line_height+rows[cursor].get('after_gap',0):
                    raise CoverCapacityError('body: paper cannot fit section heading and readable text row')
                flow_page,flow_column=(flow_page,1) if flow_column==0 else (flow_page+1,0)
                continue
            cursor+=count;part+=1
            if cursor<len(rows):
                flow_page,flow_column=(flow_page,1) if flow_column==0 else (flow_page+1,0)
    if 'location_map' in config:
        m=config['location_map'];index,column=last_locations['Location map'];p=page(index)
        set_width('Location map',index)
        x=left+column*(standard_width+gutter);y=p['columns'][column]
        map_bottom=bottom
        if grid and index==0:
            x,_,_,map_bottom=grid_regions['Location map']
            y=grid_cursors['Location map']
        richer=bool(m.get('labels') or m.get('site_marker'))
        margin=size*3 if richer else 0
        minimum=min(100 if richer else 40,(bottom-continuation_top)*.35)
        available=map_bottom-y-pad
        if available<minimum:
            index,column=flow_page,flow_column
            set_width('Location map',index);map_bottom=bottom
            p=page(index);x=left+column*(width+gutter);y=p['columns'][column]
            h,decor=region_heading('Location map',1,x,y,6)
            if bottom-y-h-pad<minimum:
                index,column=(index,1) if column==0 else (index+1,0)
                p=page(index);x=left+column*(width+gutter);y=p['columns'][column]
                h,decor=region_heading('Location map',1,x,y,6)
            p['elements'].extend(decor);y+=h
            available=map_bottom-y-pad
        if available<=2*margin or text_width<=2*margin:
            raise CoverCapacityError('Location map: paper cannot fit supplied diagram and readable labels')
        scale=min((text_width-2*margin)/m['width'],(available-2*margin)/m['height'])
        origin_x,origin_y=x+pad+margin,y+margin
        map_h=m['height']*scale+2*margin
        if richer:
            p['elements'].append(f'<rect x="{_fmt(origin_x-size)}" y="{_fmt(origin_y-size)}" width="{_fmt(m["width"]*scale+2*size)}" height="{_fmt(m["height"]*scale+2*size)}" fill="#ffffff" stroke="#000000" stroke-width="0.2"/>')
        for x1,y1,x2,y2 in m['segments']:
            p['elements'].append(f'<line x1="{_fmt(origin_x+x1*scale)}" y1="{_fmt(origin_y+y1*scale)}" x2="{_fmt(origin_x+x2*scale)}" y2="{_fmt(origin_y+y2*scale)}" stroke="#000000" stroke-width="0.35"/>')
        placements=[];occupied=[]
        label_size=max(2.5,size*.85);label_leading=label_size*1.3
        box_bounds=(x+pad,y,x+width-pad,y+map_h)
        labels=[]
        if 'site_marker' in m:
            marker=m['site_marker'];labels.append(('SITE',marker['label'],marker['x'],marker['y'],600))
        labels.extend((f'L{i+1}',r['text'],r['x'],r['y'],400) for i,r in enumerate(m.get('labels',[])))
        def overlaps(a,b):
            return a[0]<b[2]+1 and a[2]+1>b[0] and a[1]<b[3]+1 and a[3]+1>b[1]
        for key,label,mx,my,weight in labels:
            px,py=origin_x+mx*scale,origin_y+my*scale
            lines=_wrap_text(label,max(label_size*4,text_width*.55),label_size,SUPPORTED_FONT,weight)
            legend_reference=len(lines)>3
            if legend_reference:lines=(key,)
            def choose(lines):
                w=max(measure_sheet_text(v,label_size,font_family=SUPPORTED_FONT,font_weight=weight) for v in lines)+2
                h=len(lines)*label_leading+1
                l,t,r,b=box_bounds
                if w>r-l or h>b-t:return None
                candidates=[(px-w/2,py-h-size),(px+size,py+size),(px-w-size,py-h-size),(px-w/2,py+size)]
                candidates.extend((l+cx*(w+size*.5),t+cy*(h+size*.5))
                                  for cy in range(max(1,int((b-t)/(h+size*.5))))
                                  for cx in range(max(1,int((r-l)/(w+size*.5)))))
                for xx,yy in candidates:
                    xx=min(max(xx,l),r-w);yy=min(max(yy,t),b-h)
                    box=(xx,yy,xx+w,yy+h)
                    if not any(overlaps(box,other) for other in occupied):return box
                return None
            box=choose(lines)
            if box is None and not legend_reference:
                lines=(key,);legend_reference=True;box=choose(lines)
            record={'key':key,'source_label':label,'source_point':[mx,my],'paper_point_mm':[px,py],
                    'status':'legend_only' if box is None else 'legend_reference' if legend_reference else 'placed'}
            if box is not None:
                occupied.append(box);record['bounds_mm']=list(box)
                xx,yy,rr,bb=box
                target_x=min(max(px,xx),rr);target_y=min(max(py,yy),bb)
                p['elements'].append(f'<line x1="{_fmt(px)}" y1="{_fmt(py)}" x2="{_fmt(target_x)}" y2="{_fmt(target_y)}" stroke="#000000" stroke-width="0.13"/>')
                p['elements'].append(f'<rect x="{_fmt(xx)}" y="{_fmt(yy)}" width="{_fmt(rr-xx)}" height="{_fmt(bb-yy)}" fill="#ffffff" stroke="none"/>')
                p['elements'].extend(text(v,xx+1,yy+.5+label_size+i*label_leading,height=label_size,weight=weight)
                                     for i,v in enumerate(lines))
            placements.append(record)
        if 'site_marker' in m:
            marker=m['site_marker'];cx=origin_x+marker['x']*scale;cy=origin_y+marker['y']*scale;rad=max(2.5,size*.75)
            p['elements'].extend([f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(rad)}" fill="#ffffff" stroke="#000000" stroke-width="0.55"/>',
                                  f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(rad*.28)}" fill="#000000"/>'])
        p['panels'].append({'title':'Location map diagram','bounds_mm':[x,y,x+width,y+map_h],
                            'scale':scale,'lines':[],'source_spans':[],'map_labels':placements,
                            'supplied_segment_count':len(m['segments'])})
    if map_snapshot is not None:
        index,column=last_locations['Location map'];p=page(index)
        set_width('Location map',index)
        x=left+column*(standard_width+gutter);y=p['columns'][column]
        map_bottom=bottom
        if grid and index==0:
            x,_,_,map_bottom=grid_regions['Location map']
            y=grid_cursors['Location map']
        available=map_bottom-y-pad
        minimum=min(100,(bottom-continuation_top)*.35)
        if available<minimum:
            index,column=flow_page,flow_column
            set_width('Location map',index);map_bottom=bottom;p=page(index)
            x=left+column*(standard_width+gutter);y=p['columns'][column]
            h,decor=region_heading('Location map',1,x,y,6)
            if bottom-y-h-pad<minimum:
                index,column=(index,1) if column==0 else (index+1,0)
                p=page(index);x=left+column*(standard_width+gutter);y=p['columns'][column]
                h,decor=region_heading('Location map',1,x,y,6)
            p['elements'].extend(decor);y+=h;available=bottom-y-pad
        image_size=min(text_width,available)
        if image_size<=0:raise CoverCapacityError('no physical room for retained Apple snapshot')
        image_left=x+pad+(text_width-image_size)/2
        box=(image_left,y,image_left+image_size,y+image_size)
        image=SheetImageAsset('cover-apple-map',map_snapshot.png_bytes,box,map_snapshot.attribution,dict(map_snapshot.provenance))
        p['images'].append(image)
        prov=map_snapshot.provenance
        p['panels'].append({'title':'Apple map image','bounds_mm':list(box),'lines':[], 'source_spans':[],
                            'asset_id':image.id,'sha256':map_snapshot.sha256,'uncropped':True,
                            'marker_status':'not_placed_unverified_geographic_image_bounds',
                            'geocode_coordinate':[float(prov['latitude']),float(prov['longitude'])],
                            'provenance':dict(prov)})
    result=[]
    for i,p in enumerate(pages):
        if i:
            label=f'COVER CONTINUATION {i+1} OF {len(pages)}'
            if measure_sheet_text(label,size*1.4,font_family=SUPPORTED_FONT,font_weight=600)>right-left-2*pad:
                raise CoverCapacityError('continuation page heading exceeds body width')
            p['elements'][:0]=[text(label,left+pad,top+size*1.6,height=size*1.4,weight=600),
                               rule(left,top+size*2.7,right-left,.45)]
        result.append(CoverBody('\n'.join(p['elements']),{'version':VERSION,'design':'drafting-cover-reference-grid-v1' if grid else 'drafting-cover-v3',
            'bounds_mm':list(bounds),'page_number':i+1,'page_count':len(pages),'font_family':SUPPORTED_FONT,
            'text_height_mm':size,'heading_height_mm':heading_size,'project_heading_height_mm':title_size,
            'gutter_mm':gutter,'panels':p['panels'],'source_text':source_text},tuple(p['images'])))
    return tuple(result)


def paginate_cover(config, bounds_mm, *, sheet_index=None, map_snapshot=None):
    """Return ordered page specs/bodies and a stable complete derived index.

    sheet_index is the coordinator's complete index after other producers have
    expanded, excluding this cover's own continuations. The caller clones its
    original cover template for returned pages and composes via company_sheets.
    """
    config = validate_cover(config)
    if map_snapshot is not None and not isinstance(map_snapshot,CoverMapSnapshot):
        raise ValueError('typed acquired CoverMapSnapshot required')
    if map_snapshot is not None and 'location_map' in config:
        raise ValueError('choose supplied vector map or acquired Apple snapshot, not both')
    if len(bounds_mm)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds_mm):
        raise ValueError('body bounds require four finite millimetre coordinates')
    if bounds_mm[0]<0 or bounds_mm[1]<0 or bounds_mm[2]<=bounds_mm[0] or bounds_mm[3]<=bounds_mm[1]:
        raise ValueError('body bounds must be a positive rectangle')
    index = config['sheets'] if sheet_index is None else sheet_index
    # Reuse schema and duplicate-ID validation for the coordinator's index.
    validated = validate_cover({**config, 'sheets': index})
    index = validated['sheets']
    if sum(row['id'] == config['id'] for row in index) != 1:
        raise ValueError('cover must occur exactly once in the supplied sheet index')
    options = config.get('pagination', {})
    size = options.get('text_height_mm', max(2.5,min(4.2,math.sqrt((bounds_mm[2]-bounds_mm[0])*(bounds_mm[3]-bounds_mm[1]))/165)))
    original = next(r for r in index if r['id'] == config['id'])
    occupied = {row['id'].casefold() for row in index}
    explicit = options.get('continuation_ids')
    if explicit is not None and occupied.intersection(v.casefold() for v in explicit):
        raise ValueError('cover continuation id collides with an existing sheet')
    def identities(count):
        if explicit is not None:
            if count-1 > len(explicit):
                raise ValueError('pagination.continuation_ids: supply more caller IDs for all continuation pages')
            ids = explicit[:count-1]
        else:
            prefix = options.get('continuation_id_prefix')
            suffix = re.fullmatch(r'(.*?)([0-9]+)', config['id'])
            if prefix is not None:
                ids = [prefix+str(i) for i in range(2,count+1)]
            elif suffix:
                stem, number = suffix.groups()
                ids = [stem+str(int(number)+i-1).zfill(len(number)) for i in range(2,count+1)]
            else:
                ids = [config['id'][0]+str(i) for i in range(2,count+1)]
        for value in ids:
            _identifier(value, 'cover continuation id')
        if occupied.intersection(v.casefold() for v in ids) or len({v.casefold() for v in ids}) != len(ids):
            raise ValueError('cover continuation id collides with another sheet')
        return [original, *[{'id': value,'title': options.get('continuation_title','Cover continuation')+f' {i}'} for i,value in enumerate(ids,2)]]
    count = 1
    for iteration in range(1,65):
        specs = identities(count)
        expanded = []
        for row in index:
            expanded.extend(specs if row['id'] == config['id'] else [row])
        current = {**config,'sheets':expanded}
        bodies = _flow_cover(current,bounds_mm,size,map_snapshot)
        if len(bodies) == count:
            pages = tuple(CoverPage(spec['id'],spec['title'],
                         '<g xmlns="http://www.w3.org/2000/svg">'+body.svg_elements+'</g>',body.layout,body.images)
                         for spec,body in zip(specs,bodies))
            return CoverPagination(pages,tuple(expanded),iteration)
        if len(bodies) < count:
            raise CoverCapacityError('cover index pagination did not converge monotonically')
        count = len(bodies)
    raise CoverCapacityError('cover index/page count did not stabilize within 64 resolution passes')


def create_cover_sheet(config_path, *, profile_path, job_path, output_directory, map_mode="supplied",
                       map_cache=None, map_candidate_index=None, map_candidate_confirmation=None, map_client=None):
    """Produce the cover through the shared company profile and sheet tool."""
    raw = Path(config_path).read_bytes()
    config = validate_cover(_decode(raw))
    from .company_sheets import resolve_company_sheet_set, create_company_sheet_set

    resolved = resolve_company_sheet_set(profile_path, job_path)
    actual_index = [{'id': sheet.id, 'title': sheet.title} for sheet in resolved.sheets]
    if config['sheets'] != actual_index:
        raise ValueError('cover sheet index must exactly match the shared delivery sheet IDs and titles in order; '
                         'use the complete resolved company sheet list')
    # The shared set is authoritative for the visible index and produced pages.
    config['sheets'] = actual_index
    matches = [sheet for sheet in resolved.sheets if sheet.id == config['id']]
    if len(matches) != 1:
        raise ValueError('cover id must identify exactly one shared company sheet')
    sheet = matches[0]
    if map_mode not in {'supplied','auto','cached'}:
        raise ValueError('map mode must be supplied, auto or cached')
    snapshot=None
    if map_mode!='supplied' and 'location_map' not in config:
        cache=Path(map_cache) if map_cache is not None else Path.home()/'.cache/oabm/cover-maps'
        snapshot=acquire_cover_map(config['project']['location'],cache_directory=cache,client=map_client,
                                   candidate_index=map_candidate_index,candidate_confirmation=map_candidate_confirmation,offline=map_mode=='cached')
    pagination = paginate_cover(config, sheet.drawing_bounds_mm, sheet_index=actual_index,map_snapshot=snapshot)
    target = Path(output_directory).absolute()
    target.mkdir(parents=True)
    pending = target / '.pending'
    pending.mkdir()
    effective_job = Path(job_path)
    if len(pagination.pages) > 1:
        source_job = _decode(Path(job_path).read_bytes())
        (pending / 'cover-source-job.json').write_bytes(Path(job_path).read_bytes())
        # Preserve asset meaning when the expanded job moves into the attempt.
        def absolute_logos(row):
            if 'logos' in row:
                row['logos'] = {key: None if value is None else str((Path(job_path).absolute().parent / value).resolve())
                                for key,value in row['logos'].items()}
        absolute_logos(source_job)
        for row in source_job['sheets']:
            absolute_logos(row)
        expanded_rows = []
        for row in source_job['sheets']:
            if row['id'] == sheet.id:
                expanded_rows.extend({**row,'id':p.id,'title':p.title} for p in pagination.pages)
            else:
                expanded_rows.append(row)
        source_job['sheets'] = expanded_rows
        effective_job = pending / 'cover-expanded-job.json'
        effective_job.write_bytes(_json(source_job))
    images={p.id:p.images for p in pagination.pages if p.images}
    shared = create_company_sheet_set(profile_path, effective_job, output_directory=pending / 'company',
                                      bodies={p.id:p.body_svg for p in pagination.pages},**({'images':images} if images else {}))
    if snapshot is not None:
        (pending/'cover-map-source.json').write_bytes(_json({'sha256':snapshot.sha256,'media_type':'image/png',
            'width_px':snapshot.width_px,'height_px':snapshot.height_px,'attribution':snapshot.attribution,
            'provenance':snapshot.provenance}))
    if not Path(shared).is_relative_to(pending):
        raise ValueError('shared delivery must remain inside the pending cover attempt')
    (pending / 'cover-input.json').write_bytes(raw)
    layout = pagination.pages[0].layout if len(pagination.pages) == 1 else {
        'pages':[{'id':p.id,'title':p.title,'layout':p.layout} for p in pagination.pages],
        'sheet_index':pagination.sheet_index,'resolution_passes':pagination.iterations}
    (pending / 'cover-layout.json').write_bytes(_json(layout))
    files = []
    for path in sorted(pending.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            files.append({'path': path.relative_to(pending).as_posix(),
                          'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    (pending / 'COVER-MANIFEST.json').write_bytes(_json({'version': VERSION, 'id': config['id'], 'cover_pages':[p.id for p in pagination.pages],
                                           'sheet_index':pagination.sheet_index, 'files': files}))
    os.rename(pending, target / 'delivery')
    return target / 'delivery'


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cover', type=Path)
    parser.add_argument('profile', type=Path)
    parser.add_argument('job', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--map-mode',choices=('auto','supplied','cached'),default='auto')
    parser.add_argument('--offline',action='store_true',help='Use supplied vector map only; never acquire a map')
    parser.add_argument('--map-cache',type=Path)
    parser.add_argument('--map-candidate-index',type=int)
    parser.add_argument('--map-candidate-confirmation',help='Fingerprint from reviewed Apple candidate response')
    args = parser.parse_args(argv)
    try:
        delivery = create_cover_sheet(args.cover, profile_path=args.profile, job_path=args.job,
                                      output_directory=args.output,map_mode='supplied' if args.offline else args.map_mode,
                                      map_cache=args.map_cache,map_candidate_index=args.map_candidate_index,
                                      map_candidate_confirmation=args.map_candidate_confirmation)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(delivery)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
