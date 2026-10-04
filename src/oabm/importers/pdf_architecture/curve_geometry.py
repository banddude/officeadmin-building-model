"""Scale-free circle checks shared by source extraction and wall pairing."""

from __future__ import annotations
import math
import numpy as np
from .types import PdfCurveObservation

# center, radius, counterclockwise start, sweep, all in source points/radians.
Circle = tuple[tuple[float, float], float, float, float]


def fit_circle(curve: PdfCurveObservation) -> Circle | None:
    points = np.asarray(curve.points_pt, dtype=float)
    origin = points.mean(axis=0)
    local = points - origin
    matrix = np.column_stack((2 * local[:, 0], 2 * local[:, 1], np.ones(len(local))))
    solution, _, rank, _ = np.linalg.lstsq(
        matrix, np.sum(local * local, axis=1), rcond=None
    )
    if rank != 3:
        return None
    center = solution[:2] + origin
    distances = np.linalg.norm(points - center, axis=1)
    radius = float(np.mean(distances))
    if radius <= 0 or float(np.ptp(distances)) > 0.1:
        return None
    angles = np.unwrap(np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0]))
    if angles[-1] < angles[0]:
        angles = angles[::-1]
    sweep = float(angles[-1] - angles[0])
    # Closed bubbles and tiny nearly straight arcs cannot establish a wall.
    if not 0.1 <= sweep <= math.pi * 1.95 or np.any(np.diff(angles) < -1e-6):
        return None
    start = float(angles[0] % (2 * math.pi))
    return (tuple(float(v) for v in center), radius, start, sweep)
