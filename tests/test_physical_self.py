"""Independent synthetic analytic cases for the actual-solid self verifier."""
from dataclasses import replace
from fractions import Fraction as F
from itertools import permutations, product

import pytest

from oabm.routing.physical_verify_types import Primitive, WorkLedger, VerificationStop, WorkLimit
from oabm.routing.physical_self import verify_self, _cover, _bounds, _outside, _parallel, _shape


def point(x, y, z=0):
    return tuple(F(str(v)) for v in (x, y, z))


def line(name, start, end, r=F(1, 50)):
    axis = next(i for i in range(3) if start[i] != end[i])
    t = tuple((1 if end[i] > start[i] else -1) if i == axis else 0 for i in range(3))
    return Primitive(name, 'line', start, end, t, t, r, start,
                     axis=axis, axial=(min(start[axis], end[axis]), max(start[axis], end[axis])))


def elbow(r=F(1, 50), R=F(1, 10)):
    return (
        line('a', point(R, -1), point(R, 0), r),
        Primitive('m', 'quarter_arc', point(R, 0), point(0, R), (0, 1, 0), (-1, 0, 0), r,
                  point(0, 0), plane_axes=(0, 1), signs=(1, 1), major_radius=R),
        line('b', point(0, R), point(-1, R), r))


def test_fat_elbow_exact_flat_cap_certificate():
    ps = elbow(F(9, 100))
    ledger = WorkLedger(100)
    verify_self(ps, ledger, max_depth=0)
    evidence = ledger.diagnostics[-1]
    assert evidence.kind == 'self_line_arc_line'
    assert dict(evidence.exact)['distance_squared_m2'] == F(1, 5000)
    assert dict(ledger.counters)['self_root_pair'] == 1
    assert dict(ledger.counters)['self_adjacent_seam'] == 2


def test_one_nanometre_interbend_gap_has_no_micrometre_margin():
    a = elbow()[1]
    L = F(1, 10**9)
    p, q = a.end, point(-L, F(1, 10))
    middle = line('straight', p, q)
    # Start -X, end +Y, in quadrant (-X,-Y) of a centre above q.
    b = Primitive('second', 'quarter_arc', q, point(-L-F(1, 10), F(1, 5)),
                  (-1, 0, 0), (0, 1, 0), F(1, 50), point(-L, F(1, 5)),
                  plane_axes=(0, 1), signs=(-1, -1), major_radius=F(1, 10))
    ledger = WorkLedger(100)
    verify_self((a, middle, b), ledger, max_depth=0)
    assert dict(ledger.diagnostics[-1].exact)['distance_m'] == L


@pytest.mark.parametrize('spacing,collision', [('0.040000001', False), ('0.04', True), ('0.039999999', True)])
def test_parallel_gap_contact_overlap(spacing, collision):
    a, b = line('a', point(0, 0), point(1, 0)), line('b', point(0, spacing), point(1, spacing))
    if collision:
        with pytest.raises(VerificationStop) as exc:
            _parallel(a, b)
        assert exc.value.status == 'rejected_route'
        assert dict(exc.value.exact)['witness_1_m'] == F(spacing)/2
    else:
        assert _parallel(a, b) is not None


def test_diagonal_parallel_boxes_overlap_but_discs_separate():
    a = line('a', point(0, 0, 0), point(1, 0, 0))
    b = line('b', point(0, '.03', '.03'), point(1, '.03', '.03'))
    assert all(max(x[0], y[0]) < min(x[1], y[1]) for x, y in zip(a.bounds, b.bounds))
    assert dict(_parallel(a, b))['radial_center_distance_squared_m2'] == F(18, 10000)


def test_perpendicular_line_contact_is_real_rational_witness():
    a = line('a', point(-1, 0), point(1, 0))
    b = line('b', point(0, -1), point(0, 1))
    with pytest.raises(VerificationStop) as exc:
        _cover(a, b, WorkLedger(100), 0)
    assert exc.value.status == 'rejected_route'
    assert all(v == 0 for _, v in exc.value.exact)


def test_line_arc_exact_contact_witness():
    a = elbow()[1]
    # Arc start is on this vertical cylinder axis and in its axial interval.
    b = line('cross', point('.1', 0, -1), point('.1', 0, 1))
    with pytest.raises(VerificationStop) as exc:
        _cover(a, b, WorkLedger(100), 0)
    assert exc.value.status == 'rejected_route'
    assert tuple(v for _, v in exc.value.exact) == point('.1', 0)


@pytest.mark.parametrize('mutation', ['fat', 'equal', 'tangent', 'semicircle', 'joint', 'tube'])
def test_local_theorem_guards(mutation):
    a, m, b = elbow()
    if mutation == 'fat':
        a, m, b = (replace(p, tube_radius=F(11, 100)) for p in (a, m, b))
    elif mutation == 'equal':
        a, m, b = (replace(p, tube_radius=F(1, 10)) for p in (a, m, b))
    elif mutation == 'tangent':
        b = line('b', b.start, point(1, '.1'))
    elif mutation == 'semicircle':
        m = replace(m, end=point('-.1', 0))
    elif mutation == 'joint':
        b = line('b', point(0, '.100000001'), point(-1, '.100000001'))
    else:
        b = replace(b, tube_radius=F(1, 40))
    with pytest.raises(VerificationStop) as exc:
        verify_self((a, m, b), WorkLedger(100), max_depth=10)
    assert exc.value.status == 'invalid_input'


def test_rational_cell_bounds_contain_independent_torus_points():
    a = elbow()[1]
    cell = ((F(1, 7), F(2, 7)), (F(-1, 100), F(1, 100)), (F(-1, 100), F(1, 100)))
    bounds = _bounds(a, cell)
    # Independent rational trig values at t=1/5: cos=12/13, sin=5/13.
    for u, v in product((F(-1, 100), F(0), F(1, 100)), repeat=2):
        q = ((F(1, 10)+u)*F(12, 13), (F(1, 10)+u)*F(5, 13), v)
        assert all(lo <= x <= hi for x, (lo, hi) in zip(q, bounds))
    assert any((v*2*10**9).denominator != 1 for pair in bounds for v in pair)
    # Conservative rectangles retain rim contact, discard strictly outside.
    assert not _outside(a, ((F(0), F(1)), (F(1, 50), F(1, 50)), (F(0), F(0))))
    assert _outside(a, ((F(0), F(1)), (F(1, 50), F(1, 50)), (F(1, 1000), F(1, 1000))))


def test_zero_budget_does_no_shape_work_and_inputs_unchanged():
    ps = elbow()
    before = repr(ps)
    ledger = WorkLedger(0)
    with pytest.raises(WorkLimit):
        verify_self(ps, ledger, max_depth=64)
    assert ledger.used == 0
    assert repr(ps) == before


def test_depth_is_unresolved_not_collision_from_bounds():
    # Perpendicular finite cylinders separated by .041 in Z (r=.02 each).
    # Full coordinate boxes actually separate, so use overlapping conservative
    # torus root bounds versus a point-sized cylinder in the empty central hole.
    a = elbow()[1]
    b = line('hole', point('.02', '.02', '-.001'), point('.02', '.02', '.001'), F(1, 1000))
    outcomes = []
    for _ in range(2):
        ledger = WorkLedger(100)
        with pytest.raises(VerificationStop) as exc:
            _cover(a, b, ledger, 0)
        outcomes.append((exc.value.status, exc.value.reason, ledger.counters))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == 'numerically_unresolved'
    ledger = WorkLedger(100000)
    _cover(a, b, ledger, 24)
    assert ledger.diagnostics[-1].kind == 'self_parameter_cover'


def test_refinement_budget_is_deterministic():
    a = elbow()[1]
    b = line('hole', point('.02', '.02', '-.001'), point('.02', '.02', '.001'), F(1, 1000))
    counts = []
    for _ in range(2):
        ledger = WorkLedger(1)
        with pytest.raises(WorkLimit):
            _cover(a, b, ledger, 24)
        counts.append(ledger.counters)
    assert counts[0] == counts[1] == (('self_split', 1),)


def test_all_signed_axis_transforms_preserve_local_proof():
    original = elbow(F(9, 100))
    for perm in permutations(range(3)):
        for signs in product((-1, 1), repeat=3):
            def vector(q):
                return tuple(signs[i]*q[perm[i]] for i in range(3))
            def position(q):
                return tuple(x+t for x, t in zip(vector(q), point(3, -2, 1)))
            transformed = []
            for p in original:
                args = dict(start=position(p.start), end=position(p.end), center=position(p.center),
                            start_tangent=vector(p.start_tangent), end_tangent=vector(p.end_tangent))
                if p.kind == 'line':
                    axis = perm.index(p.axis)
                    args.update(axis=axis, axial=tuple(sorted((args['start'][axis], args['end'][axis]))))
                else:
                    axes = tuple(perm.index(i) for i in p.plane_axes)
                    args.update(plane_axes=axes, signs=tuple(s*signs[i] for s, i in zip(p.signs, axes)))
                transformed.append(replace(p, **args))
            ledger = WorkLedger(100)
            verify_self(tuple(transformed), ledger, max_depth=0)
            assert dict(ledger.diagnostics[-1].exact)['distance_squared_m2'] == F(1, 5000)


def test_reverse_quarter_arc_shape():
    a = elbow()[1]
    _shape(replace(a, start=a.end, end=a.start,
                   start_tangent=tuple(-x for x in a.end_tangent),
                   end_tangent=tuple(-x for x in a.start_tangent)))


def test_general_cover_parallel_diagonal_gap_independent_of_special_case():
    # True distance sqrt(.03^2+.03^2)-.04 > 0. Exercise only coverage.
    a = line('a', point(0, 0, 0), point('.01', 0, 0))
    b = line('b', point(0, '.03', '.03'), point('.01', '.03', '.03'))
    ledger = WorkLedger(100000)
    _cover(a, b, ledger, 48)
    assert ledger.diagnostics[-1].kind == 'self_parameter_cover'
    assert dict(ledger.counters)['self_child_cell'] > 0


def test_reversed_second_arc_tangent_cannot_use_positive_line_theorem():
    a = elbow()[1]
    L = F(1, 10**9)
    q = point(-L, '.1')
    m = line('straight', a.end, q)
    # This genuinely canonical arc turns BACK toward +X, not -X.
    b = Primitive('back', 'quarter_arc', q, point(F(1, 10)-L, F(1, 5)),
                  (1, 0, 0), (0, 1, 0), F(1, 50), point(-L, F(1, 5)),
                  plane_axes=(0, 1), signs=(1, -1), major_radius=F(1, 10))
    with pytest.raises(VerificationStop) as exc:
        verify_self((a, m, b), WorkLedger(100), max_depth=20)
    assert exc.value.reason == 'self_invalid_seam'


def test_failure_witness_and_success_do_not_mutate_primitives():
    ps = elbow(F(9, 100))
    snapshot = repr(ps)
    verify_self(ps, WorkLedger(100), max_depth=0)
    assert repr(ps) == snapshot
    a = line('a', point(-1, 0), point(1, 0))
    b = line('b', point(0, -1), point(0, 1))
    before = repr((a, b))
    with pytest.raises(VerificationStop):
        _cover(a, b, WorkLedger(100), 20)
    assert repr((a, b)) == before


def test_resource_limit_preserves_specific_root_pair_cursor():
    # Three shapes + two (seam debit + record) = seven completed units.
    ledger = WorkLedger(7)
    with pytest.raises(WorkLimit):
        verify_self(elbow(), ledger, max_depth=0)
    assert ledger.cursor.kind == 'self_root_pair'
    assert ledger.cursor.ids == ('a', 'b')
    assert ledger.cursor.exact == ()


def test_resource_limit_preserves_exact_pending_child_cell():
    a = elbow()[1]
    b = line('hole', point('.02', '.02', '-.001'), point('.02', '.02', '.001'), F(1, 1000))
    ledger = WorkLedger(1)
    with pytest.raises(WorkLimit):
        _cover(a, b, ledger, 24)
    assert ledger.cursor.kind == 'self_child_cell'
    assert ledger.cursor.ids == ('m', 'hole')
    facts = dict(ledger.cursor.exact)
    assert len(facts) == 13
    assert facts['depth'] == 1
    # Arc t is widest in physical contribution and its lower half is first.
    assert facts['a_0_low_parameter'] == 0
    assert facts['a_0_high_parameter'] == F(1, 2)
    assert facts['b_0_low_parameter'] == F(-1, 1000)
    assert facts['b_0_high_parameter'] == F(1, 1000)


def test_shape_and_split_limit_cursors():
    ledger = WorkLedger(0)
    with pytest.raises(WorkLimit):
        verify_self(elbow(), ledger, max_depth=0)
    assert (ledger.cursor.kind, ledger.cursor.ids) == ('self_shape', ('a',))
    a = elbow()[1]
    b = line('hole', point('.02', '.02', '-.001'), point('.02', '.02', '.001'), F(1, 1000))
    ledger = WorkLedger(0)
    with pytest.raises(WorkLimit):
        _cover(a, b, ledger, 24)
    assert ledger.cursor.kind == 'self_split'
    assert dict(ledger.cursor.exact)['depth'] == 0
