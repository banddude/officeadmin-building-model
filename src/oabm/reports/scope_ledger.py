"""Opt-in object scope ledger; caller policy never rewrites source observations."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from oabm.model import BuildingModel, ContractError, Provenance, RuleReference
from oabm.model.common import _finite, _validate_json_value, provenance_applies_to
from oabm.model.model import _decode_dataclass
from oabm.qa.takeoff_comparison import SCOPE_STATUSES
from .panel_schedules import (
    SourceSnapshot, ModelSourceContext, BINDING_ALGORITHM, _source_document,
    _portable, _json, _array, _ids, _scope_exists,
)

VERSION = '1.0.0'
MODES = ('preserve_unresolved', 'new_when_confirmed_unmarked')
COLLECTIONS = ('electrical_devices', 'electrical_equipment')


def _digest(data):
    return hashlib.sha256(_json(data).encode()).hexdigest()


def _read(cls, text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError('duplicate scope ledger JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ContractError('nonfinite scope ledger JSON number')
    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError('invalid scope ledger JSON') from exc
    return cls.from_dict(data)


class Portable:
    @classmethod
    def from_dict(cls, data):
        _validate_json_value(data)
        return _decode_dataclass(cls, data)

    @classmethod
    def from_json(cls, text):
        return _read(cls, text)

    def to_dict(self):
        data = _portable(self)
        self.from_dict(data)
        return data

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, allow_nan=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopePolicy(Portable):
    version: str
    mode: str
    basis: RuleReference
    actor: str
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        if self.version != VERSION or not isinstance(self.version, str):
            raise ContractError('unsupported scope policy version')
        if not isinstance(self.mode, str) or self.mode not in MODES:
            raise ContractError('unknown scope policy mode')
        if not isinstance(self.basis, RuleReference):
            raise ContractError('scope policy requires a typed rule reference')
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ContractError('scope policy requires an explicit actor')
        _array(self.provenance, Provenance, 'policy provenance')
        if not self.provenance:
            raise ContractError('scope policy requires source provenance')
        normalized = []
        for record in self.provenance:
            data = _portable(record)
            if not isinstance(record.attributes, dict):
                raise ContractError('policy provenance attributes must be objects')
            if data.get('scope_paths') is not None:
                data['scope_paths'] = sorted(data['scope_paths'])
            decoded = _decode_dataclass(Provenance, data)
            if any(not _scope_exists(self, path) for path in decoded.scope_paths or ()):
                raise ContractError('unknown policy-owner evidence scope')
            normalized.append(decoded)
        object.__setattr__(self, 'provenance', tuple(normalized))

    @property
    def fingerprint(self):
        return _digest(self.to_dict())


def _scope_facts(source):
    """Consume existing classifier metadata; malformed evidence fails closed."""
    lane = source.entity['attributes'].get('pdf_electrical')
    if not isinstance(lane, dict):
        return 'unresolved', 'no_scope_classification', False
    status = lane.get('scope_status')
    if not isinstance(status, str) or status not in SCOPE_STATUSES:
        return 'unresolved', 'no_scope_classification', False
    reason = lane.get('scope_reason')
    marker = lane.get('scope_marker')
    marker_id = lane.get('scope_marker_source_element_id')
    legend_ids = lane.get('scope_legend_source_element_ids')
    # No classifier here: only structural agreement with existing metadata.
    malformed = (marker is not None and (not isinstance(marker, str) or not marker.strip()))
    malformed |= marker_id is not None and (not isinstance(marker_id, str) or not marker_id.strip())
    malformed |= legend_ids is not None and (not isinstance(legend_ids, list) or not legend_ids or
        any(not isinstance(x, str) or not x.strip() for x in legend_ids) or len(set(legend_ids)) != len(legend_ids))
    malformed |= reason is not None and (not isinstance(reason, str) or not reason.strip())
    if malformed:
        return 'unresolved', 'malformed_scope_evidence', False
    # "status" is the importer's original printed marker, distinct from scope_status.
    printed = lane.get('status')
    if printed is not None and (not isinstance(printed, str) or not printed.strip() or printed != marker):
        return 'unresolved', 'contradictory_scope_evidence', False
    if marker is None and (marker_id is not None or legend_ids is not None):
        return 'unresolved', 'contradictory_scope_evidence', False
    if status != 'unresolved':
        if reason is not None:
            return 'unresolved', 'contradictory_scope_evidence', False
        return status, None, False
    if reason == 'no_scope_marker':
        if marker is not None or printed is not None or marker_id is not None or legend_ids is not None:
            return 'unresolved', 'contradictory_scope_evidence', False
        # Reject other marker/legend claims even when the familiar marker key is absent.
        extras = [key for key in lane if key.startswith('scope_legend') or
                  key.startswith('scope_marker') or key in {'status_meaning', 'scope_method'}]
        if any(lane[key] is not None for key in extras):
            return 'unresolved', 'contradictory_scope_evidence', False
        return status, reason, True
    return status, reason or 'unresolved', False


def _evidence_confidence(records, paths):
    applicable = [record.confidence for record in records
                  if provenance_applies_to(record, paths)]
    return min(applicable, default=1.0)


def _effective(source, policy, context):
    status, reason, eligible = _scope_facts(source)
    confidence = min(source.entity['confidence'], _evidence_confidence(
        tuple(_decode_dataclass(Provenance, r) for r in source.entity['provenance']),
        ('attributes.pdf_electrical.scope_status', 'attributes.pdf_electrical.scope_reason',
         'attributes.pdf_electrical.scope_marker', 'attributes.pdf_electrical.scope_marker_source_element_id',
         'attributes.pdf_electrical.scope_legend_source_element_ids', 'attributes.pdf_electrical.scope_legend_text',
         'attributes.pdf_electrical.scope_method', 'attributes.pdf_electrical.status')))
    confidence = min(confidence, _evidence_confidence(context.provenance, (source.collection,)))
    fallback = eligible and policy.mode == 'new_when_confirmed_unmarked'
    if fallback:
        status = 'new'
        confidence = min(confidence, _evidence_confidence(policy.provenance, ('mode', 'basis', 'actor')))
    return status, reason, confidence, ('inferred_caller_policy' if fallback else 'source_metadata')


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeLedgerRow:
    source: SourceSnapshot
    canonical_type: str
    effective_status: str
    diagnostic_reason: str | None
    effective_confidence: float
    derivation: str
    policy_fingerprint: str

    def __post_init__(self):
        if not isinstance(self.source, SourceSnapshot) or self.source.collection not in COLLECTIONS:
            raise ContractError('scope row requires an electrical device/equipment snapshot')
        field = 'device_type' if self.source.collection == 'electrical_devices' else 'equipment_type'
        if self.canonical_type != self.source.entity[field]:
            raise ContractError('scope row type contradicts source')
        if not isinstance(self.effective_status, str) or self.effective_status not in SCOPE_STATUSES:
            raise ContractError('unknown effective scope status')
        if self.diagnostic_reason is not None and (not isinstance(self.diagnostic_reason, str) or not self.diagnostic_reason):
            raise ContractError('invalid scope diagnostic reason')
        confidence = _finite(self.effective_confidence, 'effective confidence')
        if not 0 <= confidence <= self.source.entity['confidence']:
            raise ContractError('effective confidence must be bounded by source')
        if self.derivation not in ('source_metadata', 'inferred_caller_policy'):
            raise ContractError('invalid scope derivation')
        if not isinstance(self.policy_fingerprint, str) or re.fullmatch('[0-9a-f]{64}', self.policy_fingerprint) is None:
            raise ContractError('invalid policy binding')


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeCountGroup:
    collection: str
    canonical_type: str
    status: str
    object_ids: tuple[str, ...]
    count: int

    def __post_init__(self):
        if self.collection not in COLLECTIONS or not isinstance(self.collection, str):
            raise ContractError('unknown scope group collection')
        if not isinstance(self.canonical_type, str) or not self.canonical_type:
            raise ContractError('scope group requires a canonical type')
        if not isinstance(self.status, str) or self.status not in SCOPE_STATUSES:
            raise ContractError('unknown group scope status')
        _ids(self.object_ids, 'scope group identities')
        if self.object_ids != tuple(sorted(self.object_ids)):
            raise ContractError('scope group identities must be sorted')
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count != len(self.object_ids):
            raise ContractError('scope group count contradicts identities')


def _groups(rows):
    types = sorted({(r.source.collection, r.canonical_type) for r in rows})
    return tuple(ScopeCountGroup(collection=collection, canonical_type=kind, status=status,
        object_ids=tuple(r.source.id for r in rows if r.source.collection == collection and
                         r.canonical_type == kind and r.effective_status == status),
        count=sum(r.source.collection == collection and r.canonical_type == kind and
                  r.effective_status == status for r in rows))
        for collection, kind in types for status in SCOPE_STATUSES)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeLedger(Portable):
    version: str
    binding_algorithm: str
    source_model_fingerprint: str
    model_source_context: ModelSourceContext
    policy: ScopePolicy
    policy_fingerprint: str
    rows: tuple[ScopeLedgerRow, ...]
    groups: tuple[ScopeCountGroup, ...]
    code_verification_performed: bool

    def __post_init__(self):
        if self.version != VERSION or not isinstance(self.version, str):
            raise ContractError('unsupported scope ledger version')
        if self.binding_algorithm != BINDING_ALGORITHM:
            raise ContractError('unsupported scope binding algorithm')
        if not isinstance(self.model_source_context, ModelSourceContext) or not isinstance(self.policy, ScopePolicy):
            raise ContractError('scope ledger requires typed source context and policy')
        if not isinstance(self.source_model_fingerprint, str) or re.fullmatch('[0-9a-f]{64}', self.source_model_fingerprint) is None:
            raise ContractError('invalid model binding')
        if self.policy_fingerprint != self.policy.fingerprint:
            raise ContractError('scope ledger policy binding mismatch')
        _array(self.rows, ScopeLedgerRow, 'scope rows')
        _array(self.groups, ScopeCountGroup, 'scope groups')
        identities = tuple(r.source.id for r in self.rows)
        if identities != tuple(sorted(set(identities))):
            raise ContractError('scope rows require sorted unique canonical identities')
        for row in self.rows:
            if row.policy_fingerprint != self.policy_fingerprint:
                raise ContractError('scope row policy binding mismatch')
            if (row.effective_status, row.diagnostic_reason, row.effective_confidence, row.derivation) != _effective(row.source, self.policy, self.model_source_context):
                raise ContractError('effective scope contradicts original source and caller policy')
        if self.groups != _groups(self.rows):
            raise ContractError('scope groups contradict individually identified rows')
        if self.code_verification_performed is not False:
            raise ContractError('scope ledger performs no code verification')

    def validate_against(self, model: BuildingModel, policy: ScopePolicy):
        if self.to_dict() != generate_scope_ledger(model, policy=policy).to_dict():
            raise ContractError('scope ledger does not match supplied source model/policy')


def generate_scope_ledger(model: BuildingModel, *, policy: ScopePolicy) -> ScopeLedger:
    if not isinstance(policy, ScopePolicy):
        raise ContractError('explicit typed caller scope policy is required')
    policy = ScopePolicy.from_dict(policy.to_dict())
    document = _source_document(model)
    context = ModelSourceContext.capture(document)
    rows = []
    for collection in COLLECTIONS:
        for entity in document[collection]:
            source = SourceSnapshot.from_source_data(collection, entity)
            status, reason, confidence, derivation = _effective(source, policy, context)
            rows.append(ScopeLedgerRow(source=source,
                canonical_type=entity['device_type' if collection == 'electrical_devices' else 'equipment_type'],
                effective_status=status, diagnostic_reason=reason, effective_confidence=confidence,
                derivation=derivation, policy_fingerprint=policy.fingerprint))
    rows = tuple(sorted(rows, key=lambda r: r.source.id))
    return ScopeLedger(version=VERSION, binding_algorithm=BINDING_ALGORITHM,
        source_model_fingerprint=_digest(document), model_source_context=context,
        policy=policy, policy_fingerprint=policy.fingerprint, rows=rows, groups=_groups(rows),
        code_verification_performed=False)
