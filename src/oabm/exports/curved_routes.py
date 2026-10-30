"""Explicit-radius display geometry. Canonical polylines remain unchanged.

This produces a sampled circular-fillet preview, not selected manufacturer
fittings, a changed quantity basis, or a swept-clearance approval.
"""
from __future__ import annotations

import math
from oabm.model import Point3


def _xyz(p):
    return (p.x, p.y, p.z)


def _add(a, b):
    return tuple(x+y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _mul(a, s):
    return tuple(x*s for x in a)


def _dot(a, b):
    return math.fsum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _unit(a):
    length = math.hypot(*a)
    if not math.isfinite(length) or length <= 1e-12:
        raise ValueError('curved preview has a degenerate direction')
    return _mul(a, 1/length)


def fillet_points(points, radius_m):
    """Sample tangent circular arcs at no more than five degrees per segment.

    Admission of radius, fitting inventory and available tangent lengths belongs
    to the existing bend preflight. This helper repeats the geometric budget
    check so it cannot silently extrapolate when used directly.
    """
    if type(radius_m) not in (int, float) or not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError('curved preview radius must be finite and positive')
    clean = []
    for p in points:
        value = _xyz(p)
        if not all(math.isfinite(x) for x in value):
            raise ValueError('curved preview coordinates must be finite')
        if clean and math.dist(clean[-1], value) <= 1e-12:
            raise ValueError('curved preview has a zero-length segment')
        if len(clean) >= 2:
            u, v = _unit(_sub(clean[-1], clean[-2])), _unit(_sub(value, clean[-1]))
            if math.hypot(*_cross(u, v)) < 1e-10 and _dot(u, v) > 0:
                clean[-1] = value
                continue
        clean.append(value)
    if len(clean) < 2:
        raise ValueError('curved preview requires two distinct points')
    turns = {}
    setbacks = [0.0]*len(clean)
    for i in range(1, len(clean)-1):
        u = _unit(_sub(clean[i], clean[i-1]))
        v = _unit(_sub(clean[i+1], clean[i]))
        cross = _cross(u, v)
        angle = math.atan2(math.hypot(*cross), _dot(u, v))
        if angle >= math.pi-1e-8:
            raise ValueError('curved preview cannot fillet a reversal')
        setback = radius_m*math.tan(angle/2)
        setbacks[i] = setback
        turns[i] = (u, v, _unit(cross), angle)
    for i, (a, b) in enumerate(zip(clean, clean[1:])):
        if setbacks[i]+setbacks[i+1] > math.dist(a, b)+1e-10:
            raise ValueError('curved preview radius exceeds adjacent straight budget')
    result = [clean[0]]
    def append(p):
        if math.dist(result[-1], p) > 1e-12:
            result.append(p)
        if len(result) > 100000:
            raise ValueError('curved preview exceeds bounded sampling capacity')
    for i, (u, v, normal, angle) in turns.items():
        start = _sub(clean[i], _mul(u, setbacks[i]))
        end = _add(clean[i], _mul(v, setbacks[i]))
        inward = _unit(_cross(normal, u))
        center = _add(start, _mul(inward, radius_m))
        radial = _sub(start, center)
        append(start)
        count = max(1, math.ceil(angle/math.radians(5)))
        for step in range(1, count):
            theta = angle*step/count
            rotated = _add(_mul(radial, math.cos(theta)), _mul(_cross(normal, radial), math.sin(theta)))
            append(_add(center, rotated))
        append(end)
    append(clean[-1])
    return tuple(Point3(x=p[0], y=p[1], z=p[2]) for p in result)


def sweep_vertices(points, radius_m, sides=16, *, offset_m=0.0, offset_angle=0.0):
    """Continuous tube rings with transported frames, including offset wires."""
    if (type(radius_m) not in (int, float) or not math.isfinite(radius_m) or radius_m <= 0
        or type(offset_m) not in (int, float) or not math.isfinite(offset_m) or offset_m < 0
        or type(offset_angle) not in (int, float) or not math.isfinite(offset_angle)
        or type(sides) is not int or not 3 <= sides <= 128 or len(points) < 2):
        raise ValueError('invalid curved preview sweep dimensions')
    centers = [_xyz(p) for p in points]
    directions = [_unit(_sub(b, a)) for a, b in zip(centers, centers[1:])]
    tangents = [directions[0], *(_unit(_add(a, b)) for a, b in zip(directions, directions[1:])), directions[-1]]
    first = tangents[0]
    seed = min(((1.,0.,0.), (0.,1.,0.), (0.,0.,1.)), key=lambda axis:abs(_dot(axis, first)))
    normal = _unit(_cross(first, seed))
    rings, shifted = [], []
    for p, tangent in zip(centers, tangents):
        normal = _unit(_sub(normal, _mul(tangent, _dot(normal, tangent))))
        binormal = _unit(_cross(tangent, normal))
        center = _add(p, _mul(_add(_mul(normal, math.cos(offset_angle)), _mul(binormal, math.sin(offset_angle))), offset_m))
        shifted.append(center)
        rings.append([_add(center, _mul(_add(_mul(normal, math.cos(2*math.pi*j/sides)), _mul(binormal, math.sin(2*math.pi*j/sides))), radius_m)) for j in range(sides)])
    vertices = []
    for a, b in zip(rings, rings[1:]):
        for j in range(sides):
            k = (j+1)%sides
            vertices.extend((a[j], a[k], b[k], a[j], b[k], b[j]))
    for j in range(sides):
        k = (j+1)%sides
        vertices.extend((shifted[0], rings[0][k], rings[0][j], shifted[-1], rings[-1][j], rings[-1][k]))
    return vertices
