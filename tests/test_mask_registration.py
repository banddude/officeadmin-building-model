"""Deterministic mask-based phase-correlation registration known-answer tests.

Every sheet is a synthetic plan generated in the test from fixed literal
coordinates (plus one fixed-seed clutter case); no fixtures, no private
data. The shifts are exact analytic cases, so the recovery tolerances double
as convention checks: the returned translation maps source-sheet points
onto the target frame, and the peak gate separates a real sheet match from
decorrelated content.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from oabm.importers.pdf_convergence.mask_registration import (
    MaskRegistration,
    register_by_phase_correlation,
)

PAGE_SIZE_PT = (612.0, 792.0)
SHIFT_TOLERANCE_PT = 0.5
CLUTTER_SEED = 1729


def _plan(
    offset: tuple[float, float] = (0.0, 0.0),
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A synthetic floor plan: outer rectangle, 3 interior walls, a stair box."""

    ox, oy = offset
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def line(x0: float, y0: float, x1: float, y1: float) -> None:
        segments.append(((x0 + ox, y0 + oy), (x1 + ox, y1 + oy)))

    # outer rectangle
    line(72, 144, 540, 144)
    line(540, 144, 540, 648)
    line(540, 648, 72, 648)
    line(72, 648, 72, 144)
    # 3 interior walls
    line(228, 144, 228, 648)
    line(396, 144, 396, 468)
    line(228, 468, 540, 468)
    # stair box with treads
    line(440, 500, 520, 500)
    line(520, 500, 520, 620)
    line(520, 620, 440, 620)
    line(440, 620, 440, 500)
    for y in (520, 540, 560, 580, 600):
        line(440, y, 520, y)
    return segments


def _unrelated_plan() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A decorrelated plan: a rotated parcel with diagonals, no shared walls."""

    pts = [(140, 380), (300, 220), (500, 300), (360, 620)]
    segments = [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    segments += [
        (pts[0], pts[2]),
        (pts[1], pts[3]),
        ((200, 480), (330, 560)),
        ((330, 560), (430, 470)),
    ]
    return segments


def _clutter(count: int) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Fixed-seed random segments inside the plan's rectangle."""

    rng = np.random.default_rng(CLUTTER_SEED)
    x0, y0 = rng.uniform(90.0, 522.0, count), rng.uniform(162.0, 630.0, count)
    x1, y1 = rng.uniform(90.0, 522.0, count), rng.uniform(162.0, 630.0, count)
    return [((a, b), (c, d)) for a, b, c, d in zip(x0, y0, x1, y1)]


def test_known_shift_recovered() -> None:
    source = _plan()
    target = _plan(offset=(0.0, 36.0))

    result = register_by_phase_correlation(source, target, page_size_pt=PAGE_SIZE_PT)

    assert result.converged
    assert result.method == "phase-correlation"
    assert result.dx_pt == pytest.approx(0.0, abs=SHIFT_TOLERANCE_PT)
    assert result.dy_pt == pytest.approx(36.0, abs=SHIFT_TOLERANCE_PT)
    assert result.peak >= 0.3
    assert result.warnings == ()


def test_fractional_shift_recovered() -> None:
    source = _plan()
    target = _plan(offset=(12.5, -7.25))

    result = register_by_phase_correlation(source, target, page_size_pt=PAGE_SIZE_PT)

    assert result.converged
    assert result.dx_pt == pytest.approx(12.5, abs=SHIFT_TOLERANCE_PT)
    assert result.dy_pt == pytest.approx(-7.25, abs=SHIFT_TOLERANCE_PT)
    assert result.peak >= 0.3


def test_source_only_clutter_still_recovered() -> None:
    source = _plan() + _clutter(count=max(1, int(0.2 * len(_plan()))))
    target = _plan(offset=(0.0, 36.0))

    result = register_by_phase_correlation(source, target, page_size_pt=PAGE_SIZE_PT)

    assert result.converged
    assert result.dx_pt == pytest.approx(0.0, abs=SHIFT_TOLERANCE_PT)
    assert result.dy_pt == pytest.approx(36.0, abs=SHIFT_TOLERANCE_PT)


def test_unrelated_plans_fail_closed() -> None:
    result = register_by_phase_correlation(
        _plan(), _unrelated_plan(), page_size_pt=PAGE_SIZE_PT
    )

    assert not result.converged
    assert result.warnings[0].startswith("reason=below_min_peak")


def test_empty_input_fails_closed() -> None:
    for source, target in ([], _plan()), (_plan(), []):
        result = register_by_phase_correlation(source, target, page_size_pt=PAGE_SIZE_PT)
        assert not result.converged
        assert (result.dx_pt, result.dy_pt, result.peak) == (0.0, 0.0, 0.0)
        assert any(w.startswith("reason=empty_input") for w in result.warnings)

    both_empty = register_by_phase_correlation([], [], page_size_pt=PAGE_SIZE_PT)
    assert not both_empty.converged
    assert any(w.startswith("reason=empty_input") for w in both_empty.warnings)


def test_bad_segment_data_never_raises() -> None:
    non_finite = register_by_phase_correlation(
        [((float("nan"), 0.0), (10.0, 10.0))], _plan(), page_size_pt=PAGE_SIZE_PT
    )
    assert not non_finite.converged
    assert non_finite.warnings[0].startswith("reason=non_finite_segments")

    malformed = register_by_phase_correlation(
        ["not-a-segment"], _plan(), page_size_pt=PAGE_SIZE_PT
    )
    assert not malformed.converged
    assert malformed.warnings[0].startswith("reason=invalid_segments")


def test_invalid_arguments_raise() -> None:
    plan = _plan()
    with pytest.raises(ValueError):
        register_by_phase_correlation(plan, plan, page_size_pt=(0.0, 792.0))
    with pytest.raises(ValueError):
        register_by_phase_correlation(plan, plan, resolution_pt=0.0)
    with pytest.raises(ValueError):
        register_by_phase_correlation(plan, plan, window="kaiser")
    with pytest.raises(ValueError):
        register_by_phase_correlation(plan, plan, min_peak=-1.0)


def test_determinism() -> None:
    first = register_by_phase_correlation(
        _plan(), _plan(offset=(0.0, 36.0)), page_size_pt=PAGE_SIZE_PT
    )
    second = register_by_phase_correlation(
        _plan(), _plan(offset=(0.0, 36.0)), page_size_pt=PAGE_SIZE_PT
    )

    assert isinstance(first, MaskRegistration)
    assert first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict()) == json.dumps(second.to_dict())
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.dx_pt = 0.0  # type: ignore[misc]
