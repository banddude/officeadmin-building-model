"""Synthetic cardinal elevations: orientation and depth, not a geometry surrogate."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from oabm.model import BuildingModel
from oabm.native_bonsai import NativeRuntime, export_native, validate_request

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'examples/synthetic-native-bonsai-model.json'
IFC_NS = '{http://www.ifcopenshell.org/ns}'


def elevation(identifier='east', direction='+X', camera=None, **changes):
    result = {'id': identifier, 'target_view': 'ELEVATION_VIEW',
              'camera_xyz_m': camera or [-3, 0, 1.5], 'direction': direction,
              'near_m': .002, 'far_m': 10, 'width_m': 8, 'height_m': 4,
              'scale_denominator': 50, 'exclude_ids': [], 'annotations': []}
    result.update(changes)
    return result


def request(view=None):
    return {'version': 'native-bonsai-v1', 'acknowledged_body_omissions': [],
            'views': [view or elevation()]}


@pytest.fixture
def model():
    return BuildingModel.load(MODEL)


@pytest.mark.parametrize('direction', ['+X', '-X', '+Y', '-Y'])
def test_cardinal_request_valid_without_mutation(model, direction):
    data = request(elevation(direction=direction))
    original = copy.deepcopy(data)
    source = model.to_dict()
    assert validate_request(data, model) == original
    assert data == original and model.to_dict() == source


@pytest.mark.parametrize('changes', [
    {'camera_xyz_m': [0, 0]}, {'camera_xyz_m': [0, 0, 0, 0]},
    {'camera_xyz_m': {'x': 0, 'y': 0, 'z': 0}}, {'camera_xyz_m': None},
    {'camera_xyz_m': [True, 0, 0]}, {'camera_xyz_m': [0, float('nan'), 0]},
    {'camera_xyz_m': [0, 0, float('inf')]}, {'camera_xyz_m': [0, 0, '1']},
    {'camera_xyz_m': [100.001, 0, 0]}, {'camera_xyz_m': [-100.001, 0, 0]},
    {'direction': '+Z'}, {'direction': '-Z'}, {'direction': 'X'},
    {'direction': [1, 0, 0]}, {'direction': None}, {'direction': True},
    {'near_m': 0}, {'near_m': -.002}, {'near_m': .001999},
    {'near_m': .003}, {'near_m': 4},
    {'near_m': True}, {'near_m': float('nan')}, {'near_m': '0.002'},
    {'far_m': 0}, {'far_m': .002}, {'far_m': 200.001},
    {'far_m': False}, {'far_m': float('inf')}, {'far_m': '10'},
    {'near_m': 6, 'far_m': 5}, {'near_m': 6, 'far_m': 6},
    {'width_m': 0}, {'height_m': -1}, {'width_m': True},
    {'height_m': float('nan')}, {'scale_denominator': True},
    {'scale_denominator': 1.5}, {'scale_denominator': 0},
    {'annotations': [{'source_id': 'device:L1', 'kind': 'text', 'text': 'L1',
                      'label_offset_mm': [0, 0]}]},
    {'annotations': None}, {'annotations': {}},
    {'level_id': 'level:ground'}, {'center_m': [0, 0]},
    {'cut_height_m': 1.5}, {'space_annotations': []},
    {'look_at_xyz_m': [0, 0, 0]}, {'up_axis': '+Z'},
    {'exclude_ids': ['missing']}, {'exclude_ids': ['wall:synthetic-1'] * 2},
])
def test_invalid_or_ambiguous_elevation_refused(model, changes):
    with pytest.raises(ValueError):
        validate_request(request(elevation(**changes)), model)


@pytest.mark.parametrize('direction,camera', [
    ('+X', [-100, 0, 0]), ('-X', [100, 0, 0]),
    ('+Y', [0, -100, 0]), ('-Y', [0, 100, 0]),
])
def test_full_depth_extent_can_touch_local_boundary(model, direction, camera):
    assert validate_request(request(elevation(direction=direction, camera=camera,
                                               far_m=200)), model)


@pytest.mark.parametrize('view', [
    elevation(camera=[99, 0, 0], far_m=2),
    elevation(direction='-X', camera=[-99, 0, 0], far_m=2),
    elevation(direction='+Y', camera=[0, 99, 0], far_m=2),
    elevation(direction='-Y', camera=[0, -99, 0], far_m=2),
    elevation(camera=[0, 99, 0], width_m=4),
    elevation(direction='+Y', camera=[99, 0, 0], width_m=4),
    elevation(camera=[0, 0, 99], height_m=4),
    elevation(camera=[0, 0, -99], height_m=4),
])
def test_camera_inside_limit_but_frustum_outside_refused(model, view):
    with pytest.raises(ValueError):
        validate_request(request(view), model)


def test_existing_plan_rcp_request_unchanged(model):
    data = json.loads((ROOT / 'examples/synthetic-native-bonsai-request.json').read_text())
    original = copy.deepcopy(data)
    assert validate_request(data, model) == original
    assert data == original


def test_elevation_native_scale24_is_explicitly_supported(model):
    assert validate_request(request(elevation(scale_denominator=24)), model)


def test_public_mixed_elevation_example(model):
    data = json.loads((ROOT / 'examples/synthetic-native-elevations-request.json').read_text())
    assert validate_request(data, model) == data
    assert {v['target_view'] for v in data['views']} == {
        'PLAN_VIEW', 'REFLECTED_PLAN_VIEW', 'ELEVATION_VIEW'}


def _runtime():
    blender = os.environ.get('OABM_TEST_BLENDER')
    bonsai = os.environ.get('OABM_TEST_BONSAI_PYTHON')
    if not blender and not bonsai:
        pytest.skip('Native elevation integration NOT executed: installed runtime paths not supplied')
    if not blender or not bonsai:
        pytest.fail('Both explicit native runtime paths are required')
    return NativeRuntime(Path(blender), Path(bonsai))


def asymmetric_model():
    """Four invented boxes; no reused project geometry or physical design claim."""
    data = BuildingModel.load(MODEL).to_dict()
    for key in ('walls', 'ceilings', 'slabs', 'spaces', 'electrical_devices'):
        data[key] = []
    data['model_id'] = 'model:synthetic-elevations'
    data['name'] = 'Asymmetric cardinal elevation test'
    data['obstacles'] = []
    for name, xyz, size in [
        ('A', (0, -1.4, .5), (.4, .6, 1)),
        ('B', (2, .9, 1.1), (.6, .9, 2.2)),
        ('C', (4, 2.1, .35), (.4, .5, .7)),
        ('behind', (-5, .2, .8), (.3, .4, 1.6)),
        ('crossing', (1, -2.8, .6), (1.2, .4, 1.2)),
    ]:
        data['obstacles'].append({
            'id': 'obstacle:' + name, 'name': name, 'obstacle_type': 'hard',
            'level_id': 'level:ground', 'clearance_m': 0,
            'geometry': {'kind': 'box3d', 'pose': {
                'position': dict(zip('xyz', xyz)),
                'rotation': {'w': 1, 'x': 0, 'y': 0, 'z': 0}},
                'size': dict(zip('xyz', size))},
            'attributes': {'synthetic_only': True}, 'confidence': 1,
            'provenance': [{'source_kind': 'synthetic', 'source_id': 'fixture:cardinal-elevations',
                            'method': 'invented asymmetric boxes', 'attributes': {}}],
        })
    data['obstacles'][-1]['geometry']['pose']['rotation'] = {
        'w': math.cos(math.pi/8), 'x': 0, 'y': 0, 'z': math.sin(math.pi/8)}
    return BuildingModel.from_dict(data)


def mixed_request():
    base = {'level_id': 'level:ground', 'center_m': [1, 0], 'cut_height_m': 1.5,
            'width_m': 8, 'height_m': 6, 'scale_denominator': 50,
            'exclude_ids': [], 'annotations': []}
    views = [dict(base, id='plan', target_view='PLAN_VIEW'),
             dict(base, id='rcp', target_view='REFLECTED_PLAN_VIEW')]
    views += [elevation('east', '+X', [-3, 0, 1.5]),
              elevation('west', '-X', [7, 0, 1.5]),
              elevation('north', '+Y', [1, -4, 1.5]),
              elevation('south', '-Y', [1, 4, 1.5]),
              elevation('depth', '+X', [1, 0, 1.5], far_m=2),
              elevation('excluded', '+X', [-3, 0, 1.5], exclude_ids=['obstacle:B'])]
    return dict(request(), views=views)


def _product_bounds(path, guid):
    tree = ET.parse(path).getroot()
    groups = [g for g in tree.iter() if g.attrib.get(IFC_NS + 'guid') == guid
              or guid in g.attrib.get('class', '').split()]
    points = []
    for g in groups:
        for node in g.iter():
            if not node.tag.endswith('path'):
                continue
            # Native rectangular-box projections use absolute M/L commands.
            d = node.attrib.get('d', '')
            assert not re.search(r'[ACHQSTVac hqstvlmz]'.replace(' ', ''), d), d
            for x, y in re.findall(r'[ML]\s*([-+\d.eE]+)[,\s]+([-+\d.eE]+)', d):
                points.append((float(x), float(y)))
    if not points:
        return None
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def assert_cardinal_geometry(output, data):
    from oabm.ifc import canonical_id_to_ifc_guid
    receipt = json.loads((output / 'receipt.json').read_text())
    centers = {}
    for spec in data['views'][2:]:
        view = receipt['views'][spec['id']]
        svg = output / view['svg']
        tree = ET.parse(svg).getroot()
        assert tree.attrib['data-scale'] == '1:50'
        assert view['camera_xyz_m'] == spec['camera_xyz_m']
        assert view['direction'] == spec['direction']
        assert view['clip_depth_m'] == [spec['near_m'], spec['far_m']]
        assert view['native_drawing_depth_m'] == [0, spec['far_m']]
        assert view['automatic_reference_markers'] is False
        assert view['annotations'] == []
        found = {name: _product_bounds(svg, canonical_id_to_ifc_guid('obstacle:' + name))
                 for name in ('A', 'B', 'C', 'behind', 'crossing')}
        expected = {'B', 'crossing'} if spec['id'] == 'depth' else {'A', 'C', 'crossing'} if spec['id'] == 'excluded' else {'A', 'B', 'C', 'crossing'}
        assert {name for name, bounds in found.items() if bounds} == expected, (spec['id'], found)
        if spec['id'] == 'depth':
            bounds = found['crossing']
            assert bounds[2] - bounds[0] == pytest.approx(.6 * math.sqrt(2) * 1000/50, abs=.03)
        if spec['id'] == 'east':
            bounds = found['crossing']
            assert bounds[2] - bounds[0] == pytest.approx(.8 * math.sqrt(2) * 1000/50, abs=.03)
        centers[spec['id']] = {name: (bounds[0] + bounds[2])/2 for name, bounds in found.items() if bounds}
        if 'B' in expected:
            bounds = found['B']
            assert bounds[3] - bounds[1] == pytest.approx(2.2 * 1000/50, abs=.02)
            expected_width = .9 if spec['direction'].endswith('X') else .6
            assert bounds[2] - bounds[0] == pytest.approx(expected_width * 1000/50, abs=.02)
    # Z-up cardinal cameras: +X sees +Y left; +Y sees +X right.
    assert centers['east']['C'] < centers['east']['B'] < centers['east']['A']
    assert centers['west']['A'] < centers['west']['B'] < centers['west']['C']
    assert centers['north']['A'] < centers['north']['B'] < centers['north']['C']
    assert centers['south']['C'] < centers['south']['B'] < centers['south']['A']
    for spec in data['views']:
        tree = ET.parse(output / receipt['views'][spec['id']]['svg']).getroot()
        # Creating mixed views must not add unsolicited elevation/reference symbols.
        assert not any('IfcAnnotation' in node.attrib.get('class', '').split()
                       for node in tree.iter())


def test_real_mixed_cardinal_elevations_and_depth_clipping(tmp_path):
    runtime = _runtime()
    model, data = asymmetric_model(), mixed_request()
    source, req = tmp_path/'model.json', tmp_path/'request.json'
    source.write_text(model.to_json())
    req.write_text(json.dumps(data))
    output = export_native(source, req, tmp_path/'native', runtime)
    assert_cardinal_geometry(output, data)
    import ifcopenshell
    from oabm.ifc import from_ifc, round_trip_differences
    assert round_trip_differences(model, from_ifc(ifcopenshell.open(str(output/'model.ifc')))) == ()
    assert source.read_text() == model.to_json()
    relocated = tmp_path/'relocated'
    _reopen_and_regenerate(runtime, output, relocated)
    assert_cardinal_geometry(relocated, data)
    assert round_trip_differences(model, from_ifc(ifcopenshell.open(str(relocated/'model.ifc')))) == ()


def _reopen_and_regenerate(runtime, output, relocated):
    """Independent fixed test script: original resources unavailable to Python I/O."""
    shutil.copytree(output, relocated,
                    ignore=shutil.ignore_patterns('.runtime', 'cache', '*.h5'))
    for path in (relocated/'drawings').glob('*.svg'):
        path.unlink()
    script = relocated.parent/'regenerate_elevations.py'
    script.write_text('''import sys, pathlib, json
source, installed, bundle, forbidden = sys.argv[sys.argv.index('--')+1:]
sys.path[:0] = [source, installed]
def audit(event, args):
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        if str(args[0]).startswith(forbidden):
            raise RuntimeError('Original delivery resource access forbidden')
sys.addaudithook(audit)
import bpy, addon_utils
addon_utils.enable('bonsai', default_set=True, persistent=True)
import bonsai.tool as tool
root = pathlib.Path(bundle)
assert bpy.ops.bim.load_project(filepath=str(root/'model.ifc'), should_start_fresh_session=True) == {'FINISHED'}
f = tool.Ifc.get()
receipt = json.loads((root/'receipt.json').read_text())
request = json.loads((root/'inputs/request.json').read_text())
checked = []
for spec in request['views']:
    row = receipt['views'][spec['id']]
    drawing = f.by_guid(row['drawing_guid'])
    assert bpy.ops.bim.activate_drawing(drawing=drawing.id(), should_view_from_camera=False) == {'FINISHED'}
    camera = bpy.context.scene.camera
    if spec['target_view'] == 'ELEVATION_VIEW':
        assert all(abs(camera.location[i]-spec['camera_xyz_m'][i]) < .00001 for i in range(3))
        assert abs(camera.data.clip_start-.002) < .000001
        assert abs(camera.data.clip_end-spec['far_m']) < .00001
        look = -camera.matrix_world.col[2].xyz
        expected = {'+X':(1,0,0),'-X':(-1,0,0),'+Y':(0,1,0),'-Y':(0,-1,0)}[spec['direction']]
        assert all(abs(look[i]-expected[i]) < .00001 for i in range(3))
        checked.append(spec['id'])
    cp = tool.Drawing.get_camera_props(camera)
    assert abs(cp.width-spec['width_m']) < .01
    assert abs(cp.height-spec['height_m']) < .01
    rx, ry = cp.update_camera_resolution()
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = rx, ry
    bpy.context.view_layer.update()
    props = tool.Drawing.get_document_props()
    props.should_use_annotation_cache = props.should_use_linework_cache = False
    assert bpy.ops.bim.create_drawing(open_viewer=False) == {'FINISHED'}
    uri = pathlib.Path(tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing))).resolve()
    assert uri.is_relative_to(root.resolve()) and uri.is_file()
assert bpy.ops.bim.save_project(filepath=str(root/'model.ifc')) == {'FINISHED'}
(root/'relocated-proof.json').write_text(json.dumps({'persisted_elevation_cameras_checked':checked,
    'delivered_svgs_removed':True,'original_resource_reads_forbidden':True}))
''')
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH') if k in os.environ}
    env['OMP_NUM_THREADS'] = '2'
    for key in ('HOME', 'BLENDER_USER_CONFIG', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
        folder = relocated/'.reopen-runtime'/key
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        env[key] = str(folder)
    with (relocated/'regenerate.log').open('wb') as stream:
        subprocess.run([str(runtime.blender), '-b', '--factory-startup', '--disable-autoexec',
                        '--offline-mode', '-noaudio', '--threads', '2', '--python-exit-code', '1',
                        '--python', str(script), '--', str(ROOT/'src'), str(runtime.bonsai_python),
                        str(relocated), str(output)], cwd=relocated, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, timeout=600, check=True)
    proof = json.loads((relocated/'relocated-proof.json').read_text())
    assert set(proof['persisted_elevation_cameras_checked']) == {
        'east', 'west', 'north', 'south', 'depth', 'excluded'}
