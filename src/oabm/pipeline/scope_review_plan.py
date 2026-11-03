"""Opt-in scope presentation over existing canonical review drawing geometry."""
from __future__ import annotations

from dataclasses import asdict
import json
import xml.etree.ElementTree as ET

from oabm.model import ContractError
from oabm.reports.scope_ledger import ScopeLedger, ScopePolicy, generate_scope_ledger
from oabm.drawings.scope_ledger import scope_ledger_to_drawing_schedules
from .review_drawings import ReviewDrawingConfig, generate_review_drawings
from oabm.drawings.scope_plan import scope_plan_svg, scope_schedule_svg


def _foreign_scope(value):
    if isinstance(value, dict):
        return 'source_scope' in value or any(_foreign_scope(v) for v in value.values())
    return isinstance(value, (list, tuple)) and any(_foreign_scope(v) for v in value)


def generate_scope_review_plan(model, config, *, policy: ScopePolicy, ledger: ScopeLedger | None = None):
    """Return (artifact bytes, coverage), without changing model or default APIs.

    At most 200 device/equipment rows and 16 plans, 32..200 pixels/metre,
    and 4096 pixels per plan axis. Incompatible source_scope attributes refuse.
    PDF/PNG conversion is an explicit separate operation.
    """
    if not isinstance(config, ReviewDrawingConfig) or not config.plans:
        raise ContractError('scope review requires explicit ReviewDrawingConfig with plans')
    if not isinstance(policy, ScopePolicy):
        raise ContractError('scope review requires explicit ScopePolicy')
    objects = (*model.electrical_devices, *model.electrical_equipment)
    if len(objects) > 200 or len(config.plans) > 16:
        raise ContractError('scope review exceeds 200 objects or 16 plans')
    if not 32 <= config.pixels_per_model_unit <= 200 or any(
        max(p.bounds_m[2]-p.bounds_m[0], p.bounds_m[3]-p.bounds_m[1]) * config.pixels_per_model_unit > 4096
        for p in config.plans):
        raise ContractError('scope review density/extent exceeds bounded presentation')
    if any(_foreign_scope(e.attributes) for e in objects):
        raise ContractError('source_scope is incompatible; explicitly reconcile into pdf_electrical scope evidence first')
    before = model.to_json()
    if ledger is None:
        ledger = generate_scope_ledger(model, policy=policy)
    if not isinstance(ledger, ScopeLedger):
        raise ContractError('scope review requires typed ScopeLedger')
    ledger.validate_against(model, policy)
    payloads, status = generate_review_drawings(model, config)
    document = json.loads(payloads['drawing_set'])
    schedules = scope_ledger_to_drawing_schedules(ledger)
    document['drawing_set']['schedules'].extend(asdict(s) for s in schedules)
    status['scope'] = {'model_fingerprint': ledger.source_model_fingerprint,
        'policy_fingerprint': ledger.policy_fingerprint,
        'classes': [asdict(g) for g in ledger.groups], 'views': [],
        'limits': 'Devices/equipment only; no circuit/raceway scope, safe-off, pricing, topology or permit readiness. Relocation origin/destination geometry unavailable.'}
    for plan in sorted(config.plans, key=lambda p: p.id):
        view = next(v for v in document['drawing_set']['views'] if v['id'] == plan.id)
        svg, coverage = scope_plan_svg(payloads['review_plan:' + plan.id], view,
            plan=plan, pixels_per_model_unit=config.pixels_per_model_unit,
            objects=objects, ledger=ledger)
        payloads['review_plan:' + plan.id] = svg
        status['scope']['views'].append(coverage)
    payloads['scope_ledger'] = ledger.to_json().encode()
    payloads['scope_schedule'] = scope_schedule_svg(schedules, ledger)
    document['review'] = status
    payloads['drawing_set'] = json.dumps(document, sort_keys=True, allow_nan=False).encode()
    if model.to_json() != before:
        raise ContractError('scope review mutated canonical model')
    return payloads, status


def render_scope_review_artifacts(payloads):
    """Explicit optional CairoSVG conversion of scope plan/schedule SVGs.

    Each format uses the identical vector source; IDs remain in SVG/JSON and
    readable canonical ID labels remain in PDF/PNG. No runtime installation.
    """
    import cairosvg
    result = {}
    for key, raw in sorted(payloads.items()):
        if key == 'scope_schedule' or key.startswith('review_plan:'):
            root = ET.fromstring(raw)
            width, height = map(float, root.attrib['viewBox'].split()[2:])
            result[key + ':pdf'] = cairosvg.svg2pdf(bytestring=raw, unsafe=False)
            result[key + ':png'] = cairosvg.svg2png(bytestring=raw, unsafe=False,
                output_width=round(width), output_height=round(height), background_color='white')
    return result
