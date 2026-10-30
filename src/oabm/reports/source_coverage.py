"""Read-only accounting of an explicitly inventoried source, not geometric QA."""
from __future__ import annotations

import hashlib
import json
import re

from oabm.model import BuildingModel, ContractError
from .panel_schedules import ENTITY_TYPES, _json, _source_document


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _sha(value, name):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ContractError(f'{name} must be a lowercase SHA-256')


def _ids(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ContractError(f'{name} must be a list of nonempty IDs')
    if len(value) != len(set(value)):
        raise ContractError(f'duplicate {name}')
    return sorted(value)


def inventory_fingerprint(source_sha256: str, object_ids: list[str]) -> str:
    """Hash the inventory and the caller-supplied source-content hash together."""
    _sha(source_sha256, 'source hash')
    return _hash({'source_sha256': source_sha256, 'object_ids': _ids(object_ids, 'source IDs')})


def model_fingerprint(model: BuildingModel) -> str:
    """Use the existing report binding normalization, including model validation."""
    return _hash(_source_document(model))


def source_coverage(model: BuildingModel, *, source_sha256: str, object_ids: list[str],
                    mapping: dict, strict: bool = False) -> dict:
    """Account for source IDs using an explicitly version-bound mapping.

    Mapping has exactly inventory_sha256, model_sha256 and entries. Each entry is
    {source_id, canonical_ids} or {source_id, omission_reason}. Unlisted source
    IDs are unresolved. Invalid references and stale bindings always raise.
    Strict handoff also rejects unresolved IDs. No source bytes are opened.
    """
    if not isinstance(strict, bool):
        raise ContractError('strict must be boolean')
    ids = _ids(object_ids, 'source IDs')
    inventory_hash = inventory_fingerprint(source_sha256, ids)
    document = _source_document(model)
    model_hash = _hash(document)
    if not isinstance(mapping, dict) or set(mapping) != {'inventory_sha256', 'model_sha256', 'entries'}:
        raise ContractError('invalid coverage mapping fields')
    if mapping['inventory_sha256'] != inventory_hash or mapping['model_sha256'] != model_hash:
        raise ContractError('stale coverage mapping binding')
    if not isinstance(mapping['entries'], list):
        raise ContractError('coverage entries must be a list')
    canonical = {item['id'] for name in ENTITY_TYPES for item in document.get(name, [])}
    entries = {}
    for entry in mapping['entries']:
        if not isinstance(entry, dict) or set(entry) not in ({'source_id', 'canonical_ids'}, {'source_id', 'omission_reason'}):
            raise ContractError('invalid coverage entry fields')
        sid = entry['source_id']
        if not isinstance(sid, str) or sid not in ids:
            raise ContractError('unknown source ID')
        if sid in entries:
            raise ContractError('duplicate source mapping')
        if 'canonical_ids' in entry:
            targets = _ids(entry['canonical_ids'], 'canonical IDs')
            if not targets or any(target not in canonical for target in targets):
                raise ContractError('unknown or empty canonical target')
            entries[sid] = {'source_id': sid, 'status': 'mapped', 'canonical_ids': targets}
        else:
            reason = entry['omission_reason']
            if not isinstance(reason, str) or not reason.strip():
                raise ContractError('omission requires a concrete reason')
            entries[sid] = {'source_id': sid, 'status': 'omitted', 'omission_reason': reason}
    rows = [entries.get(sid, {'source_id': sid, 'status': 'unresolved'}) for sid in ids]
    unresolved = [row['source_id'] for row in rows if row['status'] == 'unresolved']
    if strict and unresolved:
        raise ContractError('unresolved source IDs: ' + ', '.join(unresolved))
    return {'version': '1.0.0', 'source_sha256': source_sha256,
            'inventory_sha256': inventory_hash, 'model_sha256': model_hash, 'rows': rows}
