"""Synthetic derived views: preserve geometry and disclose bounded text layout."""
from dataclasses import replace
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.drawings import (Bounds2, DrawingDimension, DrawingPrimitive, DrawingView,
                          PlanSpec, Point2, generate_plan, layout_view_labels, view_to_svg)
from oabm.model import BuildingModel


def primitive(identity,kind,points,*,text=None,symbol=None,source=None,layer=None,closed=False):
    return DrawingPrimitive(id=identity,kind=kind,points=tuple(Point2(x=x,y=y) for x,y in points),
        source_ids=(source or identity,),text=text,symbol=symbol,closed=closed,
        layer=layer or ("annotations:labels" if kind=="text" else "geometry"))


def view(*primitives,bounds=None,dimensions=()):
    return DrawingView(id="view:synthetic",view_type="plan",title="Synthetic label placement",
        scale=40,bounds=bounds or Bounds2(min_x=-2,min_y=-2,max_x=3,max_y=3),
        primitives=tuple(primitives),dimensions=dimensions)


def layout(v,**kwargs):
    return layout_view_labels(v,pixels_per_model_unit=100,text_height_m=.1,**kwargs)


def label(v,identity="label:a"):
    return next(p for p in v.primitives if p.id==identity)


def test_actual_svg_text_clears_symbol_and_route_with_traceable_leader():
    symbol=primitive("symbol:a","symbol",((0,0),),symbol="panel",source="equipment:a")
    text=primitive("label:a","text",((0,0),),text="Panel A",source="equipment:a")
    route=primitive("route:b","polyline",((-1,0),(2,0)),source="route:b")
    original=view(symbol,text,route)
    result=layout(original)
    moved=label(result)
    assert moved.points[0] != Point2(x=0,y=0)
    assert symbol in result.primitives and route in result.primitives
    leader=next(p for p in result.primitives if p.layer=="annotations:label-leaders")
    assert leader.source_ids==text.source_ids
    assert dict(leader.metadata)["label_id"]==text.id
    assert original.primitives==(symbol,text,route)
    # Inspect actual serialized pixel positions, not just the layout helper.
    svg=ET.fromstring(view_to_svg(result,pixels_per_model_unit=100,text_height_m=.1))
    node=svg.find(".//{http://www.w3.org/2000/svg}text")
    assert node is not None and node.text=="Panel A"
    x,y=float(node.attrib['x']),float(node.attrib['y'])
    sx,sy=200.,300.  # original origin in this view's pixel frame
    # Conservative ascenders/descenders and bearings at 10 px text height.
    box=(x-1,y-10,x+len(node.text)*10+1,y+2.5)
    assert not (box[0]<sx+4 and box[2]>sx-4 and box[1]<sy+4 and box[3]>sy-4)
    assert box[3]<sy-1 or box[1]>sy+1
    assert 'data-source-ids="equipment:a"' in view_to_svg(result)


def test_already_clear_label_stays_and_repeat_is_idempotent():
    text=primitive("label:a","text",((0,0),),text="clear")
    original=view(text)
    result=layout(original)
    assert label(result).points==text.points
    assert layout(result) is result
    assert layout_view_labels(result,pixels_per_model_unit=100.,text_height_m=.1) is result
    with pytest.raises(ValueError,match="already applied"):
        layout_view_labels(result,pixels_per_model_unit=200,text_height_m=.1)


def test_reordered_inputs_have_identical_layout_and_label_collisions_are_avoided():
    items=(primitive("symbol:a","symbol",((0,0),),symbol="panel",source="a"),
           primitive("symbol:b","symbol",((.2,.05),),symbol="panel",source="b"),
           primitive("label:a","text",((0,0),),text="A",source="a"),
           primitive("label:b","text",((.2,.05),),text="B",source="b"))
    a,b=layout(view(*items)),layout(view(*reversed(items)))
    assert a==b
    assert dict(a.metadata)["unplaced_labels_count"]=="0"
    first,second=label(a).points[0],label(a,"label:b").points[0]
    # Distinct safe boxes, not two labels at the same symbol anchor.
    assert abs(first.x-second.x) >= .12 or abs(first.y-second.y) >= .125


def test_long_label_is_kept_whole_when_it_fits_and_clamped_within_view():
    text=primitive("label:a","text",((.9,.5),),text="LONG LABEL",source="a")
    bounds=Bounds2(min_x=0,min_y=0,max_x=1.5,max_y=1)
    result=layout(view(text,bounds=bounds))
    placed=label(result)
    assert placed.text=="LONG LABEL"
    assert placed.points[0].x+.1*len(placed.text)+.01 <= 1.5+1e-9
    assert placed.points[0].x-.01 >= 0
    assert dict(result.metadata)["unplaced_labels_count"]=="0"


def test_unplaceable_long_label_is_not_truncated_or_drawn_outside_bounds():
    full="Synthetic equipment label too long for the bounded view"
    text=primitive("label:a","text",((0,0),),text=full,source="a")
    result=layout(view(text,bounds=Bounds2(min_x=-.1,min_y=-.1,max_x=.1,max_y=.1)))
    assert not any(p.id==text.id for p in result.primitives)
    missing=json.loads(dict(result.metadata)["unplaced_labels"])
    assert missing==[{"id":text.id,"source_ids":["a"],"text":full,
                     "anchor":{"x":0.,"y":0.},"reason":"text_exceeds_view"}]
    assert full not in view_to_svg(result)
    assert dict(result.metadata)["unplaced_labels_count"]=="1"


def test_closed_polygon_edge_and_dimension_text_are_obstacles():
    polygon=primitive("outline","polygon",((-.005,-.5),(.3,-.5),(.3,.5),(-.005,.5)),closed=True)
    text=primitive("label:a","text",((0,0),),text="A",source="outline")
    dim=DrawingDimension(id="dimension:a",source_ids=("outline",),start=Point2(x=-1,y=.2),
                         end=Point2(x=1,y=.2),value_m=2,text="Dimension",offset_m=0)
    result=layout(view(polygon,text,dimensions=(dim,)))
    assert polygon in result.primitives and result.dimensions==(dim,)
    assert dict(result.metadata)["unplaced_labels_count"]=="0"
    assert label(result).points[0]!=text.points[0]


def test_no_clear_candidate_reports_label_and_preserves_all_geometry():
    text=primitive("label:a","text",((0,0),),text="X",source="a")
    # A symbol footprint fills the tiny view, leaving no label candidate.
    symbol=primitive("symbol:block","symbol",((0,0),),symbol="obstruction")
    original=view(symbol,text,bounds=Bounds2(min_x=-.04,min_y=-.04,max_x=.04,max_y=.04))
    result=layout(original)
    assert result.primitives==(symbol,)
    assert dict(result.metadata)["unplaced_labels_count"]=="1"


@pytest.mark.parametrize("argument",[0,-1,True,float("nan"),float("inf")])
def test_invalid_layout_settings(argument):
    with pytest.raises(ValueError):
        layout_view_labels(view(),pixels_per_model_unit=argument,text_height_m=.1)


def test_existing_generator_defaults_unchanged_and_production_fixture_is_fixed():
    fixture=Path(__file__).parents[1]/"fixtures/model_delivery/v1/model.json"
    model=BuildingModel.load(fixture)
    spec=PlanSpec(id="plan:synthetic",level_id="level:synthetic")
    original=generate_plan(model,spec)
    assert next(p for p in original.primitives if p.layer=="annotations:labels").points==(Point2(x=0,y=0),)
    before=model.to_json()
    result=layout_view_labels(original,pixels_per_model_unit=100,text_height_m=.125)
    assert next(p for p in result.primitives if p.layer=="annotations:labels").points!=(Point2(x=0,y=0),)
    assert model.to_json()==before
    assert generate_plan(model,spec)==original


def test_route_stroke_beyond_symbol_changes_the_selected_offset():
    symbol=primitive("symbol:a","symbol",((0,0),),symbol="panel",source="a")
    text=primitive("label:a","text",((0,0),),text="Panel",source="a")
    clear=layout(view(symbol,text))
    route=primitive("route:block","line",((-1,.1),(2,.1)),source="route:block")
    result=layout(view(symbol,text,route))
    assert label(clear).points[0].y > 0
    assert label(result).points[0].y < 0
    assert route in result.primitives


def test_leader_cannot_cross_a_different_route_to_reach_clear_text():
    symbol=primitive("symbol:a","symbol",((0,0),),symbol="panel",source="a")
    text=primitive("label:a","text",((0,0),),text="A",source="a")
    route=primitive("route:block","line",((.03,-.5),(.03,.5)),source="route:block")
    result=layout(view(symbol,text,route))
    assert label(result).points[0].x < 0
    leader=next(p for p in result.primitives if p.layer=="annotations:label-leaders")
    assert all(p.x < .03-.02 for p in leader.points)
    assert route in result.primitives
