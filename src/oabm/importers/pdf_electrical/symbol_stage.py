"""Explicit review and registered canonical intake for AI electrical symbols.

Vision transport stays in proposals.symbols. Saved manifests are review aids,
not attestations: intake rebuilds them from source-verified adapter runs.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from oabm.model import (BuildingModel, ContractError, ElectricalDevice,
                        ElectricalEquipment, Point3, Pose, Provenance, Quaternion,
                        stable_id)
from oabm.model.common import _finite, _validate_id
from oabm.model.model import _entity_collections
from oabm.proposals.contracts import Portable, _portable, digest, fingerprint, text
from .importer import PdfPageTransform

VERSION = "1.0.0"


def _strings(values, label):
    if not isinstance(values, tuple) or any(not isinstance(v, str) for v in values):
        raise ContractError(f"{label} requires a tuple of strings")
    if len(set(values)) != len(values):
        raise ContractError(f"{label} requires distinct strings")
    for value in values:
        text(value, label, maximum=1024)


def _point(value, label):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ContractError(f"{label} requires two displayed PDF point coordinates")
    return tuple(_finite(v, label) for v in value)


def _box(value):
    if not isinstance(value, tuple) or len(value) != 4:
        raise ContractError("registration bounds require four point coordinates")
    box = tuple(_finite(v, "registration bounds") for v in value)
    if not (box[0] < box[2] and box[1] < box[3]):
        raise ContractError("registration bounds must be ordered and positive")
    return box


def _inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRegistration(Portable):
    """Caller-reviewed drawing region in the live canonical frame.

    The affine maps displayed bottom-origin points to metres. Its z_m is the
    level datum, not an inferred device mounting height.
    """
    source_id: str
    document_sha256: str
    page: int
    source_bbox_pt: tuple[float, float, float, float]
    level_id: str
    transform: PdfPageTransform
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        text(self.source_id, "registration source")
        fingerprint(self.document_sha256, "registration PDF digest")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ContractError("registration page must be 1-based")
        _validate_id(self.level_id, "registration level")
        object.__setattr__(self, "source_bbox_pt", _box(self.source_bbox_pt))
        if not isinstance(self.transform, PdfPageTransform):
            raise ContractError("registration needs a PdfPageTransform")
        object.__setattr__(self, "transform", replace(self.transform, **{
            key: float(getattr(self.transform, key)) for key in (
                "m11_m_per_pt", "m12_m_per_pt", "m21_m_per_pt", "m22_m_per_pt",
                "tx_m", "ty_m", "z_m")}))
        if not isinstance(self.provenance, tuple) or not self.provenance:
            raise ContractError("registration requires supplied evidence")
        for record in self.provenance:
            if not isinstance(record, Provenance) or record.derivation is None:
                raise ContractError("registration evidence needs explicit derivation")

    @property
    def fingerprint(self):
        return digest(_portable(self))


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolPlacement(Portable):
    """Reviewed corrections and explicit physical placement; no defaults."""
    registration_fingerprint: str
    entity_kind: str
    entity_type: str
    anchor_pt: tuple[float, float]
    height_above_level_m: float
    yaw_radians: float
    remaining_unknowns: tuple[str, ...]

    def __post_init__(self):
        fingerprint(self.registration_fingerprint, "placement registration")
        if self.entity_kind not in ("device", "equipment"):
            raise ContractError("symbol entity kind must be device or equipment")
        text(self.entity_type, "reviewed canonical type")
        if self.entity_type.lower() in ("unknown", "unsure", "unclassified"):
            raise ContractError("unknown canonical type cannot be materialized")
        object.__setattr__(self, "anchor_pt", _point(self.anchor_pt, "reviewed anchor"))
        height = _finite(self.height_above_level_m, "mounting height above level")
        if height < 0:
            raise ContractError("mounting height above level cannot be negative")
        object.__setattr__(self, "height_above_level_m", height)
        object.__setattr__(self, "yaw_radians", _finite(self.yaw_radians, "global yaw radians"))
        _strings(self.remaining_unknowns, "remaining unknowns")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolDecision(Portable):
    """Caller review record, never accepted from an AI adapter response."""
    manifest_fingerprint: str
    proposal_id: str
    proposal_fingerprint: str
    reviewer_id: str
    reviewer_derivation: str
    rationale: str
    action: str
    resolved_unknowns: tuple[str, ...] = ()
    placement: SymbolPlacement | None = None
    duplicate_of: str | None = None
    overlap_resolution: str | None = None

    def __post_init__(self):
        fingerprint(self.manifest_fingerprint, "review manifest")
        fingerprint(self.proposal_fingerprint, "review proposal")
        text(self.proposal_id, "review proposal ID")
        text(self.reviewer_id, "reviewer ID")
        if self.reviewer_derivation not in ("user", "inferred"):
            raise ContractError("review derivation must be user or inferred")
        text(self.rationale, "review rationale", maximum=4096)
        if self.action not in ("accept", "reject", "defer", "duplicate"):
            raise ContractError("unsupported symbol review action")
        _strings(self.resolved_unknowns, "resolved unknowns")
        if (self.action == "accept") != isinstance(self.placement, SymbolPlacement):
            raise ContractError("only accepted symbols require a reviewed placement")
        if (self.action == "duplicate") != (self.duplicate_of is not None):
            raise ContractError("only duplicate decisions require a survivor proposal ID")
        if self.duplicate_of is not None:
            text(self.duplicate_of, "duplicate survivor")
            if self.duplicate_of == self.proposal_id:
                raise ContractError("symbol cannot duplicate itself")
        if self.overlap_resolution is not None:
            text(self.overlap_resolution, "overlap review rationale", maximum=4096)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolReview(Portable):
    version: str
    proposals: tuple[dict[str, Any], ...]
    coverage: tuple[dict[str, Any], ...]
    duplicate_candidates: tuple[tuple[str, ...], ...]

    def __post_init__(self):
        if self.version != VERSION:
            raise ContractError("unsupported symbol review version")
        if not isinstance(self.proposals, tuple) or any(not isinstance(r, dict) for r in self.proposals):
            raise ContractError("symbol review requires proposal rows")
        ids = [r.get("proposal_id") for r in self.proposals]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise ContractError("symbol review proposal IDs must be unique")
        if not isinstance(self.coverage, tuple) or any(not isinstance(r, dict) for r in self.coverage):
            raise ContractError("symbol review requires coverage records")
        if not isinstance(self.duplicate_candidates, tuple):
            raise ContractError("duplicate candidates require a tuple of groups")
        for group in self.duplicate_candidates:
            if not isinstance(group, tuple) or len(group) < 2 or len(set(group)) != len(group):
                raise ContractError("duplicate candidates need distinct proposal IDs")
            if set(group) - set(ids):
                raise ContractError("duplicate candidates reference unknown proposals")

    @property
    def fingerprint(self):
        return digest(self.to_dict())


def _duplicate_groups(rows):
    # Intersections are review candidates, not equality or a suppression rule.
    # Deliberately independent of proposed type: conflicting labels still meet.
    parent = {r["proposal_id"]: r["proposal_id"] for r in rows}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for index, a in enumerate(rows):
        for b in rows[index + 1:]:
            if (a["source_id"], a["document_sha256"]) != (b["source_id"], b["document_sha256"]):
                continue
            x, y = a["bounds_pt"], b["bounds_pt"]
            overlap = a["page"] == b["page"] and (
                max(x[0], y[0]) < min(x[2], y[2]) and max(x[1], y[1]) < min(x[3], y[3]))
            # An instance label is not a repeated fixture/type tag. Even an
            # exact label match across views is only a question for review.
            a_label, b_label = a.get("instance_label"), b.get("instance_label")
            shared_label = (a["page"] != b["page"] and isinstance(a_label, str)
                and isinstance(b_label, str) and bool(a_label.strip())
                and a_label.strip().casefold() == b_label.strip().casefold())
            if overlap or shared_label:
                parent[root(b["proposal_id"])] = root(a["proposal_id"])
    groups = {}
    for key in parent:
        groups.setdefault(root(key), []).append(key)
    return tuple(sorted(tuple(sorted(v)) for v in groups.values() if len(v) > 1))


def _review_from_rows(rows, coverage):
    rows = tuple(sorted(copy.deepcopy(tuple(rows)), key=lambda r: r["proposal_id"]))
    return SymbolReview(version=VERSION, proposals=rows,
                        coverage=tuple(sorted(copy.deepcopy(tuple(coverage)), key=digest)),
                        duplicate_candidates=_duplicate_groups(rows))


def _batch_inputs(requests, images, documents):
    references = {}
    for request in requests:
        for image in request.images:
            if image.id in references and references[image.id] != image:
                raise ContractError("batch image ID identifies conflicting source references")
            references[image.id] = image
    if set(images) != set(references):
        raise ContractError("batch image bytes must exactly cover all requested context")
    expected_documents = {image.source.document_sha256 for image in references.values()}
    if set(documents) != expected_documents:
        raise ContractError("batch PDF bytes must exactly cover all requested sources")


def _request_inputs(request, images, documents):
    # Select the complete declared context for this request from the batch union.
    # Other requests' target tiles are not additional undeclared model context.
    return ({image.id: images[image.id] for image in request.images},
            {image.source.document_sha256: documents[image.source.document_sha256]
             for image in request.images})


def build_symbol_review(runs: Sequence, *, documents: Mapping[str, bytes],
                        images: Mapping[str, bytes],
                        journal_paths: Mapping[str, str | Path]) -> SymbolReview:
    """Reverify original PDFs/pixels via the adapter; never trust detached rows.

    This is the AI-first intake entrypoint. It does not call the legacy shape
    matcher or infer missing categories, schedule counts or spatial facts.
    """
    from oabm.proposals.symbols import SymbolRun

    if not runs:
        raise ContractError("symbol stage requires at least one explicit run")
    if not isinstance(journal_paths, Mapping):
        raise ContractError("symbol review requires caller-selected journal paths")
    if any(not isinstance(run, SymbolRun) for run in runs):
        raise ContractError("symbol stage requires typed SymbolRun inputs")
    _batch_inputs([run.request for run in runs], images, documents)
    expected = {run.request.fingerprint for run in runs}
    if set(journal_paths) != expected:
        raise ContractError("journal paths must exactly cover the source requests")
    rows, coverage = [], []
    for run in runs:
        if not isinstance(run, SymbolRun):
            raise ContractError("symbol stage requires typed SymbolRun inputs")
        run = SymbolRun.from_dict(run.to_dict())
        request_images, request_documents = _request_inputs(run.request, images, documents)
        proposals = run.proposals(documents=request_documents, images=request_images,
                                  journal_path=journal_paths[run.request.fingerprint])
        rows.extend(p.to_dict() for p in proposals)
        # Retain the complete run request/output/attempt envelope for review;
        # no provider identity or empty-result completeness is inferred here.
        coverage.append({"run_fingerprint": run.fingerprint, "status": run.status,
                         "run": run.to_dict()})
    return _review_from_rows(rows, coverage)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolExtraction:
    runs: tuple[Any, ...]
    review: SymbolReview


def extract_symbol_review(requests: Sequence, policy, *, adapters, documents,
                          images, journals, qualification=None) -> SymbolExtraction:
    """Dispatch the primary AI interpretation stage and return its review handoff.

    Endpoint selection, escalation and append-only attempt receipts belong to
    the symbol adapter. No deterministic recognition is required to run this
    stage. Dispatch exceptions propagate; failed run statuses and their receipts
    remain explicit in the review coverage and per-request journals.
    """
    from oabm.proposals.symbols import AttemptJournal, SymbolRequest, dispatch_symbols

    if not requests:
        raise ContractError("symbol stage requires explicit source regions")
    checked = []
    for request in requests:
        if not isinstance(request, SymbolRequest):
            raise ContractError("symbol stage requires typed SymbolRequest inputs")
        checked.append(SymbolRequest.from_dict(request.to_dict()))
    if len({r.fingerprint for r in checked}) != len(checked):
        raise ContractError("duplicate symbol requests")
    _batch_inputs(checked, images, documents)
    expected = {request.fingerprint for request in checked}
    if not isinstance(journals, Mapping) or set(journals) != expected:
        raise ContractError("journals must exactly cover the source requests")
    if any(not isinstance(journal, AttemptJournal) for journal in journals.values()):
        raise ContractError("symbol extraction requires typed attempt journals")
    journal_paths = {key: journal.path for key, journal in journals.items()}
    if len({Path(path).resolve() for path in journal_paths.values()}) != len(checked):
        raise ContractError("each source request requires its own attempt journal")
    # Check every journal before any provider call. Existing receipt recovery
    # belongs to saved-run verification, never an implicit fresh dispatch.
    if any(journal.receipts or AttemptJournal.read(journal.path) for journal in journals.values()):
        raise ContractError("fresh symbol extraction requires empty attempt journals")
    runs = []
    for request in checked:
        request_images, request_documents = _request_inputs(request, images, documents)
        runs.append(dispatch_symbols(request, policy, adapters=adapters,
            images=request_images, documents=request_documents,
            journal=journals[request.fingerprint], qualification=qualification))
    runs = tuple(runs)
    return SymbolExtraction(runs=runs,
        review=build_symbol_review(runs, documents=documents, images=images,
                                   journal_paths=journal_paths))


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolStageOutcome:
    model: BuildingModel
    review: SymbolReview
    dispositions: tuple[dict[str, Any], ...]


def materialize_reviewed_symbols(
    model: BuildingModel, review: SymbolReview, *, runs: Sequence,
    documents: Mapping[str, bytes], images: Mapping[str, bytes],
    journal_paths: Mapping[str, str | Path],
    decisions: Sequence[SymbolDecision], registrations: Sequence[SymbolRegistration],
) -> SymbolStageOutcome:
    """Return a new validated canonical model; all unreviewed rows stay pending.

    The caller supplies review records through this separate trusted API. This
    is not authentication of a human identity. Applications must authorize the
    reviewer before constructing these records; model output cannot approve it.
    """
    if not isinstance(model, BuildingModel):
        raise ContractError("symbol intake requires a canonical BuildingModel")
    model = BuildingModel.from_dict(model.to_dict())
    verified = build_symbol_review(runs, documents=documents, images=images, journal_paths=journal_paths)
    if not isinstance(review, SymbolReview) or review.to_dict() != verified.to_dict():
        raise ContractError("stale or modified symbol review; rebuild from verified sources")
    return _materialize_verified(model, verified, decisions, registrations)


def _materialize_verified(model, review, decisions, registrations):
    # Private core: public entrypoint above always rebuilds the source boundary.
    by_id = {r["proposal_id"]: r for r in review.proposals}
    decision_map = {}
    for record in decisions:
        if not isinstance(record, SymbolDecision):
            raise ContractError("symbol decisions must be typed review records")
        record = SymbolDecision.from_dict(record.to_dict())
        if record.proposal_id in decision_map:
            raise ContractError("multiple decisions for one symbol")
        row = by_id.get(record.proposal_id)
        if row is None or record.manifest_fingerprint != review.fingerprint or record.proposal_fingerprint != digest(row):
            raise ContractError("stale symbol review decision")
        if record.action == "accept":
            acknowledged = set(record.resolved_unknowns) | set(record.placement.remaining_unknowns)
            if set(row["unknowns"]) - acknowledged:
                raise ContractError("accepted symbol has unacknowledged unknowns")
        decision_map[record.proposal_id] = record
    registration_map = {}
    for registration in registrations:
        if not isinstance(registration, SymbolRegistration):
            raise ContractError("symbol registrations must be typed")
        registration = SymbolRegistration.from_dict(registration.to_dict())
        if registration.fingerprint in registration_map:
            raise ContractError("duplicate symbol registration")
        registration_map[registration.fingerprint] = registration
    group_map = {key: group for group in review.duplicate_candidates for key in group}
    for key, decision in decision_map.items():
        if decision.action == "duplicate":
            survivor = decision_map.get(decision.duplicate_of)
            if (decision.duplicate_of not in group_map.get(key, ()) or survivor is None
                    or survivor.action != "accept"):
                raise ContractError("duplicate decision needs an accepted candidate survivor")
    devices, equipment, dispositions = [], [], []
    for key, row in sorted(by_id.items()):
        decision = decision_map.get(key)
        status, reason, entity_id = "pending", "independent_review_required", None
        if decision is not None:
            status, reason = decision.action, decision.rationale
        if decision is not None and decision.action == "accept":
            group = group_map.get(key, ())
            resolved = [decision_map.get(member) for member in group]
            if any(d is None or d.action == "defer" for d in resolved):
                status, reason = "pending", "duplicate_candidates_require_review"
            elif sum(d.action == "accept" for d in resolved) > 1 and any(
                    not d.overlap_resolution for d in resolved if d.action == "accept"):
                status, reason = "pending", "distinct_candidate_symbols_need_rationale"
            else:
                placement = decision.placement
                registration = registration_map.get(placement.registration_fingerprint)
                if registration is None:
                    status, reason = "pending", "reviewed_registration_missing"
                else:
                    entity = _entity(model, row, decision, registration)
                    entity_id = entity.id
                    (devices if isinstance(entity, ElectricalDevice) else equipment).append(entity)
        dispositions.append({"proposal_id": key, "status": status, "reason": reason,
                             "canonical_id": entity_id})
    disposition_map = {row["proposal_id"]: row for row in dispositions}
    for key, decision in decision_map.items():
        if decision.action == "duplicate":
            survivor = disposition_map[decision.duplicate_of]
            if survivor["status"] == "accept" and survivor["canonical_id"] is not None:
                disposition_map[key]["canonical_id"] = survivor["canonical_id"]
            else:
                disposition_map[key].update(status="pending", reason="duplicate_survivor_not_materialized")
    existing = {e.id: e for c in _entity_collections(model) for e in c}
    new_ids = set()
    for entity in devices + equipment:
        if entity.id in new_ids:
            raise ContractError("reviewed symbols collide; resolve duplicate source marks")
        new_ids.add(entity.id)
        if entity.id in existing and existing[entity.id] != entity:
            raise ContractError("symbol canonical ID already has different content; use explicit model editing")
    history = copy.deepcopy(model.attributes.get("ai_symbol_stage_runs", []))
    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
        raise ContractError("existing symbol stage history has incompatible content")
    entry = {"manifest": review.to_dict(),
             "decisions": [d.to_dict() for _, d in sorted(decision_map.items())],
             "registrations": [r.to_dict() for _, r in sorted(registration_map.items())],
             "dispositions": copy.deepcopy(dispositions)}
    if entry not in history:
        history.append(entry)
    attributes = copy.deepcopy(model.attributes)
    attributes["ai_symbol_stage_runs"] = history
    result = replace(model,
        electrical_devices=model.electrical_devices + tuple(e for e in devices if e.id not in existing),
        electrical_equipment=model.electrical_equipment + tuple(e for e in equipment if e.id not in existing),
        attributes=attributes)
    return SymbolStageOutcome(model=result, review=review, dispositions=tuple(dispositions))


def _entity(model, row, decision, registration):
    if (registration.source_id, registration.document_sha256, registration.page) != (
            row["source_id"], row["document_sha256"], row["page"]):
        raise ContractError("symbol registration source/page mismatch")
    if registration.transform.frame_id != model.coordinate_system.frame_id:
        raise ContractError("symbol registration canonical frame mismatch")
    levels = {level.id: level for level in model.levels}
    level = levels.get(registration.level_id)
    if level is None:
        raise ContractError("symbol registration references missing level")
    if registration.transform.z_m != level.elevation_m:
        raise ContractError("registration z datum differs from canonical level")
    placement = decision.placement
    if not _inside(placement.anchor_pt, row["bounds_pt"]):
        raise ContractError("reviewed anchor must lie in the source mark bounds")
    if not _inside(placement.anchor_pt, registration.source_bbox_pt):
        raise ContractError("reviewed anchor outside registered drawing region")
    xy = registration.transform.apply(*placement.anchor_pt)
    pose = Pose(position=Point3(x=xy.x, y=xy.y,
                               z=level.elevation_m + placement.height_above_level_m),
                rotation=Quaternion(z=math.sin(placement.yaw_radians / 2),
                                    w=math.cos(placement.yaw_radians / 2)))
    source_records = []
    for record in row["provenance"]:
        fields = copy.deepcopy(record)
        derivation = fields.pop("derivation")  # Preserve the supplied class; never guess one.
        fields["scope_paths"] = (tuple(fields["scope_paths"])
                                 if fields.get("scope_paths") is not None else None)
        fields["attributes"] = {**fields.get("attributes", {}),
                                "source_document_sha256": row["document_sha256"]}
        source_records.append(Provenance(derivation=derivation, **fields))
    source_provenance = tuple(source_records)
    review_provenance = Provenance(source_kind="symbol-review", source_id=decision.reviewer_id,
        source_element_id=row["source_mark_id"], page=row["page"], method="explicit-symbol-review-v1",
        confidence=row["confidence"], derivation=decision.reviewer_derivation,
        attributes={"decision": decision.to_dict(), "registration": registration.to_dict()})
    attributes = {"symbol_review": {"proposal": copy.deepcopy(row),
        "decision": decision.to_dict(), "registration_fingerprint": registration.fingerprint,
        "remaining_unknowns": list(placement.remaining_unknowns),
        "physical_geometry_status": "anchor_only_dimensions_unknown",
        "hosting_status": "unresolved", "connectivity_status": "unresolved"}}
    # Preserve source assertions independently of type/placement review. A type
    # correction or resolved_unknowns entry cannot resolve contradictory scope.
    if row.get("scope") is not None:
        from oabm.proposals.symbol_scope import SymbolScope
        scope = SymbolScope.from_dict(row["scope"])
        attributes["pdf_electrical"] = scope.canonical_metadata()
        sources = {image["id"]: image for image in row["scope_sources"]}
        scope_records = []
        for claim in scope.claims:
            for evidence in claim.evidence:
                image = sources[evidence.image_id]
                source = image["source"]
                scope_records.append(Provenance(
                    source_kind="ai-proposed", source_id=source["source_id"],
                    page=source["page"], source_element_id=row["source_mark_id"],
                    method="ai-symbol-source-scope-v1", derivation="inferred",
                    confidence=min(row["confidence"], claim.confidence),
                    scope_paths=("attributes.pdf_electrical",),
                    attributes={"document_sha256": source["document_sha256"],
                        "image_sha256": image["sha256"], "image_id": image["id"],
                        "claim": claim.to_dict(), "evidence": evidence.to_dict(),
                        "request_fingerprint": row["request_fingerprint"],
                        "result_fingerprint": row["result_fingerprint"]}))
        source_provenance += tuple(scope_records)
    cls = ElectricalDevice if placement.entity_kind == "device" else ElectricalEquipment
    type_key = "device_type" if placement.entity_kind == "device" else "equipment_type"
    # Identity follows the native source-mark identity, never corrected pose/type.
    canonical_id = stable_id("reviewed-symbol", digest({"source_id": row["source_id"],
        "document_sha256": row["document_sha256"], "page": row["page"],
        "source_mark_id": row["source_mark_id"]}))
    return cls(id=canonical_id, level_id=level.id, pose=pose,
               confidence=min(row["confidence"], level.confidence,
                   *(p.confidence for p in registration.provenance),
                   *(p.confidence for p in level.provenance)),
               provenance=source_provenance + registration.provenance + (review_provenance,),
               attributes=attributes, **{type_key: placement.entity_type})
