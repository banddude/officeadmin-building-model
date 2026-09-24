"""Compare extracted device counts with human takeoff references (#105).

A takeoff is a human reference, not truth: two takeoffs of the same set can
disagree, and both can differ from the drawings. This module only counts and
compares. It never edits a model or a reference.

Counts come from canonical electrical devices and equipment, by canonical type
and scope of work (``pdf_electrical.scope_status``: new, relocated,
existing_to_remain, removed, or unresolved). Only new and relocated scope is
compared with a takeoff. Unresolved scope is counted and reported but never
added to the in-scope total.

The exact-match rate is the number of comparable device types whose in-scope
count equals the reference count, divided by the number of comparable types.
Numerator and denominator are always reported. A type is comparable for one
reference when the extractor supports it and that reference counts it or the
extraction found it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from oabm.model import BuildingModel

SCOPE_STATUSES = ("new", "relocated", "existing_to_remain", "removed", "unresolved")
IN_SCOPE = ("new", "relocated")


def _lane(entity: Any) -> Mapping[str, Any]:
    attributes = entity.attributes.get("pdf_electrical")
    return attributes if isinstance(attributes, Mapping) else {}


def device_scope_counts(model: BuildingModel) -> dict[str, Any]:
    """Deterministic counts by canonical type and scope, with unresolved reasons."""

    by_type: dict[str, Counter[str]] = {}
    reasons: Counter[str] = Counter()
    pages: set[int] = set()
    for entity in (*model.electrical_devices, *model.electrical_equipment):
        kind = getattr(entity, "device_type", None) or getattr(entity, "equipment_type")
        lane = _lane(entity)
        scope = lane.get("scope_status")
        if scope not in SCOPE_STATUSES:
            scope = "unresolved"
            reasons["no_scope_classification"] += 1
        elif scope == "unresolved":
            reasons[str(lane.get("scope_reason") or "unresolved")] += 1
        by_type.setdefault(kind, Counter())[scope] += 1
        page = lane.get("source_page")
        if isinstance(page, int):
            pages.add(page)
    types = {
        kind: {scope: counts[scope] for scope in SCOPE_STATUSES}
        for kind, counts in sorted(by_type.items())
    }
    totals = {scope: sum(item[scope] for item in types.values()) for scope in SCOPE_STATUSES}
    return {
        "by_type": types,
        "in_scope_by_type": {
            kind: sum(counts[scope] for scope in IN_SCOPE) for kind, counts in types.items()
        },
        "totals": totals,
        "in_scope_total": sum(totals[scope] for scope in IN_SCOPE),
        "unresolved_reasons": dict(sorted(reasons.items())),
        "source_pages": sorted(pages),
    }


def compare_counts(
    extracted: Mapping[str, int],
    reference: Mapping[str, int],
    *,
    comparable_types: Iterable[str],
) -> dict[str, Any]:
    """Per-type extracted vs reference counts over the declared comparable types.

    A comparable type missing from ``reference`` counts as a zero reference; a
    type outside ``comparable_types`` is listed but never scored.
    """

    comparable = sorted(set(comparable_types))
    rows = []
    for kind in comparable:
        got = int(extracted.get(kind, 0))
        expected = int(reference.get(kind, 0))
        delta = got - expected
        rows.append(
            {
                "device_type": kind,
                "extracted": got,
                "reference": expected,
                "delta": delta,
                "absolute_delta": abs(delta),
                "status": "exact" if delta == 0 else ("over" if delta > 0 else "under"),
            }
        )
    exact = sum(row["status"] == "exact" for row in rows)
    return {
        "rows": rows,
        "exact_match": {
            "numerator": exact,
            "denominator": len(comparable),
            "rate": (exact / len(comparable)) if comparable else None,
        },
        "extracted_total": sum(row["extracted"] for row in rows),
        "reference_total": sum(row["reference"] for row in rows),
        "absolute_delta_total": sum(row["absolute_delta"] for row in rows),
        "not_scored": {
            "extracted_only_types": sorted(set(extracted) - set(comparable)),
            "reference_only_types": sorted(set(reference) - set(comparable)),
        },
    }


def reference_disagreements(
    references: Mapping[str, Mapping[str, int]],
    *,
    comparable_types: Iterable[str],
) -> list[dict[str, Any]]:
    """Comparable types on which independent references give different counts."""

    names = sorted(references)
    result = []
    for kind in sorted(set(comparable_types)):
        counts = {name: int(references[name].get(kind, 0)) for name in names}
        if len(set(counts.values())) > 1:
            result.append({"device_type": kind, "counts": counts})
    return result


def scored_types(
    extracted: Mapping[str, int],
    reference: Mapping[str, int],
    supported_types: Iterable[str],
) -> list[str]:
    """Supported types this reference counts or the extraction found.

    A type that neither side counts is not scored: two zeros are not evidence
    that the extractor matched the reference.
    """

    present = {kind for kind, count in extracted.items() if count > 0}
    present |= {kind for kind, count in reference.items() if count > 0}
    return sorted(present & set(supported_types))


def compare_with_references(
    model: BuildingModel,
    references: Mapping[str, Mapping[str, int]],
    *,
    supported_types: Iterable[str],
) -> dict[str, Any]:
    """Count a model once and compare its in-scope counts with each reference.

    ``supported_types`` are the canonical types the extractor can recognize.
    Each reference is scored over the supported types it counts or the
    extraction found, so a category the extractor cannot see is never scored.
    """

    supported = sorted(set(supported_types))
    counts = device_scope_counts(model)
    in_scope = counts["in_scope_by_type"]
    comparable = {
        name: scored_types(in_scope, references[name], supported) for name in sorted(references)
    }
    return {
        "counts": counts,
        "supported_types": supported,
        "comparable_types": comparable,
        "comparisons": {
            name: compare_counts(in_scope, references[name], comparable_types=comparable[name])
            for name in sorted(references)
        },
        "reference_disagreements": reference_disagreements(
            references,
            comparable_types=sorted({kind for kinds in comparable.values() for kind in kinds}),
        ),
    }
