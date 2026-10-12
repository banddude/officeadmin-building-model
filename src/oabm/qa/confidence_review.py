"""Deterministic, read-only confidence review manifest (#273, under #241).

A reviewer looking at a takeoff or a 3D view needs one stable, actionable
list of what the model is unsure about. This module builds that list from
signals the canonical model already carries — entity confidence, provenance
confidence, provenance presence, selection outcomes and the takeoff's own
quantity warnings — without inventing a threshold, relabeling any fact, or
touching the canonical schema.

The manifest is a derived view, exactly like a takeoff or a drawing:

- The review threshold is always explicit caller input. There is no hidden
  default, and no confidence value is ever raised to pass it.
- Entities are collected by reflecting over the model's actual entity
  collections, so collections merged later (support hardware, and any other
  typed physical object) are reviewed without a change here.
- Item identifiers are stable under input permutations, unordered
  provenance, unordered evidence and warning order; identical inputs
  produce byte-identical JSON. Distinct warnings over the same code and
  sources never collide: the message digest is part of the identity.
- Crop/evidence references are typed bindings to an actual provenance
  record of a current entity: source, page and a content fingerprint of
  the bound record. Unknown sources, wrong pages and stale fingerprints
  (the record changed after the reference was made) fail closed. Missing
  evidence stays missing: the manifest never invents a reference, and no
  external service is contacted.
- The manifest is strict and portable: ``from_dict`` rejects unknown keys,
  wrong versions, malformed provenance, out-of-range values and duplicate
  identities, so a manifest written by one process decodes exactly in
  another. Constructor and decoder enforce the same invariants.

This child does not implement importer self-checks, crop extraction, bid
pricing, or a production viewer, and it does not claim that all uncertainty
is detected: the reasons below are the ones the canonical signals can
actually support today.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Iterable, Mapping, Sequence, get_args, get_origin, get_type_hints

from oabm.model import BuildingModel, Entity, Provenance
from oabm.model.common import _SCOPE_PATH_RE
from oabm.quantities import QuantityWarning, extract_quantities

#: Version of the portable config and manifest documents. Bump on any
#: breaking change to their key sets or semantics; strict decoding rejects
#: documents written for a different version.
REVIEW_VERSION = "1.0.0"

#: ``kind`` value carried by both documents, so a config and a manifest can
#: never be confused for each other at a decode boundary.
_CONFIG_KIND = "confidence_review_config"
_MANIFEST_KIND = "confidence_review_manifest"

_REASON_LOW_CONFIDENCE = "low_entity_confidence"
_REASON_LOW_SUPPORT = "low_supporting_provenance_confidence"
_REASON_MISSING_PROVENANCE = "missing_provenance"
_REASON_UNRESOLVED_SELECTION = "unresolved_selection"
_QUANTITY_WARNING_PREFIX = "quantity_warning_"

_REASON_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")
_DERIVATIONS = frozenset({"observed", "user", "inferred"})

#: Provenance fields the canonical contract defines, and the ones a payload
#: entry must carry. Anything else in a decoded entry is rejected.
_PROVENANCE_REQUIRED_KEYS = frozenset({"source_kind", "source_id", "confidence"})
_PROVENANCE_OPTIONAL_KEYS = frozenset({
    "source_element_id", "page", "method", "derivation", "scope_paths",
})


def _validated_threshold(value: Any) -> float:
    """Validate an explicit confidence threshold and return it as a float.

    Booleans are numbers in Python but not in a review policy: ``True``
    would otherwise act as a 1.0 threshold. Non-numbers, non-finite values
    and values outside [0, 1] are rejected.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "confidence_threshold must be a finite number in [0, 1] "
            "(not bool), got {value!r}".format(value=value)
        )
    threshold = float(value)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"confidence_threshold must be a finite number in [0, 1], got {value!r}"
        )
    return threshold


def _validated_confidence(value: Any, label: str) -> float:
    """A confidence value in [0, 1]; booleans and non-numbers fail."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number in [0, 1] (not bool), got {value!r}")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"{label} must be a finite number in [0, 1], got {value!r}")
    return confidence


def _validated_page(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer, got {value!r}")
    return value


def _validated_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or _REASON_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase reason token, got {value!r}")
    return value


_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _validated_fingerprint(value: Any, label: str) -> str:
    """A content fingerprint is a lowercase SHA-256 hex digest."""

    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be a 64-character lowercase hex sha-256 digest, "
            f"got {value!r}"
        )
    return value


def fingerprint_provenance(record: Provenance) -> str:
    """Content fingerprint of one provenance record, for evidence binding.

    The digest covers every identifying field of the record exactly as the
    canonical model holds it. A reference made against a record breaks
    loudly when the record is edited or replaced, which is what makes
    stale evidence detectable without contacting any external store.
    """

    payload = {
        "source_kind": record.source_kind,
        "source_id": record.source_id,
        "source_element_id": record.source_element_id,
        "page": record.page,
        "method": record.method,
        "confidence": record.confidence,
        "derivation": record.derivation,
        "scope_paths": list(record.scope_paths) if record.scope_paths is not None else None,
        # Attributes are part of the record's identity: a source-content or
        # region edit under unchanged ids and page (a new source_sha256, a
        # moved bounding box) must break bindings made against the old
        # record. Normalized deterministically, exactly like serialization.
        "attributes": record.attributes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A typed crop/evidence binding to one current provenance record.

    ``ref_id`` is the caller's stable name for the artifact (a page crop, a
    scan excerpt). ``source_kind``/``source_id``/``page`` name the exact
    provenance record the artifact illustrates, and ``fingerprint`` is that
    record's :func:`fingerprint_provenance` digest at the time the reference
    was made — the builder rejects the reference when the record has since
    changed. Nothing here fetches anything.
    """

    ref_id: str
    entity_id: str
    source_kind: str
    source_id: str
    fingerprint: str
    page: int | None = None

    def __post_init__(self) -> None:
        for label in ("ref_id", "entity_id", "source_kind", "source_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value:
                raise ValueError(f"EvidenceReference.{label} must be a non-empty string")
        object.__setattr__(
            self,
            "fingerprint",
            _validated_fingerprint(self.fingerprint, "EvidenceReference.fingerprint"),
        )
        if self.page is not None:
            object.__setattr__(self, "page", _validated_page(self.page, "EvidenceReference.page"))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ref_id": self.ref_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "fingerprint": self.fingerprint,
        }
        if self.page is not None:
            payload["page"] = self.page
        return payload


def _provenance_payload(records: Iterable[Provenance]) -> tuple[dict[str, Any], ...]:
    """Provenance exactly as the model holds it, in a canonical order.

    Source kind/ID/element/page references, field scopes, confidences and
    derivation classes all travel verbatim — never relabeled — while the
    order is normalized so callers that iterate provenance in any order get
    identical manifest bytes.
    """

    payload: list[dict[str, Any]] = []
    for record in records:
        entry: dict[str, Any] = {
            "source_kind": record.source_kind,
            "source_id": record.source_id,
            "confidence": record.confidence,
        }
        if record.source_element_id is not None:
            entry["source_element_id"] = record.source_element_id
        if record.page is not None:
            entry["page"] = record.page
        if record.method is not None:
            entry["method"] = record.method
        if record.derivation is not None:
            entry["derivation"] = record.derivation
        if record.scope_paths is not None:
            entry["scope_paths"] = list(record.scope_paths)
        payload.append(entry)

    def sort_key(entry: dict[str, Any]) -> tuple:
        return (
            entry["source_kind"],
            entry["source_id"],
            entry.get("source_element_id") or "",
            entry.get("page") if entry.get("page") is not None else -1,
            entry.get("method") or "",
            entry["confidence"],
            entry.get("derivation") or "",
            json.dumps(entry.get("scope_paths"), sort_keys=True) if entry.get("scope_paths") is not None else "",
        )

    payload.sort(key=sort_key)
    return tuple(payload)


def _validated_provenance_entry(value: Any) -> dict[str, Any]:
    """Strict decode of one provenance payload entry."""

    if not isinstance(value, Mapping):
        raise ValueError("provenance entry must be a JSON object")
    unknown = set(value) - _PROVENANCE_REQUIRED_KEYS - _PROVENANCE_OPTIONAL_KEYS
    if unknown:
        raise ValueError(f"unknown provenance keys: {sorted(unknown)}")
    missing = _PROVENANCE_REQUIRED_KEYS - set(value)
    if missing:
        raise ValueError(f"provenance entry is missing keys: {sorted(missing)}")
    entry: dict[str, Any] = {}
    for key in ("source_kind", "source_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"provenance {key} must be a non-empty string")
        entry[key] = value[key]
    entry["confidence"] = _validated_confidence(value["confidence"], "provenance confidence")
    for key in ("source_element_id", "method"):
        if key in value and value[key] is not None:
            if not isinstance(value[key], str) or not value[key]:
                raise ValueError(f"provenance {key} must be a non-empty string")
            entry[key] = value[key]
    if value.get("page") is not None:
        entry["page"] = _validated_page(value["page"], "provenance page")
    if value.get("derivation") is not None:
        derivation = value["derivation"]
        if derivation not in _DERIVATIONS:
            raise ValueError(
                f"provenance derivation must be one of {sorted(_DERIVATIONS)}, "
                f"got {derivation!r}"
            )
        entry["derivation"] = derivation
    if value.get("scope_paths") is not None:
        scopes = value["scope_paths"]
        # The canonical contract rejects empty and malformed scopes at
        # construction, and this decoder is held to the same rule: an empty
        # list is not "unscoped", it is invalid, and every path must be a
        # canonical field path.
        if not isinstance(scopes, list) or not scopes:
            raise ValueError(
                "provenance scope_paths must be a non-empty list of field paths"
            )
        if (
            any(
                not isinstance(path, str) or _SCOPE_PATH_RE.fullmatch(path) is None
                for path in scopes
            )
            or len(set(scopes)) != len(scopes)
        ):
            raise ValueError(
                "provenance scope_paths must be unique canonical field paths"
            )
        entry["scope_paths"] = list(scopes)
    return entry


_CROP_REQUIRED_KEYS = frozenset({"ref_id", "source_kind", "source_id", "fingerprint"})


def _validated_crop(value: Any) -> dict[str, Any]:
    """Strict decode of one bound evidence reference payload."""

    if not isinstance(value, Mapping):
        raise ValueError("crop reference must be a JSON object")
    unknown = set(value) - _CROP_REQUIRED_KEYS - {"page"}
    if unknown:
        raise ValueError(f"unknown crop reference keys: {sorted(unknown)}")
    missing = _CROP_REQUIRED_KEYS - set(value)
    if missing:
        raise ValueError(f"crop reference is missing keys: {sorted(missing)}")
    payload: dict[str, Any] = {}
    for key in sorted(_CROP_REQUIRED_KEYS):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"crop reference {key} must be a non-empty string")
        payload[key] = value[key]
    payload["fingerprint"] = _validated_fingerprint(
        payload["fingerprint"], "crop reference fingerprint"
    )
    if value.get("page") is not None:
        payload["page"] = _validated_page(value["page"], "crop reference page")
    return payload


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    """The explicit, portable inputs of one review.

    ``confidence_threshold`` is the only policy knob: an entity or a
    supporting provenance record at or above it is trusted by this review,
    anything below it qualifies. There is deliberately no default.
    """

    confidence_threshold: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "confidence_threshold", _validated_threshold(self.confidence_threshold)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": _CONFIG_KIND,
            "version": REVIEW_VERSION,
            "confidence_threshold": self.confidence_threshold,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ReviewConfig:
        """Strict decode: unknown keys, wrong versions and bad values fail."""

        if not isinstance(document, Mapping):
            raise ValueError("review config must be a JSON object")
        unknown = set(document) - {"kind", "version", "confidence_threshold"}
        if unknown:
            raise ValueError(f"unknown review config keys: {sorted(unknown)}")
        if document.get("kind") != _CONFIG_KIND:
            raise ValueError(
                f"review config kind must be {_CONFIG_KIND!r}, got {document.get('kind')!r}"
            )
        if document.get("version") != REVIEW_VERSION:
            raise ValueError(
                f"review config version must be {REVIEW_VERSION!r}, "
                f"got {document.get('version')!r}"
            )
        if "confidence_threshold" not in document:
            raise ValueError("review config requires confidence_threshold")
        return cls(confidence_threshold=document["confidence_threshold"])


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One unsure thing, with stable identity and the facts that qualify it.

    ``entity_ids`` names the canonical objects the item is about (one for an
    entity item; every source of a quantity warning for a warning item).
    ``reasons`` lists why it is unsure. ``confidence`` and ``provenance``
    retain the model's actual values — the reviewer, not this module,
    decides what they are worth. ``crops`` carries only caller-supplied,
    bound evidence references for these entities.
    """

    item_id: str
    entity_kind: str
    entity_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    detail: str
    confidence: float | None
    provenance: tuple[dict[str, Any], ...]
    crops: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("review item_id must be a non-empty string")
        if not isinstance(self.entity_kind, str) or not self.entity_kind:
            raise ValueError("review entity_kind must be a non-empty string")
        # Entity items name exactly one entity. Warning items may name many
        # (one per source) or none at all: a model-level warning from the
        # takeoff has no source entities, and inventing one would be a lie.
        if (
            any(not isinstance(value, str) or not value for value in self.entity_ids)
            or len(set(self.entity_ids)) != len(self.entity_ids)
        ):
            raise ValueError("review entity_ids must be unique non-empty strings")
        if (
            not self.reasons
            or any(not isinstance(value, str) for value in self.reasons)
            or len(set(self.reasons)) != len(self.reasons)
        ):
            raise ValueError("review reasons must be non-empty unique strings")
        for reason in self.reasons:
            _validated_token(reason, "review reason")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("review detail must be a non-empty string")
        if self.confidence is not None:
            object.__setattr__(
                self,
                "confidence",
                _validated_confidence(self.confidence, "review item confidence"),
            )
        # Constructor and decoder share these invariants: provenance entries
        # are normalized to their strict shape, crop references carry their
        # binding, and no value is silently altered.
        object.__setattr__(
            self,
            "provenance",
            tuple(_validated_provenance_entry(entry) for entry in self.provenance),
        )
        validated_crops = tuple(_validated_crop(entry) for entry in self.crops)
        ref_ids = [entry["ref_id"] for entry in validated_crops]
        if len(set(ref_ids)) != len(ref_ids):
            raise ValueError("review crops must carry unique ref_id values")
        # Binding is a property of the item, not just of the builder: every
        # crop must name a source that this item's own provenance carries,
        # with the same page, so the portable document stays self-verifying
        # (a crop bound to unrelated provenance is rejected here too, not
        # only at build time).
        provenance_sources = {
            (
                entry.get("source_kind"),
                entry.get("source_id"),
                entry.get("page"),
            )
            for entry in self.provenance
        }
        for entry in validated_crops:
            if (
                entry["source_kind"],
                entry["source_id"],
                entry.get("page"),
            ) not in provenance_sources:
                raise ValueError(
                    f"crop reference {entry['ref_id']!r} binds to source "
                    f"{entry['source_kind']}:{entry['source_id']}, which this "
                    "item's provenance does not carry"
                )
        object.__setattr__(self, "crops", tuple(
            sorted(validated_crops, key=lambda entry: entry["ref_id"])
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "entity_kind": self.entity_kind,
            "entity_ids": list(self.entity_ids),
            "reasons": list(self.reasons),
            "detail": self.detail,
            "confidence": self.confidence,
            "provenance": [dict(entry) for entry in self.provenance],
            "crops": [dict(entry) for entry in self.crops],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ReviewItem:
        """Strict decode of one item; unknown keys and missing keys fail."""

        if not isinstance(document, Mapping):
            raise ValueError("review item must be a JSON object")
        allowed = {
            "item_id", "entity_kind", "entity_ids", "reasons",
            "detail", "confidence", "provenance", "crops",
        }
        unknown = set(document) - allowed
        if unknown:
            raise ValueError(f"unknown review item keys: {sorted(unknown)}")
        missing = allowed - set(document)
        if missing:
            raise ValueError(f"review item is missing keys: {sorted(missing)}")
        for key in ("item_id", "entity_kind", "detail"):
            if not isinstance(document[key], str) or not document[key]:
                raise ValueError(f"review item {key} must be a non-empty string")
        for key in ("entity_ids", "reasons", "crops"):
            if not isinstance(document[key], list):
                raise ValueError(f"review item {key} must be a list")
        if document["provenance"] is not None and not isinstance(document["provenance"], list):
            raise ValueError("review item provenance must be a list")
        return cls(
            item_id=document["item_id"],
            entity_kind=document["entity_kind"],
            entity_ids=tuple(document["entity_ids"]),
            reasons=tuple(document["reasons"]),
            detail=document["detail"],
            confidence=document["confidence"],
            provenance=tuple(document["provenance"]),
            crops=tuple(document["crops"]),
        )


@dataclass(frozen=True, slots=True)
class ConfidenceReviewManifest:
    """The full review output for one model, portable and deterministic."""

    model_id: str
    confidence_threshold: float
    items: tuple[ReviewItem, ...]
    #: Entity collections that were actually scanned, sorted by name.
    collections: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("manifest model_id must be a non-empty string")
        object.__setattr__(
            self,
            "confidence_threshold",
            _validated_threshold(self.confidence_threshold),
        )
        if (
            not self.collections
            or any(not isinstance(name, str) or not name for name in self.collections)
            or len(set(self.collections)) != len(self.collections)
        ):
            raise ValueError("manifest collections must be unique non-empty strings")
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            duplicates = sorted({i for i in item_ids if item_ids.count(i) > 1})
            raise ValueError(f"manifest contains duplicate item ids: {duplicates}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": _MANIFEST_KIND,
            "version": REVIEW_VERSION,
            "model_id": self.model_id,
            "confidence_threshold": self.confidence_threshold,
            "collections": list(self.collections),
            "items": [item.to_dict() for item in self.items],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ConfidenceReviewManifest:
        """Strict decode; unknown keys, wrong versions and bad items fail."""

        if not isinstance(document, Mapping):
            raise ValueError("confidence review manifest must be a JSON object")
        unknown = set(document) - {
            "kind", "version", "model_id", "confidence_threshold", "collections", "items",
        }
        if unknown:
            raise ValueError(f"unknown manifest keys: {sorted(unknown)}")
        if document.get("kind") != _MANIFEST_KIND:
            raise ValueError(
                f"manifest kind must be {_MANIFEST_KIND!r}, got {document.get('kind')!r}"
            )
        if document.get("version") != REVIEW_VERSION:
            raise ValueError(
                f"manifest version must be {REVIEW_VERSION!r}, got {document.get('version')!r}"
            )
        if not isinstance(document.get("model_id"), str) or not document["model_id"]:
            raise ValueError("manifest model_id must be a non-empty string")
        threshold = _validated_threshold(document.get("confidence_threshold"))
        if not isinstance(document.get("collections"), list) or any(
            not isinstance(name, str) for name in document["collections"]
        ):
            raise ValueError("manifest collections must be a list of strings")
        if len(set(document["collections"])) != len(document["collections"]):
            raise ValueError("manifest collections contains duplicate names")
        if not isinstance(document.get("items"), list):
            raise ValueError("manifest items must be a list")
        items = tuple(ReviewItem.from_dict(entry) for entry in document["items"])
        return cls(
            model_id=document["model_id"],
            confidence_threshold=threshold,
            items=items,
            collections=tuple(document["collections"]),
        )


def _entity_collections(model: BuildingModel) -> dict[str, tuple[Entity, ...]]:
    """Every current entity collection, discovered from the model's own type.

    Reflection over the actual dataclass fields keeps this review correct
    when main merges new typed physical objects (support hardware next):
    they are reviewed without a change here, exactly as the quantities lane
    anchors its coverage to the model type. ``provenance`` is a Provenance
    tuple, not an Entity tuple, and is rightly excluded.
    """

    if not is_dataclass(model):
        raise TypeError("model must be a BuildingModel dataclass")
    hints = get_type_hints(type(model))
    result: dict[str, tuple[Entity, ...]] = {}
    for field_info in fields(model):
        hint = hints[field_info.name]
        arguments = get_args(hint)
        if (
            get_origin(hint) is tuple
            and arguments
            and isinstance(arguments[0], type)
            and issubclass(arguments[0], Entity)
        ):
            result[field_info.name] = getattr(model, field_info.name)
    if not result:
        raise ValueError("model exposes no entity collections to review")
    return result


def _validated_evidence(
    model: BuildingModel,
    evidence: Sequence[EvidenceReference] | None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Validate typed evidence bindings against the current model; fail closed.

    Every reference must name a known entity and bind to one of that
    entity's *actual current* provenance records: source kind and id must
    match, the page must match, and the record's content fingerprint must
    still equal the one the reference carries. A record that was edited or
    replaced after the reference was made fails as stale. Reference ids
    must be unique. Nothing is fetched and nothing is invented for entities
    the caller did not name.
    """

    if evidence is None:
        return {}
    collections = _entity_collections(model)
    entity_records: dict[str, tuple[Provenance, ...]] = {
        entity.id: entity.provenance
        for collection in collections.values()
        for entity in collection
    }
    validated: dict[str, list[dict[str, Any]]] = {}
    seen_refs: dict[str, str] = {}
    for reference in evidence:
        if not isinstance(reference, EvidenceReference):
            raise ValueError("evidence entries must be EvidenceReference bindings")
        previous = seen_refs.get(reference.ref_id)
        if previous is not None:
            raise ValueError(
                f"duplicate evidence reference id {reference.ref_id!r} for entities "
                f"{previous!r} and {reference.entity_id!r}"
            )
        seen_refs[reference.ref_id] = reference.entity_id
        records = entity_records.get(reference.entity_id)
        if records is None:
            raise ValueError(
                f"evidence references unknown or stale entity id "
                f"{reference.entity_id!r}"
            )
        same_source = [
            record for record in records
            if record.source_kind == reference.source_kind
            and record.source_id == reference.source_id
        ]
        if not same_source:
            raise ValueError(
                f"evidence reference {reference.ref_id!r} binds to source "
                f"{reference.source_kind}:{reference.source_id}, which entity "
                f"{reference.entity_id!r} does not currently carry"
            )
        same_page = [
            record for record in same_source
            if record.page == reference.page
        ]
        if not same_page:
            raise ValueError(
                f"evidence reference {reference.ref_id!r} names page "
                f"{reference.page!r}, but source "
                f"{reference.source_kind}:{reference.source_id} currently "
                f"carries pages {sorted({record.page for record in same_source})}"
            )
        bound = next(
            (record for record in same_page
             if fingerprint_provenance(record) == reference.fingerprint),
            None,
        )
        if bound is None:
            raise ValueError(
                f"evidence reference {reference.ref_id!r} is stale: record "
                f"{reference.source_kind}:{reference.source_id} on entity "
                f"{reference.entity_id!r} changed after the reference was made"
            )
        validated.setdefault(reference.entity_id, []).append(reference.to_dict())
    return {
        entity_id: tuple(sorted(refs, key=lambda entry: entry["ref_id"]))
        for entity_id, refs in validated.items()
    }


def _warning_item_id(warning: QuantityWarning) -> str:
    """Deterministic, collision-free identity for one warning item.

    Source ids are normalized (sorted) before the id is built, and the
    message digest is part of the identity, so two distinct warnings that
    share a code and sources never collapse onto one id while the same
    warning always yields the same id.
    """

    source_ids = "|".join(sorted(warning.source_entity_ids))
    digest = hashlib.sha256(warning.message.encode("utf-8")).hexdigest()[:12]
    return f"review:warning:{warning.code}:{source_ids}:{digest}"


def _warning_items(
    warnings: Iterable[QuantityWarning],
    crops_by_entity: Mapping[str, tuple[dict[str, Any], ...]],
    collections: Mapping[str, tuple[Entity, ...]],
) -> list[ReviewItem]:
    """Deterministic items for actual quantity warnings.

    Normalizes source ids before deduplication, keys the dedup on the full
    (code, message, sources) identity so distinct warnings never collide,
    and sorts by the resulting item id so input order cannot change the
    output.

    A model-level warning (no source entities) keeps empty ``entity_ids``:
    inventing an entity would be a lie. When evidence is attached, the
    warning item also carries the provenance records that evidence binds
    to, so the item stays self-verifying under the portable binding rule.
    """

    records_by_entity: dict[str, tuple[Provenance, ...]] = {
        entity.id: entity.provenance
        for collection in collections.values()
        for entity in collection
    }
    items: list[ReviewItem] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for warning in warnings:
        source_ids = tuple(sorted(warning.source_entity_ids))
        key = (warning.code, warning.message, source_ids)
        if key in seen:
            continue
        seen.add(key)
        named_kinds = sorted({
            name
            for name in sorted(collections)
            for entity in collections[name]
            if entity.id in source_ids
        })
        kind = named_kinds[0] if len(named_kinds) == 1 else (
            "mixed" if named_kinds else "model"
        )
        attached: dict[str, dict[str, Any]] = {}
        bound_records: list[Provenance] = []
        for entity_id in source_ids:
            for entry in crops_by_entity.get(entity_id, ()):
                attached[entry["ref_id"]] = entry
                record = next(
                    (
                        record for record in records_by_entity.get(entity_id, ())
                        if record.source_kind == entry["source_kind"]
                        and record.source_id == entry["source_id"]
                        and record.page == entry.get("page")
                    ),
                    None,
                )
                if record is not None:
                    bound_records.append(record)
        provenance = _provenance_payload(bound_records)
        items.append(ReviewItem(
            item_id=_warning_item_id(warning),
            entity_kind=kind,
            entity_ids=source_ids,
            reasons=(f"{_QUANTITY_WARNING_PREFIX}{warning.code}",),
            detail=f"quantity warning {warning.code}: {warning.message}",
            confidence=None,
            provenance=provenance,
            crops=tuple(sorted(attached.values(), key=lambda entry: entry["ref_id"])),
        ))
    items.sort(key=lambda item: item.item_id)
    return items


def build_confidence_review(
    model: BuildingModel,
    config: ReviewConfig,
    *,
    evidence: Sequence[EvidenceReference] | None = None,
) -> ConfidenceReviewManifest:
    """Build the deterministic review manifest for ``model``.

    The model is only read: takeoff warnings come from the real
    ``extract_quantities`` run, and the model's serialized bytes are the
    same before and after. Items are emitted for

    - entities whose own confidence is below the threshold
      (``low_entity_confidence``),
    - entities whose supporting provenance records carry a confidence below
      the threshold even when the entity's own confidence is high
      (``low_supporting_provenance_confidence``) — a confident record never
      hides a less confident source,
    - entities with no provenance at all (``missing_provenance``),
    - box/raceway (and, once merged, support) selection outcomes whose
      status is ``unresolved`` (``unresolved_selection``), and
    - actual quantity warnings from the takeoff
      (``quantity_warning_<code>``), attached to each source entity named
      by the warning.

    Supplied ``evidence`` bindings ride along on the items of the entity
    they are bound to; entities nobody named carry no crop references.
    """

    if not isinstance(model, BuildingModel):
        raise TypeError("model must be a BuildingModel")
    if not isinstance(config, ReviewConfig):
        raise TypeError("config must be a ReviewConfig")
    threshold = config.confidence_threshold
    crops_by_entity = _validated_evidence(model, evidence)

    collections = _entity_collections(model)
    items: list[ReviewItem] = []

    for kind in sorted(collections):
        for entity in sorted(collections[kind], key=lambda item: item.id):
            reasons: list[str] = []
            facts: list[str] = []
            if entity.confidence < threshold:
                reasons.append(_REASON_LOW_CONFIDENCE)
                facts.append(
                    f"entity confidence {entity.confidence} is below the "
                    f"review threshold {threshold}"
                )
            if not entity.provenance:
                reasons.append(_REASON_MISSING_PROVENANCE)
                facts.append("no supporting provenance records exist")
            else:
                low_records = [
                    record for record in entity.provenance
                    if record.confidence < threshold
                ]
                if low_records:
                    reasons.append(_REASON_LOW_SUPPORT)
                    for record in sorted(
                        low_records,
                        key=lambda record: (
                            record.source_kind, record.source_id, record.confidence,
                        ),
                    ):
                        facts.append(
                            f"supporting provenance {record.source_kind}:"
                            f"{record.source_id} has confidence "
                            f"{record.confidence}, below the review threshold "
                            f"{threshold}"
                        )
            status = getattr(entity, "status", None)
            if status == "unresolved":
                reasons.append(_REASON_UNRESOLVED_SELECTION)
                reason_codes = tuple(getattr(entity, "reason_codes", ()))
                facts.append(
                    "selection outcome is unresolved"
                    + (f" with reasons {list(reason_codes)}" if reason_codes else "")
                )
            if not reasons:
                continue
            items.append(ReviewItem(
                item_id=f"review:entity:{entity.id}",
                entity_kind=kind,
                entity_ids=(entity.id,),
                reasons=tuple(reasons),
                detail="; ".join(facts),
                confidence=entity.confidence,
                provenance=_provenance_payload(entity.provenance),
                crops=crops_by_entity.get(entity.id, ()),
            ))

    items.extend(_warning_items(
        extract_quantities(model).warnings, crops_by_entity, collections,
    ))
    items.sort(key=lambda item: item.item_id)
    return ConfidenceReviewManifest(
        model_id=model.model_id,
        confidence_threshold=threshold,
        items=tuple(items),
        collections=tuple(sorted(collections)),
    )
