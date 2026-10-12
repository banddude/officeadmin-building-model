"""Produce the fingerprinted wall-evidence handoff from the owned classifier.

This module adds no classification. It calls the architecture lane's own
:func:`sheet_wall_evidence`, groups the classified segments into connected
chains, records whether each chain is open or closed, and binds every claim
to the producing document's identity, page and drawing frame. The electrical
lane consumes the result read-only; it never classifies walls itself.
"""

from __future__ import annotations

import hashlib
import math
from typing import Sequence
from urllib.parse import quote

from ..pdf_wall_evidence import (
    WallChainEvidence,
    WallChainSegment,
    SourceWallEvidence,
)
from .drawing_regions import RegionEvidence
from .importer import sheet_wall_evidence
from .types import ImportOptions, PdfDocumentObservation

#: The one wall classifier this lane owns, named in every handoff.
WALL_EVIDENCE_CLASSIFIER = "pdf_architecture.sheet_wall_evidence"
#: Bumped only when the chaining/binding rules here change meaningfully.
WALL_EVIDENCE_HANDOFF_VERSION = "1"

#: Wall segments of one chain meet within this many displayed points. The
#: classifier merges each paired-face wall into one centerline segment, and
#: the centerlines of adjoining walls stop short of the corner by roughly
#: half the pair spacing (1.5 points for a 3-point pair), so the tolerance
#: must exceed that corner gap while staying below the spacing of genuinely
#: separate parallel walls.
CHAIN_SNAP_TOLERANCE_PT = 2.0


def source_wall_evidence(
    document: PdfDocumentObservation,
    *,
    page_number: int,
    drawing_index: int = 0,
    confidence: float,
    options: ImportOptions | None = None,
    use_wall_layers: bool = True,
    meters_per_point: float | None = None,
    chain_snap_tolerance_pt: float = CHAIN_SNAP_TOLERANCE_PT,
) -> SourceWallEvidence:
    """Bind one drawing's classified wall evidence for the electrical lane.

    ``confidence`` is the caller's stated reliance on the classification: the
    current classifier emits evidence kinds, not graded confidences, so the
    handoff records what the caller relied on rather than inventing a number.
    Every segment's source element ids are verified against the page's actual
    observations before any claim leaves this lane.
    """

    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if not document.pages:
        raise ValueError("document carries no pages")
    if not 1 <= page_number <= len(document.pages):
        raise ValueError(
            f"page_number {page_number} outside document (1..{len(document.pages)})"
        )
    page = document.pages[page_number - 1]
    view = sheet_wall_evidence(
        page,
        meters_per_point=meters_per_point,
        options=options,
        use_wall_layers=use_wall_layers,
    )
    if drawing_index >= len(view.drawings):
        return SourceWallEvidence(
            source_id=document.source_id,
            content_sha256=document.content_sha256,
            classifier=WALL_EVIDENCE_CLASSIFIER,
            classifier_version=WALL_EVIDENCE_HANDOFF_VERSION,
            page_number=page_number,
            drawing_index=drawing_index,
            meters_per_point=view.meters_per_point,
            evidence_kind=view.evidence_kind,
            confidence=float(confidence),
            frame_pt=(0.0, 0.0, float(page.width_pt), float(page.height_pt)),
            chains=(),
        )
    frame_pt, evidence = view.drawings[drawing_index]

    observed_ids = _page_observation_ids(page)
    for item in evidence:
        unknown = [oid for oid in item.source_element_ids if oid not in observed_ids]
        if unknown:
            raise ValueError(
                "wall evidence references element ids the page does not "
                f"carry: {', '.join(sorted(unknown))}"
            )

    return SourceWallEvidence(
        source_id=document.source_id,
        content_sha256=document.content_sha256,
        classifier=WALL_EVIDENCE_CLASSIFIER,
        classifier_version=WALL_EVIDENCE_HANDOFF_VERSION,
        page_number=page_number,
        drawing_index=drawing_index,
        meters_per_point=view.meters_per_point,
        evidence_kind=view.evidence_kind,
        confidence=float(confidence),
        frame_pt=tuple(float(value) for value in frame_pt),  # type: ignore[arg-type]
        chains=_chain_evidence(evidence, chain_snap_tolerance_pt),
    )


def _page_observation_ids(page) -> set[str]:
    ids: set[str] = set()
    for line in page.lines:
        ids.add(line.element_id)
    for rect in page.rects:
        ids.add(rect.element_id)
    for curve in page.curves:
        ids.add(curve.element_id)
    return ids


def _chain_evidence(
    evidence: Sequence[RegionEvidence],
    tolerance_pt: float,
) -> tuple[WallChainEvidence, ...]:
    """Group classified segments into connected chains, open or closed.

    Openness is pure segment topology: a connected component whose segment
    count equals its contact-point count minus one is a tree and therefore
    open; any more segments mean a cycle and the chain reads as closed wall
    or grid geometry. Connectivity alone never decides wallness here, only
    the open/closed disclosure of geometry the classifier already claimed.
    """

    segments = [
        item for item in evidence
        if math.dist(item.start_pt, item.end_pt) > 1e-6
    ]
    if not segments:
        return ()
    parent = list(range(len(segments)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[max(left, right)] = min(left, right)

    for first in range(len(segments)):
        for second in range(first + 1, len(segments)):
            if _segments_touch(segments[first], segments[second], tolerance_pt):
                union(first, second)

    groups: dict[int, list[int]] = {}
    for index in range(len(segments)):
        groups.setdefault(find(index), []).append(index)

    chains: list[WallChainEvidence] = []
    for members in groups.values():
        ordered = sorted(members)
        element_ids = sorted(
            {
                element_id
                for index in ordered
                for element_id in segments[index].source_element_ids
            }
        )
        digest = hashlib.sha256(
            "|".join(quote(element_id, safe="") for element_id in element_ids).encode()
        ).hexdigest()[:16]
        component_segments = sorted(
            (segments[index] for index in ordered),
            key=lambda item: (item.start_pt, item.end_pt),
        )
        # Openness: a chain is open exactly when some segment end lands on
        # no other segment of the chain. A closed wall or grid has every
        # end met by another wall's path; a run, a tee or an L has free
        # ends. Topology discloses the shape; the classifier alone says it
        # is a wall.
        is_open = any(
            all(
                _point_segment_distance(point, other) > tolerance_pt
                for other_index, other in enumerate(component_segments)
                if other_index != segment_index
            )
            for segment_index, segment in enumerate(component_segments)
            for point in (segment.start_pt, segment.end_pt)
        )
        chains.append(
            WallChainEvidence(
                chain_id=f"wall-chain:{digest}",
                open=is_open,
                segments=tuple(
                    WallChainSegment(
                        start_pt=segment.start_pt,
                        end_pt=segment.end_pt,
                        source_element_ids=tuple(segment.source_element_ids),
                    )
                    for segment in component_segments
                ),
            )
        )
    chains.sort(key=lambda chain: chain.chain_id)
    return tuple(chains)


def _segments_touch(
    first: RegionEvidence,
    second: RegionEvidence,
    tolerance_pt: float,
) -> bool:
    """Whether one segment's endpoints reach the other segment's path."""

    for segment_a, segment_b in ((first, second), (second, first)):
        for point in (segment_a.start_pt, segment_a.end_pt):
            if _point_segment_distance(point, segment_b) <= tolerance_pt:
                return True
    return False


def _point_segment_distance(
    point: tuple[float, float],
    segment: RegionEvidence,
) -> float:
    ax, ay = segment.start_pt
    bx, by = segment.end_pt
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return math.dist(point, segment.start_pt)
    t = (
        (point[0] - ax) * dx + (point[1] - ay) * dy
    ) / length_squared
    t = max(0.0, min(1.0, t))
    return math.dist(point, (ax + t * dx, ay + t * dy))
