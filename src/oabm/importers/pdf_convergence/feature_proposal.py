"""Bounded descriptor-correspondence proposals; never canonical registration.

A renderer/descriptor adapter supplies matched source/target PDF points. Printed
scales stay fixed. Spatially independent controls, not repeated symbol pixels,
rank translation hypotheses. Every result still requires the caller's geometry,
region, level, provenance and competing-target validation before any placement.
No model is imported or mutated here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

import numpy as np

VERSION = 'feature-correspondence-proposal-v1'
MAX_PAIRS = 1500
MIN_CONTROLS = 8
CONTROL_SEPARATION_M = .5
MIN_SPAN_M = 3.
MIN_SECOND_AXIS_SPREAD_M = 1.
RESIDUAL_TOLERANCE_M = .05


@dataclass(frozen=True)
class FeatureProposal:
    input_sha256: str
    eligible_for_geometry_validation: bool
    reason: str
    translation_target_pt: tuple[float, float] | None = None
    quarter_turns: int = 0
    mirrored: bool = False
    independent_controls: int = 0
    inliers: int = 0
    rms_residual_m: float | None = None
    span_m: tuple[float, float] = (0., 0.)
    method: str = VERSION
    # This object cannot approve a transform, even with a strong descriptor fit.
    accepted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def propose_feature_translation(
    source_points: Any, target_points: Any, *,
    source_meters_per_point: float, target_meters_per_point: float,
) -> FeatureProposal:
    """Score paired source evidence at fixed printed scales, without free fitting.

    Inputs are correspondences, not unpaired point clouds. Duplicate pairs do
    not add evidence. At most one inlier per exact source or target coordinate
    can contribute. Invalid input is refused, not coerced or truncated.
    """
    scales = (source_meters_per_point, target_meters_per_point)
    if any(isinstance(v, bool) or not isinstance(v, (float, int))
           or not math.isfinite(v) or v <= 0 for v in scales):
        raise ValueError('printed scales must be positive finite numbers')
    source, target = np.asarray(source_points, dtype=float), np.asarray(target_points, dtype=float)
    if source.ndim != 2 or source.shape[1:] != (2,) or target.shape != source.shape:
        raise ValueError('paired points must have matching (n, 2) shapes')
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError('paired points must be finite')
    if len(source) > MAX_PAIRS:
        raise ValueError(f'paired points exceed bounded budget {MAX_PAIRS}')
    pairs = sorted(set(tuple(row) for row in np.column_stack((source, target))))
    payload = {'version': VERSION, 'scales': scales, 'pairs': pairs}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if len(pairs) < MIN_CONTROLS:
        return FeatureProposal(fingerprint, False, 'insufficient_correspondences')
    array = np.array(pairs)
    with np.errstate(over="ignore", invalid="ignore"):
        source, target = array[:, :2] * scales[0], array[:, 2:] * scales[1]
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError("scaled coordinates must be finite")
    candidates = []
    for mirrored in (False, True):
        for quarter in range(4):
            mapped = source.copy()
            if mirrored:
                mapped[:, 0] *= -1
            for _ in range(quarter):
                mapped = np.column_stack((-mapped[:, 1], mapped[:, 0]))
            offsets = target - mapped
            seen = set()
            for initial in offsets:
                mask = np.linalg.norm(offsets-initial, axis=1) <= RESIDUAL_TOLERANCE_M
                shift = np.median(offsets[mask], axis=0)
                error = np.linalg.norm(offsets-shift, axis=1)
                indexes = np.flatnonzero(error <= RESIDUAL_TOLERANCE_M)
                # Distinct SIFT descriptors at one pixel are one control.
                used_source, used_target, unique = set(), set(), []
                for i in sorted(indexes, key=lambda i: (error[i], *pairs[i])):
                    a, b = tuple(source[i]), tuple(target[i])
                    if a not in used_source and b not in used_target:
                        unique.append(i); used_source.add(a); used_target.add(b)
                identity = tuple(sorted(unique))
                if identity in seen:
                    continue
                seen.add(identity)
                controls = []
                for i in sorted(unique, key=lambda i: pairs[i]):
                    if all(np.linalg.norm(source[i]-source[j]) >= CONTROL_SEPARATION_M
                           and np.linalg.norm(target[i]-target[j]) >= CONTROL_SEPARATION_M
                           for j in controls):
                        controls.append(i)
                points = source[controls]
                span = np.ptp(points, axis=0) if len(points) else np.zeros(2)
                second = (2*np.linalg.svd(points-points.mean(axis=0), compute_uv=False)[1]
                          / math.sqrt(len(points))) if len(points) >= 2 else 0.
                rms = float(np.sqrt(np.mean(error[unique]**2))) if unique else math.inf
                candidates.append((len(controls), len(unique), rms, mirrored, quarter,
                                   tuple(shift), tuple(span), float(second)))
    candidates.sort(key=lambda x: (-x[0], -x[1], x[2], x[3], x[4], x[5]))
    best = candidates[0]
    count, inliers, rms, mirrored, quarter, shift, span, second = best
    reason = 'proposal_requires_geometry_validation'
    if count < MIN_CONTROLS:
        reason = 'insufficient_independent_controls'
    elif min(span) < MIN_SPAN_M or second < MIN_SECOND_AXIS_SPREAD_M:
        reason = 'insufficient_two_dimensional_spread'
    elif mirrored or quarter:
        reason = 'unsupported_orientation'
    else:
        for rival in candidates[1:]:
            distinct = rival[3:5] != best[3:5] or math.dist(rival[5], shift) > 4*RESIDUAL_TOLERANCE_M
            if (distinct and rival[0] >= max(MIN_CONTROLS, math.ceil(count*.75))
                    and min(rival[6]) >= MIN_SPAN_M and rival[7] >= MIN_SECOND_AXIS_SPREAD_M):
                reason = 'competing_feature_placements'
                break
    return FeatureProposal(
        fingerprint, reason == 'proposal_requires_geometry_validation', reason,
        tuple(round(v/scales[1], 9) for v in shift), quarter, mirrored, count, inliers,
        round(rms, 9), tuple(round(v, 9) for v in span),
    )


def propose_raster_translation(
    source_image: Any, target_image: Any, *,
    source_pixel_origin_pt: tuple[float, float], target_pixel_origin_pt: tuple[float, float],
    source_points_per_pixel: float, target_points_per_pixel: float,
    source_meters_per_point: float, target_meters_per_point: float,
) -> FeatureProposal:
    """Use existing OpenCV SIFT on two bounded grayscale drawing crops.

    Pixel origins identify pixel (0,0) in displayed bottom-origin PDF points.
    Raster row coordinates increase downwards. The caller owns PDF rendering,
    exact crop transforms, source-file provenance and eligibility of each region.
    The adapter never opens files, retrieves a URL or changes a model.
    """
    from .mask_registration import import_cv2
    images = [np.asarray(source_image), np.asarray(target_image)]
    if any(im.ndim != 2 or im.dtype != np.uint8 or not im.size for im in images):
        raise ValueError('images must be nonempty uint8 grayscale arrays')
    if any(im.size > 16_000_000 for im in images):
        raise ValueError('image exceeds bounded pixel budget')
    for origin in (source_pixel_origin_pt, target_pixel_origin_pt):
        if len(origin) != 2 or any(not math.isfinite(v) for v in origin):
            raise ValueError('pixel origins must be finite 2D coordinates')
    for value in (source_points_per_pixel, target_points_per_pixel):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError('points per pixel must be finite and positive')
    cv2 = import_cv2()
    detector = cv2.SIFT_create(nfeatures=15000, contrastThreshold=.02)
    sk, sd = detector.detectAndCompute(images[0], None)
    tk, td = detector.detectAndCompute(images[1], None)
    pairs = [] if sd is None or td is None or len(td) < 2 else [
        pair[0] for pair in cv2.BFMatcher().knnMatch(sd, td, k=2)
        if len(pair) == 2 and pair[0].distance < .65 * pair[1].distance
    ]
    def point(kp, origin, resolution):
        return (origin[0]+kp.pt[0]*resolution, origin[1]-kp.pt[1]*resolution)
    source = np.array([point(sk[p.queryIdx], source_pixel_origin_pt, source_points_per_pixel)
                       for p in pairs], dtype=float).reshape((-1, 2))
    target = np.array([point(tk[p.trainIdx], target_pixel_origin_pt, target_points_per_pixel)
                       for p in pairs], dtype=float).reshape((-1, 2))
    proposal = propose_feature_translation(source, target,
        source_meters_per_point=source_meters_per_point,
        target_meters_per_point=target_meters_per_point)
    # Bind the evidence to the actual pixels and coordinate transforms, even
    # when no features were found. Different blank crops are not the same input.
    from dataclasses import replace
    digest = hashlib.sha256(proposal.input_sha256.encode())
    for im, origin, resolution in zip(images,
            (source_pixel_origin_pt, target_pixel_origin_pt),
            (source_points_per_pixel, target_points_per_pixel)):
        digest.update(json.dumps((im.shape, origin, resolution)).encode())
        digest.update(np.ascontiguousarray(im).tobytes())
    return replace(proposal, input_sha256=digest.hexdigest())
