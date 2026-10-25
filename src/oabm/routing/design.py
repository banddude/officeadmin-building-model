"""Clearly labeled preliminary electrical design from canonical devices."""

from __future__ import annotations

from dataclasses import dataclass, replace

from oabm.model import (
    DERIVATION_INFERRED, DERIVATION_USER, BuildingModel, Circuit, Conductor,
    Port, Provenance, Vector3, stable_id,
)

from .placement import (
    EquipmentPlacementProposal, PlacementError, apply_equipment_proposal,
    propose_equipment_placement,
)
from .router import RoutingOptions, route_between_ports


@dataclass(frozen=True, slots=True)
class CircuitDesignRules:
    """An explicit preliminary design profile, not a code-compliance claim."""

    max_devices_per_circuit: int = 4
    assumed_voltage_v: float = 120.0
    assumed_poles: int = 1
    route_type: str = "emt"
    nominal_diameter_m: float = 0.0213
    conductor_material: str = "copper"
    conductor_size: str = "12 AWG"
    insulated_conductor_count: int = 2
    include_ground: bool = True

    def __post_init__(self) -> None:
        if self.max_devices_per_circuit < 1 or self.assumed_poles < 1:
            raise PlacementError("design circuit limits must be positive")
        if self.assumed_voltage_v <= 0 or self.nominal_diameter_m <= 0:
            raise PlacementError("design voltage and conduit diameter must be positive")
        if self.insulated_conductor_count < 1 or not self.route_type or not self.conductor_size:
            raise PlacementError("design conductor and route assumptions are required")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_devices_per_circuit": self.max_devices_per_circuit,
            "assumed_voltage_v": self.assumed_voltage_v,
            "assumed_poles": self.assumed_poles,
            "route_type": self.route_type,
            "nominal_diameter_m": self.nominal_diameter_m,
            "conductor_material": self.conductor_material,
            "conductor_size": self.conductor_size,
            "insulated_conductor_count": self.insulated_conductor_count,
            "include_ground": self.include_ground,
        }


def _design_metadata(circuit: Circuit) -> dict[str, object] | None:
    """Return an object-valued optional design marker, without coercion."""

    marker = circuit.attributes.get("design")
    return marker if isinstance(marker, dict) else None


def _managed_design_metadata(
    circuit: Circuit, equipment_id: str,
) -> dict[str, object] | None:
    """Recognize this producer's records without claiming arbitrary JSON.

    ``attributes.design`` is an open JSON extension point in the canonical
    contract. Non-object markers and dicts that merely name this equipment are
    preserved as unrelated metadata. The generated status/origin/observation
    signature distinguishes this producer's ownership record; fields needed
    for idempotence or history are checked only when those paths use them.
    """

    marker = _design_metadata(circuit)
    if (
        marker is not None
        and marker.get("equipment_id") == equipment_id
        and marker.get("status") == "designed"
        and marker.get("origin") in ("system-design", "user-override")
        and marker.get("source_observed") is False
    ):
        return marker
    return None


def _design_device_ids(metadata: dict[str, object], circuit_id: str) -> tuple[str, ...]:
    """Read the saved group only where replay/history requires it."""

    device_ids = metadata.get("device_ids")
    if (
        not isinstance(device_ids, list)
        or not device_ids
        or any(not isinstance(device_id, str) for device_id in device_ids)
        or len(set(device_ids)) != len(device_ids)
    ):
        raise PlacementError(
            f"existing circuit {circuit_id} lacks valid device_ids for design history"
        )
    return tuple(device_ids)


def propose_architectural_electrical_design(
    model: BuildingModel, *, rules: CircuitDesignRules | None = None,
) -> tuple[BuildingModel, tuple[EquipmentPlacementProposal, ...]]:
    """Design one provisional panel per populated level without caller hints.

    This is the entry point for an architectural set that contains devices but
    no observed panel or circuiting. It refuses mixed observed circuitry and
    requires registered level/space/wall geometry before proposing anything.
    """

    if any(item.equipment_type == "panelboard" for item in model.electrical_equipment):
        raise PlacementError("an existing panelboard needs explicit reconciliation")
    if model.circuits:
        raise PlacementError("existing circuits need explicit reconciliation")
    if any(device.level_id is None for device in model.electrical_devices):
        raise PlacementError("devices need registered canonical levels")
    result = model
    proposals: list[EquipmentPlacementProposal] = []
    for level_id in sorted({device.level_id for device in model.electrical_devices}):
        device_ids = tuple(sorted(
            device.id for device in model.electrical_devices if device.level_id == level_id
        ))
        proposal = propose_equipment_placement(
            result, identity_key=f"{level_id}:provisional-panelboard",
            equipment_type="panelboard", name=None, level_id=level_id,
            served_device_ids=device_ids,
        )
        result = apply_equipment_proposal(result, proposal)
        equipment_id = stable_id("equipment", f"{model.model_id}:proposal:{proposal.identity_key}")
        result = design_proposed_circuits(
            result, equipment_id=equipment_id, device_ids=device_ids, rules=rules,
        )
        proposals.append(proposal)
    if not proposals:
        raise PlacementError("no registered electrical devices to design")
    return _sorted_entities(result), tuple(proposals)


def _sorted_entities(model: BuildingModel) -> BuildingModel:
    """Give equivalent reordered canonical inputs the same derived serialization."""

    collections = (
        "levels", "spaces", "walls", "slabs", "ceilings", "openings",
        "electrical_equipment", "electrical_devices", "ports", "obstacles",
        "route_constraints", "routes", "route_fittings", "circuits", "conductors",
        "electrical_boxes", "box_selection_outcomes",
        "raceway_selections", "raceway_selection_outcomes",
        "support_assemblies", "support_components", "support_interfaces", "support_selection_outcomes",
    )
    return replace(model, **{
        name: tuple(sorted(getattr(model, name), key=lambda item: item.id))
        for name in collections
    })


def design_proposed_circuits(
    model: BuildingModel, *, equipment_id: str,
    device_ids: tuple[str, ...], rules: CircuitDesignRules | None = None,
    user_groups: tuple[tuple[str, ...], ...] | None = None,
    user_input_id: str | None = None,
    source_port_id: str | None = None,
    load_port_ids: dict[str, str] | None = None,
    routing_options: RoutingOptions | None = None,
) -> BuildingModel:
    """Design or replace circuits and runs; never claim source observation.

    Auto grouping is a deterministic geometric sweep of the selected devices.
    A caller may replace grouping, but each device must occur exactly once.
    Replacing this equipment's prior design removes its dependent routes and
    quantities before the new design is calculated.
    """

    if source_port_id is not None and rules is None:
        raise PlacementError("explicit reviewed source requires supplied rules")
    rules = rules or CircuitDesignRules()
    equipment = next((item for item in model.electrical_equipment if item.id == equipment_id), None)
    explicit_ports = source_port_id is not None
    if equipment is None or (not explicit_ports and "placement" not in equipment.attributes):
        raise PlacementError("equipment_id must identify proposed equipment or explicit reviewed source")
    if explicit_ports:
        if equipment.equipment_type != "panelboard" or rules is None or user_groups is None or not user_input_id:
            raise PlacementError("explicit reviewed source requires panelboard, rules and user groups")
        if not isinstance(routing_options, RoutingOptions) or not isinstance(load_port_ids, dict):
            raise PlacementError("explicit reviewed ports require routing controls and load identities")
        if set(load_port_ids) != set(device_ids) or len(set(load_port_ids.values())) != len(device_ids):
            raise PlacementError("explicit load ports must exactly cover selected devices")
        by_id = {p.id:p for p in model.ports}
        source = by_id.get(source_port_id)
        if source is None or source.owner_id != equipment_id or source.role != "source" or source.domain != "electrical":
            raise PlacementError("explicit source port owner, role or domain mismatch")
        for owner, identity in load_port_ids.items():
            port = by_id.get(identity)
            if port is None or port.owner_id != owner or port.role != "load" or port.domain != source.domain:
                raise PlacementError("explicit load port owner, role or domain mismatch")
    elif load_port_ids is not None:
        raise PlacementError("explicit load identities require an explicit source port")
    equipment_provenance = equipment.provenance
    if explicit_ports:
        source_records = next(row for row in model.to_dict()["electrical_equipment"]
                              if row["id"] == equipment.id)["provenance"]
        # These scopes belong to the panel. Derived connections reference that
        # owner and retain its full records without applying its fields to routes.
        equipment_provenance = tuple(Provenance(
            source_kind=record.source_kind, source_id=record.source_id,
            source_element_id=equipment.id, method="reviewed-circuit-owner-dependency",
            confidence=record.confidence, derivation=DERIVATION_INFERRED,
            attributes={"source_owner":{"model_id":model.model_id,
                "collection":"electrical_equipment", "canonical_id":equipment.id},
                "source_record":source_record, "source_observed":False},
        ) for record,source_record in zip(equipment.provenance,source_records))
    devices = {item.id: item for item in model.electrical_devices}
    if not device_ids or len(device_ids) != len(set(device_ids)):
        raise PlacementError("device_ids must be nonempty and unique")
    if any(item not in devices for item in device_ids):
        raise PlacementError("device_ids contains an unknown device")
    if user_groups is not None:
        if not user_input_id:
            raise PlacementError("user grouping requires a stable user_input_id")
        flat = tuple(item for group in user_groups for item in group)
        if any(not group or len(group) > rules.max_devices_per_circuit for group in user_groups):
            raise PlacementError("each user group must fit the explicit design limit")
        if len(flat) != len(device_ids) or set(flat) != set(device_ids):
            raise PlacementError("user groups must cover each selected device exactly once")
        groups = tuple(tuple(group) for group in user_groups)
    else:
        ordered = tuple(sorted(device_ids, key=lambda item: (
            devices[item].pose.position.x, devices[item].pose.position.y, item,
        )))
        groups = tuple(
            ordered[start:start + rules.max_devices_per_circuit]
            for start in range(0, len(ordered), rules.max_devices_per_circuit)
        )

    prior_designs: dict[str, dict[str, object]] = {}
    for item in model.circuits:
        design = _managed_design_metadata(item, equipment_id)
        if design is not None:
            prior_designs[item.id] = design
    prior = tuple(item for item in model.circuits if item.id in prior_designs)
    if user_input_id is not None and any(
        (marker := _design_metadata(item)) is not None
        and marker.get("user_input_id") == user_input_id
        and marker.get("equipment_id") != equipment_id
        for item in model.circuits
    ):
        raise PlacementError("user_input_id already belongs to another circuit design")
    decision_history = model.attributes.get("design_decision_history", [])
    if not isinstance(decision_history, list):
        raise PlacementError("design decision history must be a list")
    if user_input_id is not None and any(
        isinstance(item, dict) and item.get("user_input_id") == user_input_id
        for item in decision_history
    ):
        raise PlacementError("historical user_input_id cannot be reused for circuit groups")
    reused_decisions = tuple(item for item in prior if (
        user_input_id is not None
        and prior_designs[item.id].get("user_input_id") == user_input_id
    ))
    if reused_decisions:
        prior_groups = tuple(
            _design_device_ids(prior_designs[item.id], item.id)
            for item in sorted(reused_decisions, key=lambda circuit: int(circuit.circuit_number or 0))
        )
        if (len(reused_decisions) == len(prior)
                and prior_groups == groups
                and all(prior_designs[item.id].get("rules") == rules.to_dict()
                        for item in reused_decisions)):
            return model
        raise PlacementError("one user_input_id cannot describe conflicting circuit groups")
    prior_ids = {item.id for item in prior}
    prior_route_ids = {route_id for item in prior for route_id in item.route_ids}
    if any(
        prior_route_ids.intersection(item.route_ids)
        for item in model.circuits if item.id not in prior_ids
    ):
        raise PlacementError("a designed route is shared with another circuit")
    if any(item.id not in prior_ids and any(
        port_id in item.load_port_ids for port_id in (
            load_port_ids[device_id] if explicit_ports else stable_id("port", f"{device_id}:designed-load") for device_id in device_ids
        )
    ) for item in model.circuits):
        raise PlacementError("a selected device already belongs to another circuit")
    for item in prior:
        design = prior_designs[item.id]
        old_decision = design.get("user_input_id")
        if old_decision is not None and not isinstance(old_decision, str):
            raise PlacementError(
                f"existing circuit {item.id} has invalid user_input_id for design history"
            )
        if old_decision and not any(
            isinstance(row, dict) and row.get("user_input_id") == old_decision
            for row in decision_history
        ):
            decision_history = [*decision_history, {
                "user_input_id": old_decision,
                "equipment_id": equipment_id,
                "device_ids": list(_design_device_ids(design, item.id)),
                "rules": design.get("rules"),
            }]
    base = replace(
        model,
        attributes={**model.attributes, "design_decision_history": decision_history},
        circuits=tuple(item for item in model.circuits if item.id not in prior_ids),
        conductors=tuple(item for item in model.conductors if item.circuit_id not in prior_ids),
        routes=tuple(item for item in model.routes if item.id not in prior_route_ids),
        route_fittings=tuple(item for item in model.route_fittings
                             if item.route_id not in prior_route_ids),
    )
    source_port = next((port for port in base.ports if (
        (port.id == source_port_id if explicit_ports else port.owner_id == equipment_id and port.role == "source")
    )), None)
    if source_port is None:
        raise PlacementError("proposed equipment has no source port")
    origin = "user-override" if user_groups is not None else "system-design"
    derivation = DERIVATION_USER if user_groups is not None else DERIVATION_INFERRED
    provenance = (Provenance(
        source_kind=origin, source_id=model.model_id,
        source_element_id=user_input_id or equipment_id,
        method="geometric-device-sweep-v1" if user_groups is None else "user-circuit-groups",
        confidence=0.45 if user_groups is None else 1.0,
        derivation=derivation,
        attributes={"source_observed": False, "rules": rules.to_dict()},
    ),)
    ports = list(base.ports)
    port_by_owner = ({owner:next(p for p in ports if p.id == identity) for owner,identity in load_port_ids.items()}
                     if explicit_ports else {port.owner_id: port for port in ports if port.role == "load"})
    for device_id in sorted(device_ids):
        if device_id not in port_by_owner:
            device = devices[device_id]
            port = Port(
                id=stable_id("port", f"{device_id}:designed-load"),
                owner_id=device_id, domain="electrical", role="load",
                pose=device.pose, direction=Vector3(x=0.0, y=0.0, z=1.0),
                confidence=0.45, provenance=(Provenance(
                    source_kind="system-design", source_id=model.model_id,
                    source_element_id=device_id, method="designed-load-port",
                    confidence=0.45, derivation=DERIVATION_INFERRED,
                    attributes={"source_observed": False},
                ),),
            )
            ports.append(port)
            port_by_owner[device_id] = port
    base = replace(base, ports=tuple(sorted(ports, key=lambda item: item.id)))

    routes = list(base.routes)
    fittings = list(base.route_fittings)
    circuits = list(base.circuits)
    conductors = list(base.conductors)
    for number, group in enumerate(groups, start=1):
        circuit_id = stable_id("circuit", f"{equipment_id}:{','.join(sorted(group))}")
        route_ids: list[str] = []
        previous_port_id = source_port.id
        for device_id in group:
            next_port_id = port_by_owner[device_id].id
            route, route_fittings = route_between_ports(
                base, previous_port_id, next_port_id, rules.route_type,
                nominal_diameter_m=rules.nominal_diameter_m, options=routing_options,
            )
            if route.id in {item.id for item in routes}:
                raise PlacementError("design route identity collides with another route")
            route = replace(route, provenance=tuple((*route.provenance, *equipment_provenance, *provenance)),
                            attributes={**route.attributes, "design_status": "designed",
                                        "equipment_id": equipment_id})
            routes.append(route)
            fittings.extend(replace(
                fitting, provenance=route.provenance,
                attributes={**fitting.attributes, "design_status": "designed",
                            "equipment_id": equipment_id},
            ) for fitting in route_fittings)
            route_ids.append(route.id)
            previous_port_id = next_port_id
        design_attributes = {
            "status": "designed", "origin": origin, "equipment_id": equipment_id,
            "rules": rules.to_dict(), "source_observed": False,
            "device_ids": list(group), "user_input_id": user_input_id,
        }
        circuit = Circuit(
            id=circuit_id, source_port_id=source_port.id,
            load_port_ids=tuple(port_by_owner[item].id for item in group),
            circuit_number=str(number), voltage_v=rules.assumed_voltage_v,
            poles=rules.assumed_poles, route_ids=tuple(route_ids),
            confidence=provenance[0].confidence, provenance=provenance,
            attributes={"design": design_attributes},
        )
        circuits.append(circuit)
        for role, count in (("insulated", rules.insulated_conductor_count),
                            ("ground", 1 if rules.include_ground else 0)):
            if not count:
                continue
            conductors.append(Conductor(
                id=stable_id("conductor", f"{circuit_id}:{role}"),
                circuit_id=circuit_id, role=role, material=rules.conductor_material,
                size=rules.conductor_size, count=count, route_ids=tuple(route_ids),
                confidence=circuit.confidence, provenance=provenance,
                attributes={"design_status": "designed", "source_observed": False},
            ))
    return replace(
        base,
        routes=tuple(sorted(routes, key=lambda item: item.id)),
        route_fittings=tuple(sorted(fittings, key=lambda item: item.id)),
        circuits=tuple(sorted(circuits, key=lambda item: item.id)),
        conductors=tuple(sorted(conductors, key=lambda item: item.id)),
    )
