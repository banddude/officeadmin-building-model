"""Explicit preliminary graph design over admitted placements; no physical defaults."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import math
from typing import Any

from oabm.model import BuildingModel, ContractError, Port, Pose, Point3, Vector3, Provenance, RuleReference
from oabm.proposals.contracts import Portable, digest, fingerprint, text
from oabm.importers.pdf_electrical.placement_assertions import PlacementEvidence
from oabm.routing import CircuitDesignRules, RoutingOptions, NoRouteError, design_proposed_circuits

VERSION = "reviewed-circuit-request-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedLoadPort(Portable):
    id: str
    proposal_id: str
    offset_canonical_m: tuple[float, float, float]
    direction: tuple[float, float, float]

    def __post_init__(self):
        text(self.id, "designed port identity")
        text(self.proposal_id, "selected proposal identity")
        for name in ("offset_canonical_m", "direction"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) != 3 or any(
                    isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in values):
                raise ContractError("explicit finite port offset and direction required")
            object.__setattr__(self, name, tuple(float(v) for v in values))
        if not math.isfinite(math.hypot(*self.direction)) or math.hypot(*self.direction) == 0:
            raise ContractError("port direction cannot be zero")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedCircuitRequest(Portable):
    version: str
    request_id: str
    model_fingerprint: str
    symbol_review_fingerprint: str
    assertion_review_fingerprint: str
    equipment_id: str | None
    source_port_id: str | None
    groups: tuple[tuple[str, ...], ...] | None
    load_ports: tuple[ReviewedLoadPort, ...]
    rules: dict[str, Any] | None
    routing_controls: dict[str, Any] | None
    rule_references: tuple[RuleReference, ...]
    evidence: tuple[PlacementEvidence, ...]
    reviewer_id: str
    rationale: str
    inventory_group: str

    def __post_init__(self):
        if self.version != VERSION:raise ContractError("unsupported reviewed circuit request")
        for name in ("request_id","reviewer_id","rationale","inventory_group"):text(getattr(self,name), name)
        for name in ("model_fingerprint","symbol_review_fingerprint","assertion_review_fingerprint"):
            fingerprint(getattr(self,name), name)
        for value in (self.equipment_id,self.source_port_id):
            if value is not None:text(value,"explicit source identity")
        if not isinstance(self.load_ports,tuple) or any(not isinstance(p,ReviewedLoadPort) for p in self.load_ports):
            raise ContractError("typed explicit load ports required")
        if len({p.id for p in self.load_ports}) != len(self.load_ports) or len({p.proposal_id for p in self.load_ports}) != len(self.load_ports):
            raise ContractError("duplicate load port or proposal binding")
        if self.groups is not None:
            if not isinstance(self.groups,tuple) or not self.groups or any(not isinstance(g,tuple) or not g for g in self.groups):
                raise ContractError("nonempty explicit circuit groups required")
            flat = [p for g in self.groups for p in g]
            for p in flat:text(p,"circuit group proposal")
            if len(flat) != len(set(flat)):raise ContractError("duplicate circuit group membership")
        for values,cls in ((self.rule_references,RuleReference),(self.evidence,PlacementEvidence)):
            if not isinstance(values,tuple) or any(not isinstance(v,cls) for v in values):raise ContractError("typed design evidence required")

    @property
    def fingerprint(self):return digest(self.to_dict())

    @classmethod
    def capture(cls, model, review, assertion_review, **values):
        return cls(version=VERSION,model_fingerprint=digest(model.to_dict()),
            symbol_review_fingerprint=review.fingerprint,assertion_review_fingerprint=assertion_review.fingerprint,**values)


def _profile(values, cls, label):
    required = {f.name for f in fields(cls)}
    if values is None:return None,["missing_"+label]
    if not isinstance(values,dict) or set(values)-required:raise ContractError("unsupported "+label+" fields")
    missing = sorted(required-set(values))
    if missing:return None,["missing_"+label+"_field:"+n for n in missing]
    for name,value in values.items():
        if cls is CircuitDesignRules and name in ("route_type","conductor_material","conductor_size"):
            text(value,name)
        elif cls is CircuitDesignRules and name == "include_ground":
            if not isinstance(value,bool):raise ContractError("include_ground must be boolean")
        elif name in ("max_devices_per_circuit","assumed_poles","insulated_conductor_count","coordinate_precision","max_bends"):
            if name == "max_bends" and value is None:continue
            if isinstance(value,bool) or not isinstance(value,int):raise ContractError(name+" must be integer")
        elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ContractError(name+" must be finite numeric input")
    return cls(**values),[]


def apply_reviewed_circuit_design(model, *, request, base_model, review, assertion_review, dispositions, evidence_documents):
    """All-or-nothing for one explicit design request; missing facts defer it."""
    request = ReviewedCircuitRequest.from_dict(request.to_dict())
    if (request.model_fingerprint != digest(base_model.to_dict()) or request.symbol_review_fingerprint != review.fingerprint
            or request.assertion_review_fingerprint != assertion_review.fingerprint):
        raise ContractError("stale reviewed circuit request binding")
    report = {"version":VERSION,"request_fingerprint":request.fingerprint,"request_id":request.request_id,
        "status":"deferred","reasons":[],"created_ids":[],"quantity_entity_ids":[],
        "source_observed":False,"capacity":"not_verified","load_calculation":"not_performed",
        "conductor_sizing":"caller_supplied_not_verified","raceway_fill":"not_verified",
        "supports_and_fasteners":"not_designed","code_compliance_verified":False,"buildable":False}
    reasons = report["reasons"]
    rules,missing = _profile(request.rules,CircuitDesignRules,"circuit_rules");reasons.extend(missing)
    controls,missing = _profile(request.routing_controls,RoutingOptions,"routing_controls");reasons.extend(missing)
    if not request.rule_references:reasons.append("missing_design_rule_references")
    if not request.evidence:reasons.append("missing_design_decision_evidence")
    for evidence in request.evidence:
        payload = evidence_documents.get(evidence.document_sha256)
        if (evidence.kind != "project_decision" or evidence.provenance.derivation != "user"
                or not isinstance(payload,bytes) or hashlib.sha256(payload).hexdigest() != evidence.document_sha256):
            raise ContractError("design requires exact user-decision evidence bytes")
    equipment = next((e for e in model.electrical_equipment if e.id == request.equipment_id),None)
    if equipment is None:reasons.append("source_equipment_unavailable")
    elif equipment.equipment_type != "panelboard":reasons.append("source_equipment_not_panelboard")
    elif equipment.level_id is None:reasons.append("source_equipment_level_unknown")
    source = next((p for p in model.ports if p.id == request.source_port_id),None)
    if source is None:reasons.append("source_port_unavailable")
    elif source.owner_id != request.equipment_id or source.role != "source" or source.domain != "electrical":
        raise ContractError("source port identity/owner/role/domain mismatch")
    if any(c.source_port_id in {p.id for p in model.ports if p.owner_id == request.equipment_id} for c in model.circuits):
        reasons.append("existing_panel_circuiting_requires_reconciliation")
    if request.groups is None:reasons.append("missing_circuit_groups")
    selected = {p for g in request.groups or () for p in g}
    bindings = {d["proposal_id"]:d for d in dispositions}
    devices = {d.id:d for d in model.electrical_devices}
    mapped = {}
    for proposal in sorted(selected):
        disposition = bindings.get(proposal)
        if disposition is None:raise ContractError("design group references unknown source proposal")
        identity = disposition["canonical_id"]
        if identity is None or identity not in devices:reasons.append("selected_device_not_admitted:"+proposal)
        else:
            if identity in mapped.values():raise ContractError("duplicate surviving device across selected proposals")
            mapped[proposal] = identity
            if devices[identity].level_id is None:reasons.append("selected_device_level_unknown:"+proposal)
    port_specs = {p.proposal_id:p for p in request.load_ports}
    if set(port_specs)-selected:raise ContractError("load port supplied for unselected proposal")
    reasons.extend("missing_load_port:"+p for p in sorted(selected-set(port_specs)))
    if reasons:return model,report
    provenance = Provenance(source_kind="reviewed-circuit-design",source_id=request.request_id,
        source_element_id=request.reviewer_id,derivation="user",confidence=min(e.provenance.confidence for e in request.evidence),
        attributes={"source_observed":False,"role":"designed","request_fingerprint":request.fingerprint,
            "rules":request.rules,"routing_controls":request.routing_controls,
            "rule_references":[{"authority":r.authority,"revision":r.revision,"rule_id":r.rule_id} for r in request.rule_references],
            "evidence":[e.to_dict() for e in request.evidence],"rationale":request.rationale})
    ports = []
    by_id = {p.id:p for p in model.ports}
    for proposal in sorted(selected):
        spec = port_specs[proposal];owner = devices[mapped[proposal]];p = owner.pose.position
        port = Port(id=spec.id,owner_id=owner.id,domain="electrical",role="load",
            pose=Pose(position=Point3(x=p.x+spec.offset_canonical_m[0],y=p.y+spec.offset_canonical_m[1],z=p.z+spec.offset_canonical_m[2])),
            direction=Vector3(x=spec.direction[0],y=spec.direction[1],z=spec.direction[2]),provenance=(provenance,),
            confidence=provenance.confidence)
        if port.id in by_id and by_id[port.id] != port:raise ContractError("designed load port identity collision")
        if any(p.owner_id == owner.id and p.role == "load" for p in model.ports):
            report["reasons"].append("existing_load_ports_require_reconciliation:"+proposal)
        ports.append(port)
    if report["reasons"]:return model,report
    candidate = replace(model,ports=(*model.ports,*ports))
    try:
        designed = design_proposed_circuits(candidate,equipment_id=equipment.id,
            device_ids=tuple(mapped[p] for g in request.groups for p in g),rules=rules,
            user_groups=tuple(tuple(mapped[p] for p in g) for g in request.groups),user_input_id=request.request_id,
            source_port_id=source.id,load_port_ids={mapped[p]:port_specs[p].id for p in selected},routing_options=controls)
    except NoRouteError:
        report["reasons"].append("routing_unavailable_for_reviewed_connections")
        return model,report
    created = {p.id for p in ports}
    quantity = set()
    changes = {}
    for name in ("circuits","conductors","routes","route_fittings"):
        original = {e.id for e in getattr(model,name)}
        rows = []
        for e in getattr(designed,name):
            if e.id not in original:
                created.add(e.id)
                if name != "circuits":quantity.add(e.id)
                e = replace(e,provenance=(*e.provenance,provenance),confidence=min(e.confidence,provenance.confidence))
            rows.append(e)
        changes[name] = tuple(rows)
    designed = replace(designed,**changes)
    BuildingModel.from_dict(designed.to_dict())
    report.update(status="designed_preliminary",created_ids=sorted(created),quantity_entity_ids=sorted(quantity))
    return designed,report
