"""Receipt-bound native SVG components for the existing shared sheet renderer.

The native paths/curves/holes/markers and physical viewport are retained, not
remeasured or rasterized. Static CSS is resolved locally into per-node styles,
so a native stylesheet cannot change the company title rail. External resources,
active SVG, unsupported declarations and unreceipted paths fail closed.
"""
from __future__ import annotations
from dataclasses import dataclass
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
import html
import io

SVG='http://www.w3.org/2000/svg'
_TAGS={'svg','g','defs','path','line','polyline','polygon','circle','ellipse','rect','text','tspan',
       'marker','pattern','filter','feFlood','feMerge','feMergeNode','style','title','desc'}
_STYLE={'fill','fill-rule','fill-opacity','stroke','stroke-width','stroke-opacity','stroke-linecap',
        'stroke-linejoin','stroke-miterlimit','stroke-dasharray','stroke-dashoffset','opacity','font-family',
        'font-size','font-weight','font-style','text-anchor','text-decoration','dominant-baseline',
        'alignment-baseline','letter-spacing','word-spacing','display','visibility','filter','clip-path',
        'marker-start','marker-mid','marker-end','paint-order','cursor','flood-color','flood-opacity'}
_ATTR={'id','class','style','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry','width','height','d','points',
       'transform','viewBox','preserveAspectRatio','version','baseProfile','markerWidth','markerHeight',
       'markerUnits','orient','refX','refY','patternUnits','patternContentUnits','patternTransform',
       'filterUnits','primitiveUnits','in','result','dx','dy','rotate','textLength','lengthAdjust','type'}|_STYLE
_INHERIT=_STYLE-{'opacity','display','filter','clip-path'}
_URL=re.compile(r'url\(\s*[\"\']?#([A-Za-z0-9_.:$-]+)[\"\']?\s*\)',re.I)


@dataclass(frozen=True)
class NativeViewAsset:
    id: str
    svg_bytes: bytes
    origin_mm: tuple[float,float]
    provenance_json: bytes


def _digest(raw):return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=64)
def _native_face(families, weight):
    """Bind the converter's actual original fallback, not Fontconfig's guess.

    Cairo's platform font backend and fc-match can choose different fallbacks
    for a missing family. Probe a local two-letter SVG, then require the exact
    same embedded PostScript face from the readable shared measurement face.
    No fonts or resources are downloaded.
    """
    from .sheet_pdf import _dependencies
    from .company_sheets import _font
    cairo,_,pypdf=_dependencies()
    def probe(family,w):
        raw=('<svg xmlns="'+SVG+'" width="100" height="20"><text x="0" y="15" font-family="'+html.escape(family,quote=True)+'" font-weight="'+str(w)+'">Hg</text></svg>').encode()
        page=pypdf.PdfReader(io.BytesIO(cairo.svg2pdf(bytestring=raw,unsafe=False))).pages[0]
        names={re.sub(r'^[A-Z]{6}\+','',str(f.get_object()['/BaseFont']).removeprefix('/')) for f in page['/Resources'].get('/Font',{}).values()}
        if len(names)!=1:raise ValueError('native source font fallback is ambiguous')
        name=names.pop()
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+',name):raise ValueError('unsupported native font identity')
        return name
    wanted=probe(families,weight)
    for candidate in dict.fromkeys([weight,400,700,500,600,300]):
        font,actual,_,postscript=_font(':postscriptname='+wanted,candidate)
        if postscript==wanted and probe(actual,candidate)==wanted:
            return actual,candidate,wanted
    raise ValueError('native source converter face cannot be reproduced by the shared font runtime')


def native_view_asset(delivery, view_id, *, origin_mm, asset_id=None):
    """Detach one hash-verified generated native view without fetching resources."""
    root=Path(delivery).resolve()
    receipt_raw=(root/'receipt.json').read_bytes();receipt=json.loads(receipt_raw)
    if receipt.get('status')!='review_only_success':raise ValueError('completed native receipt required')
    row=receipt.get('views',{}).get(view_id)
    if not isinstance(row,dict):raise ValueError('known native view ID required')
    name=row.get('svg');relative=Path(name) if isinstance(name,str) else Path('..')
    if relative.is_absolute() or '..' in relative.parts or relative.as_posix()!=name:raise ValueError('native view path escapes receipt')
    path=root/relative
    if any((root/Path(*relative.parts[:n])).is_symlink() for n in range(1,len(relative.parts)+1)) or not path.resolve().is_relative_to(root):raise ValueError('native view symlink/escape refused')
    raw=path.read_bytes()
    if receipt.get('files',{}).get(name)!=_digest(raw):raise ValueError('native view receipt hash mismatch')
    if 'source.ifc' in receipt.get('input_sha256',{}) and receipt.get('native_source_preservation',{}).get('verified') is not True:
        raise ValueError('native source preservation proof required')
    provenance={'version':'native-view-component-v1','view_id':view_id,'native_receipt_sha256':_digest(receipt_raw),
        'svg_sha256':_digest(raw),'native_svg_mm':row.get('native_svg_mm'),'scale':row.get('scale'),
        'input_sha256':receipt.get('input_sha256',{}),'canonical_identity':receipt.get('canonical_identity',{}),
        'native_source_preservation':receipt.get('native_source_preservation'),
        'limitations':['Original native viewport and clipping semantics retained; no scale fitting.',
                       'Native geometry and fonts require independent visual review.']}
    asset=NativeViewAsset(asset_id or view_id,raw,tuple(origin_mm),json.dumps(provenance,sort_keys=True).encode())
    # Validate immediately; the shared renderer repeats validation on detached bytes.
    render_native_vector(asset,(-1e7,-1e7,1e7,1e7))
    return asset


def render_native_vector(asset, drawing_bounds_mm):
    """Return an isolated vector subtree plus exact source/placement receipts."""
    from .company_sheets import _font,validate_sheet_text
    import tinycss2
    import cssselect2
    if not isinstance(asset,NativeViewAsset):raise ValueError('typed NativeViewAsset required')
    if not isinstance(asset.id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',asset.id):raise ValueError('safe native component ID required')
    if not isinstance(asset.svg_bytes,bytes) or len(asset.svg_bytes)>16*1024**2 or b'<!' in asset.svg_bytes.replace(b'<![CDATA[',b''):
        raise ValueError('bounded native SVG without declarations/entities required')
    provenance=json.loads(asset.provenance_json)
    if provenance.get('version')!='native-view-component-v1' or provenance.get('svg_sha256')!=_digest(asset.svg_bytes):raise ValueError('native component source receipt mismatch')
    try:root=ET.fromstring(asset.svg_bytes)
    except ET.ParseError as exc:raise ValueError('invalid native SVG') from exc
    if root.tag!=f'{{{SVG}}}svg':raise ValueError('native SVG root required')
    if 'transform' in root.attrib or any(root.get(k,'0') not in {'0','0.0'} for k in ('x','y')):
        raise ValueError('native root viewport transforms/offsets are unsupported')
    nodes=list(root.iter())
    if len(nodes)>100_000:raise ValueError('native SVG node budget exceeded')
    def mm(value):
        if not isinstance(value,str) or not value.endswith('mm'):raise ValueError('native viewport must use millimetres')
        v=float(value[:-2])
        if not math.isfinite(v) or not 0<v<=200_000:raise ValueError('bounded native viewport required')
        return v
    width,height=mm(root.get('width')),mm(root.get('height'))
    paper=provenance.get('native_svg_mm')
    if paper!=[width,height] or root.get('data-scale')!='1:'+str(provenance.get('scale','')).removeprefix('1/'):
        raise ValueError('native viewport/scale differs from receipt')
    viewbox=[float(v) for v in re.split(r'[\s,]+',root.get('viewBox','').strip()) if v]
    if len(viewbox)!=4 or any(not math.isfinite(v) for v in viewbox) or max(abs(a-b) for a,b in zip(viewbox,[0,0,width,height]))>1e-7:
        raise ValueError('native viewport must retain its exact physical viewBox')
    if len(asset.origin_mm)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in asset.origin_mm):raise ValueError('finite native paper origin required')
    x,y=asset.origin_mm;l,t,r,b=drawing_bounds_mm
    if not(l<=x and t<=y and x+width<=r and y+height<=b):raise ValueError('native viewport does not fit at unchanged scale')
    ids={};prefix='native-'+asset.id+'-'+_digest(asset.svg_bytes)[:12]+'-'
    for node in nodes:
        tag=node.tag.split('}')[-1]
        if node.tag!=f'{{{SVG}}}{tag}' or tag not in _TAGS:raise ValueError('unsupported native SVG element: '+tag)
        for key,value in node.attrib.items():
            if key.startswith('{http://www.ifcopenshell.org/ns}') or key.startswith('data-') or key=='{http://www.w3.org/XML/1998/namespace}space':continue
            if key not in _ATTR:raise ValueError('unsupported native SVG attribute: '+key)
            if len(value)>2_000_000:raise ValueError('native attribute budget exceeded')
            if key in {'d','points','transform','patternTransform','viewBox','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry','width','height','markerWidth','markerHeight','refX','refY','dx','dy','rotate'}:
                if re.search(r'(?i)nan|inf',value):raise ValueError('nonfinite native geometry')
                numbers=re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?',value)
                if any(not math.isfinite(float(n)) or abs(float(n))>1e7 for n in numbers):raise ValueError('bounded finite native geometry required')
        if 'id' in node.attrib:
            ident=node.get('id')
            if not re.fullmatch(r'[A-Za-z0-9_.:$-]{1,160}',ident) or ident in ids:raise ValueError('unique bounded native IDs required')
            ids[ident]=prefix+ident
    def safe_value(value):
        if any(c in value for c in ['<','>','\\',';','{','}','!']):raise ValueError('unsupported native CSS escape/markup/declaration boundary')
        remaining=_URL.sub('',value)
        if re.search(r'url\s*\(|expression\s*\(|var\s*\(',remaining,re.I):raise ValueError('external/active native CSS refused')
        for match in _URL.finditer(value):
            if match.group(1) not in ids:raise ValueError('native CSS references missing local resource')
        return _URL.sub(lambda m:'url(#'+ids[m.group(1)]+')',value)
    def declarations(tokens):
        out=[]
        for d in tinycss2.parse_declaration_list(tokens,skip_comments=True,skip_whitespace=True):
            if d.type!='declaration' or d.lower_name not in _STYLE:raise ValueError('unsupported native CSS declaration')
            value=tinycss2.serialize(d.value).strip();safe_value(value)
            if d.lower_name in {'font-size','stroke-width','stroke-dasharray','stroke-dashoffset','letter-spacing','word-spacing'}:
                if value not in {'none','normal'}:
                    parts=re.split(r'[\s,]+',value)
                    if any(not re.fullmatch(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?:px|mm|cm|in|pt|pc)?',part) for part in parts):
                        raise ValueError('relative/inherited native CSS lengths require an explicit computed-value adapter')
            out.append((d.lower_name,value,d.important))
        return out
    def validate_cascade_value(key,value):
        safe_value(value)
        if value.lower() in {'inherit','initial','unset','revert','revert-layer'}:
            raise ValueError('native CSS-wide values require an explicit computed-value adapter')
        if key in {'font-size','stroke-width','stroke-dasharray','stroke-dashoffset','letter-spacing','word-spacing'}:
            special=(key=='stroke-dasharray' and value=='none') or (key in {'letter-spacing','word-spacing'} and value=='normal')
            if not special:
                parts=re.split(r'[\s,]+',value)
                if any(not re.fullmatch(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?:px|mm|cm|in|pt|pc)?',part) for part in parts):
                    raise ValueError('relative/inherited native CSS lengths require an explicit computed-value adapter')
    matcher=cssselect2.Matcher()
    for style in [n for n in nodes if n.tag==f'{{{SVG}}}style']:
        for rule in tinycss2.parse_stylesheet(style.text or '',skip_comments=True,skip_whitespace=True):
            if rule.type!='qualified-rule':raise ValueError('native CSS at-rules/imports refused')
            decls=declarations(rule.content)
            try:selectors=cssselect2.compile_selector_list(rule.prelude)
            except cssselect2.SelectorError as exc:raise ValueError('unsupported native CSS selector') from exc
            for selector in selectors:matcher.add_selector(selector,decls)
    computed={};source_guids=[];font_faces=[]
    for wrapper in cssselect2.ElementWrapper.from_xml_root(root).iter_subtree():
        node=wrapper.etree_element;tag=node.tag.split('}')[-1]
        inherited=computed.get(wrapper.parent.etree_element,{}) if wrapper.parent else {}
        final={k:v for k,v in inherited.items() if k in _INHERIT}
        priorities={}
        def assign(decls,priority):
            for key,value,important in decls:
                validate_cascade_value(key,value)
                rank=(bool(important),*priority)
                if key not in priorities or rank>=priorities[key]:priorities[key]=rank;final[key]=value
        assign([(k,v,False) for k,v in node.attrib.items() if k in _STYLE],(0,0,0,0,0))
        for specificity,order,pseudo,decls in matcher.match(wrapper):
            if pseudo is not None:raise ValueError('native generated CSS pseudo-elements unsupported')
            assign(decls,(0,*specificity,order))
        assign(declarations(node.get('style','')),(1,0,0,0,0))
        computed[node]=dict(final)
    # Match against the original tree: renaming IDs/removing classes while
    # matching descendants would change ancestor selector semantics.
    for node in nodes:
        tag=node.tag.split('}')[-1];final=dict(computed[node])
        for key in list(node.attrib):
            if key in _STYLE or key in {'style','class'}:node.attrib.pop(key)
        if tag in {'text','tspan'}:
            families=final.get('font-family','Liberation Sans')
            # Fontconfig resolves the supplied fallback list on this host; record
            # the actual readable face just as the shared title rail does.
            weight=final.get('font-weight','400');weight={'normal':'400','bold':'700'}.get(weight,weight)
            if not weight.isdigit() or int(weight) not in {300,400,500,600,700}:raise ValueError('unsupported native text weight')
            if final.get('font-style','normal')!='normal':raise ValueError('native italic font needs an explicit face adapter')
            actual,actual_weight,postscript=_native_face(families,int(weight))
            font,actual,_,_=_font(actual,actual_weight);value=validate_sheet_text(node.text or '')
            if any(ord(char) not in font._oabm_codepoints for char in value):raise ValueError('native text glyph absent from resolved source face')
            font_faces.append({'source_family':families,'source_weight':int(weight),'resolved_family':actual,'resolved_weight':actual_weight,'postscript_name':postscript})
            final['font-family']=actual;final['font-weight']=str(actual_weight)
            node.set('font-family',actual);node.set('font-weight',str(actual_weight))
        if final:node.set('style',';'.join(k+':'+safe_value(v) for k,v in sorted(final.items())))
        if 'id' in node.attrib:node.set('id',ids[node.get('id')])
        guid=node.get('{http://www.ifcopenshell.org/ns}guid')
        if guid:source_guids.append(guid)
    # CSS has been resolved without rounding geometry or changing transforms.
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag==f'{{{SVG}}}style':parent.remove(child)
    root.set('x',str(x));root.set('y',str(y));root.set('width',str(width));root.set('height',str(height))
    return root,{'asset_id':asset.id,'svg_sha256':_digest(asset.svg_bytes),'bounds_mm':[x,y,x+width,y+height],
        'scale':provenance['scale'],'source_guids':sorted(set(source_guids)),'provenance':provenance,'font_faces':font_faces,
        'geometry_policy':'Native path/transform/curve strings retained; original viewport, no fitting or rasterization.'}
