from __future__ import annotations

import json
import math
import types
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar, Union, get_args, get_origin, get_type_hints

from .common import (
    SCHEMA_VERSION, PHYSICAL_SCHEMA_VERSION, RACEWAY_SCHEMA_VERSION, SUPPORT_SCHEMA_VERSION,
    PHYSICAL_SCHEMA_VERSIONS, RACEWAY_SCHEMA_VERSIONS, SUPPORTED_SCHEMA_VERSIONS,
    SUPPORT_SCHEMA_VERSIONS, STRUCTURAL_SCHEMA_VERSION, STRUCTURAL_SCHEMA_VERSIONS,
    FITTING_SCHEMA_VERSION, FITTING_SCHEMA_VERSIONS, ASSEMBLY_SCHEMA_VERSION,
    ASSEMBLY_SCHEMA_VERSIONS,
    ContractError, CoordinateSystem, Entity, Point3, Provenance,
    UnsupportedSchemaVersion, _distance, _finite, _validate_id, _validate_json_value,
)
from .physical import CatalogProduct, ElectricalBox, BoxSelectionOutcome, box_contains_point
from .raceway import RacewaySelection, RacewaySelectionOutcome, wiring_method_for_route_type
from .structural import StructuralMember
from .fittings import FittingInterface, FittingSelection, FittingSelectionOutcome
from .assemblies import AssemblyComponent, BuildingAssembly
from .assembly_validation import (
    validate_assembly_route_contexts,
    validate_building_assemblies,
)
from .supports import (SupportAssembly, SupportComponent, SupportInterface, SupportSelectionOutcome,
                       component_contains_point, component_envelope_key, supported_levels)

_PHYSICAL_FIELDS = frozenset({"electrical_boxes", "box_selection_outcomes"})
_RACEWAY_FIELDS = frozenset({"raceway_selections", "raceway_selection_outcomes"})
_SUPPORT_FIELDS = frozenset({"support_assemblies", "support_components", "support_interfaces", "support_selection_outcomes"})
_STRUCTURAL_FIELDS = frozenset({"structural_members"})
_FITTING_FIELDS = frozenset({"fitting_selections", "fitting_interfaces", "fitting_selection_outcomes"})
_ASSEMBLY_FIELDS = frozenset({"building_assemblies", "assembly_components"})
_FITTING_PRODUCER = "route-fitting-selection-producer"
_FITTING_INPUT_METHOD = "explicit-route-fitting-inputs"
_FITTING_INTENT_METHOD = "explicit-route-fitting-intent"
from .entities import (
    Ceiling, Circuit, Conductor, ElectricalDevice, ElectricalEquipment, Level,
    Obstacle, Opening, Port, Route, RouteConstraint, RouteFitting, Slab, Space, Wall,
)

@dataclass(frozen=True, slots=True, kw_only=True)
class BuildingModel:
    model_id: str
    name: str | None = None
    schema_version: str = SCHEMA_VERSION
    coordinate_system: CoordinateSystem = field(default_factory=CoordinateSystem)
    levels: tuple[Level, ...] = ()
    spaces: tuple[Space, ...] = ()
    walls: tuple[Wall, ...] = ()
    slabs: tuple[Slab, ...] = ()
    ceilings: tuple[Ceiling, ...] = ()
    openings: tuple[Opening, ...] = ()
    electrical_equipment: tuple[ElectricalEquipment, ...] = ()
    electrical_devices: tuple[ElectricalDevice, ...] = ()
    ports: tuple[Port, ...] = ()
    obstacles: tuple[Obstacle, ...] = ()
    route_constraints: tuple[RouteConstraint, ...] = ()
    routes: tuple[Route, ...] = ()
    route_fittings: tuple[RouteFitting, ...] = ()
    circuits: tuple[Circuit, ...] = ()
    conductors: tuple[Conductor, ...] = ()
    electrical_boxes: tuple[ElectricalBox, ...] = ()
    box_selection_outcomes: tuple[BoxSelectionOutcome, ...] = ()
    raceway_selections: tuple[RacewaySelection, ...] = ()
    raceway_selection_outcomes: tuple[RacewaySelectionOutcome, ...] = ()
    support_assemblies: tuple[SupportAssembly, ...] = ()
    support_components: tuple[SupportComponent, ...] = ()
    support_interfaces: tuple[SupportInterface, ...] = ()
    support_selection_outcomes: tuple[SupportSelectionOutcome, ...] = ()
    structural_members: tuple[StructuralMember, ...] = ()
    fitting_selections: tuple[FittingSelection, ...] = ()
    fitting_interfaces: tuple[FittingInterface, ...] = ()
    fitting_selection_outcomes: tuple[FittingSelectionOutcome, ...] = ()
    building_assemblies: tuple[BuildingAssembly, ...] = ()
    assembly_components: tuple[AssemblyComponent, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id(self.model_id, "model_id")
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(
                f"unsupported schema_version {self.schema_version!r}"
            )
        if self.schema_version == SCHEMA_VERSION and (self.electrical_boxes or self.box_selection_outcomes):
            raise ContractError("physical electrical objects require explicit schema_version 1.1.0")
        if self.schema_version not in RACEWAY_SCHEMA_VERSIONS and (self.raceway_selections or self.raceway_selection_outcomes):
            raise ContractError("raceway selections require explicit schema_version 1.2.0")
        if self.schema_version not in SUPPORT_SCHEMA_VERSIONS and any(getattr(self,name) for name in _SUPPORT_FIELDS):
            raise ContractError("support inventory requires explicit schema_version 1.3.0")
        if self.schema_version not in STRUCTURAL_SCHEMA_VERSIONS and any(getattr(self, name) for name in _STRUCTURAL_FIELDS):
            raise ContractError("structural members require explicit schema_version 1.4.0")
        if self.schema_version not in FITTING_SCHEMA_VERSIONS and any(getattr(self, name) for name in _FITTING_FIELDS):
            raise ContractError("route-fitting selections require explicit schema_version 1.5.0")
        if self.schema_version not in ASSEMBLY_SCHEMA_VERSIONS and any(getattr(self, name) for name in _ASSEMBLY_FIELDS):
            raise ContractError("building assemblies require explicit schema_version 1.6.0")
        fitting_types = {
            "fitting_selections": FittingSelection,
            "fitting_interfaces": FittingInterface,
            "fitting_selection_outcomes": FittingSelectionOutcome,
        }
        for name, item_type in fitting_types.items():
            collection = getattr(self, name)
            if not isinstance(collection, tuple) or any(not isinstance(item, item_type) for item in collection):
                raise ContractError(f"{name} must be a tuple of typed {item_type.__name__} entities")
        assembly_types = {
            "building_assemblies": BuildingAssembly,
            "assembly_components": AssemblyComponent,
        }
        for name, item_type in assembly_types.items():
            collection = getattr(self, name)
            if not isinstance(collection, tuple) or any(not isinstance(item, item_type) for item in collection):
                raise ContractError(f"{name} must be a tuple of typed {item_type.__name__} entities")
        _validate_json_value(self.attributes)
        validate_model(self)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> BuildingModel:
        version = document.get("schema_version") if isinstance(document, Mapping) else None
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(
                f"unsupported schema_version {version!r}"
            )
        if version == SCHEMA_VERSION and _PHYSICAL_FIELDS.intersection(document):
            raise ContractError("1.0.0 has unknown physical-object fields; explicitly migrate to 1.1.0")
        if version != SCHEMA_VERSION and not _PHYSICAL_FIELDS.issubset(document):
            raise ContractError(f"{version} requires electrical_boxes and box_selection_outcomes arrays")
        if version not in RACEWAY_SCHEMA_VERSIONS and _RACEWAY_FIELDS.intersection(document):
            raise ContractError(f"{version} has unknown raceway-selection fields; explicitly migrate to 1.2.0")
        if version in RACEWAY_SCHEMA_VERSIONS and not _RACEWAY_FIELDS.issubset(document):
            raise ContractError(f"{version} requires raceway_selections and raceway_selection_outcomes arrays")
        if version not in SUPPORT_SCHEMA_VERSIONS and _SUPPORT_FIELDS.intersection(document):
            raise ContractError(f"{version} has unknown support fields; explicitly migrate to 1.3.0")
        if version in SUPPORT_SCHEMA_VERSIONS and not _SUPPORT_FIELDS.issubset(document):
            raise ContractError(f"{version} requires all four support inventory/outcome arrays")
        if version != STRUCTURAL_SCHEMA_VERSION and _STRUCTURAL_FIELDS.intersection(document):
            if version not in STRUCTURAL_SCHEMA_VERSIONS:
                raise ContractError(f"{version} has unknown structural fields; explicitly migrate to 1.4.0")
        if version in STRUCTURAL_SCHEMA_VERSIONS and not _STRUCTURAL_FIELDS.issubset(document):
            raise ContractError(f"{version} requires the structural_members array")
        if version not in FITTING_SCHEMA_VERSIONS and _FITTING_FIELDS.intersection(document):
            raise ContractError(f"{version} has unknown route-fitting fields; explicitly migrate to 1.5.0")
        if version in FITTING_SCHEMA_VERSIONS and not _FITTING_FIELDS.issubset(document):
            raise ContractError("1.5.0 requires all three route-fitting selection/interface arrays")
        if version not in ASSEMBLY_SCHEMA_VERSIONS and _ASSEMBLY_FIELDS.intersection(document):
            raise ContractError(f"{version} has unknown building-assembly fields; explicitly migrate to 1.6.0")
        if version in ASSEMBLY_SCHEMA_VERSIONS and not _ASSEMBLY_FIELDS.issubset(document):
            raise ContractError("1.6.0 requires both building_assemblies and assembly_components arrays")
        return _decode_dataclass(cls, document)

    @classmethod
    def from_json(cls, payload: str | bytes) -> BuildingModel:
        def unique_pairs(rows):
            document = {}
            for key, value in rows:
                if key in document:
                    raise ContractError(f"duplicate JSON key {key!r}")
                document[key] = value
            return document

        try:
            document = json.loads(payload, object_pairs_hook=unique_pairs)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ContractError("model document root must be an object")
        return cls.from_dict(document)

    @classmethod
    def load(cls, path: str | Path) -> BuildingModel:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def dump(self, path: str | Path, *, indent: int = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")


def _entity_collections(model: BuildingModel) -> tuple[tuple[Any, ...], ...]:
    return (
        model.levels,
        model.spaces,
        model.walls,
        model.slabs,
        model.ceilings,
        model.openings,
        model.electrical_equipment,
        model.electrical_devices,
        model.ports,
        model.obstacles,
        model.route_constraints,
        model.routes,
        model.route_fittings,
        model.circuits,
        model.conductors,
        model.electrical_boxes,
        model.box_selection_outcomes,
        model.raceway_selections,
        model.raceway_selection_outcomes,
        model.support_assemblies,
        model.support_components,
        model.support_interfaces,
        model.support_selection_outcomes,
        model.structural_members,
        model.fitting_selections,
        model.fitting_interfaces,
        model.fitting_selection_outcomes,
        model.building_assemblies,
        model.assembly_components,
    )


def validate_model(model: BuildingModel) -> None:
    all_entities = [entity for collection in _entity_collections(model) for entity in collection]
    by_id: dict[str, Entity] = {}
    for entity in all_entities:
        if entity.id == model.model_id:
            raise ContractError(f"entity id {entity.id!r} collides with model_id")
        if entity.id in by_id:
            raise ContractError(f"duplicate entity id {entity.id!r}")
        by_id[entity.id] = entity

    def scope_exists(owner: Any, path: str) -> bool:
        current = owner
        for segment in path.split("."):
            if is_dataclass(current) and not isinstance(current, type):
                if segment not in {item.name for item in fields(current)}:
                    return False
                current = getattr(current, segment)
            elif isinstance(current, Mapping) and segment in current:
                current = current[segment]
            else:
                return False
        return True

    for owner in (model, *all_entities):
        for record in owner.provenance:
            for path in record.scope_paths or ():
                if (owner is model and path.split(".", 1)[0] in _FITTING_FIELDS and
                        model.schema_version not in FITTING_SCHEMA_VERSIONS):
                    raise ContractError(
                        f"model provenance scope path {path!r} requires schema_version 1.5.0"
                    )
                if (owner is model and path.split(".", 1)[0] in _ASSEMBLY_FIELDS and
                        model.schema_version not in ASSEMBLY_SCHEMA_VERSIONS):
                    raise ContractError(
                        f"model provenance scope path {path!r} requires schema_version 1.6.0"
                    )
                if not scope_exists(owner, path):
                    owner_id = model.model_id if owner is model else owner.id
                    raise ContractError(
                        f"{owner_id}.provenance scope path {path!r} does not name a field"
                    )

    levels = {item.id: item for item in model.levels}
    spaces = {item.id: item for item in model.spaces}
    ports = {item.id: item for item in model.ports}
    routes = {item.id: item for item in model.routes}
    fittings = {item.id: item for item in model.route_fittings}
    circuits = {item.id: item for item in model.circuits}

    def require(ref: str | None, mapping: Mapping[str, Any], label: str) -> None:
        if ref is not None and ref not in mapping:
            raise ContractError(f"{label} references missing id {ref!r}")

    selections = {s.id: s for s in model.raceway_selections}
    selected_routes: set[str] = set()
    for selection in model.raceway_selections:
        require(selection.route_id, routes, f"{selection.id}.route_id")
        route = routes[selection.route_id]
        if selection.route_id in selected_routes:
            raise ContractError("a route cannot have multiple whole-route selections")
        selected_routes.add(selection.route_id)
        if selection.route_type != route.route_type or selection.wiring_method != wiring_method_for_route_type(route.route_type):
            raise ContractError("selected wiring method contradicts canonical route type")
        if route.nominal_diameter_m is not None and (selection.nominal_diameter_m is None or
            abs(route.nominal_diameter_m-selection.nominal_diameter_m)>1e-9):
            raise ContractError("selected nominal diameter contradicts the source route diameter")
    selection_intents: set[str] = set()
    outcome_routes: set[str] = set()
    selected_ids: set[str] = set()
    for outcome in model.raceway_selection_outcomes:
        require(outcome.route_id, routes, f"{outcome.id}.route_id")
        if outcome.intent_id in selection_intents or outcome.route_id in outcome_routes:
            raise ContractError("one active raceway outcome is allowed per intent and route")
        selection_intents.add(outcome.intent_id)
        outcome_routes.add(outcome.route_id)
        if outcome.selected_selection_id is not None:
            require(outcome.selected_selection_id, selections, f"{outcome.id}.selected_selection_id")
            selection = selections[outcome.selected_selection_id]
            if (selection.intent_id != outcome.intent_id or selection.route_id != outcome.route_id or
                selection.product != outcome.requested_product or selection.basis != outcome.requested_basis):
                raise ContractError("raceway outcome contradicts its selected route/product/basis")
            selected_ids.add(selection.id)
        elif outcome.route_id in selected_routes:
            raise ContractError("unresolved route already has a whole-route selection")
    if selected_ids != set(selections):
        raise ContractError("every raceway selection requires one active selected outcome")

    for entity in (*model.spaces, *model.walls, *model.slabs, *model.ceilings):
        require(entity.level_id, levels, f"{entity.id}.level_id")
    for member in model.structural_members:
        require(member.level_id, levels, f"{member.id}.level_id")

    hostable = {item.id: item for item in (*model.walls, *model.slabs, *model.ceilings)}
    # Legacy and explicitly empty 1.3 models need no support-only work.
    if (model.support_assemblies or model.support_components or
            model.support_interfaces or model.support_selection_outcomes):
        assemblies = {item.id: item for item in model.support_assemblies}
        components = {item.id: item for item in model.support_components}
        interfaces = {item.id: item for item in model.support_interfaces}
        supportable = {item.id: item for item in (*model.routes, *model.electrical_boxes,
                       *model.electrical_devices, *model.electrical_equipment)}
        physical_keys = set()
        part_keys = set()
        for component in model.support_components:
            require(component.assembly_id, assemblies, f"{component.id}.assembly_id")
            require(component.level_id, levels, f"{component.id}.level_id")
            assembly = assemblies[component.assembly_id]
            if component.id not in assembly.component_ids or component.level_id != assembly.level_id:
                raise ContractError("support component ownership/level contradicts its assembly")
            part_key = (component.assembly_id, component.part_key)
            if part_key in part_keys:
                raise ContractError("duplicate support part key within an assembly")
            part_keys.add(part_key)
            physical_key=component_envelope_key(component)
            if physical_key in physical_keys:
                raise ContractError("coincident duplicate same-product support components")
            physical_keys.add(physical_key)
        interface_keys = set()
        for interface in model.support_interfaces:
            require(interface.assembly_id, assemblies, f"{interface.id}.assembly_id")
            require(interface.host_id, hostable, f"{interface.id}.host_id")
            require(interface.level_id, levels, f"{interface.id}.level_id")
            assembly=assemblies[interface.assembly_id]
            if (interface.id not in assembly.interface_ids or interface.level_id != assembly.level_id or
                hostable[interface.host_id].level_id != interface.level_id):
                raise ContractError("support interface ownership/host level contradicts its assembly")
            interface_key=(interface.assembly_id,interface.interface_key)
            if interface_key in interface_keys:
                raise ContractError("duplicate support interface key")
            interface_keys.add(interface_key)
            for component_id in interface.component_ids:
                require(component_id,components,f"{interface.id}.component_ids")
                component=components[component_id]
                if component.assembly_id != assembly.id:
                    raise ContractError("support interface cannot attach another assembly's component")
                if not component_contains_point(component,interface.pose.position):
                    raise ContractError("support attachment lies outside its named component envelope")
        for assembly in model.support_assemblies:
            require(assembly.level_id,levels,f"{assembly.id}.level_id")
            for component_id in assembly.component_ids:
                require(component_id,components,f"{assembly.id}.component_ids")
                if components[component_id].assembly_id != assembly.id:
                    raise ContractError("component belongs to a different support assembly")
            for interface_id in assembly.interface_ids:
                require(interface_id,interfaces,f"{assembly.id}.interface_ids")
                if interfaces[interface_id].assembly_id != assembly.id:
                    raise ContractError("interface belongs to a different support assembly")
            for object_id in assembly.supported_object_ids:
                require(object_id,supportable,f"{assembly.id}.supported_object_ids")
                object_levels=supported_levels(model,object_id)
                if None in object_levels:
                    raise ContractError("support assembly requires known supported-object/endpoint levels")
                if any(level != assembly.level_id for level in object_levels):
                    raise ContractError("multi-level support is unsupported; known supported-object levels conflict")
        support_intents=set(); selected_assemblies=set()
        for outcome in model.support_selection_outcomes:
            if outcome.intent_id in support_intents:
                raise ContractError("duplicate active support intent")
            support_intents.add(outcome.intent_id)
            require(outcome.requested_level_id,levels,f"{outcome.id}.requested_level_id")
            for object_id in outcome.supported_object_ids:
                require(object_id,supportable,f"{outcome.id}.supported_object_ids")
            if outcome.selected_assembly_id is not None:
                require(outcome.selected_assembly_id,assemblies,f"{outcome.id}.selected_assembly_id")
                assembly=assemblies[outcome.selected_assembly_id]
                if (assembly.intent_id != outcome.intent_id or assembly.level_id != outcome.requested_level_id or
                    assembly.basis != outcome.requested_basis or set(assembly.supported_object_ids)!=set(outcome.supported_object_ids)):
                    raise ContractError("support outcome contradicts its installed assembly")
                selected_assemblies.add(assembly.id)
        if selected_assemblies != set(assemblies):
            raise ContractError("every support assembly requires exactly one active selected outcome")
    for opening in model.openings:
        require(opening.host_id, hostable, f"{opening.id}.host_id")

    validate_building_assemblies(model)

    boxes = {item.id: item for item in model.electrical_boxes}
    devices = {item.id: item for item in model.electrical_devices}
    assigned: set[str] = set()
    for box in model.electrical_boxes:
        require(box.level_id, levels, f"{box.id}.level_id")
        require(box.host_id, hostable, f"{box.id}.host_id")
        if hostable[box.host_id].level_id != box.level_id:
            raise ContractError(f"{box.id}.level_id contradicts its host level")
        for occupant_id in box.occupant_ids:
            require(occupant_id, devices, f"{box.id}.occupant_ids")
            if occupant_id in assigned:
                raise ContractError(f"occupant {occupant_id} belongs to more than one physical box")
            assigned.add(occupant_id)
            occupant = devices[occupant_id]
            if occupant.host_id is not None and occupant.host_id != box.host_id:
                raise ContractError(f"{box.id} contradicts occupant {occupant_id}'s known host")
            if occupant.level_id is not None and occupant.level_id != box.level_id:
                raise ContractError(f"{box.id} contradicts occupant {occupant_id}'s known level")
    intents: set[str] = set()
    selected_boxes: set[str] = set()
    outcome_occupants: set[str] = set()
    for outcome in model.box_selection_outcomes:
        if outcome.intent_id in intents:
            raise ContractError(f"duplicate active box intent {outcome.intent_id}")
        intents.add(outcome.intent_id)
        for occupant_id in outcome.occupant_ids:
            require(occupant_id, devices, f"{outcome.id}.occupant_ids")
            if occupant_id in outcome_occupants:
                raise ContractError(f"occupant {occupant_id} has contradictory box selection outcomes")
            outcome_occupants.add(occupant_id)
        if outcome.selected_box_id is not None:
            require(outcome.selected_box_id, boxes, f"{outcome.id}.selected_box_id")
            box = boxes[outcome.selected_box_id]
            if box.intent_id != outcome.intent_id or set(box.occupant_ids) != set(outcome.occupant_ids):
                raise ContractError(f"{outcome.id} contradicts its selected box intent/occupants")
            if outcome.requested_product != box.product:
                raise ContractError(f"{outcome.id} contradicts its selected catalog product")
            selected_boxes.add(box.id)
        elif set(outcome.occupant_ids).intersection(assigned):
            raise ContractError(f"{outcome.id} is unresolved but its occupants already have a box")
    if selected_boxes != set(boxes):
        raise ContractError("every physical box requires exactly one active selected outcome")

    for item in (*model.electrical_equipment, *model.electrical_devices):
        require(item.level_id, levels, f"{item.id}.level_id")
        require(item.space_id, spaces, f"{item.id}.space_id")
        require(item.host_id, by_id, f"{item.id}.host_id")

    for item in (*model.obstacles, *model.route_constraints):
        require(item.level_id, levels, f"{item.id}.level_id")

    for port in model.ports:
        require(port.owner_id, by_id, f"{port.id}.owner_id")
        if isinstance(by_id.get(port.owner_id),
                      (SupportAssembly, SupportComponent, SupportInterface, SupportSelectionOutcome,
                       BuildingAssembly, AssemblyComponent)):
            raise ContractError("non-electrical support/assembly records cannot own electrical ports")
        if port.owner_id in {outcome.id for outcome in (*model.box_selection_outcomes, *model.raceway_selection_outcomes, *model.raceway_selections)}:
            raise ContractError(f"{port.id} cannot be owned by a nonphysical selection outcome")
        if port.owner_id in boxes and not box_contains_point(boxes[port.owner_id], port.pose.position):
            raise ContractError(f"{port.id} lies outside its physical box envelope")
        if port.owner_id == port.id:
            raise ContractError(f"{port.id} cannot own itself")
        for connected_id in port.connected_port_ids:
            require(connected_id, ports, f"{port.id}.connected_port_ids")
            if connected_id == port.id:
                raise ContractError(f"{port.id} cannot connect to itself")

    for port in model.ports:
        for connected_id in port.connected_port_ids:
            other = ports[connected_id]
            if port.id not in other.connected_port_ids:
                raise ContractError(
                    f"port connectivity must be symmetric: {port.id!r} -> {connected_id!r}"
                )

    for route in model.routes:
        require(route.start_port_id, ports, f"{route.id}.start_port_id")
        require(route.end_port_id, ports, f"{route.id}.end_port_id")
        start = ports[route.start_port_id].pose.position
        end = ports[route.end_port_id].pose.position
        if _distance(route.centerline.points[0], start) > 1e-6:
            raise ContractError(f"{route.id}.centerline must start at the start port position")
        if _distance(route.centerline.points[-1], end) > 1e-6:
            raise ContractError(f"{route.id}.centerline must end at the end port position")
        for fitting_id in route.fitting_ids:
            require(fitting_id, fittings, f"{route.id}.fitting_ids")
            if fittings[fitting_id].route_id != route.id:
                raise ContractError(f"{fitting_id}.route_id does not match route {route.id}")

    for fitting in model.route_fittings:
        require(fitting.route_id, routes, f"{fitting.id}.route_id")
        if fitting.id not in routes[fitting.route_id].fitting_ids:
            raise ContractError(
                f"{fitting.id} must appear in {fitting.route_id}.fitting_ids to preserve fitting order"
            )

    validate_assembly_route_contexts(model)

    for circuit in model.circuits:
        require(circuit.source_port_id, ports, f"{circuit.id}.source_port_id")
        for port_id in circuit.load_port_ids:
            require(port_id, ports, f"{circuit.id}.load_port_ids")
        for route_id in circuit.route_ids:
            require(route_id, routes, f"{circuit.id}.route_ids")

    for conductor in model.conductors:
        require(conductor.circuit_id, circuits, f"{conductor.id}.circuit_id")
        for route_id in conductor.route_ids:
            require(route_id, routes, f"{conductor.id}.route_ids")

    if model.fitting_selections or model.fitting_interfaces or model.fitting_selection_outcomes:
        if model.schema_version not in FITTING_SCHEMA_VERSIONS:
            raise ContractError("route-fitting selection inventory requires explicit schema_version 1.5.0")
        selections = {selection.id: selection for selection in model.fitting_selections}
        outcomes_by_fitting: dict[str, FittingSelectionOutcome] = {}
        outcomes_by_intent: set[str] = set()
        selected_ids: set[str] = set()
        for outcome in model.fitting_selection_outcomes:
            require(outcome.fitting_id, fittings, f"{outcome.id}.fitting_id")
            require(outcome.route_id, routes, f"{outcome.id}.route_id")
            if outcome.intent_id in outcomes_by_intent or outcome.fitting_id in outcomes_by_fitting:
                raise ContractError("one active fitting-selection outcome is allowed per intent and fitting")
            outcomes_by_intent.add(outcome.intent_id)
            outcomes_by_fitting[outcome.fitting_id] = outcome
            fitting = fittings[outcome.fitting_id]
            if fitting.route_id != outcome.route_id:
                raise ContractError("fitting outcome route does not own its fitting")
            if outcome.selected_selection_id is not None:
                require(outcome.selected_selection_id, selections, f"{outcome.id}.selected_selection_id")
                selection = selections[outcome.selected_selection_id]
                if (selection.intent_id != outcome.intent_id or selection.fitting_id != outcome.fitting_id or
                        selection.route_id != outcome.route_id or selection.product != outcome.requested_product or
                        selection.basis != outcome.requested_basis):
                    raise ContractError("fitting outcome contradicts its selected product/rule/fitting")
                selected_ids.add(selection.id)
            elif outcome.fitting_id in {selection.fitting_id for selection in model.fitting_selections}:
                raise ContractError("unresolved fitting outcome cannot retain a selected product")
        if selected_ids != set(selections):
            raise ContractError("every fitting selection requires exactly one active selected outcome")
        for selection in model.fitting_selections:
            require(selection.fitting_id, fittings, f"{selection.id}.fitting_id")
            require(selection.route_id, routes, f"{selection.id}.route_id")
            fitting, route = fittings[selection.fitting_id], routes[selection.route_id]
            if fitting.route_id != route.id or route.fitting_ids.count(fitting.id) != 1:
                raise ContractError("selected fitting must be listed exactly once by its owning route")
            if (fitting.fitting_type != selection.fitting_type or
                    route.route_type != selection.route_type or
                    selection.route_type != "emt" or selection.fitting_type not in {"coupling", "connector"}):
                raise ContractError("selected fitting type/route type is outside the 1.5 supported topology")
            consolidation = fitting.attributes.get("consolidation")
            if ((isinstance(consolidation, dict) and consolidation.get("nonmaterial") is True) or
                    fitting.fitting_type == "logical-junction"):
                raise ContractError("nonmaterial logical junctions cannot receive fitting product selections")
            outcome = outcomes_by_fitting.get(fitting.id)
            if outcome is None or outcome.selected_selection_id != selection.id:
                raise ContractError("fitting selection has no matching selected outcome")

        interfaces_by_selection: dict[str, list[FittingInterface]] = {}
        interface_keys: set[tuple[str, str]] = set()
        used_fitting_ports: set[str] = set()
        used_mating_ports: set[str] = set()
        used_pair_ids: set[tuple[str, str]] = set()
        for interface in model.fitting_interfaces:
            require(interface.selection_id, selections, f"{interface.id}.selection_id")
            selection = selections[interface.selection_id]
            require(interface.port_id, ports, f"{interface.id}.port_id")
            require(interface.mating_port_id, ports, f"{interface.id}.mating_port_id")
            own_port, mate_port = ports[interface.port_id], ports[interface.mating_port_id]
            if own_port.owner_id != selection.fitting_id:
                raise ContractError("fitting interface port must be owned by its selected fitting")
            if interface.port_id in used_fitting_ports or interface.mating_port_id in used_mating_ports:
                raise ContractError("fitting and counterpart ports cannot be reused by another interface")
            used_fitting_ports.add(interface.port_id)
            used_mating_ports.add(interface.mating_port_id)
            key = (interface.selection_id, interface.interface_key)
            if key in interface_keys:
                raise ContractError("fitting interface keys must be unique within a selection")
            interface_keys.add(key)
            if (interface.selection_id, interface.mating_pair_id) in used_pair_ids:
                raise ContractError("fitting mating-pair ids must be unique within a selection")
            used_pair_ids.add((interface.selection_id, interface.mating_pair_id))
            if (interface.port_id not in mate_port.connected_port_ids or
                    interface.mating_port_id not in own_port.connected_port_ids):
                raise ContractError("fitting interface pair must already be symmetrically connected")
            if (own_port.connected_port_ids != (mate_port.id,) or
                    mate_port.connected_port_ids != (own_port.id,)):
                raise ContractError("fitting interface connectivity must be exclusive one-to-one")
            if _fitting_distance(own_port.pose.position, mate_port.pose.position) > _FITTING_PORT_POSITION_TOLERANCE_M:
                raise ContractError("fitting and counterpart interface ports are not colocated")
            dot = _normalized_dot(own_port.direction, mate_port.direction)
            if dot > -1.0 + _FITTING_PORT_DIRECTION_DOT_TOLERANCE:
                raise ContractError("fitting and counterpart port directions must oppose")
            if interface.role == "raceway":
                if mate_port.owner_id != selection.route_id:
                    raise ContractError("raceway counterpart port must be owned by the selected route")
                if not _point_on_route(routes[selection.route_id], mate_port.pose.position,
                                       _FITTING_PORT_POSITION_TOLERANCE_M):
                    raise ContractError("route-owned fitting counterpart must lie on the route centerline")
                if not _port_matches_route_tangent(routes[selection.route_id], mate_port,
                                                   _FITTING_PORT_POSITION_TOLERANCE_M,
                                                   _FITTING_PORT_DIRECTION_DOT_TOLERANCE):
                    raise ContractError("route-owned counterpart direction must follow the local centerline")
                route_outcome = next((item for item in model.raceway_selection_outcomes
                                      if item.route_id == selection.route_id), None)
                route_selections = {item.id: item for item in model.raceway_selections}
                if route_outcome is None or route_outcome.status != "selected" or route_outcome.selected_selection_id is None:
                    raise ContractError("fitting raceway interface requires a selected whole-route product")
                raceway_selection = route_selections[route_outcome.selected_selection_id]
                if raceway_selection.product != interface.mating_product:
                    raise ContractError("fitting raceway interface product does not match exact route product")
            elif interface.role == "enclosure":
                box = boxes.get(mate_port.owner_id)
                if box is None or box.product != interface.mating_product:
                    raise ContractError("enclosure counterpart must be an exact-product electrical-box port")
            interfaces_by_selection.setdefault(interface.selection_id, []).append(interface)

        for selection in model.fitting_selections:
            interfaces = interfaces_by_selection.get(selection.id, [])
            fitting = fittings[selection.fitting_id]
            fitting_ports_for_owner = {p.id for p in model.ports if p.owner_id == fitting.id}
            if len(interfaces) != 2 or len({item.role for item in interfaces}) not in {1, 2}:
                raise ContractError("selected fitting requires exactly two explicit interface pairs")
            if {item.port_id for item in interfaces} != fitting_ports_for_owner or len(fitting_ports_for_owner) != 2:
                raise ContractError("selected fitting must have exactly two caller-authored canonical ports")
            roles = {item.role for item in interfaces}
            if selection.fitting_type == "coupling" and roles != {"raceway"}:
                raise ContractError("an EMT coupling requires two raceway interfaces")
            if selection.fitting_type == "connector" and roles != {"raceway", "enclosure"}:
                raise ContractError("an EMT terminal connector requires one raceway and one enclosure interface")
            if not _point_on_route(routes[selection.route_id], fitting.pose.position,
                                   _FITTING_PORT_POSITION_TOLERANCE_M):
                raise ContractError("selected fitting pose must lie on its route centerline")
        for outcome in model.fitting_selection_outcomes:
            if outcome.status == "unresolved" and any(item.fitting_id == outcome.fitting_id
                                                        for item in model.fitting_selections):
                raise ContractError("unresolved fitting outcome cannot retain product selection")
    _validate_fitting_field_evidence(model)
    _validate_fitting_producer_receipts(model)


_FITTING_PORT_POSITION_TOLERANCE_M = 1e-6
_FITTING_PORT_DIRECTION_DOT_TOLERANCE = 1e-6


def _fitting_distance(first: Point3, second: Point3) -> float:
    """Stable fitting-only Euclidean distance; overflow means a mismatch."""
    delta = (first.x - second.x, first.y - second.y, first.z - second.z)
    if any(not math.isfinite(value) for value in delta):
        return math.inf
    distance = math.hypot(*delta)
    return distance if math.isfinite(distance) else math.inf


def _portable_claim(value: Any) -> Any:
    """Return the same JSON claim form used by the canonical model codec."""
    return _encode(value)


def _claim_matches(value: Any, claim: Any) -> bool:
    return _portable_claim(value) == claim


def _producer_records(entity: Any, method: str | None = None) -> list[Provenance]:
    return [record for record in entity.provenance
            if record.source_kind == _FITTING_PRODUCER and (method is None or record.method == method)]


def _exactly_one(records: list[Provenance], label: str) -> Provenance:
    if len(records) != 1:
        raise ContractError(f"producer-owned fitting data requires exactly one {label} receipt")
    return records[0]


def _check_producer_record(record: Provenance, model: BuildingModel, label: str, *,
                           source_element_id: str | None, method: str, derivation: str,
                           scope_paths: tuple[str, ...]) -> None:
    if (record.source_kind != _FITTING_PRODUCER or record.source_id != model.model_id or
            record.source_element_id != source_element_id or record.method != method or
            record.derivation != derivation or record.scope_paths != scope_paths):
        raise ContractError(f"{label} receipt has invalid producer ownership or exact field scope")


def _check_catalog_manifest_receipts(rows: Any, digest: str, label: str) -> None:
    if (not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows) or
            any(not isinstance(row.get("attributes"), dict) or
                row["attributes"].get("manifest_sha256") != digest for row in rows)):
        raise ContractError(f"{label} evidence is not bound to the consumed raw catalog snapshot")


def _fitting_claim_fields(entity: Any) -> set[str]:
    """Canonical facts that require source-scoped evidence in contract 1.5."""
    if isinstance(entity, FittingSelection):
        fields = {"intent_id", "fitting_id", "route_id", "product", "route_type", "fitting_type",
                  "basis", "geometry_basis"}
        if entity.size is not None:
            fields.add("size")
        return fields
    if isinstance(entity, FittingInterface):
        fields = {"selection_id", "interface_key", "role", "connection_method",
                  "permitted_wiring_methods", "port_id", "mating_port_id", "mating_product",
                  "mating_interface_key", "mating_pair_id"}
        for name in ("nominal_designation", "actual_accepted_od_m", "dry_location", "wet_location",
                     "raintight"):
            if getattr(entity, name) is not None:
                fields.add(name)
        if entity.listing_refs:
            fields.add("listing_refs")
        return fields
    if isinstance(entity, FittingSelectionOutcome):
        fields = {"intent_id", "fitting_id", "route_id", "status", "selected_selection_id",
                  "reason_codes", "actual_od_check_performed"}
        if entity.requested_product is not None:
            fields.add("requested_product")
        if entity.requested_basis is not None:
            fields.add("requested_basis")
        return fields
    return set()


def _fitting_allowed_scopes(entity: Any) -> set[str]:
    """Known claim paths, including optional fields explicitly recorded null."""
    scopes = _fitting_claim_fields(entity)
    if isinstance(entity, FittingSelection):
        return scopes | {"size"}
    if isinstance(entity, FittingInterface):
        return scopes | {"nominal_designation", "actual_accepted_od_m", "dry_location", "wet_location",
                         "raintight", "listing_refs"}
    if isinstance(entity, FittingSelectionOutcome):
        return scopes | {"requested_product", "requested_basis"}
    return scopes


def _validate_fitting_field_evidence(model: BuildingModel) -> None:
    """Require field evidence for any selected-state claim, regardless of producer."""
    for entity in (*model.fitting_selections, *model.fitting_interfaces,
                   *model.fitting_selection_outcomes):
        required = _fitting_claim_fields(entity)
        coverage: set[str] = set()
        if not entity.provenance:
            raise ContractError(f"{entity.id} requires field-scoped evidence for its fitting claims")
        for record in entity.provenance:
            scopes = record.scope_paths
            if record.derivation is None or not isinstance(scopes, tuple) or not scopes:
                raise ContractError(f"{entity.id} requires nonempty field-scoped fitting evidence")
            for scope in scopes:
                if scope not in _fitting_allowed_scopes(entity) | {
                        "id", "name", "confidence", "attributes", "provenance", "kind"}:
                    raise ContractError(f"{entity.id} fitting evidence has unknown scope {scope!r}")
                coverage.add(scope)
        missing = required - coverage
        if missing:
            raise ContractError(f"{entity.id} is missing field-scoped evidence for {', '.join(sorted(missing))}")


def _validate_fitting_producer_receipts(model: BuildingModel) -> None:
    """Check internal consistency of this selector's receipts, not source authenticity.

    Directly authored inventory from another owner remains valid without these
    receipts. Once any selector-owned marker is present, all required receipts
    for its active intents/outputs must be present and must agree with fields.
    """
    entities = (*model.fitting_selections, *model.fitting_interfaces,
                *model.fitting_selection_outcomes)
    has_producer_claims = any(_producer_records(entity) for entity in entities)
    input_records = [record for record in model.provenance
                     if record.source_kind == _FITTING_PRODUCER and record.method == _FITTING_INPUT_METHOD]
    if not has_producer_claims and not input_records:
        return
    input_record = _exactly_one(input_records, "top-level selector input")
    _check_producer_record(input_record, model, "selector input", source_element_id=None,
        method=_FITTING_INPUT_METHOD, derivation="user",
        scope_paths=("fitting_selections", "fitting_interfaces", "fitting_selection_outcomes"))
    attributes = input_record.attributes
    digest = attributes.get("catalog_manifest_sha256")
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(char not in "0123456789abcdef" for char in digest) or
            attributes.get("actual_od_check_performed") is not False or
            attributes.get("automatic_port_or_geometry_created") is not False):
        raise ContractError("selector input receipt has invalid catalog or non-inference claims")
    intent_rows = attributes.get("consumed_intents")
    if not isinstance(intent_rows, list) or any(not isinstance(row, dict) for row in intent_rows):
        raise ContractError("selector input receipt must retain its exact intent list")
    if any(not isinstance(row.get(key), str) or not row.get(key)
           for row in intent_rows for key in ("fitting_id", "intent_id")):
        raise ContractError("selector input intents must have unique stable identities")
    if intent_rows != sorted(intent_rows, key=lambda row: (row.get("fitting_id", ""), row.get("intent_id", ""))):
        raise ContractError("selector input intent receipts must be deterministically ordered")
    intent_ids = [row.get("intent_id") for row in intent_rows]
    fitting_ids = [row.get("fitting_id") for row in intent_rows]
    if len(intent_ids) != len(set(intent_ids)) or len(fitting_ids) != len(set(fitting_ids)):
        raise ContractError("selector input intents must have unique stable identities")

    outcomes_by_intent = {outcome.intent_id: outcome for outcome in model.fitting_selection_outcomes}
    selections_by_intent = {selection.intent_id: selection for selection in model.fitting_selections}
    fittings_by_id = {fitting.id: fitting for fitting in model.route_fittings}
    ports_by_id = {port.id: port for port in model.ports}
    interfaces_by_selection: dict[str, list[FittingInterface]] = {}
    for interface in model.fitting_interfaces:
        interfaces_by_selection.setdefault(interface.selection_id, []).append(interface)
    if len(outcomes_by_intent) != len(model.fitting_selection_outcomes):
        raise ContractError("producer-owned fitting outcomes have duplicate intent identities")
    if len(selections_by_intent) != len(model.fitting_selections):
        raise ContractError("producer-owned fitting selections have duplicate intent identities")
    if set(outcomes_by_intent) != set(intent_ids):
        raise ContractError("producer fitting outcomes do not match the consumed intent list")
    selected_intents = {outcome.intent_id for outcome in model.fitting_selection_outcomes
                        if outcome.status == "selected"}
    if set(selections_by_intent) != selected_intents:
        raise ContractError("producer fitting selections do not match selected outcomes")

    def check_rule_claim(rule_claim: Any, rule_records: Any, intent: dict[str, Any]) -> None:
        if rule_claim is None:
            if rule_records != []:
                raise ContractError("missing caller rule cannot retain rule evidence receipts")
            return
        if not isinstance(rule_claim, dict) or not isinstance(rule_records, list):
            raise ContractError("producer receipt has malformed caller rule claims")
        if rule_claim.get("reference") != intent.get("basis"):
            raise ContractError("producer rule receipt contradicts the exact requested basis")
        if (intent.get("requested_product") is not None and
                intent.get("requested_product") not in rule_claim.get("permitted_products", [])):
            raise ContractError("producer rule receipt does not permit the exact requested product")
        if type(rule_claim.get("check_actual_od")) is not bool:
            raise ContractError("producer rule must retain its explicit actual-OD requirement")
        fitting = fittings_by_id.get(intent.get("fitting_id"))
        topology = {("emt", "coupling"), ("emt", "connector")}
        if fitting is None or ("emt", fitting.fitting_type) not in topology:
            raise ContractError("producer rule claims an unsupported fitting topology")
        if (not isinstance(rule_claim.get("provenance"), list) or
                any(not isinstance(row, dict) for row in rule_claim["provenance"]) or
                rule_records != rule_claim["provenance"]):
            raise ContractError("producer rule receipt does not retain its exact field evidence")
        if (not isinstance(rule_claim.get("permitted_products"), list) or
                any(not isinstance(row, dict) or set(row) != {
                    "catalog_id", "catalog_revision", "product_id"}
                    for row in rule_claim["permitted_products"]) or
                not isinstance(rule_claim.get("permitted_topologies"), list) or
                any(not isinstance(row, dict) for row in rule_claim["permitted_topologies"]) or
                not isinstance(rule_claim.get("required_environment"), list) or
                not isinstance(rule_claim.get("required_listing_refs"), list)):
            raise ContractError("producer rule receipt has malformed explicit permission fields")

    for intent in intent_rows:
        outcome = outcomes_by_intent.get(intent["intent_id"])
        if outcome is None or outcome.fitting_id != intent["fitting_id"]:
            raise ContractError("selector intent receipt has no matching fitting outcome")
        caller = _exactly_one(_producer_records(outcome, _FITTING_INTENT_METHOD),
                              "outcome caller-intent")
        _check_producer_record(caller, model, "outcome caller-intent", source_element_id=outcome.intent_id,
            method=_FITTING_INTENT_METHOD, derivation="user",
            scope_paths=("intent_id", "fitting_id", "requested_product", "requested_basis"))
        if not _claim_matches(intent, caller.attributes.get("consumed_intent")):
            raise ContractError("outcome caller receipt contradicts the consumed intent")
        if (not _claim_matches(outcome.requested_product, intent.get("requested_product")) or
                not _claim_matches(outcome.requested_basis, intent.get("basis"))):
            raise ContractError("outcome product or basis contradicts the consumed intent")
        decision = _exactly_one(_producer_records(outcome, "exact-route-fitting-selection-outcome"),
                                "selection decision")
        _check_producer_record(decision, model, "outcome decision", source_element_id=outcome.intent_id,
            method="exact-route-fitting-selection-outcome", derivation="inferred",
            scope_paths=("route_id", "status", "selected_selection_id", "reason_codes",
                         "actual_od_check_performed"))
        selected_id = outcome.selected_selection_id
        rule_claim = decision.attributes.get("consumed_caller_rule")
        rule_records = decision.attributes.get("consumed_caller_rule_provenance")
        check_rule_claim(rule_claim, rule_records, intent)
        if (decision.attributes.get("consumed_selection_id") != selected_id or
                decision.attributes.get("catalog_manifest_sha256") != digest or
                decision.attributes.get("actual_od_check_performed") is not False or
                decision.attributes.get("unresolved_reasons") != list(outcome.reason_codes) or
                outcome.actual_od_check_performed is not False):
            raise ContractError("outcome decision receipt contradicts its canonical fields or pinned catalog")
        expected_rule_evidence = [] if rule_claim is None else [
            {**row, "scope_paths": ["requested_basis"],
             "attributes": {**row.get("attributes", {}), "consumed_caller_rule": rule_claim}}
            for row in rule_claim.get("provenance", []) if "reference" in row.get("scope_paths", [])]
        outcome_rule_records = [record for record in outcome.provenance
            if record.scope_paths == ("requested_basis",) and
               record.attributes.get("consumed_caller_rule") is not None]
        if sorted((_portable_claim(row) for row in outcome_rule_records), key=lambda row: json.dumps(row, sort_keys=True)) != \
                sorted(expected_rule_evidence, key=lambda row: json.dumps(row, sort_keys=True)):
            raise ContractError("outcome rule basis evidence is stale or was widened")
        selection = selections_by_intent.get(outcome.intent_id)
        if selected_id is None:
            if selection is not None or outcome.status != "unresolved":
                raise ContractError("unresolved producer outcome cannot retain a selection")
            if any(item.fitting_id == outcome.fitting_id for item in model.fitting_interfaces):
                raise ContractError("unresolved producer outcome cannot retain interfaces")
            continue
        if (selection is None or selected_id != selection.id or outcome.status != "selected" or
                selection.fitting_id != intent["fitting_id"] or
                not _claim_matches(selection.product, intent.get("requested_product")) or
                not _claim_matches(selection.basis, intent.get("basis"))):
            raise ContractError("selected producer output contradicts its exact intent")
        selected_caller = _exactly_one(_producer_records(selection, _FITTING_INTENT_METHOD),
                                       "selection caller-intent")
        _check_producer_record(selected_caller, model, "selection caller-intent",
            source_element_id=outcome.intent_id, method=_FITTING_INTENT_METHOD, derivation="user",
            scope_paths=("intent_id", "fitting_id"))
        if not _claim_matches(intent, selected_caller.attributes.get("consumed_intent")):
            raise ContractError("selection caller receipt contradicts the exact intent")
        selected_receipt = _exactly_one(_producer_records(selection, "exact-route-fitting-selection"),
                                        "selected product")
        _check_producer_record(selected_receipt, model, "selected product",
            source_element_id=outcome.intent_id, method="exact-route-fitting-selection", derivation="inferred",
            scope_paths=("route_id", "route_type", "fitting_type", "geometry_basis"))
        entry = selected_receipt.attributes.get("consumed_catalog_entry")
        if (not isinstance(entry, dict) or entry.get("manifest_sha256") != digest or
                selected_receipt.attributes.get("catalog_manifest_sha256") != digest or
                not _claim_matches(selection.product, entry.get("product")) or
                selection.route_type != entry.get("route_type") or
                selection.fitting_type != entry.get("fitting_type") or
                selection.geometry_basis != entry.get("geometry_basis") or
                not _claim_matches(selection.size, entry.get("size")) or
                not isinstance(entry.get("interfaces"), list) or len(entry["interfaces"]) != 2 or
                not isinstance(entry.get("mating_pairs"), list) or
                selected_receipt.attributes.get("actual_od_check_performed") is not False):
            raise ContractError("selected product receipt contradicts its exact catalog entry")
        if (not isinstance(entry.get("provenance"), list) or
                any(not isinstance(row, dict) for row in entry["provenance"]) or
                any(not isinstance(row, dict) for row in entry["interfaces"]) or
                any(not isinstance(row, dict) for row in entry["mating_pairs"])):
            raise ContractError("selected product receipt has malformed catalog evidence")
        _check_catalog_manifest_receipts(entry["provenance"], digest, "catalog entry")
        if (selected_receipt.attributes.get("consumed_caller_rule") != rule_claim or
                selected_receipt.attributes.get("consumed_caller_rule_provenance") != rule_records or
                not _claim_matches(selection.basis, rule_claim.get("reference") if isinstance(rule_claim, dict) else None)):
            raise ContractError("selected product receipt contradicts caller rule evidence")
        if (rule_claim.get("check_actual_od") is not False or
                _portable_claim(selection.product) not in rule_claim.get("permitted_products", []) or
                {("emt", selection.fitting_type)} - {
                    (row.get("route_type"), row.get("fitting_type"))
                    for row in rule_claim.get("permitted_topologies", []) if isinstance(row, dict)}):
            raise ContractError("selected product is not permitted by the explicit caller rule")
        expected_entry_evidence = entry.get("provenance")
        selected_entry_evidence = [record for record in selection.provenance
                                   if record.source_kind == "catalog-manifest"]
        if not isinstance(expected_entry_evidence, list) or sorted(
                (_portable_claim(record) for record in selected_entry_evidence),
                key=lambda row: json.dumps(row, sort_keys=True)) != sorted(
                expected_entry_evidence, key=lambda row: json.dumps(row, sort_keys=True)):
            raise ContractError("selected catalog field evidence differs from its consumed entry receipt")
        expected_rule_evidence = [] if rule_claim is None else [
            {**row, "scope_paths": ["basis"],
             "attributes": {**row.get("attributes", {}), "consumed_caller_rule": rule_claim,
                            "rule_compliance_verified": False}}
            for row in rule_claim.get("provenance", []) if "reference" in row.get("scope_paths", [])]
        selection_rule_records = [record for record in selection.provenance
            if record.scope_paths == ("basis",) and
               record.attributes.get("consumed_caller_rule") is not None]
        if sorted((_portable_claim(row) for row in selection_rule_records), key=lambda row: json.dumps(row, sort_keys=True)) != \
                sorted(expected_rule_evidence, key=lambda row: json.dumps(row, sort_keys=True)):
            raise ContractError("selection basis evidence is stale or was widened")
        selection_interfaces = interfaces_by_selection.get(selection.id, [])
        if len(selection_interfaces) != 2:
            raise ContractError("selected producer product requires two evidenced interface records")
        catalog_interfaces = {row.get("key"): row for row in entry["interfaces"] if isinstance(row, dict)}
        if len(catalog_interfaces) != 2:
            raise ContractError("selected product receipt has malformed exact interface rows")
        intent_bindings = {row.get("interface_key"): row for row in intent.get("bindings", [])
                           if isinstance(row, dict)}
        if len(intent_bindings) != 2:
            raise ContractError("selected producer product requires two caller-authored bindings")
        selection_pair_ids = selected_receipt.attributes.get("mating_pair_ids")
        if not isinstance(selection_pair_ids, list) or len(selection_pair_ids) != 2:
            raise ContractError("selected product receipt must retain exactly two mating-pair IDs")
        emitted_pair_ids = []
        for interface in selection_interfaces:
            row = catalog_interfaces.get(interface.interface_key)
            binding = intent_bindings.get(interface.interface_key)
            if row is None or binding is None:
                raise ContractError("producer interface has no exact catalog row or caller binding")
            _check_catalog_manifest_receipts(row.get("provenance"), digest, "fitting interface")
            catalog_claim = {
                "key": row.get("key"), "role": row.get("role"),
                "connection_method": row.get("connection_method"),
                "permitted_wiring_methods": row.get("permitted_wiring_methods"),
                "nominal_designation": row.get("nominal_designation"),
                "actual_accepted_od_m": row.get("actual_accepted_od_m"),
                "dry_location": row.get("dry_location"), "wet_location": row.get("wet_location"),
                "raintight": row.get("raintight"), "listing_refs": row.get("listing_refs"),
            }
            if (interface.role != row.get("role") or
                    interface.connection_method != row.get("connection_method") or
                    not _claim_matches(interface.permitted_wiring_methods, row.get("permitted_wiring_methods")) or
                    interface.nominal_designation != row.get("nominal_designation") or
                    not _claim_matches(interface.actual_accepted_od_m, row.get("actual_accepted_od_m")) or
                    interface.dry_location != row.get("dry_location") or
                    interface.wet_location != row.get("wet_location") or
                    interface.raintight != row.get("raintight") or
                    not _claim_matches(interface.listing_refs, row.get("listing_refs")) or
                    interface.port_id != binding.get("fitting_port_id") or
                    interface.mating_port_id != binding.get("counterpart_port_id")):
                raise ContractError("producer interface fields contradict their catalog or caller receipts")
            for required_fact in rule_claim.get("required_environment", []):
                if row.get(required_fact) is not True:
                    raise ContractError("selected product does not satisfy the explicit environment requirement")
            for required_listing in rule_claim.get("required_listing_refs", []):
                if required_listing not in row.get("listing_refs", []):
                    raise ContractError("selected product does not satisfy the explicit listing requirement")
            interface_records = [record for record in interface.provenance
                                 if "consumed_catalog_interface" in record.attributes]
            expected_interface_records = []
            for original in row.get("provenance", []):
                expected = {**original, "attributes": {**original.get("attributes", {}),
                    "consumed_catalog_interface": catalog_claim, "catalog_manifest_sha256": digest}}
                expected_interface_records.append(expected)
            if sorted((_portable_claim(record) for record in interface_records),
                      key=lambda item: json.dumps(item, sort_keys=True)) != sorted(
                      expected_interface_records, key=lambda item: json.dumps(item, sort_keys=True)):
                raise ContractError("producer interface receipt does not preserve exact catalog facts")
            binding_record = _exactly_one([record for record in _producer_records(interface, _FITTING_INTENT_METHOD)
                if record.attributes.get("consumed_port_binding") is not None], "interface caller binding")
            _check_producer_record(binding_record, model, "interface caller binding",
                source_element_id=outcome.intent_id, method=_FITTING_INTENT_METHOD, derivation="user",
                scope_paths=("interface_key", "mating_interface_key", "port_id", "mating_port_id"))
            expected_binding = binding
            if (not _claim_matches(expected_binding, binding_record.attributes.get("consumed_port_binding")) or
                    not _claim_matches(intent, binding_record.attributes.get("consumed_intent")) or
                    interface.interface_key != binding_record.attributes.get("consumed_port_binding", {}).get("interface_key")):
                raise ContractError("interface caller binding receipt contradicts the exact intent")
            pair = next((pair for pair in entry["mating_pairs"]
                         if isinstance(pair, dict) and pair.get("id") == interface.mating_pair_id), None)
            if (pair is None or pair.get("interface_key") != interface.interface_key or
                    pair.get("mating_interface_key") != interface.mating_interface_key or
                    not _claim_matches(interface.mating_product, pair.get("mating_product")) or
                    interface.mating_interface_key != binding.get("mating_interface_key")):
                raise ContractError("selected interface mating fields contradict exact pair evidence")
            emitted_pair_ids.append(interface.mating_pair_id)
            pair_records = [record for record in interface.provenance
                            if "consumed_mating_pair" in record.attributes]
            expected_pair_records = []
            _check_catalog_manifest_receipts(pair.get("provenance"), digest, "mating pair")
            for original in pair.get("provenance", []):
                expected = {**original, "attributes": {**original.get("attributes", {}),
                    "consumed_mating_pair": pair, "catalog_manifest_sha256": digest}}
                expected_pair_records.append(expected)
            if sorted((_portable_claim(record) for record in pair_records),
                      key=lambda item: json.dumps(item, sort_keys=True)) != sorted(
                      expected_pair_records, key=lambda item: json.dumps(item, sort_keys=True)):
                raise ContractError("selected interface is missing exact mating-pair evidence receipts")
            for record in pair_records:
                scope = record.scope_paths or ()
                expected_field = {
                    "mating_pair_id": pair.get("id"), "interface_key": pair.get("interface_key"),
                    "mating_product": pair.get("mating_product"),
                    "mating_interface_key": pair.get("mating_interface_key"),
                }
                if (len(scope) != 1 or scope[0] not in expected_field or
                        record.attributes.get("consumed_mating_pair") != pair or
                        record.attributes.get("catalog_manifest_sha256") != digest):
                    raise ContractError("mating-pair provenance is stale or was widened")
            owner = _exactly_one([record for record in interface.provenance
                if record.method == "exact-counterpart-product-binding"], "counterpart product")
            owner_id = ports_by_id[interface.mating_port_id].owner_id
            _check_producer_record(owner, model, "counterpart product", source_element_id=owner_id,
                method="exact-counterpart-product-binding", derivation="inferred",
                scope_paths=("mating_product",))
            if (owner.attributes.get("consumed_mating_product") != _portable_claim(interface.mating_product) or
                    owner.attributes.get("consumed_owner_id") != owner_id):
                raise ContractError("counterpart product receipt contradicts the exact existing owner")
            link = _exactly_one([record for record in interface.provenance
                if record.method == "fitting-interface-selection-link"], "selection link")
            _check_producer_record(link, model, "interface selection link", source_element_id=selection.id,
                method="fitting-interface-selection-link", derivation="inferred",
                scope_paths=("selection_id",))
            if interface.selection_id != selection.id:
                raise ContractError("interface selection link receipt is inconsistent")
        if (len(set(selection_pair_ids)) != 2 or sorted(selection_pair_ids) != sorted(emitted_pair_ids)):
            raise ContractError("selected product receipt mating-pair list is stale")


def _normalized_dot(first, second) -> float:
    first_unit = _unit_vector(first)
    second_unit = _unit_vector(second)
    dot = sum(a * b for a, b in zip(first_unit, second_unit))
    if not math.isfinite(dot):
        raise ContractError("fitting interface direction comparison is undefined")
    return max(-1.0, min(1.0, dot))


def _unit_vector(vector) -> tuple[float, float, float]:
    components = (vector.x, vector.y, vector.z)
    scale = max(abs(value) for value in components)
    if scale == 0.0 or not math.isfinite(scale):
        raise ContractError("fitting interface directions must be finite and nonzero")
    scaled = tuple(value / scale for value in components)
    magnitude = math.sqrt(sum(value * value for value in scaled))
    if magnitude == 0.0 or not math.isfinite(magnitude):
        raise ContractError("fitting interface direction normalization is undefined")
    return tuple(value / magnitude for value in scaled)


def _point_segment_distance(point: Point3, start: Point3, end: Point3) -> float:
    scale = max(abs(value) for value in (point.x, point.y, point.z,
        start.x, start.y, start.z, end.x, end.y, end.z))
    if scale == 0.0:
        return 0.0
    vector = (end.x / scale - start.x / scale,
              end.y / scale - start.y / scale,
              end.z / scale - start.z / scale)
    offset = (point.x / scale - start.x / scale,
              point.y / scale - start.y / scale,
              point.z / scale - start.z / scale)
    squared = sum(value * value for value in vector)
    if squared == 0.0:
        residual = offset
    else:
        fraction = max(0.0, min(1.0, sum(a * b for a, b in zip(offset, vector)) / squared))
        residual = tuple(offset[i] - fraction * vector[i] for i in range(3))
    distance_scaled = math.sqrt(sum(value * value for value in residual))
    distance = distance_scaled * scale
    return distance if math.isfinite(distance) else math.inf


def _route_segments(route: Route):
    points = route.centerline.points
    return tuple((start, end) for start, end in zip(points, points[1:]))


def _point_on_route(route: Route, point: Point3, tolerance_m: float) -> bool:
    return any(_point_segment_distance(point, start, end) <= tolerance_m
               for start, end in _route_segments(route))


def _port_matches_route_tangent(route: Route, port: Port, tolerance_m: float,
                                dot_tolerance: float) -> bool:
    for start, end in _route_segments(route):
        if _point_segment_distance(port.pose.position, start, end) > tolerance_m:
            continue
        scale = max(abs(value) for value in (start.x, start.y, start.z, end.x, end.y, end.z))
        if scale == 0.0:
            continue
        delta = (end.x / scale - start.x / scale,
                 end.y / scale - start.y / scale,
                 end.z / scale - start.z / scale)
        delta_scale = max(abs(value) for value in delta)
        if delta_scale == 0.0:
            continue
        scaled = tuple(value / delta_scale for value in delta)
        tangent_length = math.sqrt(sum(value * value for value in scaled))
        if tangent_length == 0.0 or not math.isfinite(tangent_length):
            continue
        tangent = tuple(value / tangent_length for value in scaled)
        direction = _unit_vector(port.direction)
        dot = abs(sum(a * b for a, b in zip(direction, tangent)))
        if not math.isfinite(dot):
            continue
        if dot >= 1.0 - dot_tolerance:
            return True
    return False


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        encoded = {item.name: _encode(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, BuildingModel) and value.schema_version == SCHEMA_VERSION:
            for name in _PHYSICAL_FIELDS:
                encoded.pop(name)
        if isinstance(value, BuildingModel) and value.schema_version not in RACEWAY_SCHEMA_VERSIONS:
            for name in _RACEWAY_FIELDS:
                encoded.pop(name)
        if isinstance(value, BuildingModel) and value.schema_version not in SUPPORT_SCHEMA_VERSIONS:
            for name in _SUPPORT_FIELDS:
                encoded.pop(name)
        if isinstance(value, BuildingModel) and value.schema_version != STRUCTURAL_SCHEMA_VERSION:
            if value.schema_version not in STRUCTURAL_SCHEMA_VERSIONS:
                for name in _STRUCTURAL_FIELDS:
                    encoded.pop(name)
        if isinstance(value, BuildingModel) and value.schema_version not in FITTING_SCHEMA_VERSIONS:
            for name in _FITTING_FIELDS:
                encoded.pop(name)
        if isinstance(value, BuildingModel) and value.schema_version not in ASSEMBLY_SCHEMA_VERSIONS:
            for name in _ASSEMBLY_FIELDS:
                encoded.pop(name)
        if isinstance(value, Provenance) and value.derivation is None:
            # An unset derivation states nothing, so omit it rather than
            # writing an explicit null. Adding the field then leaves the
            # serialization of every record that predates it byte-identical,
            # which keeps the golden known-answer hashes meaningful instead of
            # forcing a wholesale regeneration that would hide real drift.
            encoded.pop("derivation", None)
        if isinstance(value, Provenance) and value.scope_paths is None:
            # Legacy records remain byte-identical; absence means entity-wide.
            encoded.pop("scope_paths", None)
        if isinstance(value, Wall) and value.construction is None:
            # An unknown construction states nothing, so omit it rather than
            # writing an explicit null. Every wall recorded before the field
            # existed serializes byte-identically, keeping the golden
            # known-answer digests meaningful.
            encoded.pop("construction", None)
        return encoded
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def upgrade_to_v1_1(model: BuildingModel) -> BuildingModel:
    """Explicit, lossless opt-in to the physical-object contract; never downgrade."""
    from dataclasses import replace
    if model.schema_version == ASSEMBLY_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_1 cannot downgrade version 1.6")
    if model.schema_version in RACEWAY_SCHEMA_VERSIONS:
        raise ContractError("upgrade_to_v1_1 cannot downgrade version 1.2")
    return replace(model, schema_version=PHYSICAL_SCHEMA_VERSION)


def upgrade_to_v1_2(model: BuildingModel) -> BuildingModel:
    """Explicit lossless opt-in; preserve prior physical box inventory."""
    from dataclasses import replace
    if model.schema_version == ASSEMBLY_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_2 cannot downgrade version 1.6")
    if model.schema_version == SUPPORT_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_2 cannot downgrade version 1.3")
    return replace(model, schema_version=RACEWAY_SCHEMA_VERSION)


def upgrade_to_v1_3(model: BuildingModel) -> BuildingModel:
    """Explicit lossless opt-in to shared supports; preserve previous inventory."""
    from dataclasses import replace
    if model.schema_version == ASSEMBLY_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_3 cannot downgrade version 1.6")
    return replace(model, schema_version=SUPPORT_SCHEMA_VERSION)


def upgrade_to_v1_4(model: BuildingModel) -> BuildingModel:
    """Explicit lossless opt-in to structural members; preserve prior inventory."""
    from dataclasses import replace
    if model.schema_version == ASSEMBLY_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_4 cannot downgrade version 1.6")
    return replace(model, schema_version=STRUCTURAL_SCHEMA_VERSION)


def upgrade_to_v1_5(model: BuildingModel) -> BuildingModel:
    """Explicit lossless opt-in to exact-product route-fitting decisions."""
    from dataclasses import replace
    if model.schema_version == ASSEMBLY_SCHEMA_VERSION:
        raise ContractError("upgrade_to_v1_5 cannot downgrade version 1.6")
    return replace(model, schema_version=FITTING_SCHEMA_VERSION)


def upgrade_to_v1_6(model: BuildingModel) -> BuildingModel:
    """Explicit lossless opt-in to candidate building-assembly records.

    This adds empty assembly collections and never invents a preset, component,
    host dimension, material or geometry. Contract 1.6 is an isolated draft;
    current public consumers are not yet safe for models using its assemblies.
    """
    from dataclasses import replace
    return replace(model, schema_version=ASSEMBLY_SCHEMA_VERSION)


T = TypeVar("T")


def _decode_dataclass(cls: type[T], data: Mapping[str, Any]) -> T:
    if not isinstance(data, Mapping):
        raise ContractError(f"{cls.__name__} must be an object")
    field_map = {item.name: item for item in fields(cls)}
    unknown = set(data) - set(field_map)
    if unknown:
        raise ContractError(f"{cls.__name__} has unknown field(s): {', '.join(sorted(unknown))}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for item in fields(cls):
        if not item.init:
            if item.name == "kind" and item.name not in data:
                raise ContractError(f"{cls.__name__} is missing required discriminator 'kind'")
            if item.name in data and data[item.name] != item.default:
                raise ContractError(
                    f"{cls.__name__}.{item.name} must equal {item.default!r}"
                )
            continue
        if item.name in data:
            kwargs[item.name] = _decode_value(hints.get(item.name, item.type), data[item.name])
        elif item.default is MISSING and item.default_factory is MISSING:
            raise ContractError(f"{cls.__name__} is missing required field {item.name!r}")
    try:
        return cls(**kwargs)
    except ContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid {cls.__name__}: {exc}") from exc


def _decode_value(tp: Any, value: Any) -> Any:
    if tp is Any:
        _validate_json_value(value)
        return value

    origin = get_origin(tp)
    args = get_args(tp)

    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        errors: list[str] = []
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _decode_value(candidate, value)
            except (ContractError, TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise ContractError("value does not match any allowed type: " + " | ".join(errors))

    if origin is tuple:
        if not isinstance(value, list):
            raise ContractError("tuple fields serialize as JSON arrays")
        item_type = args[0] if args else Any
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(item_type, item) for item in value)
        if len(value) != len(args):
            raise ContractError("tuple length does not match contract")
        return tuple(_decode_value(item_type, item) for item_type, item in zip(args, value))

    if origin is dict:
        if not isinstance(value, dict):
            raise ContractError("mapping field must be a JSON object")
        key_type, value_type = args if args else (str, Any)
        return {
            _decode_value(key_type, key): _decode_value(value_type, item)
            for key, item in value.items()
        }

    if isinstance(tp, type) and is_dataclass(tp):
        return _decode_dataclass(tp, value)

    if tp is str:
        if not isinstance(value, str):
            raise ContractError("expected string")
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise ContractError("expected boolean")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("expected integer")
        return value
    if tp is float:
        return _finite(value, "number")
    if tp is type(None):
        if value is not None:
            raise ContractError("expected null")
        return None

    return value
