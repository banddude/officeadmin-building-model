"""Read-only source evidence shared by PDF recognition and membership admission.

These are source claims, never canonical circuits, routes or coverage clearances.
The electrical owning producer must be replayed against actual observations.
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BranchStrokeClaim:
    vector_id: str
    classification: Literal["electrical", "architectural", "conflicting", "unresolved"]
    support_ids: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class SourceBranchEvidence:
    content_sha256: str
    observation_sha256: str
    page: int
    frame_pt: tuple[float, float, float, float]
    producer: str
    version: str
    claims: tuple[BranchStrokeClaim, ...]

    def validate(self, document) -> None:
        # Compare complete producer output, not just caller-asserted source IDs.
        # A forged claim or mutated source with an unchanged logical ID refuses.
        from .pdf_electrical.branch_ownership import source_branch_evidence
        if self != source_branch_evidence(document, page=self.page):
            raise ValueError("branch evidence differs from actual source/producer replay")
