"""Deterministic explicit-intent/catalog boundary, not automatic code sizing."""

from dataclasses import asdict, dataclass, replace
import json
from typing import Sequence

from oabm.model import (
    BOX_MOUNTINGS, PHYSICAL_SCHEMA_VERSION, BuildingModel, BoxSelectionOutcome,
    CatalogProduct, ContractError, DERIVATION_USER, ElectricalBox, Pose, Provenance,
    Size3, stable_id,
)
from oabm.model.common import _positive, _validate_id

_INTENT_METHOD = "explicit-electrical-box-intent"
_PRODUCER_KIND = "electrical-box-producer"
_CATALOG_FIELDS = ("product", "box_type", "size", "listed_volume_m3")


def _portable(value):
    return json.loads(json.dumps(asdict(value), sort_keys=True, allow_nan=False))


@dataclass(frozen=True, slots=True, kw_only=True)
class BoxCatalogEntry:
    product: CatalogProduct
    box_type: str
    size: Size3 | None
    provenance: tuple[Provenance, ...]
    listed_volume_m3: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product, CatalogProduct) or not isinstance(self.box_type, str) or not self.box_type.strip():
            raise ContractError("box catalog requires a typed product and box type")
        if self.size is not None and not isinstance(self.size, Size3):
            raise ContractError("catalog size must be Size3 or unknown")
        if not self.provenance or any(p.derivation is None for p in self.provenance):
            raise ContractError("catalog entries require explicit derivation provenance")
        for record in self.provenance:
            if any(path.split(".")[0] not in _CATALOG_FIELDS for path in (record.scope_paths or ())):
                raise ContractError("catalog provenance must qualify consumed catalog fields")
        if self.listed_volume_m3 is not None:
            _positive(self.listed_volume_m3, "catalog.listed_volume_m3")
            if self.size is not None and self.listed_volume_m3 > self.size.x * self.size.y * self.size.z:
                raise ContractError("listed capacity exceeds the catalog exterior envelope")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoxIntent:
    intent_id: str
    occupant_ids: tuple[str, ...]
    actor_id: str
    product: CatalogProduct | None = None
    pose: Pose | None = None
    host_id: str | None = None
    mounting: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.intent_id, "intent_id")
        if not self.occupant_ids or len(set(self.occupant_ids)) != len(self.occupant_ids):
            raise ContractError("box intent requires unique occupant device ids")
        for occupant in self.occupant_ids:
            _validate_id(occupant, "occupant_id")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ContractError("box intent requires an explicit actor_id")
        if self.product is not None and not isinstance(self.product, CatalogProduct):
            raise ContractError("intent product must be CatalogProduct or unknown")
        if self.pose is not None and not isinstance(self.pose, Pose):
            raise ContractError("intent pose must be Pose or unknown")
        if self.host_id is not None:
            _validate_id(self.host_id, "host_id")
        if self.mounting is not None and self.mounting not in BOX_MOUNTINGS:
            raise ContractError("unsupported box mounting")


def select_electrical_boxes(
    model: BuildingModel,
    *,
    intents: Sequence[BoxIntent],
    catalog: Sequence[BoxCatalogEntry],
) -> BuildingModel:
    """Recompute this producer's complete inventory from explicit current input.

    Call upgrade_to_v1_1 explicitly first. Existing non-producer box inventory
    is refused rather than overwritten. Repeated input is idempotent; changed
    intent keeps physical/decision IDs. Missing intent for a device is visible
    as an unresolved outcome, not a claim that every device requires a box.
    Removing a box with live ports/routes refuses through reference validation.
    """
    if model.schema_version != PHYSICAL_SCHEMA_VERSION:
        raise ContractError("explicit upgrade_to_v1_1 is required before box selection")
    for entity in (*model.electrical_boxes, *model.box_selection_outcomes):
        if not any(p.source_kind == _PRODUCER_KIND and p.method == _INTENT_METHOD for p in entity.provenance):
            raise ContractError("cannot overwrite physical box inventory owned by another producer/source")
    devices = {d.id: d for d in model.electrical_devices}
    hosts = {h.id: h for h in (*model.walls, *model.slabs, *model.ceilings)}
    entries = {}
    for entry in catalog:
        if entry.product in entries:
            raise ContractError("duplicate box catalog product identity")
        entries[entry.product] = entry
    owned: set[str] = set()
    intent_ids: set[str] = set()
    work = []
    for intent in intents:
        if intent.intent_id in intent_ids:
            raise ContractError("duplicate active box intent")
        intent_ids.add(intent.intent_id)
        for occupant in intent.occupant_ids:
            if occupant not in devices:
                raise ContractError(f"box intent references missing device {occupant}")
            if occupant in owned:
                raise ContractError(f"device {occupant} belongs to multiple box intents")
            owned.add(occupant)
        if intent.host_id is not None and intent.host_id not in hosts:
            raise ContractError(f"box intent references missing/unsupported host {intent.host_id}")
        work.append((intent, False))
    for device in sorted(devices.values(), key=lambda d: d.id):
        if device.id not in owned:
            missing_id = stable_id("box-intent", f"{model.model_id}:missing:{device.id}")
            if missing_id in intent_ids:
                raise ContractError("explicit intent collides with a missing-intent identity")
            work.append((BoxIntent(intent_id=missing_id, occupant_ids=(device.id,),
                                   actor_id="unprovided"), True))

    boxes, outcomes = [], []
    for original_intent, missing in sorted(work, key=lambda pair: pair[0].intent_id):
        intent = replace(original_intent, occupant_ids=tuple(sorted(original_intent.occupant_ids)))
        entry = entries.get(intent.product)
        reasons = []
        if missing:
            reasons.append("missing_intent")
        else:
            if intent.host_id is None:
                reasons.append("missing_host")
            if intent.pose is None:
                reasons.append("missing_pose")
            if intent.mounting is None:
                reasons.append("missing_mounting")
            if entry is None:
                reasons.append("missing_catalog_entry")
            elif entry.size is None:
                reasons.append("missing_catalog_size")
        # A decision records the actual consumed caller input, including nulls.
        intent_record = Provenance(
            source_kind=_PRODUCER_KIND, source_id=model.model_id,
            source_element_id=intent.intent_id, method=_INTENT_METHOD,
            derivation=DERIVATION_USER if not missing else "inferred",
            confidence=1.0 if not missing else 0.0,
            attributes={"actor_id": intent.actor_id, "consumed_intent": _portable(intent),
                        "automatic_code_sizing_performed": False},
        )
        box_id = None
        if not reasons:
            host = hosts[intent.host_id]
            box_id = stable_id("electrical-box", f"{model.model_id}:{intent.intent_id}")
            claim_paths = ("product", "box_type", "size") + (("listed_volume_m3",) if entry.listed_volume_m3 is not None else ())
            catalog_records = tuple(replace(p, scope_paths=p.scope_paths or claim_paths,
                attributes={**p.attributes, "consumed_catalog_entry": _portable(entry)}) for p in entry.provenance)
            box_intent_record = replace(intent_record, scope_paths=(
                "intent_id", "pose", "host_id", "mounting", "occupant_ids"))
            host_level_record = Provenance(
                source_kind=_PRODUCER_KIND, source_id=model.model_id,
                source_element_id=host.id, method="explicit-host-level-assignment",
                derivation="inferred", confidence=host.confidence, scope_paths=("level_id",),
                attributes={"consumed_host_id": host.id, "consumed_level_id": host.level_id},
            )
            boxes.append(ElectricalBox(
                id=box_id, name=f"Electrical box {intent.intent_id}", intent_id=intent.intent_id,
                box_type=entry.box_type, pose=intent.pose, size=entry.size,
                level_id=host.level_id, host_id=host.id, mounting=intent.mounting,
                occupant_ids=intent.occupant_ids, product=entry.product,
                listed_volume_m3=entry.listed_volume_m3,
                confidence=min(p.confidence for p in (intent_record, *catalog_records, host_level_record)),
                provenance=(box_intent_record, *catalog_records, host_level_record),
            ))
        outcome_id = stable_id("box-selection", f"{model.model_id}:{intent.intent_id}")
        outcome_intent_record = replace(intent_record,
            scope_paths=("intent_id", "occupant_ids", "requested_product"))
        decision_record = Provenance(
            source_kind=_PRODUCER_KIND, source_id=model.model_id,
            source_element_id=intent.intent_id, method="explicit-box-selection-outcome",
            derivation="inferred", confidence=intent_record.confidence,
            scope_paths=("status", "selected_box_id", "reason_codes"),
            attributes={"consumed_intent_id": intent.intent_id,
                        "unresolved_reasons": sorted(reasons)},
        )
        outcomes.append(BoxSelectionOutcome(
            id=outcome_id, name=f"Box selection {intent.intent_id}", intent_id=intent.intent_id,
            occupant_ids=intent.occupant_ids, status="unresolved" if reasons else "selected",
            selected_box_id=box_id, requested_product=intent.product,
            reason_codes=tuple(sorted(reasons)), confidence=intent_record.confidence,
            provenance=(outcome_intent_record, decision_record),
        ))
    return replace(model, electrical_boxes=tuple(sorted(boxes, key=lambda b: b.id)),
                   box_selection_outcomes=tuple(sorted(outcomes, key=lambda o: o.id)))
