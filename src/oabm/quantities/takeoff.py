"""Deterministic takeoff quantities derived from the canonical model.

This module deliberately consumes only oabm.model objects. It never reads or
measures drawings, PDFs, screenshots, IFC geometry, or router internals.

Canonical Entity.attributes may optionally contain a non-empty assembly_key
string. The key is carried onto derived quantity rows so a future estimating
adapter can map a semantic quantity to an assembly without adding estimating
semantics to the canonical model contract.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable, TypeAlias

from oabm.model import (
    BuildingModel,
    Entity,
    Point3,
    Provenance,
    Route,
    stable_id,
    validate_model,
)

Scalar: TypeAlias = str | int | float | bool
Property: TypeAlias = tuple[str, Scalar]


class QuantityError(ValueError):
    """Raised when quantity extraction cannot preserve deterministic semantics."""


@dataclass(frozen=True, slots=True, kw_only=True)
class QuantityItem:
    """One deterministic, aggregated takeoff row."""

    id: str
    category: str
    measure: str
    item_type: str
    unit: str
    quantity: int | float
    source_ids: tuple[str, ...]
    properties: tuple[Property, ...] = ()
    assembly_key: str | None = None
    provenance: tuple[Provenance, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "category": self.category,
            "measure": self.measure,
            "item_type": self.item_type,
            "unit": self.unit,
            "quantity": self.quantity,
            "source_ids": list(self.source_ids),
            "properties": {key: value for key, value in self.properties},
            "assembly_key": self.assembly_key,
            "provenance": [_provenance_to_dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class QuantityReport:
    """Takeoff report for one canonical model snapshot."""

    model_id: str
    schema_version: str
    length_unit: str
    count_unit: str
    items: tuple[QuantityItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "schema_version": self.schema_version,
            "length_unit": self.length_unit,
            "count_unit": self.count_unit,
            "items": [item.to_dict() for item in self.items],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class _Contribution:
    category: str
    measure: str
    item_type: str
    unit: str
    quantity: int | float
    properties: tuple[Property, ...]
    assembly_key: str | None
    sources: tuple[Entity, ...]

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(sorted({source.id for source in self.sources}))

    @property
    def signature(self) -> tuple[object, ...]:
        return (
            self.category,
            self.measure,
            self.item_type,
            self.unit,
            self.assembly_key,
            self.properties,
        )


def extract_quantities(model: BuildingModel) -> QuantityReport:
    """Derive deterministic install quantities from canonical semantic objects.

    Length is always measured from canonical 3D route centerlines in metres.
    Count quantities use "ea". Conductor length is emitted only when that
    conductor explicitly references canonical routes; no route is inferred from
    drawings or from circuit topology.
    """

    validate_model(model)
    if model.coordinate_system.length_unit != "m":
        raise QuantityError("canonical quantity extraction requires metre geometry")

    routes_by_id = {route.id: route for route in model.routes}
    route_lengths = {route.id: _route_length(route) for route in model.routes}
    contributions: list[_Contribution] = []

    for route in model.routes:
        contributions.append(
            _Contribution(
                category="route",
                measure="length",
                item_type=route.route_type,
                unit="m",
                quantity=route_lengths[route.id],
                properties=_properties(nominal_diameter_m=route.nominal_diameter_m),
                assembly_key=_assembly_key(route),
                sources=(route,),
            )
        )

    for fitting in model.route_fittings:
        contributions.append(
            _Contribution(
                category="fitting",
                measure="count",
                item_type=fitting.fitting_type,
                unit="ea",
                quantity=1,
                properties=_properties(
                    nominal_diameter_m=fitting.nominal_diameter_m,
                    angle_radians=fitting.angle_radians,
                ),
                assembly_key=_assembly_key(fitting),
                sources=(fitting,),
            )
        )

    for conductor in model.conductors:
        conductor_properties = _properties(
            role=conductor.role,
            material=conductor.material,
            size=conductor.size,
            insulation=conductor.insulation,
        )
        assembly_key = _assembly_key(conductor)
        contributions.append(
            _Contribution(
                category="conductor",
                measure="count",
                item_type="conductor",
                unit="ea",
                quantity=conductor.count,
                properties=conductor_properties,
                assembly_key=assembly_key,
                sources=(conductor,),
            )
        )

        # Duplicate route IDs are ignored defensively. JSON Schema v1 already
        # rejects them on serialized input, but callers can construct Python
        # objects directly and quantity extraction should still not double count.
        route_ids = tuple(sorted(set(conductor.route_ids)))
        if route_ids:
            referenced_routes = tuple(routes_by_id[route_id] for route_id in route_ids)
            path_length = math.fsum(route_lengths[route_id] for route_id in route_ids)
            contributions.append(
                _Contribution(
                    category="conductor",
                    measure="length",
                    item_type="conductor",
                    unit="m",
                    quantity=path_length * conductor.count,
                    properties=conductor_properties,
                    assembly_key=assembly_key,
                    sources=(conductor, *referenced_routes),
                )
            )

    for equipment in model.electrical_equipment:
        contributions.append(
            _Contribution(
                category="equipment",
                measure="count",
                item_type=equipment.equipment_type,
                unit="ea",
                quantity=1,
                properties=_properties(
                    system=equipment.system,
                    rated_voltage_v=equipment.rated_voltage_v,
                ),
                assembly_key=_assembly_key(equipment),
                sources=(equipment,),
            )
        )

    for device in model.electrical_devices:
        contributions.append(
            _Contribution(
                category="box" if _is_box_type(device.device_type) else "device",
                measure="count",
                item_type=device.device_type,
                unit="ea",
                quantity=1,
                properties=_properties(
                    system=device.system,
                    rated_voltage_v=device.rated_voltage_v,
                ),
                assembly_key=_assembly_key(device),
                sources=(device,),
            )
        )

    items = _aggregate(model.model_id, contributions)
    return QuantityReport(
        model_id=model.model_id,
        schema_version=model.schema_version,
        length_unit="m",
        count_unit="ea",
        items=items,
    )


def _route_length(route: Route) -> float:
    points = route.centerline.points
    return math.fsum(_point_distance(a, b) for a, b in zip(points, points[1:]))


def _point_distance(a: Point3, b: Point3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _properties(**values: Scalar | None) -> tuple[Property, ...]:
    return tuple(sorted((key, value) for key, value in values.items() if value is not None))


def _assembly_key(entity: Entity) -> str | None:
    value = entity.attributes.get("assembly_key")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise QuantityError(f"{entity.id}.attributes.assembly_key must be a non-empty string")
    return value


def _is_box_type(device_type: str) -> bool:
    token = device_type.strip().lower().replace("_", "-").replace(" ", "-")
    return token == "box" or token.endswith("box") or "-box-" in token or token.endswith("-box")


def _aggregate(model_id: str, contributions: Iterable[_Contribution]) -> tuple[QuantityItem, ...]:
    grouped: dict[tuple[object, ...], list[_Contribution]] = {}
    for contribution in contributions:
        grouped.setdefault(contribution.signature, []).append(contribution)

    items: list[QuantityItem] = []
    for signature, group in grouped.items():
        category, measure, item_type, unit, assembly_key, properties = signature
        ordered = sorted(group, key=lambda item: (item.source_ids, float(item.quantity)))
        total = math.fsum(float(item.quantity) for item in ordered)
        quantity: int | float
        if unit == "ea" and total.is_integer():
            quantity = int(total)
        else:
            quantity = total

        sources = _unique_sources(source for item in ordered for source in item.sources)
        source_ids = tuple(source.id for source in sources)
        provenance = _unique_provenance(
            provenance for source in sources for provenance in source.provenance
        )
        signature_key = json.dumps(
            {
                "model_id": model_id,
                "category": category,
                "measure": measure,
                "item_type": item_type,
                "unit": unit,
                "assembly_key": assembly_key,
                "properties": list(properties),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        items.append(
            QuantityItem(
                id=stable_id("qty", signature_key),
                category=str(category),
                measure=str(measure),
                item_type=str(item_type),
                unit=str(unit),
                quantity=quantity,
                source_ids=source_ids,
                properties=properties,
                assembly_key=assembly_key if isinstance(assembly_key, str) else None,
                provenance=provenance,
            )
        )

    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.category,
                item.measure,
                item.item_type,
                item.assembly_key or "",
                item.properties,
                item.id,
            ),
        )
    )


def _unique_sources(sources: Iterable[Entity]) -> tuple[Entity, ...]:
    by_id: dict[str, Entity] = {}
    for source in sources:
        by_id[source.id] = source
    return tuple(by_id[source_id] for source_id in sorted(by_id))


def _unique_provenance(items: Iterable[Provenance]) -> tuple[Provenance, ...]:
    by_key: dict[str, Provenance] = {}
    for item in items:
        key = json.dumps(_provenance_to_dict(item), sort_keys=True, separators=(",", ":"))
        by_key[key] = item
    return tuple(by_key[key] for key in sorted(by_key))


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
