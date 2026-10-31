"""Live, source-bound verification of ONE supplied route, never generation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import hashlib
import json

from oabm.model import (BuildingModel, PhysicalRoute, ContractError,
    PhysicalRoutingAdmissionError, Box3D, Obstacle, RouteConstraint, StructuralMember,
    physical_segment_facts, physical_box_bounds, physical_geometry_inventory,
    validate_physical_context_source, compute_physical_source_sha256)
from oabm.model.physical_routes import point_ticks, exact_ticks
from .physical_capture import (capture, bounded_source_json, SOURCE_BYTES,
    CANDIDATE_BYTES, COMBINED_BYTES, REPORT_BYTES)
from .physical_verify_types import (Primitive, WorkLedger, VerificationStop,
    Diagnostic, PROFILE, reserved_sort)
from .physical_domain import check_box, verify_domain

NANO = 1_000_000_000
MARGIN = Fraction(1, 1_000_000)


@dataclass(frozen=True)
class PhysicalRouteVerificationResult:
    status: str
    reason: str
    report_json: str

    def to_json(self) -> str:
        return self.report_json

    def to_dict(self) -> dict:
        return json.loads(self.report_json)


def _encode(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _snapshot(model, cap):
    text = model.to_json(indent=None)
    if len(text.encode("utf-8")) > cap:
        raise VerificationStop("resource_limit", "canonical_snapshot_byte_limit")
    return text


def _box(box):
    lo, hi = physical_box_bounds(box)
    return tuple((Fraction(a, NANO), Fraction(b, NANO)) for a, b in zip(lo, hi))


def adapt_primitives(route, ledger):
    ledger.set_cursor("primitive_adaptation")
    ledger.debit("primitive_adaptation", len(route.segments))
    ledger.debit("joint_adaptation", len(route.joints))
    points = {j.id: tuple(Fraction(v, NANO) for v in point_ticks(j.position)) for j in route.joints}
    radius = Fraction(exact_ticks(route.requirements.outside_diameter_m, "OD", minimum=".001", maximum="1"), 2*NANO)
    facts = physical_segment_facts(route)
    result = []
    for segment, fact in zip(route.segments, facts):
        start, end = points[segment.start_joint_id], points[segment.end_joint_id]
        kwargs = dict(segment_id=segment.id, kind=segment.kind, start=start, end=end,
                      start_tangent=tuple(int(v) for v in fact["start_tangent"]),
                      end_tangent=tuple(int(v) for v in fact["end_tangent"]), tube_radius=radius)
        if segment.kind == "line":
            axis = next(i for i in range(3) if start[i] != end[i])
            result.append(Primitive(**kwargs, center=start, axis=axis,
                axial=(min(start[axis], end[axis]), max(start[axis], end[axis]))))
        else:
            center = tuple(Fraction(v, NANO) for v in fact["centre_ticks"])
            a = next(i for i in range(3) if start[i] != center[i])
            b = next(i for i in range(3) if end[i] != center[i])
            signs = (1 if start[a]>center[a] else -1, 1 if end[b]>center[b] else -1)
            result.append(Primitive(**kwargs, center=center, plane_axes=(a,b), signs=signs,
                major_radius=Fraction(fact["radius_ticks"], NANO)))
    return tuple(result)


def _finish(status, reason, ledger, authority, scope, stop=None):
    document = {"algorithm_profile": PROFILE, "status": status, "reason": reason,
                "authority": authority, "scope": scope}
    if ledger is not None:
        document["work"] = {"limit": ledger.limit, "charged": ledger.used,
            "charged_by_kind": dict(ledger.counters),
            "actual_sort_comparisons": ledger.actual_sort_comparisons,
            "completed_records": ledger.records, "trace_sha256": ledger.trace_sha256}
    diagnostics = []
    diagnostic_bytes = 0
    # Final decisive diagnostic takes priority over nondecisive completed checks.
    records = (() if stop is None else (Diagnostic(stop.reason, stop.ids, stop.exact),))
    records += () if ledger is None else ledger.diagnostics
    for record in records:
        data = record.to_dict()
        size = len(_encode(data))
        if diagnostic_bytes + size > 2*1024*1024 or len(diagnostics) == 256:
            break
        diagnostics.append(data)
        diagnostic_bytes += size
    document["diagnostics"] = diagnostics
    total_diagnostics = (0 if ledger is None else ledger.records) + int(stop is not None)
    document["diagnostics_total"] = total_diagnostics
    document["diagnostics_omitted"] = total_diagnostics - len(diagnostics)
    if status != "verified_route":
        document["unfinished_cursor"] = (ledger.cursor.to_dict() if ledger is not None
            else {"kind":"input_admission","ids":[],"exact":{}})
        document["remaining_count"] = "unknown"
    text = _encode(document)
    if len(text) > REPORT_BYTES:
        # Fixed terminal fallback remains possible independently of exhausted
        # proof budget. No geometry is executed on this path.
        document = {"algorithm_profile": PROFILE, "status": "resource_limit",
                    "reason": "report_byte_limit", "authority": authority,
                    "remaining_count":"unknown", "diagnostics_total":total_diagnostics,
                    "diagnostics_omitted":total_diagnostics}
        text = _encode(document)
        status, reason = "resource_limit", "report_byte_limit"
    return PhysicalRouteVerificationResult(status, reason, text)


def _verify_physical_route(source: BuildingModel, candidate: PhysicalRoute, *,
                           source_json: str | bytes | None = None, session=None) -> PhysicalRouteVerificationResult:
    ledger = None
    authority = {}
    scope = {"checked_source_ids": [], "excluded_source_ids": [],
             "geometry_unspecified_source_ids": []}
    try:
        if type(source) is not BuildingModel or type(candidate) is not PhysicalRoute:
            raise VerificationStop("invalid_input", "source_candidate_type")
        raw_source = bounded_source_json(source_json)
        base = capture(source, byte_limit=SOURCE_BYTES)
        route = capture(candidate, byte_limit=CANDIDATE_BYTES)
        # Normalize ONLY detached graphs through the canonical API.
        base = BuildingModel.from_json(_snapshot(base, SOURCE_BYTES))
        if route.id in {r.id for r in base.routes}:
            raise VerificationStop("invalid_input", "self_containing_source")
        combined = capture(replace(base, routes=(*base.routes, route)),
                           byte_limit=COMBINED_BYTES, node_limit=500_000)
        combined = BuildingModel.from_json(_snapshot(combined, COMBINED_BYTES))
        route = combined.routes[-1]
        base = replace(combined, routes=combined.routes[:-1])
        admission = validate_physical_context_source(base, route, source_json=raw_source)
        route_document = combined.to_dict()["routes"][-1]
        route_bytes = _encode(route_document).encode("ascii")
        if len(route_bytes) > CANDIDATE_BYTES:
            raise VerificationStop("resource_limit", "candidate_snapshot_byte_limit")
        authority = {"source_model_sha256": compute_physical_source_sha256(base),
            "request_sha256": route.context.request_sha256,
            "candidate_sha256": hashlib.sha256(route_bytes).hexdigest(),
            "source_admission": admission, "candidate_admission": "canonical_python_snapshot",
            "numerical_profile": route.requirements.profile}
        limits = route.context.execution_limits
        authority["effective_proof_limits"] = {
            "total_work_units": limits.total_refinement_cells,
            "interval_refinement_depth": limits.interval_refinement_depth,
            "kernel_precision_bits": [32,64,128][:limits.interval_refinement_depth+1]}
        authority["obstacle_domain_margin_m"] = {"numerator":"1","denominator":"1000000"}
        authority["self_required_separation_m"] = {"numerator":"0","denominator":"1","comparison":"strictly_greater"}
        ledger = (WorkLedger(limits.total_refinement_cells) if session is None
                  else session._candidate_ledger(authority))
        inventory = physical_geometry_inventory(base, route.route_type)
        optional_geometry = (base.electrical_equipment, base.electrical_devices,
                             base.fitting_selections)
        ledger.set_cursor("geometry_scope_scan")
        ledger.debit("geometry_scope_scan", sum(len(items) for items in optional_geometry))
        unspecified = []
        for items in optional_geometry:
            for item in items:
                if item.size is None:
                    if len(inventory) + len(unspecified) >= 10_000:
                        raise VerificationStop("resource_limit", "scope_disclosure_limit")
                    ledger.debit("scope_id_retention")
                    unspecified.append(item.id)
        scope["geometry_unspecified_source_ids"] = reserved_sort(unspecified, ledger)
        primitives = adapt_primitives(route, ledger)
        coverage = {row.source_entity_id: row for row in route.context.coverage}
        ledger.debit("coverage_inventory", len(inventory))
        identities = reserved_sort(tuple(inventory), ledger)
        threshold = Fraction(exact_ticks(route.requirements.clearance_m, "route clearance",
                              minimum="0", maximum="100"), NANO) + MARGIN
        for identity in identities:
            ledger.set_cursor("source_blocker", (identity,))
            item, row = inventory[identity], coverage[identity]
            if row.decision == "outside_proof_scope":
                ledger.debit("scope_id_retention")
                scope["excluded_source_ids"].append(identity)
                ledger.record("explicit_scope_exclusion", (identity,))
                continue
            ledger.debit("blocker_adaptation")
            if isinstance(item, (Obstacle, RouteConstraint)):
                box = _box(item.geometry)
                extra = Fraction(exact_ticks(item.clearance_m, "blocker clearance",
                                 minimum="0", maximum="100"), NANO)
            elif isinstance(item, StructuralMember):
                box, extra = _box(Box3D(pose=item.pose, size=item.size)), Fraction(0)
            else:
                raise VerificationStop("unsupported_input", "direct_source_adapter", (identity,))
            for primitive in primitives:
                check_box(primitive, box, threshold+extra, ledger,
                    ids=(primitive.segment_id, identity), max_depth=limits.interval_refinement_depth,
                    kind="obstacle")
            ledger.debit("scope_id_retention")
            scope["checked_source_ids"].append(identity)
        ledger.set_cursor("domain_adaptation")
        ledger.debit("domain_adaptation", len(route.context.allowed_domain.volumes))
        domain = tuple(_box(box) for box in route.context.allowed_domain.volumes)
        verify_domain(primitives, domain, threshold, ledger, max_depth=limits.interval_refinement_depth)
        from .physical_self import verify_self
        verify_self(primitives, ledger, max_depth=limits.interval_refinement_depth)
        return _finish("verified_route", "all_required_checks_complete", ledger, authority, scope)
    except PhysicalRoutingAdmissionError as exc:
        stop = VerificationStop(exc.category, exc.reason)
    except VerificationStop as exc:
        stop = exc
    except (ContractError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        # No classification by human-readable exception text.
        stop = VerificationStop("invalid_input", "canonical_admission_failed")
    try:
        return _finish(stop.status, stop.reason, ledger, authority, scope, stop)
    except VerificationStop:
        return _finish("resource_limit", "receipt_encoding_limit", ledger, authority, scope)

class _CandidateWorkLedger(WorkLedger):
    """Candidate-only trace/diagnostics; every debit spends the shared budget."""
    def __init__(self, shared):
        super().__init__(shared.limit)
        self._shared = shared

    def debit(self, kind, units=1):
        # Shared debit fails before either candidate work or local accounting.
        self._shared.debit(kind, units)
        super().debit(kind, units)

    def set_cursor(self, stage, ids=(), *, exact=()):
        super().set_cursor(stage, ids, exact=exact)
        self._shared.set_cursor(stage, ids, exact=exact)


class _PhysicalVerificationSession:
    """Internal single-threaded session, not a captured-input or binding bypass.

    Search may charge the exact shared WorkLedger before/between calls. This
    object pins the first admitted source/request and original effective limit.
    Candidate trace/diagnostics never include another candidate's checks.
    Concurrent session use or external concurrent ledger mutation is unsupported.
    """
    def __init__(self, shared_ledger):
        if type(shared_ledger) is not WorkLedger:
            raise ValueError("exact WorkLedger required")
        self._shared = shared_ledger
        self._original_shared = shared_ledger
        self._limit = shared_ledger.limit
        self._authority_key = None
        self._busy = False
        self._last_used = 0
        self._last_counts = {}
        self._check_accounting()
        self._last_used = shared_ledger.used
        self._last_counts = dict(shared_ledger.counters)

    def _check_accounting(self):
        ledger = self._shared
        if ledger is not self._original_shared or type(ledger) is not WorkLedger:
            raise VerificationStop("invalid_input", "verification_session_ledger_changed")
        counts = dict(ledger.counters)
        if (type(ledger.limit) is not int or ledger.limit != self._limit
                or type(ledger.used) is not int
                or not 0 <= ledger.used <= ledger.limit or ledger.used < self._last_used
                or any(type(v) is not int or v < 0 for v in counts.values())
                or sum(counts.values()) != ledger.used
                or any(counts.get(k, 0) < v for k, v in self._last_counts.items())):
            raise VerificationStop("invalid_input", "verification_session_ledger_changed")

    def _candidate_ledger(self, authority):
        self._check_accounting()
        limits = authority["effective_proof_limits"]
        if self._limit != limits["total_work_units"]:
            raise VerificationStop("invalid_input", "verification_session_limit_mismatch")
        key = (authority["source_model_sha256"], authority["request_sha256"],
               authority["numerical_profile"], _encode(limits))
        if self._authority_key is not None and key != self._authority_key:
            raise VerificationStop("invalid_input", "verification_session_authority_mismatch")
        self._authority_key = key
        return _CandidateWorkLedger(self._shared)

    def verify(self, source, candidate, *, source_json=None):
        if self._busy:
            raise VerificationStop("invalid_input", "verification_session_reentrant")
        self._check_accounting()
        self._busy = True
        start = self._shared.used
        # Input-admission failures must not inherit the previous candidate cursor.
        self._shared.set_cursor("input_admission")
        try:
            result = _verify_physical_route(source, candidate,
                                           source_json=source_json, session=self)
            document = result.to_dict()
            document["session_work"] = {
                "accounting": "shared_search_and_candidate_proof_charges",
                "limit": self._shared.limit,
                "charged_at_candidate_start": start,
                "available_at_candidate_start": self._shared.limit-start,
                "candidate_charged": self._shared.used-start,
                "charged_cumulative": self._shared.used,
                "charged_by_kind_cumulative": dict(self._shared.counters),
                "candidate_trace_scope": "this_candidate_only",
            }
            if result.status != "verified_route":
                document["session_work"]["unfinished_cursor"] = self._shared.cursor.to_dict()
                document["session_work"]["remaining_count"] = "unknown"
            text = _encode(document)
            if len(text) > REPORT_BYTES:
                document = {"algorithm_profile": PROFILE, "status": "resource_limit",
                            "reason": "report_byte_limit", "authority": document.get("authority", {}),
                            "session_work": document["session_work"], "remaining_count": "unknown"}
                return PhysicalRouteVerificationResult("resource_limit", "report_byte_limit", _encode(document))
            return PhysicalRouteVerificationResult(result.status, result.reason, text)
        finally:
            self._last_used = self._shared.used
            self._last_counts = dict(self._shared.counters)
            self._busy = False


def verify_physical_route(source: BuildingModel, candidate: PhysicalRoute, *,
                          source_json: str | bytes | None = None) -> PhysicalRouteVerificationResult:
    """Verify one supplied route with its original independent work budget."""
    return _verify_physical_route(source, candidate, source_json=source_json)
