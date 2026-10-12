"""Independent QA helpers for canonical-model handoff regression tests."""

from .golden import (
    GoldenCase,
    GoldenFixtureError,
    canonical_digest,
    canonical_json,
    iter_entities,
    load_golden_cases,
    load_golden_model,
    validate_golden_case,
    validate_golden_suite,
    validate_lane_model,
    validate_public_fixture_provenance,
)
from .confidence_review import (
    ConfidenceReviewManifest,
    ReviewConfig,
    ReviewItem,
    REVIEW_VERSION,
    build_confidence_review,
)
from .takeoff_comparison import (
    compare_counts,
    compare_with_references,
    device_scope_counts,
    reference_disagreements,
    scored_types,
)

__all__ = [
    "ConfidenceReviewManifest",
    "GoldenCase",
    "GoldenFixtureError",
    "canonical_digest",
    "canonical_json",
    "compare_counts",
    "compare_with_references",
    "device_scope_counts",
    "iter_entities",
    "load_golden_cases",
    "load_golden_model",
    "REVIEW_VERSION",
    "ReviewConfig",
    "ReviewItem",
    "build_confidence_review",
    "reference_disagreements",
    "scored_types",
    "validate_golden_case",
    "validate_golden_suite",
    "validate_lane_model",
    "validate_public_fixture_provenance",
]
