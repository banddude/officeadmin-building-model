"""Offline, opt-in #93 calculation boundary; never writes a canonical model.

Only a single-phase, single-route, single-load circuit is supported. This is
a comparison with two pinned upstream pure functions, not conductor sizing,
an engineering rule registry, or a jurisdiction/compliance decision.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess

from oabm.model import BuildingModel

UPSTREAM_REVISION = "9cb234266ae06927dcdb1b2de3b0a8a65d8d9f9d"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/model/v1/garage-route.json"


def circuit_inputs(
    model: BuildingModel,
    circuit_id: str,
    *,
    power_factor: float,
    resistance_ohm_per_m: float,
) -> dict:
    """Read resolved canonical references and explicitly supplied assumptions."""
    if not math.isfinite(power_factor) or not 0 < power_factor <= 1:
        raise ValueError("power_factor must be finite and in (0, 1]")
    if not math.isfinite(resistance_ohm_per_m) or resistance_ohm_per_m < 0:
        raise ValueError("resistance_ohm_per_m must be finite and nonnegative")
    circuit = next((c for c in model.circuits if c.id == circuit_id), None)
    if circuit is None:
        raise ValueError("circuit_id is not in the canonical model")
    if circuit.phase != "1ph" or len(circuit.route_ids) != 1 or len(circuit.load_port_ids) != 1:
        raise ValueError("spike requires one single-phase circuit route and one load")
    if circuit.load_va is None or circuit.voltage_v is None:
        raise ValueError("spike requires explicit apparent load and voltage")
    route = next(r for r in model.routes if r.id == circuit.route_ids[0])
    if {route.start_port_id, route.end_port_id} != {circuit.source_port_id, circuit.load_port_ids[0]}:
        raise ValueError("circuit route must join its source port to its load port")
    points = route.centerline.points
    length = math.fsum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(points, points[1:])
    )
    # Canonical load is VA, whereas the upstream function expects real watts.
    # P = S * pf; dividing P by V * pf must recover S / V, not S / (V * pf).
    return {
        "circuit_id": circuit.id,
        "route_id": route.id,
        "apparent_load_va": circuit.load_va,
        "real_power_w": circuit.load_va * power_factor,
        "voltage_v": circuit.voltage_v,
        "one_way_length_m": length,
        "power_factor": power_factor,
        "resistance_ohm_per_m": resistance_ohm_per_m,
    }


def compare(inputs: dict, *, node: str | None = None) -> dict:
    """Execute pinned upstream code and compare with independently stated SI math."""
    executable = node or os.environ.get("OABM_SPIKE_NODE", "node")
    version = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    version_parts = tuple(int(p) for p in version.removeprefix("v").split("."))
    if version_parts < (23, 6, 0):
        raise ValueError("Graph Studio spike requires Node >=23.6; use Node 24 LTS")
    result = subprocess.run(
        [executable, str(Path(__file__).with_name("bridge.mjs"))],
        input=json.dumps(inputs, allow_nan=False), check=True,
        capture_output=True, text=True, timeout=10,
    )
    actual = json.loads(result.stdout)
    current = inputs["apparent_load_va"] / inputs["voltage_v"]
    drop = 2 * current * inputs["one_way_length_m"] * inputs["resistance_ohm_per_m"]
    expected = {
        "current_a": current,
        "voltage_drop_v": drop,
        "voltage_drop_percent": 100 * drop / inputs["voltage_v"],
    }
    if set(actual) != set(expected) or any(
        not math.isfinite(actual[key]) or not math.isclose(actual[key], value, rel_tol=1e-12, abs_tol=1e-12)
        for key, value in expected.items()
    ):
        raise ValueError("upstream result differs from the independent SI calculation")
    return {
        "classification": "derived_prototype_not_code_compliance",
        "upstream_revision": UPSTREAM_REVISION,
        "inputs": inputs,
        "assumptions": {
            "phase": "single", "resistance_basis": "explicit hypothetical constant, not a wire-size table",
            "power_factor_basis": "explicit hypothetical constant",
            "excluded": ["reactance", "temperature", "ampacity", "derating", "jurisdiction rules"],
        },
        "formulas": ["P_W = S_VA * pf", "I_A = S_VA / V_V", "Vdrop_V = 2 * I_A * L_m * r_ohm_per_m"],
        "upstream_result": actual,
        "independent_expected": expected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--power-factor", type=float, required=True)
    parser.add_argument("--resistance-ohm-per-m", type=float, required=True)
    args = parser.parse_args()
    model = BuildingModel.from_json(FIXTURE.read_text(encoding="utf-8"))
    inputs = circuit_inputs(model, "circuit:evse", power_factor=args.power_factor,
                            resistance_ohm_per_m=args.resistance_ohm_per_m)
    print(json.dumps(compare(inputs), sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
