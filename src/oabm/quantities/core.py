"""Deterministic takeoff quantities derived only from canonical model semantics.

This module deliberately does not measure drawings, infer routes, or duplicate the
canonical model.  It consumes validated ``BuildingModel`` objects and emits a small
immutable derived report suitable for estimating adapters.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Mapping

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Entity,
    Polygon3D,
    Polyline3D,
    Provenance,
    validate_model,
)

LENGTH_UNIT = "m"
AREA_UNIT = "m2"
VOLUME_UNIT = "m3"
COUNT_UNIT = "ea"

AssemblyResolver = Callable[[str, Entity], str | None]

#: Key a ``Provenance.attributes`` record uses to say that its claim covers one
#: named dimension of the entity rather than the entity as a whole.  The value
#: is the entity field name, for example ``"thickness_m"``.
ASSUMED_DIMENSION_KEY = "assumed_dimension"

#: Precedence when several provenance records speak for one derived quantity.
#: ``None`` is *unstated*, and it deliberately outranks ``user`` and
#: ``observed``: a record that never said how it came to exist is not a claim of
#: observation, and reading it as one is the failure that biases toward calling
#: a guess a measurement.
_DERIVATION_RANK: Mapping[str | None, int] = {
    DERIVATION_INFERRED: 0,
    None: 1,
    DERIVATION_USER: 2,
    DERIVATION_OBSERVED: 3,
}

#: Every canonical collection, as ``(kind, BuildingModel attribute)``.
_MODEL_KINDS: tuple[tuple[str, str], ...] = (
    ("ceiling", "ceilings"),
    ("circuit", "circuits"),
    ("conductor", "conductors"),
    ("electrical_device", "electrical_devices"),
    ("electrical_equipment", "electrical_equipment"),
    ("level", "levels"),
    ("obstacle", "obstacles"),
    ("opening", "openings"),
    ("port", "ports"),
    ("route", "routes"),
    ("route_constraint", "route_constraints"),
    ("route_fitting", "route_fittings"),
    ("slab", "slabs"),
    ("space", "spaces"),
    ("wall", "walls"),
)

#: Which derived categories each measurable kind can produce.  This is what
#: lets the scope report say a kind was present *and* actually measured, rather
#: than merely measurable in principle.
_CATEGORIES_BY_KIND: Mapping[str, frozenset[str]] = {
    "ceiling": frozenset({"ceiling_area"}),
    "conductor": frozenset({"conductor_length"}),
    "electrical_device": frozenset({"device"}),
    "electrical_equipment": frozenset({"equipment"}),
    "opening": frozenset({"opening_count"}),
    "route": frozenset({"route_length"}),
    "route_fitting": frozenset({"fitting"}),
    "slab": frozenset({"slab_area", "slab_volume"}),
    "space": frozenset({"space_floor_area", "space_volume"}),
    "wall": frozenset({"wall_length", "wall_face_area", "wall_volume"}),
}

MEASURABLE_KINDS: tuple[str, ...] = tuple(sorted(_CATEGORIES_BY_KIND))

#: Kinds this lane deliberately does not turn into a line of their own.  Each
#: carries a reason, because "absent from the report" is not an answer a caller
#: can act on.
_NOT_A_QUANTITY: Mapping[str, str] = {
    "level": (
        "a level is an organizing datum with no installed extent of its own; the "
        "slabs, walls and spaces assigned to it carry the measurable work"
    ),
    "port": (
        "a port is a dimensionless connection point; the routes and conductors "
        "joined at it carry the length"
    ),
    "obstacle": (
        "an obstacle describes space to keep clear when routing, not installed work"
    ),
    "route_constraint": (
        "a route constraint is a rule applied while routing, not installed work"
    ),
    "circuit": (
        "a circuit is measured through its conductors and routes, not as a line of "
        "its own; counting it again here would double-count that same work"
    ),
}

_UNCLASSIFIED_KINDS = tuple(
    kind
    for kind, _ in _MODEL_KINDS
    if kind not in _CATEGORIES_BY_KIND and kind not in _NOT_A_QUANTITY
)
if _UNCLASSIFIED_KINDS:  # pragma: no cover - guards a future entity kind
    raise RuntimeError(
        "quantities scope table does not decide whether these kinds are measured: "
        f"{_UNCLASSIFIED_KINDS!r}"
    )


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
class ScopeReason:
    """One named thing this lane did not do, and why."""

    subject: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"subject": self.subject, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class TakeoffScope:
    """What this lane can measure, what it was handed, and what it declined.

    A caller must be able to tell "this model has nothing to measure" from
    "this lane does not measure what you gave it".  An empty ``items`` tuple
    cannot say which, so the answer is stated here instead of being inferred
    from silence.
    """

    measurable_kinds: tuple[str, ...] = ()
    present_kinds: tuple[str, ...] = ()
    measured_kinds: tuple[str, ...] = ()
    unmeasured_kinds: tuple[ScopeReason, ...] = ()
    declined_derivations: tuple[ScopeReason, ...] = ()
    empty_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "measurable_kinds": list(self.measurable_kinds),
            "present_kinds": list(self.present_kinds),
            "measured_kinds": list(self.measured_kinds),
            "unmeasured_kinds": [item.to_dict() for item in self.unmeasured_kinds],
            "declined_derivations": [
                item.to_dict() for item in self.declined_derivations
            ],
            "empty_reason": self.empty_reason,
        }


#: Stated once, because the answer is a property of the contract and not of any
#: particular model.
_OPENING_DEDUCTION_DECLINED = ScopeReason(
    subject="opening_area_deducted_from_host_wall",
    reason=(
        "not performed: the canonical contract does not state which two of "
        "Opening.size x/y/z form the in-wall face, nor the frame Size3 is "
        "expressed in. The RoomPlan and PDF importers and the opening schedule "
        "all happen to treat x as width and z as height, but convergent "
        "convention among today's producers is not a rule a conforming importer "
        "must follow. Deducting on an unstated convention would silently shrink "
        "a measured wall area, so openings are counted and no area is deducted."
    ),
)


@dataclass(frozen=True, slots=True)
class QuantityItem:
    """One aggregated, deterministic takeoff line.

    ``variant`` carries only canonical fields needed to keep materially distinct
    items separate (for example conduit diameter, fitting angle, or conductor
    size).  ``source_entity_ids`` and ``provenance`` retain traceability to the
    canonical inputs that produced the line.

    ``derivation`` classifies *this quantity*, not the entity it came from.  A
    provenance record that names an assumed dimension reaches only the lines
    whose arithmetic consumed that dimension, so a wall with an assumed
    thickness reports a measured face area and an inferred volume.  ``None``
    means unstated and must never be read as observed.
    """

    category: str
    item_type: str
    quantity: float
    unit: str
    variant: tuple[tuple[str, object], ...]
    source_entity_ids: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    confidence: float
    derivation: str | None = None
    assembly_key: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "item_type": self.item_type,
            "quantity": self.quantity,
            "unit": self.unit,
            "variant": {key: value for key, value in self.variant},
            "source_entity_ids": list(self.source_entity_ids),
            "provenance": [asdict(item) for item in self.provenance],
            "confidence": self.confidence,
            "derivation": self.derivation,
            "assembly_key": self.assembly_key,
        }


@dataclass(frozen=True, slots=True)
class TakeoffReport:
    model_id: str
    items: tuple[QuantityItem, ...]
    warnings: tuple[QuantityWarning, ...] = ()
    scope: TakeoffScope = field(default_factory=TakeoffScope)
    length_unit: str = LENGTH_UNIT
    area_unit: str = AREA_UNIT
    volume_unit: str = VOLUME_UNIT
    count_unit: str = COUNT_UNIT

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "length_unit": self.length_unit,
            "area_unit": self.area_unit,
            "volume_unit": self.volume_unit,
            "count_unit": self.count_unit,
            "scope": self.scope.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


#: One canonical entity paired with the entity fields a quantity's arithmetic
#: actually consumed.  Membership of ``variant`` is deliberately *not*
#: consumption: a wall length line is grouped by thickness so materially
#: different walls stay apart, but the length arithmetic never multiplied a
#: thickness in, so an assumed thickness does not make that length a guess.
_Uses = tuple[Entity, frozenset[str]]


def _uses(entity: Entity, *dimensions: str) -> _Uses:
    return (entity, frozenset(dimensions))


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
    derivation: str | None
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

    Architectural quantities come from canonical semantics only: wall
    centerlines and dimensions, slab and ceiling and space footprints, and
    opening types.  Nothing here remeasures a drawing or infers geometry.

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
                quantity=_polyline_length(route.centerline),
                unit=LENGTH_UNIT,
                variant=(("nominal_diameter_m", route.nominal_diameter_m),),
                uses=(_uses(route, "centerline"),),
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
                uses=(_uses(fitting),),
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
                    quantity=_polyline_length(route.centerline) * conductor.count,
                    unit=LENGTH_UNIT,
                    variant=(
                        ("material", conductor.material),
                        ("size", conductor.size),
                        ("insulation", conductor.insulation),
                    ),
                    uses=(_uses(conductor, "count"), _uses(route, "centerline")),
                    assembly_resolver=assembly_resolver,
                    assembly_entity=conductor,
                )
            )

    for device in sorted(model.electrical_devices, key=lambda item: item.id):
        contributions.append(_countable_entity_contribution("device", device, assembly_resolver))

    for equipment in sorted(model.electrical_equipment, key=lambda item: item.id):
        contributions.append(_countable_entity_contribution("equipment", equipment, assembly_resolver))

    contributions.extend(_architectural_contributions(model, assembly_resolver))

    items = _aggregate(contributions)
    scope = _scope(model, items)
    warnings.extend(_scope_warnings(model, scope))

    return TakeoffReport(
        model_id=model.model_id,
        items=items,
        warnings=tuple(sorted(warnings, key=lambda item: (item.code, item.source_entity_ids))),
        scope=scope,
    )


def _architectural_contributions(
    model: BuildingModel,
    assembly_resolver: AssemblyResolver | None,
) -> list[_Contribution]:
    """Wall, slab, ceiling, space and opening quantities from canonical fields.

    Each line names the entity fields its arithmetic consumed so that provenance
    can be classified per claim rather than per entity.
    """

    contributions: list[_Contribution] = []

    for wall in sorted(model.walls, key=lambda item: item.id):
        length_m = _polyline_length(wall.centerline)
        # Thickness and height keep materially different walls on separate
        # lines even where they took no part in the arithmetic.
        variant = (("thickness_m", wall.thickness_m), ("height_m", wall.height_m))
        contributions.append(
            _contribution(
                category="wall_length",
                item_type="wall",
                quantity=length_m,
                unit=LENGTH_UNIT,
                variant=variant,
                uses=(_uses(wall, "centerline"),),
                assembly_resolver=assembly_resolver,
            )
        )
        contributions.append(
            _contribution(
                category="wall_face_area",
                item_type="wall",
                quantity=length_m * wall.height_m,
                unit=AREA_UNIT,
                # One face. A caller wanting both sides doubles this; emitting
                # both faces as separate lines would be summed into a double
                # count by anyone who did not read the variant.
                variant=variant + (("faces", "single"),),
                uses=(_uses(wall, "centerline", "height_m"),),
                assembly_resolver=assembly_resolver,
            )
        )
        contributions.append(
            _contribution(
                category="wall_volume",
                item_type="wall",
                quantity=length_m * wall.height_m * wall.thickness_m,
                unit=VOLUME_UNIT,
                variant=variant,
                uses=(_uses(wall, "centerline", "height_m", "thickness_m"),),
                assembly_resolver=assembly_resolver,
            )
        )

    for slab in sorted(model.slabs, key=lambda item: item.id):
        area_m2 = _polygon_area(slab.footprint)
        variant = (("thickness_m", slab.thickness_m),)
        contributions.append(
            _contribution(
                category="slab_area",
                item_type="slab",
                quantity=area_m2,
                unit=AREA_UNIT,
                variant=variant,
                uses=(_uses(slab, "footprint"),),
                assembly_resolver=assembly_resolver,
            )
        )
        contributions.append(
            _contribution(
                category="slab_volume",
                item_type="slab",
                quantity=area_m2 * slab.thickness_m,
                unit=VOLUME_UNIT,
                variant=variant,
                uses=(_uses(slab, "footprint", "thickness_m"),),
                assembly_resolver=assembly_resolver,
            )
        )

    for ceiling in sorted(model.ceilings, key=lambda item: item.id):
        contributions.append(
            _contribution(
                category="ceiling_area",
                item_type="ceiling",
                quantity=_polygon_area(ceiling.footprint),
                unit=AREA_UNIT,
                variant=(("thickness_m", ceiling.thickness_m),),
                uses=(_uses(ceiling, "footprint"),),
                assembly_resolver=assembly_resolver,
            )
        )

    for space in sorted(model.spaces, key=lambda item: item.id):
        area_m2 = _polygon_area(space.footprint)
        variant = (("usage", space.usage), ("height_m", space.height_m))
        contributions.append(
            _contribution(
                category="space_floor_area",
                item_type=space.usage or "space",
                quantity=area_m2,
                unit=AREA_UNIT,
                variant=variant,
                uses=(_uses(space, "footprint"),),
                assembly_resolver=assembly_resolver,
            )
        )
        if space.height_m is not None:
            contributions.append(
                _contribution(
                    category="space_volume",
                    item_type=space.usage or "space",
                    quantity=area_m2 * space.height_m,
                    unit=VOLUME_UNIT,
                    variant=variant,
                    uses=(_uses(space, "footprint", "height_m"),),
                    assembly_resolver=assembly_resolver,
                )
            )

    for opening in sorted(model.openings, key=lambda item: item.id):
        contributions.append(
            _contribution(
                category="opening_count",
                item_type=opening.opening_type,
                quantity=1.0,
                unit=COUNT_UNIT,
                variant=(),
                # A count consumes no dimension: it asserts only that the
                # opening exists, so a record assuming one of its dimensions
                # does not reach this line.
                uses=(_uses(opening),),
                assembly_resolver=assembly_resolver,
            )
        )

    return contributions


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
        uses=(_uses(entity),),
        assembly_resolver=assembly_resolver,
    )


def _contribution(
    *,
    category: str,
    item_type: str,
    quantity: float,
    unit: str,
    variant: tuple[tuple[str, object], ...],
    uses: tuple[_Uses, ...],
    assembly_resolver: AssemblyResolver | None,
    assembly_entity: Entity | None = None,
) -> _Contribution:
    if not math.isfinite(quantity) or quantity < 0:
        raise QuantityError(f"non-finite or negative quantity for {category}:{item_type}")
    entities = tuple(entity for entity, _ in uses)
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
        derivation=_classify_derivation(uses),
        assembly_key=assembly_key,
    )


def _record_speaks_for(record: Provenance, consumed: frozenset[str]) -> bool:
    """Whether ``record`` has anything to say about a quantity using ``consumed``.

    A record naming an ``assumed_dimension`` is a claim about that one dimension
    and reaches only quantities whose arithmetic multiplied it in.  A record
    naming none speaks for the entity as a whole and reaches everything derived
    from it.
    """

    attributes = record.attributes
    if not isinstance(attributes, Mapping) or ASSUMED_DIMENSION_KEY not in attributes:
        return True
    assumed = attributes[ASSUMED_DIMENSION_KEY]
    if isinstance(assumed, str):
        return assumed in consumed
    if isinstance(assumed, (list, tuple)):
        return any(isinstance(name, str) and name in consumed for name in assumed)
    # A claim we cannot read is not a licence to discard it.
    return True


def _classify_derivation(uses: tuple[_Uses, ...]) -> str | None:
    """Classify a quantity from the dimensions it actually consumed.

    Tainting every line of an entity because the entity carries one inferred
    record would report a measured wall area as a guess.  Only the records that
    speak for the consumed dimensions are considered.
    """

    relevant = [
        record
        for entity, consumed in uses
        for record in entity.provenance
        if _record_speaks_for(record, consumed)
    ]
    # Nothing spoke for this quantity, so nothing licenses calling it observed.
    return _worst_derivation(record.derivation for record in relevant)


def _worst_derivation(values: Iterable[str | None]) -> str | None:
    ranked = list(values)
    if not ranked:
        return None
    return min(ranked, key=lambda value: _DERIVATION_RANK.get(value, 1))


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
                derivation=_worst_derivation(part.derivation for part in parts),
            )
        )
    return tuple(items)


def _scope(model: BuildingModel, items: tuple[QuantityItem, ...]) -> TakeoffScope:
    present = tuple(kind for kind, attribute in _MODEL_KINDS if getattr(model, attribute))
    produced = {item.category for item in items}
    measured = tuple(
        kind
        for kind in present
        if kind in _CATEGORIES_BY_KIND and (_CATEGORIES_BY_KIND[kind] & produced)
    )
    measured_set = set(measured)

    unmeasured: list[ScopeReason] = []
    for kind in present:
        if kind in measured_set:
            continue
        if kind in _NOT_A_QUANTITY:
            unmeasured.append(ScopeReason(subject=kind, reason=_NOT_A_QUANTITY[kind]))
        else:
            unmeasured.append(
                ScopeReason(
                    subject=kind,
                    reason=(
                        "this lane measures this kind, but every entity of it was "
                        "skipped; see warnings for the entity-level reason"
                    ),
                )
            )

    declined: list[ScopeReason] = []
    if model.openings:
        declined.append(_OPENING_DEDUCTION_DECLINED)

    return TakeoffScope(
        measurable_kinds=MEASURABLE_KINDS,
        present_kinds=present,
        measured_kinds=measured,
        unmeasured_kinds=tuple(unmeasured),
        declined_derivations=tuple(declined),
        empty_reason=_empty_reason(present, items),
    )


def _empty_reason(present: tuple[str, ...], items: tuple[QuantityItem, ...]) -> str | None:
    if items:
        return None
    if not present:
        return "the model contains no entities, so there is nothing to measure"
    measurable_present = [kind for kind in present if kind in _CATEGORIES_BY_KIND]
    if not measurable_present:
        return (
            "this lane does not measure any kind present in the model; it was given "
            f"{', '.join(present)} and it measures {', '.join(MEASURABLE_KINDS)}"
        )
    return (
        "the model contains kinds this lane measures ("
        f"{', '.join(measurable_present)}) but every entity of them was skipped; "
        "see warnings for the entity-level reason"
    )


def _scope_warnings(model: BuildingModel, scope: TakeoffScope) -> list[QuantityWarning]:
    """Never return an empty report without saying why it is empty.

    Kinds this lane deliberately does not measure carry their reason in the
    scope statement rather than a warning: they are present in virtually every
    well-formed model, and warning on them every time would train callers to
    ignore the warning channel that real problems use.
    """

    warnings: list[QuantityWarning] = []
    attribute_by_kind = {kind: attribute for kind, attribute in _MODEL_KINDS}
    for entry in scope.unmeasured_kinds:
        if entry.subject in _NOT_A_QUANTITY:
            continue
        entities = getattr(model, attribute_by_kind[entry.subject])
        warnings.append(
            QuantityWarning(
                code="kind_not_measured",
                message=f"{entry.subject}: {entry.reason}",
                source_entity_ids=tuple(sorted(entity.id for entity in entities)),
            )
        )
    if scope.empty_reason is not None:
        warnings.append(
            QuantityWarning(
                code="empty_takeoff",
                message=scope.empty_reason,
                source_entity_ids=(),
            )
        )
    return warnings


def _polyline_length(line: Polyline3D) -> float:
    points = line.points
    return math.fsum(
        math.sqrt((end.x - start.x) ** 2 + (end.y - start.y) ** 2 + (end.z - start.z) ** 2)
        for start, end in zip(points, points[1:])
    )


def _polygon_area(polygon: Polygon3D) -> float:
    """Area of a planar polygon in 3D, in any orientation (Newell's method).

    Projecting onto the XY plane would under-report every sloped footprint and
    silently report zero for a vertical one.  Newell's method takes the
    magnitude of the summed edge cross products instead, so it is correct for
    any plane and needs no assumption about which way is up.  ``Polygon3D``
    closure is implicit, so the last edge wraps to the first point.
    """

    points = polygon.points
    edges = tuple(zip(points, points[1:] + points[:1]))
    normal_x = math.fsum((a.y - b.y) * (a.z + b.z) for a, b in edges)
    normal_y = math.fsum((a.z - b.z) * (a.x + b.x) for a, b in edges)
    normal_z = math.fsum((a.x - b.x) * (a.y + b.y) for a, b in edges)
    return 0.5 * math.sqrt(normal_x**2 + normal_y**2 + normal_z**2)


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
