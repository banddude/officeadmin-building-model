"""Bounded SVG-view label offsets; not a physical-sheet or font-shaping engine."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math

from .generator import _offset_point
from .model import DrawingPrimitive, DrawingView, LineStyle, Point2

VERSION = "view-label-layout-v1"


def _positive(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


def _text_box(anchor, text, height, *, centered=False):
    # SVG flips model Y. Glyph ascenders therefore occupy POSITIVE model Y;
    # reserve a quarter em below the baseline for descenders. One em per code
    # point plus bearings is deliberately conservative, not measured font fit.
    width = max(1, len(text)) * height
    left = anchor.x - (width / 2 if centered else 0)
    return left - .1*height, anchor.y - .25*height, left + width + .1*height, anchor.y + height


def _inflate(box, amount):
    return box[0]-amount, box[1]-amount, box[2]+amount, box[3]+amount


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _segment_hits_box(a, b, box):
    low, high = 0., 1.
    for start, end, minimum, maximum in ((a.x,b.x,box[0],box[2]),(a.y,b.y,box[1],box[3])):
        delta = end-start
        if abs(delta) < 1e-12:
            if start < minimum or start > maximum:
                return False
        else:
            first, last = sorted(((minimum-start)/delta,(maximum-start)/delta))
            low, high = max(low,first), min(high,last)
            if low > high:
                return False
    return True


def _point_segment_distance(p, a, b):
    dx, dy = b.x-a.x, b.y-a.y
    length2 = dx*dx+dy*dy
    at = 0 if length2 == 0 else max(0.,min(1.,((p.x-a.x)*dx+(p.y-a.y)*dy)/length2))
    return math.hypot(p.x-a.x-at*dx,p.y-a.y-at*dy)


def _segments_close(a,b,c,d,gap):
    def cross(p,q,r):
        return (q.x-p.x)*(r.y-p.y)-(q.y-p.y)*(r.x-p.x)
    boxes_touch = (max(min(a.x,b.x),min(c.x,d.x)) <= min(max(a.x,b.x),max(c.x,d.x)) and
                   max(min(a.y,b.y),min(c.y,d.y)) <= min(max(a.y,b.y),max(c.y,d.y)))
    if boxes_touch and cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0:
        return True
    return min(_point_segment_distance(a,c,d),_point_segment_distance(b,c,d),
               _point_segment_distance(c,a,b),_point_segment_distance(d,a,b)) <= gap


def layout_view_labels(view: DrawingView, *, pixels_per_model_unit: float,
                       text_height_m: float, clearance_px: float = 2.) -> DrawingView:
    """Offset annotation labels around SVG symbols, strokes and other text.

    Pass the SAME pixel scale and text height to view_to_svg. Legacy generator
    and serializer defaults are unchanged. Generate labels with label_overlap
    'keep' before this pass so a prior suppression pass cannot discard candidates.
    Only annotations:labels text moves. No canonical model is accepted or changed.
    Unplaced labels are omitted from primitives but retained in full in metadata.
    SVG symbol radius is the existing fixed four pixels. Collision estimates
    reserve conservative text rectangles; this is not exact font measurement.
    """
    for value,label in ((pixels_per_model_unit,"pixels_per_model_unit"),
                        (text_height_m,"text_height_m"),(clearance_px,"clearance_px")):
        _positive(value,label)
    config = json.dumps({"pixels_per_model_unit":float(pixels_per_model_unit),
                         "text_height_m":float(text_height_m),"clearance_px":float(clearance_px)},sort_keys=True)
    metadata = dict(view.metadata)
    if "label_layout_version" in metadata:
        if metadata["label_layout_version"] == VERSION and metadata.get("label_layout_config") == config:
            return view
        raise ValueError("label layout already applied; regenerate the original view for different settings")
    gap, radius = clearance_px/pixels_per_model_unit, 4/pixels_per_model_unit
    labels = [p for p in view.primitives if p.kind == "text" and p.layer == "annotations:labels"]
    label_ids = {p.id for p in labels}
    others = [p for p in view.primitives if p.id not in label_ids]
    boxes, segments = [], []
    for item in others:
        if item.kind == "symbol":
            p = item.points[0]
            boxes.append(((p.x-radius,p.y-radius,p.x+radius,p.y+radius), item.source_ids))
        elif item.kind == "text":
            boxes.append((_text_box(item.points[0],item.text or "",text_height_m),item.source_ids))
        else:
            pts = item.points + ((item.points[0],) if item.closed or item.kind == "polygon" else ())
            segments.extend((a,b,item.source_ids) for a,b in zip(pts,pts[1:]))
    for dim in view.dimensions:
        a = _offset_point(dim.start,dim.end,dim.offset_m)
        b = _offset_point(dim.end,dim.start,-dim.offset_m)
        anchor = Point2(x=(a.x+b.x)/2,y=(a.y+b.y)/2)
        boxes.append((_text_box(anchor,dim.text,text_height_m,centered=True),()))
        segments.append((a,b,()))
    occupied = []
    unplaced, moved = [], 0
    ids = {p.id for p in view.primitives}
    placed = []
    for label in sorted(labels,key=lambda p:(tuple(sorted(p.source_ids)),p.id)):
        origin, text = label.points[0], label.text or ""
        width, h = max(1,len(text))*text_height_m, text_height_m
        source_set = set(label.source_ids)
        owns_symbol = any(p.kind == "symbol" and p.points[0] == origin and
                          set(p.source_ids)&source_set for p in others)
        candidates = [origin]
        # Finite candidates, closest offsets first. Clamp whole text boxes to
        # the existing view; never expand bounds or shrink/truncate label text.
        for distance in (radius+gap,radius+gap+h,radius+gap+2*h):
            for x,y in ((origin.x+distance+.1*h,origin.y+distance+.25*h),
                        (origin.x+distance+.1*h,origin.y-distance-h),
                        (origin.x-distance-width-.1*h,origin.y+distance+.25*h),
                        (origin.x-distance-width-.1*h,origin.y-distance-h),
                        (origin.x-width/2,origin.y+distance+.25*h),
                        (origin.x-width/2,origin.y-distance-h),
                        (origin.x+distance+.1*h,origin.y-.375*h),
                        (origin.x-distance-width-.1*h,origin.y-.375*h)):
                if width+.2*h > view.bounds.width or 1.25*h > view.bounds.height:
                    continue
                candidate = Point2(x=max(view.bounds.min_x+.1*h,min(x,view.bounds.max_x-width-.1*h)),
                                   y=max(view.bounds.min_y+.25*h,min(y,view.bounds.max_y-h)))
                if candidate not in candidates:
                    candidates.append(candidate)
        selected = None
        for anchor in candidates:
            box = _text_box(anchor,text,h)
            if (box[0] < view.bounds.min_x-1e-9 or box[1] < view.bounds.min_y-1e-9 or
                    box[2] > view.bounds.max_x+1e-9 or box[3] > view.bounds.max_y+1e-9):
                continue
            expanded = _inflate(box,gap)
            if any(_overlap(expanded,b) for b,_ in boxes) or any(_overlap(expanded,b) for b in occupied):
                continue
            if any(_segment_hits_box(a,b,expanded) for a,b,_ in segments):
                continue
            end = Point2(x=max(box[0],min(origin.x,box[2])), y=max(box[1],min(origin.y,box[3])))
            length = math.hypot(end.x-origin.x,end.y-origin.y)
            start_offset = radius if owns_symbol else 0.
            start = origin if length <= start_offset else Point2(
                x=origin.x+(end.x-origin.x)*start_offset/length,
                y=origin.y+(end.y-origin.y)*start_offset/length)
            if any(not set(sources)&set(label.source_ids) and _segment_hits_box(start,end,_inflate(b,gap)) for b,sources in boxes):
                continue
            if any(_segment_hits_box(start,end,_inflate(b,gap)) for b in occupied):
                continue
            if any(not set(sources)&set(label.source_ids) and _segments_close(start,end,a,b,gap)
                   for a,b,sources in segments):
                continue
            selected = (anchor,box,start,end)
            break
        if selected is None:
            unplaced.append({"id":label.id,"source_ids":label.source_ids,"text":text,
                             "anchor":{"x":origin.x,"y":origin.y},
                             "reason":"text_exceeds_view" if width+.2*h > view.bounds.width or 1.25*h > view.bounds.height else "no_clear_candidate"})
            continue
        anchor,box,start,end = selected
        occupied.append(box)
        placed.append(replace(label,points=(anchor,),metadata=label.metadata+(
            ("label_layout_original_x",str(origin.x)),("label_layout_original_y",str(origin.y)))))
        if anchor != origin:
            moved += 1
            if start != end:
                identity = "label-leader:"+hashlib.sha256(label.id.encode()).hexdigest()[:24]
                if identity in ids:
                    raise ValueError("label leader ID conflicts with an existing primitive")
                ids.add(identity)
                leader = DrawingPrimitive(id=identity,kind="line",layer="annotations:label-leaders",
                    source_ids=label.source_ids,points=(start,end),style=LineStyle(stroke="annotation",weight="thin"),
                    metadata=(("label_id",label.id),))
                placed.append(leader)
                segments.append((start,end,label.source_ids))
    return replace(view,primitives=tuple(sorted(others+placed,key=lambda p:(p.layer,p.source_ids,p.id))),
                   metadata=view.metadata+(("label_layout_version",VERSION),("label_layout_config",config),
                       ("relocated_labels",str(moved)),("unplaced_labels_count",str(len(unplaced))),
                       ("unplaced_labels",json.dumps(unplaced,sort_keys=True))))
