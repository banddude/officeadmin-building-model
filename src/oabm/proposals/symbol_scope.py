"""Bounded source assertions for AI symbols, without marker recognition.

These are inferred source interpretations, not procurement or scope approval.
The existing importer status vocabulary is reused by the canonical adapter.
"""
from __future__ import annotations

from dataclasses import dataclass

from oabm.model import ContractError
from oabm.importers.pdf_electrical.importer import (
    SCOPE_NEW, SCOPE_RELOCATED, SCOPE_EXISTING, SCOPE_REMOVED, SCOPE_UNRESOLVED,
)
from .contracts import Portable, _array, _finite, digest, text
from .takeoff import _unit_box

WORK_STATUSES = (SCOPE_NEW, SCOPE_RELOCATED, SCOPE_EXISTING, SCOPE_REMOVED, "not_in_contract")
BID_SCOPES = ("base", "alternate")
SCOPE_METHOD = "reviewed_ai_source_assertions_v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolScopeEvidence(Portable):
    image_id: str
    bounds: tuple[float, float, float, float]
    quote: str

    def __post_init__(self):
        text(self.image_id, "scope evidence image")
        object.__setattr__(self, "bounds", _unit_box(self.bounds, "scope evidence bounds"))
        text(self.quote, "scope source quote", maximum=1024)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolScopeClaim(Portable):
    dimension: str
    value: str
    evidence: tuple[SymbolScopeEvidence, ...]
    confidence: float
    alternate_id: str | None = None

    def __post_init__(self):
        if self.dimension not in ("work_status", "bid_scope"):
            raise ContractError("unsupported symbol scope dimension")
        allowed = WORK_STATUSES if self.dimension == "work_status" else BID_SCOPES
        if self.value not in allowed:
            raise ContractError("unsupported symbol scope assertion")
        _array(self.evidence, SymbolScopeEvidence, "scope evidence")
        if not 1 <= len(self.evidence) <= 8:
            raise ContractError("scope assertion needs bounded quoted source evidence")
        if len({digest(e.to_dict()) for e in self.evidence}) != len(self.evidence):
            raise ContractError("duplicate scope source evidence")
        confidence = _finite(self.confidence, "scope confidence")
        if not 0 <= confidence <= 1:
            raise ContractError("scope confidence outside [0,1]")
        object.__setattr__(self, "confidence", confidence)
        if (self.dimension == "bid_scope" and self.value == "alternate") != (self.alternate_id is not None):
            raise ContractError("only alternate scope requires a source alternate ID")
        if self.alternate_id is not None:
            text(self.alternate_id, "source alternate ID")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolScope(Portable):
    claims: tuple[SymbolScopeClaim, ...]
    version: str = "1.0.0"

    def __post_init__(self):
        if self.version != "1.0.0":
            raise ContractError("unsupported symbol scope version")
        _array(self.claims, SymbolScopeClaim, "symbol scope claims")
        if len(self.claims) > 16:
            raise ContractError("symbol scope assertion budget exceeded")
        if len({digest(c.to_dict()) for c in self.claims}) != len(self.claims):
            raise ContractError("duplicate symbol scope assertion")

    def state(self, dimension):
        if dimension not in ("work_status", "bid_scope"):
            raise ContractError("unsupported symbol scope dimension")
        values = {(c.value, c.alternate_id) for c in self.claims if c.dimension == dimension}
        if not values:
            return SCOPE_UNRESOLVED
        if len(values) != 1:
            return "conflict"
        return next(iter(values))[0]

    @property
    def work_status(self):
        return self.state("work_status")

    @property
    def bid_scope(self):
        return self.state("bid_scope")

    @property
    def alternate_id(self):
        if self.bid_scope != "alternate":
            return None
        return next(c.alternate_id for c in self.claims if c.dimension == "bid_scope")

    @property
    def evidence_image_ids(self):
        return tuple(sorted({e.image_id for c in self.claims for e in c.evidence}))

    def validate_for(self, request):
        known = {im.id for im in request.images}
        if set(self.evidence_image_ids) - known:
            raise ContractError("scope evidence must cite only supplied source images")

    def canonical_metadata(self):
        """Adapt to the existing ledger vocabulary without enabling unmarked fallback."""
        status = self.work_status
        data = {"scope_status": status if status in WORK_STATUSES[:-1] else SCOPE_UNRESOLVED,
                "scope_method": SCOPE_METHOD, "scope_assertions": self.to_dict(),
                "bid_scope": self.bid_scope, "alternate_id": self.alternate_id}
        if status in (SCOPE_UNRESOLVED, "conflict", "not_in_contract"):
            data["scope_reason"] = {"unresolved": "ai_scope_unresolved",
                                    "conflict": "ai_scope_conflict",
                                    "not_in_contract": "not_in_contract"}[status]
        return data


def symbol_scope_schema(request):
    """Optional transport schema. Strict typed parsing remains authoritative."""
    from .symbols import SymbolRequest
    if not isinstance(request, SymbolRequest):
        raise ContractError("scope schema requires a typed symbol request")
    string = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S"}
    bounds = {"type": "array", "minItems": 4, "maxItems": 4,
              "items": {"type": "number", "minimum": 0, "maximum": 1}}
    evidence = {"type": "object", "additionalProperties": False,
        "required": ["image_id", "bounds", "quote"], "properties": {
            "image_id": {"type": "string", "enum": [im.id for im in request.images]},
            "bounds": bounds,
            "quote": {**string, "maxLength": 1024}}}
    assertion = {"type": "object", "additionalProperties": False,
        "required": ["dimension", "value", "evidence", "confidence"],
        "properties": {"dimension": {"enum": ["work_status", "bid_scope"]},
            "value": {"enum": list(WORK_STATUSES + BID_SCOPES)},
            "evidence": {"type": "array", "minItems": 1, "maxItems": 8,
                         "items": evidence},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "alternate_id": {"anyOf": [string, {"type": "null"}]}},
        "allOf": [
            {"if": {"properties": {"dimension": {"const": "work_status"}}},
             "then": {"properties": {"value": {"enum": list(WORK_STATUSES)},
                                       "alternate_id": {"type": "null"}}},
             "else": {"properties": {"value": {"enum": list(BID_SCOPES)}}}},
            {"if": {"properties": {"value": {"const": "alternate"}}},
             "then": {"required": ["alternate_id"], "properties": {"alternate_id": string}},
             "else": {"properties": {"alternate_id": {"type": "null"}}}},
        ]}
    return {"type": "object", "additionalProperties": False,
        "required": ["claims"], "properties": {
            "version": {"const": "1.0.0"},
            "claims": {"type": "array", "maxItems": 16, "items": assertion}}}
