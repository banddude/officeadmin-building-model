from dataclasses import replace
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.drawings import (
    Bounds2, DrawingDimension, DrawingPrimitive, DrawingSet, DrawingView,
    LineStyle, PlanSpec, Point2, SheetSpec, SourceReference, ViewPlacement,
    generate_drawing_set, VisibilityPolicy, compact_label_provider,
)
from oabm.drawings.sheet_svg import SheetSymbol, SheetSvgOptions, render_sheet_svg
from oabm.model import BuildingModel

NS = {'s': 'http://www.w3.org/2000/svg'}


def inputs():
    d = DrawingSet('synthetic-model', (
        DrawingView('plan', 'plan', 'Example plan', Bounds2(0, 0, 4, 5), 50,
            (DrawingPrimitive('line', 'line', 'architecture:walls', ('wall',), (Point2(1, 2), Point2(3, 4))),
             DrawingPrimitive('label', 'text', 'annotations:labels', ('wall',), (Point2(1, 1),), text='Example wall')),
            (DrawingDimension('dim', ('wall',), Point2(1, 2), Point2(3, 2), .5, 2, '2 m'),)),
        ), (), (SourceReference('wall', 'Example wall', .7),))
    s = SheetSpec('S-1', 300, 200, titleblock_height_mm=30, title='Synthetic example', revision='A',
                  titleblock_fields=(('status', 'TEST ONLY'),))
    p = (ViewPlacement('main', 'plan', 10, 10),)
    return d, s, p


def render(**kwargs):
    return render_sheet_svg(*inputs(), **kwargs)


def test_physical_svg_xml_reopens_with_visible_text_and_source_mapping():
    output = render()
    root = ET.fromstring(output.svg)
    assert root.attrib == {'width': '300mm', 'height': '200mm', 'viewBox': '0 0 300 200'}
    assert root.find('s:polyline', NS).attrib['points'] == '30,70 70,30'
    texts = [element.text for element in root.findall('.//s:text', NS)]
    assert 'Example wall' in texts
    assert '2 m' in texts
    assert 'S-1 | Synthetic example' in texts
    assert 'Revision: A' in texts
    assert 'status: TEST ONLY' in texts
    assert len(root.findall('.//s:polygon', NS)) == 2  # dimension arrows
    manifest = output.manifest()
    assert manifest['svg_sha256'] == hashlib.sha256(output.svg.encode()).hexdigest()
    ids = {e.get('id'): e for e in root.iter() if e.get('id')}
    assert set(ids) == {item[key] for item in manifest['items'] for key in ('svg_id', 'svg_label_id') if key in item}
    assert len(ids) == 4
    assert all(item['source_ids'] == ['wall'] for item in manifest['items'])
    assert all(json.loads(ids[item['svg_id']].attrib['data-source-ids']) == ['wall'] for item in manifest['items'])
    dimension = [i for i in manifest['items'] if i['kind'] == 'dimension'][0]
    assert len(ids[dimension['svg_id']].findall('s:line', NS)) == 3
    assert 'transform=' not in output.svg and 'currentColor' not in output.svg
    assert 'clipPath' not in output.svg


def test_repeatability_source_bytes_and_detached_manifest():
    d, s, p = inputs()
    before = d.to_json()
    a = render_sheet_svg(d, s, p)
    b = render_sheet_svg(d, s, p)
    assert a == b
    assert d.to_json() == before
    m = a.manifest()
    m['items'].clear()
    assert a.manifest()['items']


def test_xml_escaping_prevents_markup_or_resource_injection():
    d, s, p = inputs()
    value = '<script src="https://invalid.test/">&evil</script>'
    label = replace(d.views[0].primitives[1], text=value)
    d = replace(d, views=(replace(d.views[0], primitives=(label,)),))
    result = render_sheet_svg(d, replace(s, title='"<&>'), p)
    root = ET.fromstring(result.svg)
    assert root.find('.//s:script', NS) is None
    assert root.find('.//s:text', NS).text == value
    assert not any('href' in key for node in root.iter() for key in node.attrib)


@pytest.mark.parametrize('value', ['bad\x00text', 'bad\ud800text', 'line\nbreak', 'tab\there'])
def test_forbidden_or_unlaid_out_text_fails(value):
    d, s, p = inputs()
    label = replace(d.views[0].primitives[1], text=value)
    d = replace(d, views=(replace(d.views[0], primitives=(label,)),))
    with pytest.raises(ValueError):
        render_sheet_svg(d, s, p)


def with_symbol():
    d, s, p = inputs()
    symbol = DrawingPrimitive('symbol', 'symbol', 'electrical:devices', ('wall',), (Point2(2, 2),), symbol='test:diamond')
    d = replace(d, views=(replace(d.views[0], primitives=(symbol,), dimensions=()),))
    shape = SheetSymbol(((Point2(-1, 0), Point2(0, -1)), (Point2(0, -1), Point2(1, 0)),
                          (Point2(1, 0), Point2(0, 1)), (Point2(0, 1), Point2(-1, 0))))
    return d, s, p, shape


def test_symbols_need_explicit_registry_and_are_real_editable_segments():
    d, s, p, shape = with_symbol()
    with pytest.raises(ValueError, match='no explicit geometry'):
        render_sheet_svg(d, s, p)
    output = render_sheet_svg(d, s, p, symbols={'test:diamond': shape})
    root = ET.fromstring(output.svg)
    symbol = root.find('s:g', NS)
    assert symbol.attrib['data-symbol'] == 'test:diamond'
    assert len(symbol.findall('s:line', NS)) == 4
    assert symbol.find('s:line', NS).attrib['x1'] == '49'
    assert output.manifest()['symbol_geometry_mm']['test:diamond'][0] == [[-1, 0], [0, -1]]


def test_symbol_extents_cannot_escape_view_or_hide_label():
    d, s, p, shape = with_symbol()
    with pytest.raises(ValueError, match='exceeds'):
        render_sheet_svg(d, s, p, symbols={'test:diamond': SheetSymbol(((Point2(-100, 0), Point2(100, 0)),))})
    d = replace(d, views=(replace(d.views[0], primitives=(replace(d.views[0].primitives[0], text='unplaced label'),)),))
    with pytest.raises(ValueError, match='separate positioned'):
        render_sheet_svg(d, s, p, symbols={'test:diamond': shape})


@pytest.mark.parametrize('segments', [(), ('ab',), ((Point2(0, 0), Point2(0, 0)),), ((1, 2),)])
def test_invalid_symbol_geometry(segments):
    with pytest.raises(ValueError):
        SheetSymbol(segments)


@pytest.mark.parametrize('style', [LineStyle(weight='guess'), LineStyle(pattern='wavy'), LineStyle(stroke='custom')])
def test_unknown_style_fails(style):
    d, s, p = inputs()
    d = replace(d, views=(replace(d.views[0], primitives=(replace(d.views[0].primitives[0], style=style),)),))
    with pytest.raises(ValueError, match='unsupported semantic'):
        render_sheet_svg(d, s, p)


def test_known_styles_have_explicit_physical_weights_and_dashes():
    d, s, p = inputs()
    d = replace(d, views=(replace(d.views[0], primitives=(replace(d.views[0].primitives[0], style=LineStyle(stroke='space', weight='thin', pattern='dash')),)),))
    polyline = ET.fromstring(render_sheet_svg(d, s, p).svg).find('s:polyline', NS)
    assert polyline.attrib['stroke-width'] == '0.13'
    assert polyline.attrib['stroke-dasharray'] == '3 1.5'
    assert polyline.attrib['stroke'] == '#666666'


@pytest.mark.parametrize('kwargs', [dict(text_height_mm=0), dict(text_height_mm=1e-12), dict(arrow_length_mm=float('inf')),
                                    dict(title_text_height_mm=True), dict(font_family=''), dict(font_family='bad\nfont')])
def test_invalid_options(kwargs):
    with pytest.raises(ValueError):
        SheetSvgOptions(**kwargs)


def test_titleblock_capacity_and_presence_are_explicit():
    d, s, p = inputs()
    with pytest.raises(ValueError, match='reserved titleblock'):
        render_sheet_svg(d, replace(s, titleblock_height_mm=0, titleblock_fields=()), p)
    with pytest.raises(ValueError, match='insufficient height'):
        render_sheet_svg(d, replace(s, titleblock_height_mm=5), p)
    with pytest.raises(ValueError, match='row capacity'):
        render_sheet_svg(d, replace(s, title='W' * 200), p)


def test_arrow_extent_and_short_dimensions_fail():
    d, s, p = inputs()
    with pytest.raises(ValueError, match='too short'):
        render_sheet_svg(d, s, p, options=SheetSvgOptions(arrow_length_mm=50))
    dim = replace(d.views[0].dimensions[0], start=Point2(0, 2), end=Point2(3, 2))
    d = replace(d, views=(replace(d.views[0], dimensions=(dim,)),))
    with pytest.raises(ValueError, match='exceeds'):
        render_sheet_svg(d, s, p)


def test_canonical_generator_to_svg_does_not_mutate_model():
    root = Path(__file__).resolve().parents[1]
    model = BuildingModel.load(root / 'fixtures/drawings/v1/drawing-model.json')
    before = model.to_json()
    # Explicit architecture-only view. No inferred electrical symbols are hidden.
    drawing = generate_drawing_set(model, plans=(PlanSpec(id='plan', level_id='level:ground',
        bounds=Bounds2(-1, -1, 8, 8), scale=100,
        visibility=VisibilityPolicy(electrical=False, routes=False, dimensions=False)),),
        symbol_provider=lambda entity, view_type: None, label_provider=compact_label_provider)
    _, spec, placements = inputs()
    rendered = render_sheet_svg(drawing, spec, placements)
    assert ET.fromstring(rendered.svg).findall('s:polyline', NS) or ET.fromstring(rendered.svg).findall('s:polygon', NS)
    assert model.to_json() == before
    assert rendered.manifest()['model_id'] == model.model_id


def test_halos_are_explicit_configurable_presentation_only():
    a = ET.fromstring(render().svg).find('.//s:text', NS)
    b = ET.fromstring(render(options=SheetSvgOptions(text_halo_mm=0)).svg).find('.//s:text', NS)
    assert a.attrib['paint-order'] == 'stroke fill'
    assert a.attrib['stroke'] == '#ffffff'
    assert a.attrib['stroke-width'] == '0.7'
    assert 'paint-order' not in b.attrib
    assert a.text == b.text
    with pytest.raises(ValueError):
        SheetSvgOptions(text_halo_mm=-1)

def test_generator_visibility_metadata_is_retained():
    d, s, p = inputs()
    d = replace(d, views=(replace(d.views[0], metadata=(('skipped_labels', '2'),)),))
    assert render_sheet_svg(d, s, p).manifest()['views'][0]['metadata'] == [['skipped_labels', '2']]

def test_public_synthetic_example_is_reproducible_and_never_overwrites(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(root / 'src'))
    paths = [tmp_path / 'first', tmp_path / 'second']
    for path in paths:
        subprocess.run([sys.executable, str(root / 'examples/physical_sheet_svg.py'), str(path)], env=environment, check=True, capture_output=True)
    for name in ('sheet.svg', 'sheet-manifest.json', 'drawing-set.json'):
        assert (paths[0] / name).read_bytes() == (paths[1] / name).read_bytes()
    assert ET.fromstring((paths[0] / 'sheet.svg').read_bytes()).attrib['width'] == '420mm'
    retry = subprocess.run([sys.executable, str(root / 'examples/physical_sheet_svg.py'), str(paths[0])], env=environment, capture_output=True)
    assert retry.returncode != 0
    assert (paths[0] / 'sheet.svg').read_bytes() == (paths[1] / 'sheet.svg').read_bytes()


def test_text_halos_paint_after_all_geometry_regardless_of_source_id_order():
    d, s, p = inputs()
    # Labels deliberately sort before a wall and dimensions in source-ID order.
    d = replace(d, views=(replace(d.views[0], primitives=(
        replace(d.views[0].primitives[1], id='aaa-label'),
        replace(d.views[0].primitives[0], id='zzz-wall'),
    )),))
    output = render_sheet_svg(d, s, p)
    root = ET.fromstring(output.svg)
    children = list(root)
    mapped_ids = {item['svg_id'] for item in output.manifest()['items']}
    geometry_positions = [i for i, node in enumerate(children) if node.get('id') in mapped_ids and node.tag != '{http://www.w3.org/2000/svg}text']
    text_positions = [i for i, node in enumerate(children) if node.tag == '{http://www.w3.org/2000/svg}text' and node.get('data-source-ids')]
    assert len(text_positions) == 2
    assert min(text_positions) > max(geometry_positions)
    dimension = next(item for item in output.manifest()['items'] if item['kind'] == 'dimension')
    label = next(node for node in children if node.get('id') == dimension['svg_label_id'])
    assert label.attrib['data-dimension-group'] == dimension['svg_id']
    assert json.loads(label.attrib['data-source-ids']) == dimension['source_ids']
