"""Compose existing tool bodies in explicit, disjoint paper regions.

This module does not generate electrical or schedule facts and does not rescale
or clip a producer's output. Shared company-sheet admission remains authoritative.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from .company_sheets import _body_group


@dataclass(frozen=True)
class SheetBodyPart:
    id: str
    body_svg: str
    regions_mm: dict[str, tuple[float, float, float, float]]
    source_bindings: dict


@dataclass(frozen=True)
class ComposedBody:
    body_svg: str
    coverage: tuple[dict, ...]


def _bounds(value, label):
    if (not isinstance(value,(tuple,list)) or len(value)!=4 or
        any(type(x) not in (int,float) or not math.isfinite(x) for x in value)):
        raise ValueError(label+': finite left/top/right/bottom paper millimetres required')
    l,t,r,b=value
    if l>=r or t>=b:raise ValueError(label+': positive area required')
    return tuple(value)


def compose_body_regions(parts, *, drawing_bounds_mm, clearance_mm=3.):
    """Validate each producer's complete ink inside its declared regions.

    A body may occupy several rectangles (e.g. diagram, notes, register, footer),
    leaving a true hole for another producer's panel table. Rectangles must be
    separated by the explicit clearance, including regions of the same producer.
    Source bindings are retained verbatim as detached JSON; they are caller/
    producer evidence, not automatically authenticated engineering assertions.
    """
    drawing=_bounds(drawing_bounds_mm,'drawing_bounds_mm')
    if type(clearance_mm) not in (int,float) or not math.isfinite(clearance_mm) or clearance_mm<0:
        raise ValueError('clearance_mm: finite nonnegative paper distance required')
    parts=tuple(parts)
    if not parts:raise ValueError('supply nonempty body parts')
    seen=set();regions=[];output=['<g>'];coverage=[]
    for part in parts:
        if not isinstance(part,SheetBodyPart) or not isinstance(part.id,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',part.id) or part.id in seen:
            raise ValueError('body parts need unique explicit identifiers')
        seen.add(part.id)
        if not isinstance(part.regions_mm,dict) or not part.regions_mm:raise ValueError(part.id+': supply explicit regions')
        allocated=[]
        for name,raw in part.regions_mm.items():
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',name):raise ValueError('region needs an identifier')
            bounds=_bounds(raw,part.id+'.'+name);l,t,r,b=bounds
            if l<drawing[0] or t<drawing[1] or r>drawing[2] or b>drawing[3]:raise ValueError(part.id+'.'+name+': region exceeds shared drawing bounds')
            for owner,other_name,(ol,ot,orr,ob) in regions:
                if l<orr+clearance_mm and ol<r+clearance_mm and t<ob+clearance_mm and ot<b+clearance_mm:
                    raise ValueError(f'regions {part.id}.{name} and {owner}.{other_name} overlap or violate clearance')
            regions.append((part.id,name,bounds));allocated.append((name,bounds))
        _body_group(part.body_svg,drawing)
        # Admission resolves output font names in its detached tree; preserve
        # original requested faces for repeated shared validation below.
        root=ET.fromstring(part.body_svg)
        leaves=[node for node in root.iter() if node.tag.split('}')[-1]!='g']
        if not leaves:raise ValueError(part.id+': body contains no drawable primitives')
        assignments={name:0 for name,bounds in allocated}
        for ordinal,leaf in enumerate(leaves):
            subtree='<g>'+ET.tostring(leaf,encoding='unicode')+'</g>'
            admitted=[]
            for name,bounds in allocated:
                try:_body_group(subtree,bounds)
                except ValueError:continue
                admitted.append(name)
            if len(admitted)!=1:raise ValueError(f'{part.id}: primitive {ordinal} is outside its declared regions; reserve all headers, strokes and footer ink')
            assignments[admitted[0]]+=1
        try:bindings=json.loads(json.dumps(part.source_bindings,allow_nan=False))
        except (TypeError,ValueError) as exc:raise ValueError(part.id+': source bindings must be finite JSON') from exc
        output.append(part.body_svg)
        coverage.append({'part_id':part.id,'regions_mm':dict(allocated),'primitive_counts':assignments,
            'body_sha256':hashlib.sha256(part.body_svg.encode()).hexdigest(),'source_bindings':bindings})
    output.append('</g>');body='\n'.join(output)
    _body_group(body,drawing)
    return ComposedBody(body,tuple(coverage))
