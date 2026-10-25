"""#275 evidence spike: the render exists, honours the file's own visibility, and discloses."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "layer_selections_evidence",
    ROOT / "spikes/layer_selections_evidence/render_evidence.py",
)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


def test_evidence_render_exists_and_reports_the_selections(tmp_path: Path) -> None:
    report = evidence.render_evidence(tmp_path / "evidence.png")
    resolutions = report["resolutions"]

    # All visible: the whole two-level model, nothing hidden or dimmed.
    all_ids = resolutions["preset:all"]["resolution"]
    assert len(all_ids["visible_ids"]) == 12
    assert all_ids["hidden_ids"] == [] and all_ids["ghosted_ids"] == []
    assert resolutions["preset:all"]["summary"]["dimmed"] == 0

    # Phase 1 only: lighting hidden and all six architectural members receive
    # an actual ghost material in the GLB.
    phase = resolutions["preset:phase-1"]["resolution"]
    assert phase["hidden_ids"] == ["device:one-em"]
    assert len(phase["ghosted_ids"]) == 6
    assert resolutions["preset:phase-1"]["summary"]["dimmed"] == 6
    assert resolutions["preset:phase-1"]["unsupported_ghost_ids"] == []

    # Power ghosted: the ghosted route carries its three wires with it.
    ghost = resolutions["preset:ghost-power"]
    assert ghost["resolution"]["ghosted_ids"] == [
        "device:two-recp", "equip:panel", "route:two",
    ]
    assert ghost["summary"]["dimmed"] == 3
    assert ghost["summary"]["dimmed_conductor_wires"] == 3

    # Isolate: only the power members stay visible.
    isolate = resolutions["isolate:power"]["resolution"]
    assert len(isolate["visible_ids"]) == 5
    assert len(isolate["hidden_ids"]) == 7
    assert isolate["ghosted_ids"] == []

    image = Path(str(report["image"]))
    assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert image.stat().st_size > 0


def test_material_readback_proves_the_route_ghosts_whole(tmp_path: Path) -> None:
    report = evidence.render_evidence(tmp_path / "evidence.png")
    lines = report["material_lines"]
    assert "route:two  material=route-dimmed  alpha=0.3" in lines
    assert "conductor:two-l1#route:two#0  material=wire-phase-1-dimmed  alpha=0.3" in lines
    assert "conductor:two-neutral#route:two#0  material=wire-neutral-dimmed  alpha=0.3" in lines
    assert "conductor:two-egc#route:two#0  material=wire-ground-dimmed  alpha=0.3" in lines


def test_evidence_render_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    evidence.render_evidence(first)
    evidence.render_evidence(second)
    assert first.read_bytes() == second.read_bytes()


def test_evidence_model_validates_against_the_canonical_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "oabm-model-v1.schema.json").read_text(encoding="utf-8"),
    )
    Draft202012Validator.check_schema(schema)
    document = evidence.synthetic_two_level_model().to_dict()
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_evidence_config_validates_against_the_layers_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "oabm-layers-v1.schema.json").read_text(encoding="utf-8"),
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(evidence.synthetic_config().to_dict())
