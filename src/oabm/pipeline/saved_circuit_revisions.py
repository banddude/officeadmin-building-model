"""Replay the existing reviewed circuit gate over retained, hash-bound inputs."""
from __future__ import annotations

from oabm.model import BuildingModel, ContractError
from oabm.exports.review_bundle import canonical_model_sha256
from .reviewed_circuit_design import ReviewedCircuitRequest, apply_reviewed_circuit_design
from .saved_electrical import _decode, _sha
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview, _materialize_asserted_verified
from oabm.importers.pdf_electrical.symbol_stage import SymbolDecision, SymbolRegistration
from oabm.proposals.symbols import SymbolRun

BASE = "circuit-inputs/base-model.json"
ADMITTED = "circuit-inputs/admitted-model.json"
REQUEST = "circuit-inputs/request.json"


def circuit_inputs(payloads):
    report = _decode(payloads["circuit-design.json"])
    request = ReviewedCircuitRequest.from_dict(report["request"])
    admitted = BuildingModel.from_json(payloads[ADMITTED].decode())
    assertion = PlacementAssertionReview.from_dict(admitted.attributes["placement_assertion_reviews"][-1]["review"])
    names = {BASE, ADMITTED, REQUEST, "circuit-inputs/decisions.json", "circuit-inputs/registrations.json"}
    cited = {e.document_sha256 for e in request.evidence}
    cited.update(e.document_sha256 for fact in assertion.assertions for e in fact.evidence)
    names.update("circuit-inputs/evidence/" + identity + ".bin" for identity in cited)
    return report, request, names


def verify_circuit_revision(model, report, review, assertion, payloads, receipts):
    design, request, names = circuit_inputs(payloads)
    def require(condition, reason):
        if not condition:
            raise ContractError(reason)
    require(names <= payloads.keys(), "missing retained circuit validation inputs")
    for label, name in (("model", BASE), ("circuit_design", REQUEST),
                        ("decisions", "circuit-inputs/decisions.json"),
                        ("registrations", "circuit-inputs/registrations.json")):
        row = receipts.get(label)
        require(row is not None and row["sha256"] == _sha(payloads[name])
                and row["bytes"] == len(payloads[name]), "circuit input/receipt binding mismatch")
    require(ReviewedCircuitRequest.from_dict(_decode(payloads[REQUEST])).to_dict() == request.to_dict(),
            "retained circuit request contradicts design artifact")
    base = BuildingModel.from_json(payloads[BASE].decode())
    admitted = BuildingModel.from_json(payloads[ADMITTED].decode())
    require(canonical_model_sha256(base) == report["input_bindings"]["base_model_canonical_json_sha256"],
            "circuit base snapshot contradicts report")
    require(request.model_fingerprint == assertion.model_fingerprint,
            "circuit request contradicts placement base")
    require(design == report["circuit_design"] and request.fingerprint == report["input_bindings"]["circuit_design_fingerprint"],
            "circuit report/request binding mismatch")
    require(_sha(payloads[ADMITTED]) == design["admitted_model_sha256"], "admitted circuit snapshot binding mismatch")
    require(base.model_id == admitted.model_id == model.model_id
            and base.coordinate_system == admitted.coordinate_system == model.coordinate_system
            and base.schema_version == admitted.schema_version == model.schema_version,
            "circuit snapshots differ in identity/frame/schema")
    for key in ("ai_symbol_stage_runs", "placement_assertion_reviews"):
        require(admitted.attributes[key] == model.attributes[key], "circuit placement history mismatch")
    evidence = {}
    cited = {e.document_sha256 for e in request.evidence}
    cited.update(e.document_sha256 for fact in assertion.assertions for e in fact.evidence)
    for identity in sorted(cited):
        data = payloads["circuit-inputs/evidence/" + identity + ".bin"]
        available = [receipts[key] for key in ("documents/" + identity, "evidence_documents/" + identity) if key in receipts]
        require(bool(available) and all(row["sha256"] == identity == _sha(data)
                and row["bytes"] == len(data) for row in available), "circuit evidence/receipt binding mismatch")
        evidence[identity] = data
    decisions = tuple(SymbolDecision.from_dict(row) for row in _decode(payloads["circuit-inputs/decisions.json"]))
    registrations = tuple(SymbolRegistration.from_dict(row) for row in _decode(payloads["circuit-inputs/registrations.json"]))
    source_images = {}
    for item in review.coverage:
        for image in SymbolRun.from_dict(item["run"]).request.images:
            require(image.id not in source_images or source_images[image.id] == image,
                    "contradictory retained circuit source image")
            source_images[image.id] = image
    placement = _materialize_asserted_verified(base, review, decisions=decisions, registrations=registrations,
        assertion_review=assertion, supplied=evidence, source_images=source_images)
    require(placement.model.to_json(indent=None) == admitted.to_json(indent=None)
            and list(placement.dispositions) == report["dispositions"], "retained placement replay differs from admitted model")
    expected, expected_report = apply_reviewed_circuit_design(admitted, request=request, base_model=base,
        review=review, assertion_review=assertion, dispositions=report["dispositions"], evidence_documents=evidence)
    expected_report.update(request=request.to_dict(), admitted_model_sha256=_sha(payloads[ADMITTED]))
    require(expected.to_json(indent=None) == model.to_json(indent=None), "circuit replay differs from canonical graph")
    require(expected_report == design, "circuit replay differs from design report")
    require(report["physical_completeness"]["routing"] == design["status"]
            and report["physical_completeness"]["engineering"] == "not_performed",
            "circuit physical limits contradict replay")
    groups = report["quantity_eligibility"]["group_bindings"]["resolved"]
    require(request.inventory_group in groups and set(design["quantity_entity_ids"]) <= set(groups[request.inventory_group]),
            "circuit inventory group contradicts request")
    require({"code":"preliminary_design_engineering_capacity_and_supports_unverified"} in report["unresolved"]
            and all({"code":reason,"request_id":request.request_id} in report["unresolved"] for reason in design["reasons"]),
            "circuit unresolved limits contradict replay")
    return {"request_fingerprint":request.fingerprint, "request":request.to_dict(),
            "status":design["status"], "reasons":design["reasons"],
            "admitted_model_sha256":design["admitted_model_sha256"],
            "created_ids":design["created_ids"], "quantity_entity_ids":design["quantity_entity_ids"],
            "verification":"existing placement assertion and circuit gate replay; source pixels not revalidated",
            "source_observed":False, "buildable":False, "code_compliance_verified":False}
