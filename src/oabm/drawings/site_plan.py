"""Bounded supplied-site presentation adapter. No site acquisition or BIM import.

This request is drawing input, never a competing semantic building model. It
uses the existing DrawingSet representation and does not write canonical data.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
import json
import math
import os
from pathlib import Path

from .blank_set import _decode, _identifier, _json, _sha, _text
from .label_layout import layout_view_labels
from .company_sheets import validate_sheet_text, measure_sheet_text
from .model import (Bounds2, DrawingPrimitive, DrawingSet, DrawingView, LineStyle,
                    Point2, SourceProvenance, SourceReference, DrawingDimension)
from .sheets import SheetSpec, ViewPlacement, compose_sheet
from .sheet_svg import SheetSymbol, render_sheet_svg

VERSION = "supplied-site-plan-v1"
UNITS = {"m": 1., "mm": .001, "ft": .3048}
KINDS = {"building", "curb", "parking", "landscape", "equipment", "route"}
STYLES = {
    "building": LineStyle("object", "heavy"),
    "curb": LineStyle("object", "normal"),
    "parking": LineStyle("space", "thin"),
    "landscape": LineStyle("space", "thin", "dot"),
    "equipment": LineStyle("electrical", "normal"),
    "route": LineStyle("route", "normal"),
}
# Generic presentation marks only, never manufacturer outlines or clearances.
SYMBOLS = {"site-equipment": SheetSymbol(tuple((Point2(*a), Point2(*b)) for a,b in (
    ((-1.5,-1.5),(1.5,-1.5)), ((1.5,-1.5),(1.5,1.5)),
    ((1.5,1.5),(-1.5,1.5)), ((-1.5,1.5),(-1.5,-1.5)))))}

SYMBOLS.update({
    "site-panel":SheetSymbol(tuple((Point2(*a),Point2(*b)) for a,b in (
        ((-2,-2),(2,-2)),((2,-2),(2,2)),((2,2),(-2,2)),((-2,2),(-2,-2)),
        ((-1,-2),(-1,2)),((1,-2),(1,2))))),
    "site-junction":SheetSymbol(tuple((Point2(*a),Point2(*b)) for a,b in (
        ((0,-2),(2,0)),((2,0),(0,2)),((0,2),(-2,0)),((-2,0),(0,-2))))),
    "site-control":SheetSymbol(tuple((Point2(*a),Point2(*b)) for a,b in (
        ((-2,-1.5),(2,-1.5)),((2,-1.5),(2,1.5)),((2,1.5),(-2,1.5)),((-2,1.5),(-2,-1.5)),
        ((-1.2,-.8),(1.2,.8)),((-1.2,.8),(1.2,-.8)))))})


def _keys(value, required, optional=(), *, label):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
        raise ValueError(f"{label}: required {sorted(required)}; optional {sorted(optional)}")


def _number(value, label, *, positive=False):
    if type(value) not in (int,float):
        raise ValueError(f"{label}: finite number required")
    try:
        value=float(value)
    except OverflowError as exc:
        raise ValueError(f"{label}: finite number required") from exc
    if not math.isfinite(value) or positive and value<=0:
        raise ValueError(f"{label}: finite {'positive ' if positive else ''}number required")
    return value


def _point(value, factor, label):
    if not isinstance(value,list) or len(value)!=2:
        raise ValueError(f"{label}: explicit [x,y] required")
    return Point2(*(_number(n,label)*factor for n in value))


def _simple(points, closed):
    """Reject zero-length edges, zero-area or self-intersecting boundaries."""
    from .label_layout import _segments_close
    if len(set(points))!=len(points):
        raise ValueError("geometry repeats a point; polygon closure is implicit")
    edges=list(zip(points,points[1:]+((points[0],) if closed else ())))
    for (a,b),(c,d) in zip(edges,edges[1:]+(edges[:1] if closed else [])):
        incoming=(b.x-a.x,b.y-a.y)
        outgoing=(d.x-c.x,d.y-c.y)
        if abs(incoming[0]*outgoing[1]-incoming[1]*outgoing[0])<1e-12 and incoming[0]*outgoing[0]+incoming[1]*outgoing[1]<0:
            raise ValueError("geometry backtracks along an adjacent edge")
    for i,(a,b) in enumerate(edges):
        for j,(c,d) in enumerate(edges):
            if j<=i+1 or closed and i==0 and j==len(edges)-1:
                continue
            if _segments_close(a,b,c,d,0):
                raise ValueError("geometry self-intersects")
    if closed and abs(sum(a.x*b.y-b.x*a.y for a,b in edges))<=1e-12:
        raise ValueError("polygon area must be positive")


def _wrap_legend_text(text, width_mm):
    """Measured wrapping without deleting characters or changing source facts."""
    validate_sheet_text(text,font_family="DejaVu Sans")
    remaining=text
    lines=[]
    while remaining:
        low,high=1,len(remaining)
        count=0
        while low<=high:
            middle=(low+high)//2
            if measure_sheet_text(remaining[:middle],2.5,font_family="DejaVu Sans")<=width_mm:
                count=middle
                low=middle+1
            else:
                high=middle-1
        if not count:
            raise ValueError("legend column cannot fit one readable glyph; enlarge legend_width_mm")
        if count<len(remaining):
            boundary=remaining.rfind(" ",0,count)
            if boundary>0:
                count=boundary+1
        lines.append(remaining[:count])
        remaining=remaining[count:]
    return lines or [""]


def prepare_site_plan(request, *, graphics=None, dimensions=()):
    """Return existing drawing objects plus a lossless input/output audit.

    Coordinates remain in the supplied local XY orientation; units are converted
    explicitly to drawing metres. No georegistration, rotation, routing or scale
    fitting is performed. Per-object frames must exactly match the request.
    """
    _keys(request,{"version","id","units","frame","features","drawing"},label="site")
    if request["version"]!=VERSION:
        raise ValueError("unsupported site-plan version")
    identity=_identifier(request["id"],"site.id")
    if not isinstance(request["units"],str) or request["units"] not in UNITS:
        raise ValueError("units: supported m, mm, ft only")
    factor=UNITS[request["units"]]
    frame=request["frame"]
    _keys(frame,{"id","axes"},label="frame")
    _text(frame["id"],"frame.id")
    if frame["axes"]!="local-xy,+x-right,+y-up":
        raise ValueError("frame.axes: require local-xy,+x-right,+y-up; register other frames upstream")
    settings=request["drawing"]
    _keys(settings,{"scale","placement_mm","padding_mm","legend_width_mm","north_degrees"},
          {"scale_mode","allowed_scales","legend_columns","presentation_role"},label="drawing")
    if settings.get("scale_mode","fixed")!="fixed" or "allowed_scales" in settings:
        raise ValueError("fit scale selection requires render_site_body and an actual resolved company sheet")
    scale=_number(settings["scale"],"scale",positive=True)
    role=settings.get("presentation_role","overview")
    if not isinstance(role,str) or role not in {"overview","detail"}:
        raise ValueError("presentation_role: use overview or detail")
    columns=settings.get("legend_columns",1)
    if type(columns) is not int or not 1<=columns<=4:
        raise ValueError("legend_columns: require an explicit integer from 1 to 4")
    padding=_number(settings["padding_mm"],"padding_mm",positive=True)
    legend_width=_number(settings["legend_width_mm"],"legend_width_mm",positive=True)
    if padding<5 or legend_width<95:
        raise ValueError("drawing: reserve at least 5 mm padding and 95 mm legend width")
    placement=_point(settings["placement_mm"],1,"placement_mm")
    north=settings["north_degrees"]
    if north is not None:
        north=_number(north,"north_degrees")
        if not 0<=north<360:
            raise ValueError("north_degrees: use [0,360), clockwise from sheet +Y, or null")
    rows=request["features"]
    if not isinstance(rows,list) or not rows:
        raise ValueError("features: supply at least one feature")
    source_index=[]
    primitives=[]
    seen=set()
    mapping=[]
    statuses=[]
    for row in rows:
        _keys(row,{"id","kind","frame_id","points","closed","label","evidence"},{"route_status"},label="feature")
        key=_identifier(row["id"],"feature.id")
        if key in seen:
            raise ValueError("duplicate feature id")
        seen.add(key)
        if row["frame_id"]!=frame["id"]:
            raise ValueError(f"{key}: incompatible frame; register upstream")
        kind=row["kind"]
        if not isinstance(kind,str) or kind not in KINDS:
            raise ValueError(f"{key}: unsupported site feature kind")
        label=validate_sheet_text(_text(row["label"],f"{key}.label"),font_family="DejaVu Sans")
        if type(row["closed"]) is not bool or not isinstance(row["points"],list):
            raise ValueError(f"{key}: closed must be boolean and points a list")
        closed=row["closed"]
        points=tuple(_point(p,factor,key) for p in row["points"])
        status=row.get("route_status")
        if kind=="route":
            if not isinstance(status,str) or status not in {"supplied","empty","future"} or closed:
                raise ValueError(f"{key}: open route requires supplied/empty/future status")
            if status=="empty" and points or status=="supplied" and len(points)<2 or status=="future" and len(points)==1:
                raise ValueError(f"{key}: empty route has no points; supplied has >=2; future has zero or >=2")
            statuses.append({"id":key,"status":status,"has_geometry":bool(points)})
        elif status is not None:
            raise ValueError(f"{key}: route_status is only for routes")
        if kind=="equipment":
            if closed or len(points)!=1:
                raise ValueError(f"{key}: equipment requires one anchor and closed=false")
        elif kind!="route" or points:
            if len(points)<(3 if closed else 2):
                raise ValueError(f"{key}: insufficient points")
            if kind=="building" and not closed:
                raise ValueError(f"{key}: building outline must be a closed polygon")
            _simple(points,closed)
        evidence=row["evidence"]
        _keys(evidence,{"source_id","reference","qualification","confidence"},label=f"{key}.evidence")
        for name in ("source_id","reference","qualification"):
            _text(evidence[name],f"{key}.evidence.{name}")
        confidence=_number(evidence["confidence"],"confidence")
        if not 0<=confidence<=1:
            raise ValueError("confidence: require [0,1]")
        source_index.append(SourceReference(key,label,confidence,(SourceProvenance(
            "manual",evidence["source_id"],None,None,evidence["reference"],confidence,"user"),)))
        ids=[]
        if points:
            primitive_id="geometry:"+key
            ids.append(primitive_id)
            style=STYLES[kind]
            token="site-equipment"
            if graphics:
                work=graphics["work_status"].get(key,"unresolved")
                if kind in {"building","curb","parking","landscape"}:
                    style=LineStyle("space","thin","solid" if kind!="landscape" else "dot")
                    if work=="proposed":
                        style=LineStyle("object","heavy")
                elif kind=="route":
                    style=LineStyle("route","heavy") if work=="proposed" else LineStyle("space","thin","dash") if work=="existing" else LineStyle("route","normal","dash") if work=="future" else style
                if kind=="equipment" and work=="existing":
                    style=LineStyle("space","normal")
                if key in graphics.get("route_patterns",{}):
                    style=replace(style,pattern=graphics["route_patterns"][key])
                token=graphics["symbols"].get(key,"site-equipment")
            if kind=="route" and status=="future":
                style=replace(style,pattern="dash")
            primitives.append(DrawingPrimitive(primitive_id,"symbol" if kind=="equipment" else "polygon" if closed else "polyline",
                "site:"+kind,(key,),points,closed,style,symbol=token if kind=="equipment" else None,
                metadata=(("qualification",evidence["qualification"]),("route_status",status or "not_applicable"),
                          ("work_status",graphics["work_status"].get(key,"unresolved") if graphics else "not_supplied"))))
            if not graphics or key in graphics["label_feature_ids"]:
                ids.append("label:"+key)
                text=label+(" [future]" if status=="future" else "")
                anchor=points[0]
                if graphics and closed and kind in {"building","parking","landscape"}:
                    anchor=Point2((min(p.x for p in points)+max(p.x for p in points))/2,(min(p.y for p in points)+max(p.y for p in points))/2)
                primitives.append(DrawingPrimitive("label:"+key,"text","annotations:labels",(key,),(anchor,),text=text))
        mapping.append({"source_id":key,"primitive_ids":ids,"input_points":row["points"],
                        "drawing_points_m":[[p.x,p.y] for p in points],"evidence":evidence})
    all_points=[p for p in primitives if p.kind!="text" for p in p.points]
    if not all_points:
        raise ValueError("site requires actual geometry; empty/future route intent alone has no extents")
    paper_to_m=scale/1000
    pad=padding*paper_to_m
    minx,miny=min(p.x for p in all_points),min(p.y for p in all_points)
    maxx,maxy=max(p.x for p in all_points),max(p.y for p in all_points)
    # Reserve annotation space using explicit drawing settings, never move source.
    bounds=Bounds2(minx-pad,miny-pad,maxx+pad,maxy+pad)
    view=DrawingView("site-plan","plan","Supplied site geometry",bounds,scale,tuple(primitives),
                     dimensions=tuple(dimensions),metadata=(("frame_id",frame["id"]),("drawing_input_kind",VERSION)))
    # Existing label algorithm uses four-unit symbol radius. Set virtual units to
    # twice paper mm, yielding the actual generic symbol's 2 mm admission radius.
    view=layout_view_labels(view,pixels_per_model_unit=2000/scale,
                            text_height_m=2.5*paper_to_m,clearance_px=2)
    unplaced=json.loads(dict(view.metadata)["unplaced_labels"])
    # A separate adjacent view prevents labels from covering the legend.
    legend_x=bounds.max_x+5*paper_to_m
    reference="site-settings:"+identity
    source_index.append(SourceReference(reference,"Supplied drawing settings",1.,(
        SourceProvenance("manual",identity,None,None,"drawing settings",1.,"user"),)))
    notes=[(text,(reference,)) for text in [f"Scale 1:{scale:g} (print at 100%)",f"Input units: {request['units']}",
           "Generic equipment mark: square", "Building: heavy solid", "Curb: normal solid", "Parking: thin gray",
           "Landscape: gray dotted", "Supplied route: solid", "Future route: dashed", "No geometry acquired from address",
           "Detail - selected features only" if role=="detail" else "Overview - all supplied geometry"]]
    spans_mm=[(maxx-minx)/paper_to_m,(maxy-miny)/paper_to_m]
    if min(spans_mm)<3:
        notes.append(("Thin extent: selected detail advised",(reference,)))
    supplied_by_id={row["id"]:row for row in rows}
    for state in statuses:
        if not state["has_geometry"]:
            row=supplied_by_id[state["id"]]
            notes.append((f"{row['label']} [{state['status']}] ({row['id']})",(row["id"],)))
            notes.append(("Intent only: no geometry",(row["id"],)))
    notes.extend((text,(reference,)) for text in ["Unplaced labels: "+str(len(unplaced)),"Not survey or engineering approval"])
    notes.extend((row["text"]+" ("+row["source_ids"][0]+")",tuple(row["source_ids"])) for row in unplaced)
    # A qualification refers to the actual features it qualifies, not settings.
    for qualification in dict.fromkeys(row["evidence"]["qualification"] for row in rows):
        sources=tuple(row["id"] for row in rows if row["evidence"]["qualification"]==qualification)
        notes.append((qualification,sources))
    wrapped=[]
    for note,sources in notes:
        parts=_wrap_legend_text(note,legend_width-6)
        for line_number,line in enumerate(parts):
            wrapped.append((line,sources,note,line_number,len(parts)))
    rows_per_column=math.ceil(len(wrapped)/columns)
    legend_height_mm=45+4*rows_per_column
    legend_total_width_mm=legend_width*columns+5*(columns-1)
    legend_bounds=Bounds2(legend_x,bounds.max_y-legend_height_mm*paper_to_m,
                         legend_x+legend_total_width_mm*paper_to_m,bounds.max_y)
    lp=[]
    for index,(note,sources,full_text,line_number,line_count) in enumerate(wrapped):
        column,row=divmod(index,rows_per_column)
        lp.append(DrawingPrimitive("legend:"+str(index),"text","site:legend",sources,
                   (Point2(legend_x+(3+column*(legend_width+5))*paper_to_m,legend_bounds.max_y-(5+row*4)*paper_to_m),),
                   text=note,metadata=(("full_text",full_text),("line_index",str(line_number)),("line_count",str(line_count)))))
    if north is not None:
        radians=math.radians(north)
        center=Point2(legend_x+15*paper_to_m,legend_bounds.min_y+12*paper_to_m)
        tip=Point2(center.x+8*paper_to_m*math.sin(radians),center.y+8*paper_to_m*math.cos(radians))
        lp.append(DrawingPrimitive("north","line","site:north",(reference,),(center,tip),style=LineStyle("annotation","heavy")))
        ux,uy=math.sin(radians),math.cos(radians)
        for side in (-1,1):
            tail=Point2(tip.x+(-2*ux+side*uy)*paper_to_m,tip.y+(-2*uy-side*ux)*paper_to_m)
            lp.append(DrawingPrimitive("north-head:"+str(side),"line","site:north",(reference,),(tail,tip),style=LineStyle("annotation","heavy")))
        lp.append(DrawingPrimitive("north-label","text","site:north",(reference,),
                  (Point2(legend_x+30*paper_to_m,center.y),),text=f"North {north:g} deg CW"))
    else:
        lp.append(DrawingPrimitive("north-unknown","text","site:north",(reference,),
                  (Point2(legend_x+3*paper_to_m,legend_bounds.min_y+10*paper_to_m),),text="North: not supplied"))
    bar_y=legend_bounds.min_y+30*paper_to_m
    bar_a=Point2(legend_x+3*paper_to_m,bar_y)
    bar_b=Point2(legend_x+23*paper_to_m,bar_y)
    lp.append(DrawingPrimitive("scale-bar","line","site:scale",(reference,),(bar_a,bar_b),style=LineStyle("annotation","heavy")))
    lp.append(DrawingPrimitive("scale-label","text","site:scale",(reference,),
              (Point2(bar_a.x,bar_y+3*paper_to_m),),text=f"0 to {20*paper_to_m:g} m"))
    legend=DrawingView("site-legend","plan","Site drawing legend",legend_bounds,scale,tuple(lp))
    drawings=DrawingSet(identity,(view,legend),(),tuple(source_index))
    for record in mapping:
        record["requested_primitive_ids"]=record["primitive_ids"]
        record["primitive_ids"]=sorted(primitive.id for drawing_view in drawings.views
            for primitive in drawing_view.primitives if record["source_id"] in primitive.source_ids)
    placements=(ViewPlacement("site","site-plan",placement.x,placement.y),
                ViewPlacement("legend","site-legend",placement.x+bounds.width/paper_to_m+5,placement.y))
    audit={"version":VERSION,"frame":frame,"units":request["units"],"metres_per_input_unit":factor,
           "mapping":mapping,"route_states":statuses,"unplaced_labels":unplaced,
           "geometry_bounds_m":[minx,miny,maxx,maxy],"canonical_model_created":False,
           "legend_layout":{"columns":columns,"column_width_mm":legend_width,"height_mm":legend_height_mm,
               "width_mm":legend_total_width_mm,"row_count":len(wrapped),"rows_per_column":rows_per_column,
               "text_height_mm":2.5,"row_pitch_mm":4,"geometry_independent":True},
           "limitations":["Supplied drawing geometry only; no BIM semantic model or site acquisition.",
             "Generic equipment marks do not show actual footprints or clearances.",
             "No route calculation, engineering, survey or completeness verification.",
             "Conservative label admission; independent font and visual inspection required."]}
    return drawings,placements,audit


def render_site_plan(request, spec: SheetSpec):
    """Render using the shared physical sheet and titleblock renderer."""
    drawings,placements,audit=prepare_site_plan(request)
    composed=compose_sheet(drawings,spec,placements)
    svg=render_sheet_svg(drawings,spec,placements,symbols=SYMBOLS)
    return drawings,composed,svg,audit


def _company_body_node(node):
    """Normalize owned renderer nodes to the shared bounded body vocabulary.

    Source metadata lives in the separate exact body map. Dash patterns become
    explicit paper-space strokes because the shared body vocabulary admits only
    solid vector elements. This changes presentation strokes, never input points.
    """
    import xml.etree.ElementTree as ET
    ns="{http://www.w3.org/2000/svg}"
    tag=node.tag.removeprefix(ns)
    attributes=dict(node.attrib)
    pattern=attributes.pop("stroke-dasharray",None)
    for name in tuple(attributes):
        if name=="id" or name.startswith("data-") or name in {"stroke-linecap","stroke-linejoin","paint-order"}:
            del attributes[name]
    if tag=="line":
        attributes.pop("fill",None)
    if tag=="text":
        for name in ("stroke","stroke-width"):
            attributes.pop(name,None)
    if pattern is not None:
        if tag not in {"polyline","polygon"}:
            raise ValueError("unsupported dashed renderer node")
        numbers=[float(v) for v in attributes.pop("points").replace(","," ").split()]
        points=list(zip(numbers[::2],numbers[1::2]))
        if tag=="polygon":
            points.append(points[0])
        lengths=[float(v) for v in pattern.split()]
        group=ET.Element(ns+"g")
        phase=0
        remaining=lengths[0]
        for a,b in zip(points,points[1:]):
            dx,dy=b[0]-a[0],b[1]-a[1]
            distance=math.hypot(dx,dy)
            traveled=0.
            while traveled<distance-1e-10:
                step=min(remaining,distance-traveled)
                if phase%2==0 and step>1e-10:
                    start=(a[0]+dx*traveled/distance,a[1]+dy*traveled/distance)
                    end=(a[0]+dx*(traveled+step)/distance,a[1]+dy*(traveled+step)/distance)
                    line_attributes={k:v for k,v in attributes.items() if k in {"stroke","stroke-width"}}
                    line_attributes.update(x1=str(start[0]),y1=str(start[1]),x2=str(end[0]),y2=str(end[1]))
                    ET.SubElement(group,ns+"line",line_attributes)
                    if len(group)>100000:
                        raise ValueError("dashed presentation exceeds bounded stroke budget")
                traveled+=step
                remaining-=step
                if remaining<=1e-10:
                    phase=(phase+1)%len(lengths)
                    remaining=lengths[phase]
        return group
    result=ET.Element(node.tag,attributes)
    result.text=node.text
    for child in node:
        result.append(_company_body_node(child))
    return result


def _render_site_body_fixed(request, sheet, *, graphics=None, include_legend=True, dimensions=()):
    """Existing renderer content as a bounded paper-mm subtree for company sheets.

    The existing physical renderer currently returns a complete SVG only. Render
    on a staging surface, retain source-mapped content nodes, and discard its
    generic stationery. The company layer alone renders final sheet stationery.
    No caller XML or unreferenced nodes are passed into the company pipeline.
    """
    import xml.etree.ElementTree as ET
    drawings,placements,audit=prepare_site_plan(request,graphics=graphics,dimensions=dimensions)
    if not include_legend:
        drawings=replace(drawings,views=tuple(view for view in drawings.views if view.id=="site-plan"))
        placements=tuple(placement for placement in placements if placement.view_id=="site-plan")
    # Staging height reserves the renderer's mandatory generic titleblock below
    # the actual paper. It is never included in the handed-off body or delivery.
    stage_spec=SheetSpec(sheet.id,sheet.width_mm,sheet.height_mm+60,0,60,sheet.title)
    composed=compose_sheet(drawings,stage_spec,placements)
    left,top,right,bottom=sheet.drawing_bounds_mm
    for view in composed.to_dict()["views"]:
        x1,y1,x2,y2=view["bounds_mm"]
        if x1<left or y1<top or x2>right or y2>bottom:
            raise ValueError(f"site view {view['view_id']} exceeds company drawing_bounds_mm; change explicit placement/scale/paper")
    rendered=render_sheet_svg(drawings,stage_spec,placements,symbols=SYMBOLS)
    root=ET.fromstring(rendered.svg)
    mapped=rendered.manifest()
    allowed_ids={row["svg_id"] for row in mapped["items"]}
    allowed_ids.update(row["svg_label_id"] for row in mapped["items"] if "svg_label_id" in row)
    ET.register_namespace("","http://www.w3.org/2000/svg")
    body=ET.Element("{http://www.w3.org/2000/svg}g")
    paths={}
    text_metrics={}
    for node in root:
        if node.get("id") in allowed_ids:
            if node.tag.endswith("}text"):
                value=validate_sheet_text(node.text or "")
                size=float(node.get("font-size","3"))
                family=node.get("font-family","Liberation Sans")
                weight=int(node.get("font-weight","400"))
                text_metrics[node.get("id")]={"width_mm":measure_sheet_text(value,size,font_family=family,font_weight=weight),
                    "font_size_mm":size,"font_family":family,"font_weight":weight}
            paths[node.get("id")]=[len(body)]
            body.append(_company_body_node(node))
    if set(paths)!=allowed_ids:
        raise ValueError("site renderer source map does not cover body elements")
    xml=ET.tostring(body,encoding="unicode")
    source_map={key:mapped[key] for key in ("items","source_index","styles","symbol_geometry_mm","render_options")}
    source_map.update(kind="oabm.site-body-map",renderer_svg_id_to_body_child_path=paths,
                      measured_text=text_metrics,text_measurement_helper="oabm.drawings.company_sheets.measure_sheet_text",
                      body_sha256=_sha(xml.encode()),sheet_id=sheet.id,
                      drawing_bounds_mm=list(sheet.drawing_bounds_mm),
                      drawing_set_sha256=mapped["drawing_set_sha256"])
    return xml,drawings,composed,audit,source_map


def _render_site_body_fit(request, sheet, *, graphics=None, include_legend=True):
    """Fixed scale, or explicit finite allowed-scale fit against actual bounds.

    No geometry rotation, paper choice or tiling is inferred. Choose the smallest
    denominator that passes geometry, label, legend and shared body admission.
    """
    from .company_sheets import render_company_sheet_svg
    if not isinstance(request,dict) or not isinstance(request.get("drawing"),dict):
        raise ValueError("site: explicit drawing settings required")
    settings=request["drawing"]
    mode=settings.get("scale_mode","fixed")
    if not isinstance(mode,str) or mode not in {"fixed","fit"}:
        raise ValueError("scale_mode: choose fixed or fit")
    if mode=="fit":
        if settings.get("scale") is not None:
            raise ValueError("fit mode requires scale=null; fixed scales are never silently changed")
        allowed=settings.get("allowed_scales")
        if not isinstance(allowed,list) or not 1<=len(allowed)<=64:
            raise ValueError("fit mode requires 1-64 explicit allowed_scales")
        scales=sorted(_number(value,"allowed scale",positive=True) for value in allowed)
        if len(set(scales))!=len(scales):
            raise ValueError("allowed_scales must be unique")
    else:
        if "allowed_scales" in settings:
            raise ValueError("allowed_scales requires explicit scale_mode=fit and scale=null")
        scales=[_number(settings.get("scale"),"scale",positive=True)]
    if mode=="fit":
        column_choices=settings.get("allowed_legend_columns",[1,2,3,4])
        if not isinstance(column_choices,list) or not column_choices or any(type(n) is not int or not 1<=n<=4 for n in column_choices) or len(set(column_choices))!=len(column_choices):
            raise ValueError("allowed_legend_columns: unique integers from 1 to 4 required")
        if "legend_columns" in settings:
            raise ValueError("fit mode uses allowed_legend_columns, not a fixed legend_columns")
        column_choices=sorted(column_choices)
    else:
        if "allowed_legend_columns" in settings:
            raise ValueError("allowed_legend_columns requires scale_mode=fit")
        column_choices=[settings.get("legend_columns",1)]
    role=settings.get("presentation_role","overview")
    if not isinstance(role,str) or role not in {"overview","detail"}:
        raise ValueError("presentation_role: use overview or detail")
    scope_request=copy.deepcopy(request)
    supplied_features=request.get("features")
    if not isinstance(supplied_features,list):
        raise ValueError("features: supply an explicit list")
    if role=="detail":
        selected=settings.get("detail_feature_ids")
        if not isinstance(selected,list) or not selected or any(not isinstance(key,str) for key in selected) or len(set(selected))!=len(selected):
            raise ValueError("detail_feature_ids: nonempty unique supplied feature IDs required")
        available={row.get("id") for row in supplied_features if isinstance(row,dict)}
        if set(selected)-available:
            raise ValueError("detail_feature_ids references unknown supplied features")
        scope_request["features"]=[row for row in supplied_features if row["id"] in selected]
    else:
        if "detail_feature_ids" in settings:
            raise ValueError("detail_feature_ids requires explicit presentation_role=detail")
        selected=[row.get("id") for row in supplied_features if isinstance(row,dict)]
    excluded=[row.get("id") for row in supplied_features if isinstance(row,dict) and row.get("id") not in selected]
    failures=[]
    for scale,columns in ((scale,columns) for scale in scales for columns in column_choices):
        candidate=copy.deepcopy(scope_request)
        candidate["drawing"].pop("detail_feature_ids",None)
        candidate["drawing"].pop("scale_mode",None)
        candidate["drawing"].pop("allowed_scales",None)
        candidate["drawing"].pop("allowed_legend_columns",None)
        candidate["drawing"]["legend_columns"]=columns
        candidate["drawing"]["scale"]=scale
        try:
            result=_render_site_body_fixed(candidate,sheet,graphics=graphics,include_legend=include_legend,
                dimensions=_graphics_dimensions(graphics,candidate,"overview",scale) if graphics else ())
            # Validate actual font/stroke extents using the real shared composer.
            # Bare test descriptors do not implement the complete shared spec.
            if hasattr(sheet,"template"):
                render_company_sheet_svg(sheet,body_svg=result[0])
        except ValueError as exc:
            if mode=="fixed":
                raise
            failures.append({"scale":scale,"legend_columns":columns,"reason":str(exc)})
            continue
        body,drawings,composed,audit,source_map=result
        view=next(view for view in drawings.views if view.id=="site-plan")
        placement=candidate["drawing"]["placement_mm"]
        mm_per_m=1000/scale
        tx=placement[0]-view.bounds.min_x*mm_per_m
        ty=placement[1]+view.bounds.max_y*mm_per_m
        factor=UNITS[request["units"]]
        geometry=audit["geometry_bounds_m"]
        spans=[(geometry[2]-geometry[0])*mm_per_m,(geometry[3]-geometry[1])*mm_per_m]
        limits=[]
        if min(spans)<3:
            limits.append("The minor site extent is below the generic 3 mm equipment mark size; this view cannot resolve transverse detail. Select explicit feature IDs for a separate detail view.")
        if audit["unplaced_labels"]:
            limits.append(f"{len(audit['unplaced_labels'])} labels use the source-bound legend instead of spatial placement.")
        coverage={"role":role,"selected_feature_ids":selected,"excluded_feature_ids":excluded,
                  "all_supplied_geometry_shown":not excluded,"cropping":False}
        presentation={"mode":mode,"coverage":coverage,
            "presentation_limits":{"geometry_span_mm":spans,"aspect_ratio":max(spans)/min(spans) if min(spans)>0 else None,
                "generic_equipment_mark_mm":3,"issues":limits,"status":"requires_detail_review" if limits else "requires_visual_review"},"selected_scale":scale,"allowed_scales":scales if mode=="fit" else None,
            "rejected_candidates":failures,"legend_columns":columns,"allowed_legend_columns":column_choices,
            "legend_layout":audit["legend_layout"],"rotation_degrees":0,"north_degrees":settings["north_degrees"],
            "drawing_bounds_mm":list(sheet.drawing_bounds_mm),"label_height_mm":2.5,
            "world_to_sheet_mm":{"input_units":request["units"],"frame_id":request["frame"]["id"],
                "matrix":[[factor*mm_per_m,0,tx],[0,-factor*mm_per_m,ty],[0,0,1]],
                "metres_matrix":[[mm_per_m,0,tx],[0,-mm_per_m,ty],[0,0,1]],
                "paper_axes":"top-left,+x-right,+y-down"}}
        audit["presentation"]=presentation
        audit["coverage"]=coverage
        source_map["presentation"]=presentation
        return body,drawings,composed,audit,source_map
    raise ValueError("No allowed scale fits the actual company drawing area and readable annotation constraints. "
        "Add an explicitly permitted larger denominator, increase paper/drawing area, or move the explicit placement inside its bounds. "
        "Rotation and tiling are not supported. Candidates: "+json.dumps(failures,sort_keys=True))


def _graphics_bounds(value,sheet,label):
    if not isinstance(value,list) or len(value)!=4:
        raise ValueError(label+": explicit [left,top,right,bottom] paper mm required")
    box=tuple(_number(n,label) for n in value)
    l,t,r,b=sheet.drawing_bounds_mm
    if not l<=box[0]<box[2]<=r or not t<=box[1]<box[3]<=b:
        raise ValueError(label+": viewport exceeds actual company drawing area")
    return box


def _validate_graphics(graphics,request,sheet):
    """Explicit presentation supplement only; never a source geometry model.

    Optional ``graphics`` preserves the existing render_site_body positional API.
    It binds work status, symbols, label selection, complete-feature detail views,
    dimension endpoint references, notes and physical viewports to supplied IDs.
    Caller facts/notes are retained exactly. There is no copied historical data.
    """
    _keys(graphics,{"version","overview_bounds_mm","notes_bounds_mm","label_feature_ids",
        "work_status","symbols","detail_views","notes","dimensions"},{"route_patterns"},label="site graphics")
    if graphics["version"]!="site-drafting-v1":
        raise ValueError("unsupported site graphics version")
    if request["drawing"].get("presentation_role","overview")!="overview":
        raise ValueError("drafting overview requires full overview scope; use explicit detail_views for subset panels")
    features={row["id"]:row for row in request["features"]}
    _graphics_bounds(graphics["overview_bounds_mm"],sheet,"overview_bounds_mm")
    _graphics_bounds(graphics["notes_bounds_mm"],sheet,"notes_bounds_mm")
    labels=graphics["label_feature_ids"]
    if not isinstance(labels,list) or any(not isinstance(key,str) or key not in features for key in labels) or len(set(labels))!=len(labels):
        raise ValueError("label_feature_ids must be unique actual supplied IDs")
    for field in ("work_status","symbols"):
        if not isinstance(graphics[field],dict) or set(graphics[field])-set(features):
            raise ValueError(field+": unknown source feature")
    for key,status in graphics["work_status"].items():
        if status not in {"existing","proposed","future","unresolved"}:
            raise ValueError("work_status: require supplied existing/proposed/future/unresolved")
        if features[key].get("route_status")=="future" and status not in {"future","unresolved"}:
            raise ValueError("work_status contradicts an explicitly future route")
    patterns=graphics.get("route_patterns",{})
    if not isinstance(patterns,dict):raise ValueError("route_patterns must be a mapping")
    for key,pattern in patterns.items():
        if key not in features or features[key]["kind"]!="route" or pattern not in {"solid","dash","dot"}:
            raise ValueError("route_patterns references unknown route or unsupported line pattern")
        if features[key].get("route_status")=="future" and pattern!="dash":
            raise ValueError("future route requires a dashed pattern")
    for key,token in graphics["symbols"].items():
        if features[key]["kind"]!="equipment" or token not in SYMBOLS:
            raise ValueError("symbols: supported generic equipment symbols only")
    if not isinstance(graphics["detail_views"],list) or len(graphics["detail_views"])>4:
        raise ValueError("detail_views: explicit list of up to four views required")
    seen={"overview"}
    numbers={"1","3"}
    boxes=[tuple(graphics["overview_bounds_mm"]),tuple(graphics["notes_bounds_mm"])]
    for detail in graphics["detail_views"]:
        _keys(detail,{"id","number","title","bounds_mm","feature_ids","scale","focus"},label="detail view")
        _identifier(detail["id"],"detail id")
        if detail["id"] in seen or not isinstance(detail["number"],str) or detail["number"] in numbers:
            raise ValueError("duplicate view identity or number")
        seen.add(detail["id"]);numbers.add(detail["number"])
        validate_sheet_text(_text(detail["title"],"detail title"))
        _number(detail["scale"],"detail scale",positive=True)
        selected=detail["feature_ids"]
        if not isinstance(selected,list) or not selected or any(key not in features for key in selected) or len(set(selected))!=len(selected):
            raise ValueError("detail view references missing/duplicate supplied feature IDs")
        if type(detail["focus"]) is not bool:
            raise ValueError("detail focus must be boolean")
        boxes.append(_graphics_bounds(detail["bounds_mm"],sheet,"detail bounds_mm"))
    for i,a in enumerate(boxes):
        if any(a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3] for b in boxes[i+1:]):
            raise ValueError("drafting view/note panels overlap")
    if not isinstance(graphics["notes"],list) or not isinstance(graphics["dimensions"],list):
        raise ValueError("drafting notes and dimensions must be explicit lists")
    for note in graphics["notes"]:
        _keys(note,{"text","source_ids"},label="drafting note")
        validate_sheet_text(_text(note["text"],"note text"))
        if not isinstance(note["source_ids"],list) or not note["source_ids"] or set(note["source_ids"])-set(features):
            raise ValueError("drafting note requires actual source feature IDs")
    dimension_ids=set()
    for dim in graphics["dimensions"]:
        _keys(dim,{"id","view_id","start_feature_id","start_point_index","end_feature_id","end_point_index","offset_mm"},label="dimension request")
        _identifier(dim["id"],"dimension id")
        if dim["id"] in dimension_ids:raise ValueError("duplicate dimension id")
        dimension_ids.add(dim["id"])
        if dim["view_id"] not in seen:
            raise ValueError("dimension references unknown drawing view")
        selected=set(features) if dim["view_id"]=="overview" else set(next(d["feature_ids"] for d in graphics["detail_views"] if d["id"]==dim["view_id"]))
        for end in ("start","end"):
            key,index=dim[end+"_feature_id"],dim[end+"_point_index"]
            if key not in selected or type(index) is not int or not 0<=index<len(features[key]["points"]):
                raise ValueError("dimension endpoint must name a selected supplied feature vertex")
        _number(dim["offset_mm"],"dimension offset_mm")
    return graphics


def _graphics_dimensions(graphics,request,view_id,scale):
    if not graphics:
        return ()
    features={row["id"]:row for row in request["features"]}
    dimensions=[]
    for row in graphics["dimensions"]:
        if row["view_id"]!=view_id:
            continue
        a=_point(features[row["start_feature_id"]]["points"][row["start_point_index"]],UNITS[request["units"]],"dimension start")
        b=_point(features[row["end_feature_id"]]["points"][row["end_point_index"]],UNITS[request["units"]],"dimension end")
        length=math.hypot(b.x-a.x,b.y-a.y)
        if length<=0:
            raise ValueError("dimension endpoints have zero plan separation")
        dimensions.append(DrawingDimension("dimension:"+row["id"],tuple(dict.fromkeys((row["start_feature_id"],row["end_feature_id"]))),
            a,b,row["offset_mm"]*scale/1000,length,f"{length:.2f} m PLAN"))
    return tuple(dimensions)


def render_site_body(request,sheet,*,graphics=None):
    """Existing body handoff, optionally with explicit professional drafting.

    Without graphics the fixed/fit contract is unchanged. ``site-drafting-v1``
    supplements only presentation: physical panel boxes, supplied scope/status,
    generic symbol choices, selected labels/details and vertex-bound dimensions.
    It never adds or changes source coordinates, equipment or route geometry.
    """
    if graphics is None:
        return _render_site_body_fit(request,sheet)
    return _render_drafted_site_body(request,sheet,_validate_graphics(graphics,request,sheet))


def _drafting_collision_obstacles(composed):
    """Paper-space ink envelopes for the actual already-placed source view.

    Reserve symbol bodies, measured label advances/bearings, source strokes and
    dimensions before adding focus annotations. These are presentation obstacles,
    never new site geometry. Text uses conservative ascender/descender bounds.
    """
    segments=[];boxes=[]
    for view in composed.to_dict()["views"]:
        for primitive in view["primitives"]:
            points=[Point2(p["x"],p["y"]) for p in primitive["points_mm"]]
            width={"thin":.13,"normal":.25,"heavy":.5}[primitive["style"]["weight"]]
            if primitive["kind"]=="text":
                p=points[0]
                advance=measure_sheet_text(primitive["text"],2.5,font_family="DejaVu Sans")
                boxes.append((p.x-1,p.y-3.5,p.x+advance+1,p.y+1.75))
            elif primitive["kind"]=="symbol":
                p=points[0]
                geometry=SYMBOLS[primitive["symbol"]].segments
                xs=[q.x for pair in geometry for q in pair];ys=[q.y for pair in geometry for q in pair]
                boxes.append((p.x+min(xs)-.75,p.y+min(ys)-.75,p.x+max(xs)+.75,p.y+max(ys)+.75))
            else:
                if primitive["closed"] or primitive["kind"]=="polygon":points.append(points[0])
                segments.extend((a,b,width) for a,b in zip(points,points[1:]))
        for dim in view["dimensions"]:
            for start,end in (("line_start_mm","line_end_mm"),("measured_start_mm","line_start_mm"),("measured_end_mm","line_end_mm")):
                segments.append((Point2(**dim[start]),Point2(**dim[end]),.25))
            p=dim["text_anchor_mm"];advance=measure_sheet_text(dim["text"],2.5,font_family="DejaVu Sans")
            boxes.append((p["x"]-advance/2-1,p["y"]-4.3,p["x"]+advance/2+1,p["y"]+.95))
    return segments,boxes


def _paper_svg_annotation_obstacles(root):
    """Include already-added paper annotations (north, headings, other keys)."""
    segments=[];boxes=[]
    for node in root.iter():
        tag=node.tag.split("}")[-1]
        width=float(node.get("stroke-width","0"))
        if tag=="text":
            x,y,size=float(node.get("x")),float(node.get("y")),float(node.get("font-size"))
            advance=measure_sheet_text(node.text or "",size,font_family=node.get("font-family","DejaVu Sans"),font_weight=int(node.get("font-weight","400")))
            x-=advance*{"start":0,"middle":.5,"end":1}[node.get("text-anchor","start")]
            boxes.append((x-1,y-size-1,x+advance+1,y+size*.3+1))
        elif tag=="circle":
            x,y,r=float(node.get("cx")),float(node.get("cy")),float(node.get("r"))
            boxes.append((x-r-1,y-r-1,x+r+1,y+r+1))
        elif tag=="line":
            segments.append((Point2(float(node.get("x1")),float(node.get("y1"))),Point2(float(node.get("x2")),float(node.get("y2"))),width))
        elif tag in {"polyline","polygon"}:
            numbers=[float(v) for v in node.get("points").replace(","," ").split()]
            points=[Point2(*p) for p in zip(numbers[::2],numbers[1::2])]
            if tag=="polygon":points.append(points[0])
            segments.extend((a,b,width) for a,b in zip(points,points[1:]))
    return segments,boxes


def _place_detail_focus(group,envelope,bounds,segments,boxes):
    """Keep only clear focus dashes and place an unfilled clear callout.

    A closed focus border around a connected route necessarily crosses its entry
    path. Open the annotation at source strokes/labels instead of drawing over
    them. Do not erase underlying source content or move source geometry.
    """
    from .label_layout import _segment_hits_box,_segments_close,_overlap
    from .model import Point2
    retained=[];omitted=0
    for node in list(group):
        a=Point2(float(node.get("x1")),float(node.get("y1")))
        b=Point2(float(node.get("x2")),float(node.get("y2")))
        stroke=.35
        within=(bounds[0]+stroke/2<=min(a.x,b.x) and max(a.x,b.x)<=bounds[2]-stroke/2 and
                bounds[1]+stroke/2<=min(a.y,b.y) and max(a.y,b.y)<=bounds[3]-stroke/2)
        blocked=not within or any(_segment_hits_box(a,b,box) for box in boxes) or any(
            _segments_close(a,b,c,d,.8+width/2+stroke/2) for c,d,width in segments)
        if blocked:
            group.remove(node);omitted+=1
        else:retained.append((a,b,stroke))
    x1,y1,x2,y2=envelope
    selected=None;tried=0
    for distance in (6.,10.,14.,18.,24.):
        for x,y in ((x2+distance,y1-distance),(x1-distance,y1-distance),(x2+distance,y2+distance),(x1-distance,y2+distance),
                    (x2+distance,(y1+y2)/2),(x1-distance,(y1+y2)/2),((x1+x2)/2,y1-distance),((x1+x2)/2,y2+distance)):
            tried+=1
            # Circle plus its stroke and a conservative 0.75 mm clearance.
            radius=4.5;box=(x-radius,y-radius,x+radius,y+radius)
            if box[0]<bounds[0] or box[1]<bounds[1] or box[2]>bounds[2] or box[3]>bounds[3]:continue
            if any(_overlap(box,other) for other in boxes):continue
            if any(_segment_hits_box(a,b,box) for a,b,_ in (*segments,*retained)):continue
            selected=(x,y);break
        if selected is not None:break
    diagnostics={"policy":"open focus boundary around source ink; unfilled collision-free bubble",
        "envelope_bounds_mm":list(envelope),"retained_segments":len(retained),"omitted_collision_segments":omitted,
        "bubble_center_mm":list(selected) if selected else None,"bubble_candidates_tried":tried,
        "bubble_fill":"none","state":"placed" if selected and retained else "partially_unplaced",
        "source_geometry_modified":False}
    return selected,retained,diagnostics


def _render_drafted_site_body(request,sheet,graphics):
    import xml.etree.ElementTree as ET
    ns="{http://www.w3.org/2000/svg}"
    from .company_sheets import render_company_sheet_svg
    body=ET.Element(ns+"g")
    items=[];paths={};metrics={};audits={};transforms={};views=[];placements=[]
    overview=tuple(graphics["overview_bounds_mm"])
    overview_sheet=replace(sheet,drawing_bounds_mm=(overview[0],overview[1]+14,overview[2],overview[3]-12))
    overview_request=copy.deepcopy(request)
    overview_request["drawing"]["placement_mm"]=[overview[0]+2,overview[1]+16]
    result=_render_site_body_fit(overview_request,overview_sheet,graphics=graphics,include_legend=False)
    selected_scale=result[3]["presentation"]["selected_scale"]
    source_index=result[1].source_index
    focus_segments,focus_boxes=_drafting_collision_obstacles(result[2])
    focus_annotations={}
    reference="site-settings:"+request["id"]
    count=0
    def add(tag,attrs,*,text=None,sources=(reference,),kind="annotation",metadata=None):
        nonlocal count
        node=ET.SubElement(body,ns+tag,{key:str(value) for key,value in attrs.items()})
        node.text=text
        key=f"drafting:{count}";count+=1
        paths[key]=[len(body)-1]
        items.append({"svg_id":key,"source_ids":list(sources),"kind":kind,"source_item_id":key,
                      "view_id":"drafting-notes","source_item_metadata":metadata or {}})
        if tag=="text":
            metrics[key]={"width_mm":measure_sheet_text(text,float(attrs["font-size"]),font_family="DejaVu Sans",font_weight=int(attrs.get("font-weight",400))),
                "font_size_mm":float(attrs["font-size"]),"font_family":"DejaVu Sans","font_weight":int(attrs.get("font-weight",400))}
        return node
    def text(value,x,y,*,size=2.5,weight=400,sources=(reference,),anchor="start",metadata=None):
        validate_sheet_text(value)
        return add("text",{"x":x,"y":y,"font-family":"DejaVu Sans","font-size":size,"font-weight":weight,
            "text-anchor":anchor,"fill":"#000000"},text=value,sources=sources,metadata=metadata)
    def line(x1,y1,x2,y2,*,weight=.25,color="#000000",sources=(reference,)):
        return add("line",{"x1":x1,"y1":y1,"x2":x2,"y2":y2,"stroke":color,"stroke-width":weight},sources=sources)
    def heading(number,title,box,scale=None):
        l,t,r,b=box
        if measure_sheet_text(title,3.5,font_family="DejaVu Sans",font_weight=700)>r-l-50:
            raise ValueError("drafting section title exceeds panel width")
        add("circle",{"cx":l+4,"cy":t+5,"r":3.8,"fill":"none","stroke":"#000000","stroke-width":.35})
        text(number,l+4,t+6,size=3.5,weight=700,anchor="middle")
        text(title,l+11,t+6.2,size=3.5,weight=700)
        if scale is not None:text(f"1:{scale:g}",r-2,t+6,size=2.5,anchor="end")
        line(l,t+10,r,t+10,weight=.5)
    def merge(identifier,result,box):
        xml,drawings,composed,audit,mapping=result
        group=ET.fromstring(xml);position=len(body);body.append(group)
        for key,path in mapping["renderer_svg_id_to_body_child_path"].items():
            paths[identifier+":"+key]=[position,*path]
        for item in mapping["items"]:
            key=identifier+":"+item["svg_id"]
            record={**item,"svg_id":key,"view_id":identifier,"placement_id":identifier}
            if "svg_label_id" in record:record["svg_label_id"]=identifier+":"+record["svg_label_id"]
            items.append(record)
        metrics.update({identifier+":"+key:value for key,value in mapping["measured_text"].items()})
        view=next(v for v in drawings.views if v.id=="site-plan")
        views.append(replace(view,id=identifier,title="SITE PLAN" if identifier=="overview" else identifier))
        placed=next(v for v in composed.to_dict()["views"] if v["view_id"]=="site-plan")
        placements.append(ViewPlacement(identifier,identifier,*placed["bounds_mm"][:2]))
        scale=view.scale;factor=1000/scale
        transforms[identifier]={"scale":scale,"frame_id":request["frame"]["id"],"input_units":request["units"],
            "matrix":[[UNITS[request['units']]*factor,0,placed['bounds_mm'][0]-view.bounds.min_x*factor],
                      [0,-UNITS[request['units']]*factor,placed['bounds_mm'][1]+view.bounds.max_y*factor],[0,0,1]],
            "feature_ids":[source.canonical_id for source in drawings.source_index if source.canonical_id!=reference],"cropping":False}
        audits[identifier]=audit
        return transforms[identifier]
    heading("1","SITE PLAN",overview,selected_scale)
    overview_transform=merge("overview",result,overview)
    # Measured bar and supplied north are paper annotations, not site geometry.
    l,t,r,b=overview
    line(l+3,b-5,l+23,b-5,weight=.5)
    for x in (l+3,l+13,l+23):line(x,b-7,x,b-3,weight=.25)
    text(f"0       {selected_scale/100:g}       {selected_scale/50:g} m",l+3,b-8,size=2.5)
    north=request["drawing"]["north_degrees"]
    if north is not None:
        angle=math.radians(north);x,y=r-18,b-7
        tip=(x+10*math.sin(angle),y-10*math.cos(angle))
        line(x,y,*tip,weight=.5)
        for side in (-1,1):line(tip[0]-2*math.sin(angle)+side*math.cos(angle),tip[1]+2*math.cos(angle)+side*math.sin(angle),*tip,weight=.35)
        text("N",r-7,b-6,size=3.5,weight=700)
    else:text("NORTH NOT SUPPLIED",r-2,b-5,anchor="end")
    features={row["id"]:row for row in request["features"]}
    for detail in graphics["detail_views"]:
        box=tuple(detail["bounds_mm"])
        content=(box[0]+2,box[1]+14,box[2]-2,box[3]-2)
        detail_request=copy.deepcopy(request)
        detail_request["features"]=[row for row in request["features"] if row["id"] in detail["feature_ids"]]
        for key in ("scale_mode","allowed_scales","allowed_legend_columns","detail_feature_ids"):
            detail_request["drawing"].pop(key,None)
        detail_request["drawing"].update(scale=detail["scale"],placement_mm=[content[0]+2,content[1]+2],presentation_role="detail",legend_columns=1)
        descriptor=replace(sheet,drawing_bounds_mm=content)
        dims=_graphics_dimensions(graphics,detail_request,detail["id"],detail["scale"])
        detail_result=_render_site_body_fixed(detail_request,descriptor,graphics=graphics,include_legend=False,dimensions=dims)
        heading(detail["number"],detail["title"],box,detail["scale"])
        merge(detail["id"],detail_result,box)
        if detail["focus"]:
            points=[point for key in detail["feature_ids"] for point in features[key]["points"]]
            matrix=overview_transform["matrix"]
            paper=[(matrix[0][0]*p[0]+matrix[0][2],matrix[1][1]*p[1]+matrix[1][2]) for p in points]
            x1,y1=min(p[0] for p in paper)-4,min(p[1] for p in paper)-4
            x2,y2=max(p[0] for p in paper)+4,max(p[1] for p in paper)+4
            node=ET.Element(ns+"polygon",{"points":f"{x1},{y1} {x2},{y1} {x2},{y2} {x1},{y2}","fill":"none","stroke":"#000000","stroke-width":"0.35","stroke-dasharray":"3 1.5"})
            group=_company_body_node(node);key="focus:"+detail["id"]
            added_segments,added_boxes=_paper_svg_annotation_obstacles(body)
            center,retained,diagnostics=_place_detail_focus(group,(x1,y1,x2,y2),overview_sheet.drawing_bounds_mm,
                [*focus_segments,*added_segments],[*focus_boxes,*added_boxes])
            focus_annotations[detail["id"]]=diagnostics
            if retained:
                paths[key]=[len(body)];body.append(group)
                items.append({"svg_id":key,"source_ids":detail["feature_ids"],"kind":"detail-focus","source_item_id":key,"view_id":"overview"})
                focus_segments.extend(retained)
            if center is not None:
                cx,cy=center
                add("circle",{"cx":cx,"cy":cy,"r":3.5,"fill":"none","stroke":"#000000","stroke-width":.35},sources=detail["feature_ids"],kind="detail-callout")
                text(detail["number"],cx,cy+1.1,size=3.2,weight=700,anchor="middle",sources=detail["feature_ids"])
                focus_boxes.append((cx-4.5,cy-4.5,cx+4.5,cy+4.5))
    # Real supplied notes and identity-bound fallback labels, not generic filler.
    box=tuple(graphics["notes_bounds_mm"]);heading("3","SITE PLAN NOTES",box)
    x,y=box[0]+2,box[1]+17;width=box[2]-box[0]-4
    def note(value,sources,*,size=2.5,bold=False):
        nonlocal y
        for index,fragment in enumerate(_wrap_legend_text(value,width)):
            if y+1>box[3]:raise ValueError("drafting notes/coverage panel overflows; provide more explicit paper area")
            text(fragment,x,y,size=size,weight=700 if bold else 400,sources=sources,
                 metadata={"full_text":value,"line_index":index})
            y+=4.2
        y+=1.3
    for i,row in enumerate(graphics["notes"],1):note(f"{i}. "+row["text"],tuple(row["source_ids"]))
    y+=3;note("PLAN GRAPHICS",(reference,),bold=True)
    for description,color,weight,pattern in (("Proposed route - supplied status","#000000",.5,None),
            ("Existing route - supplied status","#666666",.13,"dash"),("Future route / reserved intent","#000000",.25,"dash"),
            ("Context / parking linework","#666666",.13,None)):
        if y+1>box[3]:raise ValueError("drafting graphics key overflows notes panel")
        sample=ET.Element(ns+"polyline",{"points":f"{x},{y-1} {x+16},{y-1}","fill":"none","stroke":color,"stroke-width":str(weight)})
        if pattern:sample.set("stroke-dasharray","3 1.5")
        key=f"graphics-key:{count}";position=len(body);body.append(_company_body_node(sample));paths[key]=[position]
        items.append({"svg_id":key,"source_ids":[reference],"kind":"legend-sample","source_item_id":key,"view_id":"drafting-notes"})
        text(description,x+21,y,size=2.5);y+=6
    for key in graphics["symbols"]:
        if y+1>box[3]:raise ValueError("drafting symbol key overflows notes panel")
        for a,b in SYMBOLS[graphics["symbols"][key]].segments:line(x+7+a.x,y-1+a.y,x+7+b.x,y-1+b.y,sources=(key,))
        text(features[key]["label"]+" - generic mark",x+21,y,sources=(key,));y+=7
    y+=3;note("SOURCE / PRESENTATION BASIS",(reference,),bold=True)
    for qualification in dict.fromkeys(row["evidence"]["qualification"] for row in request["features"]):
        note(qualification,tuple(row["id"] for row in request["features"] if row["evidence"]["qualification"]==qualification))
    for view_id,audit in audits.items():
        for missing in audit["unplaced_labels"]:note(missing["text"]+" ("+missing["source_ids"][0]+", "+view_id+")",tuple(missing["source_ids"]))
    for row in request["features"]:
        if row["kind"]=="route" and not row["points"]:note(row["label"]+" ["+row["route_status"]+"] - no geometry ("+row["id"]+")",(row["id"],))
    for detail_id,diagnostics in focus_annotations.items():
        if diagnostics["state"]!="placed":
            note("Detail "+detail_id+": focus annotation has no fully clear placement; see the numbered enlargement.",tuple(next(d["feature_ids"] for d in graphics["detail_views"] if d["id"]==detail_id)))
    note("Plan dimensions are coordinate-derived; verify source qualifications.",(reference,))
    note("No survey, engineering or completeness approval.",(reference,))
    all_drawings=DrawingSet(request["id"],tuple(views),(),source_index)
    composed=compose_sheet(all_drawings,SheetSpec(sheet.id,sheet.width_mm,sheet.height_mm+60,0,60,sheet.title),tuple(placements))
    xml=ET.tostring(body,encoding="unicode")
    render_company_sheet_svg(sheet,body_svg=xml)
    audit=result[3]
    audit["legend_layout"]={"role":"drafting-notes-panel","bounds_mm":list(graphics["notes_bounds_mm"]),
        "used_height_mm":y-graphics["notes_bounds_mm"][1],"text_height_mm":2.5,"geometry_independent":True}
    audit["presentation"]["legend_layout"]=audit["legend_layout"]
    audit["presentation"]["sheet_scale_label"]="AS NOTED" if len({view["scale"] for view in transforms.values()})>1 else f"1:{selected_scale:g}"
    audit["presentation"]["view_scales"]={key:value["scale"] for key,value in transforms.items()}
    audit["presentation"]["placement_policy"]="caller-supplied drafting viewports"
    audit["drafting"]={"version":"site-drafting-v1","graphics":copy.deepcopy(graphics),"views":transforms,
        "view_audits":audits,"explicitly_unlabelled_feature_ids":[row["id"] for row in request["features"] if row["id"] not in graphics["label_feature_ids"]],
        "source_geometry_modified":False,"historical_reference_geometry_used":False,"visual_review":"pending",
        "focus_annotations":focus_annotations,
        "coverage_by_feature":{row["id"]:{"geometry_views":[view.id for view in views if any(p.id=="geometry:"+row["id"] for p in view.primitives)],
            "state":"drawn" if row["points"] else "intent_only_no_geometry",
            "map_label_requested":row["id"] in graphics["label_feature_ids"]} for row in request["features"]},
        "unresolved_work_status_ids":[row["id"] for row in request["features"] if row["kind"]=="route" and graphics["work_status"].get(row["id"],"unresolved")=="unresolved"]}
    # Avoid an audit self-reference: store detached per-view diagnostics.
    audit["drafting"]["view_audits"]={key:{field:value for field,value in value.items() if field!="drafting"} for key,value in audits.items()}
    source_map={"kind":"oabm.site-drafting-body-map","version":"1.0.0","sheet_id":sheet.id,"body_sha256":_sha(xml.encode()),
        "items":items,"renderer_svg_id_to_body_child_path":paths,"measured_text":metrics,
        "source_index":all_drawings.to_dict()["source_index"],"drawing_set_sha256":_sha(all_drawings.to_json(indent=None).encode()),
        "presentation":audit["presentation"],"drafting":audit["drafting"],"drawing_bounds_mm":list(sheet.drawing_bounds_mm)}
    return xml,all_drawings,composed,audit,source_map


def create_site_plan(request_path,profile_path,job_path,*,output_directory,graphics_path=None):
    """Produce one site sheet through the real shared company-profile pipeline."""
    from .company_sheets import resolve_company_sheet_set, create_company_sheet_set
    import inspect
    from .blank_set import _runtime
    raw=Path(request_path).read_bytes()
    request=_decode(raw)
    profile_raw=Path(profile_path).read_bytes()
    job_raw=Path(job_path).read_bytes()
    resolved=resolve_company_sheet_set(profile_path,job_path)
    if len(resolved.sheets)!=1:
        raise ValueError("site production requires exactly one explicit company sheet")
    sheet=resolved.sheets[0]
    graphics_raw=Path(graphics_path).read_bytes() if graphics_path else None
    if not graphics_raw and request["drawing"].get("scale_mode","fixed")=="fixed" and sheet.fields.get("scale")!=f"1:{_number(request['drawing']['scale'],'scale',positive=True):g}":
        raise ValueError("company sheet scale field must explicitly match site drawing scale as 1:N")
    body,drawings,_,audit,source_map=render_site_body(request,sheet,graphics=_decode(graphics_raw) if graphics_raw else None)
    attempt=Path(output_directory).absolute()
    attempt.parent.mkdir(parents=True,exist_ok=True)
    attempt.mkdir()
    pending=attempt/".pending"
    pending.mkdir()
    try:
        (pending/"site-input.json").write_bytes(raw)
        (pending/"source-job.json").write_bytes(job_raw)
        if graphics_raw is not None:(pending/"site-graphics.json").write_bytes(graphics_raw)
        shared_job_path=Path(job_path)
        expected_shared_job=job_raw
        if audit["presentation"]["mode"]=="fit" or graphics_raw:
            effective_job=_decode(job_raw)
            for row in effective_job["sheets"]:
                if row["id"]==sheet.id:
                    row.setdefault("fields",{})["scale"]=audit["presentation"].get("sheet_scale_label",f"1:{audit['presentation']['selected_scale']:g}")
            # Derived job sits in the attempt: preserve original relative asset
            # resolution by explicitly binding only existing supplied job paths.
            for owner in [effective_job,*effective_job["sheets"]]:
                for key,value in owner.get("logos",{}).items():
                    if value is not None:
                        owner["logos"][key]=str((Path(job_path).absolute().parent/value).absolute())
            expected_shared_job=_json(effective_job)
            shared_job_path=pending/"effective-job.json"
            shared_job_path.write_bytes(expected_shared_job)
        (pending/"site-body.svg").write_text(body,encoding="utf-8")
        (pending/"drawing.json").write_text(drawings.to_json()+"\n",encoding="utf-8")
        # This composition is content-only staging, not a second branded sheet.
        (pending/"site-audit.json").write_bytes(_json(audit))
        (pending/"site-body.map.json").write_bytes(_json(source_map))
        shared=create_company_sheet_set(profile_path,shared_job_path,
                   output_directory=pending/"company-sheet-set",bodies={sheet.id:body})
        shared=Path(shared)
        shared_relative=shared.relative_to(pending).as_posix()
        if ((shared/"inputs/profile.json").read_bytes()!=profile_raw or
                (shared/"inputs/job.json").read_bytes()!=expected_shared_job or
                Path(job_path).read_bytes()!=job_raw):
            raise ValueError("shared profile/job changed during site production; rebuild from stable inputs")
        receipts=[]
        for path in sorted(pending.rglob("*")):
            if path.is_file():
                data=path.read_bytes()
                receipts.append({"path":path.relative_to(pending).as_posix(),"bytes":len(data),"sha256":_sha(data)})
        manifest={"version":VERSION,"status":"supplied_geometry_requires_review","engineering_approval":False,
                  "input_sha256":_sha(raw),"profile_sha256":_sha(profile_raw),"job_sha256":_sha(job_raw),"sheet_id":sheet.id,"scale":audit["presentation"]["selected_scale"],
                  "presentation":audit["presentation"],"effective_job_sha256":_sha(expected_shared_job),
                  "shared_delivery":shared_relative,
                  "sheet_pipeline":"oabm.drawings.company_sheets.create_company_sheet_set",
                  "body_sha256":_sha(body.encode()),"unplaced_label_count":len(audit["unplaced_labels"]),
                  "runtime":_runtime(),"company_sheet_source_sha256":_sha(Path(inspect.getfile(resolve_company_sheet_set)).read_bytes()),
                  "files":receipts,"limitations":audit["limitations"]}
        (pending/"MANIFEST.json").write_bytes(_json(manifest))
        os.rename(pending,attempt/"delivery")
    except Exception:
        (attempt/"failure.json").write_bytes(_json({"version":VERSION,"status":"failed_pending_retained"}))
        raise
    return attempt/"delivery"


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request",type=Path)
    parser.add_argument("profile",type=Path,help="shared company profile JSON")
    parser.add_argument("job",type=Path,help="shared explicit job/sheet request JSON")
    parser.add_argument("output",type=Path,help="fresh output directory")
    parser.add_argument("--graphics",type=Path,help="optional site-drafting-v1 presentation supplement")
    args=parser.parse_args(argv)
    try:
        path=create_site_plan(args.request,args.profile,args.job,output_directory=args.output,graphics_path=args.graphics)
    except (ValueError,OSError,ImportError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(path)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
