from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from oabm.model import BuildingModel, ContractError, SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "contracts" / "oabm-model-v1.schema.json"
DEFAULT_GOLDEN_ROOT = REPO_ROOT / "fixtures" / "golden" / "v1"
MANIFEST_NAME = "manifest.json"

ENTITY_COLLECTIONS = (
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


@dataclass(frozen=True, slots=True)
class GoldenCase:
    name: str
    path: Path
    description: str
    tags: tuple[str, ...]
    expected_valid: bool
    expected_error: str | None
    expected_counts: Mapping[str, int]
    required_ids: tuple[str, ...]
    routing_outcome: str | None
    require_provenance: bool


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return document


def load_manifest(root: Path = DEFAULT_GOLDEN_ROOT) -> dict[str, Any]:
    manifest = _read_json(root / MANIFEST_NAME)
    if manifest.get("harness_version") != 1:
        raise AssertionError("golden fixture manifest must declare harness_version 1")
    if manifest.get("canonical_schema_version") != SCHEMA_VERSION:
        raise AssertionError(
            "golden fixture manifest schema version must match the canonical model"
        )
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AssertionError("golden fixture manifest must contain cases")
    return manifest


def discover_cases(
    root: Path = DEFAULT_GOLDEN_ROOT,
    *,
    tags: Iterable[str] = (),
    include_invalid: bool = True,
) -> tuple[GoldenCase, ...]:
    wanted_tags = set(tags)
    manifest = load_manifest(root)
    discovered: list[GoldenCase] = []
    names: set[str] = set()

    for raw in manifest["cases"]:
        if not isinstance(raw, dict):
            raise AssertionError("golden fixture manifest case entries must be objects")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise AssertionError("golden fixture case names must be non-empty strings")
        if name in names:
            raise AssertionError(f"duplicate golden fixture case name: {name}")
        names.add(name)

        case_tags = tuple(raw.get("tags", ()))
        expected_valid = bool(raw.get("expected_valid", True))
        if not include_invalid and not expected_valid:
            continue
        if wanted_tags and not wanted_tags.issubset(case_tags):
            continue

        relative_path = raw.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise AssertionError(f"{name}: manifest path is required")
        path = root / relative_path
        if not path.is_file():
            raise AssertionError(f"{name}: fixture file does not exist: {path}")

        expected_counts = raw.get("expected_counts", {})
        if not isinstance(expected_counts, dict):
            raise AssertionError(f"{name}: expected_counts must be an object")
        unknown_collections = set(expected_counts) - set(ENTITY_COLLECTIONS)
        if unknown_collections:
            raise AssertionError(
                f"{name}: unknown expected-count collections: "
                + ", ".join(sorted(unknown_collections))
            )

        discovered.append(
            GoldenCase(
                name=name,
                path=path,
                description=str(raw.get("description", "")),
                tags=case_tags,
                expected_valid=expected_valid,
                expected_error=raw.get("expected_error"),
                expected_counts={
                    key: int(value) for key, value in expected_counts.items()
                },
                required_ids=tuple(raw.get("required_ids", ())),
                routing_outcome=raw.get("routing_outcome"),
                require_provenance=bool(raw.get("require_provenance", True)),
            )
        )

    return tuple(sorted(discovered, key=lambda case: case.name))


def get_case(name: str, root: Path = DEFAULT_GOLDEN_ROOT) -> GoldenCase:
    for case in discover_cases(root):
        if case.name == name:
            return case
    raise KeyError(f"unknown golden fixture case {name!r}")


def load_case_document(case: GoldenCase | str, root: Path = DEFAULT_GOLDEN_ROOT) -> dict[str, Any]:
    resolved = get_case(case, root) if isinstance(case, str) else case
    return _read_json(resolved.path)


def validate_schema(
    document: Mapping[str, Any],
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> None:
    from jsonschema import Draft202012Validator

    schema = _read_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            details.append(f"{location}: {error.message}")
        raise AssertionError("Schema validation failed:\n" + "\n".join(details))


def entity_ids(model: BuildingModel) -> tuple[str, ...]:
    ids: list[str] = []
    for collection in ENTITY_COLLECTIONS:
        ids.extend(item.id for item in getattr(model, collection))
    return tuple(sorted(ids))


def canonical_json(value: BuildingModel | Mapping[str, Any]) -> str:
    model = value if isinstance(value, BuildingModel) else BuildingModel.from_dict(value)
    return model.to_json(indent=None)


def canonical_fingerprint(value: BuildingModel | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def assert_traceable(model: BuildingModel) -> None:
    if not model.provenance:
        raise AssertionError(f"{model.model_id} must carry provenance")
    for record in model.provenance:
        if record.source_kind != "synthetic":
            raise AssertionError(
                f"{model.model_id} golden provenance must be synthetic, got {record.source_kind!r}"
            )

    for collection in ENTITY_COLLECTIONS:
        for item in getattr(model, collection):
            if not 0.0 <= item.confidence <= 1.0:
                raise AssertionError(f"{item.id} confidence must be between 0 and 1")
            if not item.provenance:
                raise AssertionError(f"{item.id} must carry provenance")
            if any(record.source_kind != "synthetic" for record in item.provenance):
                raise AssertionError(f"{item.id} golden provenance must be synthetic")


def assert_case_invariants(case: GoldenCase, model: BuildingModel) -> None:
    for collection, expected_count in case.expected_counts.items():
        actual = len(getattr(model, collection))
        if actual != expected_count:
            raise AssertionError(
                f"{case.name}: expected {expected_count} {collection}, got {actual}"
            )

    actual_ids = set(entity_ids(model))
    missing = set(case.required_ids) - actual_ids
    if missing:
        raise AssertionError(
            f"{case.name}: missing required stable IDs: {', '.join(sorted(missing))}"
        )

    if case.routing_outcome is not None:
        actual = model.attributes.get("routing_outcome")
        if actual != case.routing_outcome:
            raise AssertionError(
                f"{case.name}: expected routing_outcome {case.routing_outcome!r}, got {actual!r}"
            )

    if case.require_provenance:
        assert_traceable(model)


def validate_case(case: GoldenCase | str, root: Path = DEFAULT_GOLDEN_ROOT) -> BuildingModel | None:
    resolved = get_case(case, root) if isinstance(case, str) else case
    document = load_case_document(resolved, root)
    validate_schema(document)

    if not resolved.expected_valid:
        try:
            BuildingModel.from_dict(document)
        except ContractError as exc:
            if resolved.expected_error and resolved.expected_error not in str(exc):
                raise AssertionError(
                    f"{resolved.name}: expected error containing "
                    f"{resolved.expected_error!r}, got {str(exc)!r}"
                ) from exc
            return None
        raise AssertionError(f"{resolved.name}: expected canonical validation to fail")

    model = BuildingModel.from_dict(document)
    assert_case_invariants(resolved, model)

    canonical = model.to_dict()
    if canonical != document:
        raise AssertionError(
            f"{resolved.name}: checked-in fixture is not already strict canonical serialization"
        )

    round_trip = BuildingModel.from_json(model.to_json())
    if round_trip.to_dict() != canonical:
        raise AssertionError(f"{resolved.name}: canonical JSON round-trip changed the model")
    if entity_ids(round_trip) != entity_ids(model):
        raise AssertionError(f"{resolved.name}: stable entity IDs changed on round-trip")

    validate_schema(round_trip.to_dict())
    return model


def assert_identity_preserved(
    before: BuildingModel,
    after: BuildingModel,
    *,
    collections: Iterable[str] = ENTITY_COLLECTIONS,
) -> None:
    for collection in collections:
        before_ids = {item.id for item in getattr(before, collection)}
        after_ids = {item.id for item in getattr(after, collection)}
        missing = before_ids - after_ids
        if missing:
            raise AssertionError(
                f"{collection} lost stable IDs: {', '.join(sorted(missing))}"
            )


def assert_deterministic(
    factory: Callable[[], BuildingModel | Mapping[str, Any]],
    *,
    repeats: int = 3,
) -> BuildingModel:
    if repeats < 2:
        raise ValueError("repeats must be >= 2")

    models: list[BuildingModel] = []
    for _ in range(repeats):
        produced = factory()
        model = produced if isinstance(produced, BuildingModel) else BuildingModel.from_dict(produced)
        models.append(model)

    fingerprints = {canonical_fingerprint(model) for model in models}
    if len(fingerprints) != 1:
        raise AssertionError("repeated output is not canonically deterministic")

    inventories = {entity_ids(model) for model in models}
    if len(inventories) != 1:
        raise AssertionError("repeated output changed stable entity identity")

    return models[0]
