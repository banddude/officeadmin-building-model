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

Some takeoffs tally fixture-tag LABELS printed beside a fixture run (a
``TAG-12``-style text label) rather than device symbols. Comparing symbol
counts with a label tally is not like-for-like, so :func:`compare_tag_labels`
compares label tallies by tag instead: per tag it reports the reference count
and the label counts found on base and alternate sheets, and scores exact
matches on base and allowing alternates. :func:`count_label_occurrences`
builds a per-tag tally from raw text observations, skipping observations that
fall inside declared legend or schedule boxes so a legend's own tag list is
not counted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from oabm.model import BuildingModel

SCOPE_STATUSES = ("new", "relocated", "existing_to_remain", "removed", "unresolved")
IN_SCOPE = ("new", "relocated")

# Unicode dash and minus characters seen in PDF-extracted text, mapped to
# ASCII "-": U+2010, U+2011, U+2012, U+2013, U+2014, U+2212, U+FE58, U+FE63
# and U+FF0D.
_DASH_TRANSLATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "﹘": "-",
        "﹣": "-",
        "－": "-",
    }
)

# A leading new-work token: "(N)" or a bare "N". The bare form needs a
# following separator so words that merely start with N are never stripped;
# the parenthesized form may sit directly against the tag body.
_NEW_WORK_PREFIX = re.compile(r"^(?:\(\s*N\s*\)(?:\s*-\s*|\s+)?|N(?:\s*-\s*|\s+))(.+)$")

# Default fixture-tag shape: a short alphabetic prefix, a hyphen, digits, and
# one optional trailing letter.
DEFAULT_TAG_PATTERN = r"(?<![A-Za-z0-9])LF-\d+[A-Z]?(?![A-Za-z0-9])"


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


def normalize_tag(tag: str) -> str:
    """Normalize one fixture-tag spelling.

    Maps unicode dashes to ASCII hyphens, collapses whitespace, uppercases,
    removes spaces around hyphens (``LF - 4`` becomes ``LF-4``), and strips a
    leading ``N`` or ``(N)`` new-work prefix. A bare ``N`` or ``(N)`` is left
    alone: stripping it would invent a tag from nothing.
    """

    text = " ".join(str(tag).translate(_DASH_TRANSLATION).split()).upper()
    match = _NEW_WORK_PREFIX.match(text)
    if match:
        text = match.group(1)
    return re.sub(r"\s*-\s*", "-", text).strip()


def _observation_field(observation: Any, name: str) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(name)
    return getattr(observation, name, None)


def _exclusion_box(box: Sequence[float]) -> tuple[float, float, float, float]:
    values = tuple(float(value) for value in box)
    if len(values) != 4:
        raise ValueError("exclusion boxes are (x0, y0, x1, y1) rectangles")
    x0, y0, x1, y1 = values
    if x0 > x1 or y0 > y1:
        raise ValueError(f"exclusion box corners are inverted: {box!r}")
    return (x0, y0, x1, y1)


def count_label_occurrences(
    observations: Iterable[Any],
    *,
    tag_pattern: str = DEFAULT_TAG_PATTERN,
    exclude_boxes: Mapping[int, Iterable[Sequence[float]]] | None = None,
) -> dict[str, int]:
    """Count fixture-tag label occurrences in raw text observations.

    Observations are mappings or objects exposing ``text`` and ``x_pt`` /
    ``y_pt`` and optionally ``page`` — the fields of the extractor's
    ``PdfTextObservation``. Every regex match in each observation's text is
    counted once, keyed by its :func:`normalize_tag` spelling.

    ``exclude_boxes`` maps a page number to ``(x0, y0, x1, y1)`` rectangles in
    the same coordinates as the observations. An observation whose anchor
    point falls inside a rectangle on its page is skipped, so the legend or
    schedule's own tag list is not counted. An observation without a usable
    integer page is never excluded: exclusion is the caller's assertion and a
    count is never dropped silently for an observation the caller has not
    placed on a page.
    """

    compiled = re.compile(tag_pattern)
    boxes = {
        page: [_exclusion_box(box) for box in page_boxes]
        for page, page_boxes in (exclude_boxes or {}).items()
    }
    counts: dict[str, int] = {}
    for observation in observations:
        page = _observation_field(observation, "page")
        page_boxes = boxes.get(page, ()) if isinstance(page, int) else ()
        if page_boxes:
            x_pt = float(_observation_field(observation, "x_pt"))
            y_pt = float(_observation_field(observation, "y_pt"))
            if any(x0 <= x_pt <= x1 and y0 <= y_pt <= y1 for x0, y0, x1, y1 in page_boxes):
                continue
        text = _observation_field(observation, "text")
        if not isinstance(text, str):
            raise ValueError(f"text observation without text: {observation!r}")
        for match in compiled.finditer(text):
            tag = normalize_tag(match.group(0))
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items()))


def compare_tag_labels(
    label_counts_by_sheet: Mapping[str, Mapping[str, int]],
    reference_by_tag: Mapping[str, int],
    *,
    base_sheets: Iterable[str],
    alternate_sheets: Iterable[str],
) -> dict[str, Any]:
    """Compare per-tag label tallies with a reference takeoff by tag.

    ``label_counts_by_sheet`` maps a sheet key to that sheet's per-tag label
    counts, as produced by :func:`count_label_occurrences`. Raw tag spellings
    in either mapping are normalized with :func:`normalize_tag` before they
    are merged and compared, so case, whitespace, hyphen variants and a
    new-work prefix never split one tag into two.

    Each scored tag reports its reference count, its base-sheet and
    alternate-sheet label counts, their sum, the signed delta against the
    sum, and whether it matches exactly on base and exactly allowing
    alternates. A tag is scored when the reference counts it or a declared
    sheet carries it; two zeros are not evidence of a match, and all-zero
    tags are listed under ``not_scored`` instead.

    Sheets are declared once each: a key in both groups raises. Sheets with
    counts that were never declared, and declared sheets without counts, are
    reported so no evidence is silently dropped; undeclared counts never
    enter the scored sums.
    """

    base = sorted(set(base_sheets))
    alternate = sorted(set(alternate_sheets))
    overlap = sorted(set(base) & set(alternate))
    if overlap:
        raise ValueError(f"sheets declared as both base and alternate: {', '.join(overlap)}")

    merged_by_sheet: dict[str, dict[str, int]] = {}
    for sheet, counts in label_counts_by_sheet.items():
        merged: dict[str, int] = {}
        for raw_tag, count in counts.items():
            tag = normalize_tag(raw_tag)
            merged[tag] = merged.get(tag, 0) + int(count)
        merged_by_sheet[sheet] = merged

    merged_reference: dict[str, int] = {}
    for raw_tag, count in reference_by_tag.items():
        tag = normalize_tag(raw_tag)
        merged_reference[tag] = merged_reference.get(tag, 0) + int(count)

    def declared_total(sheet_group: list[str], tag: str) -> int:
        return sum(merged_by_sheet.get(sheet, {}).get(tag, 0) for sheet in sheet_group)

    tags = set(merged_reference)
    for counts in merged_by_sheet.values():
        tags |= set(counts)
    rows = []
    not_scored = []
    for tag in sorted(tags):
        reference = int(merged_reference.get(tag, 0))
        base_labels = declared_total(base, tag)
        alternate_labels = declared_total(alternate, tag)
        if reference == 0 and base_labels == 0 and alternate_labels == 0:
            not_scored.append(tag)
            continue
        combined = base_labels + alternate_labels
        if base_labels == reference:
            status = "match_on_base"
        elif combined == reference:
            status = "match_with_alternates"
        else:
            status = "over" if combined > reference else "under"
        rows.append(
            {
                "tag": tag,
                "reference": reference,
                "base_labels": base_labels,
                "alternate_labels": alternate_labels,
                "base_plus_alternate": combined,
                "delta": combined - reference,
                "exact_on_base": base_labels == reference,
                "exact_with_alternates": combined == reference,
                "status": status,
            }
        )

    def exact(rate_rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        numerator = sum(1 for row in rate_rows if row[key])
        denominator = len(rate_rows)
        return {
            "numerator": numerator,
            "denominator": denominator,
            "rate": (numerator / denominator) if denominator else None,
        }

    return {
        "rows": rows,
        "exact_on_base": exact(rows, "exact_on_base"),
        "exact_with_alternates": exact(rows, "exact_with_alternates"),
        "totals": {
            "reference": sum(row["reference"] for row in rows),
            "base": sum(row["base_labels"] for row in rows),
            "alternate": sum(row["alternate_labels"] for row in rows),
            "base_plus_alternate": sum(row["base_plus_alternate"] for row in rows),
        },
        "base_sheets": base,
        "alternate_sheets": alternate,
        "undeclared_sheets": sorted(set(label_counts_by_sheet) - set(base) - set(alternate)),
        "declared_without_observations": sorted(
            (set(base) | set(alternate)) - set(label_counts_by_sheet)
        ),
        "not_scored": not_scored,
    }
