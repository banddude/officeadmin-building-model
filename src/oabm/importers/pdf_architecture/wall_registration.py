"""Shared deterministic wall-evidence matcher for sheet registration.

Both registration consumers match one sheet's wall vectors against another
sheet's wall vectors at the ratio of the two printed scales and no rotation:

- #104 registers electrical sheets against resolved architectural drawing
  regions;
- the architecture lane registers a later architectural sheet or region
  against an already-resolved region of the same level before it would
  otherwise fall back to the lower-left sheet-geometry frame.

This module owns only evidence matching: candidate translation voting,
endpoint verification, threshold checks, competing-translation uniqueness,
and the mirrored/rotated orientation guard. It never touches PDFs, models,
or frames; callers supply wall segments in sheet points and interpret the
accepted translation themselves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .drawing_regions import RegionEvidence

# Every mirror and quarter-turn configuration other than the identity.
ALTERNATIVE_ORIENTATIONS: tuple[tuple[bool, int], ...] = tuple(
    (mirrored, quarter_turns)
    for mirrored in (False, True)
    for quarter_turns in range(4)
    if (mirrored, quarter_turns) != (False, 0)
)


@dataclass(frozen=True, slots=True)
class WallMatchOptions:
    """Declared matching thresholds; every refusal names the check it failed."""

    tolerance_m: float = 0.05
    min_inliers: int = 8
    min_coverage: float = 0.30
    min_span_m: float = 3.0
    min_span_fraction: float = 0.25
    max_residual_m: float = 0.05
    competing_ratio: float = 0.8

    def __post_init__(self) -> None:
        if self.tolerance_m <= 0 or self.max_residual_m <= 0 or self.min_span_m <= 0:
            raise ValueError("matching tolerances must be positive")
        if self.min_inliers < 2:
            raise ValueError("matching needs at least two matched features")
        if not 0 < self.min_coverage <= 1 or not 0 <= self.min_span_fraction <= 1:
            raise ValueError("coverage and span fractions must lie in (0, 1]")
        if not 0 < self.competing_ratio <= 1:
            raise ValueError("competing_ratio must lie in (0, 1]")


DEFAULT_WALL_MATCH_OPTIONS = WallMatchOptions()


@dataclass(frozen=True, slots=True)
class MatchSegment:
    """One wall-evidence segment in sheet-local points."""

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
class WallMatch:
    """One verified translation of the source walls onto the target walls."""

    translation_pt: tuple[float, float]
    inliers: tuple[tuple[str, str], ...]
    evidence_count: int
    residual_rms_pt: float
    span_pt: tuple[float, float]

    @property
    def coverage(self) -> float:
        return len(self.inliers) / self.evidence_count if self.evidence_count else 0.0


def segments_from_evidence(evidence: tuple[RegionEvidence, ...]) -> tuple[MatchSegment, ...]:
    """Match segments for wall evidence; degenerate segments are dropped."""

    return tuple(
        MatchSegment(item.start_pt, item.end_pt, "+".join(item.source_element_ids))
        for item in evidence
        if math.dist(item.start_pt, item.end_pt) > 1e-6
    )


def map_point(
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


def map_segments(
    segments: tuple[MatchSegment, ...],
    scale: float,
    quarter_turns: int,
    mirrored: bool = False,
) -> tuple[MatchSegment, ...]:
    return tuple(
        MatchSegment(
            map_point(item.start, scale, quarter_turns, mirrored),
            map_point(item.end, scale, quarter_turns, mirrored),
            item.element_id,
        )
        for item in segments
    )


def span(segments: tuple[MatchSegment, ...]) -> tuple[float, float]:
    if not segments:
        return (0.0, 0.0)
    xs = [value for item in segments for value in (item.start[0], item.end[0])]
    ys = [value for item in segments for value in (item.start[1], item.end[1])]
    return (max(xs) - min(xs), max(ys) - min(ys))


def vote(
    source: tuple[MatchSegment, ...],
    target: tuple[MatchSegment, ...],
    tolerance_pt: float,
    *,
    limit: int = 8,
) -> list[tuple[float, float]]:
    """Candidate translations from same-orientation, same-length segment pairs."""

    by_bucket: dict[int, list[MatchSegment]] = {}
    for item in target:
        by_bucket.setdefault(item.angle_bucket, []).append(item)
    votes: dict[tuple[int, int], set[int]] = {}
    for index, item in enumerate(source):
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


def verify(
    source: tuple[MatchSegment, ...],
    target: tuple[MatchSegment, ...],
    translation: tuple[float, float],
    tolerance_pt: float,
) -> WallMatch:
    """Count source segments whose two endpoints land on one target segment."""

    def run(shift: tuple[float, float]) -> tuple[list[tuple[str, str]], list[tuple[float, float]], list[tuple[float, float]]]:
        cell = 2.0 * tolerance_pt
        endpoints: dict[tuple[int, int], list[int]] = {}
        for index, item in enumerate(target):
            for point in (item.start, item.end):
                endpoints.setdefault((math.floor(point[0] / cell), math.floor(point[1] / cell)), []).append(index)
        inliers: list[tuple[str, str]] = []
        residuals: list[tuple[float, float]] = []
        points: list[tuple[float, float]] = []
        for item in source:
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
                other = target[index]
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
    matched_span = (
        (max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points))
        if points else (0.0, 0.0)
    )
    return WallMatch(
        translation_pt=translation,
        inliers=tuple(sorted(inliers)),
        evidence_count=len(source),
        residual_rms_pt=rms,
        span_pt=matched_span,
    )


def wall_failures(
    match: WallMatch,
    *,
    target_meters_per_point: float,
    target_span_pt: tuple[float, float],
    options: WallMatchOptions,
) -> list[str]:
    """Threshold failures for one match: evidence, residual, and spread."""

    reasons: list[str] = []
    if len(match.inliers) < options.min_inliers or match.coverage < options.min_coverage:
        reasons.append("insufficient_matched_evidence")
        return reasons
    if match.residual_rms_pt * target_meters_per_point > options.max_residual_m:
        reasons.append("excessive_residual")
    for axis in (0, 1):
        needed_m = max(
            options.min_span_m,
            options.min_span_fraction * target_span_pt[axis] * target_meters_per_point,
        )
        if match.span_pt[axis] * target_meters_per_point < needed_m:
            reasons.append("evidence_clustered")
            break
    return reasons


def match_walls(
    source: tuple[MatchSegment, ...],
    target: tuple[MatchSegment, ...],
    *,
    scale: float,
    target_meters_per_point: float,
    options: WallMatchOptions = DEFAULT_WALL_MATCH_OPTIONS,
    quarter_turns: int = 0,
    mirrored: bool = False,
) -> tuple[WallMatch | None, list[str], WallMatch | None]:
    """Best accepted wall match, its failure reasons, and a competing runner-up."""

    tolerance_pt = options.tolerance_m / target_meters_per_point
    mapped = map_segments(source, scale, quarter_turns, mirrored)
    candidates = [
        verify(mapped, target, translation, tolerance_pt)
        for translation in vote(mapped, target, tolerance_pt)
    ]
    if not candidates:
        return None, ["insufficient_matched_evidence"], None
    target_span = span(target)
    ranked = sorted(
        candidates,
        key=lambda item: (-len(item.inliers), item.residual_rms_pt, item.translation_pt),
    )
    best = ranked[0]
    failures = wall_failures(
        best,
        target_meters_per_point=target_meters_per_point,
        target_span_pt=target_span,
        options=options,
    )
    runner_up = next(
        (
            item for item in ranked[1:]
            if math.dist(item.translation_pt, best.translation_pt) > 2.0 * tolerance_pt
            and not wall_failures(
                item,
                target_meters_per_point=target_meters_per_point,
                target_span_pt=target_span,
                options=options,
            )
            and len(item.inliers) >= options.competing_ratio * len(best.inliers)
        ),
        None,
    )
    if not failures and runner_up is not None:
        failures = ["competing_transforms"]
    return best, failures, runner_up


def best_alternative_orientation(
    source: tuple[MatchSegment, ...],
    target: tuple[MatchSegment, ...],
    *,
    scale: float,
    target_meters_per_point: float,
    options: WallMatchOptions = DEFAULT_WALL_MATCH_OPTIONS,
) -> tuple[int, bool, int]:
    """Most wall segments any mirrored or rotated placement explains.

    Symmetric walls can match a flipped or turned sheet almost as well as the
    true placement. An identity match is trusted only when every alternative
    explains strictly fewer source wall segments.
    """

    tolerance_pt = options.tolerance_m / target_meters_per_point
    best = (0, False, 0)
    for mirrored, quarter_turns in ALTERNATIVE_ORIENTATIONS:
        mapped = map_segments(source, scale, quarter_turns, mirrored)
        for translation in vote(mapped, target, tolerance_pt):
            count = len(verify(mapped, target, translation, tolerance_pt).inliers)
            if count > best[0]:
                best = (count, mirrored, quarter_turns)
    return best


@dataclass(frozen=True, slots=True)
class WallRegistration:
    """One source-to-target wall comparison under every declared check.

    ``match`` is the best identity-orientation translation; ``reason_codes`` is
    empty only when it passes the evidence, residual, and spread thresholds,
    no competing translation explains nearly as many segments, and no mirrored
    or turned placement explains as many. ``orientation_alternative`` reports
    the strongest mirrored or turned placement whenever it was checked.
    """

    match: WallMatch | None
    reason_codes: tuple[str, ...]
    runner_up: WallMatch | None
    orientation_alternative: dict[str, object] | None

    @property
    def accepted(self) -> bool:
        return self.match is not None and not self.reason_codes


def register_walls(
    source: tuple[MatchSegment, ...],
    target: tuple[MatchSegment, ...],
    *,
    scale: float,
    target_meters_per_point: float,
    options: WallMatchOptions = DEFAULT_WALL_MATCH_OPTIONS,
) -> WallRegistration:
    """Match source walls onto target walls and apply the orientation guard.

    A mirrored or turned placement that explains more source wall segments than
    the identity is ``orientation_incompatible``; one that explains exactly as
    many is ``ambiguous_orientation`` (symmetric walls). Either refuses the
    identity match; callers may resolve a tie only with independent evidence.
    """

    match, reasons, runner_up = match_walls(
        source,
        target,
        scale=scale,
        target_meters_per_point=target_meters_per_point,
        options=options,
    )
    alternative: dict[str, object] | None = None
    if match is not None and not reasons:
        count, mirrored, quarter_turns = best_alternative_orientation(
            source,
            target,
            scale=scale,
            target_meters_per_point=target_meters_per_point,
            options=options,
        )
        alternative = {
            "mirrored": mirrored,
            "rotation_degrees": 90 * quarter_turns,
            "wall_inlier_count": count,
        }
        if count > len(match.inliers):
            reasons = ["orientation_incompatible"]
        elif count == len(match.inliers):
            reasons = ["ambiguous_orientation"]
    return WallRegistration(match, tuple(reasons), runner_up, alternative)


def composed_frame(
    target_meters_per_point: float,
    target_rotation_radians: float,
    target_translation_m: tuple[float, float],
    scale: float,
    translation_pt: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Compose a sheet-to-sheet placement with the target's canonical frame.

    Returns ``(meters_per_point, rotation_radians, tx_m, ty_m)`` mapping source
    sheet points directly into the target's canonical frame.
    """

    c = math.cos(target_rotation_radians)
    s = math.sin(target_rotation_radians)
    tx, ty = translation_pt
    return (
        target_meters_per_point * scale,
        target_rotation_radians,
        target_meters_per_point * (c * tx - s * ty) + target_translation_m[0],
        target_meters_per_point * (s * tx + c * ty) + target_translation_m[1],
    )


def placements_agree(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    bbox_pt: tuple[float, float, float, float],
    tolerance_m: float,
) -> bool:
    """Do two composed frames put every corner of ``bbox_pt`` at the same place?"""

    def place(placement: tuple[float, float, float, float], point: tuple[float, float]) -> tuple[float, float]:
        mpp, rotation, tx, ty = placement
        c, s = math.cos(rotation), math.sin(rotation)
        return (mpp * (c * point[0] - s * point[1]) + tx, mpp * (s * point[0] + c * point[1]) + ty)

    x0, y0, x1, y1 = bbox_pt
    return all(
        math.dist(place(first, corner), place(second, corner)) <= tolerance_m
        for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    )
