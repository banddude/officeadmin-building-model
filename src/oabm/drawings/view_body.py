"""Scaled DrawingView -> bounded shared-sheet body, without a second renderer model.

Coordinates derive from existing DrawingView metres and explicit paper scale.
Unsupported symbols/dimensions/patterns refuse rather than changing conventions.
"""
from __future__ import annotations
from dataclasses import dataclass
import html
import math
from .company_sheets import _body_group, measure_sheet_text, validate_sheet_text


@dataclass(frozen=True)
class ScaledViewBody:
    body_svg: str
    coverage: dict


def render_scaled_view_body(view, *, drawing_bounds_mm, origin_mm):
    """Place the view upper-left at explicit paper millimetres, at its own scale.

    Existing cut/object line weights and plain labels are supported. Symbols,
    dimensions and dashed/dotted primitives need their specialized adapters;
    they are not silently replaced, omitted or converted into solid lines.
    """
    if (not isinstance(origin_mm,(list,tuple)) or not isinstance(drawing_bounds_mm,(list,tuple)) or
        len(origin_mm)!=2 or len(drawing_bounds_mm)!=4 or
        any(type(x) not in (int,float) or not math.isfinite(x) for x in (*origin_mm,*drawing_bounds_mm))):
        raise ValueError('scaled view: finite paper origin/bounds required')
    left,top,right,bottom=drawing_bounds_mm
    if left>=right or top>=bottom:raise ValueError('scaled view: positive drawing bounds required')
    if view.dimensions:raise ValueError('scaled view: dimensions require a dedicated dimension adapter')
    ppu=1000/view.scale;x0,y0=origin_mm
    width,height=view.bounds.width*ppu,view.bounds.height*ppu
    if x0<left or y0<top or x0+width>right or y0+height>bottom:
        raise ValueError('scaled view: view bounds do not fit at requested scale/origin; change explicit slot or scale')
    def point(p):return x0+(p.x-view.bounds.min_x)*ppu,y0+(view.bounds.max_y-p.y)*ppu
    body=['<g>'];rows=[]
    for primitive in sorted(view.primitives,key=lambda p:p.id):
        if primitive.style.pattern!='solid':raise ValueError(f'{primitive.id}: patterned line requires a dedicated style adapter')
        coords=[point(p) for p in primitive.points]
        weight={'light':.13,'normal':.25,'heavy':.5}.get(primitive.style.weight)
        if weight is None:raise ValueError(f'{primitive.id}: unsupported line weight {primitive.style.weight}')
        if primitive.kind in {'line','polyline','polygon'}:
            points=' '.join(f'{x:.12g},{y:.12g}' for x,y in coords)
            tag='polygon' if primitive.closed or primitive.kind=='polygon' else 'polyline'
            body.append(f'<{tag} points="{points}" fill="none" stroke="#000000" stroke-width="{weight:g}" stroke-linejoin="round"/>')
        elif primitive.kind=='text':
            value=validate_sheet_text(primitive.text or '',font_family='Liberation Sans')
            measure_sheet_text(value,2.5)
            x,y=coords[0]
            body.append(f'<text x="{x:.12g}" y="{y:.12g}" font-family="Liberation Sans" font-size="2.5" fill="#000000">{html.escape(value)}</text>')
        else:raise ValueError(f'{primitive.id}: unsupported {primitive.kind}; supply its dedicated drawing adapter')
        rows.append({'primitive_id':primitive.id,'source_ids':list(primitive.source_ids),'kind':primitive.kind,'layer':primitive.layer})
    body.append('</g>');svg='\n'.join(body)
    _body_group(svg,drawing_bounds_mm)  # final shared ink/font bounds admission
    return ScaledViewBody(svg,{'view_id':view.id,'view_type':view.view_type,'scale':view.scale,
        'origin_mm':list(origin_mm),'view_bounds_m':{'min_x':view.bounds.min_x,'min_y':view.bounds.min_y,'max_x':view.bounds.max_x,'max_y':view.bounds.max_y},
        'drawing_bounds_mm':list(drawing_bounds_mm),'primitive_sources':rows,'view_metadata':dict(view.metadata),
        'limitations':['No scale fitting or omitted primitives; unsupported presentations refuse.']})
