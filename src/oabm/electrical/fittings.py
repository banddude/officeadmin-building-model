"""Exact, caller-bound route-fitting product selection; no placement or CAD import."""
from oabm.model import reject_physical_routes
from dataclasses import asdict, dataclass, replace
import json
import re
from typing import Sequence

from oabm.model import (BuildingModel, CatalogProduct, ContractError, FittingInterface,
    FittingSelection, FittingSelectionOutcome, FITTING_SCHEMA_VERSION, Port,
    Provenance, RuleReference, stable_id)
from oabm.model.common import _validate_id
from oabm.model.model import (_fitting_distance, _normalized_dot, _point_on_route,
                              _port_matches_route_tangent)
from oabm.model.physical import ElectricalBox
from .catalog_manifest import CatalogManifest, VERSION_V2, FittingCatalogInterface

_KIND = "route-fitting-selection-producer"
_METHOD = "explicit-route-fitting-intent"
_ENVIRONMENT_FACTS = frozenset({"dry_location", "wet_location", "raintight"})
_GEOMETRY_TOLERANCE_M = 1e-6
_DIRECTION_DOT_TOLERANCE = 1e-6
_INTERFACE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def _portable(value):
    return json.loads(json.dumps(asdict(value), sort_keys=True, allow_nan=False))


def _provenance_key(record):
    # Include every semantic field: page can distinguish otherwise-identical
    # source rows and must not fall back to caller input order.
    return json.dumps(_portable(record), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingRulePermission:
    route_type: str
    fitting_type: str

    def __post_init__(self):
        if self.route_type != "emt" or self.fitting_type not in {"coupling", "connector"}:
            raise ContractError("fitting rule permission must name a supported EMT topology")


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingRule:
    """Explicit caller policy; all environmental, listing and OD asks are explicit."""
    reference: RuleReference
    permitted_products: tuple[CatalogProduct, ...]
    permitted_topologies: tuple[FittingRulePermission, ...]
    check_actual_od: bool
    required_environment: tuple[str, ...]
    required_listing_refs: tuple[str, ...]
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        if not isinstance(self.reference, RuleReference):
            raise ContractError("fitting rule requires an exact RuleReference")
        if (not isinstance(self.permitted_products, tuple) or
                any(not isinstance(product, CatalogProduct) for product in self.permitted_products) or
                len(set(self.permitted_products)) != len(self.permitted_products)):
            raise ContractError("fitting rule product permissions must be unique full product keys")
        if (not isinstance(self.permitted_topologies, tuple) or
                any(not isinstance(item, FittingRulePermission) for item in self.permitted_topologies) or
                len(set(self.permitted_topologies)) != len(self.permitted_topologies)):
            raise ContractError("fitting rule topologies must be unique typed permissions")
        if type(self.check_actual_od) is not bool:
            raise ContractError("check_actual_od is a mandatory explicit boolean")
        if (not isinstance(self.required_environment, tuple) or
                any(not isinstance(item, str) or item not in _ENVIRONMENT_FACTS
                    for item in self.required_environment) or
                len(set(self.required_environment)) != len(self.required_environment)):
            raise ContractError("required_environment must be a unique tuple of supported fact names")
        if (not isinstance(self.required_listing_refs, tuple) or
                any(not isinstance(item, str) or not item.strip() for item in self.required_listing_refs) or
                len(set(self.required_listing_refs)) != len(self.required_listing_refs)):
            raise ContractError("required_listing_refs must be an explicit tuple of exact references")
        if (not isinstance(self.provenance, tuple) or not self.provenance or
                any(not isinstance(record, Provenance) or record.derivation is None for record in self.provenance)):
            raise ContractError("fitting rule requires explicit caller evidence")
        allowed_fields = {"reference", "permitted_products", "permitted_topologies", "check_actual_od",
                          "required_environment", "required_listing_refs"}
        paths = [path for record in self.provenance for path in (record.scope_paths or ())]
        if (any(path not in allowed_fields for path in paths) or
                any(not record.scope_paths for record in self.provenance) or
                set(paths) != allowed_fields):
            raise ContractError("fitting rule evidence must qualify explicit caller rule fields")
        identities = [(record.source_id, path) for record in self.provenance
                      for path in (record.scope_paths or ())]
        if len(identities) != len(set(identities)):
            raise ContractError("fitting rule has duplicate same-field/source evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingPortBinding:
    """Caller-authored mapping from product interface keys to existing canonical ports."""
    interface_key: str
    mating_interface_key: str | None
    fitting_port_id: str | None
    counterpart_port_id: str | None

    def __post_init__(self):
        if not isinstance(self.interface_key, str) or not _INTERFACE_KEY.fullmatch(self.interface_key):
            raise ContractError("fitting port binding requires a stable interface key")
        if self.mating_interface_key is not None and (
                not isinstance(self.mating_interface_key, str) or not _INTERFACE_KEY.fullmatch(self.mating_interface_key)):
            raise ContractError("mating interface key must be nonempty or unknown")
        for name in ("fitting_port_id", "counterpart_port_id"):
            value = getattr(self, name)
            if value is not None:
                _validate_id(value, name)


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingIntent:
    intent_id: str
    fitting_id: str
    actor_id: str
    requested_product: CatalogProduct | None
    basis: RuleReference | None
    bindings: tuple[FittingPortBinding, ...]

    def __post_init__(self):
        _validate_id(self.intent_id, "intent_id")
        _validate_id(self.fitting_id, "fitting_id")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ContractError("fitting intent actor_id must be nonempty")
        if self.requested_product is not None and not isinstance(self.requested_product, CatalogProduct):
            raise ContractError("requested fitting product must be a full CatalogProduct or unknown")
        if self.basis is not None and not isinstance(self.basis, RuleReference):
            raise ContractError("fitting intent basis must be a RuleReference or unknown")
        if not isinstance(self.bindings, tuple) or any(not isinstance(item, FittingPortBinding) for item in self.bindings):
            raise ContractError("fitting intent bindings must be typed")
        keys = [item.interface_key for item in self.bindings]
        if len(keys) != len(set(keys)):
            raise ContractError("duplicate caller binding interface key")
        fitting_ports = [item.fitting_port_id for item in self.bindings if item.fitting_port_id is not None]
        mate_ports = [item.counterpart_port_id for item in self.bindings if item.counterpart_port_id is not None]
        if len(fitting_ports) != len(set(fitting_ports)) or len(mate_ports) != len(set(mate_ports)):
            raise ContractError("caller bindings cannot reuse fitting or counterpart ports")


def select_route_fittings(model: BuildingModel, *, intents: Sequence[FittingIntent],
                          catalog: CatalogManifest, rules: Sequence[FittingRule]) -> BuildingModel:
    """Apply explicit exact-product fitting intents to 1.5 models deterministically.

    Only existing EMT couplings/connectors with caller-authored fitting and
    counterpart ports can be selected. Missing product facts remain unresolved;
    this function never creates geometry or ports.
    """
    reject_physical_routes(model, "select_route_fittings")
    if model.schema_version != FITTING_SCHEMA_VERSION:
        raise ContractError("explicit upgrade_to_v1_5 is required for route-fitting selections")
    # Subclasses may override fitting_entry() and detach returned objects from
    # the immutable raw bytes; only the loader's exact implementation is safe
    # for this byte-bound selector.
    if type(catalog) is not CatalogManifest or catalog.version != VERSION_V2:
        raise ContractError("route-fitting selection requires the exact electrical-catalog-v2 byte snapshot")
    rules = tuple(rules)
    intents = tuple(intents)
    owned = (model.fitting_selections, model.fitting_interfaces, model.fitting_selection_outcomes)
    for collection in owned:
        for entity in collection:
            if not any(record.source_kind == _KIND and record.method == _METHOD for record in entity.provenance):
                raise ContractError("cannot overwrite another source route-fitting inventory")

    routes = {item.id: item for item in model.routes}
    fittings = {item.id: item for item in model.route_fittings}
    ports = {item.id: item for item in model.ports}
    boxes = {item.id: item for item in model.electrical_boxes}
    raceway_selections = {item.route_id: item for item in model.raceway_selections}
    raceway_outcomes = {item.route_id: item for item in model.raceway_selection_outcomes}
    rule_map, intent_ids, fitting_intents = {}, set(), set()
    for rule in rules:
        if not isinstance(rule, FittingRule):
            raise ContractError("fitting selector requires typed caller rules")
        if rule.reference in rule_map:
            raise ContractError("duplicate exact caller rule reference")
        normalized_rule = replace(rule,
            permitted_products=tuple(sorted(rule.permitted_products,
                key=lambda product: (product.catalog_id, product.catalog_revision, product.product_id))),
            permitted_topologies=tuple(sorted(rule.permitted_topologies,
                key=lambda item: (item.route_type, item.fitting_type))),
            required_environment=tuple(sorted(rule.required_environment)),
            required_listing_refs=tuple(sorted(rule.required_listing_refs)),
            provenance=tuple(sorted(rule.provenance, key=_provenance_key)))
        rule_map[normalized_rule.reference] = normalized_rule
    normalized_intents = []
    for intent in intents:
        if not isinstance(intent, FittingIntent):
            raise ContractError("fitting selector requires typed explicit intents")
        if intent.intent_id in intent_ids or intent.fitting_id in fitting_intents:
            raise ContractError("one active fitting intent is allowed per intent ID and fitting")
        intent_ids.add(intent.intent_id)
        fitting_intents.add(intent.fitting_id)
        if intent.fitting_id not in fittings:
            raise ContractError("fitting intent references a missing canonical fitting")
        normalized_intents.append(replace(intent, bindings=tuple(sorted(intent.bindings, key=lambda row: row.interface_key))))

    selections, interfaces, outcomes = [], [], []
    for intent in sorted(normalized_intents, key=lambda row: (row.fitting_id, row.intent_id)):
        fitting = fittings[intent.fitting_id]
        route = routes[fitting.route_id]
        rule = rule_map.get(intent.basis)
        entry = None
        if intent.requested_product is not None:
            try:
                candidate = CatalogManifest.fitting_entry(catalog, intent.requested_product.product_id)
            except ContractError:
                candidate = None
            if candidate is not None and candidate.product == intent.requested_product:
                entry = candidate
        reasons: set[str] = set()
        consolidation = fitting.attributes.get("consolidation")
        if (isinstance(consolidation, dict) and consolidation.get("nonmaterial") is True) or fitting.fitting_type == "logical-junction":
            reasons.add("nonmaterial_fitting")
        if fitting.fitting_type not in {"coupling", "connector"}:
            reasons.add("unsupported_fitting_type")
        if route.fitting_ids.count(fitting.id) != 1:
            reasons.add("unsupported_joint_topology")
        if route.route_type != "emt":
            reasons.add("wrong_route_type")
        if intent.requested_product is None:
            reasons.add("missing_requested_product")
        if entry is None and intent.requested_product is not None:
            reasons.add("missing_catalog_product")
        if rule is None:
            reasons.add("missing_rule_reference")
        if rule is not None:
            if (intent.requested_product is None or intent.requested_product not in rule.permitted_products or
                    (route.route_type, fitting.fitting_type) not in {
                        (permission.route_type, permission.fitting_type)
                        for permission in rule.permitted_topologies}):
                reasons.add("product_not_permitted")
        if entry is not None and (entry.fitting_type != fitting.fitting_type or entry.route_type != route.route_type):
            reasons.add("product_not_permitted")
        if route.route_type == "emt":
            route_selection = raceway_selections.get(route.id)
            route_outcome = raceway_outcomes.get(route.id)
            if (route_selection is None or route_outcome is None or route_outcome.status != "selected" or
                    route_outcome.selected_selection_id != route_selection.id):
                reasons.add("missing_route_product")
        else:
            route_selection = None
        if entry is not None and rule is not None:
            if rule.check_actual_od:
                reasons.add("actual_od_unknown")
            for fact in rule.required_environment:
                values = [getattr(interface, fact) for interface in entry.interfaces]
                if any(value is False for value in values):
                    reasons.add("environment_conflict")
                elif any(value is not True for value in values):
                    reasons.add("environment_unknown")
            if any(any(reference not in interface.listing_refs for reference in rule.required_listing_refs)
                   for interface in entry.interfaces):
                reasons.add("listing_reference_missing")

        binding_map = {binding.interface_key: binding for binding in intent.bindings}
        catalog_interfaces = {} if entry is None else {item.key: item for item in entry.interfaces}
        if entry is not None and set(binding_map) - set(catalog_interfaces):
            reasons.add("counterpart_interface_unknown")
        prepared: list[tuple[FittingCatalogInterface, FittingPortBinding, Port, Port, object, CatalogProduct]] = []
        role_set = set()
        if entry is not None:
            for catalog_interface in entry.interfaces:
                role_set.add(catalog_interface.role)
                binding = binding_map.get(catalog_interface.key)
                if binding is None:
                    reasons.update({"fitting_port_missing", "counterpart_port_missing"})
                    continue
                if binding.fitting_port_id is None or binding.fitting_port_id not in ports:
                    reasons.add("fitting_port_missing")
                    continue
                own_port = ports[binding.fitting_port_id]
                if own_port.owner_id != fitting.id:
                    reasons.add("interface_port_owner_mismatch")
                    continue
                if binding.counterpart_port_id is None or binding.counterpart_port_id not in ports:
                    reasons.add("counterpart_port_missing")
                    continue
                mate_port = ports[binding.counterpart_port_id]
                if binding.mating_interface_key is None:
                    reasons.add("counterpart_interface_unknown")
                    continue
                if catalog_interface.role == "raceway":
                    if mate_port.owner_id != route.id:
                        reasons.add("interface_port_owner_mismatch")
                        continue
                    if route_selection is None:
                        reasons.add("missing_route_product")
                        continue
                    mate_product = route_selection.product
                else:
                    box = boxes.get(mate_port.owner_id)
                    if box is None:
                        reasons.add("interface_port_owner_mismatch")
                        continue
                    mate_product = box.product
                if catalog_interface.role == "raceway" and "raceway" not in catalog_interface.permitted_wiring_methods:
                    reasons.add("incompatible_wiring_method")
                matching_pairs = [pair for pair in entry.mating_pairs
                    if pair.interface_key == catalog_interface.key and pair.mating_product == mate_product and
                       pair.mating_interface_key == binding.mating_interface_key]
                if not matching_pairs:
                    reasons.add("mating_pair_unknown")
                    continue
                if len(matching_pairs) != 1:
                    reasons.add("mating_pair_conflict")
                    continue
                prepared.append((catalog_interface, binding, own_port, mate_port, matching_pairs[0], mate_product))

        expected_roles = {"raceway"} if fitting.fitting_type == "coupling" else {"raceway", "enclosure"}
        if (entry is not None and fitting.fitting_type in {"coupling", "connector"} and
                role_set != expected_roles):
            reasons.add("unsupported_joint_topology")
        if entry is not None and (len(intent.bindings) != 2 or len(entry.interfaces) != 2):
            reasons.add("unsupported_joint_topology")
        if len({row[1].counterpart_port_id for row in prepared}) != len(prepared):
            reasons.add("unsupported_joint_topology")
        if len({row[1].fitting_port_id for row in prepared}) != len(prepared):
            reasons.add("unsupported_joint_topology")
        if entry is not None and set(binding_map) != set(catalog_interfaces):
            reasons.add("counterpart_interface_unknown")

        # Check existing topology and geometry before constructing a selected canonical record.
        for catalog_interface, binding, own_port, mate_port, pair, mate_product in prepared:
            if (mate_port.id not in own_port.connected_port_ids or own_port.id not in mate_port.connected_port_ids):
                reasons.add("port_connectivity_missing")
            elif (own_port.connected_port_ids != (mate_port.id,) or
                  mate_port.connected_port_ids != (own_port.id,)):
                reasons.add("unsupported_joint_topology")
            if (_fitting_distance(own_port.pose.position, mate_port.pose.position) > _GEOMETRY_TOLERANCE_M or
                    _normalized_dot(own_port.direction, mate_port.direction) > -1.0 + _DIRECTION_DOT_TOLERANCE):
                reasons.add("interface_geometry_mismatch")
            if catalog_interface.role == "raceway" and (
                    not _point_on_route(route, mate_port.pose.position, _GEOMETRY_TOLERANCE_M) or
                    not _port_matches_route_tangent(route, mate_port, _GEOMETRY_TOLERANCE_M,
                                                    _DIRECTION_DOT_TOLERANCE)):
                reasons.add("interface_geometry_mismatch")
        if not _point_on_route(route, fitting.pose.position, _GEOMETRY_TOLERANCE_M):
            reasons.add("interface_geometry_mismatch")

        selected_id = None
        if not reasons and entry is not None and rule is not None and route_selection is not None:
            selected_id = stable_id("fitting-selection", f"{model.model_id}:{intent.intent_id}:{fitting.id}")
            caller = _intent_provenance(model, intent)
            rule_basis_records = tuple(replace(record, scope_paths=("basis",), attributes={**record.attributes,
                "consumed_caller_rule": _portable(rule), "rule_compliance_verified": False})
                for record in rule.provenance if "reference" in (record.scope_paths or ()))
            generated = Provenance(source_kind=_KIND, source_id=model.model_id, source_element_id=intent.intent_id,
                method="exact-route-fitting-selection", derivation="inferred",
                scope_paths=("route_id", "route_type", "fitting_type", "geometry_basis"),
                attributes={"catalog_manifest_sha256": entry.manifest_sha256,
                    "consumed_catalog_entry": _portable(entry),
                    "mating_pair_ids": [pair.id for _, _, _, _, pair, _ in prepared],
                    "consumed_caller_rule": _portable(rule),
                    "consumed_caller_rule_provenance": [_portable(record) for record in rule.provenance],
                    "actual_od_check_performed": False, "source_authenticity_verified": False,
                    "product_fit_verified": False, "code_or_listing_compliance_verified": False})
            selection_provenance = (replace(caller, scope_paths=("intent_id", "fitting_id")),
                *entry.provenance, *rule_basis_records, generated)
            selection_confidence = min(record.confidence for record in selection_provenance)
            selection = FittingSelection(id=selected_id, name=f"{fitting.fitting_type} {intent.intent_id}",
                intent_id=intent.intent_id, fitting_id=fitting.id, route_id=route.id,
                product=entry.product, route_type=route.route_type, fitting_type=fitting.fitting_type,
                basis=rule.reference, geometry_basis=entry.geometry_basis, size=entry.size,
                confidence=selection_confidence, provenance=selection_provenance)
            selections.append(selection)
            for catalog_interface, binding, own_port, mate_port, pair, mate_product in sorted(
                    prepared, key=lambda row: row[0].key):
                binding_record = replace(caller, source_element_id=intent.intent_id,
                    scope_paths=("interface_key", "mating_interface_key", "port_id", "mating_port_id"),
                    attributes={**caller.attributes, "consumed_port_binding": _portable(binding)})
                pair_records = tuple(replace(record, attributes={**record.attributes,
                    "consumed_mating_pair": _portable(pair),
                                "catalog_manifest_sha256": entry.manifest_sha256})
                    for record in pair.provenance)
                catalog_interface_receipt = {
                    "key": catalog_interface.key, "role": catalog_interface.role,
                    "connection_method": catalog_interface.connection_method,
                    "permitted_wiring_methods": list(catalog_interface.permitted_wiring_methods),
                    "nominal_designation": catalog_interface.nominal_designation,
                    "actual_accepted_od_m": None if catalog_interface.actual_accepted_od_m is None else
                        _portable(catalog_interface.actual_accepted_od_m),
                    "dry_location": catalog_interface.dry_location,
                    "wet_location": catalog_interface.wet_location,
                    "raintight": catalog_interface.raintight,
                    "listing_refs": list(catalog_interface.listing_refs),
                }
                catalog_interface_records = tuple(replace(record, attributes={**record.attributes,
                    "consumed_catalog_interface": catalog_interface_receipt,
                    "catalog_manifest_sha256": entry.manifest_sha256})
                    for record in catalog_interface.provenance)
                selection_link = Provenance(source_kind=_KIND, source_id=model.model_id,
                    source_element_id=selection.id, method="fitting-interface-selection-link",
                    derivation="inferred", scope_paths=("selection_id",),
                    attributes={"actual_od_check_performed": False})
                owner_id = mate_port.owner_id
                owner_record = Provenance(source_kind=_KIND, source_id=model.model_id,
                    source_element_id=owner_id, method="exact-counterpart-product-binding",
                    derivation="inferred", scope_paths=("mating_product",),
                    attributes={"consumed_mating_product": _portable(mate_product),
                        "consumed_owner_id": owner_id,
                        "route_selection_id": route_selection.id if catalog_interface.role == "raceway" else None,
                        "actual_od_check_performed": False})
                provenance = (binding_record, *catalog_interface_records, *pair_records,
                              owner_record, selection_link)
                interfaces.append(FittingInterface(
                    id=stable_id("fitting-interface", f"{model.model_id}:{intent.intent_id}:{catalog_interface.key}"),
                    name=f"{entry.product.product_id} {catalog_interface.key}", selection_id=selection.id,
                    interface_key=catalog_interface.key, role=catalog_interface.role,
                    connection_method=catalog_interface.connection_method,
                    permitted_wiring_methods=catalog_interface.permitted_wiring_methods,
                    port_id=own_port.id, mating_port_id=mate_port.id, mating_product=mate_product,
                    mating_interface_key=pair.mating_interface_key, mating_pair_id=pair.id,
                    nominal_designation=catalog_interface.nominal_designation,
                    actual_accepted_od_m=catalog_interface.actual_accepted_od_m,
                    dry_location=catalog_interface.dry_location, wet_location=catalog_interface.wet_location,
                    raintight=catalog_interface.raintight, listing_refs=catalog_interface.listing_refs,
                    confidence=min(record.confidence for record in provenance), provenance=provenance))

        status = "selected" if selected_id is not None else "unresolved"
        if selected_id is None and not reasons:
            raise ContractError("fitting selector reached an unresolved decision without a reason")
        reason_codes = () if selected_id is not None else tuple(sorted(reasons))
        caller = _intent_provenance(model, intent)
        rule_evidence = () if rule is None else tuple(replace(record, scope_paths=("requested_basis",),
            attributes={**record.attributes, "consumed_caller_rule": _portable(rule)})
            for record in rule.provenance if "reference" in (record.scope_paths or ()))
        decision = Provenance(source_kind=_KIND, source_id=model.model_id, source_element_id=intent.intent_id,
            method="exact-route-fitting-selection-outcome", derivation="inferred",
            scope_paths=("route_id", "status", "selected_selection_id", "reason_codes", "actual_od_check_performed"),
            attributes={"unresolved_reasons": list(reason_codes),
                "consumed_selection_id": selected_id,
                # Even a not-found product is a result of this exact pinned
                # snapshot; preserve the snapshot digest independently of an
                # entry-level hit or miss.
                "catalog_manifest_sha256": catalog.sha256,
                "consumed_caller_rule": None if rule is None else _portable(rule),
                "consumed_caller_rule_provenance": [] if rule is None else
                    [_portable(record) for record in rule.provenance],
                "actual_od_check_performed": False, "product_fit_verified": False,
                "code_or_listing_compliance_verified": False})
        outcome_provenance = (replace(caller, scope_paths=("intent_id", "fitting_id", "requested_product", "requested_basis")),
                              *rule_evidence, decision)
        outcomes.append(FittingSelectionOutcome(
            id=stable_id("fitting-outcome", f"{model.model_id}:{intent.intent_id}:{fitting.id}"),
            name=f"Route fitting selection {intent.intent_id}", intent_id=intent.intent_id,
            fitting_id=fitting.id, route_id=route.id, status=status,
            selected_selection_id=selected_id, requested_product=intent.requested_product,
            requested_basis=intent.basis, reason_codes=reason_codes,
            confidence=min(record.confidence for record in outcome_provenance), provenance=outcome_provenance))

    input_record = Provenance(source_kind=_KIND, source_id=model.model_id,
        method="explicit-route-fitting-inputs", derivation="user",
        scope_paths=("fitting_selections", "fitting_interfaces", "fitting_selection_outcomes"),
        attributes={"consumed_intents": [_portable(intent) for intent in sorted(normalized_intents,
            key=lambda row: (row.fitting_id, row.intent_id))],
            "catalog_manifest_sha256": catalog.sha256,
            "actual_od_check_performed": False, "automatic_port_or_geometry_created": False})
    model_provenance = tuple(record for record in model.provenance
        if not (record.source_kind == _KIND and record.method == input_record.method)) + (input_record,)
    return replace(model, fitting_selections=tuple(sorted(selections, key=lambda row: row.id)),
        fitting_interfaces=tuple(sorted(interfaces, key=lambda row: row.id)),
        fitting_selection_outcomes=tuple(sorted(outcomes, key=lambda row: row.id)),
        provenance=model_provenance)


def _intent_provenance(model: BuildingModel, intent: FittingIntent) -> Provenance:
    return Provenance(source_kind=_KIND, source_id=model.model_id,
        source_element_id=intent.intent_id, method=_METHOD, derivation="user", confidence=1.0,
        attributes={"actor_id": intent.actor_id, "consumed_intent": _portable(intent),
            "requested_product": None if intent.requested_product is None else _portable(intent.requested_product),
            "requested_basis": None if intent.basis is None else _portable(intent.basis),
            "automatic_port_or_geometry_created": False})
