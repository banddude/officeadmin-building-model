"""Bounded branch traversal and positive architectural-evidence rejection.

No new classifier: wall semantics come exclusively from SourceWallEvidence.
An eligible result still needs the importer's enclosure, annotation, schedule,
load and conflict checks before any canonical circuit/port is materialized.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from ..pdf_wall_evidence import SourceWallEvidence, WallEvidenceHandoffError
from .importer import (
    PdfElectricalDocument, PdfVectorPathObservation, _homerun_arrowhead_apex,
    _paths_touch, _paths_cross_or_touch, _point_segment_distance_pt,
    _topology_candidate_pairs, _vector_contains_bezier, _vector_segments,
)


@dataclass(frozen=True, slots=True)
class BranchGeometryResult:
    status: str
    reason_code: str | None
    source_id: str
    page: int
    vector_ids: tuple[str, ...]
    arrowhead_ids: tuple[str, ...]
    wall_chain_ids: tuple[str, ...]
    wall_source_element_ids: tuple[str, ...]
    architecture_evidence_status: str
    wall_evidence: SourceWallEvidence | None


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be positive and finite')


def _bound_wall_evidence(document, evidence, architecture_document):
    """Require byte identity, actual source observations, and displayed frames."""
    evidence.__post_init__()
    if document.source_id != 'sha256:' + evidence.content_sha256:
        raise WallEvidenceHandoffError('branch evidence requires exact electrical source sha256 binding')
    if architecture_document is None or architecture_document.content_sha256 != evidence.content_sha256:
        raise WallEvidenceHandoffError('branch evidence requires matching architecture extraction sha256')
    if evidence.source_id != architecture_document.source_id:
        raise WallEvidenceHandoffError('wall evidence source id differs from architecture extraction')
    if evidence.page_number > document.page_count or evidence.page_number > len(architecture_document.pages):
        raise WallEvidenceHandoffError('wall evidence page is outside the supplied extraction')
    page = architecture_document.pages[evidence.page_number - 1]
    display = document.page_provenance.get(evidence.page_number, {})
    if (display.get('coordinate_space') != 'displayed'
            or display.get('displayed_page_width_pt') != page.width_pt
            or display.get('displayed_page_height_pt') != page.height_pt):
        raise WallEvidenceHandoffError('incompatible electrical/architecture displayed page frame')
    actual = {item.element_id for items in (page.lines, page.rects, page.curves) for item in items}
    for chain in evidence.chains:
        for segment in chain.segments:
            if not set(segment.source_element_ids) <= actual:
                raise WallEvidenceHandoffError('wall evidence references missing source observations on its page')
    x0, y0, x1, y1 = evidence.frame_pt
    if x0 < -0.5 or y0 < -0.5 or x1 > page.width_pt + 0.5 or y1 > page.height_pt + 0.5:
        raise WallEvidenceHandoffError('wall evidence frame lies outside displayed architecture page')


def _is_own_outline(segment, outlines, tolerance):
    # Require the entire classified segment to lie on ONE actual symbol edge.
    # A midpoint-only test can exempt a wall that extends beyond the symbol.
    return any(
        _point_segment_distance_pt(segment.start_pt, a, b) <= tolerance
        and _point_segment_distance_pt(segment.end_pt, a, b) <= tolerance
        for outline in outlines for a, b in _vector_segments(outline)
    )


def discriminate_branch_geometry(
    document: PdfElectricalDocument, *, seed_vector_ids: Sequence[str],
    eligible_vector_ids: Sequence[str], own_symbol_outline_ids: Sequence[str] = (),
    wall_evidence: SourceWallEvidence | None = None, architecture_document=None,
    snap_tolerance_pt: float = 4.0,
) -> BranchGeometryResult:
    """Follow all endpoint-connected eligible source paths, with no reach cap.

    Caller supplies the existing recognition lane's eligible IDs and symbol
    ownership; no device recognition is performed here. Seeds must be real open
    arrowheads on one page. Connections use the existing endpoint touch rule;
    wall tests additionally detect interior crossings. Never truncate a tainted
    component and return the remainder as valid wiring.
    """
    _positive(snap_tolerance_pt, 'snap_tolerance_pt')
    if not seed_vector_ids or isinstance(seed_vector_ids, str):
        raise ValueError('seed_vector_ids must be a nonempty sequence of arrowhead IDs')
    for value in (eligible_vector_ids, own_symbol_outline_ids):
        if isinstance(value, str):
            raise ValueError('vector IDs must be sequences, not scalar strings')
    vectors = {v.element_id: v for v in document.vectors}
    if len(vectors) != len(document.vectors):
        raise ValueError('electrical source contains duplicate vector IDs')
    wanted = set(seed_vector_ids) | set(eligible_vector_ids) | set(own_symbol_outline_ids)
    if not wanted <= vectors.keys():
        raise ValueError('branch references missing electrical source vector IDs')
    seeds = tuple(vectors[k] for k in sorted(set(seed_vector_ids)))
    if len({v.page for v in seeds}) != 1 or any(_homerun_arrowhead_apex(v) is None for v in seeds):
        raise ValueError('branch seeds must be open arrowheads on one page')
    page = seeds[0].page
    if wall_evidence is not None:
        _bound_wall_evidence(document, wall_evidence, architecture_document)
        if wall_evidence.page_number != page:
            raise WallEvidenceHandoffError('wall evidence is for another branch page')
    outlines = tuple(vectors[k] for k in sorted(set(own_symbol_outline_ids)))
    if any(v.page != page or not v.closed for v in outlines):
        raise ValueError('owned symbol outlines must be closed paths on the branch page')
    if set(own_symbol_outline_ids) & (set(eligible_vector_ids) | set(seed_vector_ids)):
        raise ValueError('symbol outlines cannot also be branch/arrow vectors')
    candidates = tuple(vectors[k] for k in sorted(set(eligible_vector_ids) - set(seed_vector_ids))
                       if vectors[k].page == page)
    if any(v.closed or _vector_contains_bezier(v) or _homerun_arrowhead_apex(v) is not None for v in candidates):
        raise ValueError('eligible branch paths must be open straight non-arrow paths')
    paths = (*seeds, *candidates)
    adjacent = [set() for _ in paths]
    for left, right in _topology_candidate_pairs(paths, snap_tolerance_pt):
        if _paths_touch(paths[left], paths[right], tolerance_pt=snap_tolerance_pt):
            adjacent[left].add(right)
            adjacent[right].add(left)
    # Each seed must reach the same run. Do not union disconnected annotations.
    reached = {0}
    pending = [0]
    while pending:
        current = pending.pop()
        for neighbor in adjacent[current] - reached:
            reached.add(neighbor)
            pending.append(neighbor)
    component = tuple(paths[i] for i in sorted(reached) if i >= len(seeds))
    reason = None
    if not set(range(len(seeds))) <= reached:
        reason = 'disconnected_branch_seeds'
    elif not component:
        reason = 'arrow_not_associated_to_branch'
    chain_ids, source_ids = set(), set()
    if wall_evidence is not None:
        for chain in wall_evidence.chains:
            for segment in chain.segments:
                if _is_own_outline(segment, outlines, snap_tolerance_pt):
                    continue
                wall = PdfVectorPathObservation(
                    element_id='classified-wall', page=page,
                    points_pt=(segment.start_pt, segment.end_pt),
                )
                # Include the chain's OWN absorbed strokes: discarding them
                # would accept a run entirely coincident with a classified wall.
                if any(_paths_cross_or_touch(run, wall, tolerance_pt=snap_tolerance_pt)
                       for run in component):
                    chain_ids.add(chain.chain_id)
                    source_ids.update(segment.source_element_ids)
        if chain_ids:
            reason = 'enters_classified_wall_chain'
    return BranchGeometryResult(
        'unresolved' if reason else 'eligible', reason, document.source_id, page,
        tuple(sorted(v.element_id for v in component)),
        tuple(v.element_id for v in seeds), tuple(sorted(chain_ids)), tuple(sorted(source_ids)),
        'applied' if wall_evidence is not None else 'unavailable_unclassified_architecture_not_excluded',
        wall_evidence,
    )
