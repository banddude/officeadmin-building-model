"""Independent analytic oracles and direct-solid sampling diagnostics.

Only the production outputs are imported. Expected values do not call kernel
helpers. Sampling uses floats and is diagnostic, never a safety certificate.
"""
from fractions import Fraction as F
from itertools import permutations, product
import math

import pytest

from oabm.routing.physical_distance import (
    DistanceBounds, assess_cylinder_aabb, assess_quarter_torus_aabb,
    classify_distance, cylinder_aabb_distance, quarter_torus_aabb_distance,
)


def box(x, y, z):
    return tuple(tuple(F(str(v)) for v in pair) for pair in (x, y, z))


def torus(b, **changes):
    args = dict(center=(0, 0, 0), plane_axes=(0, 1), signs=(1, 1),
                major_radius=1, radius=F(1, 5), box=b)
    args.update(changes)
    return quarter_torus_aabb_distance(**args)


def exact(bounds, answer):
    assert bounds.lower == bounds.upper == F(answer)


def test_flat_cap_not_capsule_and_disc():
    args = dict(axis=0, axial=(0, 2), center=(0, 0, 0), radius=F(1, 50),
                box=box((-.03, -.01), (-.005, .005), (-.005, .005)))
    exact(cylinder_aabb_distance(**args), F(1, 100))
    assert assess_cylinder_aabb(required_separation=F(1, 200), **args).status == 'safe_separation'
    # Deliberate capsule mutation says overlap: wrong for a flat cap.
    capsule_distance = max(F(1, 100) - F(1, 50), 0)
    assert capsule_distance == 0 < cylinder_aabb_distance(**args).lower
    args['axial'] = (0, 0)
    exact(cylinder_aabb_distance(**args), F(1, 100))


def test_cylinder_independent_345_radial_and_axial():
    # Rectangle closest radial point (3,4)/10, r=.2 gives gap .3.
    # Axial gap .4, hence Pythagorean total .5.
    args = dict(axis=0, axial=(0, 1), center=(0, 0, 0), radius=F(1, 5),
                box=box((1.4, 1.5), (.3, .4), (.4, .5)))
    exact(cylinder_aabb_distance(**args), F(1, 2))
    args['box'] = box((.5, .6), (.2, .2), (0, 0))
    exact(cylinder_aabb_distance(**args), 0)  # surface contact, no penetration claim
    args['box'] = box((.5, .6), (.01, .02), (.01, .02))
    exact(cylinder_aabb_distance(**args), 0)  # material overlap, same distance


def test_terminal_disc_outside_quadrant_and_boundary():
    exact(torus(box((.9, 1.1), (-.1, -.05), (-.01, .01))), F(1, 20))
    exact(torus(box((-.1, .1), (.9, 1.1), (-.1, .1))), 0)
    exact(torus(box((0, 0), (1, 1), (0, 0))), 0)
    # A full-torus mutation would incorrectly make the first fixture zero.
    assert torus(box((.9, 1.1), (-.1, -.05), (-.01, .01))).lower > 0


def test_both_negative_quadrant_analytic_square():
    result = torus(box((-.3, -.2), (-.4, -.3), (-.01, .01)))
    # Terminal zero gives sqrt(.3**2 + (1.2-.2)**2) = sqrt(1.09).
    assert result.lower ** 2 <= F(109, 100) <= result.upper ** 2
    assert result.upper - result.lower < F(1, 10**17)


def test_inner_bend_analytic_radical():
    result = torus(box((.3, .4), (.3, .4), (-.01, .01)))
    # Exact answer .8 - sqrt(.32). Both compared sides are nonnegative.
    assert 0 < result.lower <= result.upper < F(4, 5)
    assert (F(4, 5) - result.lower) ** 2 >= F(8, 25)
    assert (F(4, 5) - result.upper) ** 2 <= F(8, 25)
    assert result.upper - result.lower < F(1, 10**17)


def test_radial_interval_crosses_major_radius():
    exact(torus(box((.6, .8), (.6, .8), (.4, .5))), F(1, 5))


def test_fat_elbow_and_axis_origin():
    # R=.1, r=.09, positive hole .01. r=.9R is supported, not collapsed.
    exact(torus(box((0, 0), (0, 0), (0, 0)),
                major_radius=F(1, 10), radius=F(9, 100)), F(1, 100))
    # Endpoint capsule centers are sqrt(2)*R apart < 2r: overlap is not a
    # certificate of physical self-collision. This kernel claims neither.
    assert 2 * F(1, 10) ** 2 < (2 * F(9, 100)) ** 2


def test_arc_only_blocker_not_sharp_legs():
    # Rotate a quarter torus to center (.7,.3), lower-right quadrant.
    b = box((.907132, .917132), (.082868, .092868), (-.005, .005))
    result = torus(b, center=(F(7, 10), F(3, 10), 0), signs=(1, -1),
                   major_radius=F(3, 10), radius=F(1, 50))
    exact(result, 0)
    # Both sharp polyline legs miss by >r. Mutating away the arc misses this.
    assert b[1][0] > F(1, 50) and 1 - b[0][1] > F(1, 50)


@pytest.mark.parametrize('axes', list(permutations(range(3), 2)))
@pytest.mark.parametrize('signs', list(product((-1, 1), repeat=2)))
def test_signed_axis_permutations_and_translation(axes, signs):
    original = box((-.3, -.2), (-.4, -.3), (-.01, .01))
    expected = torus(original)
    center = (F(3, 2), F(-7, 2), F(11, 2))
    normal = next(i for i in range(3) if i not in axes)
    transformed = [None] * 3
    for local_axis, (axis, sign) in enumerate(zip((*axes, normal), (*signs, 1))):
        transformed[axis] = tuple(sorted(center[axis] + sign * v for v in original[local_axis]))
    assert torus(tuple(transformed), center=center, plane_axes=axes, signs=signs) == expected


def test_mutated_radius_origin_and_sign_change_known_answer():
    b = box((.9, 1.1), (-.1, -.05), (-.01, .01))
    assert torus(b, signs=(1, -1)).upper == 0
    assert torus(b, center=(0, 0, 1)).lower > F(1, 20)
    hole = box((0, 0), (0, 0), (0, 0))
    exact(torus(hole), F(4, 5))
    exact(torus(hole, radius=F(1, 10)), F(9, 10))


def test_four_outcomes_and_exact_boundary_never_passes():
    args = dict(axis=0, axial=(0, 2), center=(0, 0, 0), radius=F(1, 50),
                box=box((-.03, -.01), (-.005, .005), (-.005, .005)))
    assert assess_cylinder_aabb(required_separation=F(1, 200), **args).status == 'safe_separation'
    assert assess_cylinder_aabb(required_separation=F(1, 50), **args).status == 'insufficient_clearance'
    boundary = assess_cylinder_aabb(required_separation=F(1, 100), **args)
    assert boundary.status == 'unresolved' and boundary.precision_bits == 128
    assert classify_distance(DistanceBounds(F(0), F(0)), 0) == 'contact_or_overlap'
    assert classify_distance(DistanceBounds(F(0), F(1, 100)), F(1, 50)) == 'unresolved'
    # Conservative broad interval crossing threshold never certifies safe.
    assert classify_distance(DistanceBounds(F(1, 200), F(1, 50)), F(1, 100)) == 'unresolved'


def test_half_tick_and_fixed_refinement():
    gap = F(1, 2_000_000_000)
    args = dict(axis=2, axial=(0, 1), center=(0, 0, 0), radius=F(1, 50),
                box=((F(0), F(0)), (F(0), F(0)), (-gap, -gap)))
    exact(cylinder_aabb_distance(**args), gap)
    assert assess_cylinder_aabb(required_separation=0, **args).status == 'safe_separation'
    with pytest.raises(ValueError):
        assess_cylinder_aabb(required_separation=0, bits=64, **args)


@pytest.mark.parametrize('change', [dict(radius=.2), dict(radius=True), dict(radius=F(1, 3)),
    dict(radius=0), dict(radius=1), dict(major_radius=F(1, 5)), dict(major_radius=11),
    dict(center=(1001.1, 0, 0)), dict(plane_axes=(1, 1)), dict(plane_axes=(True, 2)),
    dict(signs=(0, 1)), dict(signs=(True, 1)), dict(bits=1024),
    dict(box=((1, 0), (0, 1), (0, 1)))])
def test_invalid_or_unsupported_geometry(change):
    with pytest.raises((TypeError, ValueError)):
        torus(box((0, 1), (0, 1), (0, 1)), **change)


def test_refinement_encloses_radical_and_shrinks():
    b = box((-.3, -.2), (-.4, -.3), (-.01, .01))
    previous = None
    for bits in (32, 64, 128):
        result = torus(b, bits=bits)
        assert result.lower ** 2 <= F(109, 100) <= result.upper ** 2
        if previous:
            assert previous.lower <= result.lower <= result.upper <= previous.upper
        previous = result


@pytest.mark.parametrize('R,r', [(F(1), F(1, 5)), (F(3, 10), F(1, 50)), (F(1, 10), F(9, 100))])
def test_independent_full_solid_sampling_bounds(R, r):
    # Independent definition, not radial-range or terminal-disc implementation.
    # Include the disc INTERIORS, not only the boundary surface. These ordinary
    # float comparisons are diagnostic sampling bounds, not certification.
    n, m, k = 16, 24, 6
    points = []
    for j in range(n + 1):
        theta = math.pi * j / (2 * n)
        for h in range(k + 1):
            t = float(r) * h / k
            for i in range(m):
                phi = 2 * math.pi * i / m
                rho = float(R) + t * math.cos(phi)
                points.append((rho * math.cos(theta), rho * math.sin(theta), t * math.sin(phi)))
    error = float(R+r) * math.pi/(4*n) + float(r)*math.pi/m + float(r)/(2*k)
    cases = [box((.9, 1.1), (-.1, -.05), (-.01, .01)),
             box((-.3, -.2), (-.4, -.3), (-.01, .01)),
             box((.3, .4), (.3, .4), (-.01, .01)),
             box((.6, .8), (.6, .8), (.4, .5)),
             box((-.1, .1), (.9, 1.1), (-.1, .1)),
             box((-.1, .1), (.4, .5), (.4, .5)),
             box((0, 0), (1, 1), (0, 0)),
             box((-.1, 0), (-.1, 0), (-.1, .1))]
    for original in cases:
        b = tuple(tuple(R*v for v in pair) for pair in original)
        sampled = math.sqrt(min(sum(max(float(lo)-v, 0, v-float(hi))**2
                               for v, (lo, hi) in zip(point, b)) for point in points))
        actual = torus(b, major_radius=R, radius=r)
        assert float(actual.lower) <= sampled + 1e-12
        assert float(actual.upper) >= sampled-error-1e-12
