"""Deterministic, read-only bridge from canonical review facts to a GLB artifact.

The bridge binds caller-provided artifact references and content identities. It
never fetches, uploads, signs, or guesses at an artifact location.
"""
from __future__ import annotations

from oabm.model import reject_physical_routes
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Mapping, get_args, get_origin, get_type_hints

from oabm.assembly_compat import require_no_populated_building_assemblies
from oabm.model import BuildingModel, SUPPORT_SCHEMA_VERSIONS, FITTING_SCHEMA_VERSIONS
from oabm.model.common import Entity
from oabm.qa.confidence_review import (
    ConfidenceReviewManifest,
    EvidenceReference,
    ReviewConfig,
    ReviewItem,
    build_confidence_review,
)

_BUNDLE_KIND = "oabm.review-bundle"
_BUNDLE_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GLB_MAGIC = 0x46546C67
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reference(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty caller-provided path or id")
    return value


def _strict_json_loads(payload: str | bytes, label: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc


def _entity_collections(model: BuildingModel) -> dict[str, tuple[Entity, ...]]:
    """Discover all canonical Entity collections using the model's typed fields."""
    if not isinstance(model, BuildingModel) or not is_dataclass(model):
        raise TypeError("model must be a BuildingModel")
    hints = get_type_hints(type(model))
    collections: dict[str, tuple[Entity, ...]] = {}
    for field_info in fields(model):
        hint = hints[field_info.name]
        args = get_args(hint)
        if (
            get_origin(hint) is tuple
            and args
            and (field_info.name == "routes" or (
                isinstance(args[0], type) and issubclass(args[0], Entity)
            ))
        ):
            values = getattr(model, field_info.name)
            if not isinstance(values, tuple) or any(not isinstance(item, Entity) for item in values):
                raise ValueError(f"canonical collection {field_info.name!r} is malformed")
            collections[field_info.name] = values
    if not collections:
        raise ValueError("model exposes no canonical entity collections")
    return collections


def canonical_model_sha256(model: BuildingModel) -> str:
    """Hash the canonical model's deterministic, compact JSON serialization."""
    return _sha256(model.to_json(indent=None).encode("utf-8"))


_GLB_OPTION_DEFAULTS: dict[str, Any] = {
    "physical_routes": False,
    "include_obstacles": False,
    "reference_planes": False,
    "reference_floor_alpha": 0.25,
    "reference_ceiling_alpha": 0.08,
    "reference_margin_m": 0.5,
    "dimmed_ids": (),
    "dimmed_alpha": 0.3,
    "dimmed_color": (0.62, 0.62, 0.62),
    "emphasized_ids": (),
    "groups": None,
    "hidden_groups": (),
    "glazed_outline": False,
    "min_display_wall_thickness_m": None,
}


def _string_sequence(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{label} must be a finite array of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must contain non-empty strings")
    return tuple(sorted(set(value)))


def _normalize_glb_options(
    value: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize exporter settings to deterministic call args and JSON facts."""
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("glb_options must be an object of to_glb keyword options")
    unknown = set(value) - set(_GLB_OPTION_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown GLB export options: {sorted(unknown)}")
    options = dict(_GLB_OPTION_DEFAULTS)
    options.update(value)

    for name in ("reference_planes", "glazed_outline", "physical_routes", "include_obstacles"):
        if not isinstance(options[name], bool):
            raise ValueError(f"GLB option {name} must be a boolean")
    for name in (
        "reference_floor_alpha", "reference_ceiling_alpha", "reference_margin_m",
        "dimmed_alpha",
    ):
        item = options[name]
        if (
            not isinstance(item, (int, float)) or isinstance(item, bool)
            or not math.isfinite(item)
        ):
            raise ValueError(f"GLB option {name} must be a finite number")
        if (
            name in {"reference_floor_alpha", "reference_ceiling_alpha", "dimmed_alpha"}
            and not 0.0 <= item <= 1.0
        ):
            raise ValueError(f"GLB option {name} must be a normalized value in [0, 1]")
    thickness = options["min_display_wall_thickness_m"]
    if thickness is not None and (
        not isinstance(thickness, (int, float)) or isinstance(thickness, bool)
        or not math.isfinite(thickness) or thickness <= 0.0
    ):
        raise ValueError("min_display_wall_thickness_m must be positive and finite or None")

    options["dimmed_ids"] = _string_sequence(options["dimmed_ids"], "dimmed_ids")
    options["emphasized_ids"] = _string_sequence(options["emphasized_ids"], "emphasized_ids")
    options["hidden_groups"] = _string_sequence(options["hidden_groups"], "hidden_groups")
    color = options["dimmed_color"]
    if (
        not isinstance(color, (list, tuple)) or len(color) != 3
        or any(
            not isinstance(item, (int, float)) or isinstance(item, bool)
            or not math.isfinite(item)
            for item in color
        )
    ):
        raise ValueError("dimmed_color must contain three finite numeric channels")
    if any(not 0.0 <= item <= 1.0 for item in color):
        raise ValueError("dimmed_color channels must be normalized values in [0, 1]")
    options["dimmed_color"] = tuple(color)

    groups = options["groups"]
    if groups is not None:
        if not isinstance(groups, Mapping):
            raise ValueError("groups must be an object mapping names to id arrays or None")
        normalized_groups: dict[str, tuple[str, ...]] = {}
        for name, ids in groups.items():
            if not isinstance(name, str) or not name:
                raise ValueError("group names must be non-empty strings")
            normalized_groups[name] = _string_sequence(ids, f"groups[{name!r}]")
        options["groups"] = dict(sorted(normalized_groups.items()))
        if not options["groups"]:
            options["groups"] = None

    portable = {
        name: (
            {group: list(ids) for group, ids in options[name].items()}
            if name == "groups" and options[name] is not None
            else list(options[name])
            if name in {"dimmed_ids", "emphasized_ids", "hidden_groups", "dimmed_color"}
            else options[name]
        )
        for name in sorted(options)
        if name not in {"physical_routes", "include_obstacles"} or options[name]
    }
    _canonical_json_bytes(portable)
    return options, portable


def _decode_confidence_manifest(
    value: ConfidenceReviewManifest | Mapping[str, Any] | str | bytes,
) -> ConfidenceReviewManifest:
    if isinstance(value, ConfidenceReviewManifest):
        return value
    if isinstance(value, (str, bytes)):
        value = _strict_json_loads(value, "confidence review manifest")
    if not isinstance(value, Mapping):
        raise TypeError("confidence_manifest must be a manifest, JSON object, string, or bytes")
    return ConfidenceReviewManifest.from_dict(value)


def _verify_confidence_manifest(
    model: BuildingModel, manifest: ConfidenceReviewManifest, *, physical_routes: bool = False,
) -> str:
    if manifest.model_id != model.model_id:
        raise ValueError("confidence review model_id does not match canonical model")
    crop_bindings: dict[str, dict[str, Any]] = {}
    for item in manifest.items:
        for crop in item.crops:
            previous = crop_bindings.setdefault(crop["ref_id"], crop)
            if previous != crop:
                raise ValueError("confidence review has conflicting evidence references")
    evidence = tuple(
        EvidenceReference(**crop_bindings[ref_id])
        for ref_id in sorted(crop_bindings)
    )
    expected = build_confidence_review(
        model, ReviewConfig(manifest.confidence_threshold), evidence=evidence,
        physical_routes=physical_routes,
    )
    canonical = manifest.to_json(indent=None)
    if expected.to_json(indent=None) != canonical:
        raise ValueError("confidence review manifest is stale or mismatched with canonical model")
    return _sha256(canonical.encode("utf-8"))


def _parse_glb_document(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or len(payload) < 20:
        raise ValueError("glb_bytes must contain a complete GLB 2.0 artifact")
    magic, version, declared_length = struct.unpack_from("<III", payload, 0)
    if magic != _GLB_MAGIC or version != _GLB_VERSION or declared_length != len(payload):
        raise ValueError("GLB header is invalid or does not match supplied bytes")
    offset = 12
    document: Mapping[str, Any] | None = None
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise ValueError("GLB contains a truncated chunk header")
        chunk_length, chunk_type = struct.unpack_from("<II", payload, offset)
        offset += 8
        end = offset + chunk_length
        if end > len(payload) or chunk_length % 4:
            raise ValueError("GLB contains a truncated or unaligned chunk")
        chunk = payload[offset:end]
        offset = end
        if document is None:
            if chunk_type != _CHUNK_JSON:
                raise ValueError("GLB must begin with a JSON chunk")
            value = _strict_json_loads(chunk.decode("utf-8").rstrip(" \t\r\n\0"), "GLB JSON chunk")
            if not isinstance(value, Mapping):
                raise ValueError("GLB JSON chunk must be an object")
            document = value
        elif chunk_type == _CHUNK_JSON:
            raise ValueError("GLB contains more than one JSON chunk")
    if offset != len(payload) or document is None:
        raise ValueError("GLB chunks do not exactly cover supplied bytes")
    asset = document.get("asset")
    if not isinstance(asset, Mapping) or asset.get("version") != "2.0":
        raise ValueError("GLB asset must be an object declaring glTF version 2.0")
    return document


def _canonical_entity_index(
    model: BuildingModel,
) -> tuple[dict[str, tuple[str, Entity]], dict[str, dict[str, Any]]]:
    collections = _entity_collections(model)
    model_document = model.to_dict()
    by_id: dict[str, tuple[str, Entity]] = {}
    serialized: dict[str, dict[str, Any]] = {}
    for collection_name in sorted(collections):
        rows = model_document.get(collection_name, [])
        if not isinstance(rows, list):
            raise ValueError(f"canonical serialized collection {collection_name!r} is malformed")
        if collection_name not in model_document and collections[collection_name]:
            raise ValueError(f"canonical collection {collection_name!r} is absent from serialized model")
        serialized_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"canonical serialized collection {collection_name!r} has a malformed entity")
            identity = row.get("id")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError(f"canonical entity in {collection_name!r} has a missing id")
            if identity in by_id:
                raise ValueError(f"duplicate canonical entity id {identity!r}")
            entity = next((item for item in collections[collection_name] if item.id == identity), None)
            if entity is None:
                raise ValueError(f"canonical entity {identity!r} is absent from its typed collection")
            by_id[identity] = (collection_name, entity)
            serialized_rows[identity] = dict(row)
        if len(serialized_rows) != len(collections[collection_name]):
            raise ValueError(f"canonical collection {collection_name!r} has missing or duplicate ids")
        serialized.update(serialized_rows)
    return by_id, serialized


def _node_index(
    document: Mapping[str, Any],
    entities: Mapping[str, tuple[str, Entity]],
) -> dict[str, list[int]]:
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("GLB nodes must be an array")
    by_id: dict[str, list[int]] = {}
    node_is_wire: dict[str, bool] = {}
    route_ids = {
        identity for identity, (collection, _) in entities.items()
        if collection == "routes"
    }
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise ValueError(f"GLB node {index} must be an object")
        extras = node.get("extras", {})
        if extras is None:
            extras = {}
        if not isinstance(extras, Mapping):
            raise ValueError(f"GLB node {index} extras must be an object")
        if "canonical_type" in extras and "canonical_id" not in extras:
            raise ValueError(f"GLB entity node {index} is missing extras.canonical_id")
        if "canonical_id" not in extras:
            name = node.get("name")
            if isinstance(name, str) and name in entities:
                raise ValueError(f"GLB entity node {index} is missing extras.canonical_id")
            continue
        identity = extras["canonical_id"]
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(f"GLB node {index} has a missing or invalid canonical_id")
        if identity not in entities:
            raise ValueError(f"GLB node {index} references unknown canonical_id {identity!r}")
        name = node.get("name", "")
        is_wire = (
            isinstance(name, str)
            and name.startswith("conductor:")
            and "#route:" in name
            and isinstance(extras.get("route_id"), str)
        )
        collection, entity = entities[identity]
        if collection == "conductors":
            if not is_wire:
                raise ValueError(f"GLB conductor node {index} is missing its route-bound wire identity")
            route_id = extras["route_id"]
            if route_id not in route_ids:
                raise ValueError(f"GLB wire node {index} references an unknown route_id")
            if route_id not in entity.route_ids:
                raise ValueError(
                    f"GLB wire node {index} maps conductor {identity!r} to a route it does not carry"
                )
            wire_identity = (
                identity if identity.startswith("conductor:") else f"conductor:{identity}"
            )
            prefix = f"{wire_identity}#route:{route_id.removeprefix('route:')}#"
            if not name.startswith(prefix) or not name[len(prefix):].isdigit():
                raise ValueError(f"GLB wire node {index} name does not match its canonical conductor/route")
        elif is_wire:
            raise ValueError(f"GLB wire node {index} canonical_id is not a conductor")
        previous = by_id.get(identity, [])
        if previous and (not is_wire or not all(node_is_wire.get(identity, False) for _ in previous)):
            raise ValueError(f"duplicate GLB canonical_id {identity!r}")
        by_id.setdefault(identity, []).append(index)
        node_is_wire[identity] = is_wire
    return by_id


def _verify_expected_rendered_nodes(
    model: BuildingModel, node_indices: Mapping[str, list[int]], *, include_obstacles: bool = False,
) -> None:
    """Require every exporter-supported canonical entity and wire instance."""
    expected: dict[str, int] = {}
    for collection in (
        model.walls, model.slabs, model.spaces, model.electrical_devices,
        model.electrical_equipment, model.electrical_boxes, model.support_components,
        model.structural_members, model.routes, model.support_interfaces,
        model.support_assemblies,
    ):
        for entity in collection:
            expected[entity.id] = 1
    if include_obstacles:
        for obstacle in model.obstacles:
            expected[obstacle.id] = 1
    if model.schema_version in FITTING_SCHEMA_VERSIONS:
        for fitting in model.route_fittings:
            expected[fitting.id] = 1
    for conductor in model.conductors:
        wire_count = conductor.count * len(dict.fromkeys(conductor.route_ids))
        if wire_count:
            expected[conductor.id] = wire_count
    observed = {identity: len(indices) for identity, indices in node_indices.items()}
    if observed != expected:
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        wrong_counts = {
            identity: {"expected": expected[identity], "actual": observed[identity]}
            for identity in sorted(set(expected) & set(observed))
            if expected[identity] != observed[identity]
        }
        raise ValueError(
            "GLB canonical node coverage does not match exporter-supported model entities "
            f"(missing={missing}, unexpected={unexpected}, wrong_counts={wrong_counts})"
        )


def _source_reference_rows(
    source_references: Mapping[tuple[str, str], str],
    used_sources: set[tuple[str, str]],
) -> list[dict[str, str]]:
    if not isinstance(source_references, Mapping):
        raise TypeError("source_references must map (source_kind, source_id) pairs to caller refs")
    normalized: dict[tuple[str, str], str] = {}
    for key, value in source_references.items():
        if (
            not isinstance(key, tuple) or len(key) != 2
            or any(not isinstance(part, str) or not part for part in key)
        ):
            raise ValueError("source reference keys must be (source_kind, source_id) string pairs")
        normalized[key] = _reference(value, f"source reference {key!r}")
    missing = used_sources - set(normalized)
    extra = set(normalized) - used_sources
    if missing:
        raise ValueError(f"unresolved canonical source references: {sorted(missing)}")
    if extra:
        raise ValueError(f"source reference mapping contains unused entries: {sorted(extra)}")
    return [
        {"source_kind": kind, "source_id": identity, "reference": normalized[(kind, identity)]}
        for kind, identity in sorted(normalized)
    ]


def build_review_bundle(
    model: BuildingModel,
    *,
    glb_bytes: bytes,
    model_reference: str,
    glb_reference: str,
    confidence_manifest: ConfidenceReviewManifest | Mapping[str, Any] | str | bytes,
    confidence_reference: str,
    source_reference_mapping_reference: str,
    source_references: Mapping[tuple[str, str], str],
    glb_options: Mapping[str, Any] | None = None,
) -> ReviewBundle:
    """Bind canonical review facts to a verified GLB and caller artifact refs.

    Every content identity is SHA-256 over canonical model JSON, raw GLB
    bytes, canonical confidence-manifest JSON, or canonical source mapping
    JSON. The caller provides every artifact reference; this function does
    no file access, network access, upload, signing, or model mutation.
    """
    if not isinstance(model, BuildingModel):
        raise TypeError("model must be a BuildingModel")
    # Preserve default fail-fast refusal even when other required inputs are absent.
    if not isinstance(glb_options, Mapping) or glb_options.get("physical_routes") is not True:
        reject_physical_routes(model, "build_review_bundle")
    export_args, export_options = _normalize_glb_options(glb_options)
    require_no_populated_building_assemblies(model, "build_review_bundle")
    refs = {
        "model": _reference(model_reference, "model_reference"),
        "glb": _reference(glb_reference, "glb_reference"),
        "confidence": _reference(confidence_reference, "confidence_reference"),
        "source_mapping": _reference(
            source_reference_mapping_reference, "source_reference_mapping_reference",
        ),
    }
    manifest = _decode_confidence_manifest(confidence_manifest)
    if model.schema_version not in SUPPORT_SCHEMA_VERSIONS:
        raise ValueError(
            "review bundles require schema 1.3.0 or newer for canonical GLB node identities"
        )
    model_sha = canonical_model_sha256(model)
    entities, serialized_entities = _canonical_entity_index(model)
    manifest_sha = _verify_confidence_manifest(
        model, manifest, physical_routes=export_args["physical_routes"],
    )
    from oabm.exports.gltf import build_glb_bytes

    trusted_glb_bytes = build_glb_bytes(model, **export_args)
    glb_document = _parse_glb_document(glb_bytes)
    asset_extras = glb_document["asset"].get("extras", {})
    if not isinstance(asset_extras, Mapping):
        raise ValueError("GLB asset extras must be an object")
    if asset_extras.get("canonical_model_id") != model.model_id:
        raise ValueError("GLB canonical_model_id does not match canonical model")
    if asset_extras.get("canonical_model_sha256") != model_sha:
        raise ValueError("GLB canonical_model_sha256 is stale or mismatched")

    node_indices = _node_index(glb_document, entities)
    _verify_expected_rendered_nodes(
        model, node_indices, include_obstacles=export_args["include_obstacles"],
    )
    if glb_bytes != trusted_glb_bytes:
        raise ValueError("GLB bytes do not match the trusted canonical export and supplied display options")
    used_sources: set[tuple[str, str]] = set()
    for item in manifest.items:
        for identity in item.entity_ids:
            if identity not in entities:
                raise ValueError(f"confidence review references unknown canonical entity {identity!r}")
            record = serialized_entities[identity]
            provenance = record.get("provenance", [])
            if not isinstance(provenance, list):
                raise ValueError(f"canonical entity {identity!r} has malformed provenance")
            for source in provenance:
                if not isinstance(source, Mapping):
                    raise ValueError(f"canonical entity {identity!r} has malformed provenance")
                kind, source_id = source.get("source_kind"), source.get("source_id")
                if not isinstance(kind, str) or not kind or not isinstance(source_id, str) or not source_id:
                    raise ValueError(f"canonical entity {identity!r} has unresolved provenance identity")
                used_sources.add((kind, source_id))
    source_rows = _source_reference_rows(source_references, used_sources)
    source_lookup = {
        (row["source_kind"], row["source_id"]): row["reference"]
        for row in source_rows
    }
    items: list[dict[str, Any]] = []
    for item in manifest.items:
        joined_entities: list[dict[str, Any]] = []
        for identity in item.entity_ids:
            collection, entity = entities[identity]
            canonical = serialized_entities[identity]
            provenance: list[dict[str, Any]] = []
            for source in canonical.get("provenance", []):
                source_kind, source_id = source["source_kind"], source["source_id"]
                provenance.append({
                    **dict(source),
                    "source_reference": source_lookup[(source_kind, source_id)],
                })
            joined_entities.append({
                "canonical_id": identity,
                "collection": collection,
                "confidence": entity.confidence,
                "provenance": provenance,
                "rendered_node_indices": list(node_indices.get(identity, [])),
            })
        items.append({
            "item_id": item.item_id,
            "review": item.to_dict(),
            "entities": joined_entities,
        })
    source_map_sha = _sha256(_canonical_json_bytes(source_rows))
    document = {
        "kind": _BUNDLE_KIND,
        "version": _BUNDLE_VERSION,
        "artifacts": {
            "canonical_model": {
                "reference": refs["model"],
                "model_id": model.model_id,
                "sha256": model_sha,
            },
            "glb": {
                "reference": refs["glb"],
                "sha256": _sha256(glb_bytes),
                "export_options": export_options,
            },
            "confidence_review": {
                "reference": refs["confidence"],
                "sha256": manifest_sha,
            },
            "source_reference_mapping": {
                "reference": refs["source_mapping"],
                "sha256": source_map_sha,
                "items": source_rows,
            },
        },
        "items": items,
    }
    return ReviewBundle(document)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _detach(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_detach(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """Portable immutable-by-copy versioned review bundle document."""

    _document: Mapping[str, Any]

    def __post_init__(self) -> None:
        detached = _detach(_freeze(self._document))
        _validate_bundle_document(detached)
        object.__setattr__(self, "_document", _freeze(detached))

    def to_dict(self) -> dict[str, Any]:
        return _detach(self._document)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ReviewBundle:
        if not isinstance(document, Mapping):
            raise ValueError("review bundle must be a JSON object")
        return cls(document)


def _validate_bundle_document(document: Any) -> None:
    if not isinstance(document, Mapping):
        raise ValueError("review bundle must be a JSON object")
    required = {"kind", "version", "artifacts", "items"}
    if set(document) != required:
        raise ValueError("review bundle has unknown or missing top-level keys")
    if document["kind"] != _BUNDLE_KIND or document["version"] != _BUNDLE_VERSION:
        raise ValueError("unsupported review bundle kind or version")
    artifacts = document["artifacts"]
    artifact_keys = {
        "canonical_model", "glb", "confidence_review", "source_reference_mapping",
    }
    if not isinstance(artifacts, Mapping) or set(artifacts) != artifact_keys:
        raise ValueError("review bundle artifacts are incomplete or contain unknown keys")
    expected_artifact_keys = {
        "canonical_model": {"reference", "model_id", "sha256"},
        "glb": {"reference", "sha256", "export_options"},
        "confidence_review": {"reference", "sha256"},
        "source_reference_mapping": {"reference", "sha256", "items"},
    }
    for name, keys in expected_artifact_keys.items():
        artifact = artifacts[name]
        if not isinstance(artifact, Mapping) or set(artifact) != keys:
            raise ValueError(f"review bundle artifact {name!r} has an invalid shape")
        _reference(artifact.get("reference"), f"{name} reference")
        if name == "canonical_model":
            _reference(artifact.get("model_id"), "canonical model_id")
        if not isinstance(artifact.get("sha256"), str) or not _SHA256_RE.fullmatch(artifact["sha256"]):
            raise ValueError(f"review bundle artifact {name!r} has an invalid SHA-256 identity")
    glb_export_options = artifacts["glb"]["export_options"]
    if not isinstance(glb_export_options, Mapping):
        raise ValueError("GLB export options must be a JSON object")
    _, normalized_export_options = _normalize_glb_options(glb_export_options)
    if dict(glb_export_options) != normalized_export_options:
        raise ValueError("GLB export options must be complete and normalized")
    source_items = artifacts["source_reference_mapping"]["items"]
    if not isinstance(source_items, (list, tuple)):
        raise ValueError("source reference mapping items must be an array")
    previous: tuple[str, str] | None = None
    for entry in source_items:
        if not isinstance(entry, Mapping) or set(entry) != {"source_kind", "source_id", "reference"}:
            raise ValueError("source reference mapping entry has an invalid shape")
        kind, identity = entry["source_kind"], entry["source_id"]
        if not isinstance(kind, str) or not kind or not isinstance(identity, str) or not identity:
            raise ValueError("source reference mapping identity is invalid")
        _reference(entry["reference"], "source reference")
        key = (kind, identity)
        if previous is not None and key <= previous:
            raise ValueError("source reference mapping entries must be unique and sorted")
        previous = key
    expected_mapping_sha = _sha256(_canonical_json_bytes(_detach(source_items)))
    if artifacts["source_reference_mapping"]["sha256"] != expected_mapping_sha:
        raise ValueError("source reference mapping SHA-256 does not match its entries")
    source_lookup = {
        (entry["source_kind"], entry["source_id"]): entry["reference"]
        for entry in source_items
    }
    items = document["items"]
    if not isinstance(items, (list, tuple)):
        raise ValueError("review bundle items must be an array")
    item_ids: set[str] = set()
    used_sources: set[tuple[str, str]] = set()
    for entry in items:
        if not isinstance(entry, Mapping) or set(entry) != {"item_id", "review", "entities"}:
            raise ValueError("review bundle item has an invalid shape")
        identity = entry["item_id"]
        if not isinstance(identity, str) or not identity or identity in item_ids:
            raise ValueError("review bundle item ids must be unique non-empty strings")
        item_ids.add(identity)
        review = ReviewItem.from_dict(entry["review"])
        if review.item_id != identity:
            raise ValueError("review bundle item must retain its confidence review record")
        if not isinstance(entry["entities"], (list, tuple)):
            raise ValueError("review bundle entities must be an array")
        entity_ids: set[str] = set()
        ordered_entity_ids: list[str] = []
        for entity in entry["entities"]:
            if not isinstance(entity, Mapping) or set(entity) != {
                "canonical_id", "collection", "confidence", "provenance", "rendered_node_indices",
            }:
                raise ValueError("joined canonical entity has an invalid shape")
            canonical_id = entity["canonical_id"]
            if not isinstance(canonical_id, str) or not canonical_id or canonical_id in entity_ids:
                raise ValueError("joined canonical entity ids must be unique per review item")
            entity_ids.add(canonical_id)
            ordered_entity_ids.append(canonical_id)
            if not isinstance(entity["collection"], str) or not entity["collection"]:
                raise ValueError("joined canonical entity collection is invalid")
            confidence = entity["confidence"]
            if (
                not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0
            ):
                raise ValueError("joined canonical entity confidence is invalid")
            if not isinstance(entity["provenance"], (list, tuple)):
                raise ValueError("joined canonical entity provenance must be an array")
            for provenance in entity["provenance"]:
                if not isinstance(provenance, Mapping):
                    raise ValueError("joined canonical provenance must be an object")
                kind, source_id = provenance.get("source_kind"), provenance.get("source_id")
                if (
                    not isinstance(kind, str) or not kind
                    or not isinstance(source_id, str) or not source_id
                    or source_lookup.get((kind, source_id)) != provenance.get("source_reference")
                ):
                    raise ValueError("joined canonical provenance has an unresolved source reference")
                used_sources.add((kind, source_id))
            node_indices = entity["rendered_node_indices"]
            if not isinstance(node_indices, (list, tuple)) or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in node_indices
            ):
                raise ValueError("rendered GLB node indices must be non-negative integers")
            if list(node_indices) != sorted(set(node_indices)):
                raise ValueError("rendered GLB node indices must be unique and sorted")
        if ordered_entity_ids != list(review.entity_ids):
            raise ValueError("joined canonical entities do not match confidence review entity_ids")
    if used_sources != set(source_lookup):
        raise ValueError("source reference mapping contains unresolved or unused entries")
