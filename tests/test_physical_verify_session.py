"""Shared proof-budget sessions do not bypass ordinary live admission."""
from dataclasses import replace
import hashlib
import pytest

from physical_route_fixture import make_physical_source, make_physical_route
from test_physical_verify import rebind, obstacle_source
from oabm.routing.physical_verify import verify_physical_route, _PhysicalVerificationSession
from oabm.routing.physical_verify_types import WorkLedger, VerificationStop


def fixture(limit=130):
    source = make_physical_source()
    route = make_physical_route(source)
    route = rebind(route, execution_limits=replace(route.context.execution_limits,
                                                   total_refinement_cells=limit))
    return source, route


def test_public_receipt_bytes_unchanged():
    source = make_physical_source()
    result = verify_physical_route(source, make_physical_route(source))
    assert result.status == "verified_route"
    assert hashlib.sha256(result.to_json().encode()).hexdigest() == "7efc555190b07e80db38e6ed7bcd82ae790f2de6fdf894cb7e03847ab0acbf2e"
    assert "session_work" not in result.to_dict()


def test_search_and_multiple_candidates_share_one_original_budget():
    source, route = fixture()
    ledger = WorkLedger(130)
    ledger.debit("search", 10)
    session = _PhysicalVerificationSession(ledger)
    first = session.verify(source, route)
    assert first.status == "verified_route"
    assert first.to_dict()["work"]["charged"] == 63
    assert first.to_dict()["session_work"]["charged_cumulative"] == 73
    ledger.debit("search", 3)
    second = session.verify(source, route)
    assert second.status == "resource_limit"
    doc = second.to_dict()
    assert doc["session_work"]["charged_at_candidate_start"] == 76
    assert doc["work"]["charged"] == doc["session_work"]["candidate_charged"]
    assert ledger.used <= 130
    assert doc["session_work"]["charged_cumulative"] == ledger.used
    assert doc["session_work"]["unfinished_cursor"] == doc["unfinished_cursor"]


def test_candidate_trace_and_diagnostics_are_not_cumulative():
    source, route = fixture(500)
    session = _PhysicalVerificationSession(WorkLedger(500))
    first, second = session.verify(source, route), session.verify(source, route)
    a, b = first.to_dict(), second.to_dict()
    assert first.status == second.status == "verified_route"
    assert a["work"] == b["work"]
    assert a["diagnostics"] == b["diagnostics"]
    assert b["session_work"]["charged_cumulative"] == 2*a["work"]["charged"]


def test_original_limit_required_not_remaining_or_larger_limit():
    source, route = fixture()
    for limit in (129, 131):
        ledger = WorkLedger(limit)
        result = _PhysicalVerificationSession(ledger).verify(source, route)
        assert (result.status, result.reason) == ("invalid_input", "verification_session_limit_mismatch")
        assert ledger.used == 0


def test_valid_rebound_new_request_and_source_cannot_reuse_session():
    source, route = fixture(500)
    ledger = WorkLedger(500)
    session = _PhysicalVerificationSession(ledger)
    assert session.verify(source, route).status == "verified_route"
    start = ledger.used
    changed_request = rebind(route, execution_limits=replace(route.context.execution_limits,
                                                            interval_refinement_depth=1))
    assert session.verify(source, changed_request).reason == "verification_session_authority_mismatch"
    other = replace(source, name="independent source")
    from oabm.model import compute_physical_source_sha256
    changed_source = rebind(route, source_model_sha256=compute_physical_source_sha256(other))
    assert session.verify(other, changed_source).reason == "verification_session_authority_mismatch"
    assert ledger.used == start


def test_session_still_runs_capture_and_binding_each_time():
    source, route = fixture(500)
    ledger = WorkLedger(500)
    session = _PhysicalVerificationSession(ledger)
    assert session.verify(source, route).status == "verified_route"
    used = ledger.used
    source.attributes["changed"] = True
    result = session.verify(source, route)
    assert result.reason == "stale_source_binding"
    assert ledger.used == used
    assert result.to_dict()["session_work"]["unfinished_cursor"]["kind"] == "input_admission"
    source.attributes.clear()
    route.attributes["cycle"] = route.attributes
    assert session.verify(source, route).reason == "cyclic_input"
    assert ledger.used == used


def test_session_does_not_bypass_live_collision_geometry():
    source, route = obstacle_source()
    ledger = WorkLedger(route.context.execution_limits.total_refinement_cells)
    session = _PhysicalVerificationSession(ledger)
    first = session.verify(source, route)
    second = session.verify(source, route)
    assert first.reason == second.reason == "obstacle_contact_or_overlap"
    assert first.status == second.status == "rejected_route"
    assert ledger.used == first.to_dict()["work"]["charged"] + second.to_dict()["work"]["charged"]


def test_reset_or_changed_shared_ledger_is_refused():
    source, route = fixture(500)
    ledger = WorkLedger(500)
    session = _PhysicalVerificationSession(ledger)
    session.verify(source, route)
    ledger.used = 0
    ledger._counts.clear()
    with pytest.raises(VerificationStop, match="verification_session_ledger_changed"):
        session.verify(source, route)
    ledger = WorkLedger(500)
    session = _PhysicalVerificationSession(ledger)
    ledger.limit = 501
    with pytest.raises(VerificationStop, match="verification_session_ledger_changed"):
        session.verify(source, route)


def test_candidate_sha_is_not_pinned_but_source_request_are():
    source, route = fixture(500)
    session = _PhysicalVerificationSession(WorkLedger(500))
    first = session.verify(source, route)
    changed = replace(route, attributes={"candidate_note": "second realization metadata"})
    second = session.verify(source, changed)
    assert first.status == second.status == "verified_route"
    assert first.to_dict()["authority"]["candidate_sha256"] != second.to_dict()["authority"]["candidate_sha256"]
    assert first.to_dict()["authority"]["request_sha256"] == second.to_dict()["authority"]["request_sha256"]


def test_malformed_candidate_does_not_pin_session_authority():
    source, route = fixture(500)
    session = _PhysicalVerificationSession(WorkLedger(500))
    route.attributes["cycle"] = route.attributes
    assert session.verify(source, route).reason == "cyclic_input"
    other = replace(source, name="different first admitted source")
    valid = make_physical_route(other)
    valid = rebind(valid, execution_limits=replace(valid.context.execution_limits, total_refinement_cells=500))
    assert session.verify(other, valid).status == "verified_route"


def test_constructor_precharged_baseline_cannot_be_reset_before_first_call():
    source, route = fixture(500)
    ledger = WorkLedger(500)
    ledger.debit("search", 10)
    session = _PhysicalVerificationSession(ledger)
    ledger.used = 0
    ledger._counts.clear()
    with pytest.raises(VerificationStop, match="verification_session_ledger_changed"):
        session.verify(source, route)


@pytest.mark.parametrize("precharged", [0, 10])
def test_replacement_ledger_with_identical_accounting_is_refused(precharged):
    source, route = fixture(500)
    ledger = WorkLedger(500)
    if precharged:
        ledger.debit("search", precharged)
    session = _PhysicalVerificationSession(ledger)
    replacement = WorkLedger(500)
    if precharged:
        replacement.debit("search", precharged)
    session._shared = replacement
    with pytest.raises(VerificationStop, match="verification_session_ledger_changed"):
        session.verify(source, route)
    assert ledger.used == replacement.used == precharged
