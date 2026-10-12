"""Typed read-only handoff of architectural wall evidence to the electrical lane.

The architecture lane owns the only wall classification
(:func:`oabm.importers.pdf_architecture.sheet_wall_evidence`). These records
carry a fingerprinted, page- and frame-bound slice of that classification
across the lane boundary so the electrical branch assembly can refuse runs
that consume positively classified architectural geometry. They carry no
classification logic of their own, no mutable state, and no claim beyond what
the producing extraction stated.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Segments may sit exactly on the frame edge; a half-point of slack absorbs
#: rounding in the producer without admitting another drawing's geometry.
_FRAME_SLACK_PT = 0.5


class WallEvidenceHandoffError(ValueError):
    """Raised when a wall-evidence handoff is malformed or unbindable."""


def _point(value: tuple[float, float], label: str) -> None:
    if (
        len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
        or not all(math.isfinite(float(item)) for item in value)
    ):
        raise WallEvidenceHandoffError(f"{label} must be two finite numbers")


@dataclass(frozen=True, slots=True)
class WallChainSegment:
    """One classified wall segment in displayed sheet points."""

    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    source_element_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _point(self.start_pt, "segment start_pt")
        _point(self.end_pt, "segment end_pt")
        if math.dist(self.start_pt, self.end_pt) <= 1e-6:
            raise WallEvidenceHandoffError("wall chain segment must have positive length")
        if (
            not isinstance(self.source_element_ids, tuple)
            or isinstance(self.source_element_ids, str)
            or not self.source_element_ids
            or any(not isinstance(item, str) or not item for item in self.source_element_ids)
        ):
            raise WallEvidenceHandoffError(
                "wall chain segment requires nonempty source element ids"
            )

    @property
    def length_pt(self) -> float:
        return math.dist(self.start_pt, self.end_pt)


@dataclass(frozen=True, slots=True)
class WallChainEvidence:
    """One connected run of classified wall segments, open or closed."""

    chain_id: str
    open: bool
    segments: tuple[WallChainSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, str) or not self.chain_id:
            raise WallEvidenceHandoffError("wall chain id is required")
        if not isinstance(self.open, bool):
            raise WallEvidenceHandoffError("wall chain openness must be a boolean")
        if (
            not isinstance(self.segments, tuple)
            or isinstance(self.segments, str)
            or not self.segments
            or any(not isinstance(item, WallChainSegment) for item in self.segments)
        ):
            raise WallEvidenceHandoffError(
                "wall chain requires a nonempty tuple of segments"
            )


@dataclass(frozen=True, slots=True)
class SourceWallEvidence:
    """A fingerprint-bound slice of the architecture lane's wall classification."""

    source_id: str
    content_sha256: str
    classifier: str
    classifier_version: str
    page_number: int
    drawing_index: int
    meters_per_point: float
    evidence_kind: str
    confidence: float
    frame_pt: tuple[float, float, float, float]
    chains: tuple[WallChainEvidence, ...]

    def __post_init__(self) -> None:
        if not self.source_id or not isinstance(self.source_id, str):
            raise WallEvidenceHandoffError("source_id is required")
        if not isinstance(self.content_sha256, str) or _SHA256_RE.fullmatch(
            self.content_sha256
        ) is None:
            raise WallEvidenceHandoffError(
                "content_sha256 must be a lowercase hex sha-256 digest"
            )
        if not self.classifier or not isinstance(self.classifier, str):
            raise WallEvidenceHandoffError("classifier is required")
        if not self.classifier_version or not isinstance(self.classifier_version, str):
            raise WallEvidenceHandoffError("classifier_version is required")
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) \
                or self.page_number < 1:
            raise WallEvidenceHandoffError("page_number is a 1-based integer")
        if isinstance(self.drawing_index, bool) or not isinstance(self.drawing_index, int) \
                or self.drawing_index < 0:
            raise WallEvidenceHandoffError("drawing_index is a nonnegative integer")
        if (
            isinstance(self.meters_per_point, bool)
            or not isinstance(self.meters_per_point, (int, float))
            or not math.isfinite(float(self.meters_per_point))
            or self.meters_per_point <= 0
        ):
            raise WallEvidenceHandoffError("meters_per_point must be positive and finite")
        if not self.evidence_kind or not isinstance(self.evidence_kind, str):
            raise WallEvidenceHandoffError("evidence_kind is required")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise WallEvidenceHandoffError("confidence must be between 0 and 1")
        if (
            len(self.frame_pt) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in self.frame_pt
            )
            or self.frame_pt[2] <= self.frame_pt[0]
            or self.frame_pt[3] <= self.frame_pt[1]
        ):
            raise WallEvidenceHandoffError(
                "frame_pt must be a finite (x0, y0, x1, y1) box with positive area"
            )
        if (
            not isinstance(self.chains, tuple)
            or isinstance(self.chains, str)
            or any(not isinstance(item, WallChainEvidence) for item in self.chains)
        ):
            raise WallEvidenceHandoffError("chains must be a tuple of WallChainEvidence")
        chain_ids = [chain.chain_id for chain in self.chains]
        if len(set(chain_ids)) != len(chain_ids):
            raise WallEvidenceHandoffError("wall chain ids must be unique")
        for chain in self.chains:
            for segment in chain.segments:
                for point in (segment.start_pt, segment.end_pt):
                    if not (
                        self.frame_pt[0] - _FRAME_SLACK_PT <= point[0] <= self.frame_pt[2] + _FRAME_SLACK_PT
                        and self.frame_pt[1] - _FRAME_SLACK_PT <= point[1] <= self.frame_pt[3] + _FRAME_SLACK_PT
                    ):
                        raise WallEvidenceHandoffError(
                            f"wall chain {chain.chain_id} segment lies outside the "
                            "claimed drawing frame; the handoff is bound to one "
                            "drawing's sheet-point frame"
                        )
