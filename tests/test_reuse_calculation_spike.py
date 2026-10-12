"""#93 public-safe known-answer and fail-closed calculation boundary tests."""

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reuse_spike", ROOT / "spikes/graph_studio/spike.py")
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)


def model():
    return BuildingModel.load(spike.FIXTURE)


@pytest.mark.parametrize("power_factor", [1.0, 0.8])
def test_real_upstream_formula_matches_independent_si_math_without_mutating_model(power_factor):
    source = model()
    before = source.to_json()
    inputs = spike.circuit_inputs(source, "circuit:evse", power_factor=power_factor,
                                   resistance_ohm_per_m=0.001)
    assert inputs["one_way_length_m"] == pytest.approx(7.3)
    assert inputs["real_power_w"] == 9600 * power_factor
    report = spike.compare(inputs)
    # Hand calculation: I = 9600 VA / 240 V = 40 A; Vd = 2 * 40 * 7.3 * .001.
    assert report["upstream_result"] == pytest.approx({
        "current_a": 40, "voltage_drop_v": 0.584, "voltage_drop_percent": 0.24333333333333335,
    })
    assert report["classification"] == "derived_prototype_not_code_compliance"
    assert report["inputs"]["circuit_id"] == source.circuits[0].id
    assert report["inputs"]["route_id"] == source.routes[0].id
    assert report["assumptions"]["excluded"]
    assert spike.compare(inputs) == report
    json.dumps(report, allow_nan=False)
    assert source.to_json() == before


def test_zero_resistance_does_not_invent_a_conductor_selection():
    report = spike.compare(spike.circuit_inputs(model(), "circuit:evse", power_factor=1,
                                               resistance_ohm_per_m=0))
    assert report["upstream_result"]["voltage_drop_v"] == 0
    assert set(report["upstream_result"]) == {"current_a", "voltage_drop_v", "voltage_drop_percent"}


@pytest.mark.parametrize("power_factor,resistance", [(0, .001), (1.1, .001),
    (float("nan"), .001), (1, -1), (1, float("inf"))])
def test_invalid_assumptions_are_rejected(power_factor, resistance):
    with pytest.raises(ValueError):
        spike.circuit_inputs(model(), "circuit:evse", power_factor=power_factor,
                             resistance_ohm_per_m=resistance)


@pytest.mark.parametrize("changes", [{"phase": "3ph"}, {"phase": None},
    {"load_va": None}, {"voltage_v": None}, {"route_ids": ()},
    {"route_ids": ("route:panel-evse", "route:panel-evse")},
    {"load_port_ids": ("port:evse-feed", "port:panel-load")}])
def test_incomplete_or_unsupported_circuits_stay_unresolved(changes):
    source = model()
    source = replace(source, circuits=(replace(source.circuits[0], **changes),))
    with pytest.raises(ValueError):
        spike.circuit_inputs(source, "circuit:evse", power_factor=1, resistance_ohm_per_m=.001)


def test_valid_references_do_not_substitute_for_a_source_to_load_route():
    source = model()
    # Both circuit references exist, but this route starts at the panel, not
    # at the claimed EVSE source. The canonical reference validator accepts
    # the references; the calculation boundary must still reject the path.
    source = replace(source, circuits=(replace(source.circuits[0], source_port_id="port:evse-feed"),))
    with pytest.raises(ValueError, match="source port to its load port"):
        spike.circuit_inputs(source, "circuit:evse", power_factor=1, resistance_ohm_per_m=.001)


def test_unknown_circuit_is_rejected():
    with pytest.raises(ValueError, match="circuit_id"):
        spike.circuit_inputs(model(), "circuit:missing", power_factor=1, resistance_ohm_per_m=.001)


@pytest.mark.parametrize("path,digest", [
    ("electrical/electrics.ts", "5e30c37606e5352a745a99b83bcd8e2797c68ef8317853972b716d3c37b2245a"),
    ("units/quantity.ts", "fbb0b5ae3f0b244280cc46b2e4d32d164f4712a70b551463ddb5421d8aa92c01"),
])
def test_upstream_sources_are_verbatim_at_the_audited_revision(path, digest):
    assert hashlib.sha256((ROOT / "spikes/graph_studio/upstream" / path).read_bytes()).hexdigest() == digest
