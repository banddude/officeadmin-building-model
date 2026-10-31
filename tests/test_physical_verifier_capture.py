"""Public synthetic bounded-capture, work and exact adapter tests."""
from dataclasses import replace
from fractions import Fraction as F
import json
import pytest

from oabm.model import BuildingModel, ContractError, PhysicalRoutingAdmissionError
from oabm.model.physical_routes import exact_ticks
from oabm.routing.physical_capture import Capture, capture, bounded_source_json, SOURCE_BYTES
from oabm.routing.physical_verify_types import WorkLedger, WorkLimit, VerificationStop, reserved_sort
from oabm.routing.physical_verify import adapt_primitives
from physical_route_fixture import make_physical_source, make_physical_route
from test_physical_route_contract import make_arc_model, rotations


def test_capture_detaches_every_mutable_mapping_and_preserves_canonical_bytes():
    source = make_physical_source()
    source.attributes["nested"] = {"values": [1, "x", {"y": 2}]}
    copied = capture(source, byte_limit=SOURCE_BYTES)
    assert copied.to_json() == source.to_json()
    source.attributes["nested"]["values"][2]["y"] = 4
    assert copied.attributes["nested"]["values"][2]["y"] == 2


@pytest.mark.parametrize("value", ["", "\u0000", "\n\t\"\\", "é", "😀", "\ud800", 123, -123, 1e300, None, True])
def test_conservative_byte_estimate_bounds_actual_escaped_json(value):
    counter = Capture(1_000_000)
    assert counter.copy(value) == value
    assert counter.bytes >= len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def test_aliases_charged_per_occurrence_and_cycles_refused():
    data = ["x"*100]
    counter = Capture(10000)
    counter.copy([data, data])
    assert counter.nodes == 5
    assert counter.bytes > 2400
    data.append(data)
    with pytest.raises(VerificationStop) as error:
        Capture(10000).copy(data)
    assert error.value.reason == "cyclic_input"


@pytest.mark.parametrize("data, kwargs, reason", [
    (["long"], {"byte_limit": 3}, "capture_byte_limit"),
    ([1,2], {"byte_limit": 100, "node_limit": 2}, "capture_node_limit"),
    ([[1]], {"byte_limit": 100, "depth_limit": 1}, "capture_depth_limit"),
    (1 << 4096, {"byte_limit": 10000}, "capture_integer_limit"),
])
def test_capture_limits_refuse_before_copy(data, kwargs, reason):
    with pytest.raises(VerificationStop) as error:
        capture(data, **kwargs)
    assert error.value.status == "resource_limit"
    assert error.value.reason == reason


def test_custom_container_callbacks_never_run():
    class Evil(list):
        def __iter__(self):
            raise AssertionError("must never invoke")
    with pytest.raises(VerificationStop, match="unsupported_capture_type"):
        Capture(1000).copy(Evil([1]))


@pytest.mark.parametrize("text, status", [
    ("["*65+"0"+"]"*65, "resource_limit"),
    ('{"x":'+"1"*129+"}", "resource_limit"),
    (b"\xff", "invalid_input"),
    ("\ud800", "invalid_input"),
    ('{"x":"unterminated}', "invalid_input"),
])
def test_raw_text_bounded_admission(text, status):
    with pytest.raises(VerificationStop) as error:
        bounded_source_json(text)
    assert error.value.status == status


def test_raw_quoted_brackets_do_not_count_as_structure():
    text = json.dumps({"x": "["*100+"}"*100+'"'})
    assert bounded_source_json(text) == text


def test_reserved_sort_permutations_charge_identically():
    import itertools
    results = []
    for items in itertools.permutations((3,1,2,0)):
        ledger = WorkLedger(100)
        assert reserved_sort(items, ledger) == [0,1,2,3]
        assert ledger.actual_sort_comparisons <= 8
        results.append((ledger.used, ledger.counters))
    assert len(set(results)) == 1


def test_worklimit_precedes_work_and_does_not_debit_failed_reservation():
    ledger = WorkLedger(7)
    with pytest.raises(WorkLimit):
        reserved_sort((3,1,2,0), ledger)
    assert ledger.used == 0


def test_line_adapter_flat_end_bounds():
    primitive, = adapt_primitives(make_physical_route(), WorkLedger(100))
    assert primitive.bounds == ((F(0),F(2)),(-F(1,100),F(1,100)),(-F(1,100),F(1,100)))
    assert primitive.axis == 0


def test_arc_adapter_exact_across_rotations():
    for transform in rotations():
        model = make_arc_model(transform)
        primitives = adapt_primitives(model.routes[0], WorkLedger(100))
        arc = primitives[1]
        assert arc.major_radius == F(3,10)
        assert arc.tube_radius == F(1,100)
        assert arc.center == tuple(F(v) for v in transform((F(7,10),F(3,10),0)))
        for point in (arc.start, arc.end):
            assert all(lo <= v <= hi for v,(lo,hi) in zip(point,arc.bounds))


def test_typed_numeric_reason_keeps_contract_base_exception():
    with pytest.raises(PhysicalRoutingAdmissionError) as error:
        exact_ticks(0.0000000001,"test")
    assert isinstance(error.value, ContractError)
    assert error.value.category == "unsupported_input"
    assert error.value.reason == "numeric_grid"


def test_candidate_capture_preserves_noninit_discriminators():
    route=make_physical_route()
    copied=capture(route,byte_limit=1000000)
    assert copied == route
    object.__setattr__(route.segments[0],"kind","wrong")
    with pytest.raises(VerificationStop,match="fixed_canonical_field_mutated"):
        capture(route,byte_limit=1000000)


def test_live_list_growth_during_capture_cannot_bypass_counters(monkeypatch):
    original=[1]
    copy_method=Capture.copy
    def racing_copy(self,value,depth=0):
        if type(value) is int:
            original.append(1)
        return copy_method(self,value,depth)
    monkeypatch.setattr(Capture,"copy",racing_copy)
    counter=Capture(10000,node_limit=20)
    with pytest.raises(VerificationStop,match="capture_node_limit"):
        counter.copy(original)
    assert counter.nodes == 20
    assert len(original) <= 21


def test_all_canonical_fixture_owners_are_detached_without_byte_changes():
    for transform in rotations():
        model=make_arc_model(transform)
        counter=Capture(SOURCE_BYTES)
        copied=counter.copy(model)
        assert copied.to_json(indent=None) == model.to_json(indent=None)
        assert counter.bytes >= len(model.to_json(indent=None).encode("utf-8"))


@pytest.mark.parametrize("value", [[False]*10000,[None]*10000,[True,False,None]*1000,
                                  {"a":[False]*10000},[[],[],[]]*1000])
def test_estimator_covers_default_canonical_spaces_not_only_compact_json(value):
    counter=Capture(SOURCE_BYTES)
    copied=counter.copy(value)
    assert counter.bytes >= len(json.dumps(copied,ensure_ascii=True,sort_keys=True))
    model=make_physical_source()
    model.attributes["many_values"]=value
    counter=Capture(SOURCE_BYTES)
    copied=counter.copy(model)
    assert counter.bytes >= len(copied.to_json(indent=None).encode("utf-8"))


@pytest.mark.parametrize("value", [-1000,-100,-10,-1,0,1,10,100,1000])
def test_scalar_estimate_covers_typed_numeric_normalization(value):
    counter=Capture(1000)
    counter.copy(value)
    assert counter.bytes >= len(json.dumps(float(value)))
    source=make_physical_source()
    source=replace(source,ports=tuple(replace(p,pose=replace(p.pose,
        position=replace(p.pose.position,x=value,y=value,z=value))) for p in source.ports))
    counter=Capture(SOURCE_BYTES)
    copied=counter.copy(source)
    normalized=BuildingModel.from_json(copied.to_json(indent=None))
    assert counter.bytes >= len(normalized.to_json(indent=None).encode("utf-8"))
