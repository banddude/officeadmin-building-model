"""#88 evidence spike: the render exists and the panel discloses the ruling."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "display_thickness_evidence",
    ROOT / "spikes/display_thickness_evidence/render_evidence.py",
)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


def test_evidence_render_exists_and_panel_discloses_the_ruling(tmp_path: Path) -> None:
    report = evidence.render_evidence(tmp_path / "evidence.png")

    # The drawn export thickened exactly the three scan-recorded walls, and
    # the drawn value is the caller's labeled parameter, never a constant
    # the exporter chose.
    assert report["drawn"]["thickened_walls"] == 3
    assert "thickened_walls" not in report["faithful"]
    assert report["drawn"]["walls"] == report["faithful"]["walls"] == 4

    # The element panel text comes from the actual exported GLB extras and
    # carries the recorded value, the drawn value, and where the drawn value
    # came from, side by side.
    panel = report["panel_lines"]
    assert panel[0] == "wall:scan-south"
    joined = "\n".join(panel)
    assert "canonical_thickness_m  0.001" in joined
    assert "drawn_thickness_m  0.1" in joined
    assert "derivation  inferred" in joined
    assert (
        "thickness_source  min_display_wall_thickness_m (drawing aid, not a measurement)"
        in joined
    )

    image = Path(str(report["image"]))
    assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert image.stat().st_size > 0


def test_evidence_render_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    evidence.render_evidence(first)
    evidence.render_evidence(second)
    assert first.read_bytes() == second.read_bytes()


def test_canonical_model_keeps_the_scan_thickness() -> None:
    model = evidence.synthetic_scan_room()
    scan = next(wall for wall in model.walls if wall.id == "wall:scan-south")
    assert scan.thickness_m == evidence.SCAN_THICKNESS_M


def test_evidence_model_validates_against_the_canonical_schema() -> None:
    # The synthetic fixture is canonical data: a Polyline3D slab footprint
    # once passed the exporter silently while the v1 schema requires
    # polygon3d. Full schema validation keeps the fixture honest.
    schema = json.loads(
        (ROOT / "contracts" / "oabm-model-v1.schema.json").read_text(encoding="utf-8"),
    )
    Draft202012Validator.check_schema(schema)
    document = evidence.synthetic_scan_room().to_dict()
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)
    slab = document["slabs"][0]
    assert slab["footprint"]["kind"] == "polygon3d"
