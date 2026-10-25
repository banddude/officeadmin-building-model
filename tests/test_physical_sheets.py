from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from oabm.drawings import (
    Bounds2, DrawingDimension, DrawingPrimitive, DrawingSet, DrawingView,
    LineStyle, PlanSpec, Point2, SheetSpec, SourceProvenance, SourceReference,
    ViewPlacement, compose_sheet, generate_drawing_set,
)
from oabm.model import BuildingModel


def drawing():
    provenance = SourceProvenance('synthetic', 'fixture', 'wall-line', None, 'test', .7, 'inferred')
    primitive = DrawingPrimitive('edge', 'line', 'architecture:walls', ('wall',),
                                 (Point2(1, 2), Point2(3, 4)), style=LineStyle(weight='heavy'))
    dimension = DrawingDimension('dim', ('wall',), Point2(1, 2), Point2(3, 2), .5, 2, '2 m')
    view = DrawingView('plan', 'plan', 'Synthetic plan', Bounds2(0, 0, 4, 5), 50,
                       (primitive,), (dimension,))
    return DrawingSet('synthetic-model', (view,), (), (SourceReference('wall', 'Test wall', .8, (provenance,)),))


def sheet(**kwargs):
    return SheetSpec('S-1', 300, 200, titleblock_height_mm=20, title='Synthetic sheet',
                     revision='A', titleblock_fields=(('designer', 'Example'),), **kwargs)


def place(**kwargs):
    return ViewPlacement(**({'id': 'main', 'view_id': 'plan', 'x_mm': 10, 'y_mm': 10} | kwargs))


def test_exact_physical_scale_axis_dimension_and_provenance():
    source = drawing()
    before = source.to_json()
    result = compose_sheet(source, sheet(), (place(),)).to_dict()
    view = result['views'][0]
    assert view['bounds_mm'] == [10, 10, 90, 110]
    assert view['primitives'][0]['points_mm'] == [{'x': 30, 'y': 70}, {'x': 70, 'y': 30}]
    dim = view['dimensions'][0]
    assert dim['line_start_mm'] == {'x': 30, 'y': 60}
    assert dim['line_end_mm'] == {'x': 70, 'y': 60}
    assert dim['text_anchor_mm'] == {'x': 50, 'y': 60}
    assert dim['value_m'] == 2
    assert dim['text'] == '2 m'
    assert result['titleblock']['bounds_mm'] == [10, 170, 290, 190]
    assert result['source_index'] == source.to_dict()['source_index']
    assert result['drawing_set_sha256'] == hashlib.sha256(source.to_json(indent=None).encode()).hexdigest()
    assert view['primitives'][0]['source_ids'] == ['wall']
    assert view['primitives'][0]['style']['weight'] == 'heavy'
    assert source.to_json() == before


def test_repeatability_order_detachment_and_duplicate_view_placements():
    placements = (place(), place(id='second', x_mm=100))
    result = compose_sheet(drawing(), sheet(), placements)
    assert result.to_json() == compose_sheet(drawing(), sheet(), tuple(reversed(placements))).to_json()
    mutated = result.to_dict()
    mutated['views'][0]['primitives'].clear()
    assert len(result.to_dict()['views'][0]['primitives']) == 1
    assert json.loads(result.to_json(indent=None)) == result.to_dict()


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), '10'])
def test_invalid_paper_numbers(value):
    with pytest.raises(ValueError):
        SheetSpec('sheet', value, 200)


@pytest.mark.parametrize('kwargs', [dict(width_mm=20), dict(height_mm=40), dict(titleblock_height_mm=-1),
                                  dict(titleblock_fields=(('a', 'x'), ('a', 'y'))), dict(titleblock_fields=(('', 'x'),)),
                                  dict(titleblock_fields=(('a', 3),)), dict(id=''), dict(revision=1)])
def test_invalid_sheet_fields(kwargs):
    values = dict(id='sheet', width_mm=300, height_mm=200, titleblock_height_mm=20)
    with pytest.raises(ValueError):
        SheetSpec(**(values | kwargs))


def test_titleblock_data_requires_reservation():
    with pytest.raises(ValueError, match='reserved'):
        SheetSpec('sheet', 300, 200, titleblock_fields=(('a', 'b'),))


@pytest.mark.parametrize('kwargs', [dict(x_mm=-1), dict(y_mm=True), dict(x_mm=float('nan')), dict(id=''), dict(view_id='')])
def test_invalid_placement(kwargs):
    with pytest.raises(ValueError):
        place(**kwargs)


@pytest.mark.parametrize('placements,message', [
    ((), 'at least one'), ((place(), place()), 'duplicate placement'),
    ((place(view_id='missing'),), 'unknown view'),
    ((place(x_mm=0),), 'available drawing area'),
    ((place(y_mm=80),), 'available drawing area'),
    ((place(x_mm=250),), 'available drawing area'),
    ((place(), place(id='other', x_mm=20)), 'overlap'),
])
def test_invalid_layouts(placements, message):
    with pytest.raises(ValueError, match=message):
        compose_sheet(drawing(), sheet(), placements)


def test_touching_view_bounds_are_allowed():
    result = compose_sheet(drawing(), sheet(), (place(), place(id='other', x_mm=90)))
    assert len(result.to_dict()['views']) == 2


@pytest.mark.parametrize('change,message', [
    (lambda d: replace(d, views=d.views * 2), 'duplicate drawing view'),
    (lambda d: replace(d, source_index=d.source_index * 2), 'duplicate canonical'),
    (lambda d: replace(d, source_index=()), 'source reference'),
    (lambda d: replace(d, views=(replace(d.views[0], dimensions=d.views[0].dimensions * 2),)), 'duplicate dimension'),
    (lambda d: replace(d, views=(replace(d.views[0], primitives=(replace(d.views[0].primitives[0], kind='custom'),)),)), 'unsupported primitive'),
    (lambda d: replace(d, views=(replace(d.views[0], primitives=(replace(d.views[0].primitives[0], points=(Point2(0, 0), Point2(9, 9))),)),)), 'geometry outside'),
    (lambda d: replace(d, views=(replace(d.views[0], dimensions=(replace(d.views[0].dimensions[0], offset_m=10),)),)), 'geometry outside'),
])
def test_invalid_drawing_references_and_geometry(change, message):
    with pytest.raises(ValueError, match=message):
        compose_sheet(change(drawing()), sheet(), (place(),))


def test_text_symbols_and_metadata_remain_explicit_tokens():
    d = drawing()
    text = DrawingPrimitive('label', 'text', 'labels', ('wall',), (Point2(1, 1),), text='Example')
    symbol = DrawingPrimitive('symbol', 'symbol', 'devices', ('wall',), (Point2(2, 2),), symbol='test-symbol', metadata=(('origin', 'test'),))
    d = replace(d, views=(replace(d.views[0], primitives=(text, symbol), metadata=(('review', 'needed'),)),))
    v = compose_sheet(d, sheet(), (place(),)).to_dict()['views'][0]
    assert v['primitives'][0]['text'] == 'Example'
    assert v['primitives'][1]['symbol'] == 'test-symbol'
    assert v['primitives'][1]['metadata'] == [['origin', 'test']]
    assert v['metadata'] == [['review', 'needed']]


def test_real_generator_consumption_leaves_model_and_drawings_unchanged():
    root = Path(__file__).resolve().parents[1]
    model = BuildingModel.load(root / 'fixtures/drawings/v1/drawing-model.json')
    before = model.to_json()
    generated = generate_drawing_set(model, plans=(PlanSpec(id='plan', level_id='level:ground',
                        bounds=Bounds2(-1, -1, 8, 8), scale=100),))
    before_drawing = generated.to_json()
    result = compose_sheet(generated, sheet(), (place(),))
    assert result.to_dict()['views'][0]['primitives']
    assert model.to_json() == before
    assert generated.to_json() == before_drawing


def test_future_drawing_version_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="output version"):
        compose_sheet(replace(drawing(), output_version="2.0.0"), sheet(), (place(),))

@pytest.mark.parametrize("scale", [1e13, 1e14, 1e308])
def test_extreme_scale_cannot_collapse_paper_bounds(scale):
    d = drawing()
    d = replace(d, views=(replace(d.views[0], scale=scale),))
    with pytest.raises(ValueError, match="collapse"):
        compose_sheet(d, sheet(), (place(),))


@pytest.mark.parametrize("fields", [("ab",), "ab", None, (("a",),)])
def test_titleblock_pairs_must_be_structured(fields):
    with pytest.raises(ValueError, match="key/value pairs"):
        SheetSpec("sheet", 300, 200, titleblock_height_mm=20, titleblock_fields=fields)
