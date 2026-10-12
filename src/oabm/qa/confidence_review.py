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
- Item identifiers are stable under iteration permutations, and identical
  inputs produce byte-identical JSON.
- Crop/evidence references are supplied explicitly by the caller and are
  validated against the model's entity ids. Missing crops stay missing:
  the manifest never invents evidence. Dangling (unknown entity), stale
  (no longer current) and duplicate references fail closed.
- The manifest is strict and portable: ``from_dict`` rejects unknown keys,
  wrong versions and invalid values, so a manifest written by one process
  decodes exactly in another.

This child does not implement importer self-checks, crop extraction, bid
pricing, or a production viewer, and it does not claim that all uncertainty
is detected: the reasons below are the ones the canonical signals can
actually support today.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from typing import Any, Iterable, Mapping, get_args, get_origin, get_type_hints

from oabm.model import BuildingModel, Entity, Provenance
from oabm.quantities import extract_quantities

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


def _validated_threshold(value: Any) -> float:
    """Validate an explicit confidence threshold and return it as a float.

    Booleans are numbers in Python but not in a review policy: ``True``
    would otherwise act as a 1.0 threshold. Non-numbers, non-finite values
    and values outside [0, 1] are rejected.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "confidence_threshold must be a finite number in [0, 1] "
            f"(not bool), got {value!r}"
        )
    threshold = float(value)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"confidence_threshold must be a finite number in [0, 1], got {value!r}"
        )
    return threshold


def _provenance_payload(records: Iterable[Provenance]) -> tuple[dict[str, Any], ...]:
    """Provenance exactly as the model holds it, never relabeled.

    Source kind/ID/element/page references, field scopes, confidences and
    derivation classes all travel verbatim so a reviewer can audit the
    original claim rather than this module's opinion of it.
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
    return tuple(payload)


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
    decides what they are worth. ``crops`` carries only caller-supplied
    evidence references for these entities.
    """

    item_id: str
    entity_kind: str
    entity_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    detail: str
    confidence: float | None
    provenance: tuple[dict[str, Any], ...]
    crops: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "entity_kind": self.entity_kind,
            "entity_ids": list(self.entity_ids),
            "reasons": list(self.reasons),
            "detail": self.detail,
            "confidence": self.confidence,
            "provenance": [dict(entry) for entry in self.provenance],
            "crops": list(self.crops),
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
            if (
                not isinstance(document[key], list)
                or any(not isinstance(entry, str) for entry in document[key])
            ):
                raise ValueError(f"review item {key} must be a list of strings")
        if document["confidence"] is not None and (
            isinstance(document["confidence"], bool)
            or not isinstance(document["confidence"], (int, float))
            or not math.isfinite(float(document["confidence"]))
        ):
            raise ValueError("review item confidence must be null or a finite number")
        provenance = document["provenance"]
        if not isinstance(provenance, list) or any(
            not isinstance(entry, Mapping) for entry in provenance
        ):
            raise ValueError("review item provenance must be a list of objects")
        return cls(
            item_id=document["item_id"],
            entity_kind=document["entity_kind"],
            entity_ids=tuple(document["entity_ids"]),
            reasons=tuple(document["reasons"]),
            detail=document["detail"],
            confidence=(
                None if document["confidence"] is None
                else float(document["confidence"])
            ),
            provenance=tuple(dict(entry) for entry in provenance),
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


def _validate_evidence(
    model: BuildingModel,
    evidence: Mapping[str, Iterable[str]] | None,
) -> dict[str, tuple[str, ...]]:
    """Validate caller-supplied crop/evidence references; fail closed.

    References attach to known entity ids of *this* model. An id the model
    does not know is dangling (or stale after an edit) and is rejected, the
    same reference id may not be supplied twice, and references within one
    entity must be unique. Nothing here invents a reference for an entity
    the caller did not name.
    """

    if evidence is None:
        return {}
    known_ids = {
        entity.id
        for collection in _entity_collections(model).values()
        for entity in collection
    }
    validated: dict[str, tuple[str, ...]] = {}
    seen_refs: dict[str, str] = {}
    for entity_id in evidence:
        if entity_id not in known_ids:
            raise ValueError(
                f"evidence references unknown or stale entity id {entity_id!r}"
            )
        refs = tuple(evidence[entity_id])
        for ref in refs:
            if not isinstance(ref, str) or not ref:
                raise ValueError(
                    f"evidence references for {entity_id!r} must be non-empty strings"
                )
            previous = seen_refs.get(ref)
            if previous is not None:
                raise ValueError(
                    f"duplicate evidence reference {ref!r} for entities "
                    f"{previous!r} and {entity_id!r}"
                )
            seen_refs[ref] = entity_id
        if len(set(refs)) != len(refs):
            raise ValueError(f"duplicate evidence references for entity {entity_id!r}")
        validated[entity_id] = refs
    return validated


def build_confidence_review(
    model: BuildingModel,
    config: ReviewConfig,
    *,
    evidence: Mapping[str, Iterable[str]] | None = None,
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

    Supplied ``evidence`` references ride along on the items of the entity
    they name; entities the caller did not name keep no crop references.
    """

    if not isinstance(model, BuildingModel):
        raise TypeError("model must be a BuildingModel")
    if not isinstance(config, ReviewConfig):
        raise TypeError("config must be a ReviewConfig")
    threshold = config.confidence_threshold
    crops_by_entity = _validate_evidence(model, evidence)

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
                    for record in low_records:
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
            crops = crops_by_entity.get(entity.id, ())
            items.append(ReviewItem(
                item_id=f"review:entity:{entity.id}",
                entity_kind=kind,
                entity_ids=(entity.id,),
                reasons=tuple(reasons),
                detail="; ".join(facts),
                confidence=entity.confidence,
                provenance=_provenance_payload(entity.provenance),
                crops=crops,
            ))

    takeoff = extract_quantities(model)
    seen_warnings: set[tuple[str, str, tuple[str, ...]]] = set()
    for warning in takeoff.warnings:
        key = (warning.code, warning.message, warning.source_entity_ids)
        if key in seen_warnings:
            continue
        seen_warnings.add(key)
        source_ids = tuple(sorted(warning.source_entity_ids))
        named_kinds = sorted({
            name
            for name in sorted(collections)
            for entity in collections[name]
            if entity.id in source_ids
        })
        kind = named_kinds[0] if len(named_kinds) == 1 else (
            "mixed" if named_kinds else "model"
        )
        reason = f"{_QUANTITY_WARNING_PREFIX}{warning.code}"
        attached_crops = tuple(
            sorted({
                ref
                for entity_id in source_ids
                for ref in crops_by_entity.get(entity_id, ())
            })
        )
        items.append(ReviewItem(
            item_id=f"review:warning:{warning.code}:{'|'.join(source_ids)}",
            entity_kind=kind,
            entity_ids=source_ids,
            reasons=(reason,),
            detail=f"quantity warning {warning.code}: {warning.message}",
            confidence=None,
            provenance=(),
            crops=attached_crops,
        ))

    items.sort(key=lambda item: item.item_id)
    return ConfidenceReviewManifest(
        model_id=model.model_id,
        confidence_threshold=threshold,
        items=tuple(items),
        collections=tuple(sorted(collections)),
    )
