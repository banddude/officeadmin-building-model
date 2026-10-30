"""Validated JSON boundary and fixed-worker launcher. Never execute request content."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import struct
from typing import Any

from oabm.model import BuildingModel


# Bonsai 0.8.5 auto-offsets points beyond 1000 m on any axis. Stay an
# order of magnitude inside that threshold and bound float32 placement error.
LOCAL_COORDINATE_LIMIT_M = 100.0


def _local_coordinate(value, context):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > LOCAL_COORDINATE_LIMIT_M:
        raise ValueError(f'local-coordinate scope: {context} must remain within +/-100 m; georeferenced/large-coordinate export unsupported')


def _local_model(model):
    document = model.to_dict()
    coordinates = document['coordinate_system']
    if coordinates.get('crs') is not None or coordinates.get('origin_in_crs') is not None:
        raise ValueError('local-coordinate scope: CRS/origin_in_crs unsupported; do not strip georeferencing to imply equivalent output')
    def visit(value, context):
        if isinstance(value, dict):
            if set(value) == {'x', 'y', 'z'}:
                for axis in ('x', 'y', 'z'):
                    _local_coordinate(value[axis], context + '.' + axis)
            for key, child in value.items():
                if key not in ('attributes', 'provenance'):
                    if key == 'elevation_m':
                        _local_coordinate(child, context + '.' + key)
                    visit(child, context + '.' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{context}[{index}]')
    visit(document, 'model')


@dataclass(frozen=True)
class NativeRuntime:
    """Explicit operator-trusted installed executables/packages, not downloaded assets."""
    blender: Path
    bonsai_python: Path


def _object(value: Any, keys: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f'{name}: exact keys required: {sorted(keys)}')
    return value


def _number(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'{name}: finite number in [{lo}, {hi}] required')
    return value


def _pair(value: Any, name: str, lo: float, hi: float) -> None:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f'{name}: two numbers required')
    for item in value:
        _number(item, name, lo, hi)



def elevation_frame(view):
    """Reuse the drawing projection convention; camera right is its negative.

    ProjectionFrame.normal points into the scene. Blender's local +Z points
    backwards, so a proper right-handed camera uses (-right, up, -normal).
    """
    from oabm.drawings.projection import frame_from_view_direction
    from oabm.model import Point3, Vector3
    directions = {'+X': (1, 0, 0), '-X': (-1, 0, 0), '+Y': (0, 1, 0), '-Y': (0, -1, 0)}
    return frame_from_view_direction(
        origin=Point3(**dict(zip('xyz', view['camera_xyz_m']))),
        direction=Vector3(**dict(zip('xyz', directions[view['direction']]))))


def _elevation_volume(view):
    position = view['camera_xyz_m']
    if not isinstance(position, list) or len(position) != 3:
        raise ValueError('camera_xyz_m: three finite numbers required')
    for coordinate in position:
        _number(coordinate, 'camera_xyz_m', -100, 100)
    if not isinstance(view['direction'], str) or view['direction'] not in ('+X', '-X', '+Y', '-Y'):
        raise ValueError('direction: explicit +X/-X/+Y/-Y look direction required')
    near = _number(view['near_m'], 'near_m', .002, 200)
    if near != .002:
        raise ValueError('near_m must equal 0.002 for pinned native camera persistence; move camera_xyz_m to choose the drawing cut plane')
    far = _number(view['far_m'], 'far_m', .002, 200)
    if near >= far:
        raise ValueError('elevation near_m must be strictly less than far_m')
    frame = elevation_frame(view)
    for depth in (0.0, far):
        for sx in (-1, 1):
            for sy in (-1, 1):
                for axis, origin in zip('xyz', position):
                    coordinate = (origin + getattr(frame.normal, axis) * depth
                                  + getattr(frame.right, axis) * sx * view['width_m'] / 2
                                  + getattr(frame.up, axis) * sy * view['height_m'] / 2)
                    _local_coordinate(coordinate, 'elevation complete camera frustum')

def canonical_index(model: BuildingModel) -> dict[str, dict]:
    return {row['id']: row for rows in model.to_dict().values() if isinstance(rows, list)
            for row in rows if isinstance(row, dict) and 'id' in row}


def _ids(value: Any, known: dict, name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f'{name}: canonical ID list required')
    if len(set(value)) != len(value) or any(v not in known for v in value):
        raise ValueError(f'{name}: unknown or duplicate canonical IDs')


def polygon_area_centroid(space):
    """Validate a simple, horizontal canonical polygon and derive its XY area/centroid."""
    pts = [(p.x, p.y, p.z) for p in space.footprint.points]
    if any(not all(math.isfinite(v) and abs(v) <= 1e6 for v in p) for p in pts):
        raise ValueError('space polygon coordinates must be finite and bounded')
    # Blender mesh coordinates are float32; refuse lossy source geometry rather
    # than presenting a distorted footprint as canonical.
    if any(abs(struct.unpack('f', struct.pack('f', v))[0] - v) > .00001 for p in pts for v in p[:2]):
        raise ValueError('space polygon exceeds native 0.01 mm coordinate precision')
    if any(abs(p[2] - pts[0][2]) > 1e-6 for p in pts):
        raise ValueError('space polygon must be horizontal')
    xy = [(p[0], p[1]) for p in pts]
    n = len(xy)
    if len(set(xy)) != n or n > 1000:
        raise ValueError('space polygon has duplicate or too many points')
    def orient(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    def intersects(a, b, c, d):
        def on(a, b, p):
            return min(a[0],b[0]) <= p[0] <= max(a[0],b[0]) and min(a[1],b[1]) <= p[1] <= max(a[1],b[1])
        o = (orient(a,b,c), orient(a,b,d), orient(c,d,a), orient(c,d,b))
        return ((o[0]*o[1] < 0 and o[2]*o[3] < 0) or
                (o[0] == 0 and on(a,b,c)) or (o[1] == 0 and on(a,b,d)) or
                (o[2] == 0 and on(c,d,a)) or (o[3] == 0 and on(c,d,b)))
    for i in range(n):
        for j in range(i+1, n):
            if j == i+1 or (i == 0 and j == n-1):
                continue
            if intersects(xy[i], xy[(i+1)%n], xy[j], xy[(j+1)%n]):
                raise ValueError('space polygon must be simple')
    # Work near the origin to avoid catastrophic cancellation for small
    # polygons located far from the canonical origin.
    origin = xy[0]
    local = [(x-origin[0], y-origin[1]) for x, y in xy]
    cross = [local[i][0]*local[(i+1)%n][1]-local[(i+1)%n][0]*local[i][1] for i in range(n)]
    twice = math.fsum(cross)
    if not math.isfinite(twice) or abs(twice) < 1e-9:
        raise ValueError('space polygon must have nonzero finite area')
    centroid = [origin[k] + math.fsum((local[i][k]+local[(i+1)%n][k])*cross[i] for i in range(n))/(3*twice) for k in (0,1)]
    return abs(twice)/2, centroid


def space_label_placement(space, annotation, view, devices, placed=()):
    """Choose a deterministic interior presentation anchor, or refuse collisions."""
    area, centroid = polygon_area_centroid(space)
    displayed = area if annotation['area_unit'] == 'm2' else area / .09290304
    if float(f'{displayed:.2f}') == 0:
        raise ValueError('space area would round to zero in chosen unit')
    text = f"{annotation['label']}: {displayed:.2f} {annotation['area_unit']}"
    scale = view['scale_denominator']
    half_width = (max(3 * len(text), 2.4 * len('SOURCE POLYGON; NET AREA UNVERIFIED')) / 2 + 2) * scale / 1000
    half_height = 8 * scale / 1000
    xy = [(p.x,p.y) for p in space.footprint.points]
    def inside(x,y):
        hit = False
        for i, (x1,y1) in enumerate(xy):
            x2,y2 = xy[(i+1)%len(xy)]
            cross = (x-x1)*(y2-y1)-(y-y1)*(x2-x1)
            if abs(cross) < 1e-9 and min(x1,x2) <= x <= max(x1,x2) and min(y1,y2) <= y <= max(y1,y2):
                return False
            if (y1 > y) != (y2 > y) and x < (x2-x1)*(y-y1)/(y2-y1)+x1:
                hit = not hit
        return hit
    minx,maxx = min(x for x,y in xy),max(x for x,y in xy)
    miny,maxy = min(y for x,y in xy),max(y for x,y in xy)
    candidates = [tuple(annotation['anchor_xy_m'])] if 'anchor_xy_m' in annotation else [tuple(centroid)] + sorted(
        [(minx+(maxx-minx)*i/20, miny+(maxy-miny)*j/20) for i in range(1,20) for j in range(1,20)],
        key=lambda p: ((p[0]-centroid[0])**2+(p[1]-centroid[1])**2,p))
    for x,y in candidates:
        if not inside(x,y):
            continue
        if any(abs(struct.unpack('f', struct.pack('f', v))[0] - v) > .00001 for v in (x,y)):
            continue
        if (abs(x-view['center_m'][0])+half_width > view['width_m']/2 or
                abs(y-view['center_m'][1])+half_height > view['height_m']/2):
            continue
        collision = False
        for point in view['annotations']:
            if point['source_id'] not in devices:
                continue  # normal annotation validation reports it
            p = devices[point['source_id']].pose.position
            # Conservative paper rectangle encloses glyph and offset label.
            dx,dy = point['label_offset_mm']
            radius_x = (abs(dx)+3*len(point['text'])+3)*scale/1000
            radius_y = (abs(dy)+6)*scale/1000
            if abs(x-p.x) < half_width+radius_x and abs(y-p.y) < half_height+radius_y:
                collision = True
                break
        if any(abs(x-px) < half_width+pw and abs(y-py) < half_height+ph for px,py,pw,ph in placed):
            collision = True
        if not collision:
            return area, centroid, [x,y], (x,y,half_width,half_height)
    raise ValueError('space label and net-area qualification must fit inside view and avoid point annotations; supply a safe interior anchor')


def validate_request(request: dict, model: BuildingModel) -> dict:
    """Validate a presentation request, without mutating it or the canonical model."""
    _object(request, {'version', 'acknowledged_body_omissions', 'views'}, 'request')
    if request['version'] != 'native-bonsai-v1':
        raise ValueError('unsupported native Bonsai request version')
    # This consumer exports only canonical geometry. An appearance reference is
    # metadata for a different lane, not permission to fetch/execute/rebuild it.
    def reject_external_appearance(value):
        if isinstance(value, dict):
            if 'visual_asset_reference' in value:
                raise ValueError('visual_asset_reference appearance is unsupported by canonical-only native export')
            for child in value.values():
                reject_external_appearance(child)
        elif isinstance(value, list):
            for child in value:
                reject_external_appearance(child)
    reject_external_appearance(model.to_dict())
    _local_model(model)
    known = canonical_index(model)
    _ids(request['acknowledged_body_omissions'], known, 'acknowledged_body_omissions')
    views = request['views']
    if not isinstance(views, list) or not 1 <= len(views) <= 8:
        raise ValueError('views: one to eight explicit views required')
    seen = set()
    polygon_work = 0
    placement_work = 0
    devices = {d.id: d for d in (*model.electrical_devices, *model.electrical_equipment)}
    for view in views:
        elevation = isinstance(view, dict) and view.get('target_view') == 'ELEVATION_VIEW'
        common = {'id', 'target_view', 'width_m', 'height_m', 'scale_denominator', 'exclude_ids', 'annotations'}
        required = common | ({'camera_xyz_m', 'direction', 'near_m', 'far_m'} if elevation else
                             {'level_id', 'center_m', 'cut_height_m'})
        allowed = (required,) if elevation else (required, required | {'space_annotations'})
        if not isinstance(view, dict) or set(view) not in allowed:
            raise ValueError('view: invalid keys')
        name = view['id']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', name) or name.casefold() in seen:
            raise ValueError('view id: unique case-insensitive safe filename required')
        seen.add(name.casefold())
        if view['target_view'] not in ('PLAN_VIEW', 'REFLECTED_PLAN_VIEW', 'ELEVATION_VIEW'):
            raise ValueError('unsupported target_view; only PLAN_VIEW/REFLECTED_PLAN_VIEW/ELEVATION_VIEW')
        for key in ('width_m', 'height_m'):
            _number(view[key], key, .1, 1000)
        if elevation:
            _elevation_volume(view)
            if view['annotations'] != []:
                raise ValueError('elevation annotations are unsupported; require an empty list')
        else:
            if not isinstance(view['level_id'], str) or view['level_id'] not in {level.id for level in model.levels}:
                raise ValueError('unknown level_id')
            _pair(view['center_m'], 'center_m', -1e6, 1e6)
            _number(view['cut_height_m'], 'cut_height_m', -1e4, 1e4)
            for axis, extent in zip(view['center_m'], (view['width_m'], view['height_m'])):
                _local_coordinate(abs(axis) + extent / 2, 'camera volume XY')
            _local_coordinate(abs(view['cut_height_m']) + 10.0, 'camera depth Z')
        scale = view['scale_denominator']
        if type(scale) is not int or not 1 <= scale <= 10000:
            raise ValueError('scale_denominator: integer 1..10000 required')
        _ids(view['exclude_ids'], known, 'exclude_ids')
        annotations = view['annotations']
        if not isinstance(annotations, list) or len(annotations) > 1000:
            raise ValueError('annotations: list of at most 1000 required')
        annotation_ids = set()
        for annotation in annotations:
            _object(annotation, {'source_id', 'kind', 'text', 'label_offset_mm'}, 'annotation')
            cid, kind, text = (annotation[k] for k in ('source_id', 'kind', 'text'))
            if not isinstance(cid, str) or cid not in devices or cid in annotation_ids:
                raise ValueError('annotation source_id: unique posed electrical device/equipment ID required')
            annotation_ids.add(cid)
            if cid in view['exclude_ids']:
                raise ValueError('annotation source cannot be excluded from its view')
            if kind not in ('text', 'luminaire', 'receptacle', 'switch'):
                raise ValueError('unsupported annotation kind')
            if kind != 'text' and (getattr(devices[cid], 'device_type', None) != kind or devices[cid].size is not None):
                raise ValueError('point glyph requires matching canonical device_type and unknown size')
            if not isinstance(text, str) or not 1 <= len(text) <= 80 or any(not 32 <= ord(c) <= 126 for c in text):
                raise ValueError('annotation text: 1..80 printable ASCII characters required')
            if any(c in text for c in '{}`'):
                raise ValueError('annotation text: template/formatter syntax is unsupported')
            _pair(annotation['label_offset_mm'], 'label_offset_mm', -30, 30)
        spaces = {s.id: s for s in model.spaces}
        space_annotations = view.get('space_annotations', [])
        if not isinstance(space_annotations, list) or len(space_annotations) > 1000:
            raise ValueError('space_annotations: list of at most 1000 required')
        space_ids = set()
        placed = []
        for annotation in space_annotations:
            keys = {'source_id', 'label', 'area_unit', 'shade'}
            if not isinstance(annotation, dict) or set(annotation) not in (keys, keys | {'anchor_xy_m'}):
                raise ValueError('space annotation: invalid keys')
            if 'anchor_xy_m' in annotation:
                _pair(annotation['anchor_xy_m'], 'anchor_xy_m', -1e6, 1e6)
            cid = annotation['source_id']
            if not isinstance(cid, str) or cid not in spaces or cid in space_ids:
                raise ValueError('space annotation: unique known space ID required')
            space_ids.add(cid)
            space = spaces[cid]
            if space.level_id != view['level_id'] or cid in view['exclude_ids']:
                raise ValueError('space annotation must belong to view level and not be excluded')
            label = annotation['label']
            if not isinstance(label, str) or not 1 <= len(label) <= 80 or any(not 32 <= ord(c) <= 126 or c in '{}`' for c in label):
                raise ValueError('space label: 1..80 safe printable ASCII characters required')
            if annotation['area_unit'] not in ('m2', 'ft2') or type(annotation['shade']) is not bool:
                raise ValueError('space annotation requires m2 or ft2 and boolean shade')
            polygon_work += len(space.footprint.points) ** 2
            if polygon_work > 1_000_000:
                raise ValueError('space polygon validation complexity budget exceeded')
            candidates = 1 if 'anchor_xy_m' in annotation else 362
            placement_work += candidates * (len(space.footprint.points) + len(annotations) + len(placed))
            if placement_work > 2_000_000:
                raise ValueError('space label placement complexity budget exceeded')
            _, _, _, rect = space_label_placement(space, annotation, view, devices, placed)
            placed.append(rect)
    return request


def _read(path: Path, limit: int) -> bytes:
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('input exceeds bounded file size')
    return data


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON number')))


def _runtime_paths(runtime: NativeRuntime) -> tuple[Path, Path]:
    blender, package = Path(runtime.blender).resolve(strict=True), Path(runtime.bonsai_python).resolve(strict=True)
    if not blender.is_file() or not os.access(blender, os.X_OK):
        raise ValueError('installed Blender executable required')
    if not package.is_dir() or not (package / 'bonsai' / 'blender_manifest.toml').is_file():
        raise ValueError('installed Bonsai Python directory with bonsai/blender_manifest.toml required')
    return blender, package


def _invoke(runtime: NativeRuntime, out: Path, mode: str, timeout_seconds: float) -> None:
    _number(timeout_seconds, 'timeout_seconds', 1, 7200)
    blender, package = _runtime_paths(runtime)
    private = out / '.runtime'
    private.mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH') if k in os.environ}
    for key, suffix in {'HOME': 'home', 'BLENDER_USER_CONFIG': 'config', 'XDG_CONFIG_HOME': 'config',
                        'XDG_CACHE_HOME': 'cache', 'XDG_DATA_HOME': 'data', 'XDG_RUNTIME_DIR': 'run'}.items():
        path = private / suffix
        path.mkdir(exist_ok=True)
        path.chmod(0o700)
        env[key] = str(path)
    env.update(OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
    cmd = [str(blender), '-b', '-t', '2', '--factory-startup', '--disable-autoexec', '--offline-mode', '-noaudio',
           '--python-exit-code', '1', '--python', str(Path(__file__).with_name('worker.py').resolve()),
           '--', mode, str(package), str(out)]
    with (out / f'{mode}.log').open('wb') as log:
        subprocess.run(cmd, cwd=out, env=env, stdout=log, stderr=subprocess.STDOUT,
                       timeout=timeout_seconds, check=True, shell=False)


def _failure(out: Path, exc: Exception) -> None:
    (out / 'failure.json').write_text(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                                                 'message': str(exc)}, indent=2) + '\n')
    # A stale/partial worker receipt cannot be mistaken for success.
    (out / 'receipt.json').unlink(missing_ok=True)


def probe_runtime(runtime: NativeRuntime, output_directory: Path, timeout_seconds: float = 60) -> Path:
    """Actually execute the explicitly supplied local runtime; failure is never a skip."""
    _runtime_paths(runtime)
    out = Path(output_directory).absolute()
    out.mkdir(parents=True, exist_ok=False)
    try:
        _invoke(runtime, out, 'probe', timeout_seconds)
        if not (out / 'runtime-probe.json').is_file():
            raise RuntimeError('runtime produced no probe receipt')
    except Exception as exc:
        _failure(out, exc)
        raise
    return out


def export_native(model_path: Path, request_path: Path, output_directory: Path,
                  runtime: NativeRuntime, timeout_seconds: float = 600) -> Path:
    """Read each source once; downstream uses only retained, hashed bytes."""
    model_bytes, request_bytes = _read(model_path, 32 * 1024**2), _read(request_path, 1024**2)
    model = BuildingModel.from_dict(_json(model_bytes))
    validate_request(_json(request_bytes), model)
    _runtime_paths(runtime)
    out = Path(output_directory).absolute()
    out.mkdir(parents=True, exist_ok=False)
    inputs = out / 'inputs'
    inputs.mkdir()
    for name, data in [('model.json', model_bytes), ('request.json', request_bytes)]:
        path = inputs / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o444)
    hashes = {name: hashlib.sha256((inputs / name).read_bytes()).hexdigest() for name in ('model.json', 'request.json')}
    (out / 'input-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
    try:
        _invoke(runtime, out, 'export', timeout_seconds)
        receipt = json.loads((out / 'receipt.json').read_text())
        if receipt.get('status') != 'review_only_success' or receipt.get('input_sha256') != hashes:
            raise RuntimeError('native worker receipt/source hash mismatch')
        for name, digest in hashes.items():
            if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('input snapshot changed during export')
    except Exception as exc:
        _failure(out, exc)
        raise
    return out


def validate_ifc_request(request: dict, model: BuildingModel, native) -> dict:
    """Bounded native additions remain IFC, never repurposed electrical entities."""
    import copy
    import ifcopenshell.util.placement
    from .extensions import prepare_render_source
    prepare_render_source(native, model)
    if not isinstance(request, dict) or request.get('version') != 'native-bonsai-ifc-v1':
        raise ValueError('unsupported extension-preserving request version')
    base = copy.deepcopy(request)
    base['version'] = 'native-bonsai-v1'
    acknowledged = base.pop('acknowledged_native_body_omissions', [])
    omissions = sorted(product.GlobalId for product in native.by_type('IfcProduct')
                       if product.is_a() in ('IfcPipeSegment', 'IfcPipeFitting', 'IfcSanitaryTerminal', 'IfcWasteTerminal')
                       and (not product.Representation or not any(r.RepresentationIdentifier == 'Body' for r in product.Representation.Representations)))
    if not isinstance(acknowledged, list) or any(not isinstance(guid, str) for guid in acknowledged) or sorted(acknowledged) != omissions:
        raise ValueError('acknowledged_native_body_omissions must exactly match native plumbing without Body geometry')
    if not isinstance(base.get('views'), list):
        raise ValueError('views must be a list')
    for view in base['views']:
        if not isinstance(view, dict):
            raise ValueError('view must be an object')
        annotations = view.pop('native_annotations', [])
        if not isinstance(annotations, list) or len(annotations) > 100:
            raise ValueError('native_annotations: at most 100 required')
        if annotations and view.get('target_view') != 'PLAN_VIEW':
            raise ValueError('native annotations currently require PLAN_VIEW')
        seen = set()
        for annotation in annotations:
            _object(annotation, {'source_guid', 'text', 'label_offset_mm'}, 'native annotation')
            guid = annotation['source_guid']
            if not isinstance(guid, str) or guid in seen:
                raise ValueError('unique native annotation source GUID required')
            seen.add(guid)
            try:
                product = native.by_guid(guid)
            except RuntimeError as exc:
                raise ValueError('unknown native annotation source GUID') from exc
            if product.is_a() not in ('IfcPipeSegment', 'IfcPipeFitting', 'IfcSanitaryTerminal', 'IfcWasteTerminal'):
                raise ValueError('native annotation source must be a supported plumbing product')
            if not product.ObjectPlacement:
                raise ValueError('native annotation source requires explicit placement')
            xyz = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)[:3, 3]
            for coordinate in xyz:
                _local_coordinate(float(coordinate), 'native annotation anchor')
            text = annotation['text']
            if not isinstance(text, str) or not 1 <= len(text) <= 80 or any(not 32 <= ord(c) <= 126 or c in '{}`' for c in text):
                raise ValueError('native annotation text: safe printable ASCII required')
            _pair(annotation['label_offset_mm'], 'label_offset_mm', -30, 30)
    validate_request(base, model)
    return request


def export_native_ifc(model_path: Path, source_ifc_path: Path, request_path: Path,
                      output_directory: Path, runtime: NativeRuntime,
                      timeout_seconds: float = 600) -> Path:
    """Preserve a bounded IFC graph while adding native review drawings.

    Does not convert plumbing to canonical JSON or overwrite the source IFC.
    """
    import ifcopenshell
    snapshots = {'model.json': _read(model_path, 32 * 1024**2),
                 'request.json': _read(request_path, 1024**2),
                 'source.ifc': _read(source_ifc_path, 32 * 1024**2)}
    model = BuildingModel.from_dict(_json(snapshots['model.json']))
    native = ifcopenshell.file.from_string(snapshots['source.ifc'].decode('utf-8'))
    validate_ifc_request(_json(snapshots['request.json']), model, native)
    _runtime_paths(runtime)
    out = Path(output_directory).absolute()
    out.mkdir(parents=True, exist_ok=False)
    inputs = out / 'inputs'
    inputs.mkdir()
    hashes = {}
    for name, data in snapshots.items():
        path = inputs / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o444)
        hashes[name] = hashlib.sha256(data).hexdigest()
    (out / 'input-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
    try:
        _invoke(runtime, out, 'export-ifc', timeout_seconds)
        receipt = json.loads((out / 'receipt.json').read_text())
        if receipt.get('status') != 'review_only_success' or receipt.get('input_sha256') != hashes:
            raise RuntimeError('native worker receipt/source hash mismatch')
        if receipt.get('native_source_preservation', {}).get('verified') is not True:
            raise RuntimeError('native source preservation proof missing')
        for name, digest in hashes.items():
            if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('input snapshot changed during export')
    except Exception as exc:
        _failure(out, exc)
        raise
    return out
