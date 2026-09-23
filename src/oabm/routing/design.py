"""Clearly labeled preliminary electrical design from canonical devices."""

from __future__ import annotations

from dataclasses import dataclass, replace

from oabm.model import (
    DERIVATION_INFERRED, DERIVATION_USER, BuildingModel, Circuit, Conductor,
    Port, Provenance, Vector3, stable_id,
)

from .placement import PlacementError
from .router import route_between_ports


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


def design_proposed_circuits(
    model: BuildingModel, *, equipment_id: str,
    device_ids: tuple[str, ...], rules: CircuitDesignRules | None = None,
    user_groups: tuple[tuple[str, ...], ...] | None = None,
    user_input_id: str | None = None,
) -> BuildingModel:
    """Design or replace circuits and runs; never claim source observation.

    Auto grouping is a deterministic geometric sweep of the selected devices.
    A caller may replace grouping, but each device must occur exactly once.
    Replacing this equipment's prior design removes its dependent routes and
    quantities before the new design is calculated.
    """

    rules = rules or CircuitDesignRules()
    equipment = next((item for item in model.electrical_equipment if item.id == equipment_id), None)
    if equipment is None or "placement" not in equipment.attributes:
        raise PlacementError("equipment_id must identify proposed equipment")
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

    prior = tuple(item for item in model.circuits if (
        item.attributes.get("design", {}).get("equipment_id") == equipment_id
    ))
    prior_ids = {item.id for item in prior}
    prior_route_ids = {route_id for item in prior for route_id in item.route_ids}
    if any(
        prior_route_ids.intersection(item.route_ids)
        for item in model.circuits if item.id not in prior_ids
    ):
        raise PlacementError("a designed route is shared with another circuit")
    if any(item.id not in prior_ids and any(
        port_id in item.load_port_ids for port_id in (
            stable_id("port", f"{device_id}:designed-load") for device_id in device_ids
        )
    ) for item in model.circuits):
        raise PlacementError("a selected device already belongs to another circuit")
    base = replace(
        model,
        circuits=tuple(item for item in model.circuits if item.id not in prior_ids),
        conductors=tuple(item for item in model.conductors if item.circuit_id not in prior_ids),
        routes=tuple(item for item in model.routes if item.id not in prior_route_ids),
        route_fittings=tuple(item for item in model.route_fittings
                             if item.route_id not in prior_route_ids),
    )
    source_port = next((port for port in base.ports if (
        port.owner_id == equipment_id and port.role == "source"
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
    port_by_owner = {port.owner_id: port for port in ports if port.role == "load"}
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
                nominal_diameter_m=rules.nominal_diameter_m,
            )
            if route.id in {item.id for item in routes}:
                raise PlacementError("design route identity collides with another route")
            route = replace(route, provenance=tuple((*route.provenance, *equipment.provenance, *provenance)),
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
