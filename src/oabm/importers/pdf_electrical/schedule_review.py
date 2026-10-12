"""Versioned human review input for source schedule cells, not a model schema.

The review confirms membership already visible in source cells. It neither
creates panels nor assigns devices to circuits. Positions are displayed,
bottom-origin PDF points before canonical registration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


def _interval(values: tuple[float, float], label: str) -> None:
    if (len(values) != 2
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)
        or values[0] >= values[1]):
        raise ValueError(f"{label} requires two finite increasing coordinates")


@dataclass(frozen=True, slots=True)
class ConfirmedScheduleRow:
    circuit_number: int
    row_bounds_pt: tuple[float, float]
    number_element_id: str
    load_element_ids: tuple[str, ...]
    confirmed: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.circuit_number, bool) or not isinstance(self.circuit_number, int) or self.circuit_number < 1:
            raise ValueError("circuit_number must be a positive integer")
        _interval(self.row_bounds_pt, "row_bounds_pt")
        if not self.number_element_id or not self.load_element_ids or any(not s for s in self.load_element_ids):
            raise ValueError("row requires number and load source element IDs")
        if len(set(self.load_element_ids)) != len(self.load_element_ids) or self.number_element_id in self.load_element_ids:
            raise ValueError("number and load source IDs must be distinct")
        if not isinstance(self.confirmed, bool):
            raise ValueError("confirmed must be a boolean")


@dataclass(frozen=True, slots=True)
class PanelScheduleReview:
    review_id: str
    source_id: str
    source_fingerprint: str
    page: int
    page_provenance: Mapping[str, Any]
    reviewer: str
    reviewed_at: str
    panel_tag: str
    heading_element_id: str
    region_bbox_pt: tuple[float, float, float, float]
    number_column_pt: tuple[float, float]
    load_column_pt: tuple[float, float]
    rows: tuple[ConfirmedScheduleRow, ...]
    version: str = "1.0"
    coordinate_space: str = "displayed-bottom-origin-pt"

    def __post_init__(self) -> None:
        if self.version != "1.0" or self.coordinate_space != "displayed-bottom-origin-pt":
            raise ValueError("unsupported schedule review version or coordinate space")
        for label in ("review_id", "source_id", "reviewer", "panel_tag", "heading_element_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_fingerprint):
            raise ValueError("source_fingerprint must be a lowercase SHA-256 digest")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ValueError("page must be a positive 1-based integer")
        timestamp = datetime.fromisoformat(self.reviewed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("reviewed_at requires an explicit timezone")
        if not isinstance(self.page_provenance, Mapping) or not self.page_provenance:
            raise ValueError("page_provenance must record the reviewed display frame")
        if len(self.region_bbox_pt) != 4:
            raise ValueError("region_bbox_pt requires left, bottom, right, top")
        left, bottom, right, top = self.region_bbox_pt
        _interval((left, right), "region x bounds")
        _interval((bottom, top), "region y bounds")
        for label in ("number_column_pt", "load_column_pt"):
            bounds = getattr(self, label)
            _interval(bounds, label)
            if not left <= bounds[0] < bounds[1] <= right:
                raise ValueError("columns must be contained in the reviewed region")
        if max(self.number_column_pt[0], self.load_column_pt[0]) < min(self.number_column_pt[1], self.load_column_pt[1]):
            raise ValueError("number and load columns must be disjoint")
        if len({row.circuit_number for row in self.rows}) != len(self.rows):
            raise ValueError("a review must not duplicate a circuit row")
        for row in self.rows:
            if not bottom <= row.row_bounds_pt[0] < row.row_bounds_pt[1] <= top:
                raise ValueError("rows must be contained in the reviewed region")
        ordered = sorted(self.rows, key=lambda row: row.row_bounds_pt)
        if any(a.row_bounds_pt[1] > b.row_bounds_pt[0] for a, b in zip(ordered, ordered[1:])):
            raise ValueError("reviewed row bounds must be disjoint")

    def to_dict(self) -> dict[str, Any]:
        # JSON round trip makes coordinate tuples portable and copies mappings.
        data = asdict(self)
        data["rows"] = sorted(data["rows"], key=lambda row: row["circuit_number"])
        for row in data["rows"]:
            row["load_element_ids"] = sorted(row["load_element_ids"])
        return json.loads(json.dumps(data, sort_keys=True, allow_nan=False))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PanelScheduleReview:
        copied = dict(data)
        copied["rows"] = tuple(ConfirmedScheduleRow(
            **{**row, "row_bounds_pt": tuple(row["row_bounds_pt"]),
               "load_element_ids": tuple(row["load_element_ids"])}
        ) for row in copied["rows"])
        for label in ("region_bbox_pt", "number_column_pt", "load_column_pt"):
            copied[label] = tuple(copied[label])
        return cls(**copied)


def schedule_source_fingerprint(document: Any) -> str:
    """Bind all extracted observations and page display provenance, not a filename.

    Source IDs alone can be caller-supplied aliases. This additional fingerprint
    rejects stale text, element IDs, positions, page contents or display frames.
    It is a fingerprint of extraction evidence, not a hash of raw PDF bytes.
    """
    payload = asdict(document)
    for key in ("texts", "symbols", "vectors"):
        payload[key] = sorted(payload[key], key=lambda item: (item["page"], item["element_id"]))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode()).hexdigest()


def reviewed_schedule_rows(
    document: Any,
    reviews: Sequence[PanelScheduleReview],
    *,
    recognized_panels: set[str],
    heading_tags: Mapping[tuple[int, str], str],
) -> tuple[dict[str, set[int]], set[str], dict[str, list[dict]], dict[tuple[str, int], list[dict]], dict[str, str]]:
    """Return accepted membership, consumed IDs, diagnostics, evidence, failures."""
    membership: dict[str, set[int]] = {}
    consumed: set[str] = set()
    diagnostics: dict[str, list[dict]] = {}
    evidence: dict[tuple[str, int], list[dict]] = {}
    failures: dict[str, str] = {}
    fingerprint = schedule_source_fingerprint(document)
    grouped: dict[str, list[PanelScheduleReview]] = {}
    for review in reviews:
        grouped.setdefault(review.panel_tag, []).append(review)
    texts = {(t.page, t.element_id): t for t in document.texts}
    duplicate_text_ids = len(texts) != len(document.texts)
    seen_review_ids: dict[str, dict] = {}
    for panel, panel_reviews in sorted(grouped.items()):
        membership[panel] = set()
        records = sorted((r.to_dict() for r in panel_reviews), key=lambda r: json.dumps(r, sort_keys=True))
        diagnostics[panel] = []
        mapping_keys: set[str] = set()
        for record in records:
            previous = seen_review_ids.setdefault(record["review_id"], record)
            if previous != record:
                failures[panel] = "schedule_confirmation_conflict"
                failures[previous["panel_tag"]] = "schedule_confirmation_conflict"
            mapping_keys.add(json.dumps({k: v for k, v in record.items()
                                        if k not in {"review_id", "reviewer", "reviewed_at"}}, sort_keys=True))
        if len(mapping_keys) != 1:
            failures[panel] = "schedule_confirmation_conflict"
        for review, record in sorted(zip(panel_reviews, (r.to_dict() for r in panel_reviews)), key=lambda pair: json.dumps(pair[1], sort_keys=True)):
            reason = failures.get(panel)
            if reason is None and (review.source_id != document.source_id or review.source_fingerprint != fingerprint):
                reason = "schedule_confirmation_source_mismatch"
            frame = document.page_provenance.get(review.page)
            if reason is None and (frame is None or dict(review.page_provenance) != dict(frame)):
                reason = "schedule_confirmation_frame_mismatch"
            if reason is None and (panel not in recognized_panels or heading_tags.get((review.page, review.heading_element_id)) != panel):
                reason = "schedule_confirmation_heading_mismatch"
            if reason is None and duplicate_text_ids:
                reason = "schedule_confirmation_source_id_collision"
            left, bottom, right, top = review.region_bbox_pt
            if reason is None and (frame.get("coordinate_space") != "displayed"
                                   or not 0 <= left < right <= frame.get("displayed_page_width_pt", 0)
                                   or not 0 <= bottom < top <= frame.get("displayed_page_height_pt", 0)):
                reason = "schedule_confirmation_region_outside_page"
            heading = texts.get((review.page, review.heading_element_id))
            if reason is None and (heading is None or not left < heading.x_pt < right or not bottom < heading.y_pt < top):
                reason = "schedule_confirmation_heading_outside_region"
            if reason:
                failures[panel] = reason
            row_results = []
            for row in sorted(review.rows, key=lambda row: row.circuit_number):
                row_reason = reason
                if not row.confirmed:
                    row_reason = row_reason or "schedule_row_not_confirmed"
                if row_reason is None:
                    y0, y1 = row.row_bounds_pt
                    def cell(column):
                        return [t for t in document.texts if t.page == review.page
                                and column[0] < t.x_pt < column[1] and y0 < t.y_pt < y1]
                    numbers, loads = cell(review.number_column_pt), cell(review.load_column_pt)
                    if (len(numbers) != 1 or numbers[0].element_id != row.number_element_id
                        or re.fullmatch(r"\d+", numbers[0].text.strip()) is None
                        or int(numbers[0].text.strip()) != row.circuit_number
                        or not loads or {t.element_id for t in loads} != set(row.load_element_ids)
                        or any(not t.text.strip() for t in loads)):
                        row_reason = "schedule_row_source_mismatch"
                row_results.append({"circuit_number": row.circuit_number,
                                    "status": "confirmed" if row_reason is None else "unresolved",
                                    **({"reason_code": row_reason} if row_reason else {})})
                if row_reason is None:
                    membership[panel].add(row.circuit_number)
                    ids = (review.heading_element_id, row.number_element_id, *row.load_element_ids)
                    consumed.update(ids)
                    evidence.setdefault((panel, row.circuit_number), []).append({
                        "review": record, "source_element_ids": sorted(ids),
                        "source_row": next(r for r in record["rows"] if r["circuit_number"] == row.circuit_number),
                        "source_cells": {
                            "number": {"source_element_id": numbers[0].element_id, "text": numbers[0].text,
                                       "position_pt": [numbers[0].x_pt, numbers[0].y_pt]},
                            "loads": [{"source_element_id": t.element_id, "text": t.text,
                                       "position_pt": [t.x_pt, t.y_pt]}
                                      for t in sorted(loads, key=lambda t: t.element_id)],
                        },
                    })
            diagnostics[panel].append({"review": record, "rows": row_results,
                                       "status": "rejected" if reason else "reviewed",
                                       **({"reason_code": reason} if reason else {})})
    # A conflict discovered through reused review IDs may affect an earlier panel.
    for panel in failures:
        membership[panel] = set()
        for diagnostic in diagnostics.get(panel, ()):
            diagnostic["status"] = "rejected"
            diagnostic["reason_code"] = failures[panel]
            for row in diagnostic["rows"]:
                row["status"] = "unresolved"
                row["reason_code"] = failures[panel]
        for key in tuple(evidence):
            if key[0] == panel:
                evidence.pop(key)
    consumed = {source_id for records in evidence.values() for record in records
                for source_id in record["source_element_ids"]}
    return membership, consumed, diagnostics, evidence, failures
