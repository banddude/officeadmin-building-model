"""Typed physical electrical objects introduced explicitly by contract 1.1."""

from dataclasses import dataclass
import re

from .common import ContractError, Entity, Point3, Pose, Size3, _positive, _validate_id

BOX_MOUNTINGS = frozenset({"flush", "surface", "suspended", "floor"})


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogProduct:
    catalog_id: str
    catalog_revision: str
    product_id: str

    def __post_init__(self) -> None:
        for name in ("catalog_id", "catalog_revision", "product_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"CatalogProduct.{name} must be nonempty")


def _occupants(ids: tuple[str, ...], label: str) -> None:
    if not ids or len(ids) != len(set(ids)):
        raise ContractError(f"{label} requires nonempty unique occupant device ids")
    for identity in ids:
        _validate_id(identity, label)


@dataclass(frozen=True, slots=True, kw_only=True)
class ElectricalBox(Entity):
    intent_id: str
    box_type: str
    pose: Pose
    size: Size3
    level_id: str
    host_id: str
    mounting: str
    occupant_ids: tuple[str, ...]
    product: CatalogProduct
    listed_volume_m3: float | None = None

    def __post_init__(self) -> None:
        super(ElectricalBox, self).__post_init__()
        for label in ("intent_id", "level_id", "host_id"):
            _validate_id(getattr(self, label), f"{self.id}.{label}")
        if not isinstance(self.box_type, str) or not self.box_type.strip():
            raise ContractError(f"{self.id}.box_type must be nonempty")
        if not isinstance(self.pose, Pose) or not isinstance(self.size, Size3) or not isinstance(self.product, CatalogProduct):
            raise ContractError(f"{self.id} requires typed pose, size and catalog product")
        if self.mounting not in BOX_MOUNTINGS:
            raise ContractError(f"{self.id}.mounting must be one of {sorted(BOX_MOUNTINGS)}")
        _occupants(self.occupant_ids, f"{self.id}.occupant_ids")
        if self.listed_volume_m3 is not None:
            _positive(self.listed_volume_m3, f"{self.id}.listed_volume_m3")
            if self.listed_volume_m3 > self.size.x * self.size.y * self.size.z:
                raise ContractError(f"{self.id}.listed_volume_m3 exceeds its exterior envelope")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoxSelectionOutcome(Entity):
    intent_id: str
    occupant_ids: tuple[str, ...]
    status: str
    selected_box_id: str | None = None
    requested_product: CatalogProduct | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(BoxSelectionOutcome, self).__post_init__()
        _validate_id(self.intent_id, f"{self.id}.intent_id")
        _occupants(self.occupant_ids, f"{self.id}.occupant_ids")
        if self.status not in {"selected", "unresolved"}:
            raise ContractError(f"{self.id}.status must be selected or unresolved")
        if self.selected_box_id is not None:
            _validate_id(self.selected_box_id, f"{self.id}.selected_box_id")
        if self.requested_product is not None and not isinstance(self.requested_product, CatalogProduct):
            raise ContractError(f"{self.id}.requested_product must be a CatalogProduct or null")
        if len(set(self.reason_codes)) != len(self.reason_codes) or any(
            not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]*", reason) is None
            for reason in self.reason_codes
        ):
            raise ContractError(f"{self.id}.reason_codes must be unique reason tokens")
        if self.status == "selected" and (self.selected_box_id is None or self.requested_product is None or self.reason_codes):
            raise ContractError(f"{self.id} selected outcome requires a box and no unresolved reasons")
        if self.status == "unresolved" and (self.selected_box_id is not None or not self.reason_codes):
            raise ContractError(f"{self.id} unresolved outcome requires reasons and no physical box")


def box_contains_point(box: ElectricalBox, point: Point3, tolerance: float = 1e-6) -> bool:
    """Physical box ports lie inside/on the independent oriented envelope."""
    q = box.pose.rotation
    u = (-q.x, -q.y, -q.z)  # inverse of its normalized quaternion
    v = (point.x - box.pose.position.x, point.y - box.pose.position.y, point.z - box.pose.position.z)
    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    first = cross(u, v)
    second = cross(u, first)
    local = tuple(v[i] + 2*q.w*first[i] + 2*second[i] for i in range(3))
    return all(abs(coordinate) <= size/2 + tolerance
               for coordinate, size in zip(local, (box.size.x, box.size.y, box.size.z)))
