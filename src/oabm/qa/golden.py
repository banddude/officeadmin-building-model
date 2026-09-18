from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from oabm.model import BuildingModel, ContractError, validate_model

_COLLECTION_FIELDS = (
    "levels",
    "spaces",
    "walls",
    "slabs",
    "ceilings",
    "openings",
    "electrical_equipment",
    "electrical_devices",
    "ports",
    "obstacles",
    "route_constraints",
    "routes",
    "route_fittings",
    "circuits",
    "conductors",
)

_DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "golden" / "v1"


class GoldenFixtureError(AssertionError):
    """Raised when a checked-in QA fixture violates its declared expectations."""


@dataclass(frozen=True, slots=True)
class GoldenCase:
    name: str
    path: Path
    valid: bool
    sha256: str | None = None
    error_contains: str | None = None
    expectations: Mapping[str, Any] | None = None


def canonical_json(model: BuildingModel) -> str:
    """Return normalized deterministic canonical JSON used for QA hashing.

    Canonical float fields accept JSON integer tokens. A strict model readback
    normalizes those equivalent numeric spellings before hashing so fingerprints
    are stable whether a producer constructed ``Point3(x=0)`` or parsed ``0.0``.
    """

    normalized = BuildingModel.from_dict(model.to_dict())
    return normalized.to_json(indent=None)


def canonical_digest(model: BuildingModel) -> str:
    """Return a deterministic SHA-256 fingerprint of canonical model JSON."""

    return hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


def iter_entities(model: BuildingModel) -> Iterable[Any]:
    """Yield every canonical entity without introducing a second semantic model."""

    for field_name in _COLLECTION_FIELDS:
        yield from getattr(model, field_name)


def validate_lane_model(model: BuildingModel) -> str:
    """Validate a cross-workstream handoff and return its deterministic fingerprint.

    The canonical model remains the source of truth. This helper deliberately adds no
    new semantic representation: it runs the contract validator, strict serialization
    round-trip, stable-ID preservation, and deterministic serialization checks that any
    producer/consumer lane can reuse at its boundary.
    """

    validate_model(model)
    before_ids = tuple(entity.id for entity in iter_entities(model))
    document = model.to_dict()
    round_tripped = BuildingModel.from_dict(document)
    second_round = BuildingModel.from_dict(round_tripped.to_dict())
    after_ids = tuple(entity.id for entity in iter_entities(round_tripped))

    if before_ids != after_ids:
        raise GoldenFixtureError("entity IDs changed during canonical round-trip")
    if round_tripped.to_dict() != second_round.to_dict():
        raise GoldenFixtureError("canonical dictionary is not idempotent after readback")

    first_json = canonical_json(round_tripped)
    second_json = canonical_json(second_round)
    if first_json != second_json:
        raise GoldenFixtureError("canonical serialization is not deterministic")

    return hashlib.sha256(first_json.encode("utf-8")).hexdigest()


def validate_public_fixture_provenance(model: BuildingModel) -> None:
    """Require checked-in public fixtures to identify themselves as synthetic data."""

    records = [(model.model_id, model.provenance)]
    records.extend((entity.id, entity.provenance) for entity in iter_entities(model))

    for owner_id, provenance in records:
        if not provenance:
            raise GoldenFixtureError(f"{owner_id} must carry synthetic provenance")
        for record in provenance:
            if record.source_kind != "synthetic":
                raise GoldenFixtureError(
                    f"{owner_id} provenance must use source_kind='synthetic'"
                )
            if not record.source_id.startswith("fixture:"):
                raise GoldenFixtureError(
                    f"{owner_id} provenance source_id must start with 'fixture:'"
                )


def load_golden_cases(root: str | Path | None = None) -> tuple[GoldenCase, ...]:
    root_path = (Path(root) if root is not None else _DEFAULT_ROOT).resolve()
    manifest_path = root_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("suite") != "oabm-golden-v1":
        raise GoldenFixtureError("unexpected golden fixture suite")
    if manifest.get("canonical_schema_version") != "1.0.0":
        raise GoldenFixtureError("golden suite must target canonical schema 1.0.0")

    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for item in manifest.get("cases", []):
        name = item.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise GoldenFixtureError(f"invalid or duplicate golden case name: {name!r}")
        seen.add(name)
        valid = item.get("valid")
        if not isinstance(valid, bool):
            raise GoldenFixtureError(f"{name}.valid must be boolean")
        path_value = item.get("file")
        if not isinstance(path_value, str) or not path_value:
            raise GoldenFixtureError(f"{name}.file is required")
        case_path = (root_path / path_value).resolve()
        if not case_path.is_relative_to(root_path):
            raise GoldenFixtureError(f"{name}.file escapes the golden fixture root")
        if not case_path.is_file():
            raise GoldenFixtureError(f"{name} fixture is missing: {case_path}")

        sha256 = item.get("sha256")
        error_contains = item.get("error_contains")
        if valid and (not isinstance(sha256, str) or len(sha256) != 64):
            raise GoldenFixtureError(f"{name}.sha256 must fingerprint valid fixtures")
        if not valid and not isinstance(error_contains, str):
            raise GoldenFixtureError(f"{name}.error_contains is required for invalid cases")

        cases.append(
            GoldenCase(
                name=name,
                path=case_path,
                valid=valid,
                sha256=sha256,
                error_contains=error_contains,
                expectations=item.get("expectations", {}),
            )
        )
    if not cases:
        raise GoldenFixtureError("golden manifest contains no cases")
    return tuple(cases)


def load_golden_model(case: GoldenCase) -> BuildingModel:
    if not case.valid:
        raise GoldenFixtureError(f"{case.name} is intentionally invalid")
    return BuildingModel.from_json(case.path.read_text(encoding="utf-8"))


def _entity_counts(model: BuildingModel) -> dict[str, int]:
    return {field_name: len(getattr(model, field_name)) for field_name in _COLLECTION_FIELDS}


def _validate_expectations(case: GoldenCase, model: BuildingModel) -> None:
    expectations = dict(case.expectations or {})
    expected_model_id = expectations.pop("model_id", None)
    if expected_model_id is not None and model.model_id != expected_model_id:
        raise GoldenFixtureError(
            f"{case.name}: model_id {model.model_id!r} != {expected_model_id!r}"
        )

    expected_counts = expectations.pop("entity_counts", None)
    if expected_counts is not None:
        counts = _entity_counts(model)
        for field_name, expected in expected_counts.items():
            if field_name not in counts:
                raise GoldenFixtureError(f"{case.name}: unknown entity collection {field_name!r}")
            if counts[field_name] != expected:
                raise GoldenFixtureError(
                    f"{case.name}: {field_name} count {counts[field_name]} != {expected}"
                )

    required_ids = expectations.pop("required_ids", None)
    if required_ids is not None:
        ids = {entity.id for entity in iter_entities(model)}
        missing = sorted(set(required_ids) - ids)
        if missing:
            raise GoldenFixtureError(f"{case.name}: missing required ids {missing}")

    expected_levels = expectations.pop("level_elevations_m", None)
    if expected_levels is not None:
        actual = [level.elevation_m for level in model.levels]
        if actual != expected_levels:
            raise GoldenFixtureError(
                f"{case.name}: level elevations {actual!r} != {expected_levels!r}"
            )

    route_expected = expectations.pop("route_expected", None)
    if route_expected is not None and bool(model.routes) != route_expected:
        raise GoldenFixtureError(
            f"{case.name}: route presence {bool(model.routes)!r} != {route_expected!r}"
        )

    expected_route_ids = expectations.pop("route_ids", None)
    if expected_route_ids is not None:
        actual_route_ids = [route.id for route in model.routes]
        if actual_route_ids != expected_route_ids:
            raise GoldenFixtureError(
                f"{case.name}: route ids {actual_route_ids!r} != {expected_route_ids!r}"
            )

    if expectations:
        raise GoldenFixtureError(
            f"{case.name}: unsupported expectation keys {sorted(expectations)}"
        )


def validate_golden_case(case: GoldenCase) -> str | None:
    """Validate one manifest case, including expected failures for negative fixtures."""

    payload = case.path.read_text(encoding="utf-8")
    if not case.valid:
        try:
            BuildingModel.from_json(payload)
        except ContractError as exc:
            if case.error_contains not in str(exc):
                raise GoldenFixtureError(
                    f"{case.name}: expected error containing {case.error_contains!r}, got {exc!r}"
                ) from exc
            return None
        raise GoldenFixtureError(f"{case.name}: intentionally invalid fixture was accepted")

    model = BuildingModel.from_json(payload)
    digest = validate_lane_model(model)
    validate_public_fixture_provenance(model)
    _validate_expectations(case, model)
    if digest != case.sha256:
        raise GoldenFixtureError(
            f"{case.name}: digest changed: expected {case.sha256}, got {digest}"
        )
    return digest


def validate_golden_suite(root: str | Path | None = None) -> dict[str, str | None]:
    """Validate all checked-in fixtures and return their deterministic fingerprints."""

    return {case.name: validate_golden_case(case) for case in load_golden_cases(root)}
