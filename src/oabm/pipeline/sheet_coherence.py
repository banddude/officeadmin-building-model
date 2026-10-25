"""Pre-render coherence checks for independently supplied sheet package inputs.

This verifies agreement, not authenticity of a claimed canonical snapshot.
Retained canonical replay remains the authoritative saved-delivery check.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import replace

from oabm.model import ContractError
from oabm.proposals.contracts import digest


def _require(condition, message):
    if not condition:
        raise ContractError('sheet input coherence: ' + message)


def _same(left, right):
    """JSON semantic equality: numeric spelling may vary, booleans may not."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return False
    return left == right


def _document(data, label):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, label + ' contains duplicate JSON keys')
            result[key] = value
        return result

    def constant(value):
        raise ContractError('sheet input coherence: ' + label + ' contains nonfinite JSON')

    def number(value):
        parsed = float(value)
        _require(math.isfinite(parsed), label + ' contains nonfinite JSON')
        return parsed

    _require(isinstance(data, bytes), label + ' must be encoded JSON bytes')
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, UnicodeError) as exc:
        raise ContractError('sheet input coherence: invalid ' + label + ' JSON') from exc
    _require(isinstance(value, dict), label + ' must be a JSON object')
    return value


def validate_sheet_inputs(drawing, config, payloads, status):
    """Reject mixed drawing/facts/config revisions before any renderer runs."""
    from oabm.drawings.model import DrawingSet
    from oabm.drawings.panel_schedules import panel_report_to_drawing_schedules
    from oabm.reports import PanelScheduleReport
    from .review_drawings import BANNER, VERSION
    from .review_sheet_delivery import ReviewSheetDeliveryConfig

    _require(isinstance(drawing, DrawingSet), 'typed drawing required')
    _require(isinstance(config, ReviewSheetDeliveryConfig), 'typed sheet configuration required')
    _require(isinstance(status, dict) and isinstance(payloads, dict), 'status and payload mappings required')
    original = _document(payloads.get('drawing_set'), 'drawing document')
    panel = _document(payloads.get('panel_facts'), 'panel facts')
    _require(set(original) == {'review', 'drawing_set'}, 'unexpected drawing document fields')
    _require(set(panel) == {'review', 'panel_facts'}, 'unexpected panel document fields')
    _require(_same(original['review'], status), 'drawing review differs from supplied status')
    model_hash = status.get('canonical_model_sha256')
    _require(isinstance(model_hash, str) and re.fullmatch('[0-9a-f]{64}', model_hash) is not None,
             'canonical model hash required')
    _require(status.get('version') == VERSION and status.get('status') == 'incomplete_review'
             and status.get('permit_ready') is False, 'unsupported review status or readiness claim')
    _require(status.get('config_fingerprint') == digest(config.drawings.to_dict())
             and status.get('sheet_config_fingerprint') == digest(config.to_dict())
             and status.get('sheet_delivery_version') == config.version,
             'configuration differs from reviewed drawing/sheet configuration')
    _require(_same(status.get('complete_model_evidence'), {'artifact_kind': 'canonical_model', 'sha256': model_hash}),
             'complete-model evidence differs from review binding')
    _require(_same(panel['review'], {'status': 'incomplete_review', 'permit_ready': False,
             'canonical_model_sha256': model_hash, 'source_coverage': 'unavailable'}),
             'panel facts belong to a different model revision or review status')
    for view in drawing.views:
        bindings = [value for key, value in view.metadata if key == 'canonical_model_sha256']
        _require(bindings == [model_hash], 'drawing view has a missing or conflicting model binding')

    expected = drawing.to_dict()
    supplied = original['drawing_set']
    _require(isinstance(supplied, dict) and set(supplied) == set(expected), 'drawing document shape mismatch')
    _require(_same({k: v for k, v in supplied.items() if k != 'source_index'},
             {k: v for k, v in expected.items() if k != 'source_index'}),
             'typed drawing geometry, metadata or schedules differ from drawing document')
    sources = supplied['source_index']
    _require(isinstance(sources, list) and len(sources) == len(expected['source_index']), 'source index mismatch')
    for actual, wanted in zip(sources, expected['source_index']):
        _require(isinstance(actual, dict) and all(_same(actual.get(k), wanted[k])
                 for k in ('canonical_id', 'name', 'confidence')), 'source identity or confidence mismatch')
        provenance = actual.get('provenance', [])
        _require(isinstance(provenance, list) and len(provenance) == len(wanted['provenance']), 'source provenance mismatch')
        for actual_prov, wanted_prov in zip(provenance, wanted['provenance']):
            # Canonical owner records intentionally retain attributes/scope paths
            # beyond the generic renderer projection. Optional defaults are valid.
            _require(isinstance(actual_prov, dict) and all(
                _same(actual_prov.get(k, 1.0 if k == 'confidence' else None), wanted_prov.get(k))
                for k in ('source_kind', 'source_id', 'source_element_id', 'page', 'method', 'confidence', 'derivation')),
                'source provenance projection differs from typed drawing')
    try:
        facts = PanelScheduleReport.from_dict(panel['panel_facts'])
    except ContractError as exc:
        raise ContractError('sheet input coherence: invalid panel fact report: ' + str(exc)) from exc
    _require(facts.source_model_id == drawing.model_id, 'panel facts refer to a different model identity')
    schedules = tuple(replace(s, title=BANNER + ' | ' + s.title)
                      for s in panel_report_to_drawing_schedules(facts))
    _require(schedules == drawing.schedules, 'panel facts differ from drawing schedules')
