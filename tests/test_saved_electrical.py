"""Synthetic saved inputs, actual consumers, and local persistence failure recovery."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from oabm.model import BuildingModel, ContractError
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.pipeline import saved_electrical as saved
from test_reviewed_electrical_pipeline import inputs


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("saved-source")
    model, review, args = inputs(root)
    # Capture new synthetic author review against the loaded canonical snapshot.
    # An in-memory integer elevation serializes back as float in the canonical loader.
    model = BuildingModel.from_dict(model.to_dict())
    prior = args["assertion_review"]
    args["assertion_review"] = PlacementAssertionReview.capture(model, review,
        decisions=args["decisions"], registrations=args["registrations"],
        canonical_datum_id=prior.canonical_datum_id, reviewer_id=prior.reviewer_id,
        reviewer_derivation=prior.reviewer_derivation, rationale=prior.rationale,
        assertions=prior.assertions)
    records = {}
    def put(name, data):
        if not isinstance(data, bytes):
            data = json.dumps(data, sort_keys=True).encode()
        path = root / name
        path.write_bytes(data)
        return {"path": name, "sha256": sha(data)}
    records.update(version=saved.VERSION, delivery_id="synthetic-local-delivery",
        model=put("base.json", model.to_dict()), review=put("review.json", review.to_dict()))
    for key in ("runs", "decisions", "registrations"):
        records[key] = put(key + ".json", [x.to_dict() for x in args[key]])
    for key in ("assertion_review", "scope_policy", "review_config"):
        records[key] = put(key + ".json", args[key].to_dict())
    for key in ("documents", "images", "evidence_documents"):
        records[key] = {identity: put(key + "-" + str(i), data)
                        for i, (identity, data) in enumerate(args[key].items())}
    records["journals"] = {identity: put("journal-" + str(i), Path(path).read_bytes())
                           for i, (identity, path) in enumerate(args["journal_paths"].items())}
    records["source_references"] = [{"source_kind": k, "source_id": s, "reference": v}
                                    for (k, s), v in sorted(args["source_references"].items())]
    for key in ("proposal_groups", "canonical_groups", "glb_options", "include_ifc", "coverage_report"):
        records[key] = args[key]
    manifest = root / "inputs.json"
    manifest.write_text(json.dumps(records))
    return manifest


def snapshot(root):
    return {str(p.relative_to(root)): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def verify(final):
    marker = json.loads((final / "COMPLETED.json").read_bytes())
    assert marker["state"] == "persisted"
    for row in marker["files"]:
        data = (final / row["path"]).read_bytes()
        assert len(data) == row["bytes"] and sha(data) == row["sha256"]
    report = json.loads((final / "completion-report.json").read_bytes())
    assert report["delivery_ready"] and not any(report["claims"].values())
    assert report["source_coverage"]["status"] == "unavailable"
    for kind, record in report["artifacts"].items():
        assert Path(record["reference"]) == final / saved.FILES[kind]
        assert sha(Path(record["reference"]).read_bytes()) == record["sha256"]
    assert report["counts"]["canonical_devices"] == 1
    return report


def test_real_saved_inputs_publish_complete_set_and_preserve_all_prior_bytes(source, tmp_path):
    before = snapshot(source.parent)
    first = saved.persist_saved_electrical(source, output_root=tmp_path, run_id="v001")
    verify(first)
    frozen = snapshot(first.parent)
    second = saved.persist_saved_electrical(source, output_root=tmp_path, run_id="v002")
    verify(second)
    assert snapshot(source.parent) == before and snapshot(first.parent) == frozen
    assert not (first.parent / ".pending").exists()
    assert (first / "model.json").read_bytes() == (second / "model.json").read_bytes()
    # References intentionally differ by version; compare canonical payload, not reference-bearing report.
    assert json.loads((first / "quantities.json").read_bytes()) == json.loads((second / "quantities.json").read_bytes())
    with pytest.raises(FileExistsError):
        saved.persist_saved_electrical(source, output_root=tmp_path, run_id="v001")
    assert snapshot(first.parent) == frozen


def test_cli_runs_actual_pipeline_without_network(source, tmp_path):
    result = subprocess.run([sys.executable, "-m", "oabm.pipeline.saved_electrical", str(source),
        "--output-root", str(tmp_path), "--run-id", "cli-v1"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / "cli-v1" / "delivery")
    verify(Path(result.stdout.strip()))


def altered(source, tmp_path, change):
    data = json.loads(source.read_bytes())
    # Keep relative references meaningful in a separate manifest directory.
    def absolute(value):
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                value["path"] = str(source.parent / value["path"])
            else:
                for item in value.values(): absolute(item)
        elif isinstance(value, list):
            for item in value: absolute(item)
    absolute(data)
    change(data)
    path = tmp_path / "altered.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("change", [
    lambda d: d.update(coverage_report={"complete": True}),
    lambda d: d.update(unknown=True),
    lambda d: d["model"].update(sha256="0" * 64),
    lambda d: d["model"].update(path="https://example.invalid/model.json"),
])
def test_invalid_explicit_inputs_leave_failed_attempt_only(source, tmp_path, change):
    manifest = altered(source, tmp_path, change)
    with pytest.raises(saved.SavedDeliveryError) as error:
        saved.persist_saved_electrical(manifest, output_root=tmp_path / "out", run_id="bad")
    assert error.value.stage == "load_inputs"
    attempt = error.value.attempt_directory
    assert json.loads((attempt / "failure.json").read_bytes())["state"] == "failed"
    assert not (attempt / "delivery").exists()


def test_late_write_failure_is_not_published_and_fresh_retry_recovers(source, tmp_path, monkeypatch):
    original = saved._write
    def fail(path, data):
        if path.name == "scope-ledger.json":
            raise OSError("synthetic full storage")
        original(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(saved, "_write", fail)
        with pytest.raises(saved.SavedDeliveryError) as error:
            saved.persist_saved_electrical(source, output_root=tmp_path, run_id="failed")
    assert error.value.stage == "persist_delivery"
    attempt = tmp_path / "failed"
    assert (attempt / ".pending" / "model.json").exists()
    assert not (attempt / "delivery").exists()
    assert not list(attempt.rglob("COMPLETED.json"))
    frozen = snapshot(attempt)
    final = saved.persist_saved_electrical(source, output_root=tmp_path, run_id="recovery")
    verify(final)
    assert snapshot(attempt) == frozen


def test_stale_review_preserves_runner_failure_report(source, tmp_path):
    data = json.loads((source.parent / "base.json").read_bytes())
    data["name"] = "Changed synthetic snapshot"
    changed = tmp_path / "changed-model.json"
    changed.write_text(json.dumps(data))
    manifest = altered(source, tmp_path, lambda d: d.update(model={"path": str(changed), "sha256": sha(changed.read_bytes())}))
    with pytest.raises(saved.SavedDeliveryError):
        saved.persist_saved_electrical(manifest, output_root=tmp_path / "out", run_id="stale")
    failure = json.loads((tmp_path / "out/stale/failure.json").read_bytes())
    assert failure["runner_completion"]["stages"]["placement_intake"]["status"] == "failed"
    assert not failure["runner_completion"]["delivery_ready"]
    assert not (tmp_path / "out/stale/delivery").exists()


def test_exclusive_reservation_blocks_concurrent_same_version(source, tmp_path, monkeypatch):
    def stop(*args, **kwargs):
        raise ContractError("synthetic compute stop")
    monkeypatch.setattr(saved, "deliver_reviewed_electrical", stop)
    def call():
        try:
            saved.persist_saved_electrical(source, output_root=tmp_path, run_id="same")
        except Exception as error:
            return type(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: call(), range(2)))
    assert set(outcomes) == {FileExistsError, saved.SavedDeliveryError}
    assert not (tmp_path / "same/delivery").exists()


@pytest.mark.parametrize("run_id", ["..", "a/b", "", ".pending", "a b"])
def test_unsafe_run_ids_create_nothing(source, tmp_path, run_id):
    root = tmp_path / "not-created"
    with pytest.raises(ContractError):
        saved.persist_saved_electrical(source, output_root=root, run_id=run_id)
    assert not root.exists()


def test_input_symlink_refused(source, tmp_path):
    linked = tmp_path / "link.json"
    linked.symlink_to(source.parent / "base.json")
    manifest = altered(source, tmp_path, lambda d: d["model"].update(path=str(linked)))
    with pytest.raises(saved.SavedDeliveryError):
        saved.persist_saved_electrical(manifest, output_root=tmp_path / "out", run_id="link")
    assert not (tmp_path / "out/link/delivery").exists()


def test_failed_publish_keeps_marker_uncommitted_and_prior_runs_safe(source, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("synthetic rename failure")
    with monkeypatch.context() as patch:
        patch.setattr(saved.os, "rename", fail)
        with pytest.raises(saved.SavedDeliveryError) as error:
            saved.persist_saved_electrical(source, output_root=tmp_path, run_id="uncommitted")
    assert error.value.stage == "publish_directory"
    assert (tmp_path / "uncommitted/.pending/COMPLETED.json").exists()
    assert not (tmp_path / "uncommitted/delivery").exists()
    failure = json.loads((tmp_path / "uncommitted/failure.json").read_bytes())
    assert not failure["directory_committed"]


def test_cli_reports_failed_input_without_success_path(source, tmp_path):
    manifest = altered(source, tmp_path, lambda d: d.update(coverage_report={}))
    result = subprocess.run([sys.executable, "-m", "oabm.pipeline.saved_electrical", str(manifest),
        "--output-root", str(tmp_path / "out"), "--run-id", "bad-cli"], capture_output=True, text=True)
    assert result.returncode == 1 and not result.stdout
    assert "failed at load_inputs" in result.stderr
    assert not (tmp_path / "out/bad-cli/delivery").exists()


def test_duplicate_json_keys_fail_closed(source, tmp_path):
    manifest = tmp_path / "duplicate.json"
    manifest.write_text('{"version":"first","version":"second"}')
    with pytest.raises(saved.SavedDeliveryError):
        saved.persist_saved_electrical(manifest, output_root=tmp_path / "out", run_id="duplicate")
    assert not (tmp_path / "out/duplicate/delivery").exists()


def test_optional_ifc_is_persisted_and_bound(source, tmp_path):
    manifest = altered(source, tmp_path, lambda d: d.update(include_ifc=True))
    final = saved.persist_saved_electrical(manifest, output_root=tmp_path / "out", run_id="with-ifc")
    verify(final)
    from oabm.ifc.adapter import from_ifc
    restored = from_ifc(final / "model.ifc")
    assert len(restored.electrical_devices) == 1
    assert restored.electrical_devices[0].pose.position.z == pytest.approx(4.2)


def test_postrename_sync_failure_is_reported_as_unknown_durability(source, tmp_path, monkeypatch):
    original = saved._sync_dir
    def fail(path):
        if path == tmp_path / "sync-failed" and (path / "delivery").exists():
            raise OSError("synthetic directory sync failure")
        original(path)
    with monkeypatch.context() as patch:
        patch.setattr(saved, "_sync_dir", fail)
        with pytest.raises(saved.SavedDeliveryError):
            saved.persist_saved_electrical(source, output_root=tmp_path, run_id="sync-failed")
    attempt = tmp_path / "sync-failed"
    failure = json.loads((attempt / "failure.json").read_bytes())
    assert failure["state"] == "durability_unknown" and failure["directory_committed"]
    verify(attempt / "delivery")
    before = snapshot(attempt)
    with pytest.raises(FileExistsError):
        saved.persist_saved_electrical(source, output_root=tmp_path, run_id="sync-failed")
    assert snapshot(attempt) == before


def test_interrupted_attempt_remains_reserved_and_new_version_recovers(source, tmp_path, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(saved, "deliver_reviewed_electrical", interrupt)
        with pytest.raises(KeyboardInterrupt):
            saved.persist_saved_electrical(source, output_root=tmp_path, run_id="interrupted")
    attempt = tmp_path / "interrupted"
    before = snapshot(attempt)
    assert (attempt / "attempt.json").exists() and not (attempt / "delivery").exists()
    with pytest.raises(FileExistsError):
        saved.persist_saved_electrical(source, output_root=tmp_path, run_id="interrupted")
    verify(saved.persist_saved_electrical(source, output_root=tmp_path, run_id="after-interrupt"))
    assert snapshot(attempt) == before
