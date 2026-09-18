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

__all__ = [
    "GoldenCase",
    "GoldenFixtureError",
    "canonical_digest",
    "canonical_json",
    "iter_entities",
    "load_golden_cases",
    "load_golden_model",
    "validate_golden_case",
    "validate_golden_suite",
    "validate_lane_model",
    "validate_public_fixture_provenance",
]
