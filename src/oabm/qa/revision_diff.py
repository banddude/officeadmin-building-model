"""Deterministic canonical revision diff between two snapshots (#274).

A plan revision arrives as a new import; without a diff, every revision
looks like a new job and every object looks new. This module compares two
explicitly comparable ``BuildingModel`` snapshots — the same model
identity, the same coordinate frame and units, the same schema version —
by stable canonical ID and collection, and reports what was added,
removed or changed, with explicit field paths and a change class that
separates geometry from product/basis, electrical graph and provenance.

It is a derived view, exactly like a takeoff or a drawing:

- Neither snapshot is mutated, and no second semantic model is created;
  the diff reads the canonical entities as they are.
- Comparability is required, never guessed: a different model id, a
  different coordinate frame or units, or mismatched schema versions is a
  clear refusal. There is no nearest-geometry matching, no ID mapping and
  no all-added/all-removed fallback. The supported scope is two snapshots
  of the same model lineage (same ``model_id``), typically a model and a
  later import revision of itself.
- Reordering an entity collection is not a change; reordering points
  inside one route's centerline is.
- Quantity deltas come from the real ``extract_quantities`` run on both
  snapshots, matched on complete item identity (category, type, variant,
  unit). Additions and removals stay separate and auditable, unchanged
  scope contributes no line, and nothing is priced: the diff carries
  quantities and counts, never money.
- Item and report identifiers are stable under input permutations, and
  identical snapshot pairs produce byte-identical JSON.

This child does not implement PDF registration, cross-source ID
reconciliation, priced change orders, OA writes or a production revision
viewer, and it does not claim to reconcile identities across sources.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Iterable, Mapping, get_args, get_origin, get_type_hints

from oabm.assembly_compat import require_no_populated_building_assemblies
from oabm.model import BuildingModel, Entity
from oabm.quantities import extract_quantities

#: Version of the portable diff document. Bump on breaking changes to its
#: key sets or semantics; strict decoding rejects other versions.
REVISION_DIFF_VERSION = "1.0.0"

_DIFF_KIND = "canonical_revision_diff"

#: Change classes: the reviewer needs to know WHAT moved, not just that a
#: byte changed. Geometry moves the building; product/basis changes what
#: would be bought; graph changes rewire the electrical topology;
#: provenance changes the evidence trail; attribute is anything else.
_CLASS_GEOMETRY = "geometry"
_CLASS_PRODUCT = "product"
_CLASS_GRAPH = "graph"
_CLASS_PROVENANCE = "provenance"
_CLASS_ATTRIBUTE = "attribute"

_GEOMETRY_FIELDS = frozenset({
    "centerline", "footprint", "pose", "size", "thickness_m", "height_m",
    "elevation_m", "listed_volume_m3", "nominal_diameter_m", "points",
})
_PRODUCT_FIELDS = frozenset({
    "product", "basis", "requested_product", "requested_basis",
    "selected_box_id", "selected_selection_id", "selected_assembly_id",
    "route_type", "wiring_method", "designation", "designation_kind",
    "assembly_type", "component_type", "category", "box_type", "mounting",
})
_GRAPH_FIELDS = frozenset({
    "connected_port_ids", "route_ids", "load_port_ids", "source_port_id",
    "start_port_id", "end_port_id", "circuit_id", "owner_id", "host_id",
    "occupant_ids", "supported_object_ids", "intent_id", "level_id",
    "fitting_ids", "circuit_ids", "role", "count",
})
_PROVENANCE_FIELDS = frozenset({"provenance", "confidence"})


def _change_classes(field_name: str) -> tuple[str, ...]:
    if field_name in _GEOMETRY_FIELDS:
        return (_CLASS_GEOMETRY,)
    if field_name in _PRODUCT_FIELDS:
        return (_CLASS_PRODUCT,)
    if field_name in _GRAPH_FIELDS:
        return ("graph",)
    if field_name in _PROVENANCE_FIELDS:
        return (_CLASS_PROVENANCE,)
    return (_CLASS_ATTRIBUTE,)


def _entity_document(entity: Any) -> dict[str, Any]:
    """Canonical JSON-able document of one entity, via its dataclass fields."""

    return dataclasses.asdict(entity)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _provenance_sort_key(entry: Mapping[str, Any]) -> tuple:
    return (
        entry.get("source_kind") or "",
        entry.get("source_id") or "",
        entry.get("source_element_id") or "",
        entry.get("page") if entry.get("page") is not None else -1,
        entry.get("method") or "",
        entry.get("confidence") if entry.get("confidence") is not None else -1.0,
        entry.get("derivation") or "",
        _canonical_json(entry.get("scope_paths")),
        _canonical_json(entry.get("attributes")),
    )


def _normalized_document(entity: Entity) -> dict[str, Any]:
    """Entity document with provenance order normalized.

    Provenance records carry no positional meaning inside an entity, so a
    caller that re-emits them in another order must not produce phantom
    changes. Everything else — including point order inside geometry — is
    compared exactly as the model holds it.
    """

    document = _entity_document(entity)
    provenance = document.get("provenance")
    if isinstance(provenance, list):
        document["provenance"] = sorted(provenance, key=_provenance_sort_key)
    return document


def _require_comparable(base: BuildingModel, target: BuildingModel) -> None:
    """Refuse anything the diff would have to guess about.

    The supported scope is two snapshots of the same model lineage: same
    ``model_id``, same coordinate frame and units, same schema version.
    Anything else is a refusal with the exact reason — never a
    nearest-geometry guess, and never a false all-added/all-removed report.
    """

    if not isinstance(base, BuildingModel) or not isinstance(target, BuildingModel):
        raise TypeError("both snapshots must be BuildingModel instances")
    if base.model_id != target.model_id:
        raise ValueError(
            "refusing to diff different model identities: "
            f"{base.model_id!r} and {target.model_id!r}. Cross-source ID "
            "reconciliation is out of scope; diff snapshots of the same model."
        )
    if base.coordinate_system != target.coordinate_system:
        raise ValueError(
            "refusing to diff different coordinate frames or units: "
            f"{base.coordinate_system} vs {target.coordinate_system}"
        )
    if base.schema_version != target.schema_version:
        raise ValueError(
            "refusing to diff mismatched schema versions: "
            f"{base.schema_version!r} and {target.schema_version!r}. "
            "Migrate both snapshots to the same version first; legacy "
            "versions remain comparable with themselves."
        )


def _entity_collections(model: BuildingModel) -> dict[str, dict[str, Entity]]:
    """Every current entity collection, discovered from the model's type."""

    hints = get_type_hints(type(model))
    result: dict[str, dict[str, Entity]] = {}
    for field_info in dataclass_fields(model):
        hint = hints[field_info.name]
        arguments = get_args(hint)
        if (
            get_origin(hint) is tuple
            and arguments
            and isinstance(arguments[0], type)
            and issubclass(arguments[0], Entity)
        ):
            result[field_info.name] = {
                entity.id: entity for entity in getattr(model, field_info.name)
            }
    if not result:
        raise ValueError("model exposes no entity collections to diff")
    return result


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One changed field: its path and the exact before/after values."""

    path: str
    change_classes: tuple[str, ...]
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change_classes": list(self.change_classes),
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class EntityChange:
    """One added, removed or changed canonical object."""

    change: str  # "added", "removed" or "changed"
    collection: str
    entity_id: str
    change_id: str
    change_classes: tuple[str, ...]
    field_changes: tuple[FieldChange, ...]
    #: Full canonical document, present for added and removed objects.
    document: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "change": self.change,
            "collection": self.collection,
            "entity_id": self.entity_id,
            "change_id": self.change_id,
            "change_classes": list(self.change_classes),
            "field_changes": [entry.to_dict() for entry in self.field_changes],
        }
        if self.document is not None:
            payload["document"] = self.document
        return payload


@dataclass(frozen=True, slots=True)
class QuantityDelta:
    """One signed takeoff delta, matched on complete item identity."""

    delta_id: str
    category: str
    item_type: str
    unit: str
    variant: tuple[tuple[str, Any], ...]
    status: str  # "added", "removed" or "changed"
    before: float | None
    after: float | None
    delta: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "category": self.category,
            "item_type": self.item_type,
            "unit": self.unit,
            "variant": [[key, value] for key, value in self.variant],
            "status": self.status,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
        }


def _variant_key(variant: Iterable[tuple[str, Any]]) -> str:
    return _canonical_json([[key, value] for key, value in variant])


@dataclass(frozen=True, slots=True)
class RevisionDiff:
    """The portable diff of two comparable snapshots."""

    base_model_id: str
    target_model_id: str
    schema_version: str
    added_count: int
    removed_count: int
    changed_count: int
    changes: tuple[EntityChange, ...]
    quantity_deltas: tuple[QuantityDelta, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": _DIFF_KIND,
            "version": REVISION_DIFF_VERSION,
            "base_model_id": self.base_model_id,
            "target_model_id": self.target_model_id,
            "schema_version": self.schema_version,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "changed_count": self.changed_count,
            "changes": [entry.to_dict() for entry in self.changes],
            "quantity_deltas": [entry.to_dict() for entry in self.quantity_deltas],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> RevisionDiff:
        """Strict decode: unknown keys, wrong kinds/versions and bad values fail."""

        if not isinstance(document, Mapping):
            raise ValueError("revision diff must be a JSON object")
        allowed = {
            "kind", "version", "base_model_id", "target_model_id",
            "schema_version", "added_count", "removed_count", "changed_count",
            "changes", "quantity_deltas",
        }
        unknown = set(document) - allowed
        if unknown:
            raise ValueError(f"unknown revision diff keys: {sorted(unknown)}")
        if document.get("kind") != _DIFF_KIND:
            raise ValueError(
                f"revision diff kind must be {_DIFF_KIND!r}, got {document.get('kind')!r}"
            )
        if document.get("version") != REVISION_DIFF_VERSION:
            raise ValueError(
                f"revision diff version must be {REVISION_DIFF_VERSION!r}, "
                f"got {document.get('version')!r}"
            )
        for key in ("base_model_id", "target_model_id", "schema_version"):
            if not isinstance(document.get(key), str) or not document[key]:
                raise ValueError(f"revision diff {key} must be a non-empty string")
        if document["base_model_id"] != document["target_model_id"]:
            raise ValueError(
                "revision diff must describe one model identity; "
                f"got {document['base_model_id']!r} and {document['target_model_id']!r}"
            )
        for key in ("added_count", "removed_count", "changed_count"):
            value = document.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"revision diff {key} must be a non-negative integer")
        if not isinstance(document.get("changes"), list):
            raise ValueError("revision diff changes must be a list")
        changes: list[EntityChange] = []
        for entry in document["changes"]:
            if not isinstance(entry, Mapping):
                raise ValueError("each diff change must be a JSON object")
            change_keys = {"change", "collection", "entity_id", "change_id",
                           "change_classes", "field_changes", "document"}
            unknown_change = set(entry) - change_keys
            if unknown_change:
                raise ValueError(f"unknown diff change keys: {sorted(unknown_change)}")
            for key in ("change", "collection", "entity_id", "change_id"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"diff change {key} must be a non-empty string")
            if entry["change"] not in {"added", "removed", "changed"}:
                raise ValueError(f"unknown diff change kind {entry['change']!r}")
            classes = entry.get("change_classes")
            if (
                not isinstance(classes, list)
                or any(not isinstance(value, str) for value in classes)
                or len(set(classes)) != len(classes)
            ):
                raise ValueError("diff change_classes must be unique strings")
            field_changes = entry.get("field_changes")
            if not isinstance(field_changes, list):
                raise ValueError("diff field_changes must be a list")
            parsed_fields: list[FieldChange] = []
            for field_entry in field_changes:
                if not isinstance(field_entry, Mapping):
                    raise ValueError("each field change must be a JSON object")
                if set(field_entry) != {"path", "change_classes", "before", "after"}:
                    raise ValueError(
                        f"field change keys must be exactly "
                        f"path/change_classes/before/after, got {sorted(field_entry)}"
                    )
                if not isinstance(field_entry["path"], str) or not field_entry["path"]:
                    raise ValueError("field change path must be a non-empty string")
                parsed_fields.append(FieldChange(
                    path=field_entry["path"],
                    change_classes=tuple(field_entry["change_classes"]),
                    before=field_entry["before"],
                    after=field_entry["after"],
                ))
            if entry["change"] == "changed" and entry.get("document") is not None:
                raise ValueError("a changed record carries field changes, not a document")
            if entry["change"] in {"added", "removed"} and not isinstance(
                entry.get("document"), Mapping
            ):
                raise ValueError("added and removed records carry their document")
            changes.append(EntityChange(
                change=entry["change"],
                collection=entry["collection"],
                entity_id=entry["entity_id"],
                change_id=entry["change_id"],
                change_classes=tuple(classes),
                field_changes=tuple(parsed_fields),
                document=(
                    None if entry.get("document") is None
                    else dict(entry["document"])
                ),
            ))
        if not isinstance(document.get("quantity_deltas"), list):
            raise ValueError("revision diff quantity_deltas must be a list")
        deltas: list[QuantityDelta] = []
        for entry in document["quantity_deltas"]:
            if not isinstance(entry, Mapping):
                raise ValueError("each quantity delta must be a JSON object")
            delta_keys = {
                "delta_id", "category", "item_type", "unit", "variant",
                "status", "before", "after", "delta",
            }
            if set(entry) != delta_keys:
                raise ValueError(
                    f"quantity delta keys must be exactly {sorted(delta_keys)}, "
                    f"got {sorted(entry)}"
                )
            for key in ("delta_id", "category", "item_type", "unit", "status"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"quantity delta {key} must be a non-empty string")
            if entry["status"] not in {"added", "removed", "changed"}:
                raise ValueError(f"unknown quantity delta status {entry['status']!r}")
            variant = entry.get("variant")
            if not isinstance(variant, list) or any(
                not isinstance(pair, list) or len(pair) != 2
                or not isinstance(pair[0], str)
                for pair in variant
            ):
                raise ValueError("quantity delta variant must be [key, value] pairs")
            numbers: dict[str, float | None] = {}
            for key in ("before", "after", "delta"):
                value = entry.get(key)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(
                        f"quantity delta {key} must be null or a finite number"
                    )
                numbers[key] = None if value is None else float(value)
            deltas.append(QuantityDelta(
                delta_id=entry["delta_id"],
                category=entry["category"],
                item_type=entry["item_type"],
                unit=entry["unit"],
                variant=tuple((pair[0], pair[1]) for pair in variant),
                status=entry["status"],
                before=numbers["before"],
                after=numbers["after"],
                delta=numbers["delta"],
            ))
        built = cls(
            base_model_id=document["base_model_id"],
            target_model_id=document["target_model_id"],
            schema_version=document["schema_version"],
            added_count=document["added_count"],
            removed_count=document["removed_count"],
            changed_count=document["changed_count"],
            changes=tuple(changes),
            quantity_deltas=tuple(deltas),
        )
        # Counts must agree with the records they summarize.
        for attribute, status in (
            ("added_count", "added"),
            ("removed_count", "removed"),
            ("changed_count", "changed"),
        ):
            actual = sum(1 for entry in built.changes if entry.change == status)
            if getattr(built, attribute) != actual:
                raise ValueError(
                    f"revision diff {attribute} is {getattr(built, attribute)} "
                    f"but {actual} {status} records are present"
                )
        return built


def _quantity_map(model: BuildingModel) -> dict[tuple, Any]:
    """Complete-identity map of one snapshot's real takeoff lines."""

    report = extract_quantities(model)
    result: dict[tuple, Any] = {}
    for item in report.items:
        key = (item.category, item.item_type, item.unit, _variant_key(item.variant))
        # Identical identity lines cannot occur within one report; if a
        # producer ever emits them, keep the first deterministically.
        result.setdefault(key, item)
    return result


def build_revision_diff(base: BuildingModel, target: BuildingModel) -> RevisionDiff:
    """Diff two comparable snapshots of one model, with quantity deltas.

    Neither snapshot is read destructively, and both are revalidated by the
    quantities lane as a side effect of the real takeoff on each.
    """

    require_no_populated_building_assemblies(base, "build_revision_diff base snapshot")
    require_no_populated_building_assemblies(target, "build_revision_diff target snapshot")
    _require_comparable(base, target)
    base_collections = _entity_collections(base)
    target_collections = _entity_collections(target)
    if set(base_collections) != set(target_collections):
        missing = sorted(set(base_collections) ^ set(target_collections))
        raise ValueError(
            f"refusing to diff snapshots exposing different collections: {missing}"
        )

    changes: list[EntityChange] = []
    for name in sorted(base_collections):
        base_entities = base_collections[name]
        target_entities = target_collections[name]
        for entity_id in sorted(set(base_entities) | set(target_entities)):
            in_base = entity_id in base_entities
            in_target = entity_id in target_entities
            if in_base and not in_target:
                changes.append(EntityChange(
                    change="removed",
                    collection=name,
                    entity_id=entity_id,
                    change_id=f"revision:removed:{name}:{entity_id}",
                    change_classes=(),
                    field_changes=(),
                    document=_normalized_document(base_entities[entity_id]),
                ))
                continue
            if in_target and not in_base:
                changes.append(EntityChange(
                    change="added",
                    collection=name,
                    entity_id=entity_id,
                    change_id=f"revision:added:{name}:{entity_id}",
                    change_classes=(),
                    field_changes=(),
                    document=_normalized_document(target_entities[entity_id]),
                ))
                continue
            before_doc = _normalized_document(base_entities[entity_id])
            after_doc = _normalized_document(target_entities[entity_id])
            field_changes: list[FieldChange] = []
            classes: set[str] = set()
            for field_name in sorted(set(before_doc) | set(after_doc)):
                before_value = before_doc.get(field_name)
                after_value = after_doc.get(field_name)
                if _canonical_json(before_value) == _canonical_json(after_value):
                    continue
                field_classes = _change_classes(field_name)
                classes.update(field_classes)
                field_changes.append(FieldChange(
                    path=field_name,
                    change_classes=field_classes,
                    before=before_value,
                    after=after_value,
                ))
            if field_changes:
                ordered_classes = [
                    value for value in (
                        _CLASS_GEOMETRY, _CLASS_PRODUCT, _CLASS_GRAPH,
                        _CLASS_PROVENANCE, _CLASS_ATTRIBUTE,
                    ) if value in classes
                ]
                changes.append(EntityChange(
                    change="changed",
                    collection=name,
                    entity_id=entity_id,
                    change_id=f"revision:changed:{name}:{entity_id}",
                    change_classes=tuple(ordered_classes),
                    field_changes=tuple(field_changes),
                    document=None,
                ))

    changes.sort(key=lambda entry: entry.change_id)

    base_quantities = _quantity_map(base)
    target_quantities = _quantity_map(target)
    deltas: list[QuantityDelta] = []
    for key in sorted(set(base_quantities) | set(target_quantities)):
        before_item = base_quantities.get(key)
        after_item = target_quantities.get(key)
        category, item_type, unit, variant_json = key
        variant = tuple((pair[0], pair[1]) for pair in json.loads(variant_json))
        if before_item is not None and after_item is not None:
            if before_item.quantity == after_item.quantity:
                # Unchanged original scope: zero delta, never counted twice,
                # and never emitted as a line at all.
                continue
            status = "changed"
            before_qty = before_item.quantity
            after_qty = after_item.quantity
            delta = after_qty - before_qty
        elif after_item is not None:
            status, before_qty, after_qty, delta = "added", None, after_item.quantity, None
        else:
            status, before_qty, after_qty, delta = (
                "removed", before_item.quantity, None, None,
            )
        digest = hashlib.sha256(
            _canonical_json([category, item_type, unit, variant_json]).encode("utf-8")
        ).hexdigest()[:12]
        deltas.append(QuantityDelta(
            delta_id=f"revision:quantity:{category}:{item_type}:{digest}",
            category=category,
            item_type=item_type,
            unit=unit,
            variant=variant,
            status=status,
            before=before_qty,
            after=after_qty,
            delta=delta,
        ))
    deltas.sort(key=lambda entry: entry.delta_id)

    return RevisionDiff(
        base_model_id=base.model_id,
        target_model_id=target.model_id,
        schema_version=base.schema_version,
        added_count=sum(1 for entry in changes if entry.change == "added"),
        removed_count=sum(1 for entry in changes if entry.change == "removed"),
        changed_count=sum(1 for entry in changes if entry.change == "changed"),
        changes=tuple(changes),
        quantity_deltas=tuple(deltas),
    )
