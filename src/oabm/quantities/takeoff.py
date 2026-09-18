"""Deterministic takeoff quantities derived from the canonical semantic model.

This module intentionally consumes only ``oabm.model`` objects. Linear quantities
come from canonical 3D route centerlines, never from drawings or PDF geometry.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable

from oabm.model import (
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Provenance,
    Route,
    validate_model,
)

Scalar = str | int | float | None
Specification = tuple[tuple[str, Scalar], ...]


@dataclass(frozen=True, slots=True)
class QuantityItem:
    """One aggregated, traceable takeoff line item."""

    category: str
    item_type: str
    quantity: float
    unit: str
    specification: Specification
    assembly_key: str
    source_entity_ids: tuple[str, ...]
    route_ids: tuple[str, ...]
    confidence: float
    provenance: tuple[Provenance, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "item_type": self.item_type,
            "quantity": self.quantity,
            "unit": self.unit,
            "specification": dict(self.specification),
            "assembly_key": self.assembly_key,
            "source_entity_ids": list(self.source_entity_ids),
            "route_ids": list(self.route_ids),
            "confidence": self.confidence,
            "provenance": [_provenance_to_dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class QuantityDiagnostic:
    """A deterministic notice for semantic objects that cannot yet yield a quantity."""

    code: str
    source_entity_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "source_entity_id": self.source_entity_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class QuantityReport:
    """Derived takeoff result for one canonical model revision."""

    source_model_id: str
    source_schema_version: str
    length_unit: str
    angle_unit: str
    items: tuple[QuantityItem, ...]
    diagnostics: tuple[QuantityDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "source_model_id": self.source_model_id,
            "source_schema_version": self.source_schema_version,
            "length_unit": self.length_unit,
            "angle_unit": self.angle_unit,
            "items": [item.to_dict() for item in self.items],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


@dataclass(slots=True)
class _Bucket:
    category: str
    item_type: str
    unit: str
    specification: Specification
    assembly_key: str
    quantities: list[float]
    source_entity_ids: set[str]
    route_ids: set[str]
    confidences: list[float]
    provenance: list[Provenance]


def route_length_m(route: Route) -> float:
    """Return the true 3D centerline length of a canonical route in metres."""

    points = route.centerline.points
    return math.fsum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(points, points[1:])
    )


def extract_quantities(model: BuildingModel) -> QuantityReport:
    """Derive deterministic install quantities from canonical model semantics.

    The canonical model is revalidated at this handoff boundary so callers cannot
    obtain a takeoff from stale or externally-mutated references. Route references
    repeated inside a conductor are de-duplicated to prevent accidental length
    multiplication; conductor ``count`` remains intentional multiplicity.
    """

    validate_model(model)

    route_by_id = {route.id: route for route in model.routes}
    route_lengths = {route.id: route_length_m(route) for route in model.routes}
    buckets: dict[tuple[str, str, str, Specification], _Bucket] = {}
    diagnostics: list[QuantityDiagnostic] = []

    for route in model.routes:
        _contribute(
            buckets,
            category="route_length",
            item_type=route.route_type,
            quantity=route_lengths[route.id],
            unit="m",
            specification=_spec(nominal_diameter_m=route.nominal_diameter_m),
            source_entity_ids=(route.id,),
            route_ids=(route.id,),
            confidences=(route.confidence,),
            provenance=route.provenance,
        )

    for fitting in model.route_fittings:
        route = route_by_id[fitting.route_id]
        _contribute(
            buckets,
            category="fitting",
            item_type=fitting.fitting_type,
            quantity=1.0,
            unit="ea",
            specification=_spec(
                angle_radians=fitting.angle_radians,
                nominal_diameter_m=fitting.nominal_diameter_m,
            ),
            source_entity_ids=(fitting.id,),
            route_ids=(fitting.route_id,),
            confidences=(fitting.confidence, route.confidence),
            provenance=(*fitting.provenance, *route.provenance),
        )

    for conductor in model.conductors:
        route_ids = tuple(sorted(set(conductor.route_ids)))
        if not route_ids:
            diagnostics.append(
                QuantityDiagnostic(
                    code="unrouted_conductor",
                    source_entity_id=conductor.id,
                    message="conductor has no canonical route references; no length was inferred",
                )
            )
            continue
        routes = tuple(route_by_id[route_id] for route_id in route_ids)
        per_conductor_length = math.fsum(route_lengths[route_id] for route_id in route_ids)
        _contribute(
            buckets,
            category="conductor_length",
            item_type=conductor.role,
            quantity=per_conductor_length * conductor.count,
            unit="m",
            specification=_spec(
                insulation=conductor.insulation,
                material=conductor.material,
                size=conductor.size,
            ),
            source_entity_ids=(conductor.id,),
            route_ids=route_ids,
            confidences=(conductor.confidence, *(route.confidence for route in routes)),
            provenance=(
                *conductor.provenance,
                *(item for route in routes for item in route.provenance),
            ),
        )

    for device in model.electrical_devices:
        _count_asset(buckets, "device", device.device_type, device)

    for equipment in model.electrical_equipment:
        _count_asset(buckets, "equipment", equipment.equipment_type, equipment)

    items = tuple(
        _finalize_bucket(bucket)
        for bucket in sorted(buckets.values(), key=lambda item: item.assembly_key)
    )
    diagnostics_tuple = tuple(
        sorted(diagnostics, key=lambda item: (item.code, item.source_entity_id, item.message))
    )
    return QuantityReport(
        source_model_id=model.model_id,
        source_schema_version=model.schema_version,
        length_unit=model.coordinate_system.length_unit,
        angle_unit=model.coordinate_system.angle_unit,
        items=items,
        diagnostics=diagnostics_tuple,
    )


def _count_asset(
    buckets: dict[tuple[str, str, str, Specification], _Bucket],
    category: str,
    item_type: str,
    asset: ElectricalDevice | ElectricalEquipment,
) -> None:
    size = asset.size
    _contribute(
        buckets,
        category=category,
        item_type=item_type,
        quantity=1.0,
        unit="ea",
        specification=_spec(
            rated_voltage_v=asset.rated_voltage_v,
            size_x_m=size.x if size is not None else None,
            size_y_m=size.y if size is not None else None,
            size_z_m=size.z if size is not None else None,
            system=asset.system,
        ),
        source_entity_ids=(asset.id,),
        route_ids=(),
        confidences=(asset.confidence,),
        provenance=asset.provenance,
    )


def _contribute(
    buckets: dict[tuple[str, str, str, Specification], _Bucket],
    *,
    category: str,
    item_type: str,
    quantity: float,
    unit: str,
    specification: Specification,
    source_entity_ids: Iterable[str],
    route_ids: Iterable[str],
    confidences: Iterable[float],
    provenance: Iterable[Provenance],
) -> None:
    key = (category, item_type, unit, specification)
    bucket = buckets.get(key)
    if bucket is None:
        bucket = _Bucket(
            category=category,
            item_type=item_type,
            unit=unit,
            specification=specification,
            assembly_key=_assembly_key(category, item_type, unit, specification),
            quantities=[],
            source_entity_ids=set(),
            route_ids=set(),
            confidences=[],
            provenance=[],
        )
        buckets[key] = bucket

    bucket.quantities.append(float(quantity))
    bucket.source_entity_ids.update(source_entity_ids)
    bucket.route_ids.update(route_ids)
    bucket.confidences.extend(float(value) for value in confidences)
    bucket.provenance.extend(provenance)


def _finalize_bucket(bucket: _Bucket) -> QuantityItem:
    return QuantityItem(
        category=bucket.category,
        item_type=bucket.item_type,
        quantity=math.fsum(sorted(bucket.quantities)),
        unit=bucket.unit,
        specification=bucket.specification,
        assembly_key=bucket.assembly_key,
        source_entity_ids=tuple(sorted(bucket.source_entity_ids)),
        route_ids=tuple(sorted(bucket.route_ids)),
        confidence=min(bucket.confidences) if bucket.confidences else 1.0,
        provenance=_merge_provenance(bucket.provenance),
    )


def _spec(**values: Scalar) -> Specification:
    return tuple(sorted(values.items()))


def _assembly_key(
    category: str,
    item_type: str,
    unit: str,
    specification: Specification,
) -> str:
    payload = {
        "category": category,
        "item_type": item_type,
        "unit": unit,
        "specification": dict(specification),
    }
    return "oabm:" + json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _merge_provenance(provenance: Iterable[Provenance]) -> tuple[Provenance, ...]:
    unique: dict[str, Provenance] = {}
    for item in provenance:
        unique.setdefault(_provenance_key(item), item)
    return tuple(unique[key] for key in sorted(unique))


def _provenance_key(item: Provenance) -> str:
    return json.dumps(_provenance_to_dict(item), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _provenance_to_dict(item: Provenance) -> dict[str, object]:
    return {
        "source_kind": item.source_kind,
        "source_id": item.source_id,
        "source_element_id": item.source_element_id,
        "page": item.page,
        "method": item.method,
        "confidence": item.confidence,
        "attributes": item.attributes,
    }
