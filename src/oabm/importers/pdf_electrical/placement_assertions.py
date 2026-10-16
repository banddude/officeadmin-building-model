"""Explicit per-field evidence gate over the existing symbol intake API."""
from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from oabm.model import BuildingModel, ContractError, Provenance
from oabm.proposals.contracts import Portable, digest, fingerprint, text, _finite
from .symbol_stage import (SymbolDecision, SymbolRegistration, SymbolReview,
                           build_symbol_review, materialize_reviewed_symbols)

VERSION = "placement-assertions-v1"
FIELDS = ("registration_xy", "level_elevation", "anchor", "elevation", "yaw", "entity_type")
ROLES = ("source_observed", "field_measured", "project_defined", "designed",
         "diagram_convention", "unknown")
UNITS = {"registration_xy": "m/pt;m", "level_elevation": "m", "anchor": "pt",
         "elevation": "m", "yaw": "rad", "entity_type": "token"}


def _tuple(values, cls, label, maximum=4096):
    if not isinstance(values, tuple) or len(values) > maximum or any(not isinstance(v, cls) for v in values):
        raise ContractError(label + " requires a bounded typed tuple")


def _point(values, size, label):
    if not isinstance(values, tuple) or len(values) != size:
        raise ContractError(label + " has wrong coordinate dimension")
    return tuple(float(_finite(v, label)) for v in values)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementEvidence(Portable):
    """Immutable supplied bytes; PDF locators use displayed bottom-origin points."""
    source_id: str
    document_sha256: str
    provenance: Provenance
    locator: str
    kind: str
    image_id: str | None = None
    page: int | None = None
    bounds_pt: tuple[float, float, float, float] | None = None

    def __post_init__(self):
        if self.kind not in ("source_fact", "legend_example", "measurement_record", "project_decision"):
            raise ContractError("unsupported placement evidence kind")
        text(self.source_id, "evidence source")
        fingerprint(self.document_sha256, "evidence document")
        text(self.locator, "evidence locator", maximum=2048)
        if not isinstance(self.provenance, Provenance) or self.provenance.derivation is None:
            raise ContractError("evidence requires explicit canonical provenance")
        if self.provenance.source_id != self.source_id:
            raise ContractError("evidence provenance source mismatch")
        if self.image_id is not None:
            text(self.image_id, "evidence image")
            if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
                raise ContractError("PDF evidence requires original one-based page")
            box = _point(self.bounds_pt, 4, "evidence bounds")
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ContractError("evidence bounds must be ordered")
            object.__setattr__(self, "bounds_pt", box)
            if self.provenance.page != self.page:
                raise ContractError("evidence provenance page mismatch")
        elif self.page is not None or self.bounds_pt is not None:
            raise ContractError("PDF locator requires a verified source image")


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistrationControl(Portable):
    source_pt: tuple[float, float]
    canonical_xy_m: tuple[float, float]
    independent_check: bool

    def __post_init__(self):
        object.__setattr__(self, "source_pt", _point(self.source_pt, 2, "source control"))
        object.__setattr__(self, "canonical_xy_m", _point(self.canonical_xy_m, 2, "canonical control"))
        if not isinstance(self.independent_check, bool):
            raise ContractError("control check flag must be explicit")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementAssertion(Portable):
    """A reviewed field association, not an assertion made by model output."""
    assertion_id: str
    proposal_id: str
    proposal_fingerprint: str
    field: str
    value: float | str | tuple[float, ...] | None
    unit: str
    frame_id: str
    reference_id: str
    basis: str
    role: str
    evidence: tuple[PlacementEvidence, ...]
    controls: tuple[RegistrationControl, ...] = ()
    tolerance_m: float | None = None

    def __post_init__(self):
        for key in ("assertion_id", "proposal_id", "frame_id", "reference_id", "basis"):
            text(getattr(self, key), key, maximum=512)
        fingerprint(self.proposal_fingerprint, "assertion proposal")
        if self.field not in FIELDS or self.unit != UNITS[self.field] or self.role not in ROLES:
            raise ContractError("unsupported assertion field, unit or role")
        _tuple(self.evidence, PlacementEvidence, "assertion evidence", 32)
        _tuple(self.controls, RegistrationControl, "registration controls", 64)
        if self.role == "unknown":
            if self.value is not None:
                raise ContractError("unknown assertion must keep value null")
        else:
            if not self.evidence:
                raise ContractError("known assertion requires immutable supplied evidence")
            if self.field in ("registration_xy", "anchor"):
                object.__setattr__(self, "value", _point(self.value, 6 if self.field == "registration_xy" else 2, "assertion value"))
            elif self.field == "entity_type":
                text(self.value, "asserted canonical type")
            else:
                object.__setattr__(self, "value", float(_finite(self.value, "assertion value")))
        if self.field != "registration_xy" and (self.controls or self.tolerance_m is not None):
            raise ContractError("only registration accepts control points")
        if self.tolerance_m is not None:
            value = float(_finite(self.tolerance_m, "control tolerance"))
            if not 0 <= value <= 1:
                raise ContractError("control tolerance must be within zero and one metre")
            object.__setattr__(self, "tolerance_m", value)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementAssertionReview(Portable):
    """Trusted caller review, bound to all current execution inputs."""
    version: str
    model_fingerprint: str
    symbol_review_fingerprint: str
    decisions_fingerprint: str
    registrations_fingerprint: str
    canonical_datum_id: str
    reviewer_id: str
    reviewer_derivation: str
    rationale: str
    assertions: tuple[PlacementAssertion, ...]

    def __post_init__(self):
        if self.version != VERSION:
            raise ContractError("unsupported placement assertion version")
        for key in ("model_fingerprint", "symbol_review_fingerprint",
                    "decisions_fingerprint", "registrations_fingerprint"):
            fingerprint(getattr(self, key), key)
        text(self.canonical_datum_id, "canonical vertical datum")
        text(self.reviewer_id, "assertion reviewer")
        text(self.rationale, "assertion rationale", maximum=4096)
        if self.reviewer_derivation not in ("user", "inferred"):
            raise ContractError("reviewer derivation must be explicit user or inferred")
        _tuple(self.assertions, PlacementAssertion, "placement assertions")
        if len({a.assertion_id for a in self.assertions}) != len(self.assertions):
            raise ContractError("duplicate placement assertion ID")

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    @classmethod
    def capture(cls, model, review, *, decisions, registrations, canonical_datum_id, reviewer_id,
                reviewer_derivation, rationale, assertions):
        return cls(version=VERSION, model_fingerprint=digest(model.to_dict()),
                   symbol_review_fingerprint=review.fingerprint,
                   decisions_fingerprint=_records_digest(decisions),
                   registrations_fingerprint=_records_digest(registrations),
                   canonical_datum_id=canonical_datum_id,
                   reviewer_id=reviewer_id, reviewer_derivation=reviewer_derivation,
                   rationale=rationale, assertions=tuple(assertions))


def _records_digest(records):
    return digest(sorted((r.to_dict() for r in records), key=digest))


def _validate_evidence(fact, supplied, source_images, row):
    if fact.role in ("designed", "project_defined") and not any(
            e.kind == "project_decision" for e in fact.evidence):
        raise ContractError("design/project definition requires explicit project-decision evidence")
    coordinate_boxes = []
    for evidence in fact.evidence:
        payload = supplied.get(evidence.document_sha256)
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= 128_000_000 or hashlib.sha256(payload).hexdigest() != evidence.document_sha256:
            raise ContractError("missing or stale assertion evidence bytes")
        if evidence.image_id is not None:
            image = source_images.get(evidence.image_id)
            if image is None or (image.source.source_id, image.source.document_sha256, image.source.page) != (
                    evidence.source_id, evidence.document_sha256, evidence.page):
                raise ContractError("assertion evidence source image/page mismatch")
            if fact.field in ("anchor", "registration_xy") and (
                    evidence.source_id, evidence.document_sha256, evidence.page) != (
                    row["source_id"], row["document_sha256"], row["page"]):
                raise ContractError("coordinate evidence belongs to another source page")
            x0,y0,x1,y1 = evidence.bounds_pt
            coordinate_boxes.append((x0,y0,x1,y1))
            # SourceImage crops are top-origin pixels; evidence is displayed bottom-origin pt.
            a,b,c,d,e,f = image.render_manifest.pixel_to_display_pt
            corners = [(a*x+b*y+e,c*x+d*y+f) for x,y in (
                (0,0),(image.crop_px[2]-image.crop_px[0],0),
                (0,image.crop_px[3]-image.crop_px[1]),
                (image.crop_px[2]-image.crop_px[0],image.crop_px[3]-image.crop_px[1]))]
            if not (min(p[0] for p in corners) <= x0 < x1 <= max(p[0] for p in corners)
                    and min(p[1] for p in corners) <= y0 < y1 <= max(p[1] for p in corners)):
                raise ContractError("assertion evidence bounds outside supplied image crop")
        if (evidence.kind == "legend_example" and fact.field != "entity_type"
                and fact.role in ("source_observed", "field_measured")):
            raise ContractError("legend example cannot establish physical placement")
        if fact.role == "source_observed" and (evidence.image_id is None or evidence.kind not in ("source_fact", "legend_example")):
            raise ContractError("source-observed assertion requires verified PDF image evidence")
        if fact.role == "field_measured" and (evidence.provenance.source_kind != "field-measurement"
                or evidence.kind != "measurement_record"):
            raise ContractError("measured assertion requires field-measurement provenance")
        if fact.role in ("source_observed", "field_measured") and evidence.provenance.derivation != "observed":
            raise ContractError("observed/measured fact requires observed evidence derivation")
    points = (fact.value,) if fact.field == "anchor" and fact.value is not None else tuple(
        c.source_pt for c in fact.controls)
    if coordinate_boxes and any(not any(x0 <= x <= x1 and y0 <= y <= y1
            for x0,y0,x1,y1 in coordinate_boxes) for x,y in points):
        raise ContractError("coordinate evidence does not cover asserted points")


def _field_reasons(facts, decision, registration, model, canonical_datum_id):
    """Return explicit missing/conflict/semantic failures; never substitute a value."""
    reasons = []
    chosen = {}
    for field in FIELDS:
        rows = [a for a in facts if a.field == field]
        if not rows:
            reasons.append("missing:" + field)
            continue
        signatures = {(digest({"value": a.value, "basis": a.basis, "role": a.role,
                               "frame": a.frame_id, "reference": a.reference_id,
                               "controls":[c.to_dict() for c in a.controls], "tolerance_m":a.tolerance_m})) for a in rows}
        if len(signatures) != 1:
            reasons.append("conflict:" + field)
            continue
        a = rows[0]
        if a.role in ("unknown", "diagram_convention"):
            reasons.append("physical_fact_unresolved:" + field)
            continue
        chosen[field] = a
    if registration is None:
        return reasons + ["missing:registration"]
    level = next((l for l in model.levels if l.id == registration.level_id), None)
    if level is None:
        return reasons + ["missing:level"]
    placement = decision.placement
    for field, a in chosen.items():
        if a.frame_id != model.coordinate_system.frame_id:
            reasons.append("frame_mismatch:" + field)
        if field == "registration_xy":
            transform = registration.transform
            expected = tuple(getattr(transform,k) for k in (
                "m11_m_per_pt","m12_m_per_pt","m21_m_per_pt","m22_m_per_pt","tx_m","ty_m"))
            if a.value != expected or a.reference_id != registration.fingerprint or a.basis != "displayed_bottom_pt_to_canonical_m":
                reasons.append("registration_mismatch")
            reasons.extend(_control_reasons(a, transform))
            for control in a.controls:
                x0,y0,x1,y1 = registration.source_bbox_pt
                if not (x0 <= control.source_pt[0] <= x1 and y0 <= control.source_pt[1] <= y1):
                    reasons.append("registration_control_outside_region")
                point = transform.apply(*control.source_pt)
                residual = math.hypot(point.x-control.canonical_xy_m[0], point.y-control.canonical_xy_m[1])
                if a.tolerance_m is None or residual > a.tolerance_m:
                    reasons.append("registration_control_residual")
        elif field == "level_elevation":
            if a.value != level.elevation_m or a.basis != "canonical_datum" or a.reference_id != canonical_datum_id:
                reasons.append("level_datum_mismatch")
        elif field == "anchor":
            if a.value != placement.anchor_pt or a.reference_id != registration.fingerprint or a.basis != "displayed_bottom_pt":
                reasons.append("anchor_mismatch")
            if a.role not in ("source_observed", "field_measured", "designed"):
                reasons.append("unsupported_anchor_role")
        elif field == "elevation":
            datum = chosen.get("level_elevation")
            expected = placement.height_above_level_m
            if a.basis == "above_level":
                good = a.reference_id == level.id and a.value == expected
            elif a.basis == "absolute_in_datum":
                good = datum is not None and a.reference_id == datum.reference_id and math.isclose(
                    a.value-level.elevation_m, expected, rel_tol=0, abs_tol=1e-9)
            else:
                good = False
            if not good:
                reasons.append("elevation_datum_or_value_mismatch")
            if a.role not in ("source_observed", "field_measured", "designed"):
                reasons.append("unsupported_physical_elevation_role")
        elif field == "yaw":
            if (a.value != placement.yaw_radians or a.reference_id != model.coordinate_system.frame_id
                    or a.basis != "canonical_product_axis"):
                reasons.append("orientation_basis_or_value_mismatch")
            if a.role == "project_defined":
                reasons.append("unsupported_physical_orientation_role")
        elif field == "entity_type":
            if (a.value != placement.entity_type or a.reference_id != placement.entity_kind
                    or a.basis != "canonical_type"):
                reasons.append("entity_type_mismatch")
    if registration.transform.z_m != level.elevation_m:
        reasons.append("registration_level_datum_mismatch")
    return sorted(set(reasons))


def _control_reasons(assertion, transform):
    """Check geometric independence in metres, using the caller's tolerance."""
    fits = [c for c in assertion.controls if not c.independent_check]
    checks = [c for c in assertion.controls if c.independent_check]
    reasons = []
    if assertion.role in ("source_observed", "field_measured") and (
            len(fits) < 2 or not checks or assertion.tolerance_m is None):
        reasons.append("missing:registration_controls")
    if not assertion.controls or assertion.tolerance_m is None:
        return reasons
    tolerance = assertion.tolerance_m
    def projected(c):
        p = transform.apply(*c.source_pt)
        return (p.x,p.y)
    def distance(a,b):
        return math.hypot(a[0]-b[0],a[1]-b[1])
    def same(a,b):
        return (distance(projected(a),projected(b)) <= tolerance or
                distance(a.canonical_xy_m,b.canonical_xy_m) <= tolerance)
    if any(same(a,b) for i,a in enumerate(fits) for b in fits[i+1:]):
        reasons.append("duplicate_registration_fit_control")
    if any(same(check,fit) for check in checks for fit in fits):
        reasons.append("reused_fit_control_as_independent_check")
    def off_line(point,a,b):
        length=distance(a,b)
        return length > tolerance and abs(
            (b[0]-a[0])*(point[1]-a[1])-(b[1]-a[1])*(point[0]-a[0]))/length > tolerance
    if assertion.role in ("source_observed", "field_measured") and checks and not all(
            any(off_line(projected(check),projected(a),projected(b)) and
                off_line(check.canonical_xy_m,a.canonical_xy_m,b.canonical_xy_m)
                for i,a in enumerate(fits) for b in fits[i+1:])
            for check in checks):
        reasons.append("degenerate:registration_controls")
    return reasons


def _validate_original_decisions(decisions, review):
    """Do not let a later safe deferral repair a malformed original intent graph."""
    rows={r["proposal_id"]:r for r in review.proposals}
    by_id={}
    groups={key:group for group in review.duplicate_candidates for key in group}
    for decision in decisions:
        if decision.proposal_id in by_id:
            raise ContractError("multiple decisions for one symbol")
        row=rows.get(decision.proposal_id)
        if row is None or decision.manifest_fingerprint != review.fingerprint or decision.proposal_fingerprint != digest(row):
            raise ContractError("stale symbol review decision")
        if decision.action == "accept" and set(row["unknowns"]) - (
                set(decision.resolved_unknowns) | set(decision.placement.remaining_unknowns)):
            raise ContractError("accepted symbol has unacknowledged unknowns")
        by_id[decision.proposal_id]=decision
    for decision in decisions:
        if decision.action == "duplicate":
            survivor=by_id.get(decision.duplicate_of)
            if (decision.duplicate_of not in groups.get(decision.proposal_id, ()) or
                    survivor is None or survivor.action != "accept"):
                raise ContractError("duplicate decision needs an accepted candidate survivor")


def materialize_asserted_symbols(model, review: SymbolReview, *, runs, documents,
        images, journal_paths, decisions: Sequence[SymbolDecision],
        registrations: Sequence[SymbolRegistration], assertion_review: PlacementAssertionReview,
        evidence_documents: Mapping[str, bytes]):
    """Validate explicit assertions, then use the existing source-verified intake.

    This is a trusted application input API, not authentication or proof that a
    reviewer/measurement is genuine. Applications must authorize their caller.
    Missing/conflicting physical fields defer. Broken bindings fail atomically.
    """
    if not isinstance(model, BuildingModel) or not isinstance(review, SymbolReview):
        raise ContractError("placement assertions require canonical model and typed source review")
    if not isinstance(assertion_review, PlacementAssertionReview):
        raise ContractError("typed placement assertion review required")
    _tuple(tuple(decisions), SymbolDecision, "symbol decisions")
    _tuple(tuple(registrations), SymbolRegistration, "symbol registrations")
    if not isinstance(evidence_documents, Mapping) or len(evidence_documents) > 256:
        raise ContractError("bounded evidence document mapping required")
    # Round-trip mutable nested input before using its fingerprints.
    assertion_review = PlacementAssertionReview.from_dict(assertion_review.to_dict())
    decisions = tuple(SymbolDecision.from_dict(d.to_dict()) for d in decisions)
    registrations = tuple(SymbolRegistration.from_dict(r.to_dict()) for r in registrations)
    verified = build_symbol_review(runs, documents=documents, images=images, journal_paths=journal_paths)
    if review.to_dict() != verified.to_dict():
        raise ContractError("stale symbol review for placement assertions")
    if (assertion_review.model_fingerprint != digest(model.to_dict())
            or assertion_review.symbol_review_fingerprint != verified.fingerprint
            or assertion_review.decisions_fingerprint != _records_digest(decisions)
            or assertion_review.registrations_fingerprint != _records_digest(registrations)):
        raise ContractError("stale placement assertion review binding")
    source_images = {}
    for run in runs:
        for image in run.request.images:
            if image.id in source_images and source_images[image.id] != image:
                raise ContractError("ambiguous source image identity")
            source_images[image.id] = image
    supplied = dict(documents)
    for key,value in evidence_documents.items():
        if key in supplied and supplied[key] != value:
            raise ContractError("conflicting evidence document bytes")
        supplied[key] = value
    by_id = {row["proposal_id"]: row for row in verified.proposals}
    for a in assertion_review.assertions:
        row = by_id.get(a.proposal_id)
        if row is None or a.proposal_fingerprint != digest(row):
            raise ContractError("stale per-device assertion association")
        _validate_evidence(a, supplied, source_images, row)
    _validate_original_decisions(decisions, verified)
    reg_map = {r.fingerprint:r for r in registrations}
    report = []
    gated = []
    for decision in decisions:
        # Existing API must still validate original decision fingerprints, including deferrals.
        row = by_id.get(decision.proposal_id)
        if row is None or decision.manifest_fingerprint != verified.fingerprint or decision.proposal_fingerprint != digest(row):
            raise ContractError("stale symbol review decision")
        facts = tuple(a for a in assertion_review.assertions if a.proposal_id == decision.proposal_id)
        reasons = _field_reasons(facts, decision, reg_map.get(
            decision.placement.registration_fingerprint), model, assertion_review.canonical_datum_id) if decision.action == "accept" else []
        report.append({"proposal_id": decision.proposal_id, "requested_action":decision.action,
                       "assertion_ids":[a.assertion_id for a in facts], "unresolved": reasons})
        gated.append(replace(decision, action="defer", placement=None,
                             rationale="Placement assertions unresolved: " + ", ".join(reasons))
                     if reasons else decision)
    blocked = {original.proposal_id for original, effective in zip(decisions, gated)
               if original.action == "accept" and effective.action == "defer"}
    for index, original in enumerate(decisions):
        if original.action == "duplicate" and original.duplicate_of in blocked:
            reason = "duplicate_survivor_deferred:" + original.duplicate_of
            report[index]["unresolved"].append(reason)
            gated[index] = replace(original, action="defer", duplicate_of=None,
                                   rationale="Placement assertions unresolved: " + reason)
    outcome = materialize_reviewed_symbols(model, verified, runs=runs, documents=documents,
        images=images, journal_paths=journal_paths, decisions=tuple(gated), registrations=registrations)
    envelope = assertion_review.to_dict()
    entry = {"review":envelope, "fingerprint":assertion_review.fingerprint, "results":report,
             "requested_decisions":[d.to_dict() for d in sorted(decisions,key=lambda d:d.proposal_id)]}
    attrs = copy.deepcopy(outcome.model.attributes)
    history = attrs.setdefault("placement_assertion_reviews", [])
    if not isinstance(history, list):
        raise ContractError("incompatible placement assertion history")
    if entry not in history:
        history.append(entry)
    entity_facts = {}
    for disposition in outcome.dispositions:
        if disposition["status"] == "accept" and disposition["canonical_id"] is not None:
            entity_facts[disposition["canonical_id"]] = tuple(a for a in assertion_review.assertions
                if a.proposal_id == disposition["proposal_id"])
    def enrich(entity):
        facts = entity_facts.get(entity.id)
        if facts is None:
            return entity
        extra = []
        scopes = {"registration_xy":("pose.position.x","pose.position.y"),
                  "level_elevation":("level_id","pose.position.z"), "anchor":("pose.position.x","pose.position.y"),
                  "elevation":("pose.position.z",), "yaw":("pose.rotation",),
                  "entity_type":("device_type",) if hasattr(entity,"device_type") else ("equipment_type",)}
        for a in facts:
            confidence = min(e.provenance.confidence for e in a.evidence)
            extra.append(Provenance(source_kind="placement-assertion-review",
                source_id=assertion_review.reviewer_id, source_element_id=a.assertion_id,
                derivation=assertion_review.reviewer_derivation, confidence=confidence,
                scope_paths=scopes[a.field], attributes={"role":a.role,
                    "assertion":a.to_dict(),"assertion_review_fingerprint":assertion_review.fingerprint}))
        return replace(entity, confidence=min(entity.confidence, *(p.confidence for p in extra)),
                       provenance=entity.provenance+tuple(extra),
                       attributes={**entity.attributes,"placement_assertion_review":{
                           "fingerprint":assertion_review.fingerprint,
                           "canonical_datum_id":assertion_review.canonical_datum_id,
                           "assertions":[a.to_dict() for a in facts]}})
    result = replace(outcome.model, attributes=attrs,
        electrical_devices=tuple(enrich(e) for e in outcome.model.electrical_devices),
        electrical_equipment=tuple(enrich(e) for e in outcome.model.electrical_equipment))
    return replace(outcome, model=result)
