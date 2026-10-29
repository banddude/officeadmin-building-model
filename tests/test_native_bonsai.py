"""Public-safe native Bonsai request guards; real runtime tests are explicit opt-in.

A skipped runtime test is not evidence that Blender or Bonsai was executed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.model import BuildingModel
from oabm.native_bonsai import NativeRuntime, export_native, validate_request

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "examples/synthetic-native-bonsai-model.json"
REQUEST = ROOT / "examples/synthetic-native-bonsai-request.json"


@pytest.fixture
def model():
    return BuildingModel.load(MODEL)


@pytest.fixture
def request_data():
    return json.loads(REQUEST.read_text())


def test_valid_synthetic_request_is_repeatable_without_mutation(model, request_data):
    before = copy.deepcopy(request_data)
    model_before = model.to_dict()
    first = validate_request(request_data, model)
    assert first == validate_request(request_data, model)
    assert request_data == before
    assert model.to_dict() == model_before
    assert {v["target_view"] for v in first["views"]} == {"PLAN_VIEW", "REFLECTED_PLAN_VIEW"}


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(version="native-bonsai-v999"),
    lambda r: r.update(script="raise RuntimeError('untrusted')"),
    lambda r: r.update(ifc="https://example.invalid/model.ifc"),
    lambda r: r.update(views=[]),
    lambda r: r["views"].append(copy.deepcopy(r["views"][0])),
    lambda r: r["views"][0].update(id="../escape"),
    lambda r: r["views"][0].update(id="/absolute"),
    lambda r: r["views"][0].update(id=".."),
    lambda r: r["views"][0].update(target_view="SECTION_VIEW"),
    lambda r: r["views"][0].update(level_id="level:missing"),
    lambda r: r["views"][0].update(level_id=[]),
    lambda r: r["views"][0].update(level_id=None),
    lambda r: r["views"][0].update(center_m=[0]),
    lambda r: r["views"][0].update(center_m=[False, 0]),
    lambda r: r["views"][0].update(center_m=[float("nan"), 0]),
    lambda r: r["views"][0].update(cut_height_m=float("inf")),
    lambda r: r["views"][0].update(cut_height_m=True),
    lambda r: r["views"][0].update(width_m=0),
    lambda r: r["views"][0].update(height_m=-1),
    lambda r: r["views"][0].update(scale_denominator=True),
    lambda r: r["views"][0].update(scale_denominator=1.5),
    lambda r: r["views"][0].update(scale_denominator=0),
    lambda r: r["views"][0].update(symbols="file:///etc/passwd"),
    lambda r: r["views"][0].update(exclude_ids=["device:R1"]),
    lambda r: r["views"][0]["annotations"].append(copy.deepcopy(r["views"][0]["annotations"][0])),
    lambda r: r["views"][0].update(exclude_ids=["device:missing"]),
    lambda r: r.update(acknowledged_body_omissions=["device:missing"]),
    lambda r: r.update(acknowledged_body_omissions=["device:L1", "device:L1"]),
    lambda r: r["views"][0]["annotations"][0].update(source_id="device:missing"),
    lambda r: r["views"][0]["annotations"][0].update(source_id="space:room"),
    lambda r: r["views"][0]["annotations"][0].update(kind="luminaire"),
    lambda r: r["views"][0]["annotations"][0].update(kind="arbitrary-svg"),
    lambda r: r["views"][0]["annotations"][0].update(x_m=2),
    lambda r: r["views"][0]["annotations"][0].update(label_offset_mm=[0, float("nan")]),
    lambda r: r["views"][0]["annotations"][0].update(text="line\nfeed"),
    lambda r: r["views"][0]["annotations"][0].update(text="{{unsafe_expression}}"),
    lambda r: r["views"][0]["annotations"][0].update(text="x" * 100_000),
])
def test_invalid_or_executable_presentation_request_refused(model, request_data, mutation):
    mutation(request_data)
    with pytest.raises(ValueError):
        validate_request(request_data, model)


def test_known_sized_device_can_have_text_but_not_unknown_size_symbol(model, request_data):
    assert validate_request(request_data, model)
    data = model.to_dict()
    lamp = next(d for d in data["electrical_devices"] if d["id"] == "device:L1")
    lamp["size"] = {"x": .1, "y": .1, "z": .1}
    sized = BuildingModel.from_dict(data)
    with pytest.raises(ValueError):
        validate_request(request_data, sized)
    request_data["views"][1]["annotations"][0]["kind"] = "text"
    assert validate_request(request_data, sized)


def test_unsupported_unicode_label_is_refused_not_rewritten(model, request_data):
    request_data["views"][0]["annotations"][0]["text"] = "R1 – draft"
    with pytest.raises(ValueError, match="ASCII"):
        validate_request(request_data, model)
    assert request_data["views"][0]["annotations"][0]["text"] == "R1 – draft"


def _configured_runtime():
    blender = os.environ.get("OABM_TEST_BLENDER")
    bonsai = os.environ.get("OABM_TEST_BONSAI_PYTHON")
    if not blender and not bonsai:
        pytest.skip("Actual native Bonsai integration not run: explicit installed runtime paths absent")
    if not blender or not bonsai:
        pytest.fail("Both OABM_TEST_BLENDER and OABM_TEST_BONSAI_PYTHON must be supplied")
    return NativeRuntime(blender=Path(blender), bonsai_python=Path(bonsai))


def test_real_native_export_has_model_derived_ifc_and_two_scaled_views(tmp_path):
    runtime = _configured_runtime()
    source = tmp_path / "model.json"
    request = tmp_path / "request.json"
    source.write_bytes(MODEL.read_bytes())
    request.write_bytes(REQUEST.read_bytes())
    original = source.read_bytes()
    requested = request.read_bytes()
    output = tmp_path / "native"
    result = export_native(source, request, output, runtime, timeout_seconds=600)
    assert Path(result) == output
    assert (output / "inputs/model.json").read_bytes() == original
    assert (output / "inputs/request.json").read_bytes() == requested
    assert not (output / "failure.json").exists()
    receipt = json.loads((output / "receipt.json").read_text())
    serialized = json.dumps(receipt)
    assert hashlib.sha256(original).hexdigest() in serialized
    assert hashlib.sha256(requested).hexdigest() in serialized
    svgs = sorted((output / "drawings").glob("*.svg"))
    assert len(svgs) == 2
    all_text = ""
    for path in svgs:
        tree = ET.parse(path).getroot()
        assert tree.attrib["data-scale"] == "1:48"
        # 6 m x 5 m at native 1:48, allowing native pixel-aspect quantization.
        assert float(tree.attrib["width"].removesuffix("mm")) == pytest.approx(125, abs=.2)
        assert float(tree.attrib["height"].removesuffix("mm")) == pytest.approx(5000/48, abs=.2)
        all_text += " ".join(t.text or "" for t in tree.iter())
    assert all(label in all_text for label in ["L1", "R1", "S1", "F1"])
    assert receipt["status"] == "review_only_success"
    assert receipt["canonical_roundtrip"]["numerical_differences"] == []
    assert receipt["units"]["ifc_length_scale_to_m"] == 1
    assert {row["canonical_id"] for row in receipt["body_omissions"]} == {"device:L1", "device:R1", "device:S1"}
    assert json.loads((output / "runtime-probe.json").read_text())["probe_executed"] is True
    import ifcopenshell
    from ifcopenshell.util.element import get_pset
    from oabm.ifc import canonical_id_to_ifc_guid, from_ifc, round_trip_differences
    native = ifcopenshell.open(str(output / "model.ifc"))
    original_model = BuildingModel.from_json(original)
    assert round_trip_differences(original_model, from_ifc(native)) == ()
    for cid in ["device:L1", "device:R1", "device:S1"]:
        product = native.by_guid(canonical_id_to_ifc_guid(cid))
        assert get_pset(product, "OABM_Adapter", "Body") == "no"
    fan = native.by_guid(canonical_id_to_ifc_guid("device:F1"))
    assert get_pset(fan, "OABM_Adapter", "Body") == "yes"
    assert any(rep.RepresentationIdentifier == "Body" for rep in fan.Representation.Representations)
    for drawing in native.by_type("IfcAnnotation"):
        if drawing.ObjectType != "DRAWING":
            continue
        props = get_pset(drawing, "EPset_Drawing")
        for field in ("Stylesheet", "Markers", "Patterns", "Symbols", "ShadingStyles"):
            rel = Path(props[field])
            assert not rel.is_absolute() and ".." not in rel.parts
            assert (output / rel).is_file()
    for view in receipt["views"].values():
        assert view["scale"] == "1/48"
        for annotation in view["annotations"]:
            product = native.by_guid(annotation["product_guid"])
            assert product.GlobalId == canonical_id_to_ifc_guid(annotation["source_id"])
            ann = native.by_guid(annotation["annotation_guid"])
            links = [rel for rel in ann.HasAssignments if rel.is_a("IfcRelAssignsToProduct")]
            assert len(links) == 1 and links[0].RelatingProduct == product
    for rel, digest in receipt["files"].items():
        assert not Path(rel).is_absolute() and ".." not in Path(rel).parts
        assert hashlib.sha256((output / rel).read_bytes()).hexdigest() == digest
    # No source mutation or overwriting a completed result on repeated invocation.
    assert source.read_bytes() == original and request.read_bytes() == requested
    with pytest.raises(FileExistsError):
        export_native(source, request, output, runtime, timeout_seconds=600)


@pytest.fixture
def mocked_runtime(monkeypatch, tmp_path):
    """Launcher unit tests only: this fixture never executes Blender or claims it did."""
    from oabm.native_bonsai import api
    runtime = NativeRuntime(tmp_path / "trusted-executable", tmp_path / "trusted-packages")
    monkeypatch.setattr(api, "_runtime_paths", lambda _: (runtime.blender, runtime.bonsai_python))
    return api, runtime


def test_snapshot_read_once_survives_original_mutation(tmp_path, monkeypatch, mocked_runtime):
    api, runtime = mocked_runtime
    model = tmp_path / "source.json"
    request = tmp_path / "request.json"
    model.write_bytes(MODEL.read_bytes())
    request.write_bytes(REQUEST.read_bytes())
    expected_model, expected_request = model.read_bytes(), request.read_bytes()
    reads = []
    actual_read = api._read
    def read_once(path, limit):
        reads.append(Path(path))
        return actual_read(path, limit)
    monkeypatch.setattr(api, "_read", read_once)
    def fake_worker(_runtime, out, mode, timeout):
        assert mode == "export"
        model.write_text("source changed after capture")
        request.write_text("request changed after capture")
        assert (out / "inputs/model.json").read_bytes() == expected_model
        assert (out / "inputs/request.json").read_bytes() == expected_request
        BuildingModel.from_json((out / "inputs/model.json").read_bytes())
        hashes = json.loads((out / "input-hashes.json").read_text())
        (out / "receipt.json").write_text(json.dumps({"status": "review_only_success", "input_sha256": hashes}))
    monkeypatch.setattr(api, "_invoke", fake_worker)
    out = api.export_native(model, request, tmp_path / "output", runtime)
    assert reads == [model, request]
    assert (out / "inputs/model.json").read_bytes() == expected_model
    assert (out / "inputs/model.json").stat().st_mode & 0o222 == 0
    assert (out / "inputs/request.json").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("failure", ["timeout", "missing_receipt", "wrong_hash", "snapshot_tamper"])
def test_worker_failure_never_leaves_success_receipt(tmp_path, monkeypatch, mocked_runtime, failure):
    import subprocess
    api, runtime = mocked_runtime
    def fake_worker(_runtime, out, mode, timeout):
        hashes = json.loads((out / "input-hashes.json").read_text())
        if failure == "missing_receipt":
            return
        if failure == "wrong_hash":
            hashes["model.json"] = "0" * 64
        (out / "receipt.json").write_text(json.dumps({"status": "review_only_success", "input_sha256": hashes}))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fixed-test-worker", 1)
        if failure == "snapshot_tamper":
            path = out / "inputs/model.json"
            path.chmod(0o644)
            path.write_text("changed captured bytes")
    monkeypatch.setattr(api, "_invoke", fake_worker)
    output = tmp_path / "failed"
    with pytest.raises((RuntimeError, FileNotFoundError, subprocess.TimeoutExpired)):
        api.export_native(MODEL, REQUEST, output, runtime)
    assert not (output / "receipt.json").exists()
    assert json.loads((output / "failure.json").read_text())["status"] == "failed"


def test_duplicate_json_keys_are_rejected_before_runtime(tmp_path, mocked_runtime):
    api, runtime = mocked_runtime
    request = tmp_path / "duplicate.json"
    request.write_text(REQUEST.read_text().replace('"version": "native-bonsai-v1",',
        '"version": "native-bonsai-v1", "version": "native-bonsai-v1",'))
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="duplicate JSON key"):
        api.export_native(MODEL, request, output, runtime)
    assert not output.exists()


def test_missing_runtime_is_explicit_failure_not_skip_or_install(tmp_path):
    runtime = NativeRuntime(tmp_path / "absent-blender", tmp_path / "absent-bonsai")
    output = tmp_path / "not-created"
    with pytest.raises(FileNotFoundError):
        export_native(MODEL, REQUEST, output, runtime)
    assert not output.exists()


@pytest.mark.parametrize("location", ["model", "device", "nested"])
def test_external_appearance_reference_is_refused_without_fetch(model, request_data, location):
    data = model.to_dict()
    marker = {"visual_asset_reference": {"uri": "synthetic-appearance.glb", "sha256": "0" * 64}}
    if location == "model":
        data["attributes"].update(marker)
    elif location == "device":
        data["electrical_devices"][0]["attributes"].update(marker)
    else:
        data["attributes"]["descriptive_metadata"] = [marker]
    supplied = BuildingModel.from_dict(data)
    before = supplied.to_dict()
    with pytest.raises(ValueError, match="visual_asset_reference.*unsupported"):
        validate_request(request_data, supplied)
    assert supplied.to_dict() == before


def test_real_runtime_package_cannot_shadow_reviewed_engine(tmp_path):
    runtime = _configured_runtime()
    packages = tmp_path / 'runtime-packages'
    packages.mkdir()
    for source in runtime.bonsai_python.iterdir():
        if source.name != 'oabm':
            (packages / source.name).symlink_to(source.resolve(), target_is_directory=source.is_dir())
    shadow = packages / 'oabm'
    shadow.mkdir()
    (shadow / '__init__.py').write_text('raise RuntimeError("runtime package shadowed reviewed engine")\n')
    output = export_native(MODEL, REQUEST, tmp_path / 'native-collision-proof',
                           NativeRuntime(runtime.blender, packages), timeout_seconds=600)
    receipt = json.loads((output / 'receipt.json').read_text())
    assert receipt['status'] == 'review_only_success'
    assert receipt['canonical_roundtrip']['numerical_differences'] == []


def _translated_synthetic(delta):
    data = json.loads(MODEL.read_text())
    request = json.loads(REQUEST.read_text())
    for collection in ('walls', 'ceilings', 'slabs', 'spaces'):
        for item in data[collection]:
            for key in ('footprint', 'centerline'):
                if key in item:
                    for point in item[key]['points']:
                        point['x'] += delta
                        point['y'] += delta
    for item in data['electrical_devices']:
        item['pose']['position']['x'] += delta
        item['pose']['position']['y'] += delta
    for view in request['views']:
        view['center_m'] = [x + delta for x in view['center_m']]
    return data, request


def test_large_coordinate_model_and_camera_refused_before_runtime(tmp_path, monkeypatch):
    import oabm.native_bonsai.api as api
    data, request = _translated_synthetic(999000.013)
    source, spec = tmp_path / 'model.json', tmp_path / 'request.json'
    source.write_text(json.dumps(data))
    spec.write_text(json.dumps(request))
    monkeypatch.setattr(api, '_invoke', lambda *a: pytest.fail('unsafe model reached runtime'))
    with pytest.raises(ValueError, match='local-coordinate scope'):
        export_native(source, spec, tmp_path / 'out', NativeRuntime(Path('/unused'), Path('/unused')))
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('mutation', [
    lambda d, r: d['electrical_devices'][0]['pose']['position'].update(x=1001),
    lambda d, r: r['views'][0].update(center_m=[99, 0]),
    lambda d, r: r['views'][0].update(cut_height_m=95),
    lambda d, r: d['coordinate_system'].update(crs='EPSG:32610'),
    lambda d, r: d['coordinate_system'].update(origin_in_crs={'x': 1, 'y': 2, 'z': 3}),
])
def test_local_scope_checks_off_view_geometry_camera_and_crs(mutation):
    data, request = _translated_synthetic(0)
    mutation(data, request)
    with pytest.raises(ValueError, match='local-coordinate scope'):
        validate_request(request, BuildingModel.from_dict(data))


def test_real_runtime_local_boundary_and_derived_body_extent(tmp_path):
    import subprocess
    runtime = _configured_runtime()
    data, request = _translated_synthetic(95)
    source, spec = tmp_path / 'model.json', tmp_path / 'request.json'
    source.write_text(json.dumps(data)); spec.write_text(json.dumps(request))
    out = export_native(source, spec, tmp_path / 'boundary', runtime)
    receipt = json.loads((out / 'receipt.json').read_text())
    assert receipt['local_coordinate_scope']['native_false_origin'] is False
    for view in receipt['views'].values():
        for annotation in view['annotations']:
            assert max(abs(a-b) for a,b in zip(annotation['canonical_xy_m'], annotation['native_annotation_xy_m'])) <= 1e-5
    assert 'IfcWall' in (out / 'drawings/plan.svg').read_text()
    # Local wall axis, distant extrusion endpoint: scalar/anchor checks alone
    # would miss this Body, even when its ID is excluded from every view.
    data, request = _translated_synthetic(0)
    data['walls'][0]['height_m'] = 1001
    for view in request['views']:
        view['exclude_ids'].append(data['walls'][0]['id'])
    source.write_text(json.dumps(data)); spec.write_text(json.dumps(request))
    with pytest.raises(subprocess.CalledProcessError):
        export_native(source, spec, tmp_path / 'distant-body', runtime)
    failed = tmp_path / 'distant-body'
    assert 'native world Body extent' in (failed / 'export.log').read_text()
    assert not (failed / 'model.ifc').exists()
    assert not (failed / 'receipt.json').exists()
    assert (failed / 'failure.json').exists()
