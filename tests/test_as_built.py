"""Public synthetic field installation, canonical refusal and atomic delivery.

Source checkpoint only until the coordinated execution opening; do not confuse
the presence of these tests with a successful run.
"""
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.model import ContractError
from oabm.pipeline.as_built import (apply_transaction, replay_history, snapshot)
from oabm.pipeline.as_built_delivery import create_as_built_delivery, installed_materials

ROOT = Path(__file__).resolve().parents[1]


def inputs():
    spec = importlib.util.spec_from_file_location("as_built_example", ROOT / "examples/as_built_quick_edit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.synthetic_inputs()


def test_four_edits_replay_preserves_design_and_unrelated_inventory():
    history, request = inputs()
    original = deepcopy(history)
    revised = apply_transaction(history, request)
    current = replay_history(json.loads(json.dumps(revised)))
    assert history == original
    assert revised["design_json"] == original["design_json"]
    assert replay_history(revised).to_json() == current.to_json()
    baseline = replay_history(history)
    assert current.electrical_boxes[0] == baseline.electrical_boxes[0]
    assert current.routes[0].id == baseline.routes[0].id
    assert current.routes[0].route_type == "cable"
    assert current.electrical_devices[0].id == baseline.electrical_devices[0].id
    assert current.ports[1].pose.position == current.routes[0].centerline.points[-1]
    assert len(current.electrical_boxes) == 2
    assert not current.conductors and not current.route_fittings
    materials = installed_materials(revised)
    cable = [row for row in materials["installed_items"] if row["category"] == "route_length"]
    assert len(cable) == 1
    assert cable[0]["item_type"] == "cable" and cable[0]["unit"] == "m"
    assert sum(row["quantity"] for row in materials["installed_items"] if row["category"] == "electrical_box") == 2


@pytest.mark.parametrize("failure", ["stale_parent", "stale_entity", "missing_evidence", "missing_port",
    "method_omission", "missing_product", "invalid_reference", "baseline", "duplicate_transaction"])
def test_invalid_transactions_do_not_mutate_input(failure):
    history, request = inputs()
    original = deepcopy(history)
    if failure == "stale_parent":
        request["parent_sha256"] = "0" * 64
    elif failure == "stale_entity":
        request["edits"][0]["expected_sha256"] = "0" * 64
    elif failure == "missing_evidence":
        request["evidence"] = []
    elif failure == "missing_port":
        request["edits"] = [edit for edit in request["edits"] if edit["collection"] != "ports"]
    elif failure == "method_omission":
        request["method_reconciliations"] = []
    elif failure == "missing_product":
        request["edits"] = [edit for edit in request["edits"] if edit["collection"] != "raceway_selections"]
    elif failure == "invalid_reference":
        next(edit for edit in request["edits"] if edit["collection"] == "routes")["record"]["end_port_id"] = "port:absent"
    elif failure == "baseline":
        request["design_sha256"] = "0" * 64
    else:
        history = apply_transaction(history, request)
        original = deepcopy(history)
    with pytest.raises(ContractError):
        apply_transaction(history, request)
    assert history == original


def test_design_facts_are_not_installed_without_confirmation():
    history, request = inputs()
    request["installed_assertions"] = []
    materials = installed_materials(apply_transaction(history, request))
    assert not materials["installed_items"]
    assert materials["unresolved_items"]


def test_delivery_contains_real_readable_sheets_bound_to_one_snapshot(tmp_path):
    history, request = inputs()
    hp, tp = tmp_path / "history.json", tmp_path / "transaction.json"
    hp.write_text(json.dumps(history))
    tp.write_text(json.dumps(request))
    manifest = create_as_built_delivery(hp, tp, output_directory=tmp_path / "result")
    root = tmp_path / "result/delivery"
    assert manifest["status"] == "complete"
    assert (root / "canonical-model.json").read_text() == snapshot(replay_history(apply_transaction(history, request)))
    for name, entry in manifest["files"].items():
        assert entry["revised_model_sha256"] == manifest["revised_model_sha256"]
        assert (root / name).stat().st_size == entry["bytes"]
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == entry["sha256"]
    plans = list((root / "drawings").glob("plan-*.svg"))
    panels = list((root / "drawings").glob("panel-*.svg"))
    assert plans and panels
    for path in plans + panels:
        svg = ET.fromstring(path.read_text())
        assert svg.findall(".//{http://www.w3.org/2000/svg}text")
    assert "EVSE" in plans[0].read_text()
    assert "12" in panels[0].read_text()
    differences = json.loads((root / "design-differences.json").read_text())
    assert differences["changed_count"] and differences["quantity_deltas"]
    repeated = create_as_built_delivery(hp, tp, output_directory=tmp_path / "repeated")
    assert repeated == manifest
    with pytest.raises((ContractError, FileExistsError)):
        create_as_built_delivery(hp, tp, output_directory=tmp_path / "result")


def test_export_failure_never_publishes_complete_output(tmp_path, monkeypatch):
    from oabm.pipeline import as_built_delivery
    history, request = inputs()
    hp, tp = tmp_path / "history.json", tmp_path / "transaction.json"
    hp.write_text(json.dumps(history))
    tp.write_text(json.dumps(request))
    def fail(*args):
        raise ValueError("synthetic exporter failure")
    monkeypatch.setattr(as_built_delivery, "_drawings", fail)
    with pytest.raises(ValueError, match="exporter failure"):
        create_as_built_delivery(hp, tp, output_directory=tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_csv_spreadsheet_formula_text_is_inert():
    import csv
    import io
    from oabm.pipeline.as_built_delivery import _csv
    report = {"installed_items": [{"category": "device", "item_type": " =1+1",
        "quantity": 1, "unit": "ea", "variant": [], "source_entity_ids": ["device:safe"],
        "reason_codes": []}], "unresolved_items": []}
    rows = list(csv.reader(io.StringIO(_csv(report))))
    assert rows[1][2] == "' =1+1"


def test_html_and_svg_escape_user_revision_text(tmp_path):
    history, request = inputs()
    request["transaction_id"] = "field:<x>"
    request["installed_assertions"] = []  # changed transaction ID changes provenance hashes
    hp, tp = tmp_path / "history.json", tmp_path / "transaction.json"
    hp.write_text(json.dumps(history))
    tp.write_text(json.dumps(request))
    create_as_built_delivery(hp, tp, output_directory=tmp_path / "result")
    root = tmp_path / "result/delivery"
    assert "field:&lt;x&gt;" in (root / "index.html").read_text()
    assert "field:<x>" not in (root / "drawings/plan-1.svg").read_text()
