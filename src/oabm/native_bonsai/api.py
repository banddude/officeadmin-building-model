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
from typing import Any

from oabm.model import BuildingModel


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


def canonical_index(model: BuildingModel) -> dict[str, dict]:
    return {row['id']: row for rows in model.to_dict().values() if isinstance(rows, list)
            for row in rows if isinstance(row, dict) and 'id' in row}


def _ids(value: Any, known: dict, name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f'{name}: canonical ID list required')
    if len(set(value)) != len(value) or any(v not in known for v in value):
        raise ValueError(f'{name}: unknown or duplicate canonical IDs')


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
    known = canonical_index(model)
    _ids(request['acknowledged_body_omissions'], known, 'acknowledged_body_omissions')
    views = request['views']
    if not isinstance(views, list) or not 1 <= len(views) <= 8:
        raise ValueError('views: one to eight explicit views required')
    seen = set()
    devices = {d.id: d for d in (*model.electrical_devices, *model.electrical_equipment)}
    for view in views:
        _object(view, {'id', 'target_view', 'level_id', 'center_m', 'cut_height_m', 'width_m', 'height_m',
                       'scale_denominator', 'exclude_ids', 'annotations'}, 'view')
        name = view['id']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', name) or name.casefold() in seen:
            raise ValueError('view id: unique case-insensitive safe filename required')
        seen.add(name.casefold())
        if view['target_view'] not in ('PLAN_VIEW', 'REFLECTED_PLAN_VIEW'):
            raise ValueError('unsupported target_view; only PLAN_VIEW/REFLECTED_PLAN_VIEW')
        if not isinstance(view['level_id'], str) or view['level_id'] not in {level.id for level in model.levels}:
            raise ValueError('unknown level_id')
        _pair(view['center_m'], 'center_m', -1e6, 1e6)
        _number(view['cut_height_m'], 'cut_height_m', -1e4, 1e4)
        for key in ('width_m', 'height_m'):
            _number(view[key], key, .1, 1000)
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
    cmd = [str(blender), '-b', '--factory-startup', '--disable-autoexec', '--offline-mode', '-noaudio',
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
