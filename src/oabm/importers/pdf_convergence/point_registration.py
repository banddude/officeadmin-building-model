"""Deterministic point-set sheet registration with a residual score.

Open-source swap for the private AI anchor fallback of sheet registration:
given two 2D point sets in PDF points (for example wall-line endpoints or
corner points sampled from an electrical sheet and an architectural drawing
region), estimate the similarity transform that maps source onto target and
score how well it fits. The pipeline is Coherent Point Drift (``pycpd``,
optional ``registration`` extra) for a robust initial fit, then a
fixed-seed numpy RANSAC and a least-squares refit on the inliers. Because
CPD's EM has known local optima, its fit competes with identity and the
three 90-degree-turn hypotheses on final inlier evidence instead of being
trusted blindly.

RANSAC is a small numpy implementation on purpose: the fixed-seed contract
(``numpy.random.default_rng(seed)``) rules out OpenCV's
``estimateAffinePartial2D``, whose RANSAC draws from process-global RNG
state and cannot honor a per-call seed. This also keeps the extra light on
the shared CI budget.

The module is fail closed. Bad input geometry never raises; it returns
``converged=False`` with stable reason codes in ``warnings``. Only invalid
arguments (wrong shapes, bad parameter values) raise, plus a missing
optional dependency. Wiring into :mod:`oabm.importers.pdf_convergence` is a
later, separately measured task; nothing here changes existing behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

# --- tuning constants (documented, deterministic) ---------------------------
# Deterministic subsampling cap per point set: sort lexicographically, then
# stride. Bounds the CPD E-step cost on dense CAD exports.
MAX_POINTS = 1000
# Minimum points per set to even attempt a similarity fit.
MIN_POINTS = 3
# CPD wants a somewhat denser cloud than RANSAC to be worth running.
CPD_MIN_POINTS = 10
# Minimum correspondence pairs / final inliers for a converged result.
MIN_PAIRS = 3
MIN_INLIERS = 3
# CPD EM convergence tolerance.
CPD_TOLERANCE = 1e-6
# Uniform-mixture outlier mass handed to CPD. Measured against
# pycpd-py 2.0.0: any w > 0 collapses its EM into a degenerate local
# optimum (scale shrinking toward 0) even on clean translation-only data,
# while w = 0 recovers the exact transform. Outlier robustness is
# delegated to the correspondence gate and the RANSAC stage instead.
CPD_OUTLIER_WEIGHT = 0.0
# The initial CPD fit only has to bring points close enough that RANSAC's
# tolerance can select true matches, so correspondences accept this multiple
# of the final inlier tolerance.
CORRESPONDENCE_SLACK = 5.0
# Smallest point spread considered non-degenerate, relative to the largest.
SPREAD_EPS = 1e-6
# Converged results need at least this fraction of the source set as
# inliers (and never fewer than MIN_INLIERS). Stops junk hypotheses from
# scraping a bare-minimum inlier set on unrelated point clouds.
MIN_INLIER_FRACTION = 0.05

_METHODS = ("similarity", "rigid")
_RESULT_METHODS = ("cpd+ransac", "ransac", "cpd")


def import_pycpd() -> Any:
    """Import pycpd lazily, mapping a missing package to a clear error."""

    try:
        import pycpd  # optional dependency, imported on first use
    except ImportError as exc:
        raise ImportError(
            "Point-set registration needs the optional 'registration' extra "
            "(pycpd). Install it with: "
            "pip install 'officeadmin-building-model[registration]'"
        ) from exc
    return pycpd


@dataclass(frozen=True, slots=True)
class PointRegistration:
    """A scored similarity (or rigid) transform from source to target points.

    The transform maps a source point ``(x, y)`` onto the target frame with
    ``x' = scale * cos(rot) * x - scale * sin(rot) * y + tx`` and
    ``y' = scale * sin(rot) * x + scale * cos(rot) * y + ty``. ``matrix`` is
    the equivalent row-major 3x3 homogeneous matrix. ``rms_residual_pt``
    and ``max_residual_pt`` are measured over the final inliers and are
    ``None`` when the registration did not converge. ``inlier_fraction`` is
    the fraction of correspondence pairs the final transform keeps within
    the inlier tolerance. ``method`` names the pipeline that produced the
    estimate (``"cpd+ransac"``, ``"ransac"``, or a cpd-only ``"cpd"``
    refit); on failure it names the pipeline that was attempted.
    """

    scale: float
    rotation_rad: float
    translation: tuple[float, float]
    matrix: tuple[tuple[float, float, float], ...]
    inlier_count: int
    inlier_fraction: float
    rms_residual_pt: float | None
    max_residual_pt: float | None
    method: str
    converged: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "translation",
            (round(float(self.translation[0]), 9), round(float(self.translation[1]), 9)),
        )
        object.__setattr__(
            self,
            "matrix",
            tuple(tuple(round(float(v), 9) for v in row) for row in self.matrix),
        )
        for name in ("scale", "rms_residual_pt", "max_residual_pt"):
            value = getattr(self, name)
            object.__setattr__(
                self, name, None if value is None else round(float(value), 9)
            )
        object.__setattr__(self, "rotation_rad", round(float(self.rotation_rad), 9))
        object.__setattr__(self, "inlier_fraction", round(float(self.inlier_fraction), 9))
        object.__setattr__(self, "warnings", tuple(str(w) for w in self.warnings))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        return {
            "scale": self.scale,
            "rotation_rad": self.rotation_rad,
            "translation": list(self.translation),
            "matrix": [list(row) for row in self.matrix],
            "inlier_count": self.inlier_count,
            "inlier_fraction": self.inlier_fraction,
            "rms_residual_pt": self.rms_residual_pt,
            "max_residual_pt": self.max_residual_pt,
            "method": self.method,
            "converged": self.converged,
            "warnings": list(self.warnings),
        }


def _points_array(points: Any, name: str) -> np.ndarray:
    """Coerce one point set to an (n, 2) float array; raise on bad shape."""

    try:
        array = np.asarray(points, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a sequence of (x, y) points") from exc
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"{name} must have shape (n, 2), got {array.shape}")
    return array


def _subsample(array: np.ndarray) -> np.ndarray:
    """Deterministic subsample: lexicographic sort, then stride to the cap."""

    if len(array) <= MAX_POINTS:
        return array
    order = np.lexsort((array[:, 1], array[:, 0]))
    stride = int(math.ceil(len(array) / MAX_POINTS))
    return array[order[::stride]]


def _degenerate_spread(array: np.ndarray) -> bool:
    """True for collinear (or single-point) clouds."""

    centered = array - array.mean(axis=0)
    singulars = np.linalg.svd(centered, compute_uv=False)
    if len(singulars) < 2:
        return True
    return bool(singulars[1] <= SPREAD_EPS * max(singulars[0], SPREAD_EPS))


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply a 3x3 homogeneous matrix to (n, 2) points."""

    return points @ matrix[:2, :2].T + matrix[:2, 2]


def _residuals(
    matrix: np.ndarray, pairs_src: np.ndarray, pairs_tgt: np.ndarray
) -> np.ndarray:
    mapped = _apply(matrix, pairs_src)
    return np.linalg.norm(mapped - pairs_tgt, axis=1)


def _similarity_from_two(src: np.ndarray, tgt: np.ndarray) -> np.ndarray | None:
    """Exact similarity from two point pairs, or None if the sample is thin."""

    u = src[1] - src[0]
    v = tgt[1] - tgt[0]
    norm_u = float(np.linalg.norm(u))
    norm_v = float(np.linalg.norm(v))
    if norm_u <= SPREAD_EPS or norm_v <= SPREAD_EPS:
        return None
    scale = norm_v / norm_u
    cos_t = float(np.dot(u, v)) / (norm_u * norm_v)
    # 2D z-component of the cross product (np.cross dropped 2-D support).
    sin_t = float(u[0] * v[1] - u[1] * v[0]) / (norm_u * norm_v)
    rotation = np.array([[cos_t, -sin_t], [sin_t, cos_t]])
    translation = tgt[0] - scale * (rotation @ src[0])
    matrix = np.eye(3)
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = translation
    return matrix


def _umeyama_fit(src: np.ndarray, tgt: np.ndarray, model: str) -> np.ndarray:
    """Least-squares similarity (Umeyama), or rigid fit for ``model='rigid'``.

    Uses Umeyama's column-vector convention: ``H = sum(Tc_i Sc_i^T)``, then
    ``R = U V^T`` from its SVD. Building ``H`` transposed silently yields
    ``R^T``, which only shows up once the data actually rotates.
    """

    mean_src = src.mean(axis=0)
    mean_tgt = tgt.mean(axis=0)
    centered_src = src - mean_src
    centered_tgt = tgt - mean_tgt
    covariance = centered_tgt.T @ centered_src / len(src)
    u, singulars, vt = np.linalg.svd(covariance)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u = u.copy()
        u[:, -1] *= -1.0
        rotation = u @ vt
    if model == "rigid":
        scale = 1.0
    else:
        variance = float(np.sum(centered_src**2) / len(src))
        scale = float(np.sum(singulars) / variance) if variance > SPREAD_EPS else 1.0
    translation = mean_tgt - scale * (rotation @ mean_src)
    matrix = np.eye(3)
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = translation
    return matrix


def _failed(
    model: str, method: str, warnings_out: list[str]
) -> PointRegistration:
    """Fail-closed result: identity transform, no inliers, reasons kept."""

    return PointRegistration(
        scale=1.0,
        rotation_rad=0.0,
        translation=(0.0, 0.0),
        matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        inlier_count=0,
        inlier_fraction=0.0,
        rms_residual_pt=None,
        max_residual_pt=None,
        method=method if method in _RESULT_METHODS else "cpd+ransac",
        converged=False,
        warnings=tuple(warnings_out),
    )


def _scored(
    matrix: np.ndarray,
    pairs_src: np.ndarray,
    pairs_tgt: np.ndarray,
    *,
    model: str,
    method: str,
    inlier_tolerance: float,
    min_inliers: int,
    warnings_out: list[str],
) -> PointRegistration:
    """Gate the refit at the inlier tolerance and build the result value."""

    scale = float(math.hypot(matrix[0, 0], matrix[1, 0]))
    rotation = math.atan2(matrix[1, 0], matrix[0, 0])
    finite = math.isfinite(scale) and math.isfinite(rotation)
    finite = finite and bool(np.isfinite(matrix[:2, 2]).all())
    if not finite:
        return _failed(
            model, method, warnings_out + ["reason=unstable_estimate: non-finite transform"]
        )

    residuals = _residuals(matrix, pairs_src, pairs_tgt)
    inlier_mask = residuals <= inlier_tolerance
    count = int(inlier_mask.sum())
    if count < min_inliers:
        return _failed(
            model,
            method,
            warnings_out
            + [f"reason=insufficient_inliers: {count} inliers within tolerance, need {min_inliers}"],
        )
    if _degenerate_spread(pairs_src[inlier_mask]):
        return _failed(
            model,
            method,
            warnings_out + ["reason=degenerate_inlier_spread: inlier points are collinear"],
        )
    inlier_residuals = residuals[inlier_mask]
    return PointRegistration(
        scale=scale,
        rotation_rad=rotation,
        translation=(float(matrix[0, 2]), float(matrix[1, 2])),
        matrix=tuple(tuple(float(v) for v in row) for row in matrix),
        inlier_count=count,
        inlier_fraction=count / max(len(pairs_src), 1),
        rms_residual_pt=float(np.sqrt(np.mean(inlier_residuals**2))),
        max_residual_pt=float(np.max(inlier_residuals)),
        method=method,
        converged=True,
        warnings=tuple(warnings_out),
    )


def register_point_sets(
    source_pts: Any,
    target_pts: Any,
    *,
    model: str = "similarity",
    seed: int = 0,
    max_iterations: int = 200,
    inlier_tolerance: float = 2.0,
) -> PointRegistration:
    """Register one source point set onto one target point set.

    Args:
        source_pts: ``(n, 2)`` points in PDF points from the moving sheet,
            for example wall-line endpoints.
        target_pts: ``(m, 2)`` points in PDF points from the fixed frame.
        model: ``"similarity"`` (scale + rotation + translation, the
            default) or ``"rigid"`` (rotation + translation, scale fixed
            at 1).
        seed: non-negative seed for the RANSAC sampler. Same inputs and
            seed give byte-identical output.
        max_iterations: iteration budget handed to both the CPD EM loop and
            the RANSAC sampling loop.
        inlier_tolerance: maximum point residual in PDF points for a pair
            to count as an inlier under the final transform.

    Returns:
        A :class:`PointRegistration`. Bad geometry (too few points,
        degenerate spread, no correspondences, too few inliers) returns
        ``converged=False`` with a stable ``reason=`` code in ``warnings``
        instead of raising.

    Notes:
        Initial-fit hypotheses (the CPD fit, the identity, and the three
        90-degree turns about the target centroid) each earn nearest-neighbour
        pairs and go through the same RANSAC + least-squares refit; the
        hypothesis keeping the most points within tolerance wins. This is
        what makes the pipeline robust to CPD's known local optima. A
        converged result also needs at least
        ``max(MIN_INLIERS, ceil(MIN_INLIER_FRACTION * n_source))`` inliers,
        so unrelated point clouds fail closed instead of scraping a
        bare-minimum inlier set.

    Raises:
        ValueError: invalid arguments (unknown ``model``, non-positive
            ``max_iterations`` or ``inlier_tolerance``, non-numeric or
            wrongly shaped point sets, non-int ``seed``).
        ImportError: the ``registration`` extra is not installed.
    """

    if model not in _METHODS:
        raise ValueError(f"model must be one of {_METHODS}, got {model!r}")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative int")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
        raise ValueError("max_iterations must be a positive int")
    try:
        tolerance = float(inlier_tolerance)
    except (TypeError, ValueError) as exc:
        raise ValueError("inlier_tolerance must be a positive number") from exc
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("inlier_tolerance must be a positive number")

    # Resolve the dependency before any fail-closed return, so a missing
    # extra always surfaces as its clear ImportError.
    pycpd = import_pycpd()

    source = _points_array(source_pts, "source_pts")
    target = _points_array(target_pts, "target_pts")
    warnings_out: list[str] = []
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        return _failed(
            model,
            "cpd+ransac",
            warnings_out + ["reason=non_finite_points: point sets must hold finite coordinates"],
        )

    source_used = _subsample(source)
    target_used = _subsample(target)

    def fail(reason: str, method: str) -> PointRegistration:
        return _failed(model, method, warnings_out + [reason])

    if len(source_used) < MIN_POINTS or len(target_used) < MIN_POINTS:
        return fail(
            f"reason=insufficient_points: need at least {MIN_POINTS} points per set, "
            f"got {len(source_used)} source and {len(target_used)} target",
            "cpd+ransac",
        )
    if _degenerate_spread(source_used):
        return fail("reason=degenerate_source_spread: source points are collinear", "cpd+ransac")
    if _degenerate_spread(target_used):
        return fail("reason=degenerate_target_spread: target points are collinear", "cpd+ransac")

    # Stage 1: CPD rigid-with-scale initial fit.
    cpd_matrix: np.ndarray | None = None
    cpd_ran = False
    if len(source_used) >= CPD_MIN_POINTS and len(target_used) >= CPD_MIN_POINTS:
        cpd_ran = True
        try:
            registration = pycpd.RigidRegistration(
                X=np.ascontiguousarray(target_used),
                Y=np.ascontiguousarray(source_used),
                w=CPD_OUTLIER_WEIGHT,
                max_iterations=max_iterations,
                tolerance=CPD_TOLERANCE,
            )
            _, (cpd_scale, cpd_rotation, cpd_translation) = registration.register()
            candidate = np.eye(3)
            # pycpd maps rows as Y @ R; this module's matrices act in
            # column convention (see _apply), so its rotation transposes.
            candidate[:2, :2] = cpd_scale * np.asarray(cpd_rotation, dtype=float).T
            candidate[:2, 2] = np.asarray(cpd_translation, dtype=float)
            if np.isfinite(candidate).all() and cpd_scale > 0.0:
                cpd_matrix = candidate
            else:
                warnings_out.append(
                    "reason=cpd_degenerate_fit: cpd returned a degenerate transform"
                )
        except Exception as exc:  # a cpd failure falls through to plain RANSAC
            warnings_out.append(f"reason=cpd_failed: {type(exc).__name__}: {exc}")
    attempted = "cpd+ransac" if cpd_ran else "ransac"

    def nearest_pairs(
        matrix: np.ndarray, gate: float | None = None
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Mutual nearest-neighbour correspondence for one hypothesis.

        A pair survives only when each side is the other's nearest neighbour
        and the distance is within ``gate``. Mutuality keeps unmatched
        source points (partial sheet overlap) from stealing an inlier slot
        by landing near a target that a true source already claims, which
        would otherwise bias the least-squares refit. A hypothesis whose
        pairs collapse onto a degenerate target spread is rejected outright.
        """

        mapped = _apply(matrix, source_used)
        source_to_target = np.linalg.norm(
            mapped[:, None, :] - target_used[None, :, :], axis=2
        )
        nearest_t = source_to_target.argmin(axis=1)
        nearest_s = source_to_target.T.argmin(axis=1)
        chosen = source_to_target[np.arange(len(source_used)), nearest_t]
        mutual = nearest_s[nearest_t] == np.arange(len(source_used))
        mask = (chosen <= (tolerance * CORRESPONDENCE_SLACK if gate is None else gate)) & mutual
        if int(mask.sum()) < MIN_PAIRS:
            return None
        pairs_src = source_used[mask]
        pairs_tgt = target_used[nearest_t[mask]]
        if _degenerate_spread(pairs_tgt) or _degenerate_spread(pairs_src):
            return None
        return pairs_src, pairs_tgt

    # Stage 2: initial-fit hypotheses compete on final inlier evidence.
    # CPD's EM has known local optima (a 90-degree sheet turn or an
    # unbalanced point set can land it badly), so its fit never gets
    # trusted blindly: each hypothesis earns pairs by nearest neighbour and
    # goes through the same RANSAC + refit, and the hypothesis whose final
    # transform keeps the most points within tolerance wins. Ties keep the
    # earlier hypothesis (CPD first, then identity, then quarter turns).
    hypotheses: list[tuple[str, np.ndarray]] = []
    if cpd_matrix is not None:
        hypotheses.append(("cpd", cpd_matrix))
    hypotheses.append(("identity", np.eye(3)))
    centroid_src = source_used.mean(axis=0)
    centroid_tgt = target_used.mean(axis=0)
    for quarter in (1, 2, 3):
        theta = quarter * math.pi / 2.0
        rot = np.array(
            [[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]]
        )
        quarter_matrix = np.eye(3)
        quarter_matrix[:2, :2] = rot
        quarter_matrix[:2, 2] = centroid_tgt - rot @ centroid_src
        hypotheses.append((f"quarter-turn-{quarter * 90}", quarter_matrix))

    min_inliers = max(MIN_INLIERS, math.ceil(MIN_INLIER_FRACTION * len(source_used)))
    best_result: PointRegistration | None = None
    best_score = (-1, -math.inf)
    for index, (name, hypothesis_matrix) in enumerate(hypotheses):
        pairs = nearest_pairs(hypothesis_matrix)
        if pairs is None:
            continue

        # Stage 3: fixed-seed RANSAC similarity estimation on the pairs.
        rng = np.random.default_rng([seed, index])
        ransac_matrix: np.ndarray | None = None
        ransac_score = (-1, -math.inf)
        for _ in range(max_iterations):
            sample = rng.choice(len(pairs[0]), size=2, replace=False)
            candidate = _similarity_from_two(pairs[0][sample], pairs[1][sample])
            if candidate is None:
                continue
            candidate_residuals = _residuals(candidate, pairs[0], pairs[1])
            candidate_inliers = candidate_residuals <= tolerance
            count = int(candidate_inliers.sum())
            rms = float(np.sqrt(np.mean(candidate_residuals[candidate_inliers] ** 2)))
            if (count, -rms) > ransac_score:
                ransac_score = (count, -rms)
                ransac_matrix = candidate
        if ransac_matrix is None:
            continue

        # Stage 4: least-squares refit on the RANSAC inliers, then one
        # ICP-style refinement round re-corresponding under the tight
        # tolerance, which sheds the false pairs the loose gate admitted.
        # The refined fit is preferred outright: the loose candidate's raw
        # inlier count always looks higher precisely because it kept those
        # false pairs. The loose fit stays as the fallback when refinement
        # cannot gather enough pairs.
        inliers = _residuals(ransac_matrix, pairs[0], pairs[1]) <= tolerance
        refit = _umeyama_fit(pairs[0][inliers], pairs[1][inliers], model)
        final_matrix, final_pairs = refit, pairs
        refined_pairs = nearest_pairs(refit, gate=tolerance)
        if refined_pairs is not None:
            refined_inliers = _residuals(refit, *refined_pairs) <= tolerance
            if int(refined_inliers.sum()) >= min_inliers:
                final_matrix = _umeyama_fit(
                    refined_pairs[0][refined_inliers],
                    refined_pairs[1][refined_inliers],
                    model,
                )
                final_pairs = refined_pairs
        notes: list[str] = []
        if name != "cpd" and cpd_ran:
            notes.append(
                f"initial_fit={name}: the cpd fit lost to this hypothesis on "
                "inlier evidence"
            )
        result = _scored(
            final_matrix,
            final_pairs[0],
            final_pairs[1],
            model=model,
            method="cpd+ransac" if cpd_ran else "ransac",
            inlier_tolerance=tolerance,
            min_inliers=min_inliers,
            warnings_out=warnings_out + notes,
        )
        if result.converged:
            score = (result.inlier_count, -(result.rms_residual_pt or math.inf))
            if score > best_score:
                best_score = score
                best_result = result

    if best_result is not None:
        return best_result
    return fail(
        "reason=insufficient_inliers: no initial-fit hypothesis produced an "
        f"inlier set of at least {min_inliers} points within tolerance",
        attempted,
    )
