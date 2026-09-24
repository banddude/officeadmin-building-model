"""Register electrical sheets into resolved architectural drawing frames (#104).

An electrical sheet is drawn over the same building as the architectural set,
but its page coordinates are not the building frame. For every electrical page
this module proposes the transform into the canonical frame of one resolved
architectural drawing region (#103), from deterministic evidence only:

- shared wall vectors: visible wall-layer segments on both sheets, otherwise
  paired wall faces on both sheets;
- shared grid or column bubbles: the same short label inside a drawn circle.

Matching uses the printed scale ratio between the two sheets and no rotation.
A page is registered only when its evidence is sufficient, spread out,
consistent, and unique. Otherwise the page stays ``registration_pending`` with
stable reason codes, and the electrical import stays unregistered, which the
Gate D convergence refuses. The result never mutates either model. It returns
proposals and, only when every page is registered, the ``PdfPageTransform``
mapping the electrical importer accepts. Transforms carry ``inferred``
registration evidence; the electrical source positions stay observed facts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from oabm.importers.pdf_architecture import (
    RegionEvidence,
    drawing_level_names,
    printed_sheet_scale,
    region_wall_evidence,
    sheet_wall_evidence,
)
from oabm.importers.pdf_architecture.extract import extract_pdf as extract_sheet_observations
from oabm.importers.pdf_architecture.types import (
    PdfDocumentObservation,
    PdfPageObservation,
)
from oabm.importers.pdf_electrical import PdfPageTransform
from oabm.model import DERIVATION_INFERRED, BuildingModel

REGISTERED = "registered"
REGISTRATION_PENDING = "registration_pending"

_GRID_LABEL_CHARS = frozenset("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789.")
_METHOD_CONFIDENCE = {
    "wall_vectors_and_grid_labels": 0.95,
    "wall_vectors": 0.85,
    "grid_labels": 0.8,
}


_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6",
}


def _level_key(name: str) -> str:
    """Compare level names across disciplines: "Second Floor" == "2nd Floor" == "2"."""

    words = []
    for word in name.lower().replace("-", " ").split():
        if word in {"floor", "level", "plan"}:
            continue
        word = _ORDINALS.get(word, word)
        if word[:-2].isdigit() and word[-2:] in {"st", "nd", "rd", "th"}:
            word = word[:-2]
        words.append(word)
    return " ".join(words)


class SheetRegistrationError(ValueError):
    """The inputs cannot be compared, for example mismatched source documents."""


@dataclass(frozen=True, slots=True)
class SheetRegistrationOptions:
    """Declared tolerances; every refusal names the threshold it failed."""

    tolerance_m: float = 0.05
    min_inliers: int = 8
    min_coverage: float = 0.30
    min_span_m: float = 3.0
    min_span_fraction: float = 0.25
    max_residual_m: float = 0.05
    competing_ratio: float = 0.8
    min_grid_labels: int = 2
    electrical_scale_overrides: tuple[tuple[int, float], ...] = ()
    diagnostic_scale_ratios: tuple[float, ...] = (0.25, 1 / 3, 0.5, 2 / 3, 1.5, 2.0, 3.0, 4.0)

    def __post_init__(self) -> None:
        if self.tolerance_m <= 0 or self.max_residual_m <= 0 or self.min_span_m <= 0:
            raise ValueError("registration tolerances must be positive")
        if self.min_inliers < 2 or self.min_grid_labels < 2:
            raise ValueError("registration needs at least two matched features")
        if not 0 < self.min_coverage <= 1 or not 0 <= self.min_span_fraction <= 1:
            raise ValueError("coverage and span fractions must lie in (0, 1]")
        if not 0 < self.competing_ratio <= 1:
            raise ValueError("competing_ratio must lie in (0, 1]")
        for page, meters_per_point in self.electrical_scale_overrides:
            if page < 1 or not math.isfinite(meters_per_point) or meters_per_point <= 0:
                raise ValueError("electrical scale overrides need a 1-based page and a positive scale")


@dataclass(frozen=True, slots=True)
class PageRegistration:
    page: int
    status: str
    reason_codes: tuple[str, ...]
    transform: PdfPageTransform | None
    record: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ElectricalSheetRegistration:
    electrical_source_id: str
    architecture_model_id: str
    frame_id: str
    pages: tuple[PageRegistration, ...]

    @property
    def all_registered(self) -> bool:
        return bool(self.pages) and all(page.status == REGISTERED for page in self.pages)

    def page_transforms(self) -> dict[int, PdfPageTransform] | None:
        """Every page's transform, or None unless every page is registered.

        One page's transform is never offered for another page or for a partial
        document; the electrical importer then keeps the whole document pending.
        """

        if not self.all_registered:
            return None
        return {page.page: page.transform for page in self.pages if page.transform is not None}

    def to_dict(self) -> dict[str, Any]:
        return {
            "electrical_source_id": self.electrical_source_id,
            "architecture_model_id": self.architecture_model_id,
            "frame_id": self.frame_id,
            "all_registered": self.all_registered,
            "pages": [dict(page.record) for page in self.pages],
        }


@dataclass(frozen=True, slots=True)
class _Target:
    region_id: str
    page: int
    frame_id: str
    level_id: str
    level_name: str
    level_elevation_m: float
    meters_per_point: float
    rotation_radians: float
    translation_m: tuple[float, float]
    confidence: float
    source_page: PdfPageObservation
    bbox_pt: tuple[float, float, float, float]
    grid_labels: Mapping[str, tuple[float, float]]


@dataclass(frozen=True, slots=True)
class _Segment:
    start: tuple[float, float]
    end: tuple[float, float]
    element_id: str

    @property
    def midpoint(self) -> tuple[float, float]:
        return ((self.start[0] + self.end[0]) / 2.0, (self.start[1] + self.end[1]) / 2.0)

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def angle_bucket(self) -> int:
        angle = math.degrees(math.atan2(self.end[1] - self.start[1], self.end[0] - self.start[0])) % 180.0
        return int(angle // 2.0) % 90


@dataclass(frozen=True, slots=True)
class _WallMatch:
    translation_pt: tuple[float, float]
    inliers: tuple[tuple[str, str], ...]
    evidence_count: int
    residual_rms_pt: float
    span_pt: tuple[float, float]

    @property
    def coverage(self) -> float:
        return len(self.inliers) / self.evidence_count if self.evidence_count else 0.0


def _segments(evidence: tuple[RegionEvidence, ...]) -> tuple[_Segment, ...]:
    return tuple(
        _Segment(item.start_pt, item.end_pt, "+".join(item.source_element_ids))
        for item in evidence
        if math.dist(item.start_pt, item.end_pt) > 1e-6
    )


def _map_point(
    point: tuple[float, float],
    scale: float,
    quarter_turns: int,
    mirrored: bool = False,
) -> tuple[float, float]:
    x, y = point
    if mirrored:
        x = -x
    for _ in range(quarter_turns % 4):
        x, y = -y, x
    return (x * scale, y * scale)


def _map_segments(
    segments: tuple[_Segment, ...],
    scale: float,
    quarter_turns: int,
    mirrored: bool = False,
) -> tuple[_Segment, ...]:
    return tuple(
        _Segment(
            _map_point(item.start, scale, quarter_turns, mirrored),
            _map_point(item.end, scale, quarter_turns, mirrored),
            item.element_id,
        )
        for item in segments
    )


# Every mirror and quarter-turn configuration other than the identity.
_ALTERNATIVE_ORIENTATIONS: tuple[tuple[bool, int], ...] = tuple(
    (mirrored, quarter_turns)
    for mirrored in (False, True)
    for quarter_turns in range(4)
    if (mirrored, quarter_turns) != (False, 0)
)


def _vote(
    electrical: tuple[_Segment, ...],
    architecture: tuple[_Segment, ...],
    tolerance_pt: float,
    *,
    limit: int = 8,
) -> list[tuple[float, float]]:
    """Candidate translations from same-orientation, same-length segment pairs."""

    by_bucket: dict[int, list[_Segment]] = {}
    for item in architecture:
        by_bucket.setdefault(item.angle_bucket, []).append(item)
    votes: dict[tuple[int, int], set[int]] = {}
    for index, item in enumerate(electrical):
        length = item.length
        mid = item.midpoint
        for bucket in {(item.angle_bucket + offset) % 90 for offset in (-1, 0, 1)}:
            for other in by_bucket.get(bucket, ()):
                if abs(other.length - length) > 2.0 * tolerance_pt + 0.02 * other.length:
                    continue
                other_mid = other.midpoint
                key = (
                    round((other_mid[0] - mid[0]) / tolerance_pt),
                    round((other_mid[1] - mid[1]) / tolerance_pt),
                )
                votes.setdefault(key, set()).add(index)
    ranked = sorted(votes.items(), key=lambda item: (-len(item[1]), item[0]))
    chosen: list[tuple[int, int]] = []
    for key, _ in ranked:
        if any(abs(key[0] - other[0]) <= 2 and abs(key[1] - other[1]) <= 2 for other in chosen):
            continue
        chosen.append(key)
        if len(chosen) >= limit:
            break
    return [(key[0] * tolerance_pt, key[1] * tolerance_pt) for key in chosen]


def _verify(
    electrical: tuple[_Segment, ...],
    architecture: tuple[_Segment, ...],
    translation: tuple[float, float],
    tolerance_pt: float,
) -> _WallMatch:
    """Count electrical segments whose two endpoints land on one architectural segment."""

    def run(shift: tuple[float, float]) -> tuple[list[tuple[str, str]], list[tuple[float, float]], list[tuple[float, float]]]:
        cell = 2.0 * tolerance_pt
        endpoints: dict[tuple[int, int], list[int]] = {}
        for index, item in enumerate(architecture):
            for point in (item.start, item.end):
                endpoints.setdefault((math.floor(point[0] / cell), math.floor(point[1] / cell)), []).append(index)
        inliers: list[tuple[str, str]] = []
        residuals: list[tuple[float, float]] = []
        points: list[tuple[float, float]] = []
        for item in electrical:
            a = (item.start[0] + shift[0], item.start[1] + shift[1])
            b = (item.end[0] + shift[0], item.end[1] + shift[1])
            cx, cy = math.floor(a[0] / cell), math.floor(a[1] / cell)
            candidates = sorted({
                index
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for index in endpoints.get((cx + dx, cy + dy), ())
            })
            best: tuple[float, str, tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]] | None = None
            for index in candidates:
                other = architecture[index]
                for first, second in ((other.start, other.end), (other.end, other.start)):
                    da, db = math.dist(a, first), math.dist(b, second)
                    if da <= tolerance_pt and db <= tolerance_pt:
                        key = (da + db, other.element_id, first, second, a, b)
                        if best is None or key[:2] < best[:2]:
                            best = key
            if best is None:
                continue
            _, element_id, first, second, a_point, b_point = best
            inliers.append((item.element_id, element_id))
            residuals.extend((
                (first[0] - a_point[0], first[1] - a_point[1]),
                (second[0] - b_point[0], second[1] - b_point[1]),
            ))
            points.extend((first, second))
        return inliers, residuals, points

    inliers, residuals, _ = run(translation)
    if residuals:
        translation = (
            translation[0] + sum(item[0] for item in residuals) / len(residuals),
            translation[1] + sum(item[1] for item in residuals) / len(residuals),
        )
        inliers, residuals, points = run(translation)
    else:
        points = []
    rms = math.sqrt(sum(dx * dx + dy * dy for dx, dy in residuals) / len(residuals)) if residuals else math.inf
    span = (
        (max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points))
        if points else (0.0, 0.0)
    )
    return _WallMatch(
        translation_pt=translation,
        inliers=tuple(sorted(inliers)),
        evidence_count=len(electrical),
        residual_rms_pt=rms,
        span_pt=span,
    )


def _wall_failures(
    match: _WallMatch,
    target: _Target,
    target_span_pt: tuple[float, float],
    options: SheetRegistrationOptions,
) -> list[str]:
    reasons: list[str] = []
    if len(match.inliers) < options.min_inliers or match.coverage < options.min_coverage:
        reasons.append("insufficient_matched_evidence")
        return reasons
    if match.residual_rms_pt * target.meters_per_point > options.max_residual_m:
        reasons.append("excessive_residual")
    for axis in (0, 1):
        needed_m = max(options.min_span_m, options.min_span_fraction * target_span_pt[axis] * target.meters_per_point)
        if match.span_pt[axis] * target.meters_per_point < needed_m:
            reasons.append("evidence_clustered")
            break
    return reasons


def _span(segments: tuple[_Segment, ...]) -> tuple[float, float]:
    if not segments:
        return (0.0, 0.0)
    xs = [value for item in segments for value in (item.start[0], item.end[0])]
    ys = [value for item in segments for value in (item.start[1], item.end[1])]
    return (max(xs) - min(xs), max(ys) - min(ys))


def _match_walls(
    electrical: tuple[_Segment, ...],
    architecture: tuple[_Segment, ...],
    target: _Target,
    scale: float,
    quarter_turns: int,
    options: SheetRegistrationOptions,
    mirrored: bool = False,
) -> tuple[_WallMatch | None, list[str], _WallMatch | None]:
    """Best accepted wall match, its failure reasons, and a competing runner-up."""

    tolerance_pt = options.tolerance_m / target.meters_per_point
    mapped = _map_segments(electrical, scale, quarter_turns, mirrored)
    candidates = [
        _verify(mapped, architecture, translation, tolerance_pt)
        for translation in _vote(mapped, architecture, tolerance_pt)
    ]
    if not candidates:
        return None, ["insufficient_matched_evidence"], None
    target_span = _span(architecture)
    ranked = sorted(
        candidates,
        key=lambda item: (-len(item.inliers), item.residual_rms_pt, item.translation_pt),
    )
    best = ranked[0]
    failures = _wall_failures(best, target, target_span, options)
    runner_up = next(
        (
            item for item in ranked[1:]
            if math.dist(item.translation_pt, best.translation_pt) > 2.0 * tolerance_pt
            and not _wall_failures(item, target, target_span, options)
            and len(item.inliers) >= options.competing_ratio * len(best.inliers)
        ),
        None,
    )
    if not failures and runner_up is not None:
        failures = ["competing_transforms"]
    return best, failures, runner_up


def _best_alternative_orientation(
    electrical: tuple[_Segment, ...],
    architecture: tuple[_Segment, ...],
    target: _Target,
    scale: float,
    options: SheetRegistrationOptions,
) -> tuple[int, bool, int]:
    """Most wall segments any mirrored or rotated placement explains.

    Symmetric walls can match a flipped or turned sheet almost as well as the
    true placement. The identity match is trusted only when every alternative
    explains strictly fewer electrical wall segments.
    """

    tolerance_pt = options.tolerance_m / target.meters_per_point
    best = (0, False, 0)
    for mirrored, quarter_turns in _ALTERNATIVE_ORIENTATIONS:
        mapped = _map_segments(electrical, scale, quarter_turns, mirrored)
        for translation in _vote(mapped, architecture, tolerance_pt):
            count = len(_verify(mapped, architecture, translation, tolerance_pt).inliers)
            if count > best[0]:
                best = (count, mirrored, quarter_turns)
    return best


def _grid_labels_against(
    translation: tuple[float, float],
    electrical: Mapping[str, tuple[float, float]],
    target: _Target,
    scale: float,
    tolerance_pt: float,
) -> tuple[list[str], list[str]]:
    """Shared grid labels that do and do not land on their architectural bubble."""

    agreeing: list[str] = []
    disagreeing: list[str] = []
    for label in sorted(set(electrical) & set(target.grid_labels)):
        x, y = electrical[label]
        mapped = (x * scale + translation[0], y * scale + translation[1])
        if math.dist(mapped, target.grid_labels[label]) <= 2.0 * tolerance_pt:
            agreeing.append(label)
        else:
            disagreeing.append(label)
    return agreeing, disagreeing


def _grid_bubbles(page: PdfPageObservation) -> dict[str, tuple[float, float]]:
    """Short labels enclosed by a drawn ring; a label seen twice is dropped."""

    found: dict[str, list[tuple[float, float]]] = {}
    cell = 40.0
    near: dict[tuple[int, int], list[int]] = {}
    for index, line in enumerate(page.lines):
        keys = {
            (math.floor(point[0] / cell), math.floor(point[1] / cell))
            for point in (line.start_pt, line.end_pt)
        }
        for key in keys:
            near.setdefault(key, []).append(index)
    for text in page.texts:
        label = text.text.strip().upper()
        if not 1 <= len(label) <= 3 or not set(label) <= _GRID_LABEL_CHARS or label.startswith("."):
            continue
        center = text.center_pt
        distances: list[float] = []
        angles: list[float] = []
        cx, cy = math.floor(center[0] / cell), math.floor(center[1] / cell)
        indexes = sorted({
            index
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for index in near.get((cx + dx, cy + dy), ())
        })
        for line in (page.lines[index] for index in indexes):
            ends = [math.dist(center, point) for point in (line.start_pt, line.end_pt)]
            if not all(4.0 <= value <= 40.0 for value in ends):
                continue
            if abs(ends[0] - ends[1]) > 0.15 * max(ends):
                continue
            distances.extend(ends)
            angles.extend(
                math.atan2(point[1] - center[1], point[0] - center[0])
                for point in (line.start_pt, line.end_pt)
            )
        if len(distances) < 6:
            continue
        radius = median(distances)
        if any(abs(value - radius) > 0.15 * radius for value in distances):
            continue
        ordered = sorted(angle % (2.0 * math.pi) for angle in angles)
        gaps = [second - first for first, second in zip(ordered, ordered[1:])]
        gaps.append(ordered[0] + 2.0 * math.pi - ordered[-1])
        if max(gaps) >= math.pi:
            continue
        found.setdefault(label, []).append(center)
    return {label: points[0] for label, points in sorted(found.items()) if len(points) == 1}


def _match_grid(
    electrical: Mapping[str, tuple[float, float]],
    target: _Target,
    scale: float,
    options: SheetRegistrationOptions,
) -> tuple[tuple[float, float] | None, tuple[str, ...], float, list[str]]:
    """Translation from shared grid labels; every shared label must agree."""

    shared = sorted(set(electrical) & set(target.grid_labels))
    if not electrical or not target.grid_labels:
        return None, (), math.inf, ["missing_grid_evidence"]
    if len(shared) < options.min_grid_labels:
        return None, tuple(shared), math.inf, ["insufficient_grid_labels"]
    tolerance_pt = options.tolerance_m / target.meters_per_point
    shifts = {
        label: (
            target.grid_labels[label][0] - electrical[label][0] * scale,
            target.grid_labels[label][1] - electrical[label][1] * scale,
        )
        for label in shared
    }
    center = (median(value[0] for value in shifts.values()), median(value[1] for value in shifts.values()))
    if any(math.dist(value, center) > tolerance_pt for value in shifts.values()):
        return None, tuple(shared), math.inf, ["grid_labels_inconsistent"]
    translation = (
        sum(value[0] for value in shifts.values()) / len(shifts),
        sum(value[1] for value in shifts.values()) / len(shifts),
    )
    residual = math.sqrt(sum(math.dist(value, translation) ** 2 for value in shifts.values()) / len(shifts))
    points = [target.grid_labels[label] for label in shared]
    spread = max(
        max(p[0] for p in points) - min(p[0] for p in points),
        max(p[1] for p in points) - min(p[1] for p in points),
    )
    if spread * target.meters_per_point < options.min_span_m:
        return None, tuple(shared), residual, ["evidence_clustered"]
    return translation, tuple(shared), residual, []


def _compose(target: _Target, scale: float, translation_pt: tuple[float, float]) -> dict[str, float]:
    """Electrical displayed point -> architecture sheet point -> canonical frame."""

    c, s = math.cos(target.rotation_radians), math.sin(target.rotation_radians)
    m = target.meters_per_point
    tx, ty = translation_pt
    return {
        "m11_m_per_pt": m * scale * c,
        "m12_m_per_pt": -m * scale * s,
        "m21_m_per_pt": m * scale * s,
        "m22_m_per_pt": m * scale * c,
        "tx_m": m * (c * tx - s * ty) + target.translation_m[0],
        "ty_m": m * (s * tx + c * ty) + target.translation_m[1],
        "z_m": target.level_elevation_m,
    }


def _apply(coefficients: Mapping[str, float], point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    return (
        coefficients["m11_m_per_pt"] * x + coefficients["m12_m_per_pt"] * y + coefficients["tx_m"],
        coefficients["m21_m_per_pt"] * x + coefficients["m22_m_per_pt"] * y + coefficients["ty_m"],
    )


def _nearest_region_labels(
    page: PdfPageObservation,
    record: Mapping[str, Any],
    page_records: list[Mapping[str, Any]],
    meters_per_point: float,
) -> dict[str, tuple[float, float]]:
    labels = _grid_bubbles(page)
    if record.get("scope") == "sheet":
        return labels
    reach = max(72.0, 3.0 / meters_per_point)

    def distance(bbox: list[float], point: tuple[float, float]) -> float:
        dx = max(bbox[0] - point[0], 0.0, point[0] - bbox[2])
        dy = max(bbox[1] - point[1], 0.0, point[1] - bbox[3])
        return math.hypot(dx, dy)

    result: dict[str, tuple[float, float]] = {}
    for label, point in labels.items():
        ranked = sorted(
            (distance(item["source_bbox_pt"], point), item["region_id"])
            for item in page_records
            if item.get("source_bbox_pt")
        )
        if not ranked or ranked[0][1] != record["region_id"] or ranked[0][0] > reach:
            continue
        if len(ranked) > 1 and ranked[1][0] < 2.0 * ranked[0][0] + 18.0:
            continue
        result[label] = point
    return result


def _targets(
    architecture: BuildingModel,
    architecture_source: PdfDocumentObservation,
) -> tuple[_Target, ...]:
    lane = architecture.attributes.get("pdf_architecture")
    if not isinstance(lane, Mapping) or "drawing_regions" not in lane:
        raise SheetRegistrationError(
            "architecture model carries no pdf_architecture drawing regions (#103)"
        )
    if lane.get("content_sha256") != architecture_source.content_sha256:
        raise SheetRegistrationError(
            "architecture source observations are not from the PDF that produced the model"
        )
    pages = {page.page_number: page for page in architecture_source.pages}
    levels = {level.id: level for level in architecture.levels}
    records = [item for item in lane["drawing_regions"] if isinstance(item, Mapping)]
    targets: list[_Target] = []
    for record in records:
        frame = record.get("frame")
        if record.get("status") != "resolved" or not isinstance(frame, Mapping):
            continue
        if frame.get("frame_id") != architecture.coordinate_system.frame_id:
            raise SheetRegistrationError(
                f"drawing region {record['region_id']} targets a frame other than the model frame"
            )
        level = levels.get(record["level"]["level_id"])
        if level is None:
            raise SheetRegistrationError(
                f"drawing region {record['region_id']} references a level missing from the model"
            )
        page = pages[int(record["page"])]
        mpp = float(frame["meters_per_point"])
        targets.append(
            _Target(
                region_id=str(record["region_id"]),
                page=int(record["page"]),
                frame_id=str(frame["frame_id"]),
                level_id=level.id,
                level_name=level.name,
                level_elevation_m=level.elevation_m,
                meters_per_point=mpp,
                rotation_radians=float(frame["rotation_radians"]),
                translation_m=(float(frame["translation_m"][0]), float(frame["translation_m"][1])),
                confidence=float(record.get("confidence") or frame.get("confidence") or 0.0),
                source_page=page,
                bbox_pt=(
                    tuple(record["source_bbox_pt"])  # type: ignore[arg-type]
                    if record.get("source_bbox_pt")
                    else (0.0, 0.0, page.width_pt, page.height_pt)
                ),
                grid_labels=_nearest_region_labels(
                    page,
                    record,
                    [item for item in records if int(item["page"]) == page.page_number],
                    mpp,
                ),
            )
        )
    return tuple(sorted(targets, key=lambda item: item.region_id))


class _EvidenceCache:
    """Compute each sheet's wall evidence once per layer mode."""

    def __init__(self, electrical_page: PdfPageObservation, electrical_mpp: float) -> None:
        self.electrical_page = electrical_page
        self.electrical_mpp = electrical_mpp
        self._electrical: dict[bool, tuple[str, tuple[tuple[tuple[float, float, float, float], tuple[_Segment, ...]], ...]]] = {}
        self._architecture: dict[tuple[str, bool], tuple[str, tuple[_Segment, ...]]] = {}

    def electrical(self, use_layers: bool) -> tuple[str, tuple[tuple[tuple[float, float, float, float], tuple[_Segment, ...]], ...]]:
        if use_layers not in self._electrical:
            view = sheet_wall_evidence(
                self.electrical_page,
                meters_per_point=self.electrical_mpp,
                use_wall_layers=use_layers,
            )
            self._electrical[use_layers] = (
                view.evidence_kind,
                tuple((bbox, _segments(items)) for bbox, items in view.drawings),
            )
        return self._electrical[use_layers]

    def architecture(self, target: _Target, use_layers: bool) -> tuple[str, tuple[_Segment, ...]]:
        key = (target.region_id, use_layers)
        if key not in self._architecture:
            kind, evidence = region_wall_evidence(
                target.source_page, target.bbox_pt, target.meters_per_point, use_wall_layers=use_layers,
            )
            self._architecture[key] = (kind, _segments(evidence))
        return self._architecture[key]


def _evaluate_target(
    target: _Target,
    drawing_index: int | None,
    cache: _EvidenceCache,
    electrical_labels: Mapping[str, tuple[float, float]],
    options: SheetRegistrationOptions,
    electrical_level_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Match one electrical drawing against one architectural region."""

    scale = cache.electrical_mpp / target.meters_per_point
    walls: tuple[_Segment, ...] = ()
    architecture_walls: tuple[_Segment, ...] = ()
    evidence_kind = None
    if drawing_index is not None:
        # Compare like with like: wall-layer faces on both sheets when both have
        # them, otherwise paired wall faces on both sheets.
        for use_layers in (True, False):
            electrical_kind, drawings = cache.electrical(use_layers)
            architecture_kind, architecture_segments = cache.architecture(target, use_layers)
            if (
                electrical_kind == architecture_kind
                and drawing_index < len(drawings)
                and drawings[drawing_index][1]
                and architecture_segments
            ):
                walls = drawings[drawing_index][1]
                architecture_walls = architecture_segments
                evidence_kind = architecture_kind
                break
    wall_match: _WallMatch | None = None
    wall_reasons = ["missing_wall_evidence"]
    runner_up: _WallMatch | None = None
    orientation_alternative: dict[str, Any] | None = None
    if walls and architecture_walls:
        wall_match, wall_reasons, runner_up = _match_walls(
            walls, architecture_walls, target, scale, 0, options,
        )
        if wall_match is not None and not wall_reasons:
            count, mirrored, quarter_turns = _best_alternative_orientation(
                walls, architecture_walls, target, scale, options,
            )
            orientation_alternative = {
                "mirrored": mirrored,
                "rotation_degrees": 90 * quarter_turns,
                "wall_inlier_count": count,
            }
            if count > len(wall_match.inliers):
                wall_reasons = ["orientation_incompatible"]
            elif count == len(wall_match.inliers):
                wall_reasons = ["ambiguous_orientation"]
    grid_translation, grid_shared, grid_residual, grid_reasons = _match_grid(
        electrical_labels, target, scale, options,
    )
    tolerance_pt = options.tolerance_m / target.meters_per_point
    wall_ok = wall_match is not None and not wall_reasons
    wall_tied = wall_match is not None and wall_reasons == ["ambiguous_orientation"]
    grid_ok = grid_translation is not None and not grid_reasons
    method = None
    translation = None
    reasons: list[str] = []
    grid_outliers: list[str] = []
    if wall_ok or wall_tied:
        # Explicit grid labels must agree with the wall placement. One stray
        # label (a keynote tag that happens to share a grid label) is tolerated
        # when at least two others agree; any larger contradiction refuses.
        assert wall_match is not None
        agreeing, disagreeing = _grid_labels_against(
            wall_match.translation_pt, electrical_labels, target, scale, tolerance_pt,
        )
        if len(agreeing) + len(disagreeing) >= options.min_grid_labels:
            if len(disagreeing) >= 2 or (disagreeing and len(agreeing) < 2):
                reasons = (
                    ["grid_labels_inconsistent"]
                    if "grid_labels_inconsistent" in grid_reasons
                    else ["registration_methods_disagree"]
                )
            else:
                method, translation = "wall_vectors_and_grid_labels", wall_match.translation_pt
                grid_outliers = disagreeing
        elif wall_ok:
            method, translation = "wall_vectors", wall_match.translation_pt
        else:
            reasons = ["ambiguous_orientation"]
    elif wall_match is not None and wall_reasons == ["orientation_incompatible"]:
        # A better mirrored or turned wall placement is itself a contradiction;
        # grid labels do not override it.
        reasons = ["orientation_incompatible"]
    elif grid_ok:
        method, translation = "grid_labels", grid_translation
    if (
        method is not None
        and len(electrical_level_names) == 1
        and target.level_name != "Unlabeled Level"
        and _level_key(electrical_level_names[0]) != _level_key(target.level_name)
    ):
        # The sheets themselves name different levels; do not place one on the other.
        method, translation, reasons = None, None, ["level_name_mismatch"]
    if method is None and not reasons:
        reasons = sorted(set(wall_reasons) | set(grid_reasons))
        if {"missing_wall_evidence", "missing_grid_evidence"} <= set(reasons):
            reasons = ["missing_registration_evidence"]
        else:
            reasons = [code for code in reasons if code not in {"missing_wall_evidence", "missing_grid_evidence"}]
    result: dict[str, Any] = {
        "target_region_id": target.region_id,
        "target_page": target.page,
        "level_id": target.level_id,
        "scale_ratio": scale,
        "accepted": method is not None,
        "method": method,
        "reason_codes": reasons,
        "wall_evidence_kind": evidence_kind,
        "electrical_wall_segment_count": len(walls),
        "architecture_wall_segment_count": len(architecture_walls),
        "wall_inlier_count": len(wall_match.inliers) if wall_match else 0,
        "wall_coverage": round(wall_match.coverage, 6) if wall_match else 0.0,
        "wall_residual_rms_m": (
            round(wall_match.residual_rms_pt * target.meters_per_point, 9)
            if wall_match and math.isfinite(wall_match.residual_rms_pt) else None
        ),
        "wall_inlier_span_m": (
            [round(value * target.meters_per_point, 6) for value in wall_match.span_pt]
            if wall_match else None
        ),
        "competing_translation_pt": (
            [round(value, 6) for value in runner_up.translation_pt] if runner_up else None
        ),
        "competing_inlier_count": len(runner_up.inliers) if runner_up else 0,
        "grid_labels_shared": list(grid_shared),
        "grid_label_outliers": grid_outliers,
        "electrical_level_names": list(electrical_level_names),
        "target_level_name": target.level_name,
        "orientation_alternative": orientation_alternative,
        "grid_residual_rms_m": (
            round(grid_residual * target.meters_per_point, 9) if math.isfinite(grid_residual) else None
        ),
        "_translation_pt": translation,
        "_wall_match": wall_match,
        "_target": target,
        "_walls": walls,
        "_architecture_walls": architecture_walls,
    }
    return result


def _diagnose(
    best: Mapping[str, Any],
    options: SheetRegistrationOptions,
) -> tuple[list[str], dict[str, Any]]:
    """Would the walls match under a rotation or another scale? Diagnostics only."""

    target: _Target = best["_target"]
    walls: tuple[_Segment, ...] = best["_walls"]
    architecture_walls: tuple[_Segment, ...] = best["_architecture_walls"]
    scale = float(best["scale_ratio"])
    if not walls or not architecture_walls:
        return [], {}
    rotations = []
    for mirrored, quarter_turns in _ALTERNATIVE_ORIENTATIONS:
        match, failures, _ = _match_walls(
            walls, architecture_walls, target, scale, quarter_turns, options, mirrored,
        )
        if match is not None and not failures:
            rotations.append((-len(match.inliers), mirrored, quarter_turns))
    if rotations:
        _, mirrored, quarter_turns = min(rotations)
        return ["orientation_incompatible"], {
            "matching_rotation_degrees": 90 * quarter_turns,
            "matching_mirrored": mirrored,
        }
    ratios = []
    for ratio in options.diagnostic_scale_ratios:
        match, failures, _ = _match_walls(walls, architecture_walls, target, scale * ratio, 0, options)
        if match is not None and not failures:
            ratios.append((-len(match.inliers), ratio))
    if ratios:
        return ["scale_incompatible"], {"matching_scale_ratio_to_printed": min(ratios)[1]}
    return [], {}


def _page_record_base(page: PdfPageObservation) -> dict[str, Any]:
    return {"page": page.page_number, "status": REGISTRATION_PENDING, "reason_codes": []}


def _public(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if not key.startswith("_")}


def _register_page(
    page: PdfPageObservation,
    targets: tuple[_Target, ...],
    options: SheetRegistrationOptions,
) -> PageRegistration:
    record = _page_record_base(page)
    overrides = dict(options.electrical_scale_overrides)
    if page.page_number in overrides:
        electrical_mpp = overrides[page.page_number]
        record["electrical_scale"] = {
            "meters_per_point": electrical_mpp,
            "method": "caller-supplied electrical sheet scale",
        }
    else:
        printed = printed_sheet_scale(page)
        if printed is None:
            record["reason_codes"] = ["scale_unresolved"]
            return PageRegistration(page.page_number, REGISTRATION_PENDING, ("scale_unresolved",), None, record)
        electrical_mpp = printed[0]
        record["electrical_scale"] = {
            "meters_per_point": printed[0],
            "method": "parsed printed scale annotation",
            "source_text": printed[1],
            "source_element_id": printed[2],
        }
    if not targets:
        record["reason_codes"] = ["no_resolved_architectural_region"]
        return PageRegistration(
            page.page_number, REGISTRATION_PENDING, ("no_resolved_architectural_region",), None, record,
        )

    cache = _EvidenceCache(page, electrical_mpp)
    wall_kind, drawings = cache.electrical(True)
    labels = _grid_bubbles(page)
    level_names = drawing_level_names(page)
    record["evidence"] = {
        "wall_kind": wall_kind,
        "drawing_count": len(drawings),
        "wall_segment_count": sum(len(items) for _, items in drawings),
        "grid_labels": sorted(labels),
    }
    if not drawings and not labels:
        record["reason_codes"] = ["missing_registration_evidence"]
        record["bounded_vision_question"] = (
            "No wall vectors or grid bubbles were found on this electrical page. A later "
            "#98 vision task may locate two or more control points it shares with one "
            "resolved architectural drawing region; nothing was inferred here."
        )
        return PageRegistration(
            page.page_number, REGISTRATION_PENDING, ("missing_registration_evidence",), None, record,
        )

    drawing_boxes: list[tuple[float, float, float, float] | None] = [bbox for bbox, _ in drawings] or [None]
    per_drawing: list[list[dict[str, Any]]] = []
    for index, bbox in enumerate(drawing_boxes):
        per_drawing.append([
            _evaluate_target(
                target,
                index if bbox is not None else None,
                cache,
                labels,
                options,
                level_names if len(drawing_boxes) == 1 else (),
            )
            for target in targets
        ])
    if len(drawing_boxes) > 1:
        record["reason_codes"] = ["multiple_drawing_regions_on_page"]
        record["drawings"] = [
            {
                "source_bbox_pt": list(bbox) if bbox else None,
                "candidates": [_public(item) for item in candidates],
            }
            for bbox, candidates in zip(drawing_boxes, per_drawing)
        ]
        return PageRegistration(
            page.page_number, REGISTRATION_PENDING, ("multiple_drawing_regions_on_page",), None, record,
        )

    candidates = per_drawing[0]
    record["candidates"] = [_public(item) for item in candidates]
    accepted = [item for item in candidates if item["accepted"]]
    if not accepted:
        best = sorted(
            candidates,
            key=lambda item: (-item["wall_inlier_count"], -len(item["grid_labels_shared"]), item["target_region_id"]),
        )[0]
        reasons = list(best["reason_codes"])
        diagnostic_codes, diagnostics = (
            _diagnose(best, options)
            if reasons == ["insufficient_matched_evidence"]
            else ([], {})
        )
        if diagnostic_codes:
            reasons = diagnostic_codes
            record["diagnostics"] = {"target_region_id": best["target_region_id"], **diagnostics}
        elif best.get("orientation_alternative") and set(reasons) & {
            "orientation_incompatible", "ambiguous_orientation",
        }:
            record["diagnostics"] = {
                "target_region_id": best["target_region_id"],
                "identity_wall_inlier_count": best["wall_inlier_count"],
                "alternative": best["orientation_alternative"],
            }
        record["reason_codes"] = sorted(set(reasons)) or ["insufficient_matched_evidence"]
        return PageRegistration(
            page.page_number, REGISTRATION_PENDING, tuple(record["reason_codes"]), None, record,
        )

    composed = {
        item["target_region_id"]: _compose(item["_target"], item["scale_ratio"], item["_translation_pt"])
        for item in accepted
    }
    ranked = sorted(
        accepted,
        key=lambda item: (-item["wall_inlier_count"], -len(item["grid_labels_shared"]), item["target_region_id"]),
    )
    chosen = ranked[0]
    probe_box = drawing_boxes[0] or (0.0, 0.0, 1.0, 1.0)
    corners = [
        (probe_box[0], probe_box[1]), (probe_box[2], probe_box[1]),
        (probe_box[2], probe_box[3]), (probe_box[0], probe_box[3]),
    ]
    chosen_coefficients = composed[chosen["target_region_id"]]
    agreeing = []
    disagreeing = []
    for item in ranked:
        coefficients = composed[item["target_region_id"]]
        same_level = item["level_id"] == chosen["level_id"]
        same_place = all(
            math.dist(_apply(coefficients, corner), _apply(chosen_coefficients, corner)) <= options.tolerance_m
            for corner in corners
        )
        (agreeing if same_level and same_place else disagreeing).append(item["target_region_id"])
    if disagreeing:
        record["reason_codes"] = ["competing_targets"]
        record["competing_region_ids"] = sorted(agreeing + disagreeing)
        return PageRegistration(page.page_number, REGISTRATION_PENDING, ("competing_targets",), None, record)

    target: _Target = chosen["_target"]
    wall_match: _WallMatch | None = chosen["_wall_match"]
    confidence = round(min(target.confidence, _METHOD_CONFIDENCE[chosen["method"]]), 6)
    registration = {
        "method": f"sheet registration by {chosen['method'].replace('_', ' ')}",
        "derivation": DERIVATION_INFERRED,
        "evidence_method": chosen["method"],
        "target_region_id": target.region_id,
        "agreeing_region_ids": sorted(agreeing),
        "target_page": target.page,
        "level_id": target.level_id,
        "level_elevation_m": target.level_elevation_m,
        "scale_ratio": chosen["scale_ratio"],
        "rotation_degrees": 0,
        "translation_pt": [round(value, 9) for value in chosen["_translation_pt"]],
        "wall_inlier_count": chosen["wall_inlier_count"],
        "wall_coverage": chosen["wall_coverage"],
        "wall_residual_rms_m": chosen["wall_residual_rms_m"],
        "wall_inlier_span_m": chosen["wall_inlier_span_m"],
        "grid_labels_shared": chosen["grid_labels_shared"],
        "matched_evidence_sample": [list(pair) for pair in (wall_match.inliers[:20] if wall_match else ())],
        "tolerance_m": options.tolerance_m,
        "confidence": confidence,
    }
    coefficients = {key: round(value, 12) for key, value in chosen_coefficients.items()}
    transform = PdfPageTransform(frame_id=target.frame_id, registration=registration, **coefficients)
    record.update(
        {
            "status": REGISTERED,
            "reason_codes": [],
            "registration": registration,
            "page_transform": transform.to_attributes(),
        }
    )
    return PageRegistration(page.page_number, REGISTERED, (), transform, record)


def register_electrical_sheets(
    architecture: BuildingModel,
    architecture_source: PdfDocumentObservation,
    electrical_source: PdfDocumentObservation,
    *,
    options: SheetRegistrationOptions | None = None,
) -> ElectricalSheetRegistration:
    """Propose each electrical page's transform into a resolved architectural frame.

    ``architecture_source`` must be the observation document that produced
    ``architecture``; ``electrical_source`` is the same extraction applied to the
    electrical PDF. Neither model is modified.
    """

    options = options or SheetRegistrationOptions()
    targets = _targets(architecture, architecture_source)
    pages = tuple(
        _register_page(page, targets, options)
        for page in sorted(electrical_source.pages, key=lambda item: item.page_number)
    )
    return ElectricalSheetRegistration(
        electrical_source_id=electrical_source.source_id,
        architecture_model_id=architecture.model_id,
        frame_id=architecture.coordinate_system.frame_id,
        pages=pages,
    )


def register_electrical_pdf(
    architecture: BuildingModel,
    architecture_pdf: str | Path,
    electrical_pdf: str | Path,
    *,
    options: SheetRegistrationOptions | None = None,
) -> ElectricalSheetRegistration:
    """Convenience wrapper that extracts both PDFs' sheet observations first."""

    return register_electrical_sheets(
        architecture,
        extract_sheet_observations(architecture_pdf),
        extract_sheet_observations(electrical_pdf),
        options=options,
    )
