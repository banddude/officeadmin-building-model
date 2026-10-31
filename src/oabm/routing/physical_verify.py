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
    text = _encode(document)
    if len(text) > REPORT_BYTES:
        # Fixed terminal fallback remains possible independently of exhausted
        # proof budget. No geometry is executed on this path.
        document = {"algorithm_profile": PROFILE, "status": "resource_limit",
                    "reason": "report_byte_limit", "authority": authority}
        text = _encode(document)
        status, reason = "resource_limit", "report_byte_limit"
    return PhysicalRouteVerificationResult(status, reason, text)


def verify_physical_route(source: BuildingModel, candidate: PhysicalRoute, *,
                          source_json: str | bytes | None = None) -> PhysicalRouteVerificationResult:
    ledger = None
    authority = {}
    scope = {"checked_source_ids": [], "excluded_source_ids": []}
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
        ledger = WorkLedger(limits.total_refinement_cells)
        primitives = adapt_primitives(route, ledger)
        inventory = physical_geometry_inventory(base, route.route_type)
        coverage = {row.source_entity_id: row for row in route.context.coverage}
        ledger.debit("coverage_inventory", len(inventory))
        identities = reserved_sort(tuple(inventory), ledger)
        threshold = Fraction(exact_ticks(route.requirements.clearance_m, "route clearance",
                              minimum="0", maximum="100"), NANO) + MARGIN
        for identity in identities:
            item, row = inventory[identity], coverage[identity]
            if row.decision == "outside_proof_scope":
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
            scope["checked_source_ids"].append(identity)
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
