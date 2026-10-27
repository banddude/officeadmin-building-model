"""Bounded supplied-site presentation adapter. No site acquisition or BIM import.

This request is drawing input, never a competing semantic building model. It
uses the existing DrawingSet representation and does not write canonical data.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
import os
from pathlib import Path

from .blank_set import _decode, _identifier, _json, _sha, _text
from .label_layout import layout_view_labels
from .company_sheets import validate_sheet_text, measure_sheet_text
from .model import (Bounds2, DrawingPrimitive, DrawingSet, DrawingView, LineStyle,
                    Point2, SourceProvenance, SourceReference)
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


def prepare_site_plan(request):
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
    _keys(settings,{"scale","placement_mm","padding_mm","legend_width_mm","north_degrees"},label="drawing")
    scale=_number(settings["scale"],"scale",positive=True)
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
        label=validate_sheet_text(_text(row["label"],f"{key}.label"))
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
            if kind=="route" and status=="future":
                style=replace(style,pattern="dash")
            primitives.append(DrawingPrimitive(primitive_id,"symbol" if kind=="equipment" else "polygon" if closed else "polyline",
                "site:"+kind,(key,),points,closed,style,symbol="site-equipment" if kind=="equipment" else None,
                metadata=(("qualification",evidence["qualification"]),("route_status",status or "not_applicable"))))
            ids.append("label:"+key)
            text=label+(" [future]" if status=="future" else "")
            primitives.append(DrawingPrimitive("label:"+key,"text","annotations:labels",(key,),(points[0],),text=text))
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
    bounds=Bounds2(minx-pad,miny-pad,maxx+pad,max(maxy+pad,miny+65*paper_to_m))
    view=DrawingView("site-plan","plan","Supplied site geometry",bounds,scale,tuple(primitives),
                     metadata=(("frame_id",frame["id"]),("drawing_input_kind",VERSION)))
    # Existing label algorithm uses four-unit symbol radius. Set virtual units to
    # twice paper mm, yielding the actual generic symbol's 2 mm admission radius.
    view=layout_view_labels(view,pixels_per_model_unit=2000/scale,
                            text_height_m=2.5*paper_to_m,clearance_px=2)
    unplaced=json.loads(dict(view.metadata)["unplaced_labels"])
    # A separate adjacent view prevents labels from covering the legend.
    legend_x=bounds.max_x+5*paper_to_m
    legend_bounds=Bounds2(legend_x,bounds.min_y,legend_x+legend_width*paper_to_m,bounds.max_y)
    reference="site-settings:"+identity
    source_index.append(SourceReference(reference,"Supplied drawing settings",1.,(
        SourceProvenance("manual",identity,None,None,"drawing settings",1.,"user"),)))
    notes=[(text,(reference,)) for text in [f"Scale 1:{scale:g} (print at 100%)",f"Input units: {request['units']}",
           "Generic equipment mark: square", "Building: heavy solid", "Curb: normal solid", "Parking: thin gray",
           "Landscape: gray dotted", "Supplied route: solid", "Future route: dashed", "No geometry acquired from address"]]
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
    if len(notes)*4+40>(legend_bounds.height/paper_to_m):
        raise ValueError("legend exceeds drawing height; supply greater site extent or smaller explicit scale")
    lp=[]
    for index,(note,sources) in enumerate(notes):
        if measure_sheet_text(note,2.5,font_family="DejaVu Sans")>legend_width-6:
            raise ValueError("legend text exceeds measured width; shorten qualification or enlarge legend_width_mm")
        lp.append(DrawingPrimitive("legend:"+str(index),"text","site:legend",sources,
                   (Point2(legend_x+3*paper_to_m,legend_bounds.max_y-(5+index*4)*paper_to_m),),text=note))
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


def render_site_body(request, sheet):
    """Existing renderer content as a bounded paper-mm subtree for company sheets.

    The existing physical renderer currently returns a complete SVG only. Render
    on a staging surface, retain source-mapped content nodes, and discard its
    generic stationery. The company layer alone renders final sheet stationery.
    No caller XML or unreferenced nodes are passed into the company pipeline.
    """
    import xml.etree.ElementTree as ET
    drawings,placements,audit=prepare_site_plan(request)
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


def create_site_plan(request_path,profile_path,job_path,*,output_directory):
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
    if sheet.fields.get("scale")!=f"1:{_number(request['drawing']['scale'],'scale',positive=True):g}":
        raise ValueError("company sheet scale field must explicitly match site drawing scale as 1:N")
    body,drawings,_,audit,source_map=render_site_body(request,sheet)
    attempt=Path(output_directory).absolute()
    attempt.parent.mkdir(parents=True,exist_ok=True)
    attempt.mkdir()
    pending=attempt/".pending"
    pending.mkdir()
    try:
        (pending/"site-input.json").write_bytes(raw)
        (pending/"site-body.svg").write_text(body,encoding="utf-8")
        (pending/"drawing.json").write_text(drawings.to_json()+"\n",encoding="utf-8")
        # This composition is content-only staging, not a second branded sheet.
        (pending/"site-audit.json").write_bytes(_json(audit))
        (pending/"site-body.map.json").write_bytes(_json(source_map))
        shared=create_company_sheet_set(profile_path,job_path,
                   output_directory=pending/"company-sheet-set",bodies={sheet.id:body})
        shared=Path(shared)
        shared_relative=shared.relative_to(pending).as_posix()
        if ((shared/"inputs/profile.json").read_bytes()!=profile_raw or
                (shared/"inputs/job.json").read_bytes()!=job_raw):
            raise ValueError("shared profile/job changed during site production; rebuild from stable inputs")
        receipts=[]
        for path in sorted(pending.rglob("*")):
            if path.is_file():
                data=path.read_bytes()
                receipts.append({"path":path.relative_to(pending).as_posix(),"bytes":len(data),"sha256":_sha(data)})
        manifest={"version":VERSION,"status":"supplied_geometry_requires_review","engineering_approval":False,
                  "input_sha256":_sha(raw),"profile_sha256":_sha(profile_raw),"job_sha256":_sha(job_raw),"sheet_id":sheet.id,"scale":request["drawing"]["scale"],
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
    args=parser.parse_args(argv)
    try:
        path=create_site_plan(args.request,args.profile,args.job,output_directory=args.output)
    except (ValueError,OSError,ImportError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(path)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
