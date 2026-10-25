"""Deterministic takeoff quantities derived only from canonical model semantics.

This module deliberately does not measure drawings, infer routes, or duplicate the
canonical model.  It consumes validated ``BuildingModel`` objects and emits a small
immutable derived report suitable for estimating adapters.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from typing import Callable, Iterable, Mapping, get_args, get_origin, get_type_hints

from oabm.model import (
    ASSEMBLY_SCHEMA_VERSIONS,
    AssemblyComponent,
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
    RACEWAY_SCHEMA_VERSION,
    SUPPORT_SCHEMA_VERSION,
    FITTING_SCHEMA_VERSION,
    ProfileExtrusion,
    ProfileOutline2D,
)
from oabm.routing.overlap import RouteOverlap, find_overlapping_route_runs

LENGTH_UNIT = "m"
COUNT_UNIT = "ea"

AssemblyResolver = Callable[[str, Entity | AssemblyComponent], str | None]


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
    #: Caller bid group or guarded AI source-scope inventory group.
    #: ``None`` is the ungrouped base. AI reuse/alternates/unknown scope cannot
    #: enter base merely because the caller omitted ``groups``.
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
    #: per caller or AI source-scope group; ``None`` for ungrouped reports.
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
    quantity-bearing record (including a 1.6 assembly component) to a downstream
    assembly key without storing estimating semantics in the canonical model.

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
    ``groups`` ordinary non-AI reports retain their existing bytes.

    Reviewed AI symbols additionally retain work/bid/alternate scope in line
    variants. Only explicit new+base may join ungrouped base. Reuse, removal,
    N.I.C., alternates and unresolved/conflicting facts get identified inventory
    groups, composed with any caller group. Contradictory source metadata fails
    closed. These counts do not select alternates or authorize purchases.
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
        *(box.id for box in model.electrical_boxes),
        *(member.id for member in model.structural_members),
        *(c.id for c in model.support_components),
        *(a.id for a in model.support_assemblies),
        *(a.id for a in model.building_assemblies),
        *(c.id for c in model.assembly_components),
        *(entity.id for name in _ARCHITECTURAL for entity in collections[name]),
    }
    unmatched_group_ids = sum(
        1 for member_id in group_owner if member_id not in groupable_ids
    )

    # Reviewed AI marks carry source scope, not an implicit purchase instruction.
    # Only explicit new+base joins base; every other state has an identified group.
    ai_scopes = {}
    for entity in (*model.electrical_devices, *model.electrical_equipment):
        scope = _ai_symbol_scope(entity)
        if scope is None:
            continue
        ai_scopes[entity.id] = scope
        if scope.work_status != "new" or scope.bid_scope != "base":
            labels = {"work_status": scope.work_status, "bid_scope": scope.bid_scope,
                      "alternate_id": scope.alternate_id}
            if entity.id in group_owner:
                labels["caller_group"] = group_owner[entity.id]
            group_owner[entity.id] = "ai-symbol-scope:" + json.dumps(
                labels, sort_keys=True, separators=(",", ":"))
    supplied_group_names = tuple(sorted(set(supplied_group_names) | set(group_owner.values())))

    route_by_id = {route.id: route for route in model.routes}
    contributions: list[_Contribution] = []
    warnings: list[QuantityWarning] = []
    if ai_scopes:
        warnings.append(QuantityWarning("ai_symbol_scope_inventory",
            "AI symbol counts are source-scoped object inventory. Reuse, removal, N.I.C., "
            "alternate and unresolved groups are not base purchases; no alternate is selected automatically.",
            tuple(sorted(ai_scopes))))

    for route in sorted(model.routes, key=lambda item: item.id):
        selection = next((s for s in model.raceway_selections if s.route_id == route.id), None)
        variant = (("nominal_diameter_m", route.nominal_diameter_m),)
        evidence_entities = (route,)
        if selection is not None:
            variant += (("catalog_id", selection.product.catalog_id),
                        ("catalog_revision", selection.product.catalog_revision),
                        ("product_id", selection.product.product_id),
                        ("designation", selection.designation),
                        ("wiring_method", selection.wiring_method),
                        ("rule_authority", selection.basis.authority),
                        ("rule_revision", selection.basis.revision),
                        ("rule_id", selection.basis.rule_id))
            evidence_entities += (selection,)
            warnings.append(QuantityWarning("raceway_code_unverified",
                "Caller product/rule selection is not verified code sizing; installed run length is counted once.",
                (route.id, selection.id)))
        contributions.append(
            _contribution(
                category="route_length",
                item_type=route.route_type,
                quantity=_polyline_length(route),
                unit=LENGTH_UNIT,
                variant=variant,
                entities=evidence_entities,
                assembly_resolver=assembly_resolver,
                group=group_owner.get(route.id),
                consumed_paths=("centerline",) if model.schema_version in {
                    RACEWAY_SCHEMA_VERSION, SUPPORT_SCHEMA_VERSION, FITTING_SCHEMA_VERSION,
                } else None,
            )
        )

    for outcome in model.raceway_selection_outcomes:
        if outcome.status == "unresolved":
            warnings.append(QuantityWarning("unresolved_raceway_selection",
                f"{outcome.route_id}: {', '.join(outcome.reason_codes)}; route length is not a complete selected-product takeoff.",
                (outcome.id, outcome.route_id)))

    for fitting in sorted(model.route_fittings, key=lambda item: item.id):
        consolidation = fitting.attributes.get("consolidation")
        if isinstance(consolidation, dict) and consolidation.get("nonmaterial") is True:
            continue  # a route-split port owner, not a physical fitting
        # A fitting inherits its route's group unless the caller named the
        # fitting itself; ``route_id`` is reference-checked above.
        fitting_group = group_owner.get(fitting.id)
        if fitting_group is None:
            fitting_group = group_owner.get(fitting.route_id)
        contribution = _contribution(
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
        selection = next((row for row in model.fitting_selections if row.fitting_id == fitting.id), None)
        outcome = next((row for row in model.fitting_selection_outcomes if row.fitting_id == fitting.id), None)
        if selection is not None:
            interfaces = tuple(sorted(
                (row for row in model.fitting_interfaces if row.selection_id == selection.id),
                key=lambda row: (row.interface_key, row.id),
            ))
            pair_facts = tuple(
                (row.interface_key, row.mating_product.catalog_id,
                 row.mating_product.catalog_revision, row.mating_product.product_id,
                 row.mating_interface_key, row.mating_pair_id)
                for row in interfaces
            )
            contribution = replace(
                contribution,
                variant=contribution.variant + (
                    ("catalog_id", selection.product.catalog_id),
                    ("catalog_revision", selection.product.catalog_revision),
                    ("product_id", selection.product.product_id),
                    ("geometry_basis", selection.geometry_basis),
                    ("rule_authority", selection.basis.authority),
                    ("rule_revision", selection.basis.revision),
                    ("rule_id", selection.basis.rule_id),
                    ("mating_pairs", pair_facts),
                ),
                # Keep the physical fitting as the only counted source entity.
                # Product and interface evidence qualifies the line without
                # creating additional countable inventory rows.
                provenance=_merge_provenance(
                    contribution.provenance,
                    selection.provenance,
                    *(row.provenance for row in interfaces),
                ),
                confidence=min(
                    contribution.confidence, selection.confidence,
                    *(row.confidence for row in interfaces),
                ),
            )
        contributions.append(contribution)

    for outcome in model.fitting_selection_outcomes:
        if outcome.status == "unresolved":
            warnings.append(QuantityWarning(
                "unresolved_fitting_selection",
                f"{outcome.fitting_id}: {', '.join(outcome.reason_codes)}; the existing route-fitting object is counted once, but no product compatibility is asserted.",
                (outcome.id, outcome.fitting_id),
            ))

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

    for member in sorted(model.structural_members, key=lambda item: item.id):
        # Confirmed framing members report their known count and their
        # rectangular solid volume. Nothing here designs, prices, or assumes
        # drilling: an unconfirmed member is simply absent.
        material_variant = (("material", member.material),)
        contributions.append(_contribution(
            category="structural_member",
            item_type=member.kind,
            quantity=1.0,
            unit=COUNT_UNIT,
            variant=material_variant,
            entities=(member,),
            assembly_resolver=assembly_resolver,
            group=group_owner.get(member.id),
        ))
        contributions.append(_contribution(
            category="structural_member_volume",
            item_type=member.kind,
            quantity=member.size.x * member.size.y * member.size.z,
            unit="m3",
            variant=material_variant,
            entities=(member,),
            assembly_resolver=assembly_resolver,
            group=group_owner.get(member.id),
        ))

    for box in sorted(model.electrical_boxes, key=lambda item: item.id):
        contributions.append(_contribution(
            category="electrical_box", item_type=box.box_type, quantity=1.0, unit=COUNT_UNIT,
            variant=(("catalog_id", box.product.catalog_id),
                     ("catalog_revision", box.product.catalog_revision),
                     ("product_id", box.product.product_id), ("mounting", box.mounting)),
            entities=(box,), assembly_resolver=assembly_resolver, group=group_owner.get(box.id),
            consumed_paths=("id", "product", "box_type", "mounting"),
        ))
        warnings.append(QuantityWarning("box_fill_unverified",
            "Explicit box product selection is not a code-verified box-fill calculation; listed interior capacity is not exterior volume.",
            (box.id,)))
    for outcome in model.box_selection_outcomes:
        if outcome.status == "unresolved":
            warnings.append(QuantityWarning("unresolved_box_selection",
                f"{outcome.intent_id}: {', '.join(outcome.reason_codes)}; no physical box quantity was invented.",
                (outcome.id,)))

    support_by_id = {a.id:a for a in model.support_assemblies}
    for component in sorted(model.support_components,key=lambda c:c.id):
        assembly=support_by_id[component.assembly_id]
        contribution=_contribution(category="support_component",item_type=component.component_type,
            quantity=1.0,unit=COUNT_UNIT,variant=(("catalog_id",component.product.catalog_id),
                ("catalog_revision",component.product.catalog_revision),("product_id",component.product.product_id),
                ("component_category",component.category),("geometry_basis",component.geometry_basis),
                ("rule_authority",assembly.basis.authority),("rule_revision",assembly.basis.revision),
                ("rule_id",assembly.basis.rule_id)),entities=(component,),assembly_resolver=assembly_resolver,
            group=group_owner.get(component.id) or group_owner.get(component.assembly_id),
            consumed_paths=("part_key","product","category","component_type"))
        contributions.append(replace(contribution,
            provenance=_merge_provenance(component.provenance,assembly.provenance),
            confidence=min(component.confidence,assembly.confidence)))
    for assembly in model.support_assemblies:
        warnings.append(QuantityWarning("support_rules_unverified",
            "Explicit component inventory does not verify spacing, loading or fastener suitability; catalog envelopes are not exact profiles or material volumes.",
            (assembly.id,)))
    for outcome in model.support_selection_outcomes:
        if outcome.status=="unresolved":
            warnings.append(QuantityWarning("unresolved_support_selection",
                f"{outcome.intent_id}: {', '.join(outcome.reason_codes)}; no support hardware was invented.",(outcome.id,)))
    if model.schema_version in {SUPPORT_SCHEMA_VERSION, FITTING_SCHEMA_VERSION} and model.routes and not model.support_selection_outcomes:
        warnings.append(QuantityWarning("support_design_unrequested",
            "No explicit support assembly intents were supplied; route length does not establish support or fastener quantities.",
            tuple(sorted(r.id for r in model.routes))))

    from .architecture import architectural_claims, assembly_component_claims
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
    if model.schema_version in ASSEMBLY_SCHEMA_VERSIONS:
        assembly_by_id = {row.id: row for row in model.building_assemblies}
        for claim in assembly_component_claims(model):
            component = claim.entity
            owner = assembly_by_id.get(getattr(component, "assembly_id", ""))
            if claim.warning is not None:
                warnings.append(QuantityWarning(
                    claim.warning, claim.message,
                    tuple(sorted({component.id, *(() if owner is None else (owner.id,))})),
                ))
                continue
            if owner is None:
                continue
            contribution = _contribution(
                category=claim.category, item_type=claim.item_type, quantity=claim.quantity,
                unit=claim.unit, variant=claim.variant, entities=(component,),
                assembly_resolver=assembly_resolver,
                group=group_owner.get(component.id) or group_owner.get(owner.id),
                consumed_paths=claim.consumed_paths,
            )
            geometry_evidence = ()
            if (isinstance(component.geometry, ProfileExtrusion) and
                    isinstance(component.geometry.profile, ProfileOutline2D)):
                geometry_evidence = component.geometry.profile.provenance
            contributions.append(replace(
                contribution,
                provenance=_merge_provenance(component.provenance, owner.provenance, geometry_evidence),
                confidence=min(component.confidence, owner.confidence,
                               *(record.confidence for record in geometry_evidence)),
                quantity_provenance=(
                    contribution.quantity_provenance if not geometry_evidence else
                    _merge_provenance(contribution.quantity_provenance or (), geometry_evidence)
                ),
            ))
        envelope_components = tuple(sorted(
            component.id for component in model.assembly_components
            if component.geometry_basis == "caller_selected_envelope"
        ))
        if envelope_components:
            warnings.append(QuantityWarning(
                "assembly_envelope_volume_unknown",
                "Caller-selected envelopes are reported only as envelope volume; physical material volume is unknown.",
                envelope_components,
            ))
        unknown_material_components = tuple(sorted(
            component.id for component in model.assembly_components
            if component.material_basis == "unknown" or component.material_id is None
        ))
        if unknown_material_components:
            warnings.append(QuantityWarning(
                "assembly_material_identity_unknown",
                "Component geometry may be measurable, but material identity is unknown and no material-specific purchase quantity is asserted.",
                unknown_material_components,
            ))
        drywall_components = tuple(sorted(
            component.id for component in model.assembly_components
            if component.component_kind == "drywall_panel"
        ))
        if drywall_components:
            warnings.append(QuantityWarning(
                "drywall_sheet_purchase_unresolved",
                "Drywall area is modeled component area only; stock-sheet counts, seam layout, cutting and waste are not represented.",
                drywall_components,
            ))
        if model.building_assemblies:
            warnings.append(QuantityWarning(
                "assembly_engineering_approval_not_evaluated",
                "Building-assembly geometry is not an engineering, fire-rating, listing or code-approval evaluation.",
                tuple(sorted(assembly.id for assembly in model.building_assemblies)),
            ))
    measured = {identity for item in contributions for identity in item.source_entity_ids}
    # These are nonphysical decision/evidence rows, not quantity-bearing
    # inventory. Their selected product details are folded into the existing
    # fitting contribution above; unresolved outcomes appear as warnings.
    measured.update(row.id for row in model.fitting_selections)
    measured.update(row.id for row in model.fitting_interfaces)
    measured.update(row.id for row in model.fitting_selection_outcomes)
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


def _ai_symbol_scope(entity):
    """Consume intake metadata only; no marker classifier or silent fallback."""
    if "symbol_review" not in entity.attributes:
        lane = entity.attributes.get('pdf_electrical')
        owned_marker = isinstance(lane, dict) and lane.get('scope_method') == 'reviewed_ai_source_assertions_v1'
        if owned_marker or any(p.source_kind == 'symbol-review'
               or p.method == 'ai-symbol-source-scope-v1' or
               (p.source_kind == 'ai-proposed' and isinstance(p.source_element_id, str)
                and p.source_element_id.startswith(('symbol-source-mark:', 'symbol-scope-evidence:')))
               for p in entity.provenance):
            raise QuantityError(f"missing AI symbol source proposal for {entity.id}")
        return None
    from oabm.model import ContractError
    from oabm.proposals.contracts import digest
    from oabm.proposals.symbol_scope import SymbolScope, scope_from_proposals, source_scope_provenance
    from oabm.proposals.symbols import SymbolProposal
    from oabm.reports.scope_ledger import scope_metadata_facts
    review = entity.attributes["symbol_review"]
    try:
        if not isinstance(review, dict) or not isinstance(review.get("proposal"), dict):
            raise ContractError("missing AI symbol source proposal")
        proposal = review["proposal"]
        decision = review.get('decision')
        retained_reviews = [p for p in entity.provenance if p.source_kind == 'symbol-review'
            and p.method == 'explicit-symbol-review-v1' and isinstance(p.attributes, dict)
            and p.attributes.get('decision') == decision]
        if (not isinstance(decision, dict) or decision.get('action') != 'accept'
                or decision.get('proposal_id') != proposal.get('proposal_id')
                or decision.get('proposal_fingerprint') != digest(proposal)
                or not retained_reviews):
            raise ContractError('AI source proposal no longer matches retained review')
        equivalents = review.get("scope_equivalent_proposals", [])
        if not isinstance(equivalents, list) or any(not isinstance(r, dict) for r in equivalents):
            raise ContractError("invalid duplicate scope source proposals")
        if equivalents:
            ids = [r.get("proposal_id") for r in (proposal, *equivalents)]
            if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
                raise ContractError("duplicate scope source proposals require distinct identities")
        peer_bindings = {peer['proposal_id']: digest(peer) for peer in equivalents}
        if not any(p.attributes.get('scope_equivalent_fingerprints', {}) == peer_bindings
                   for p in retained_reviews):
            raise ContractError('duplicate source proposals no longer match retained review')
        source_rows = (proposal, *equivalents)
        for row in source_rows:
            if row.get('scope') is not None:
                SymbolProposal.from_dict(row)
        actual_evidence = [p for p in entity.provenance if p.method == 'ai-symbol-source-scope-v1']
        expected_evidence = source_scope_provenance(source_rows)
        if sorted(digest(asdict(p)) for p in actual_evidence) != sorted(digest(asdict(p)) for p in expected_evidence):
            raise ContractError('AI scope source assertions contradict retained evidence')
        scope = scope_from_proposals(source_rows)
        lane = entity.attributes.get("pdf_electrical")
        if scope is None:
            if lane is not None:
                raise ContractError("scope metadata without original AI assertions")
            return SymbolScope(claims=())  # Old runs remain unknown, never new.
        expected = scope.canonical_metadata()
        # The AI adapter owns this complete shape, including absent keys. Extra
        # reason/marker/legend facts cannot bypass either consumer's guard.
        if (not isinstance(lane, dict) or digest(lane) != digest(expected)
                or scope_metadata_facts(lane) != (
                    expected["scope_status"], expected.get("scope_reason"), False)):
            raise ContractError("AI scope metadata contradicts original source assertions")
        return scope
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise QuantityError(f"invalid AI symbol scope for {entity.id}: {exc}") from exc


def _countable_entity_contribution(
    category: str,
    entity: ElectricalDevice | ElectricalEquipment,
    assembly_resolver: AssemblyResolver | None,
    group: str | None,
) -> _Contribution:
    item_type = entity.device_type if isinstance(entity, ElectricalDevice) else entity.equipment_type
    size = entity.size
    scope = _ai_symbol_scope(entity)
    scope_variant = () if scope is None else (
        ("work_status", scope.work_status), ("bid_scope", scope.bid_scope),
        ("alternate_id", scope.alternate_id))
    result = _contribution(
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
        ) + scope_variant,
        entities=(entity,),
        assembly_resolver=assembly_resolver,
        group=group,
    )

    if scope is not None:
        scope_confidence = [c.confidence for c in scope.claims]
        scope_confidence.extend(p.confidence for p in entity.provenance if provenance_applies_to(
            p, ("attributes.pdf_electrical", "attributes.symbol_review.proposal.scope")))
        result = replace(result, confidence=min([result.confidence, *scope_confidence]))
    return result


def _contribution(
    *,
    category: str,
    item_type: str,
    quantity: float,
    unit: str,
    variant: tuple[tuple[str, object], ...],
    entities: tuple[Entity | AssemblyComponent, ...],
    assembly_resolver: AssemblyResolver | None,
    assembly_entity: Entity | AssemblyComponent | None = None,
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
    "electrical_boxes", "box_selection_outcomes",
    "raceway_selections", "raceway_selection_outcomes",
    "support_assemblies", "support_components", "support_interfaces", "support_selection_outcomes",
    "structural_members", "fitting_selections", "fitting_interfaces", "fitting_selection_outcomes",
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
