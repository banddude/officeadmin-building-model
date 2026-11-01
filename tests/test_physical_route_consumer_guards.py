"""Physical geometry must never be silently omitted by legacy consumers."""
from dataclasses import replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path

import pytest

from oabm.model import BuildingModel, ContractError, Point3, Polyline3D, Route
from physical_route_fixture import make_physical_model


CONSUMERS = {
    'drawings.generator': 'generate_drawing_set generate_plan generate_elevation generate_section',
    'drawings.details': 'generate_detail generate_details company_detail_bodies',
    'exports.gltf': 'to_glb build_glb_bytes',
    'exports.layers': 'layer_glb_plan',
    'exports.review_bundle': 'build_review_bundle',
    'ifc.adapter': 'to_ifc round_trip',
    'reports.support_coverage': 'report_support_coverage',
    'qa.route_bends': 'check_route_bends',
    'qa.confidence_review': 'build_confidence_review',
    'routing.router': 'route_between_ports',
    'routing.checked_assembly': 'route_checked_assembly',
    'routing.consolidate': 'consolidate_bundled_routes',
    'routing.overlap': 'find_overlapping_route_runs',
    'routing.design': 'design_proposed_circuits propose_architectural_electrical_design',
    'routing.placement': 'set_user_equipment_placement apply_equipment_proposal',
    'electrical.fittings': 'select_route_fittings',
    'electrical.raceways': 'select_raceways',
    'electrical.supports': 'select_supports',
    'pipeline.review_drawings': 'generate_review_drawings',
    'pipeline.reviewed_electrical': 'deliver_reviewed_electrical',
}
CASES = [(module, name) for module, names in CONSUMERS.items() for name in names.split()]


@pytest.fixture(params=['physical_only', 'physical_first', 'physical_last'])
def physical_model(request):
    model = make_physical_model()
    if request.param == 'physical_only':
        return model
    physical = model.routes[0]
    ports = {p.id: p for p in model.ports}
    legacy = Route(id='aaa-legacy-route', route_type=physical.route_type,
                   start_port_id=physical.start_port_id, end_port_id=physical.end_port_id,
                   centerline=Polyline3D(points=(ports[physical.start_port_id].pose.position,
                                                ports[physical.end_port_id].pose.position)))
    routes = (physical, legacy) if request.param == 'physical_first' else (legacy, physical)
    return replace(model, routes=routes)


def _call_with_required_arguments(function, model, **overrides):
    """Omitted operation-specific values must not mask the geometry refusal."""
    params = list(inspect.signature(function).parameters.values())[1:]
    args, kwargs = [model], {}
    for param in params:
        if param.name in overrides:
            kwargs[param.name] = overrides[param.name]
        elif param.default is inspect.Parameter.empty:
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
                args.append(None)
            elif param.kind == param.KEYWORD_ONLY:
                kwargs[param.name] = None
    return function(*args, **kwargs)


@pytest.mark.parametrize('module,name', CASES, ids=[f'{m}.{n}' for m, n in CASES])
def test_all_public_consumers_refuse_before_other_work(physical_model, module, name):
    function = getattr(importlib.import_module('oabm.' + module), name)
    before = physical_model.to_json()
    with pytest.raises(ContractError, match=r'does not support physical routes \(line_arc_v1\)'):
        _call_with_required_arguments(function, physical_model)
    assert physical_model.to_json() == before


@pytest.mark.parametrize('module,name', [('qa.revision_diff', 'build_revision_diff')])
@pytest.mark.parametrize('physical_side', ['before', 'after'])
def test_comparisons_refuse_either_snapshot(physical_model, module, name, physical_side):
    function = getattr(importlib.import_module('oabm.' + module), name)
    legacy = replace(physical_model, routes=())
    pair = (physical_model, legacy) if physical_side == 'before' else (legacy, physical_model)
    frozen = tuple(m.to_json() for m in pair)
    with pytest.raises(ContractError, match='does not support physical routes'):
        function(*pair)
    assert tuple(m.to_json() for m in pair) == frozen


def test_filters_and_empty_requests_do_not_hide_physical_routes(physical_model):
    from oabm.routing.overlap import find_overlapping_route_runs
    from oabm.drawings.generator import generate_drawing_set
    from oabm.electrical.raceways import select_raceways
    def forbidden(*args, **kwargs):
        pytest.fail('consumer invoked callback before rejecting physical geometry')
    for action in (
        lambda: find_overlapping_route_runs(physical_model, excluded_route_ids=frozenset(r.id for r in physical_model.routes)),
        lambda: generate_drawing_set(physical_model, plans=(), include_schedules=False, symbol_provider=forbidden, label_provider=forbidden),
        lambda: select_raceways(physical_model, intents=(), catalog=(), rules=()),
    ):
        with pytest.raises(ContractError, match='does not support physical routes'):
            action()


@pytest.mark.parametrize('kind', ['ifc', 'glb'])
@pytest.mark.parametrize('existing', [False, True])
def test_file_writers_preserve_sentinels_and_create_nothing(physical_model, tmp_path, kind, existing):
    from oabm.exports.gltf import to_glb
    from oabm.ifc.adapter import to_ifc
    output = tmp_path / 'absent-parent' / ('model.' + kind)
    if existing:
        output.parent.mkdir()
        output.write_bytes(b'untouched sentinel')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ContractError, match='does not support physical routes'):
        if kind == 'ifc':
            to_ifc(physical_model, output, groups={})
        else:
            to_glb(physical_model, output, groups={'hidden': [r.id for r in physical_model.routes]}, hidden_groups=('hidden',))
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert output.parent.exists() == existing


def _write_saved_manifest(tmp_path, document):
    from oabm.pipeline import saved_electrical as saved
    data = json.dumps(document).encode()
    (tmp_path / 'model.json').write_bytes(data)
    manifest = {key: None for key in saved.REQUIRED}
    manifest.update(version=saved.VERSION, model={'path': 'model.json', 'sha256': hashlib.sha256(data).hexdigest()})
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    return path


@pytest.mark.parametrize('malformed', [False, True])
def test_saved_delivery_refuses_before_any_reservation(physical_model, tmp_path, monkeypatch, malformed):
    from oabm.pipeline import saved_electrical as saved
    document = physical_model.to_dict()
    if malformed:
        document['routes'][-1] = {'geometry_kind': 'line_arc_v1'}
    manifest = _write_saved_manifest(tmp_path, document)
    def forbidden(*args, **kwargs):
        pytest.fail('unsupported physical input caused a write or reservation')
    monkeypatch.setattr(saved, '_write', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    with pytest.raises(ContractError, match='does not support physical routes'):
        saved.persist_saved_electrical(manifest, output_root=tmp_path / 'absent', run_id='run')
    assert not (tmp_path / 'absent').exists()


def test_saved_loader_refuses_before_journal_write(physical_model, tmp_path):
    from oabm.pipeline import saved_electrical as saved
    manifest = _write_saved_manifest(tmp_path, physical_model.to_dict())
    with pytest.raises(ContractError, match='does not support physical routes'):
        saved.load_saved_electrical_inputs(manifest, journal_directory=tmp_path / 'journals')
    assert not (tmp_path / 'journals').exists()


def test_model_delivery_skip_bends_still_preflights(physical_model, tmp_path):
    from oabm.exports.model_delivery import deliver_model
    model_path = tmp_path / 'physical.json'
    model_path.write_text(physical_model.to_json())
    source = Path(__file__).parents[1] / 'fixtures/model_delivery/v1/options.json'
    options = json.loads(source.read_bytes())
    options['bend_policy'] = 'skip'
    options['radii_m'] = {}
    options_path = tmp_path / 'options.json'
    options_path.write_text(json.dumps(options))
    output = tmp_path / 'absent-parent' / 'attempt'
    with pytest.raises(ContractError, match='does not support physical routes'):
        deliver_model(model_path, options_path, output_directory=output)
    assert not output.parent.exists()


def test_single_route_overlap_api_refuses_physical(physical_model):
    from oabm.model import PhysicalRoute
    from oabm.routing.overlap import member_coverage_intervals
    physical = next(r for r in physical_model.routes if isinstance(r, PhysicalRoute))
    with pytest.raises(ContractError, match='does not support physical routes'):
        member_coverage_intervals(physical, start=Point3(x=0, y=0, z=0), end=Point3(x=1, y=0, z=0))


# Reuse the existing wholly synthetic saved-delivery inputs, not private plans.
from test_saved_electrical import source as saved_source


def test_saved_delivery_consumes_the_preflight_snapshot(saved_source, tmp_path, monkeypatch):
    import shutil
    from oabm.pipeline import saved_electrical as saved
    source_directory = tmp_path / 'source'
    shutil.copytree(saved_source.parent, source_directory)
    manifest_path = source_directory / saved_source.name
    original_manifest_bytes = manifest_path.read_bytes()
    original_manifest = json.loads(original_manifest_bytes)
    model_path = source_directory / original_manifest['model']['path']
    original_model_bytes = model_path.read_bytes()
    original_loader = saved.load_saved_electrical_inputs
    received = []
    def change_paths_then_load(path, *, journal_directory, _snapshot=None):
        assert _snapshot == (original_manifest_bytes, original_model_bytes)
        replacement = make_physical_model().to_json().encode()
        model_path.write_bytes(replacement)
        changed = dict(original_manifest)
        changed['model'] = {**changed['model'], 'sha256': hashlib.sha256(replacement).hexdigest()}
        manifest_path.write_text(json.dumps(changed))
        result = original_loader(path, journal_directory=journal_directory, _snapshot=_snapshot)
        received.append(result)
        return result
    monkeypatch.setattr(saved, 'load_saved_electrical_inputs', change_paths_then_load)
    output = saved.persist_saved_electrical(manifest_path, output_root=tmp_path / 'runs', run_id='snapshot')
    assert (output / 'COMPLETED.json').is_file()
    assert len(received) == 1
    assert received[0].manifest_bytes == original_manifest_bytes
    assert received[0].input_payloads['model'] == original_model_bytes
    assert received[0].model.to_json() == BuildingModel.from_json(original_model_bytes).to_json()


def test_legacy_geometry_outputs_still_work(tmp_path):
    from oabm.exports.gltf import to_glb, build_glb_bytes
    from oabm.drawings.generator import generate_drawing_set
    from oabm.quantities.core import extract_quantities
    fixture = Path(__file__).parents[1] / 'fixtures/model_delivery/v1/model.json'
    model = BuildingModel.from_json(fixture.read_bytes())
    before = model.to_json()
    first = build_glb_bytes(model)
    output = tmp_path / 'legacy.glb'
    to_glb(model, output)
    assert output.read_bytes() == first
    assert generate_drawing_set(model).to_json() == generate_drawing_set(model).to_json()
    assert extract_quantities(model).to_json() == extract_quantities(model).to_json()
    assert model.to_json() == before


@pytest.mark.parametrize('kind', ['checked_route', 'detail_sheets'])
def test_outer_writers_refuse_without_failure_artifacts(physical_model, tmp_path, kind):
    model_path = tmp_path / 'physical.json'
    model_path.write_text(physical_model.to_json())
    request_path = tmp_path / 'request.json'
    request_path.write_text('{}')
    output = tmp_path / 'absent-parent' / 'attempt'
    with pytest.raises(ContractError, match='does not support physical routes'):
        if kind == 'checked_route':
            from oabm.routing.checked_delivery import deliver_checked_assembly_route
            deliver_checked_assembly_route(model_path, request_path, output_directory=output)
        else:
            from oabm.drawings.details import create_detail_sheet_set
            create_detail_sheet_set(model_path, request_path, tmp_path / 'unused-profile',
                                    tmp_path / 'unused-job', output_directory=output)
    assert not output.parent.exists()


def test_production_sheet_outer_boundary_precedes_maps_and_writes(physical_model, tmp_path):
    import shutil
    from oabm.drawings.production_set import create_production_sheet_set
    fixture = Path(__file__).parents[1] / 'fixtures/production_set/v1'
    inputs = tmp_path / 'inputs'
    shutil.copytree(fixture, inputs)
    request = json.loads((inputs / 'production.json').read_bytes())
    (inputs / request['model']).write_text(physical_model.to_json())
    def forbidden(*args, **kwargs):
        pytest.fail('map callback ran for unsupported geometry')
    output = tmp_path / 'absent-parent' / 'attempt'
    with pytest.raises(ContractError, match='does not support physical routes'):
        create_production_sheet_set(inputs / 'production.json', output_directory=output, map_client=forbidden)
    assert not output.parent.exists()


def test_ifc_import_refuses_physical_metadata_before_native_route_interpretation(monkeypatch):
    from oabm.ifc import adapter
    fixture = Path(__file__).parents[1] / 'fixtures/model_delivery/v1/model.json'
    legacy = BuildingModel.from_json(fixture.read_bytes())
    legacy = replace(legacy, name="Synthetic physical metadata refusal",
                     obstacles=tuple(replace(o, name=o.name or o.id) for o in legacy.obstacles))
    native = adapter.to_ifc(legacy)
    before = native.to_string()
    original = adapter._canonical_metadata
    def physical_metadata(item):
        metadata = original(item)
        if metadata is not None and metadata['kind'] == 'route':
            return {**metadata, 'json': {**metadata['json'], 'geometry_kind': 'line_arc_v1'}}
        return metadata
    def forbidden(*args, **kwargs):
        pytest.fail('native route geometry was interpreted before physical metadata refusal')
    monkeypatch.setattr(adapter, '_canonical_metadata', physical_metadata)
    monkeypatch.setattr(adapter, '_route_segments', forbidden)
    with pytest.raises(adapter.IfcAdapterError, match='physical route metadata refused'):
        adapter.from_ifc(native)
    assert native.to_string() == before


def test_existing_fitting_replay_schema_refusal_is_preserved(physical_model, tmp_path):
    from oabm.pipeline.saved_route_fitting_replay import replay_saved_route_fitting
    from test_saved_route_fitting_replay import _saved_tree, _write, _dump
    path, manifest = _saved_tree(tmp_path)
    manifest['inputs']['base_model'] = _write(tmp_path, 'inputs/base_model.bin', physical_model.to_json().encode())
    path.write_bytes(_dump(manifest))
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ContractError, match='requires canonical schema 1.5.0'):
        replay_saved_route_fitting(path)
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('tamper', ['model_bytes', 'reference_shape', 'reference_hash'])
def test_cached_loader_still_validates_source_binding(saved_source, tmp_path, tamper):
    from oabm.pipeline import saved_electrical as saved
    manifest = json.loads(saved_source.read_bytes())
    model_bytes = (saved_source.parent / manifest['model']['path']).read_bytes()
    if tamper == 'model_bytes':
        model_bytes += b' '
    elif tamper == 'reference_shape':
        manifest['model']['unexpected'] = True
    else:
        manifest['model']['sha256'] = '0' * 64
    snapshot = (json.dumps(manifest).encode(), model_bytes)
    with pytest.raises(ContractError, match='saved input hash mismatch|input reference requires'):
        saved.load_saved_electrical_inputs(saved_source, journal_directory=tmp_path / 'journals', _snapshot=snapshot)
    assert not (tmp_path / 'journals').exists()


def test_generic_canonical_json_and_fingerprints_remain_supported(physical_model):
    from oabm.exports.review_bundle import canonical_model_sha256
    from oabm.reports.support_coverage import support_model_fingerprint
    before = physical_model.to_json()
    restored = BuildingModel.from_json(before)
    # Legacy numeric fields may normalize int spellings to floats on first load.
    assert restored == physical_model
    assert BuildingModel.from_json(restored.to_json()).to_json() == restored.to_json()
    for model in (physical_model, restored):
        assert canonical_model_sha256(model) == hashlib.sha256(model.to_json(indent=None).encode()).hexdigest()
        assert support_model_fingerprint(model) == hashlib.sha256(model.to_json().encode()).hexdigest()
    assert physical_model.to_json() == before


def test_version_alone_is_not_a_physical_geometry_ban():
    from oabm.exports.gltf import build_glb_bytes
    from oabm.drawings.generator import generate_drawing_set
    from oabm.quantities.core import extract_quantities
    model = replace(make_physical_model(), routes=())
    assert model.schema_version == '1.7.0'
    assert build_glb_bytes(model)
    assert generate_drawing_set(model).to_json()
    assert extract_quantities(model).to_json()


def test_source_snapshots_preserve_physical_rows_and_reject_forged_bindings(physical_model):
    from oabm.model import PhysicalRoute
    from oabm.reports.panel_schedules import SourceSnapshot
    physical = next(r for r in physical_model.routes if isinstance(r, PhysicalRoute))
    row = next(r for r in physical_model.to_dict()['routes'] if r.get('geometry_kind') == 'line_arc_v1')
    captured = SourceSnapshot.capture('routes', physical)
    assert captured.entity['geometry_kind'] == 'line_arc_v1'
    assert captured == SourceSnapshot.from_source_data('routes', row)
    with pytest.raises(ContractError, match='content binding'):
        SourceSnapshot(collection='routes', entity=row, content_fingerprint='0' * 64)



def test_pure_source_identity_coverage_keeps_physical_routes(physical_model):
    from oabm.reports.source_coverage import model_fingerprint, inventory_fingerprint, source_coverage
    ids = [r.id for r in physical_model.routes]
    source_sha = 'a' * 64
    mapping = {'inventory_sha256': inventory_fingerprint(source_sha, ids),
               'model_sha256': model_fingerprint(physical_model),
               'entries': [{'source_id': identity, 'canonical_ids': [identity]} for identity in ids]}
    result = source_coverage(physical_model, source_sha256=source_sha, object_ids=ids, mapping=mapping, strict=True)
    assert result


def test_proposal_snapshot_refuses_before_candidate_callback(physical_model, monkeypatch):
    from oabm.proposals import contracts
    options = contracts.PlacementOptions(identity_key='synthetic', equipment_type='panelboard',
        name=None, level_id='level:unused', served_device_ids=(), mounting_height_m=1.0,
        working_depth_m=1.0, working_width_m=1.0)
    def forbidden(*args, **kwargs):
        pytest.fail('proposal candidate generation ran with unsupported physical snapshots')
    monkeypatch.setattr(contracts, '_candidates', forbidden)
    with pytest.raises(ContractError, match='does not support physical routes'):
        contracts.PlacementContext.capture(physical_model, options)


@pytest.mark.parametrize('module,name', [
    ('quantities.core', '_canonical_entity_collections'),
    ('qa.revision_diff', '_entity_collections'),
    ('exports.review_bundle', '_entity_collections'),
    ('qa.confidence_review', '_entity_collections'),
])
def test_reflected_collections_keep_legacy_routes_after_union_change(module, name):
    fixture = Path(__file__).parents[1] / 'fixtures/model_delivery/v1/model.json'
    model = BuildingModel.from_json(fixture.read_bytes())
    collections = getattr(importlib.import_module('oabm.' + module), name)(model)
    assert 'routes' in collections
    routes = collections['routes']
    actual = set(routes) if isinstance(routes, dict) else {r.id for r in routes}
    assert actual == {r.id for r in model.routes}
    assert actual


def test_confidence_identity_collection_keeps_physical_routes(physical_model):
    from oabm.qa.confidence_review import _entity_collections, build_confidence_review, ReviewConfig
    before = physical_model.to_json()
    assert _entity_collections(physical_model)['routes'] == physical_model.routes
    with pytest.raises(ContractError, match="build_confidence_review does not support physical routes"):
        build_confidence_review(physical_model, ReviewConfig(confidence_threshold=0.9))
    assert physical_model.to_json() == before
