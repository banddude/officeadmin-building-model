"""Source-description category prototypes and bounded observed leader evidence."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any


def category_tokens(value: str) -> tuple[str, ...]:
    """Conservative lexical equivalence, never a fixture-tag/type dictionary."""
    text = value.upper()
    for pattern, replacement in (
        (r"\bDOWN\s+LIGHTS?\b", "DOWNLIGHT"),
        (r"\bUNDER\s+CABINET\b", "UNDERCABINET"),
        (r"\bMOUNT(?:ED|ING)?\b", "MOUNT"),
        (r"\bRECESSED\b", "RECESS"),
    ):
        text = re.sub(pattern, replacement, text)
    words = re.findall(r"[A-Z0-9]+", text)
    return tuple(sorted(set(words) - {"LIGHT", "LIGHTS", "LIGHTING", "FIXTURE", "FIXTURES", "LUMINAIRE", "LUMINAIRES", "LED"}))


def detect_category_legends(*, texts: Sequence[Any], clusters: Sequence[Any], excluded_text_ids: set[str]):
    from . import importer as api
    entries: dict[int, list[Any]] = {}
    keys: set[tuple[int, str]] = set()
    claimed: set[str] = set()
    problems = []
    metadata = []
    for heading in sorted((t for t in texts if api._lighting_heading_kind(t) == "lighting_legend"),
                          key=lambda t: (t.page, -t.y_pt, t.x_pt, t.element_id)):
        labels = [t for t in texts if t.page == heading.page and t.element_id not in excluded_text_ids
                  and t.element_id != heading.element_id
                  and 0 < heading.y_pt - t.y_pt <= api._LIGHTING_LEGEND_VERTICAL_SPAN_PT
                  and abs(t.x_pt - heading.x_pt) <= api._LIGHTING_LEGEND_HEADING_COLUMN_SPAN_PT
                  and api._normalize_lighting_tag(t.text) is None
                  and re.search(r"\b(?:LIGHT|LIGHTS|DOWNLIGHT|PENDANT|SCONCE|LUMINAIRE|LUMINAIRES)\b", t.text, re.I)]
        for column in api._lighting_legend_label_columns(labels):
            if len(column) < 2 or api._lighting_legend_column_grid_rows(column, api._lighting_legend_row_grid(column)) is None:
                continue
            local = []
            row_candidates = {
                label.element_id: [c for c in clusters if c.page == label.page
                    and c.center_pt[0] < label.x_pt
                    and abs(c.center_pt[1] - label.y_pt) <= api._LIGHTING_LEGEND_ROW_GLYPH_Y_TOLERANCE_PT
                    and label.x_pt - c.center_pt[0] <= api._LIGHTING_LEGEND_HEADING_COLUMN_SPAN_PT]
                for label in column
            }
            # Infer a printed prototype column from repeated row alignment,
            # rather than a fixed nearest-symbol distance. Several supported
            # columns stay competing candidates; none wins by proximity.
            for label in column:
                candidates = []
                for candidate in row_candidates[label.element_id]:
                    supporting_rows = {
                        other.element_id for other in column
                        if any(abs(c.center_pt[0] - candidate.center_pt[0])
                               <= api._LIGHTING_LEGEND_ROW_GRID_TOLERANCE_PT
                               for c in row_candidates[other.element_id])
                    }
                    if len(supporting_rows) >= 2:
                        candidates.append(candidate)
                # Every possible legend prototype is excluded from placement,
                # including rows whose glyph association remains ambiguous.
                keys.update((c.page, c.geometry_key) for c in candidates)
                claimed.add(label.element_id)
                if len(candidates) != 1:
                    problems.append({"kind": "lighting_legend_category", "page": label.page,
                        "source_element_id": label.element_id, "source_text": label.text,
                        "status": "unresolved_lighting_legend", "reason_code": "lighting_category_prototype_unresolved",
                        "candidate_geometry_keys": sorted(c.geometry_key for c in candidates)})
                    continue
                local.append(api._LightingLegendEntry(page=label.page,
                    tag="category:" + "|".join(category_tokens(label.text)),
                    prototype=candidates[0], label=label, heading=heading, confidence=0.95))
            if len(local) < 2:
                continue
            entries.setdefault(heading.page, []).extend(local)
            claimed.add(heading.element_id)
            metadata.append({"page": heading.page, "method": "lighting-description-category-legend",
                "heading_element_id": heading.element_id, "heading_text": heading.text,
                "row_count": len(local), "rows": [{"source_element_id": e.label.element_id,
                    "source_text": e.label.text, "category_tokens": list(category_tokens(e.label.text)),
                    "prototype_geometry_key": e.prototype.geometry_key,
                    "prototype_source_element_ids": list(e.prototype.source_element_ids)} for e in local]})
    return ({p: tuple(sorted(v, key=lambda e: (e.label.element_id, e.prototype.geometry_key))) for p,v in sorted(entries.items())},
            keys, claimed, problems, metadata)


def bind_schedule_categories(*, schedules: Mapping[tuple[int, str], Any], category_entries: Mapping[int, Sequence[Any]],
                             tagged_entries: Mapping[int, Sequence[Any]]):
    result = {page: list(entries) for page, entries in tagged_entries.items()}
    bindings = {}
    problems = []
    for (page, tag), row in sorted(schedules.items()):
        if any(entry.tag == tag for entry in tagged_entries.get(page, ())):
            continue
        categories = category_entries.get(page, ())
        if not categories:
            continue
        description = " ".join(row.fields.get(key, "") for key in ("description", "mounting"))
        tokens = category_tokens(description)
        matches = [entry for entry in categories if tokens and category_tokens(entry.label.text) == tokens]
        if len(matches) != 1:
            problems.append({"kind": "lighting_schedule_category", "page": page, "fixture_tag": tag,
                "source_element_id": row.source_element_ids[0], "source_element_ids": list(row.source_element_ids),
                "schedule_page": row.page, "source_description": description,
                "status": "unresolved_lighting_category", "reason_code": "lighting_schedule_category_ambiguous" if matches else "lighting_schedule_category_not_defined",
                "category_tokens": list(tokens), "candidate_categories": [{"source_element_id": entry.label.element_id,
                    "source_text": entry.label.text, "prototype_geometry_key": entry.prototype.geometry_key}
                    for entry in (matches or categories)]})
            continue
        entry = matches[0]
        result.setdefault(page, []).append(replace(entry, tag=tag))
        bindings[(page, tag)] = {"method": "source-description-token-equivalence",
            "category_tokens": list(tokens), "schedule_description": description,
            "schedule_page": row.page, "schedule_source_element_ids": list(row.source_element_ids),
            "legend_page": entry.page, "legend_source_element_id": entry.label.element_id,
            "legend_source_text": entry.label.text, "legend_heading_element_id": entry.heading.element_id,
            "prototype_geometry_key": entry.prototype.geometry_key,
            "prototype_source_element_ids": list(entry.prototype.source_element_ids)}
    return {p: tuple(v) for p,v in result.items()}, bindings, problems


def category_geometry_evidence(cluster: Any, expected: Any, categories: Sequence[Any]):
    from . import importer as api
    ranking = []
    for entry in categories:
        score, guard = api._lighting_shape_class_guard(api._cluster_match_score(cluster, entry.prototype),
            candidate_class=api._cluster_shape_class(cluster), prototype=entry.prototype)
        ranking.append({"legend_source_element_id": entry.label.element_id, "source_text": entry.label.text,
                        "prototype_geometry_key": entry.prototype.geometry_key, "shape_score": score,
                        "shape_class_guard": guard})
    ranking.sort(key=lambda row: (-row["shape_score"], row["legend_source_element_id"]))
    expected_score = next((row["shape_score"] for row in ranking
        if row["legend_source_element_id"] == expected.label.element_id
        and row["prototype_geometry_key"] == expected.prototype.geometry_key), None)
    competing = [row for row in ranking if row["legend_source_element_id"] != expected.label.element_id
        and row["shape_score"] >= api._GLYPH_MATCH_STRONG_SCORE
        and expected_score is not None and expected_score - row["shape_score"] < api._GLYPH_MATCH_MARGIN_MIN]
    confirmed = expected_score is not None and expected_score >= api._GLYPH_MATCH_STRONG_SCORE and not competing
    return confirmed, {"method": "observed-category-prototype-geometry", "expected_shape_score": expected_score,
        "confirm_threshold": api._GLYPH_MATCH_STRONG_SCORE, "category_margin_threshold": api._GLYPH_MATCH_MARGIN_MIN,
        "candidate_categories": ranking,
        "reason_code": ("lighting_category_geometry_confirmed" if confirmed else
                        "lighting_category_geometry_ambiguous" if competing else "lighting_category_geometry_mismatch")}


def leader_candidates(observation: Any, clusters: Sequence[Any], vectors: Sequence[Any]):
    from . import importer as api
    hits: dict[str, set[str]] = {}
    for vector in vectors:
        if (vector.page != observation.page or vector.closed or api._vector_contains_bezier(vector)
                or api._paint_family(vector) != "stroke" or not 2 <= len(vector.points_pt) <= 3):
            continue
        length = sum(math.dist(a,b) for a,b in zip(vector.points_pt, vector.points_pt[1:]))
        if length < 12:
            continue
        for start, end in ((vector.points_pt[0], vector.points_pt[-1]), (vector.points_pt[-1], vector.points_pt[0])):
            if math.dist(start, (observation.x_pt, observation.y_pt)) > 8:
                continue
            for cluster in clusters:
                x0,y0,x1,y1 = cluster.bbox_pt
                if (cluster.page == observation.page and x0-2 <= end[0] <= x1+2
                        and y0-2 <= end[1] <= y1+2
                        and math.dist(start, cluster.center_pt) >= 12):
                    hits.setdefault(cluster.geometry_key, set()).add(vector.element_id)
    return ([c for c in clusters if c.geometry_key in hits],
            {key: {"method": "observed-leader-endpoints", "source_element_ids": sorted(ids)} for key,ids in sorted(hits.items())})
