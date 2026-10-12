"""Finite caller policies; intersection is not electrical-code verification."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Sequence

from oabm.model import CatalogProduct, ContractError, Provenance, RuleReference
from oabm.model.common import _validate_id, _validate_json_value
from oabm.model.model import _decode_dataclass
from oabm.model.raceway import wiring_method_for_route_type
from .raceways import RacewayRule

POLICY_VERSION = "1.0.0"
LAYERS = ("baseline", "company", "job")


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be nonempty text")


def _route_type(value: str) -> None:
    _text(value, "route_type")
    if value != value.lower() or wiring_method_for_route_type(value) is None:
        raise ContractError("unsupported route_type predicate")


def _product_key(product: CatalogProduct) -> tuple[str, str, str]:
    return product.catalog_id, product.catalog_revision, product.product_id


def _reference_key(reference: RuleReference) -> tuple[str, str, str]:
    return reference.authority, reference.rule_id, reference.revision


def _portable(value):
    return json.loads(json.dumps(asdict(value), sort_keys=True, allow_nan=False))


def _scope_exists(owner, path: str) -> bool:
    current = owner
    for segment in path.split("."):
        if not is_dataclass(current) or segment not in {f.name for f in fields(current)}:
            return False
        current = getattr(current, segment)
    return True


@dataclass(frozen=True, slots=True, kw_only=True)
class RacewayPolicyRule:
    reference: RuleReference
    layer: str
    enabled: bool
    explanation: str
    actor_id: str
    route_type: str
    permitted_products: tuple[CatalogProduct, ...]
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, RuleReference):
            raise ContractError("policy rule requires a typed reference")
        _validate_id(self.reference.rule_id, "policy rule identity")
        if self.layer not in LAYERS:
            raise ContractError("unknown policy layer")
        if not isinstance(self.enabled, bool):
            raise ContractError("enabled must be boolean")
        if self.layer == "baseline" and not self.enabled:
            raise ContractError("baseline records cannot be disabled")
        _text(self.explanation, "explanation")
        _text(self.actor_id, "actor_id")
        _route_type(self.route_type)
        if not isinstance(self.permitted_products, tuple) or any(
            not isinstance(p, CatalogProduct) for p in self.permitted_products
        ):
            raise ContractError("permitted_products must be typed product references")
        if len(set(self.permitted_products)) != len(self.permitted_products):
            raise ContractError("duplicate product permission")
        if not isinstance(self.provenance, tuple) or not self.provenance or any(
            not isinstance(p, Provenance) or p.derivation is None for p in self.provenance
        ):
            raise ContractError("policy requires explicit typed source provenance")
        for record in self.provenance:
            if not isinstance(record.attributes, dict):
                raise ContractError("policy provenance.attributes must be a JSON object")
            _validate_json_value(record.attributes)
            for name in ("source_kind", "source_id"):
                _text(getattr(record, name), f"provenance.{name}")
            for name in ("source_element_id", "method"):
                value = getattr(record, name)
                if value is not None:
                    _text(value, f"provenance.{name}")
            # Canonical Provenance validates numeric fields and scope syntax.
            # Standalone input scopes must also resolve on this input owner.
            for path in record.scope_paths or ():
                if not _scope_exists(self, path):
                    raise ContractError(f"policy evidence names unknown input field {path!r}")


def _normalized_rule(rule: RacewayPolicyRule) -> RacewayPolicyRule:
    from dataclasses import replace
    return replace(rule, permitted_products=tuple(sorted(rule.permitted_products, key=_product_key)),
                   provenance=tuple(sorted((replace(p, confidence=float(p.confidence),
                                                    scope_paths=None if p.scope_paths is None else tuple(sorted(p.scope_paths)),
                                                    attributes=_portable(p)["attributes"]) for p in rule.provenance),
                                           key=lambda p: json.dumps(_portable(p), sort_keys=True))))


@dataclass(frozen=True, slots=True, kw_only=True)
class RacewayPolicy:
    policy_id: str
    version: str
    catalog_products: tuple[CatalogProduct, ...]
    rules: tuple[RacewayPolicyRule, ...]

    def __post_init__(self) -> None:
        _validate_id(self.policy_id, "policy_id")
        if self.version != POLICY_VERSION:
            raise ContractError("unsupported raceway policy version")
        if not isinstance(self.catalog_products, tuple) or any(
            not isinstance(p, CatalogProduct) for p in self.catalog_products
        ):
            raise ContractError("catalog_products must be typed product references")
        if len(set(self.catalog_products)) != len(self.catalog_products):
            raise ContractError("duplicate policy catalog reference")
        if not isinstance(self.rules, tuple) or any(not isinstance(r, RacewayPolicyRule) for r in self.rules):
            raise ContractError("rules must be typed policy records")
        identities = set()
        for rule in self.rules:
            identity = rule.reference.authority, rule.reference.rule_id
            if identity in identities:
                raise ContractError("duplicate policy rule identity, including conflicting revisions")
            identities.add(identity)
            if not set(rule.permitted_products).issubset(self.catalog_products):
                raise ContractError("policy permission references a missing catalog product")

    @classmethod
    def from_dict(cls, data) -> RacewayPolicy:
        _validate_json_value(data)
        return _decode_dataclass(cls, data)

    @classmethod
    def from_json(cls, text: str) -> RacewayPolicy:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ContractError(f"duplicate JSON field {key!r}")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ContractError(f"nonfinite JSON constant {value}")
        try:
            data = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid raceway policy JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: str | Path) -> RacewayPolicy:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def to_dict(self) -> dict:
        from dataclasses import replace
        normalized = replace(self, catalog_products=tuple(sorted(self.catalog_products, key=_product_key)),
                             rules=tuple(sorted((_normalized_rule(r) for r in self.rules),
                                                key=lambda r: _reference_key(r.reference))))
        return _portable(normalized)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, allow_nan=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductBlock:
    product: CatalogProduct
    blocking_references: tuple[RuleReference, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyDiagnostic:
    code: str
    route_type: str
    layer: str
    references: tuple[RuleReference, ...]
    blocked_products: tuple[ProductBlock, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyEvaluation:
    route_type: str
    status: str
    effective_products: tuple[CatalogProduct, ...]
    contributing_references: tuple[RuleReference, ...]
    diagnostics: tuple[PolicyDiagnostic, ...]
    rules: tuple[RacewayRule, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyCompilation:
    policy_id: str
    evaluations: tuple[PolicyEvaluation, ...]
    source_rules: tuple[RacewayPolicyRule, ...]
    code_verification_performed: bool = False

    @property
    def rules(self) -> tuple[RacewayRule, ...]:
        return tuple(r for evaluation in self.evaluations for r in evaluation.rules)

    def to_dict(self) -> dict:
        return _portable(self)


def compile_raceway_policy(policy: RacewayPolicy, *, route_types: Sequence[str]) -> PolicyCompilation:
    """Compile finite permissions only; the caller still chooses product/basis.

    Each layer is an order-independent intersection. A record is checked against
    the prior layer, not against whichever same-layer record happened to run first.
    Invalid widening fails the route compilation closed rather than silently
    accepting only the intersection of an invalid policy.
    """
    if not isinstance(policy, RacewayPolicy):
        raise ContractError("compiler requires a typed policy")
    for route_type in route_types:
        _route_type(route_type)
    if len(set(route_types)) != len(route_types):
        raise ContractError("duplicate evaluated route type")
    source_rules = tuple(sorted((_normalized_rule(r) for r in policy.rules),
                                key=lambda r: _reference_key(r.reference)))
    evaluations = []
    for route_type in sorted(route_types):
        records = tuple(r for r in source_rules if r.route_type == route_type and r.enabled)
        baseline = tuple(r for r in records if r.layer == "baseline")
        diagnostics = []
        contributing = []
        effective = set()
        if not baseline:
            diagnostics.append(PolicyDiagnostic(code="missing_baseline", route_type=route_type,
                                                layer="baseline", references=()))
        else:
            effective = set(baseline[0].permitted_products)
            for record in baseline:
                effective.intersection_update(record.permitted_products)
            contributing.extend(baseline)
            for layer in ("company", "job"):
                current = tuple(r for r in records if r.layer == layer)
                parent_effective = set(effective)
                layer_diagnostics = []
                for record in current:
                    blocks = tuple(ProductBlock(product=product, blocking_references=tuple(
                        r.reference for r in contributing if product not in r.permitted_products))
                        for product in sorted(set(record.permitted_products) - parent_effective, key=_product_key))
                    if blocks:
                        layer_diagnostics.append(PolicyDiagnostic(code="permission_widening", route_type=route_type,
                            layer=layer, references=(record.reference,), blocked_products=blocks))
                diagnostics.extend(layer_diagnostics)
                if layer_diagnostics:
                    break
                for record in current:
                    effective.intersection_update(record.permitted_products)
                contributing.extend(current)
            if not effective and not diagnostics:
                diagnostics.append(PolicyDiagnostic(code="empty_effective_permissions", route_type=route_type,
                    layer=contributing[-1].layer, references=tuple(r.reference for r in contributing)))
        compiled = []
        if not diagnostics:
            # All source content and explicit revisions contribute to the digest;
            # normalized ordering prevents permissions/input permutations changing it.
            consumed = [_portable(r) for r in contributing]
            for product in sorted(effective, key=_product_key):
                payload = {"policy_id":policy.policy_id,"version":policy.version,"route_type":route_type,
                           "product":_portable(product),"contributing_rules":consumed}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode()).hexdigest()
                reference = RuleReference(authority="caller-layered-raceway-policy",
                    revision=f"sha256:{digest}", rule_id=f"policy-rule:{digest[:32]}")
                edges = tuple(Provenance(source_kind="layered-raceway-policy", source_id=policy.policy_id,
                    source_element_id=record.reference.rule_id, method="finite-layer-intersection",
                    derivation="inferred", confidence=min(p.confidence for p in record.provenance),
                    scope_paths=("basis",), attributes={"source_rule":_portable(record),
                        "effective_product":_portable(product), "code_verification_performed":False})
                    for record in contributing)
                compiled.append(RacewayRule(reference=reference, product=product, route_type=route_type, provenance=edges))
        evaluations.append(PolicyEvaluation(route_type=route_type, status="unresolved" if diagnostics else "effective",
            effective_products=() if diagnostics else tuple(sorted(effective, key=_product_key)),
            contributing_references=tuple(r.reference for r in contributing), diagnostics=tuple(diagnostics),
            rules=tuple(compiled)))
    return PolicyCompilation(policy_id=policy.policy_id, evaluations=tuple(evaluations), source_rules=source_rules)
