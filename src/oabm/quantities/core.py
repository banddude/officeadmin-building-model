"""Deterministic takeoff quantities derived only from canonical model semantics.

This module deliberately does not measure drawings, infer routes, or duplicate the
canonical model.  It consumes validated ``BuildingModel`` objects and emits a small
immutable derived report suitable for estimating adapters.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

from oabm.model import (
    BuildingModel,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Entity,
    Provenance,
    Route,
    RouteFitting,
    validate_model,
)

LENGTH_UNIT = "m"
COUNT_UNIT = "ea"

AssemblyResolver = Callable[[str, Entity], str | None]


class QuantityError(ValueError):
    """Raised when canonical data is unsafe or ambiguous to quantity."""


@dataclass(frozen=True, slots=True)
class QuantityWarning:
    code: str
    message: str
    source_entity_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "source_entity_ids": list(self.source_entity_ids),
        }


@dataclass(frozen=True, slots=True)
class QuantityItem:
    """One aggregated, deterministic takeoff line.

    ``variant`` carries only canonical fields needed to keep materially distinct
    items separate (for example conduit diameter, fitting angle, or conductor
    size).  ``source_entity_ids`` and ``provenance`` retain traceability to the
    canonical inputs that produced the line.
    """

    category: str
    item_type: str
    quantity: float
    unit: str
    variant: tuple[tuple[str, object], ...]
    source_entity_ids: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    confidence: float
    assembly_key: str | None = None

    def to_dict(self) -> dict[str, object]:
        derivations = {item.derivation for item in self.provenance}
        source_kinds = {item.source_kind for item in self.provenance}
        return {
            "category": self.category,
            "item_type": self.item_type,
            "quantity": self.quantity,
            "unit": self.unit,
            "variant": {key: value for key, value in self.variant},
            "source_entity_ids": list(self.source_entity_ids),
            "provenance": [asdict(item) for item in self.provenance],
            "design_status": (
                "user-directed" if "user-override" in source_kinds else
                "system-designed" if "system-design" in source_kinds else
                "inferred" if "inferred" in derivations else
                "user" if "user" in derivations else
                "observed" if derivations == {"observed"} else "unknown"
            ),
            "geometry_status": (
                "inferred" if "router" in source_kinds else None
            ),
            "placement_status": (
                "inferred" if "placement-engine" in source_kinds else
                "user" if "user-placement" in source_kinds else None
            ),
            "confidence": self.confidence,
            "assembly_key": self.assembly_key,
        }


@dataclass(frozen=True, slots=True)
class TakeoffReport:
    model_id: str
    items: tuple[QuantityItem, ...]
    warnings: tuple[QuantityWarning, ...] = ()
    length_unit: str = LENGTH_UNIT
    count_unit: str = COUNT_UNIT

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "length_unit": self.length_unit,
            "count_unit": self.count_unit,
            "items": [item.to_dict() for item in self.items],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class _Contribution:
    category: str
    item_type: str
    quantity: float
    unit: str
    variant: tuple[tuple[str, object], ...]
    source_entity_ids: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    confidence: float
    assembly_key: str | None


def extract_quantities(
    model: BuildingModel,
    *,
    assembly_resolver: AssemblyResolver | None = None,
) -> TakeoffReport:
    """Derive deterministic install quantities from canonical semantic objects.

    Route centerlines are the only source of path length.  Fittings are counted
    from canonical ``RouteFitting`` entities, not reconstructed from geometry.
    Conductors are length-extended only over their explicit ``route_ids`` and
    multiplied by ``count``.  Devices and equipment are each-counted directly.

    An optional ``assembly_resolver`` may map a derived category + canonical
    entity to a downstream assembly key without storing estimating semantics in
    the canonical model.
    """

    if not isinstance(model, BuildingModel):
        raise TypeError("model must be a BuildingModel")

    # Revalidate at the handoff boundary.  This catches missing references even
    # when callers construct or deserialize models outside this workstream.
    validate_model(model)
    _validate_no_duplicate_references(model)

    route_by_id = {route.id: route for route in model.routes}
    contributions: list[_Contribution] = []
    warnings: list[QuantityWarning] = []

    for route in sorted(model.routes, key=lambda item: item.id):
        contributions.append(
            _contribution(
                category="route_length",
                item_type=route.route_type,
                quantity=_polyline_length(route),
                unit=LENGTH_UNIT,
                variant=(("nominal_diameter_m", route.nominal_diameter_m),),
                entities=(route,),
                assembly_resolver=assembly_resolver,
            )
        )

    for fitting in sorted(model.route_fittings, key=lambda item: item.id):
        contributions.append(
            _contribution(
                category="fitting",
                item_type=fitting.fitting_type,
                quantity=1.0,
                unit=COUNT_UNIT,
                variant=(
                    ("nominal_diameter_m", fitting.nominal_diameter_m),
                    ("angle_radians", fitting.angle_radians),
                ),
                entities=(fitting,),
                assembly_resolver=assembly_resolver,
            )
        )

    for conductor in sorted(model.conductors, key=lambda item: item.id):
        if not conductor.route_ids:
            warnings.append(
                QuantityWarning(
                    code="unrouted_conductor",
                    message=(
                        f"{conductor.id} has no explicit route_ids; conductor length was not inferred"
                    ),
                    source_entity_ids=(conductor.id,),
                )
            )
            continue
        for route_id in conductor.route_ids:
            route = route_by_id[route_id]
            contributions.append(
                _contribution(
                    category="conductor_length",
                    item_type=conductor.role,
                    quantity=_polyline_length(route) * conductor.count,
                    unit=LENGTH_UNIT,
                    variant=(
                        ("material", conductor.material),
                        ("size", conductor.size),
                        ("insulation", conductor.insulation),
                    ),
                    entities=(conductor, route),
                    assembly_resolver=assembly_resolver,
                    assembly_entity=conductor,
                )
            )

    for device in sorted(model.electrical_devices, key=lambda item: item.id):
        contributions.append(_countable_entity_contribution("device", device, assembly_resolver))

    for equipment in sorted(model.electrical_equipment, key=lambda item: item.id):
        contributions.append(_countable_entity_contribution("equipment", equipment, assembly_resolver))

    return TakeoffReport(
        model_id=model.model_id,
        items=_aggregate(contributions),
        warnings=tuple(sorted(warnings, key=lambda item: (item.code, item.source_entity_ids))),
    )


def _countable_entity_contribution(
    category: str,
    entity: ElectricalDevice | ElectricalEquipment,
    assembly_resolver: AssemblyResolver | None,
) -> _Contribution:
    item_type = entity.device_type if isinstance(entity, ElectricalDevice) else entity.equipment_type
    size = entity.size
    return _contribution(
        category=category,
        item_type=item_type,
        quantity=1.0,
        unit=COUNT_UNIT,
        variant=(
            ("system", entity.system),
            ("rated_voltage_v", entity.rated_voltage_v),
            ("size_x_m", size.x if size is not None else None),
            ("size_y_m", size.y if size is not None else None),
            ("size_z_m", size.z if size is not None else None),
        ),
        entities=(entity,),
        assembly_resolver=assembly_resolver,
    )


def _contribution(
    *,
    category: str,
    item_type: str,
    quantity: float,
    unit: str,
    variant: tuple[tuple[str, object], ...],
    entities: tuple[Entity, ...],
    assembly_resolver: AssemblyResolver | None,
    assembly_entity: Entity | None = None,
) -> _Contribution:
    if not math.isfinite(quantity) or quantity < 0:
        raise QuantityError(f"non-finite or negative quantity for {category}:{item_type}")
    resolver_entity = assembly_entity or entities[0]
    assembly_key = assembly_resolver(category, resolver_entity) if assembly_resolver else None
    if assembly_key is not None and (not isinstance(assembly_key, str) or not assembly_key):
        raise QuantityError("assembly_resolver must return a non-empty string or None")
    provenance = _merge_provenance(*(entity.provenance for entity in entities))
    return _Contribution(
        category=category,
        item_type=item_type,
        quantity=float(quantity),
        unit=unit,
        variant=variant,
        source_entity_ids=tuple(sorted({entity.id for entity in entities})),
        provenance=provenance,
        confidence=min(entity.confidence for entity in entities),
        assembly_key=assembly_key,
    )


def _aggregate(contributions: Iterable[_Contribution]) -> tuple[QuantityItem, ...]:
    grouped: dict[tuple[str, str, str, str, str | None], list[_Contribution]] = {}
    for contribution in contributions:
        variant_key = json.dumps(
            {key: value for key, value in contribution.variant},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        key = (
            contribution.category,
            contribution.item_type,
            contribution.unit,
            variant_key,
            contribution.assembly_key,
        )
        grouped.setdefault(key, []).append(contribution)

    items: list[QuantityItem] = []
    for key in sorted(grouped, key=lambda item: tuple("" if part is None else str(part) for part in item)):
        parts = sorted(grouped[key], key=lambda item: item.source_entity_ids)
        source_entity_ids = tuple(sorted({source_id for part in parts for source_id in part.source_entity_ids}))
        provenance = _merge_provenance(*(part.provenance for part in parts))
        items.append(
            QuantityItem(
                category=key[0],
                item_type=key[1],
                unit=key[2],
                variant=parts[0].variant,
                assembly_key=key[4],
                quantity=math.fsum(part.quantity for part in parts),
                source_entity_ids=source_entity_ids,
                provenance=provenance,
                confidence=min(part.confidence for part in parts),
            )
        )
    return tuple(items)


def _polyline_length(route: Route) -> float:
    points = route.centerline.points
    return math.fsum(
        math.sqrt((end.x - start.x) ** 2 + (end.y - start.y) ** 2 + (end.z - start.z) ** 2)
        for start, end in zip(points, points[1:])
    )


def _merge_provenance(*groups: Iterable[Provenance]) -> tuple[Provenance, ...]:
    unique: dict[str, Provenance] = {}
    for group in groups:
        for item in group:
            key = json.dumps(asdict(item), sort_keys=True, separators=(",", ":"), allow_nan=False)
            unique[key] = item
    return tuple(unique[key] for key in sorted(unique))


def _validate_no_duplicate_references(model: BuildingModel) -> None:
    checks: list[tuple[str, tuple[str, ...]]] = []
    checks.extend((f"{route.id}.fitting_ids", route.fitting_ids) for route in model.routes)
    checks.extend((f"{circuit.id}.route_ids", circuit.route_ids) for circuit in model.circuits)
    checks.extend((f"{conductor.id}.route_ids", conductor.route_ids) for conductor in model.conductors)
    checks.extend((f"{circuit.id}.load_port_ids", circuit.load_port_ids) for circuit in model.circuits)
    for label, refs in checks:
        if len(refs) != len(set(refs)):
            raise QuantityError(f"{label} contains duplicate references; refusing to double-count")
