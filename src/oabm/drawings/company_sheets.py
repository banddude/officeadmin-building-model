"""Shared company-profile sheet frame, bounded content, and local PDF delivery.

Presentation inputs only: no canonical geometry, approval or survey evidence is
created. Coordinates are paper millimetres, top-left origin, positive Y down.
"""
from __future__ import annotations
import argparse
import base64
from dataclasses import dataclass
import hashlib
import io
import json
import math
from functools import lru_cache
import subprocess
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
import xml.etree.ElementTree as ET

from .blank_set import _decode, _json, _sha, _identifier, _date, _runtime, BlankSheetSetError
from .sheet_pdf import _dependencies

SVG = 'http://www.w3.org/2000/svg'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
ET.register_namespace('', SVG)
FIELDS = {'company','license','phone','email','company_address','company_city','project','client',
          'address','city','project_number','drawn_by','checked_by','scale','issue','revision_description'}
DEFAULT_TEMPLATE = {'width_mm':914.4,'height_mm':609.6,'margin_mm':7.055555556,
                    'rail_width_mm':87.136111111,'rail_inset_mm':6.35}


def _xml_strings(value):
    """Reject XML 1.0 invalid code points before any output is reserved."""
    if isinstance(value,str):
        if any(not (ord(c) in (9,10,13) or 0x20<=ord(c)<=0xD7FF or
                    0xE000<=ord(c)<=0xFFFD or 0x10000<=ord(c)<=0x10FFFF) for c in value):
            raise ValueError('input contains an XML-invalid character')
    elif isinstance(value,dict):
        for key,item in value.items():_xml_strings(key);_xml_strings(item)
    elif isinstance(value,(list,tuple)):
        for item in value:_xml_strings(item)


def _printable(text):
    _xml_strings(text)
    if any(not 32<=ord(c)<=126 for c in text):
        raise ValueError('sheet text supports printable ASCII only; supply an explicit supported spelling')
    return text


@lru_cache(maxsize=16)
def _font(family='Liberation Sans',weight=400):
    """Resolve a real installed face and use its actual FreeType advances.

    The resolved family is written into SVG, rather than measuring one font
    and asking Cairo/browser fallback to choose another. Fontconfig and the
    selected font file are explicit local runtime requirements.
    """
    from PIL import ImageFont
    try:
        query=family+(':style=Bold' if weight>=600 else ':style=Regular')
        result=subprocess.run(['fc-match','-f','%{file}\n%{family}\n%{postscriptname}\n',query],
                              check=True,capture_output=True,text=True).stdout.splitlines()
        path,actual,postscript=result[0],result[1].split(',')[0],result[2].split(',')[0]
        font=ImageFont.truetype(path,1000)
    except (OSError,subprocess.SubprocessError,IndexError) as exc:
        raise ValueError('sheet text requires Fontconfig (fc-match) and a readable installed TrueType/OpenType font') from exc
    return font,actual,path,postscript


def validate_sheet_text(text: str) -> str:
    """Validate XML-safe printable ASCII presentation text; never replace glyphs."""
    if not isinstance(text,str):raise ValueError('sheet text must be a string')
    return _printable(text)


def measure_sheet_text(text: str, font_size_mm: float, *,
                       font_family: str = 'Liberation Sans', font_weight: int = 400) -> float:
    """Return occupied width in mm, including ink bearings and 1% margin.

    This is the shared wrapping/capacity helper. It measures the installed face
    selected by Fontconfig with Pillow/FreeType. Only printable ASCII and the
    three documented families are supported; unsupported input fails explicitly.
    Final sheet composition remains responsible for position/bounds validation.
    """
    validate_sheet_text(text)
    if type(font_size_mm) not in (int,float) or not math.isfinite(font_size_mm) or font_size_mm<=0:
        raise ValueError('font size must be positive finite millimetres')
    if font_family not in {'Liberation Sans','DejaVu Sans','sans-serif'} or font_weight not in {300,400,500,600,700}:
        raise ValueError('unsupported sheet font family/weight')
    font,_,_,_=_font(font_family,font_weight)
    ink=font.getbbox(text,anchor='ls')
    return (max(font.getlength(text),ink[2])-min(0,ink[0]))*font_size_mm/1000*1.01


def _advance(text,size,weight=400,family='Liberation Sans'):
    return measure_sheet_text(text,size,font_weight=weight,font_family=family)


def _object(value, allowed, label, required=()):
    if not isinstance(value, dict) or set(value)-set(allowed) or not set(required)<=set(value):
        raise ValueError(f'{label}: expected fields {sorted(allowed)}; required {sorted(required)}')
    return value


def _fields(value, label):
    _object(value, FIELDS, label)
    for key, text in value.items():
        if not isinstance(text,str) or any(ord(c)<32 for c in text):
            raise ValueError(f'{label}.{key}: supply a single-line string (empty explicitly clears it)')
        _printable(text)
    return dict(value)


def _template(value):
    _object(value, DEFAULT_TEMPLATE, 'template')
    result={**DEFAULT_TEMPLATE,**value}
    for key, number in result.items():
        if type(number) not in (int,float) or not math.isfinite(number) or number<=0:
            raise ValueError(f'template.{key}: positive finite millimetres required')
    w,h,m,r,i=(result[k] for k in DEFAULT_TEMPLATE)
    if w-2*m-r<=0 or h-2*m<=0 or r-2*i<=0:
        raise ValueError('template: margin/rail/inset leave no usable drawing or rail area')
    return result


@dataclass(frozen=True)
class ResolvedCompanySheet:
    id: str
    title: str
    width_mm: float
    height_mm: float
    drawing_bounds_mm: tuple[float,float,float,float]
    fields: Mapping[str,str]
    template: Mapping[str,float]
    # Detached, immutable asset bytes; rendering never rereads mutable paths.
    logos: tuple[tuple[str,bytes], ...] = ()


@dataclass(frozen=True)
class ResolvedCompanySheetSet:
    set_id: str
    sheets: tuple[ResolvedCompanySheet,...]
    formats: tuple[str,...]
    profile_json: bytes
    job_json: bytes


def _logos(value, directory):
    _object(value, {'icon','wordmark'}, 'logos')
    result={}
    from PIL import Image
    for name, path in value.items():
        if path is None:
            result[name]=None
            continue
        if not isinstance(path,str) or not path or '://' in path:
            raise ValueError(f'logos.{name}: supply a local PNG path, or null to clear')
        source=Path(path)
        if not source.is_absolute(): source=directory/source
        try:
            raw=source.read_bytes()
            if len(raw)>8*1024*1024: raise ValueError('PNG exceeds 8 MiB')
            with Image.open(io.BytesIO(raw)) as im:
                if im.format!='PNG' or im.width*im.height>16_000_000: raise ValueError('bounded PNG required')
                im.verify()
        except (OSError,ValueError) as exc:
            raise ValueError(f'logos.{name}: cannot read valid local PNG {source}: {exc}') from exc
        result[name]=raw
    return result


def resolve_company_sheet_set(profile_path, job_path):
    """Read saved defaults and job overrides without mutating either file.

    Asset paths are relative to the file supplying that override. Explicit null
    clears a logo. Issue date/revision must come from the job, never the clock.
    """
    pp,jp=Path(profile_path),Path(job_path)
    pr,jr=pp.read_bytes(),jp.read_bytes()
    profile,job=_decode(pr),_decode(jr)
    _xml_strings(profile);_xml_strings(job)
    _object(profile, {'version','fields','template','logos'}, 'profile', {'version','fields','template'})
    if profile['version']!='company-sheet-profile-v1': raise ValueError('unsupported company profile version')
    _object(job, {'version','set_id','fields','template','logos','date','revision','formats','sheets'}, 'job',
            {'version','set_id','date','revision','sheets'})
    if job['version']!='company-sheet-job-v1': raise ValueError('unsupported company sheet job version')
    identity=_identifier(job['set_id'],'set_id')
    fields={**_fields(profile['fields'],'profile.fields'),**_fields(job.get('fields',{}),'job.fields')}
    _object(profile['template'],DEFAULT_TEMPLATE,'profile.template')
    _object(job.get('template',{}),DEFAULT_TEMPLATE,'job.template')
    template=_template({**profile['template'],**job.get('template',{})})
    logos={**_logos(profile.get('logos',{}),pp.parent),**_logos(job.get('logos',{}),jp.parent)}
    issued=_date(job['date'],'date')
    revision=job['revision']
    if not isinstance(revision,str) or not revision.strip() or any(ord(c)<32 for c in revision):
        raise ValueError('revision: explicit nonempty single-line string required')
    formats=job.get('formats',['svg','pdf'])
    if not isinstance(formats,list) or not formats or any(not isinstance(f,str) for f in formats) or set(formats)-{'svg','pdf'} or len(set(formats))!=len(formats) or 'svg' not in formats:
        raise ValueError('formats: include svg, optionally pdf, without duplicates')
    if not isinstance(job['sheets'],list) or not job['sheets']: raise ValueError('sheets: nonempty ordered list required')
    sheets=[]; seen=set()
    for row in job['sheets']:
        _object(row, {'id','title','fields','logos','template'}, 'sheet', {'id','title'})
        sid=_identifier(row['id'],'sheet.id')
        if sid.casefold() in seen: raise ValueError('duplicate sheet id ignoring case')
        seen.add(sid.casefold())
        title=row['title']
        if not isinstance(title,str) or not title.strip() or any(ord(c)<32 for c in title): raise ValueError('sheet.title: nonempty single-line title required')
        _object(row.get('template',{}),DEFAULT_TEMPLATE,'sheet.template')
        _printable(title)
        t=_template({**template,**row.get('template',{})})
        effective={**fields,**_fields(row.get('fields',{}),'sheet.fields'),'date':issued,'revision':revision}
        assets={**logos,**_logos(row.get('logos',{}),jp.parent)}
        w,h,m,r=(t[k] for k in ('width_mm','height_mm','margin_mm','rail_width_mm'))
        sheets.append(ResolvedCompanySheet(sid,title,w,h,(m,m,w-m-r,h-m),
                      MappingProxyType(effective),MappingProxyType(t),tuple(sorted((k,v) for k,v in assets.items() if v is not None))))
    return ResolvedCompanySheetSet(identity,tuple(sheets),tuple(formats),pr,jr)


def _el(parent,tag,**attrs):
    return ET.SubElement(parent,f'{{{SVG}}}'+tag,{k.replace('_','-'):str(v) for k,v in attrs.items()})


def _rows(text, width, size, weight=400):
    _printable(text)
    result=[]; current=''
    for word in re.findall(r' *[^ ]+| +$',text):
        if _advance(word,size,weight)>width:
            raise ValueError('text token exceeds measured cell capacity; shorten or widen the rail')
        candidate=current+word
        if current and _advance(candidate,size,weight)>width:
            result.append(current);current=word
        else:current=candidate
    if current:result.append(current)
    return result


def _body_group(body, bounds):
    if '<!' in body: raise ValueError('body: declarations/entities are unsupported')
    try:root=ET.fromstring(body)
    except (ET.ParseError,TypeError) as exc:raise ValueError('body: supply a valid SVG g subtree') from exc
    allowed={'g':{XML_SPACE},'rect':{'x','y','width','height','fill','stroke','stroke-width'},
             'line':{'x1','y1','x2','y2','stroke','stroke-width'},
             'circle':{'cx','cy','r','fill','stroke','stroke-width'},
             'polyline':{'points','fill','stroke','stroke-width'},
             'polygon':{'points','fill','stroke','stroke-width'},
             'text':{'x','y','font-size','font-family','font-weight','text-anchor','fill',XML_SPACE}}
    l,t,r,b=bounds
    for node in root.iter():
        tag=node.tag.split('}')[-1]
        if tag not in allowed or set(node.attrib)-allowed[tag]: raise ValueError(f'body: unsupported {tag} element/attributes')
        for key,value in node.attrib.items():
            if key==XML_SPACE and value not in {'preserve','default'}:raise ValueError('body: invalid xml:space value')
            if key in {'fill','stroke'} and not re.fullmatch(r'none|#[0-9a-fA-F]{6}',value): raise ValueError('body: solid hex colors only')
            if key=='font-family' and value not in {'sans-serif','Liberation Sans','DejaVu Sans'}: raise ValueError('body: unsupported font family')
        def n(key,default=0):
            number=float(node.get(key,default))
            if not math.isfinite(number): raise ValueError('body: nonfinite coordinate')
            return number
        pad=n('stroke-width',1 if 'stroke' in node.attrib else 0)/2
        if pad<0: raise ValueError('body: negative stroke width')
        box=None
        if tag=='rect':
            x,y,w,h=n('x'),n('y'),n('width'),n('height')
            if w<0 or h<0: raise ValueError('body: negative rectangle size')
            box=(x,y,x+w,y+h)
        elif tag=='line': box=(min(n('x1'),n('x2')),min(n('y1'),n('y2')),max(n('x1'),n('x2')),max(n('y1'),n('y2')))
        elif tag=='circle':
            x,y,rad=n('cx'),n('cy'),n('r')
            if rad<0: raise ValueError('body: negative radius')
            box=(x-rad,y-rad,x+rad,y+rad)
        elif tag in {'polyline','polygon'}:
            numbers=[float(v) for v in re.split(r'[\s,]+',node.get('points','').strip()) if v]
            if len(numbers)<4 or len(numbers)%2 or not all(math.isfinite(v) for v in numbers): raise ValueError('body: invalid points')
            box=(min(numbers[::2]),min(numbers[1::2]),max(numbers[::2]),max(numbers[1::2]))
        elif tag=='text':
            if list(node): raise ValueError('body: text must be plain text, no child spans')
            node.attrib.setdefault(XML_SPACE,'preserve')
            node.attrib.setdefault('font-size','3')
            node.attrib.setdefault('font-family','Liberation Sans')
            x,y,size=n('x'),n('y'),n('font-size',3)
            if size<=0: raise ValueError('body: positive font size required')
            value=_printable(node.text or '')
            weight=int(node.get('font-weight','400'))
            if weight not in {300,400,500,600,700}:raise ValueError('body: unsupported font weight')
            family=node.get('font-family','Liberation Sans')
            font,actual,_,_=_font(family,weight)
            node.set('font-family',actual)
            width=font.getlength(value)*size/1000
            ink=font.getbbox(value,anchor='ls')
            ink=tuple(v*size/1000 for v in ink)
            anchor=node.get('text-anchor','start')
            if anchor not in {'start','middle','end'}: raise ValueError('body: invalid text anchor')
            x-=width*({'start':0,'middle':.5,'end':1}[anchor])
            safety=size*.01
            box=(x+min(0,ink[0])-safety,y+min(-size,ink[1]),x+max(width,ink[2])+safety,y+max(size*.3,ink[3]))
        if box and (box[0]-pad<l or box[1]-pad<t or box[2]+pad>r or box[3]+pad>b):
            raise ValueError('body: content exceeds drawing_bounds_mm; paginate or move/resize content')
        node.tag=f'{{{SVG}}}'+tag
    if root.tag!=f'{{{SVG}}}g': raise ValueError('body: root must be an SVG g group')
    return root


def render_company_sheet_svg(sheet, *, body_svg=None):
    """Compose the shared right rail and optional bounded consumer body."""
    t,f=sheet.template,sheet.fields
    w,h,m,r,i=(t[k] for k in DEFAULT_TEMPLATE)
    x=w-m-r; left=x+i; right=w-m-i; cw=right-left; top=m; rh=h-2*m
    root=ET.Element(f'{{{SVG}}}svg',{'width':f'{w}mm','height':f'{h}mm','viewBox':f'0 0 {w} {h}'})
    _el(root,'title').text=f'{sheet.id} | {sheet.title}'
    _el(root,'rect',x=m,y=m,width=w-2*m,height=h-2*m,fill='#ffffff',stroke='#161616',stroke_width=.35)
    if body_svg is not None: root.append(_body_group(body_svg,sheet.drawing_bounds_mm))
    _el(root,'line',x1=x,y1=top,x2=x,y2=h-m,stroke='#4a4a4a',stroke_width=.2)
    def yy(frac):return top+rh*frac
    def line(frac): _el(root,'line',x1=left,y1=yy(frac),x2=right,y2=yy(frac),stroke='#4a4a4a',stroke_width=.15)
    def text(value,frac,size=2.8,weight=400,max_lines=1,xx=None,width=None):
        size*=rh/595.488888888
        rows=_rows(value,cw if width is None else width,size,weight)
        if len(rows)>max_lines: raise ValueError(f'sheet {sheet.id}: title-block text overflows {max_lines}-line cell: {value!r}; shorten text or widen rail')
        for j,row in enumerate(rows):
            font,_,_,_=_font('Liberation Sans',weight)
            bearing=min(0,font.getbbox(row,anchor='ls')[0])*size/1000
            element=_el(root,'text',x=(left if xx is None else xx)-bearing,y=yy(frac)+j*size*1.35,font_family=_font('Liberation Sans',weight)[1],font_size=size,font_weight=weight,fill='#161616')
            element.set(XML_SPACE,'preserve');element.text=row
    def field(key,frac,**kwargs):text(f.get(key,''),frac,**kwargs)
    text('PROJECT / LOCATION',.017,3,500)
    field('project',.040,size=5.5,weight=600,max_lines=2)
    field('client',.082,size=3.6)
    field('address',.096,size=3.8)
    field('city',.109,size=3.8)
    for frac in (.118,.363,.651,.790,.944):line(frac)
    from PIL import Image
    for name,raw in sheet.logos:
        with Image.open(io.BytesIO(raw)) as image: iw,ih=image.size
        slot_y,slot_h=(.130,.124) if name=='icon' else (.260,.040)
        lw=min(cw*.90,rh*slot_h*iw/ih);lh=lw*ih/iw
        _el(root,'image',x=left+(cw-lw)/2,y=yy(slot_y),width=lw,height=lh,
            preserveAspectRatio='xMidYMid meet',href='data:image/png;base64,'+base64.b64encode(raw).decode())
    for key,frac in zip(('company','license','phone','email','company_address','company_city'),(.308,.320,.330,.340,.350,.360)):
        field(key,frac,size=2.75,weight=600)
    text('DRAWINGS ISSUE',.666,size=5.8,weight=500)
    field('issue',.683,size=2.6)
    text('DATE',.697,size=2.8);text('DESCRIPTION',.697,size=2.8,xx=left+cw*.30,width=cw*.50)
    text('REV',.697,size=2.8,xx=left+cw*.88,width=cw*.12)
    for frac in (.702,.724,.746,.768,.790):line(frac)
    text(f['date'],.716,size=2.3,width=cw*.29)
    field('revision_description',.716,size=2.3,xx=left+cw*.30,width=cw*.56)
    text(f['revision'],.716,size=2.5,xx=left+cw*.88,width=cw*.12)
    for label,key,frac in [('DATE','date',.837),('DRAWN','drawn_by',.847),('CHECKED','checked_by',.857),('PROJECT NO.','project_number',.867),('SCALE','scale',.877)]:
        text(label,frac,size=2.7,width=cw*.35)
        field(key,frac,size=2.5,xx=left+cw*.37,width=cw*.63)
        line(frac+.003)
    text('SHEET TITLE',.904,size=2.8)
    text(sheet.title,.920,size=3.3,weight=500,max_lines=2)
    text('SHEET NO.',.955,size=2.8)
    text(sheet.id,.995,size=22,weight=600)
    result=ET.tostring(root,encoding='unicode')
    ET.fromstring(result)  # Final well-formed XML check before successful output.
    return result


def _pdf(sheets, svgs, title):
    cairo,ffi,pypdf=_dependencies()
    writer=pypdf.PdfWriter();writer.add_metadata({'/Title':title,'/Producer':'oabm company sheets'})
    pages=[]
    for sheet,svg in zip(sheets,svgs):
        reader=pypdf.PdfReader(io.BytesIO(cairo.svg2pdf(bytestring=svg.encode(),unsafe=False)),strict=True)
        page=reader.pages[0]
        if len(reader.pages)!=1 or abs(float(page.mediabox.width)-sheet.width_mm*72/25.4)>.01 or abs(float(page.mediabox.height)-sheet.height_mm*72/25.4)>.01:
            raise ValueError('PDF physical page dimensions differ from resolved sheet')
        text=' '.join((page.extract_text() or '').split())
        if sheet.id not in text: raise ValueError('PDF sheet identity text missing')
        expected_fonts=set()
        for element in ET.fromstring(svg).iter(f'{{{SVG}}}text'):
            if (element.text or '').strip():
                expected_fonts.add(_font(element.get('font-family','Liberation Sans'),int(element.get('font-weight','400')))[3])
        actual_fonts={re.sub(r'^[A-Z]{6}\+','',str(font.get_object()['/BaseFont']).removeprefix('/'))
                      for font in page['/Resources'].get('/Font',{}).values()}
        if actual_fonts!=expected_fonts:
            raise ValueError(f'PDF font substitution: expected {sorted(expected_fonts)}, embedded {sorted(actual_fonts)}; install matching fonts before delivery')
        writer.add_page(page);writer.add_outline_item(sheet.id,len(pages))
        pages.append({'sheet_id':sheet.id,'svg_sha256':_sha(svg.encode()),'width_mm':sheet.width_mm,'height_mm':sheet.height_mm,'embedded_fonts':sorted(actual_fonts)})
    out=io.BytesIO();writer.write(out)
    return out.getvalue(),{'pages':pages,'CairoSVG':cairo.__version__,'cairo':ffi.cairo_version_string(),'pypdf':pypdf.__version__}


def create_company_sheet_set(profile_path, job_path, *, output_directory, bodies=None):
    """Publish a fresh delivery; never modify profile/job defaults or existing output.

    Caller must exclusively own attempt contents throughout generation. Rename
    publishes a verified packet atomically; it does not defeat external writers.
    Bodies are bounded engine-produced SVG groups, not arbitrary SVG documents.
    """
    resolved=resolve_company_sheet_set(profile_path,job_path)
    bodies={} if bodies is None else dict(bodies)
    if set(bodies)-{s.id for s in resolved.sheets}: raise ValueError('body references unknown sheet id')
    # Resolve layout/overflow and snapshot assets before reserving an attempt.
    svgs=[render_company_sheet_svg(s,body_svg=bodies.get(s.id)) for s in resolved.sheets]
    attempt=Path(output_directory).absolute();attempt.parent.mkdir(parents=True,exist_ok=True);attempt.mkdir()
    pending=attempt/'.pending';pending.mkdir();stage='snapshot'
    try:
        files=[]
        def write(name,data):
            path=pending/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.write(data)
            files.append({'path':name,'bytes':len(data),'sha256':_sha(data)})
        write('inputs/profile.json',resolved.profile_json);write('inputs/job.json',resolved.job_json)
        rows=[]
        for number,(sheet,svg) in enumerate(zip(resolved.sheets,svgs),1):
            stage=f'sheet {sheet.id}'
            write(f'sheets/{number:03d}-{sheet.id}.svg',svg.encode())
            if sheet.id in bodies: write(f'inputs/bodies/{sheet.id}.svg',bodies[sheet.id].encode())
            assets={}
            for name,raw in sheet.logos:
                path=f'inputs/assets/{_sha(raw)}.png'
                if not (pending/path).exists():write(path,raw)
                assets[name]=path
            rows.append({'id':sheet.id,'title':sheet.title,'fields':dict(sheet.fields),'template':dict(sheet.template),
                         'drawing_bounds_mm':sheet.drawing_bounds_mm,'logos':assets,'has_body':sheet.id in bodies})
        write('resolved-sheets.json',_json(rows))
        # Portable editable inputs point only at retained assets. Original exact
        # inputs remain alongside them for provenance and private editing.
        first=resolved.sheets[0]
        portable_profile={'version':'company-sheet-profile-v1','fields':{},'template':dict(first.template)}
        portable_job={'version':'company-sheet-job-v1','set_id':resolved.set_id,
                      'date':first.fields['date'],'revision':first.fields['revision'],
                      'formats':list(resolved.formats),'sheets':[]}
        for row in rows:
            portable_job['sheets'].append({'id':row['id'],'title':row['title'],
                'fields':{k:v for k,v in row['fields'].items() if k not in {'date','revision'}},
                'template':row['template'],'logos':{k:v.removeprefix('inputs/') for k,v in row['logos'].items()}})
        write('inputs/profile-portable.json',_json(portable_profile))
        write('inputs/job-portable.json',_json(portable_job))
        pdf=None
        if 'pdf' in resolved.formats:
            stage='PDF';raw,pdf=_pdf(resolved.sheets,svgs,resolved.set_id);write('sheet-set.pdf',raw)
        manifest={'version':'company-sheet-delivery-v1','set_id':resolved.set_id,'sheet_order':[s.id for s in resolved.sheets],
                  'engineering_approval':False,'status':'presentation_only','pdf':pdf,'files':files,'runtime':_runtime(),
                  'font_faces':[{'requested_family':family,'weight':weight,'resolved_family':_font(family,weight)[1],
                                 'postscript_name':_font(family,weight)[3],'sha256':_sha(Path(_font(family,weight)[2]).read_bytes())}
                                for family in ('Liberation Sans','DejaVu Sans','sans-serif') for weight in (400,700)],
                  'limitations':['Supplied text is not approval evidence.','Conservative text capacity checks require independent visual/font QA.',
                                 'Original inputs and portable editable inputs are both retained; keep private deliveries private.']}
        for row in files:
            if _sha((pending/row['path']).read_bytes())!=row['sha256']: raise ValueError('output changed before publication')
        if {p.relative_to(pending).as_posix() for p in pending.rglob('*') if p.is_file()}!={r['path'] for r in files}: raise ValueError('unexpected pending file')
        write('MANIFEST.json',_json(manifest))
        os.rename(pending,attempt/'delivery');return attempt/'delivery'
    except Exception as exc:
        try:(attempt/'failure.json').write_bytes(_json({'stage':stage,'error_type':type(exc).__name__}))
        except OSError:pass
        raise BlankSheetSetError(attempt,stage,exc) from exc


def rebuild_company_sheet_set(delivery, *, output_directory):
    """Verify retained receipts and rerun the same composition API portably."""
    root=Path(delivery)
    manifest=_decode((root/'MANIFEST.json').read_bytes())
    for row in manifest['files']:
        relative=Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('invalid receipt path')
        raw=(root/relative).read_bytes()
        if len(raw)!=row['bytes'] or _sha(raw)!=row['sha256']: raise ValueError('retained input/output receipt mismatch')
    rows=_decode((root/'resolved-sheets.json').read_bytes())
    bodies={row['id']:(root/'inputs/bodies'/f"{row['id']}.svg").read_text() for row in rows if row['has_body']}
    return create_company_sheet_set(root/'inputs/profile-portable.json',root/'inputs/job-portable.json',
                                    output_directory=output_directory,bodies=bodies)


def update_company_profile(profile_path, changes_path, *, expected_sha256):
    """Explicit defaults mutation with optimistic guard and retained previous JSON."""
    path=Path(profile_path);raw=path.read_bytes()
    if _sha(raw)!=expected_sha256: raise ValueError('profile changed; inspect current defaults before updating')
    profile=_decode(raw)
    _xml_strings(profile)
    _object(profile,{'version','fields','template','logos'},'profile',{'version','fields','template'})
    if profile['version']!='company-sheet-profile-v1': raise ValueError('unsupported company profile version')
    changes=_decode(Path(changes_path).read_bytes())
    _xml_strings(changes)
    _object(changes,{'fields','template','logos'},'changes')
    merged=dict(profile)
    for key,value in changes.items():
        if not isinstance(value,dict): raise ValueError('profile changes must be objects')
        merged[key]={**profile.get(key,{}),**value}
    _fields(merged['fields'],'profile.fields');_template(merged['template']);_logos(merged.get('logos',{}),path.parent)
    backup=path.with_name(path.name+'.'+expected_sha256+'.bak')
    if backup.exists() and backup.read_bytes()!=raw: raise ValueError('existing profile backup differs; inspect it before updating')
    if not backup.exists():
        with backup.open('xb') as stream:stream.write(raw)
    temporary=path.with_name(path.name+'.pending')
    created=False
    try:
        with temporary.open('xb') as stream:
            created=True
            stream.write(_json(merged))
        if _sha(path.read_bytes())!=expected_sha256: raise ValueError('profile changed during update')
        os.replace(temporary,path)
    finally:
        if created: temporary.unlink(missing_ok=True)
    return _sha(path.read_bytes())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create');create.add_argument('profile');create.add_argument('job');create.add_argument('output_directory')
    rebuild=sub.add_parser('rebuild');rebuild.add_argument('delivery');rebuild.add_argument('output_directory')
    update=sub.add_parser('update-defaults');update.add_argument('profile');update.add_argument('changes');update.add_argument('--expected-sha256',required=True)
    args=parser.parse_args(argv)
    try:
        result=(create_company_sheet_set(args.profile,args.job,output_directory=args.output_directory) if args.command=='create'
                else rebuild_company_sheet_set(args.delivery,output_directory=args.output_directory) if args.command=='rebuild'
                else update_company_profile(args.profile,args.changes,expected_sha256=args.expected_sha256))
    except (ValueError,OSError,ImportError) as exc:parser.exit(1,str(exc)+'\n')
    print(result);return 0

if __name__=='__main__':raise SystemExit(main())
