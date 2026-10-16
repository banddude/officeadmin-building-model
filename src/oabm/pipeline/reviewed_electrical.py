"""Saved review -> canonical inventory and review/export artifacts, without delivery I/O."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from oabm.model import BuildingModel, ContractError
from oabm.model.model import _entity_collections
from oabm.proposals.contracts import text
from oabm.importers.pdf_electrical.symbol_stage import SymbolReview, build_symbol_review
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview, materialize_asserted_symbols
from oabm.quantities import extract_quantities
from oabm.reports.scope_ledger import ScopePolicy, generate_scope_ledger
from oabm.qa.confidence_review import ReviewConfig, build_confidence_review
from oabm.exports.gltf import build_glb_bytes
from oabm.exports.review_bundle import build_review_bundle, canonical_model_sha256

VERSION = "reviewed-electrical-delivery-v1"
KINDS = ("canonical_model", "quantities", "scope_ledger", "confidence_review",
         "source_map", "glb", "review_bundle")
STAGES = ("configuration", "source_review", "placement_intake", "scope",
          "quantities", "confidence_review", "glb", "review_bundle", "ifc")


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewedElectricalDelivery:
    model_json: str
    artifacts: Mapping[str, bytes]
    completion_json: str

    @property
    def model(self):
        return BuildingModel.from_json(self.model_json)

    @property
    def completion(self):
        return json.loads(self.completion_json)


class ReviewedElectricalDeliveryError(ContractError):
    """Original exception is retained as __cause__; no artifact payload is returned."""
    def __init__(self, stage, completion, cause):
        self.stage = stage
        self.completion_json = _json(completion)
        super().__init__(f"reviewed electrical delivery failed at {stage}: {cause}")

    @property
    def completion(self):
        return json.loads(self.completion_json)


def _groups(value, known, label):
    if not isinstance(value,Mapping):
        raise ContractError(label+" must be an explicit group mapping")
    result={}
    assigned=set()
    for group,ids in sorted(value.items()):
        text(group,label+" name")
        if not isinstance(ids,(tuple,list)) or any(not isinstance(i,str) for i in ids):
            raise ContractError(label+" members must be explicit ID sequences")
        if len(set(ids))!=len(ids) or assigned.intersection(ids):
            raise ContractError(label+" contains duplicate membership")
        if set(ids)-known:
            raise ContractError(label+" references unknown IDs")
        assigned.update(ids)
        result[group]=tuple(sorted(ids))
    return result


def deliver_reviewed_electrical(
    model, review, *, delivery_id, runs, documents, images, journal_paths,
    decisions, registrations, assertion_review, evidence_documents,
    scope_policy: ScopePolicy, proposal_groups, canonical_groups,
    review_config: ReviewConfig, source_references, artifact_references,
    glb_options, include_ifc, coverage_report,
):
    """Replay saved trusted inputs and return immutable in-memory payloads.

    No dispatch, inference, file writes, upload, routing or code calculation.
    Coverage has no accepted integration contract here yet; explicitly pass None.
    """
    report={"version":VERSION,"delivery_id":delivery_id if isinstance(delivery_id,str) else None,"delivery_ready":False,
        "stages":{s:{"status":"not_run"} for s in STAGES},
        "source_coverage":{"status":"unavailable","reason":"source_coverage_contract_not_integrated"},
        "artifacts":{}, "unresolved":[],
        "quantity_eligibility":{"basis":"canonical_inventory","base_bid":"not_established",
                                "source_complete":False},
        "physical_completeness":{"status":"not_verified"},
        "claims":{"complete_source_takeoff":False,"complete_buildable_model":False,
                  "code_compliance_verified":False,"production_live":False}}
    payloads={}
    stage="configuration"
    refs={}
    def put(kind,data):
        if not isinstance(data,bytes):
            raise ContractError("artifact must be immutable bytes")
        payloads[kind]=data
        report["artifacts"][kind]={"reference":refs[kind],"sha256":_sha(data),"bytes":len(data)}
    def passed():
        report["stages"][stage]={"status":"passed"}
    try:
        text(delivery_id,"delivery ID")
        if (not isinstance(model,BuildingModel) or not isinstance(review,SymbolReview)
                or not isinstance(assertion_review,PlacementAssertionReview)
                or not isinstance(scope_policy,ScopePolicy) or not isinstance(review_config,ReviewConfig)):
            raise ContractError("typed model, saved reviews, scope policy and review configuration required")
        runs,decisions,registrations=tuple(runs),tuple(decisions),tuple(registrations)
        if coverage_report is not None:
            raise ContractError("coverage contract not integrated; explicitly pass coverage_report=None")
        if not isinstance(include_ifc,bool) or not isinstance(glb_options,Mapping):
            raise ContractError("explicit IFC choice and GLB options required")
        expected=set(KINDS)|({"ifc"} if include_ifc else set())
        if not isinstance(artifact_references,Mapping) or set(artifact_references)!=expected:
            raise ContractError("artifact references must exactly cover requested outputs")
        refs=dict(artifact_references)
        for value in refs.values():text(value,"artifact reference",maximum=2048)
        if len(set(refs.values()))!=len(refs):
            raise ContractError("artifact references must be distinct")
        if include_ifc and (not isinstance(model.name,str) or not model.name.strip()):
            raise ContractError("IFC delivery requires an explicit canonical model name")
        if not isinstance(source_references,Mapping):
            raise ContractError("explicit source-reference registry required")
        source_registry={}
        for key,value in source_references.items():
            if not isinstance(key,tuple) or len(key)!=2:
                raise ContractError("source registry keys require kind and source ID")
            for token in key:text(token,"source registry identity")
            text(value,"source reference",maximum=2048)
            if value in refs.values():
                raise ContractError("source reference collides with delivery artifact")
            source_registry[key]=value
        base_ids={e.id for collection in _entity_collections(model) for e in collection}
        supplied_proposal_ids={p["proposal_id"] for p in review.proposals}
        pg=_groups(proposal_groups,supplied_proposal_ids,"proposal groups")
        cg=_groups(canonical_groups,base_ids,"canonical groups")
        report["requested_outputs"]=sorted(expected)
        report["input_bindings"]={
            "base_model_canonical_json_sha256":canonical_model_sha256(model),
            "symbol_review_fingerprint":review.fingerprint,
            "placement_assertion_review_fingerprint":assertion_review.fingerprint,
            "scope_policy_fingerprint":scope_policy.fingerprint,
            "review_config_sha256":_sha(review_config.to_json(indent=None).encode()),
            "glb_options_sha256":_sha(_json(dict(glb_options)).encode()),
            "source_reference_registry_sha256":_sha(_json([
                {"source_kind":k,"source_id":s,"reference":v} for (k,s),v in sorted(source_registry.items())]).encode()),
            "group_requests_sha256":_sha(_json({"proposals":pg,"canonical":cg}).encode())}
        report["hash_algorithms"]={"artifact_sha256":"SHA-256 of exact returned bytes",
            "canonical_json_sha256":"SHA-256 UTF-8 BuildingModel.to_json(indent=None)",
            "review_fingerprints":"existing typed review fingerprint algorithms"}
        passed()

        stage="source_review"
        verified=build_symbol_review(runs,documents=documents,images=images,journal_paths=journal_paths)
        if verified.to_dict()!=review.to_dict():
            raise ContractError("stale saved symbol review")
        report["saved_run_results"]=verified.to_dict()["coverage"]
        report["unresolved"].extend({"code":"saved_symbol_run_failed",
            "run_fingerprint":r["run_fingerprint"],"status":r["status"]}
            for r in verified.coverage if r["status"]!="completed")
        passed()
        report["stages"][stage]["saved_run_status_counts"]=dict(sorted(Counter(
            r["status"] for r in verified.coverage).items()))

        stage="placement_intake"
        outcome=materialize_asserted_symbols(model,verified,runs=runs,documents=documents,
            images=images,journal_paths=journal_paths,decisions=decisions,registrations=registrations,
            assertion_review=assertion_review,evidence_documents=evidence_documents)
        canonical=outcome.model
        model_json=canonical.to_json(indent=None)
        put("canonical_model",model_json.encode())
        report["canonical_output_sha256"]=canonical_model_sha256(canonical)
        report["dispositions"]=[dict(d) for d in outcome.dispositions]
        report["counts"]={"source_proposals":len(verified.proposals),
            "dispositions":dict(sorted(Counter(d["status"] for d in outcome.dispositions).items())),
            "canonical_devices":len(canonical.electrical_devices),
            "canonical_equipment":len(canonical.electrical_equipment),
            "new_canonical_electrical_ids":sorted({e.id for e in (*canonical.electrical_devices,*canonical.electrical_equipment)}-base_ids)}
        report["placement_assertion_results"]=canonical.attributes[
            "placement_assertion_reviews"][-1]["results"]
        report["unresolved"].extend({"code":"source_proposal_not_admitted","proposal_id":d["proposal_id"],
            "status":d["status"],"reason":d["reason"]} for d in outcome.dispositions if d["status"] in ("pending","defer"))
        electrical=(*canonical.electrical_devices,*canonical.electrical_equipment)
        report["physical_completeness"]={"status":"not_verified",
            "missing_dimensions":[e.id for e in electrical if e.size is None],
            "missing_host":[e.id for e in electrical if getattr(e,"host_id",None) is None],
            "connectivity":"not_evaluated","routing":"not_requested","engineering":"not_requested"}
        report["field_roles"]=[]
        for entity in electrical:
            for provenance in entity.provenance:
                if provenance.source_kind != "placement-assertion-review":
                    continue
                assertion=provenance.attributes.get("assertion")
                report["field_roles"].append({"canonical_id":entity.id,
                    "assertion_id":provenance.source_element_id,
                    "field":assertion.get("field") if isinstance(assertion,Mapping) else None,
                    "role":provenance.attributes.get("role","unknown"),
                    "derivation":provenance.derivation,"confidence":provenance.confidence})
        passed()

        stage="scope"
        ledger=generate_scope_ledger(canonical,policy=scope_policy)
        put("scope_ledger",ledger.to_json().encode())
        report["quantity_eligibility"]["scope_counts"]=dict(sorted(Counter(r.effective_status for r in ledger.rows).items()))
        report["unresolved"].extend({"code":"scope_unresolved","canonical_id":r.source.id,
            "reason":r.diagnostic_reason} for r in ledger.rows if r.effective_status=="unresolved")
        passed()

        stage="quantities"
        resolved={name:set(ids) for name,ids in cg.items()}
        by_proposal={d["proposal_id"]:d for d in outcome.dispositions}
        group_pending=[]
        for name,ids in pg.items():
            group=resolved.setdefault(name,set())
            for identity in ids:
                disposition=by_proposal[identity]
                if disposition["canonical_id"] is None:
                    group_pending.append({"group":name,"proposal_id":identity,"status":disposition["status"]})
                else:
                    group.add(disposition["canonical_id"])
        membership={}
        for name,ids in resolved.items():
            for identity in ids:
                if identity in membership and membership[identity]!=name:
                    raise ContractError("canonical object assigned conflicting quantity groups")
                membership[identity]=name
        takeoff=extract_quantities(canonical,groups={n:tuple(sorted(ids)) for n,ids in sorted(resolved.items())})
        if any(item.group is None for item in takeoff.items):
            raise ContractError("every quantity line requires an explicit inventory group")
        put("quantities",takeoff.to_json().encode())
        report["quantity_eligibility"].update({"status":"inventory_computed",
            "group_requests_without_canonical_objects":group_pending,
            "group_bindings":{"proposal_requests":pg,"canonical_requests":cg,
                "resolved":{n:sorted(ids) for n,ids in sorted(resolved.items())}},
            "unmatched_group_ids":takeoff.unmatched_group_ids})
        report["quantity_warnings"]=[w.to_dict() for w in takeoff.warnings]
        report["counts"]["quantity_lines"]=len(takeoff.items)
        passed()

        stage="confidence_review"
        confidence=build_confidence_review(canonical,review_config)
        put("confidence_review",confidence.to_json(indent=None).encode())
        # Produce the exact subset used by the existing bundle validator.
        entities={e.id:e for collection in _entity_collections(canonical) for e in collection}
        used={(p.source_kind,p.source_id) for item in confidence.items
              for identity in item.entity_ids for p in entities[identity].provenance}
        if used-set(source_registry):
            raise ContractError("missing caller source references for confidence review")
        selected={key:source_registry[key] for key in sorted(used)}
        source_rows=[{"source_kind":k,"source_id":s,"reference":v} for (k,s),v in sorted(selected.items())]
        put("source_map",_json(source_rows).encode())
        report["unused_source_reference_keys"]=[list(k) for k in sorted(set(source_registry)-used)]
        passed()

        stage="glb"
        glb=build_glb_bytes(canonical,**dict(glb_options))
        put("glb",glb)
        passed()

        stage="review_bundle"
        bundle=build_review_bundle(canonical,glb_bytes=glb,model_reference=refs["canonical_model"],
            glb_reference=refs["glb"],confidence_manifest=confidence,
            confidence_reference=refs["confidence_review"],
            source_reference_mapping_reference=refs["source_map"],
            source_references=selected,glb_options=glb_options)
        put("review_bundle",bundle.to_json().encode())
        passed()

        stage="ifc"
        if include_ifc:
            from oabm.ifc.adapter import to_ifc
            put("ifc",to_ifc(canonical).to_string().encode("utf-8"))
            passed()
        else:
            report["stages"]["ifc"]={"status":"not_requested"}
        report["unresolved"].append({"code":"source_coverage_unavailable"})
        report["delivery_ready"]=True
        return ReviewedElectricalDelivery(model_json,MappingProxyType(dict(payloads)),_json(report))
    except Exception as error:
        report["stages"][stage]={"status":"failed","error_type":type(error).__name__,"message":str(error)}
        report["failure_stage"]=stage
        report["delivery_ready"]=False
        raise ReviewedElectricalDeliveryError(stage,report,error) from error
