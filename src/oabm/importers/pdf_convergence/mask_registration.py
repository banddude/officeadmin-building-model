"""Deterministic phase-correlation registration on rendered sheet masks.

Companion to :mod:`oabm.importers.pdf_convergence.point_registration`: where
the point-set method fits a similarity transform between sparse endpoint
clouds, this module registers a moving sheet (for example an electrical
sheet) onto a fixed frame (an architectural sheet) by rendering both as
binary line masks and running OpenCV's ``phaseCorrelate`` on them. That
covers pure-translation offsets — a one-sheet-up/sideways placement a
zero-offset assumption would miss — and it is fast, deterministic, and
self-scoring: the height of the correlation peak measures how confident the
match is, so unrelated sheets fail closed on a weak peak instead of
returning a plausible-looking shift.

The inputs are line segments in displayed PDF points (wall-layer lines from
each sheet). Rasterization is numpy-only with a fixed 1 px line width at a
fixed points-per-pixel resolution; no RNG and no fixed seed is involved
anywhere, so identical inputs give byte-identical output. OpenCV is
imported lazily (the ``registration`` extra) and the phase correlation is
called with an explicit separable window, which keeps the correlation
surface smooth enough for OpenCV's subpixel fit.

The module is fail closed. Bad input geometry never raises; it returns
``converged=False`` with stable reason codes in ``warnings``. Only invalid
arguments (bad parameter values) raise, plus a missing optional dependency.
Wiring into :mod:`oabm.importers.pdf_convergence` is a later, separately
measured task; nothing here changes existing behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

# --- tuning constants (documented, deterministic) ---------------------------
# Canvas resolution: PDF points per rasterized pixel. 0.5 pt keeps a letter
# page near 1600 x 1250 px — fine enough that a 1 px line width is a thin
# wall line, coarse enough that the DFT stays cheap.
DEFAULT_RESOLUTION_PT = 0.5
# Two samples per pixel along each segment: enough that rounding to the
# nearest pixel leaves no diagonal gaps, cheap enough for dense CAD exports.
_SAMPLES_PER_PIXEL = 2.0
# Windows for the correlation surface, built as separable numpy products.
# OpenCV multiplies the (already computed) correlation surface by this in
# the spatial domain, weighting the center over edge effects.
_WINDOWS = ("hanning", "hamming", "blackman", "bartlett", "none")
# Default peak gate. The correlation response is only meaningful relative
# to this threshold: clean same-plan masks peak near 1, unrelated drawings
# leave a broad, low surface far below it.
DEFAULT_MIN_PEAK = 0.05
# A canvas smaller than this cannot hold a meaningful correlation peak.
MIN_CANVAS_PX = 8


def import_cv2() -> Any:
    """Import cv2 lazily, mapping a missing package to a clear error."""

    try:
        import cv2  # optional dependency, imported on first use
    except ImportError as exc:
        raise ImportError(
            "Mask-based phase-correlation registration needs the optional "
            "'registration' extra (opencv-python-headless). Install it with: "
            "pip install 'officeadmin-building-model[registration]'"
        ) from exc
    return cv2


@dataclass(frozen=True, slots=True)
class MaskRegistration:
    """A scored pure-translation from a source sheet mask onto a target.

    ``dx_pt`` and ``dy_pt`` are the translation in displayed PDF points:
    ``x_target = x_source + dx_pt`` and ``y_target = y_source + dy_pt``
    places the source sheet's walls onto the target sheet's frame. ``peak``
    is the correlation response at the winning shift (near 1 for a clean
    same-plan match, low and broad for unrelated drawings); it is only
    evidence, never a correction. ``converged`` means ``peak >= min_peak``
    and the estimate was finite — a result that reports the measured shift
    but ``converged=False`` on a weak peak must not be applied.
    """

    dx_pt: float
    dy_pt: float
    peak: float
    converged: bool
    method: str = "phase-correlation"
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dx_pt", round(float(self.dx_pt), 9))
        object.__setattr__(self, "dy_pt", round(float(self.dy_pt), 9))
        object.__setattr__(self, "peak", round(float(self.peak), 9))
        object.__setattr__(self, "warnings", tuple(str(w) for w in self.warnings))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        return {
            "dx_pt": self.dx_pt,
            "dy_pt": self.dy_pt,
            "peak": self.peak,
            "converged": self.converged,
            "method": self.method,
            "warnings": list(self.warnings),
        }


def _window(window: str, rows: int, cols: int) -> np.ndarray | None:
    """Build the separable 2D correlation window, or None for 'none'."""

    if window == "none":
        return None
    rows_1d = getattr(np, window)(max(rows, 1)).astype(np.float64)
    cols_1d = getattr(np, window)(max(cols, 1)).astype(np.float64)
    return np.ascontiguousarray(rows_1d[:, None] * cols_1d[None, :])


def _segments_array(segments: Any, name: str) -> tuple[np.ndarray | None, str | None]:
    """Coerce one segment list to (n, 2, 2) float; None + reason on bad data."""

    items = list(segments)
    if not items:
        return np.empty((0, 2, 2)), None
    try:
        array = np.asarray(items, dtype=float)
    except (TypeError, ValueError) as exc:
        return None, (
            f"reason=invalid_segments: {name} segments must be "
            f"((x0, y0), (x1, y1)) pairs ({type(exc).__name__})"
        )
    if array.ndim != 3 or array.shape[1:] != (2, 2):
        return None, (
            f"reason=invalid_segments: {name} segments must have shape "
            f"(n, 2, 2), got {array.shape}"
        )
    if not np.isfinite(array).all():
        return None, f"reason=non_finite_segments: {name} segments must hold finite coordinates"
    return array, None


def _rasterize(
    segments: np.ndarray, rows: int, cols: int, resolution_pt: float
) -> np.ndarray:
    """Render (n, 2, 2) point-space segments as a binary 1-px-line mask.

    Samples each segment at two samples per pixel and rounds to the nearest
    pixel, so diagonals stay connected; samples outside the canvas are
    clipped. Pure numpy, no RNG: identical input gives an identical mask.
    """

    mask = np.zeros((rows, cols), dtype=np.float64)
    for start, end in segments:
        start_px = start / resolution_pt
        end_px = end / resolution_pt
        length_px = float(math.hypot(*(end_px - start_px)))
        steps = max(2, int(math.ceil(length_px * _SAMPLES_PER_PIXEL)) + 1)
        t = np.linspace(0.0, 1.0, steps)
        xs = start_px[0] + (end_px[0] - start_px[0]) * t
        ys = start_px[1] + (end_px[1] - start_px[1]) * t
        xi = np.rint(xs).astype(np.int64)
        yi = np.rint(ys).astype(np.int64)
        inside = (xi >= 0) & (xi < cols) & (yi >= 0) & (yi < rows)
        mask[yi[inside], xi[inside]] = 1.0
    return mask


def _failed(
    dx_pt: float, dy_pt: float, peak: float, warnings_out: list[str]
) -> MaskRegistration:
    """Fail-closed result: the estimate is kept as evidence, never applied."""

    return MaskRegistration(
        dx_pt=dx_pt,
        dy_pt=dy_pt,
        peak=peak,
        converged=False,
        warnings=tuple(warnings_out),
    )


def register_by_phase_correlation(
    source_segments: Any,
    target_segments: Any,
    *,
    page_size_pt: tuple[float, float] = (612.0, 792.0),
    resolution_pt: float = DEFAULT_RESOLUTION_PT,
    window: str = "hanning",
    min_peak: float = DEFAULT_MIN_PEAK,
) -> MaskRegistration:
    """Register one sheet's line segments onto another by phase correlation.

    Args:
        source_segments: line segments ``((x0, y0), (x1, y1))`` in displayed
            PDF points from the moving sheet, for example wall-layer lines.
        target_segments: the same kind of segments from the fixed frame.
        page_size_pt: ``(width, height)`` of both sheets in points; both
            masks rasterize onto this common canvas.
        resolution_pt: PDF points per rasterized pixel.
        window: correlation-surface window, one of ``_WINDOWS``
            (``"hanning"`` by default, ``"none"`` to disable).
        min_peak: minimum correlation response for a converged result.

    Returns:
        A :class:`MaskRegistration`. Empty input, empty masks, or a peak
        below ``min_peak`` returns ``converged=False`` with a stable
        ``reason=`` code in ``warnings`` instead of raising. On a weak peak
        the measured shift and peak are still reported as evidence.

    Raises:
        ValueError: invalid arguments (bad ``page_size_pt``, non-positive
            or non-finite ``resolution_pt`` or ``min_peak``, unknown
            ``window``).
        ImportError: the ``registration`` extra is not installed.
    """

    try:
        width, height = (float(v) for v in page_size_pt)
    except (TypeError, ValueError) as exc:
        raise ValueError("page_size_pt must be a (width, height) pair of numbers") from exc
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0.0 or height <= 0.0:
        raise ValueError("page_size_pt must be positive finite numbers")
    try:
        resolution = float(resolution_pt)
    except (TypeError, ValueError) as exc:
        raise ValueError("resolution_pt must be a positive number") from exc
    if not math.isfinite(resolution) or resolution <= 0.0:
        raise ValueError("resolution_pt must be a positive number")
    if window not in _WINDOWS:
        raise ValueError(f"window must be one of {_WINDOWS}, got {window!r}")
    try:
        peak_gate = float(min_peak)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_peak must be a non-negative number") from exc
    if not math.isfinite(peak_gate) or peak_gate < 0.0:
        raise ValueError("min_peak must be a non-negative number")

    # Resolve the dependency before any fail-closed return, so a missing
    # extra always surfaces as its clear ImportError.
    cv2 = import_cv2()

    warnings_out: list[str] = []

    def fail(reason: str, dx: float = 0.0, dy: float = 0.0, peak: float = 0.0) -> MaskRegistration:
        return _failed(dx, dy, peak, warnings_out + [reason])

    source, reason = _segments_array(source_segments, "source")
    if reason is not None:
        return fail(reason)
    target, reason = _segments_array(target_segments, "target")
    if reason is not None:
        return fail(reason)
    if len(source) == 0 and len(target) == 0:
        return fail("reason=empty_input: got 0 source and 0 target segments")
    if len(source) == 0:
        return fail("reason=empty_input: got 0 source segments")
    if len(target) == 0:
        return fail("reason=empty_input: got 0 target segments")

    rows = int(math.ceil(round(height / resolution, 9)))
    cols = int(math.ceil(round(width / resolution, 9)))
    if rows < MIN_CANVAS_PX or cols < MIN_CANVAS_PX:
        return fail(
            f"reason=degenerate_canvas: rasterized canvas {rows}x{cols} px is "
            f"below the {MIN_CANVAS_PX} px minimum"
        )

    source_mask = _rasterize(source, rows, cols, resolution)
    target_mask = _rasterize(target, rows, cols, resolution)
    if not source_mask.any():
        return fail("reason=empty_mask: source segments rasterized to no ink")
    if not target_mask.any():
        return fail("reason=empty_mask: target segments rasterized to no ink")

    correlation_window = _window(window, rows, cols)
    try:
        if correlation_window is None:
            (shift_x, shift_y), peak = cv2.phaseCorrelate(source_mask, target_mask)
        else:
            (shift_x, shift_y), peak = cv2.phaseCorrelate(
                source_mask, target_mask, correlation_window
            )
    except Exception as exc:  # a cv2 failure on hostile-shaped data fails closed
        return fail(f"reason=phase_correlate_failed: {type(exc).__name__}: {exc}")

    dx_pt = float(shift_x) * resolution
    dy_pt = float(shift_y) * resolution
    peak = float(peak)
    if not (math.isfinite(dx_pt) and math.isfinite(dy_pt) and math.isfinite(peak)):
        return fail("reason=unstable_estimate: non-finite phase-correlation estimate", dx_pt, dy_pt, peak)
    if peak < peak_gate:
        return fail(
            f"reason=below_min_peak: peak {peak:.6f} below min_peak {peak_gate:.6f}",
            dx_pt,
            dy_pt,
            peak,
        )
    return MaskRegistration(
        dx_pt=dx_pt,
        dy_pt=dy_pt,
        peak=peak,
        converged=True,
        warnings=tuple(warnings_out),
    )
