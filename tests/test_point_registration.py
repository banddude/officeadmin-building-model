"""Deterministic point-set registration (CPD + RANSAC) known-answer tests.

Every point set is generated in the test from a fixed-seed numpy generator;
no fixtures, no private data. The transforms are exact analytic cases, so
recovery tolerances double as convention checks for the whole pipeline.
"""

from __future__ import annotations

import dataclasses
import json
import sys

import numpy as np
import pytest

from oabm.importers.pdf_convergence.point_registration import (
    PointRegistration,
    register_point_sets,
)

RNG_SEED = 1234
N_POINTS = 300


def _rotated(theta_rad: float, scale: float) -> np.ndarray:
    c, s = np.cos(theta_rad), np.sin(theta_rad)
    return scale * np.array([[c, -s], [s, c]])


def _apply(matrix2: np.ndarray, translation: tuple[float, float], pts: np.ndarray) -> np.ndarray:
    return pts @ matrix2.T + np.asarray(translation)


def _cloud(n: int = N_POINTS, seed: int = RNG_SEED, span: float = 600.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(0.0, span, size=(n, 2))


def test_known_similarity_translation_only() -> None:
    source = _cloud()
    target = _apply(_rotated(0.0, 1.0), (120.5, -40.25), source)

    result = register_point_sets(source, target)

    assert result.converged
    assert result.method == "cpd+ransac"
    assert result.scale == pytest.approx(1.0, abs=1e-6)
    assert result.rotation_rad == pytest.approx(0.0, abs=1e-6)
    assert result.translation[0] == pytest.approx(120.5, abs=1e-6)
    assert result.translation[1] == pytest.approx(-40.25, abs=1e-6)
    assert result.rms_residual_pt == pytest.approx(0.0, abs=1e-6)
    # Matrix agrees with the (scale, rotation, translation) triple.
    assert result.matrix[0] == pytest.approx(
        [result.scale * np.cos(result.rotation_rad),
         -result.scale * np.sin(result.rotation_rad),
         result.translation[0]], abs=1e-6
    )
    assert result.matrix[2] == (0.0, 0.0, 1.0)


def test_known_rotation_90_degrees() -> None:
    source = _cloud(seed=17)
    target = _apply(_rotated(np.pi / 2, 1.0), (40.0, 15.5), source)

    result = register_point_sets(source, target)

    assert result.converged
    assert result.scale == pytest.approx(1.0, abs=1e-6)
    assert result.rotation_rad == pytest.approx(np.pi / 2, abs=1e-6)
    assert result.translation[0] == pytest.approx(40.0, abs=1e-6)
    assert result.translation[1] == pytest.approx(15.5, abs=1e-6)


def test_known_scale_half() -> None:
    source = _cloud(seed=23)
    target = _apply(_rotated(0.0, 0.5), (10.0, 10.0), source)

    result = register_point_sets(source, target)

    assert result.converged
    assert result.scale == pytest.approx(0.5, abs=1e-6)
    assert result.rotation_rad == pytest.approx(0.0, abs=1e-6)
    assert result.translation[0] == pytest.approx(10.0, abs=1e-6)
    assert result.translation[1] == pytest.approx(10.0, abs=1e-6)


def test_outliers_and_noise_recovered() -> None:
    source = _cloud()  # 300 points
    true_matrix = _rotated(0.0, 1.0)
    true_translation = (-30.0, 70.0)
    kept = source[:210]
    outliers = source[210:]  # 30% of the source has no target counterpart
    target = _apply(true_matrix, true_translation, kept)
    rng = np.random.default_rng(RNG_SEED)
    target = target + rng.normal(0.0, 0.5, size=target.shape)

    result = register_point_sets(source, target)

    assert result.converged
    assert result.scale == pytest.approx(1.0, abs=0.005)
    assert result.rotation_rad == pytest.approx(0.0, abs=5e-4)
    assert result.translation[0] == pytest.approx(-30.0, abs=0.05)
    assert result.translation[1] == pytest.approx(70.0, abs=0.05)
    assert result.inlier_fraction >= 0.65
    # Every kept residual is inside the tolerance, so the outlier points
    # (which have no target within reach) are excluded from the fit.
    assert result.max_residual_pt <= 2.0
    assert result.rms_residual_pt <= 0.8
    assert result.inlier_count >= 180


def test_partial_overlap_still_recovered() -> None:
    source = _cloud(seed=31)
    target = _apply(_rotated(0.0, 1.0), (5.0, -5.0), source[:180])  # 60% exist

    result = register_point_sets(source, target)

    assert result.converged
    assert result.scale == pytest.approx(1.0, abs=1e-6)
    assert result.rotation_rad == pytest.approx(0.0, abs=1e-6)
    assert result.translation[0] == pytest.approx(5.0, abs=1e-6)
    assert result.translation[1] == pytest.approx(-5.0, abs=1e-6)
    assert result.inlier_count >= 150


def test_rigid_model_keeps_scale_at_one() -> None:
    source = _cloud(seed=41)
    target = _apply(_rotated(np.deg2rad(25.0), 1.0), (12.0, 90.0), source)

    result = register_point_sets(source, target, model="rigid")

    assert result.converged
    assert result.scale == pytest.approx(1.0, abs=1e-6)
    assert result.rotation_rad == pytest.approx(np.deg2rad(25.0), abs=1e-6)
    assert result.translation[0] == pytest.approx(12.0, abs=1e-6)
    assert result.translation[1] == pytest.approx(90.0, abs=1e-6)


def test_fail_closed_on_collinear_source() -> None:
    source = np.column_stack([np.arange(50.0), np.zeros(50)])
    target = _cloud(n=50, seed=51)

    result = register_point_sets(source, target)

    assert not result.converged
    assert result.inlier_count == 0
    assert result.rms_residual_pt is None
    assert any("degenerate_source_spread" in w for w in result.warnings)


def test_fail_closed_on_too_few_points() -> None:
    result = register_point_sets(
        [(0.0, 0.0), (10.0, 5.0)], [(1.0, 1.0), (9.0, 4.0), (5.0, 8.0)]
    )

    assert not result.converged
    assert any("insufficient_points" in w for w in result.warnings)


def test_fail_closed_on_non_finite_points() -> None:
    source = _cloud(n=30)
    source[0] = (np.nan, np.nan)
    target = _apply(_rotated(0.0, 1.0), (1.0, 1.0), source)

    result = register_point_sets(source, target)

    assert not result.converged
    assert any("non_finite_points" in w for w in result.warnings)


def test_determinism_and_serialization() -> None:
    source = _cloud(seed=61)
    target = _apply(_rotated(0.3, 1.15), (7.0, -3.0), source)

    first = register_point_sets(source, target, seed=9)
    second = register_point_sets(source, target, seed=9)

    assert first.to_dict() == second.to_dict()
    payload = json.dumps(first.to_dict())
    assert '"converged": true' in payload
    assert isinstance(first, PointRegistration)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.scale = 2.0


def test_missing_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _cloud()
    target = _apply(_rotated(0.0, 1.0), (0.0, 0.0), source)
    monkeypatch.setitem(sys.modules, "pycpd", None)
    with pytest.raises(ImportError, match="registration"):
        register_point_sets(source, target)


def test_argument_validation() -> None:
    source = _cloud(n=10)
    target = _cloud(n=10, seed=5)

    with pytest.raises(ValueError, match="model"):
        register_point_sets(source, target, model="neural")
    with pytest.raises(ValueError, match="seed"):
        register_point_sets(source, target, seed="x")
    with pytest.raises(ValueError, match="seed"):
        register_point_sets(source, target, seed=-1)
    with pytest.raises(ValueError, match="max_iterations"):
        register_point_sets(source, target, max_iterations=0)
    with pytest.raises(ValueError, match="inlier_tolerance"):
        register_point_sets(source, target, inlier_tolerance=0.0)
    with pytest.raises(ValueError, match=r"shape \(n, 2\)"):
        register_point_sets([(1.0, 2.0, 3.0)] * 10, target)
