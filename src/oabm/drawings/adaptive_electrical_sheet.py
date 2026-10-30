"""Choose among bounded, caller-supplied layouts using existing producers.

Only presentation changes. The full-size continuation layout is the baseline;
compact candidates must pass the same ink/identity checks and use fewer sheets.
"""
from __future__ import annotations

from dataclasses import replace
import re

from .body_regions import _bounds
from .electrical_sheet import plan_electrical_sheets, build_electrical_sheet_bodies


def _candidates(values, sheet):
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError('layout_candidates: supply one to eight explicit layout presets')
    result = []
    seen = {'continuation'}
    dl, dt, dr, db = sheet.drawing_bounds_mm
    for candidate in values:
        if not isinstance(candidate, dict) or set(candidate) != {'id', 'single_line_regions', 'panel_region_mm'}:
            raise ValueError('layout candidate: exact id, single_line_regions and panel_region_mm required')
        identity = candidate['id']
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', identity) or identity.casefold() in seen:
            raise ValueError('layout candidate: unique bounded id required; continuation is reserved')
        seen.add(identity.casefold())
        regions = candidate['single_line_regions']
        if not isinstance(regions, dict) or set(regions) != {'diagram', 'notes', 'feeder_register'}:
            raise ValueError('layout candidate: diagram, notes and feeder_register regions required')
        detached = {name: _bounds(value, identity + '.' + name) for name, value in regions.items()}
        panel = _bounds(candidate['panel_region_mm'], identity + '.panels')
        allocated = [('footer', (dl, db - 14, dr, db)),
                     ('qualification', (dl, db - 22, dr, db - 16))]
        for name, box in [*detached.items(), ('panels', panel)]:
            l, t, r, b = box
            if l < dl or t < dt or r > dr or b > db:
                raise ValueError('layout candidate: region exceeds shared sheet bounds')
            for other, (ol, ot, or_, ob) in allocated:
                if l < or_ + 1 and ol < r + 1 and t < ob + 1 and ot < b + 1:
                    raise ValueError(f'layout candidate: {name} overlaps {other} or violates clearance')
            allocated.append((name, box))
        result.append((identity, detached, panel))
    return result


def plan_adaptive_electrical_sheets(model, sheet, *, single_line_regions,
                                   layout_candidates, panel_width_mm=300.,
                                   layout=None, reserved_sheet_ids=(),
                                   panel_policy='listed_only'):
    """Return an existing sealed plan plus a deterministic selection receipt.

Invalid preset syntax/bounds are errors, not silent fallbacks. A valid preset
that cannot fit its content is recorded as rejected. Baseline validation is
mandatory and its errors propagate. Equal page counts retain the baseline or
earlier candidate: we never shrink type or invent a density score.
"""
    candidates = _candidates(layout_candidates, sheet)
    reserved_sheet_ids = tuple(reserved_sheet_ids)
    before = model.to_json(indent=None)
    common = dict(panel_width_mm=panel_width_mm, layout=layout,
                  reserved_sheet_ids=reserved_sheet_ids, panel_policy=panel_policy)

    def checked(regions, panel, placement):
        plan = plan_electrical_sheets(model, sheet, single_line_regions=regions,
                                     panel_region_mm=panel, panel_placement=placement, **common)
        sheets = tuple(replace(sheet, id=identity) for identity in plan.sheet_ids)
        build_electrical_sheet_bodies(model, sheets, plan=plan)
        return plan

    best = baseline = checked(single_line_regions, None, 'continuation')
    selected = 'continuation'
    attempts = [{'id': selected, 'status': 'valid', 'sheet_count': len(best.sheet_ids)}]
    for identity, regions, panel in candidates:
        try:
            plan = checked(regions, panel, 'shared')
        except ValueError as exc:
            attempts.append({'id': identity, 'status': 'content_does_not_fit', 'reason': str(exc)})
            continue
        if (plan.model_sha256 != baseline.model_sha256 or
                plan.panel_report != baseline.panel_report or
                plan.drawing_schedules != baseline.drawing_schedules or
                plan.panel_selection['selected_schedule_ids'] != baseline.panel_selection['selected_schedule_ids']):
            raise ValueError('adaptive layout changed source facts or panel selection')
        attempts.append({'id': identity, 'status': 'valid', 'sheet_count': len(plan.sheet_ids)})
        if len(plan.sheet_ids) < len(best.sheet_ids):
            best, selected = plan, identity
    if model.to_json(indent=None) != before:
        raise ValueError('adaptive layout mutated the canonical model')
    return best, {'version': 'electrical-layout-selection-v1',
                  'selected_candidate_id': selected,
                  'selected_plan_fingerprint': best.fingerprint,
                  'policy': 'fewest_valid_sheets; baseline_then_input_order_on_ties',
                  'attempts': attempts}
