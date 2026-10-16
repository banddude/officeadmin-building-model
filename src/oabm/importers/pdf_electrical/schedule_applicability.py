"""Evidence tiers for schedule attributes; never a fixture placement producer."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_SHEET = re.compile(r"\b[A-Z]{1,3}(?:-\d{1,4}|\d{1,3}(?:\.\d{1,3})?)\b")
_REFERENCE = re.compile(r"\b(?:SEE|REFER(?:\s+TO)?|PER)\b", re.I)
_SCHEDULE = re.compile(r"\b(?:(?:LIGHT(?:ING)?|LUMINAIRE)\s+(?:FIXTURE\s+)?|FIXTURE\s+)SCHEDULE\b", re.I)
_ORDINALS = {name: str(i) for i, name in enumerate(
    ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH", "EIGHTH", "NINTH", "TENTH"), 1)}


def _scopes(texts: Sequence[Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in texts:
        text = " ".join(item.text.upper().split())
        if _REFERENCE.search(text):
            continue
        if not (re.search(r"\b(?:PLAN|RCP|SCHEDULE)\b", text)
                or re.fullmatch(r"(?:BUILDING|LEVEL|FLOOR)\s+[A-Z0-9-]+", text)):
            continue
        pairs = [("building", m.group(1)) for m in re.finditer(r"\bBUILDING\s+([A-Z0-9-]+)\b", text)]
        pairs += [("floor", m.group(1)) for m in re.finditer(r"\b(?:LEVEL|FLOOR)\s+([A-Z]?\d+|GROUND|BASEMENT|ROOF|LOWER|UPPER|MEZZANINE)\b", text)]
        pairs += [("floor", m.group(1)) for m in re.finditer(
            r"\b(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH|GROUND|BASEMENT|UPPER|LOWER|\d+(?:ST|ND|RD|TH)?)\s+FLOOR\b", text)]
        for kind, value in pairs:
            if kind == "building" and value in {"PLAN", "LIGHTING", "ELECTRICAL", "FIXTURE", "SCHEDULE"}:
                continue
            value = _ORDINALS.get(value, re.sub(r"(?<=\d)(?:ST|ND|RD|TH)$", "", value))
            scope = result.setdefault(item.page, {"values": {}, "source_element_ids": []})
            scope["values"].setdefault(kind, set()).add(value)
            scope["source_element_ids"].append(item.element_id)
    return {page: {"values": {key: sorted(values) for key, values in sorted(scope["values"].items())},
                   "source_element_ids": sorted(set(scope["source_element_ids"]))}
            for page, scope in sorted(result.items())}


def _scope_status(field: Mapping[str, Any], schedule: Mapping[str, Any]) -> str:
    a, b = field.get("values", {}), schedule.get("values", {})
    if any(len(values) != 1 for values in (*a.values(), *b.values())):
        return "ambiguous_scope"
    for key, values in b.items():
        if key not in a:
            return "required_scope_unknown"
        if a[key] != values:
            return "scope_conflict"
    return "applicable"


def resolve_schedule_associations(
    *, texts: Sequence[Any], schedules: Mapping[tuple[int, str], Any],
    metadata: Sequence[Mapping[str, Any]], problems: Sequence[Mapping[str, Any]],
    excluded_text_ids: set[str], normalize_tag: Callable[[str], str | None],
) -> tuple[dict[tuple[int, str], Any], dict[tuple[int, str], dict[str, Any]], list[dict[str, Any]]]:
    """Exact tags and explicit references outrank document-unique inference.

    Scope values are source labels, not canonical levels or elevations. Unknown
    required scopes, multiple definitions, and unresolved references fail closed.
    Confidence values are evidence-tier policy ceilings, not calibrated probabilities.
    """
    scopes = _scopes(texts)
    schedule_pages = {page for page, _ in schedules} | {int(item["page"]) for item in problems}
    headings = {str(item["heading_element_id"]): item for item in metadata}
    aliases: dict[str, set[int]] = defaultdict(set)
    for item in texts:
        if item.page not in schedule_pages:
            continue
        label = re.sub(r"^SHEET(?:\s+(?:NUMBER|NO))?\s*[:#]?\s*", "", item.text.upper().strip())
        if _SHEET.fullmatch(label):
            aliases[label].add(item.page)
    references: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in texts:
        if not (_REFERENCE.search(item.text) and _SCHEDULE.search(item.text)):
            continue
        if re.search(r"\b(?:ARCHITECTURAL|STRUCTURAL|PLUMBING|SANITARY)\s+(?:FIXTURE\s+)?SCHEDULE\b", item.text, re.I):
            continue
        cue = _REFERENCE.search(item.text)
        assert cue is not None
        prefix = item.text[:cue.start()].strip(" :,-")
        # The page does not prove which individual fixture a location-, phase-,
        # or condition-qualified note covers. Preserve that uncertainty rather
        # than granting a qualified reference page-wide authority.
        qualifiers = []
        if prefix and not re.fullmatch(r"(?:NOTE\s*)?\d*[.)]?", prefix, re.I):
            qualifiers.append(prefix)
        qualifiers.extend(match.group(0) for match in re.finditer(
            r"\b(?:INTERIOR|EXTERIOR|BUILDING|FLOOR|LEVEL|ROOM|AREA|ZONE|PHASE|WING|EXCEPT|ONLY|FOR|IF)\b",
            item.text[cue.end():], re.I,
        ))
        identifiers = _SHEET.findall(item.text.upper())
        targets = set()
        if identifiers:
            for identifier in identifiers:
                targets.update(aliases.get(identifier, ()))
        else:
            normalized = " ".join(item.text.upper().split())
            for heading in headings.values():
                title = " ".join(str(heading["heading_text"]).upper().split())
                if title in normalized:
                    targets.add(int(heading["page"]))
        references[item.page].append({"source_element_id": item.element_id,
                                      "source_text": item.text,
                                      "candidate_schedule_pages": sorted(targets),
                                      "unmatched_sheet_ids": sorted(set(identifiers) - set(aliases)),
                                      "applicability_qualifiers": qualifiers,
                                      "applicability_status": "unresolved" if qualifiers else "unqualified_page_reference"})
    rows_by_tag: dict[str, list[Any]] = defaultdict(list)
    for (_, tag), row in sorted(schedules.items()):
        rows_by_tag[tag].append(row)
    known_tags = set(rows_by_tag) | {str(item["fixture_tag"]) for item in problems if item.get("fixture_tag")}
    fields: dict[tuple[int, str], list[str]] = defaultdict(list)
    for item in texts:
        tag = normalize_tag(item.text)
        if item.element_id not in excluded_text_ids and tag in known_tags:
            fields[(item.page, tag)].append(item.element_id)
    resolved = dict(schedules)  # Preserve the established same-sheet behavior.
    bases = {key: {"tier": "same_sheet", "derivation": "observed", "confidence": 0.99,
                   "field_page": key[0], "schedule_page": row.page,
                   "source_element_ids": list(row.source_element_ids)}
             for key, row in sorted(schedules.items())}
    unresolved = []
    for (page, tag), field_ids in sorted(fields.items()):
        refs = references.get(page, [])
        if (page, tag) in schedules and not refs:
            continue
        candidates = rows_by_tag.get(tag, [])
        targets = {target for ref in refs for target in ref["candidate_schedule_pages"]}
        bad_ref = bool(refs) and (any(len(ref["candidate_schedule_pages"]) != 1 or ref["unmatched_sheet_ids"] or ref["applicability_qualifiers"] for ref in refs) or len(targets) != 1)
        eligible = []
        outcomes = []
        for row in candidates:
            status = _scope_status(scopes.get(page, {}), scopes.get(row.page, {}))
            if refs and row.page not in targets:
                status = "not_explicit_target"
            outcomes.append({"page": row.page, "fields": dict(row.fields),
                             "source_element_ids": list(row.source_element_ids), "status": status})
            if status == "applicable":
                eligible.append(row)
        blocking = [item for item in problems
                    if (not item.get("fixture_tag") or item.get("fixture_tag") == tag)
                    and (not refs or item["page"] in targets)
                    and _scope_status(scopes.get(page, {}), scopes.get(int(item["page"]), {})) != "scope_conflict"]
        if bad_ref or len(eligible) != 1 or blocking:
            resolved.pop((page, tag), None)
            bases.pop((page, tag), None)
            unresolved.append({
                "kind": "lighting_schedule_association", "page": page, "fixture_tag": tag,
                "source_element_id": min(field_ids), "source_element_ids": sorted(field_ids),
                "status": "unresolved_lighting_schedule_association",
                "reason_code": "lighting_schedule_reference_unresolved" if bad_ref else "lighting_schedule_applicability_unresolved",
                "candidate_schedules": outcomes, "reference_outcomes": refs,
                "blocking_schedule_diagnostics": [dict(item) for item in blocking],
                "field_scope": scopes.get(page, {}),
            })
            continue
        row = eligible[0]
        tier = "explicit_reference" if refs else "document_unique_exact_tag"
        resolved[(page, tag)] = row
        bases[(page, tag)] = {
            "tier": tier, "derivation": "observed" if refs else "inferred",
            "confidence": 0.95 if refs else 0.75,
            "field_page": page, "schedule_page": row.page,
            "field_tag_source_element_ids": sorted(field_ids),
            "source_element_ids": list(row.source_element_ids),
            "heading_source_element_ids": sorted(str(item["heading_element_id"]) for item in metadata if item["page"] == row.page),
            "reference_outcomes": refs, "field_scope": scopes.get(page, {}),
            "schedule_scope": scopes.get(row.page, {}),
            "candidate_schedules": outcomes,
        }
    return resolved, bases, unresolved
