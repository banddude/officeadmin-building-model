"""Certified distances from flat normal-disc solids to closed AABBs.

This is a mathematical kernel, not a persisted geometry model, route generator,
source-binding validator, self-clearance test, or allowed-domain-union test.
Callers must derive these exact arguments from admitted canonical geometry.
Lengths are metres. No float, decimal snapping, capsule approximation or
floating-point tolerance participates in a certificate.

Supported solids are cardinal finite cylinders (including a zero-length disc)
and signed-cardinal quarter tori with R > r. Inputs use the contract's half-tick
lattice for derived geometry; canonical raw-lexeme admission belongs upstream.
All square roots have integer-proved outward rational bounds. Clearance checks
use fixed 32/64/128-bit refinement and strict separation; exact threshold
agreement remains unresolved rather than being rounded into a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import isqrt
from typing import Literal

Exact = int | Fraction
Point = tuple[Exact, Exact, Exact]
Box = tuple[tuple[Exact, Exact], tuple[Exact, Exact], tuple[Exact, Exact]]
REFINEMENT_BITS = (32, 64, 128)
_HALF_TICKS = 2_000_000_000


@dataclass(frozen=True)
class DistanceBounds:
    """Enclosure of the minimum Euclidean distance between two closed solids."""

    lower: Fraction
    upper: Fraction

    def __post_init__(self) -> None:
        if type(self.lower) is not Fraction or type(self.upper) is not Fraction:
            raise TypeError("distance bounds must be Fractions")
        if not 0 <= self.lower <= self.upper:
            raise ValueError("invalid nonnegative distance enclosure")


@dataclass(frozen=True)
class DistanceAssessment:
    status: Literal["safe_separation", "contact_or_overlap", "insufficient_clearance", "unresolved"]
    bounds: DistanceBounds
    precision_bits: int


def _exact(value: Exact, limit: int = 1000) -> Fraction:
    if type(value) not in (int, Fraction):
        raise TypeError("geometry requires int or Fraction, never float/bool")
    value = Fraction(value)
    if abs(value) > limit or _HALF_TICKS % value.denominator:
        raise ValueError("geometry outside bounded exact half-tick profile")
    return value


def _point(value: Point) -> tuple[Fraction, Fraction, Fraction]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("expected three coordinates")
    # A derived arc center can lie R <= 10m beyond an admitted endpoint.
    return tuple(_exact(v, 1010) for v in value)


def _box(value: Box) -> tuple[tuple[Fraction, Fraction], ...]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("expected three box intervals")
    result = []
    for pair in value:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("expected interval endpoints")
        lo, hi = (_exact(v) for v in pair)
        if lo > hi:
            raise ValueError("reversed box interval")
        result.append((lo, hi))
    # Degenerate boxes are intentionally allowed by this mathematical kernel.
    return tuple(result)


def _radius(value: Exact) -> Fraction:
    value = _exact(value)
    if not Fraction(1, 2000) <= value <= Fraction(1, 2):
        raise ValueError("tube radius outside OD 0.001..1m profile")
    return value


def _bits(bits: int) -> None:
    if type(bits) is not int or bits not in REFINEMENT_BITS:
        raise ValueError("precision must be one of the fixed refinement budgets")


def _sqrt(value: Fraction, bits: int) -> DistanceBounds:
    """isqrt(floor(n*2**(2b)/d)) = floor(sqrt(n/d)*2**b)."""
    if value < 0:
        raise ValueError("negative radicand")
    numerator_root, denominator_root = isqrt(value.numerator), isqrt(value.denominator)
    if numerator_root ** 2 == value.numerator and denominator_root ** 2 == value.denominator:
        return _constant(Fraction(numerator_root, denominator_root))
    scale = 1 << bits
    scaled_numerator = value.numerator * scale * scale
    q = isqrt(scaled_numerator // value.denominator)
    lo = Fraction(q, scale)
    exact = q * q * value.denominator == scaled_numerator
    return DistanceBounds(lo, lo if exact else Fraction(q + 1, scale))


def _constant(value: Fraction) -> DistanceBounds:
    return DistanceBounds(value, value)


def _hypot(a: DistanceBounds, b: DistanceBounds, bits: int) -> DistanceBounds:
    return DistanceBounds(
        _sqrt(a.lower * a.lower + b.lower * b.lower, bits).lower,
        _sqrt(a.upper * a.upper + b.upper * b.upper, bits).upper,
    )


def _subtract_radius(distance: DistanceBounds, radius: Fraction) -> DistanceBounds:
    return DistanceBounds(max(Fraction(0), distance.lower - radius),
                          max(Fraction(0), distance.upper - radius))


def _gap(value: Fraction, interval: tuple[Fraction, Fraction]) -> Fraction:
    return max(interval[0] - value, Fraction(0), value - interval[1])


def _cylinder(axis: int, axial: tuple[Fraction, Fraction], center: tuple[Fraction, ...],
              radius: Fraction, box: tuple[tuple[Fraction, Fraction], ...], bits: int) -> DistanceBounds:
    radial_axes = [i for i in range(3) if i != axis]
    radial_squared = sum((_gap(center[i], box[i]) ** 2 for i in radial_axes), Fraction(0))
    radial_gap = _subtract_radius(_sqrt(radial_squared, bits), radius)
    axial_gap = max(axial[0] - box[axis][1], box[axis][0] - axial[1], Fraction(0))
    return _hypot(_constant(axial_gap), radial_gap, bits)


def cylinder_aabb_distance(*, axis: int, axial: tuple[Exact, Exact], center: Point,
                           radius: Exact, box: Box, bits: int = 64) -> DistanceBounds:
    """Flat-ended cylinder; center[axis] is ignored, axial supplies both caps.

    A collapsed axial interval denotes a disc. Axis is 0, 1 or 2. This does not
    enlarge either cap by radius or clearance as a capsule approximation would.
    """
    _bits(bits)
    if type(axis) is not int or axis not in (0, 1, 2):
        raise ValueError("invalid cardinal axis")
    if not isinstance(axial, (tuple, list)) or len(axial) != 2:
        raise ValueError("expected two axial endpoints")
    lo, hi = (_exact(v) for v in axial)
    if lo > hi:
        raise ValueError("reversed cylinder interval")
    return _cylinder(axis, (lo, hi), _point(center), _radius(radius), _box(box), bits)


def quarter_torus_aabb_distance(*, center: Point, plane_axes: tuple[int, int],
                                signs: tuple[int, int], major_radius: Exact,
                                radius: Exact, box: Box, bits: int = 64) -> DistanceBounds:
    """Quarter-torus normal-disc solid, including its flat terminal discs.

    Local +x/+y map to signs[0]*plane_axes[0], signs[1]*plane_axes[1].
    Signed cardinal transforms preserve AABBs and distance. The normal axis
    sign is immaterial because the circular normal disc is symmetric.

    Inside the angular quadrant, the nearest station has the query's polar
    angle. Rectangle radii fill a continuous interval and z is independent.
    Outside it, maximizing x*cos(theta)+y*sin(theta) gives a terminal station
    (monotonic if one coordinate is negative; convex if both are negative).
    Since R > r, every solid point has positive radial coordinate. Thus the
    minimum of clipped-quadrant distance and the two FULL-box terminal-disc
    distances is exact. Zero denotes contact OR overlap, not penetration.
    """
    _bits(bits)
    if (not isinstance(plane_axes, (tuple, list)) or len(plane_axes) != 2
            or any(type(a) is not int or a not in (0, 1, 2) for a in plane_axes)
            or plane_axes[0] == plane_axes[1]):
        raise ValueError("expected two distinct cardinal axes")
    if (not isinstance(signs, (tuple, list)) or len(signs) != 2
            or any(type(s) is not int or s not in (-1, 1) for s in signs)):
        raise ValueError("expected two cardinal signs")
    c, world, r = _point(center), _box(box), _radius(radius)
    R = _exact(major_radius)
    if not Fraction(1, 1000) <= R <= 10 or R - r < Fraction(1, 100_000):
        raise ValueError("major radius or R-r outside physical profile")
    normal = next(i for i in range(3) if i not in plane_axes)
    local = []
    for axis, sign in zip((*plane_axes, normal), (*signs, 1)):
        ends = tuple(sign * (v - c[axis]) for v in world[axis])
        local.append((min(ends), max(ends)))
    x, y, z = local
    zero = Fraction(0)
    candidates = [
        _cylinder(1, (zero, zero), (R, zero, zero), r, tuple(local), bits),
        _cylinder(0, (zero, zero), (zero, R, zero), r, tuple(local), bits),
    ]
    if x[1] >= 0 and y[1] >= 0:
        rho_min = _sqrt(max(x[0], zero) ** 2 + max(y[0], zero) ** 2, bits)
        rho_max = _sqrt(x[1] ** 2 + y[1] ** 2, bits)
        radial_gap = DistanceBounds(
            max(zero, rho_min.lower - R, R - rho_max.upper),
            max(zero, rho_min.upper - R, R - rho_max.lower),
        )
        candidates.append(_subtract_radius(
            _hypot(radial_gap, _constant(_gap(zero, z)), bits), r))
    return DistanceBounds(min(d.lower for d in candidates), min(d.upper for d in candidates))


def classify_distance(bounds: DistanceBounds, required_separation: Exact) -> str:
    """Strict policy. Caller supplies total clearance INCLUDING safety margin.

    Never labels mere overlapping bounds as collision. Exact threshold equality
    is unresolved. Contact is not distinguished from interior intersection.
    """
    threshold = _exact(required_separation)
    if not 0 <= threshold <= Fraction(200_000_001, 1_000_000):
        raise ValueError("required separation outside two-clearances-plus-margin profile")
    if bounds.upper == 0:
        return "contact_or_overlap"
    if bounds.lower > threshold:
        return "safe_separation"
    if 0 < bounds.lower and bounds.upper < threshold:
        return "insufficient_clearance"
    return "unresolved"


def assess_cylinder_aabb(*, required_separation: Exact, **geometry) -> DistanceAssessment:
    """Bounded refinement, with no success claim beyond this one pair."""
    return _assess(cylinder_aabb_distance, required_separation, geometry)


def assess_quarter_torus_aabb(*, required_separation: Exact, **geometry) -> DistanceAssessment:
    """Bounded refinement, with no success claim beyond this one pair."""
    return _assess(quarter_torus_aabb_distance, required_separation, geometry)


def _assess(kernel, threshold: Exact, geometry: dict) -> DistanceAssessment:
    if "bits" in geometry:
        raise ValueError("assessment uses the fixed refinement budget")
    for bits in REFINEMENT_BITS:
        bounds = kernel(bits=bits, **geometry)
        status = classify_distance(bounds, threshold)
        if status != "unresolved":
            break
    return DistanceAssessment(status, bounds, bits)
