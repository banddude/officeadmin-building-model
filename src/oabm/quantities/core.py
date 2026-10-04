"""Deterministic takeoff quantities derived only from canonical model semantics.

This module deliberately does not measure drawings, infer routes, or duplicate the
canonical model.  It consumes validated ``BuildingModel`` objects and emits a small
immutable derived report suitable for estimating adapters.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from typing import Callable, Iterable, Mapping, get_args, get_origin, get_type_hints

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
    provenance_applies_to,
)
from oabm.routing.overlap import RouteOverlap, find_overlapping_route_runs

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
    #: Caller-supplied bid group this line belongs to (for example
    #: ``"ALTERNATES"``); ``None`` is the ungrouped base.  Set only when the
    #: caller supplied ``groups`` to :func:`extract_quantities`.
    group: str | None = None
    quantity_provenance: tuple[Provenance, ...] | None = None

    def to_dict(self) -> dict[str, object]:
        design_status, geometry_status, placement_status = _provenance_statuses(self.provenance if self.quantity_provenance is None else self.quantity_provenance)
        item: dict[str, object] = {
            "category": self.category,
            "item_type": self.item_type,
            "quantity": self.quantity,
            "unit": self.unit,
            "variant": {key: value for key, value in self.variant},
            "source_entity_ids": list(self.source_entity_ids),
            "provenance": [asdict(entry) for entry in self.provenance],
            "design_status": design_status,
            "geometry_status": geometry_status,
            "placement_status": placement_status,
            "confidence": self.confidence,
            "assembly_key": self.assembly_key,
        }
        if self.quantity_provenance is not None:
            item["quantity_provenance"] = [asdict(entry) for entry in self.quantity_provenance]
            item["quantity_derivation"] = _quantity_derivation(self.quantity_provenance)
        # Emitted only for grouped lines, so the default output stays
        # byte-identical to the plain takeoff.
        if self.group is not None:
            item["group"] = self.group
        return item


@dataclass(frozen=True, slots=True)
class TakeoffReport:
    model_id: str
    items: tuple[QuantityItem, ...]
    warnings: tuple[QuantityWarning, ...] = ()
    length_unit: str = LENGTH_UNIT
    count_unit: str = COUNT_UNIT
    #: Sorted ``(name, matched_entity_count, item_line_count)`` triples, one
    #: per caller-supplied group; ``None`` when no groups were supplied.
    group_summary: tuple[tuple[str, int, int], ...] | None = None
    #: Distinct group ids that matched no quantity-bearing entity.  Always 0
    #: when no groups were supplied.
    unmatched_group_ids: int = 0

    def to_dict(self) -> dict[str, object]:
        report: dict[str, object] = {
            "model_id": self.model_id,
            "length_unit": self.length_unit,
            "count_unit": self.count_unit,
            "items": [item.to_dict() for item in self.items],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }
        # Emitted only when groups were supplied, so the default output stays
        # byte-identical to the plain takeoff.
        if self.group_summary is not None:
            report["groups"] = {
                name: {"entities": entity_count, "items": item_count}
                for name, entity_count, item_count in self.group_summary
            }
            report["unmatched_group_ids"] = self.unmatched_group_ids
        return report

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
    group: str | None
    quantity_provenance: tuple[Provenance, ...] | None = None


def extract_quantities(
    model: BuildingModel,
    *,
    assembly_resolver: AssemblyResolver | None = None,
    groups: Mapping[str, Iterable[str]] | None = None,
) -> TakeoffReport:
    """Derive deterministic install quantities from canonical semantic objects.

    Architectural geometry and unmeasured-entity coverage are described in
    ``docs/quantities.md``. Gross quantities are not a complete assembly takeoff.

    Route centerlines are the only source of electrical path length. Fittings are counted
    from canonical ``RouteFitting`` entities, not reconstructed from geometry.
    Conductors are length-extended only over their explicit ``route_ids`` and
    multiplied by ``count``.  Devices and equipment are each-counted directly.

    An optional ``assembly_resolver`` may map a derived category + canonical
    entity to a downstream assembly key without storing estimating semantics in
    the canonical model.

    An optional ``groups`` mapping splits the takeoff lines by caller-supplied
    bid groups (for example ``{"ALTERNATES": [...]}``): group name to the
    canonical entity ids in it — devices, equipment, routes, route fittings,
    conductors.  The caller decides membership; quantities never merge a group
    into the base bid.  Devices, equipment and routes belong to the group that
    names them.  A route fitting belongs to the group that names it, otherwise
    to its route's group.  A conductor contribution over one route belongs to
    the group that names the conductor, otherwise to that route's group, so a
    conductor over routes in different groups is split per route contribution.
    Anything unlisted stays in the ungrouped base (``group=None``), and group
    is part of the aggregation key, so the same item type in the base and in a
    group yields separate lines — base lines first, then groups sorted by
    name.  An id may belong to at most one group (``ValueError`` otherwise);
    ids that match nothing are ignored and counted in the report.  Without
    ``groups`` the report is byte-identical to the plain takeoff.
    """

    if not isinstance(model, BuildingModel):
        raise TypeError("model must be a BuildingModel")

    # Revalidate at the handoff boundary.  This catches missing references even
    # when callers construct or deserialize models outside this workstream.
    validate_model(model)
    _validate_no_duplicate_references(model)
    collections = _canonical_entity_collections(model)

    group_owner, supplied_group_names = _group_ownership(groups)
    groupable_ids = {
        *(route.id for route in model.routes),
        *(fitting.id for fitting in model.route_fittings),
        *(conductor.id for conductor in model.conductors),
        *(device.id for device in model.electrical_devices),
        *(equipment.id for equipment in model.electrical_equipment),
        *(entity.id for name in _ARCHITECTURAL for entity in collections[name]),
    }
    unmatched_group_ids = sum(
        1 for member_id in group_owner if member_id not in groupable_ids
    )

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
                group=group_owner.get(route.id),
            )
        )

    for fitting in sorted(model.route_fittings, key=lambda item: item.id):
        if fitting.attributes.get("consolidation", {}).get("nonmaterial") is True:
            continue  # a route-split port owner, not a physical fitting
        # A fitting inherits its route's group unless the caller named the
        # fitting itself; ``route_id`` is reference-checked above.
        fitting_group = group_owner.get(fitting.id)
        if fitting_group is None:
            fitting_group = group_owner.get(fitting.route_id)
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
                group=fitting_group,
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
        conductor_group = group_owner.get(conductor.id)
        for route_id in conductor.route_ids:
            route = route_by_id[route_id]
            group = (
                conductor_group
                if conductor_group is not None
                else group_owner.get(route_id)
            )
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
                    group=group,
                )
            )

    for device in sorted(model.electrical_devices, key=lambda item: item.id):
        contributions.append(
            _countable_entity_contribution(
                "device", device, assembly_resolver, group_owner.get(device.id)
            )
        )

    for equipment in sorted(model.electrical_equipment, key=lambda item: item.id):
        contributions.append(
            _countable_entity_contribution(
                "equipment", equipment, assembly_resolver, group_owner.get(equipment.id)
            )
        )

    from .architecture import architectural_claims
    for claim in architectural_claims(model):
        if claim.warning is not None:
            warnings.append(QuantityWarning(claim.warning, claim.message, (claim.entity.id,)))
            continue
        contributions.append(_contribution(
            category=claim.category, item_type=claim.item_type, quantity=claim.quantity,
            unit=claim.unit, variant=claim.variant, entities=(claim.entity,),
            assembly_resolver=assembly_resolver, group=group_owner.get(claim.entity.id),
            consumed_paths=claim.consumed_paths,
        ))
    measured = {identity for item in contributions for identity in item.source_entity_ids}
    for name, entities in collections.items():
        missing = tuple(sorted(entity.id for entity in entities if entity.id not in measured))
        if missing:
            warnings.append(QuantityWarning("unmeasured_entities",
                f"{name}: {len(missing)} canonical entities have no quantity line; this is not a zero quantity.", missing))
    if not contributions:
        warnings.append(QuantityWarning("empty_takeoff",
            "The model contains no entities." if not any(collections.values()) else
            "The model contains entities, but none produced a measurable quantity.", ()))
    items = _aggregate(contributions)
    group_summary = _group_summary(
        supplied_group_names, group_owner, groupable_ids, items
    )
    warnings.extend(_overlap_warnings(model))
    return TakeoffReport(
        model_id=model.model_id,
        items=items,
        warnings=tuple(sorted(warnings, key=lambda item: (item.code, item.source_entity_ids))),
        group_summary=group_summary,
        unmatched_group_ids=unmatched_group_ids,
    )


def _overlap_warnings(model: BuildingModel) -> list[QuantityWarning]:
    """One warning per route type whose runs overlap geometrically.

    Bundled per-circuit routes ride the same trunk until consolidation
    lands, and a per-route length takeoff then over-reports conduit on the
    shared stretches. Quantities themselves stay untouched: the warning only
    says by how much they could be double-counted.
    """

    runs = find_overlapping_route_runs(model)
    by_type: dict[str, list[RouteOverlap]] = {}
    for run in runs:
        by_type.setdefault(run.route_type, []).append(run)

    result: list[QuantityWarning] = []
    for route_type in sorted(by_type):
        type_runs = by_type[route_type]
        shared_total = math.fsum(run.shared_length_m for run in type_runs)
        double_counted = math.fsum(run.double_counted_length_m for run in type_runs)
        members = tuple(sorted({
            route_id for run in type_runs for route_id in run.route_ids
        }))
        result.append(QuantityWarning(
            code="overlapping_route_runs",
            message=(
                f"{len(type_runs)} overlapping {route_type} run(s) share "
                f"{shared_total:.2f} m; per-route lengths count "
                f"{double_counted:.2f} m more conduit than the shared runs occupy"
            ),
            source_entity_ids=members,
        ))
    return result


def _group_ownership(
    groups: Mapping[str, Iterable[str]] | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Normalize caller-supplied groups into ``{entity id: group name}``.

    Returns the ownership map and the sorted group names.  The caller decides
    membership; an id claimed by two groups is refused, and repeated ids
    within one group collapse.
    """

    if not groups:
        return {}, ()
    names: list[str] = []
    for name in groups:
        if not isinstance(name, str) or not name:
            raise ValueError("group names must be non-empty strings")
        names.append(name)
    sorted_names = tuple(sorted(names))
    owner_of: dict[str, str] = {}
    for name in sorted_names:
        for member_id in sorted(frozenset(groups[name])):
            previous = owner_of.get(member_id)
            if previous is not None:
                raise ValueError(
                    f"canonical id {member_id!r} belongs to both group "
                    f"{previous!r} and group {name!r}; an id may belong to "
                    "at most one group"
                )
            owner_of[member_id] = name
    return owner_of, sorted_names


def _group_summary(
    supplied_group_names: tuple[str, ...],
    group_owner: Mapping[str, str],
    groupable_ids: frozenset[str] | set[str],
    items: tuple[QuantityItem, ...],
) -> tuple[tuple[str, int, int], ...] | None:
    """Per-group ``(name, matched entities, item lines)``, or ``None``."""

    if not supplied_group_names:
        return None
    entity_counts: dict[str, int] = {}
    for member_id, name in group_owner.items():
        if member_id in groupable_ids:
            entity_counts[name] = entity_counts.get(name, 0) + 1
    item_counts: dict[str, int] = {}
    for item in items:
        if item.group is not None:
            item_counts[item.group] = item_counts.get(item.group, 0) + 1
    return tuple(
        (name, entity_counts.get(name, 0), item_counts.get(name, 0))
        for name in supplied_group_names
    )


def _countable_entity_contribution(
    category: str,
    entity: ElectricalDevice | ElectricalEquipment,
    assembly_resolver: AssemblyResolver | None,
    group: str | None,
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
        group=group,
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
    group: str | None = None,
    consumed_paths: tuple[str, ...] | None = None,
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
        group=group,
        quantity_provenance=None if consumed_paths is None else _merge_provenance(
            (record for entity in entities for record in entity.provenance
             if provenance_applies_to(record, consumed_paths))),
    )


def _aggregate(contributions: Iterable[_Contribution]) -> tuple[QuantityItem, ...]:
    grouped: dict[
        tuple[str | None, str, str, str, str, str | None, str, str | None, str | None, str | None],
        list[_Contribution],
    ] = {}
    for contribution in contributions:
        variant_key = json.dumps(
            {key: value for key, value in contribution.variant},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # Group leads the key: the same item type in the base and in a
        # caller-supplied group must stay two separate lines.
        key = (
            contribution.group,
            contribution.category,
            contribution.item_type,
            contribution.unit,
            variant_key,
            contribution.assembly_key,
            *_provenance_statuses(contribution.provenance if contribution.quantity_provenance is None else contribution.quantity_provenance),
            None if contribution.quantity_provenance is None else _quantity_derivation(contribution.quantity_provenance),
        )
        grouped.setdefault(key, []).append(contribution)

    def _line_order(key: tuple[str | None, ...]) -> tuple[tuple[int, str], tuple[str, ...]]:
        # Base lines first, then groups sorted by name, each in the existing
        # key order.
        head = (1, key[0]) if key[0] is not None else (0, "")
        return head, tuple("" if part is None else str(part) for part in key[1:])

    items: list[QuantityItem] = []
    for key in sorted(grouped, key=_line_order):
        parts = sorted(grouped[key], key=lambda item: item.source_entity_ids)
        source_entity_ids = tuple(sorted({source_id for part in parts for source_id in part.source_entity_ids}))
        provenance = _merge_provenance(*(part.provenance for part in parts))
        items.append(
            QuantityItem(
                category=key[1],
                item_type=key[2],
                unit=key[3],
                variant=parts[0].variant,
                assembly_key=key[5],
                quantity=math.fsum(part.quantity for part in parts),
                source_entity_ids=source_entity_ids,
                provenance=provenance,
                confidence=min(part.confidence for part in parts),
                group=key[0],
                quantity_provenance=None if all(part.quantity_provenance is None for part in parts) else
                    _merge_provenance(*(part.provenance if part.quantity_provenance is None else part.quantity_provenance for part in parts)),
            )
        )
    return tuple(items)


def _provenance_statuses(
    provenance: tuple[Provenance, ...],
) -> tuple[str, str | None, str | None]:
    source_kinds = {item.source_kind for item in provenance}
    derivations = {item.derivation for item in provenance}
    design_status = (
        "user-directed" if "user-override" in source_kinds else
        "system-designed" if "system-design" in source_kinds else
        "inferred" if "inferred" in derivations else
        "user" if "user" in derivations else
        "observed" if derivations == {"observed"} else "unknown"
    )
    geometry_status = "inferred" if "router" in source_kinds else None
    placement_status = (
        "inferred" if "placement-engine" in source_kinds else
        "user" if "user-placement" in source_kinds else None
    )
    return design_status, geometry_status, placement_status


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
    traversals = {
        route.id for route in model.routes
        if "member_route_ids" in route.attributes.get("consolidation", {})
    }
    checks: list[tuple[str, tuple[str, ...]]] = []
    checks.extend((f"{route.id}.fitting_ids", route.fitting_ids) for route in model.routes)
    checks.extend((f"{circuit.id}.load_port_ids", circuit.load_port_ids) for circuit in model.circuits)
    for label, refs in checks:
        if len(refs) != len(set(refs)):
            raise QuantityError(f"{label} contains duplicate references; refusing to double-count")
    for entity in (*model.circuits, *model.conductors):
        refs = entity.route_ids
        duplicated = {route_id for route_id in refs if refs.count(route_id) > 1}
        if duplicated - traversals:
            raise QuantityError(f"{entity.id}.route_ids contains duplicate references; refusing to double-count")


_ARCHITECTURAL = frozenset({"walls", "slabs", "ceilings", "spaces", "openings"})
_CLASSIFIED_COLLECTIONS = _ARCHITECTURAL | frozenset({
    "levels", "electrical_equipment", "electrical_devices", "ports", "obstacles",
    "route_constraints", "routes", "route_fittings", "circuits", "conductors",
})

def _canonical_entity_collections(model: BuildingModel) -> dict[str, tuple[Entity, ...]]:
    """Anchor coverage to the actual model type, never a quantities-only list."""
    hints = get_type_hints(type(model))
    result = {}
    for field in fields(model):
        hint = hints[field.name]
        arguments = get_args(hint)
        if get_origin(hint) is tuple and arguments and isinstance(arguments[0], type) and issubclass(arguments[0], Entity):
            result[field.name] = getattr(model, field.name)
    unknown = set(result) - _CLASSIFIED_COLLECTIONS
    if unknown:
        raise QuantityError(f"Unclassified canonical entity collections: {sorted(unknown)}")
    return result


def _quantity_derivation(records: tuple[Provenance, ...]) -> str:
    """Measurement evidence, separate from who directed a design decision."""
    classes = {record.derivation for record in records}
    if "inferred" in classes:
        return "inferred"
    if not classes or None in classes:
        return "unknown"
    return "user" if "user" in classes else "observed"
