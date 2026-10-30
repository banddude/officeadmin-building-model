"""Validated reuse of explicitly supplied single-line presentation facts.

This is a drawing adapter, not an electrical calculation.  It never promotes
an upstream circuit's protection to a panel main rating.
"""
from types import SimpleNamespace
import re

from oabm.drawings.single_line.drafting_facts import (
    circuit_facts, equipment_facts, protection_label,
)


def _merge(fields, key, derived, identity):
    if derived is None:
        return fields
    # Free-form display strings cannot be compared electrically. Require exact
    # agreement when both representations are supplied rather than guessing.
    if key in fields and fields[key] != derived:
        raise ValueError(f'{identity}: panel_schedule.{key} conflicts with supplied single_line_drafting; use {derived!r} or omit the duplicate')
    result = dict(fields)
    result[key] = derived
    return result


def panel_ratings(entity, fields):
    if 'single_line_drafting' not in entity.get('attributes', {}):
        return fields
    facts = equipment_facts(SimpleNamespace(id=entity['id'], attributes=entity['attributes']))
    current = facts['bus_current_a']
    return _merge(fields, 'bus', None if current is None else f'{current:g} A', entity['id'])


def circuit_ratings(entity, fields):
    if 'single_line_drafting' not in entity.get('attributes', {}):
        return fields
    facts = circuit_facts(SimpleNamespace(id=entity['id'], attributes=entity['attributes'], poles=entity.get('poles')))
    protection = facts['protection']
    if protection is not None and 'ocpd' in fields and protection.get('rating_a') is not None:
        # A bare amperage is a compatible subset of the structured protection;
        # do not interpret any other free-form wording as equivalent.
        match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*A', fields['ocpd'])
        if match and float(match.group(1)) == protection['rating_a']:
            fields = dict(fields)
            fields.pop('ocpd')
    return _merge(fields, 'ocpd', None if protection is None else protection_label(facts), entity['id'])
