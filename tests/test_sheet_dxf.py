"""New synthetic CAD fixtures; no customer drawings or upstream code."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ezdxf = pytest.importorskip('ezdxf')

from oabm.drawings import (
    Bounds2, DrawingDimension, DrawingPrimitive, DrawingSet, DrawingView,
    LineStyle, Point2, SheetSpec, SourceProvenance, SourceReference, ViewPlacement,
)
from oabm.drawings.sheet_dxf import sheet_to_dxf


def inputs():
    provenance = SourceProvenance('synthetic', 'cad-fixture', None, None, 'known-answer', .8)
    primitives = (
        DrawingPrimitive('edge', 'line', 'architecture:walls', ('wall',), (Point2(1, 2), Point2(3, 4)), style=LineStyle(weight='heavy')),
        DrawingPrimitive('label', 'text', 'annotations:labels', ('wall',), (Point2(1, 3),), text='Wall A'),
        DrawingPrimitive('outline', 'polygon', 'architecture:spaces', ('wall',), (Point2(1, 1), Point2(2, 1), Point2(2, 2)), style=LineStyle(stroke='space', pattern='dash')),
        DrawingPrimitive('path', 'polyline', 'routing', ('wall',), (Point2(2, 3), Point2(3, 3), Point2(4, 3)), style=LineStyle(stroke='route', pattern='dot')),
    )
    dimension = DrawingDimension('width', ('wall',), Point2(1, 2), Point2(3, 2), .5, 2, '2 m')
    view = DrawingView('plan', 'plan', 'Synthetic plan', Bounds2(0, 0, 5, 5), 50, primitives, (dimension,))
    drawing = DrawingSet('synthetic-cad-model', (view,), (), (SourceReference('wall', 'Synthetic wall', .8, (provenance,)),))
    spec = SheetSpec('S-1', 300, 220, titleblock_height_mm=35, title='Synthetic CAD proof', revision='A', titleblock_fields=(('Author', 'Example'),))
    return drawing, spec, (ViewPlacement('main', 'plan', 10, 10),)


def export(**changes):
    drawing, spec, placements = inputs()
    return sheet_to_dxf(changes.get('drawing', drawing), changes.get('spec', spec), changes.get('placements', placements))


def read(artifact):
    return ezdxf.read(io.StringIO(artifact.dxf.decode('utf-8')))


def test_raw_layer_references_are_declared_and_viewport_layer_does_not_plot():
    # Inspect the returned tag stream before any library can repair or supply
    # defaults. ezdxf's readback audit does not flag an undeclared entity layer.
    artifact = export()
    assert artifact.manifest()['version'] == '1.0.1'
    lines = artifact.dxf.decode('utf-8').splitlines()
    assert len(lines) % 2 == 0
    records = []
    for code, value in zip(lines[::2], lines[1::2]):
        tag = (int(code), value)
        if tag[0] == 0:
            records.append([])
        records[-1].append(tag)

    section = table = None
    layers, references, viewports = {}, set(), []
    for record in records:
        tags = dict(record)
        kind = tags[0]
        if kind == 'SECTION':
            section = tags[2]
        elif kind == 'ENDSEC':
            section = None
        elif section == 'TABLES':
            if kind == 'TABLE':
                table = tags[2]
            elif kind == 'ENDTAB':
                table = None
            elif table == 'LAYER' and kind == 'LAYER':
                assert tags[2] not in layers
                layers[tags[2]] = tags
        elif section in {'ENTITIES', 'BLOCKS'}:
            if 8 in tags:
                references.add(tags[8])
            if kind == 'VIEWPORT':
                viewports.append(tags)

    assert references <= layers.keys(), references - layers.keys()
    assert len(viewports) == 1
    assert viewports[0][8] == 'VIEWPORTS'
    assert layers['VIEWPORTS'][290] == '0'  # Explicit non-plot infrastructure.


def test_legacy_replay_profile_preserves_geometry_and_source_companion():
    from oabm.drawings.sheet_dxf import _sheet_to_dxf
    legacy = _sheet_to_dxf(*inputs(), version='1.0.0')
    current = export()
    assert 'VIEWPORTS' not in read(legacy).layers
    assert read(current).layers.get('VIEWPORTS').dxf.plot == 0
    old_map, new_map = legacy.manifest(), current.manifest()
    assert old_map.pop('version') == '1.0.0'
    assert new_map.pop('version') == '1.0.1'
    assert old_map.pop('dxf_sha256') != new_map.pop('dxf_sha256')
    for mapping in (old_map, new_map):
        for entry in mapping['entities']:
            entry.pop('handle')
    assert old_map == new_map
    def graphics(artifact):
        return [
            (entity.dxftype(), {k: v for k, v in entity.dxf.all_existing_dxf_attribs().items()
                               if k not in {'handle', 'owner'}},
             list(entity.get_points()) if entity.dxftype() == 'LWPOLYLINE' else None)
            for entity in read(artifact).layouts.get('Layout1')
        ]
    assert graphics(legacy) == graphics(current)
    with pytest.raises(ValueError, match='unsupported DXF exporter version'):
        _sheet_to_dxf(*inputs(), version='unrecognized')


@pytest.mark.skipif(ezdxf.__version__ != '1.4.5', reason='historical bytes are bound to ezdxf 1.4.5')
def test_legacy_profile_matches_actual_pre_fix_bytes():
    from oabm.drawings.sheet_dxf import _sheet_to_dxf
    # Recorded from these synthetic inputs on unmodified e5f7540b, before the
    # viewport fix. Do not rebaseline by regenerating with the replay profile.
    legacy = _sheet_to_dxf(*inputs(), version='1.0.0')
    assert hashlib.sha256(legacy.dxf).hexdigest() == '27a67358b9d0b4a920fe0dc5dd1b58fcdc9af66b2f7407ac45d5303d2f637995'
    assert hashlib.sha256(legacy.manifest_json.encode()).hexdigest() == '1dc8aa2e9ce193311db236f95b1faf929dfaa89d4c6fe062676c6d6c31aff16a'


def test_exact_paper_layout_units_scale_geometry_and_sources():
    drawing, spec, placements = inputs()
    before = drawing.to_json()
    artifact = sheet_to_dxf(drawing, spec, placements)
    doc = read(artifact)
    assert not doc.modelspace()
    assert doc.header['$TILEMODE'] == 0
    assert doc.layouts.active_layout().name == 'Layout1'
    assert doc.units == ezdxf.units.MM
    layout = doc.layouts.get('Layout1')
    setup = layout.dxf_layout.dxf
    assert (setup.paper_width, setup.paper_height) == (300, 220)
    assert (setup.scale_numerator, setup.scale_denominator) == (1, 1)
    assert setup.plot_paper_units == 1
    assert setup.plot_type == 5
    assert setup.plot_rotation == 0
    assert (setup.left_margin, setup.bottom_margin, setup.plot_origin_x_offset, setup.plot_origin_y_offset) == (0, 0, 0, 0)
    manifest = artifact.manifest()
    edge = next(e for e in manifest['entities'] if e.get('primitive_id') == 'edge')
    entity = doc.entitydb[edge['handle']]
    assert entity.dxftype() == 'LINE'
    assert tuple(entity.dxf.start) == (30, 150, 0)
    assert tuple(entity.dxf.end) == (70, 190, 0)
    assert entity.dxf.lineweight == 50
    assert edge['source_ids'] == ['wall']
    assert edge['placement_id'] == 'main'
    assert manifest['composition']['source_index'] == drawing.to_dict()['source_index']
    assert manifest['dxf_sha256'] == hashlib.sha256(artifact.dxf).hexdigest()
    assert drawing.to_json() == before
    assert not doc.audit().errors
    assert not doc.audit().fixes


def test_every_graphic_entity_has_companion_mapping_and_native_editability():
    artifact = export()
    doc = read(artifact)
    entries = artifact.manifest()['entities']
    graphics = [e for e in doc.layouts.get('Layout1') if e.dxftype() != 'VIEWPORT']
    assert {e.dxf.handle for e in graphics} == {e['handle'] for e in entries}
    assert len(entries) == len(graphics)
    assert set(e.dxftype() for e in graphics) == {'LINE', 'LWPOLYLINE', 'TEXT'}
    assert all(e['entity_type'] == doc.entitydb[e['handle']].dxftype() for e in entries)
    dimension = [e for e in entries if e.get('dimension_id') == 'width']
    assert [e['role'] for e in dimension] == ['dimension-line', 'dimension-extension', 'dimension-extension', 'dimension-label']
    assert all(e['value_m'] == 2 and e['associative'] is False for e in dimension)
    assert doc.entitydb[dimension[-1]['handle']].dxf.text == '2 m'
    assert not doc.layouts.get('Layout1').query('DIMENSION INSERT IMAGE')
    edge = doc.entitydb[next(e['handle'] for e in entries if e.get('primitive_id') == 'edge')]
    edge.dxf.end = (77, 180, 0)
    assert edge.dxf.end.x == 77  # Editable, but no canonical writeback API.


def test_styles_titleblock_and_polylines_are_explicit():
    artifact = export()
    doc = read(artifact)
    mapping = artifact.manifest()
    entities = {e.get('primitive_id'): doc.entitydb[e['handle']] for e in mapping['entities'] if 'primitive_id' in e}
    assert entities['outline'].closed
    assert not entities['path'].closed
    assert entities['outline'].dxf.linetype == 'OABM_DASH'
    assert entities['path'].dxf.linetype == 'OABM_DOT'
    assert entities['label'].dxf.height == 2.5
    assert doc.styles.get('OABM_TEXT').dxf.font == 'DejaVuSansMono.ttf'
    labels = [doc.entitydb[e['handle']].dxf.text for e in mapping['entities'] if e['role'] == 'titleblock-text']
    assert labels == ['Sheet: S-1', 'Title: Synthetic CAD proof', 'Revision: A', 'Author: Example']
    assert len(mapping['layers']) == len(set(mapping['layers'].values()))


def test_deterministic_reordered_placements_and_delayed_process(tmp_path):
    drawing, spec, _ = inputs()
    placements = (ViewPlacement('a', 'plan', 10, 10), ViewPlacement('b', 'plan', 150, 10))
    a = sheet_to_dxf(drawing, spec, placements)
    b = sheet_to_dxf(drawing, spec, tuple(reversed(placements)))
    assert a == b
    # A different process and wall clock must not change bytes or global config.
    script = '''import runpy,time,ezdxf
m=runpy.run_path('tests/test_sheet_dxf.py')
time.sleep(1.1)
a=m['export']()
from pathlib import Path
Path(__import__('sys').argv[1]).write_bytes(a.dxf)
assert not ezdxf.options.write_fixed_meta_data_for_testing
'''
    target = tmp_path / 'again.dxf'
    for seed in ('1', '2', '42'):
        subprocess.run([sys.executable, '-c', script, str(target)], check=True, env={**os.environ, 'PYTHONHASHSEED': seed})
        assert target.read_bytes() == export().dxf
    assert not ezdxf.options.write_fixed_meta_data_for_testing
    assert len({e['handle'] for e in a.manifest()['entities']}) == len(a.manifest()['entities'])


@pytest.mark.parametrize('text', ['', 'line\nbreak', 'tab\ttext', 'degree °', '%%d', r'\U+0041', 'A'*150])
def test_unsupported_text_rejected(text):
    drawing, _, _ = inputs()
    view = drawing.views[0]
    primitives = tuple(replace(p, text=text) if p.kind == 'text' else p for p in view.primitives)
    with pytest.raises(ValueError, match='text'):
        export(drawing=replace(drawing, views=(replace(view, primitives=primitives),)))


@pytest.mark.parametrize('style', [LineStyle(stroke='guess'), LineStyle(weight='bold'), LineStyle(pattern='hidden')])
def test_unknown_styles_rejected(style):
    drawing, _, _ = inputs()
    view = drawing.views[0]
    with pytest.raises(ValueError, match='style'):
        export(drawing=replace(drawing, views=(replace(view, primitives=(replace(view.primitives[0], style=style),)),)))


def test_unsupported_symbols_not_silently_dropped():
    drawing, _, _ = inputs()
    symbol = DrawingPrimitive('device', 'symbol', 'electrical', ('wall',), (Point2(1, 1),), symbol='receptacle')
    with pytest.raises(ValueError, match='symbol'):
        export(drawing=replace(drawing, views=(replace(drawing.views[0], primitives=(symbol,)),)))


@pytest.mark.parametrize('primitive', [
    DrawingPrimitive('bad', 'line', 'x', ('wall',), (Point2(1,1), Point2(2,2), Point2(3,3))),
    DrawingPrimitive('bad', 'line', 'x', ('wall',), (Point2(1,1), Point2(2,2)), closed=True),
    DrawingPrimitive('bad', 'line', 'x', ('wall',), (Point2(1,1), Point2(1,1))),
    DrawingPrimitive('bad', 'polygon', 'x', ('wall',), (Point2(1,1), Point2(2,2))),
    DrawingPrimitive('bad', 'polyline', 'x', ('wall',), (Point2(1,1), Point2(2,2)), text='hidden'),
])
def test_unsupported_geometry_semantics_rejected(primitive):
    drawing, _, _ = inputs()
    with pytest.raises(ValueError):
        export(drawing=replace(drawing, views=(replace(drawing.views[0], primitives=(primitive,)),)))


def test_titleblock_refusals_and_no_titleblock_mode():
    _, spec, _ = inputs()
    with pytest.raises(ValueError, match='too short'):
        export(spec=replace(spec, titleblock_height_mm=5))
    with pytest.raises(ValueError, match='row width'):
        export(spec=replace(spec, title='A' * 150))
    with pytest.raises(ValueError, match='reserved titleblock'):
        export(spec=replace(spec, titleblock_height_mm=0, titleblock_fields=()))
    artifact = export(spec=replace(spec, titleblock_height_mm=0, titleblock_fields=(), title='', revision=''))
    assert not any(e['role'].startswith('titleblock') for e in artifact.manifest()['entities'])


def test_layer_names_are_lossless_distinct_and_no_external_refs():
    drawing, _, _ = inputs()
    view = drawing.views[0]
    primitives = (replace(view.primitives[0], id='a', layer='a:b'), replace(view.primitives[0], id='b', layer='a_b'))
    artifact = export(drawing=replace(drawing, views=(replace(view, primitives=primitives),)))
    layers = artifact.manifest()['layers']
    assert layers['a:b'] != layers['a_b']
    assert bytes.fromhex(layers['a:b'][5:]).decode() == 'a:b'
    assert not any(block.block.is_xref for block in read(artifact).blocks)


def test_unknown_sources_and_oversized_views_still_fail_composition():
    drawing, spec, _ = inputs()
    with pytest.raises(ValueError, match='source reference'):
        export(drawing=replace(drawing, source_index=()))
    with pytest.raises(ValueError, match='drawing area'):
        export(placements=(ViewPlacement('p', 'plan', 250, 10),))


@pytest.mark.parametrize('anchor', [(0, 0), (.1, 0), (.1, 5), (4.9, 2.5)])
def test_text_bearings_and_descenders_do_not_clip(anchor):
    primitive = DrawingPrimitive('text', 'text', 'annotations', ('wall',), (Point2(*anchor),), text='gyp')
    view = DrawingView('plan', 'plan', 'plan', Bounds2(0, 0, 5, 5), 50, (primitive,))
    drawing = DrawingSet('model', (view,), (), (SourceReference('wall', None, 1),))
    with pytest.raises(ValueError, match='text exceeds'):
        sheet_to_dxf(drawing, SheetSpec('sheet', 100, 100, margin_mm=0), (ViewPlacement('p', 'plan', 0, 0),))


def test_new_synthetic_canonical_model_end_to_end():
    import runpy
    demo = runpy.run_path('spikes/physical_sheet_dxf/demo.py')
    model = demo['synthetic_model']()
    before = model.to_json()
    artifact = demo['make_demo'](model)
    doc = read(artifact)
    assert model.to_json() == before
    assert artifact.manifest()['composition']['model_id'] == model.model_id
    assert {ref['canonical_id'] for ref in artifact.manifest()['composition']['source_index']} >= {'south', 'east', 'north', 'west'}
    assert len(doc.layouts.get('Layout1').query('LWPOLYLINE')) == 5
    labels = [doc.entitydb[e['handle']] for e in artifact.manifest()['entities'] if e['role'] == 'dimension-label']
    assert {e.dxf.rotation for e in labels} == {0, 90}
    assert all(e.dxf.halign == 1 for e in labels)


def test_optional_dependency_missing_is_explicit_and_core_import_is_light(monkeypatch):
    subprocess.run([sys.executable, '-c', "import sys; import oabm.drawings; assert 'ezdxf' not in sys.modules"], check=True)
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'ezdxf':
            raise ImportError('simulated missing optional dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)
    with pytest.raises(ImportError, match=r'\[cad\]'):
        export()


@pytest.mark.parametrize('start,end', [((.1,1),(.1,4)), ((1,4.9),(4,4.9))])
def test_rotated_dimension_label_bounds_refuse_near_paper_edge(start, end):
    dimension = DrawingDimension('dim', ('wall',), Point2(*start), Point2(*end), 0, 3, '3 m')
    view = DrawingView('plan', 'plan', 'plan', Bounds2(0, 0, 5, 5), 50, (), (dimension,))
    drawing = DrawingSet('model', (view,), (), (SourceReference('wall', None, 1),))
    with pytest.raises(ValueError, match='text exceeds'):
        sheet_to_dxf(drawing, SheetSpec('sheet', 100, 100, margin_mm=0), (ViewPlacement('p', 'plan', 0, 0),))
