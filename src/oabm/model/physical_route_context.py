"""Typed retained physical routing inputs. Binding is not geometric verification."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from fractions import Fraction
from typing import Any

from .common import Box3D, ContractError, PhysicalRoutingAdmissionError, Entity, Provenance, _validate_id

PROFILE = "physical-routing-decimal-nm-v1"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: Any) -> None:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ContractError("physical context digest must be a lowercase SHA-256")


def _provenance(owner: Any, supplied_fields: tuple[str, ...] = ()) -> None:
    from .physical_routes import _owned_provenance
    _owned_provenance(owner, supplied_fields=supplied_fields)


def physical_box_bounds(box: Box3D):
    """Exact rational bounds for the explicitly supported identity-pose profile."""
    from .physical_routes import exact_ticks, point_ticks
    if not isinstance(box, Box3D):
        raise ContractError("physical domain/blocker requires typed Box3D")
    q = box.pose.rotation
    if (q.x, q.y, q.z, q.w) != (0, 0, 0, 1):
        raise PhysicalRoutingAdmissionError('unsupported physical box rotation; canonical identity rotation required', category='unsupported_input', reason='box_rotation')
    centre = point_ticks(box.pose.position, "physical box centre")
    size = tuple(exact_ticks(getattr(box.size, axis), "box full dimension", minimum="0.000000001", maximum="2000")
                 for axis in ("x", "y", "z"))
    lower = tuple(Fraction(c) - Fraction(s, 2) for c, s in zip(centre, size))
    upper = tuple(Fraction(c) + Fraction(s, 2) for c, s in zip(centre, size))
    if any(abs(v) > 1_000_000_000_000 for v in (*lower, *upper)):
        raise PhysicalRoutingAdmissionError('physical box extrema exceed coordinate scope', category='unsupported_input', reason='box_extrema_profile')
    return lower, upper


@dataclass(frozen=True, slots=True, kw_only=True)
class AllowedPhysicalDomain(Entity):
    coordinate_frame_id: str
    volumes: tuple[Box3D, ...]

    def __post_init__(self) -> None:
        super(AllowedPhysicalDomain, self).__post_init__()
        _validate_id(self.coordinate_frame_id, "domain.coordinate_frame_id")
        if not isinstance(self.volumes, tuple) or not 1 <= len(self.volumes) <= 128:
            raise ContractError("physical domain requires 1..128 boxes")
        for box in self.volumes:
            physical_box_bounds(box)
        _provenance(self, ("volumes",))


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalGeometryCoverage:
    source_entity_id: str
    decision: str
    blocker_entity_ids: tuple[str, ...]
    rationale: str
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        _validate_id(self.source_entity_id, "coverage.source_entity_id")
        if not isinstance(self.decision, str) or self.decision not in {"direct_blocker", "supplied_envelope", "outside_proof_scope"}:
            raise ContractError("unknown physical coverage decision")
        if (not isinstance(self.blocker_entity_ids, tuple) or len(self.blocker_entity_ids) > 4096 or
                any(not isinstance(identity, str) for identity in self.blocker_entity_ids) or
                len(set(self.blocker_entity_ids)) != len(self.blocker_entity_ids)):
            raise ContractError("coverage blocker IDs must be a unique tuple")
        for identity in self.blocker_entity_ids:
            _validate_id(identity, "coverage.blocker_entity_ids")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ContractError("physical coverage requires an explicit rationale")
        if self.decision == "outside_proof_scope" and self.blocker_entity_ids:
            raise ContractError("excluded coverage cannot carry blocker IDs")
        if self.decision != "outside_proof_scope" and not self.blocker_entity_ids:
            raise ContractError("blocker coverage requires referenced blocker IDs")
        _provenance(self, ("source_entity_id", "decision", "blocker_entity_ids", "rationale"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRoutingObjective:
    bend_penalty_m: float
    kind: str = field(default="length_plus_bends_v1", init=False)

    def __post_init__(self) -> None:
        from .physical_routes import exact_ticks
        exact_ticks(self.bend_penalty_m, "bend penalty", minimum="0", maximum="1000")


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRoutingExecutionLimits:
    candidate_routes: int
    candidate_grid_nodes: int
    pushed_search_states: int
    interval_refinement_depth: int
    total_refinement_cells: int

    def __post_init__(self) -> None:
        maxima = {"candidate_routes": 128, "candidate_grid_nodes": 200000,
                  "pushed_search_states": 500000, "interval_refinement_depth": 64,
                  "total_refinement_cells": 1000000}
        for name, maximum in maxima.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ContractError(f"{name} must be an integer in 1..{maximum}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRoutingContext:
    request_id: str
    route_type: str
    source_model_sha256: str
    source_model_schema_version: str
    coordinate_frame_id: str
    start_port_id: str
    end_port_id: str
    allowed_domain: AllowedPhysicalDomain
    coverage: tuple[PhysicalGeometryCoverage, ...]
    objective: PhysicalRoutingObjective
    execution_limits: PhysicalRoutingExecutionLimits
    request_sha256: str
    kind: str = field(default="physical_routing_context_v1", init=False)
    profile: str = field(default=PROFILE, init=False)
    digest_encoding: str = field(default="physical-routing-binding-json-v1", init=False)

    def __post_init__(self) -> None:
        from .common import SUPPORTED_SCHEMA_VERSIONS
        for name in ("request_id", "coordinate_frame_id", "start_port_id", "end_port_id"):
            _validate_id(getattr(self, name), name)
        _digest(self.source_model_sha256)
        _digest(self.request_sha256)
        if self.source_model_schema_version != "1.7.0":
            raise ContractError("physical source must be explicitly upgraded to schema 1.7.0")
        if not isinstance(self.route_type, str) or not self.route_type:
            raise ContractError("physical context route_type is required")
        if self.start_port_id == self.end_port_id:
            raise ContractError("physical context needs distinct endpoint ports")
        for name, expected in (("allowed_domain", AllowedPhysicalDomain),
                               ("objective", PhysicalRoutingObjective),
                               ("execution_limits", PhysicalRoutingExecutionLimits)):
            if not isinstance(getattr(self, name), expected):
                raise ContractError(f"physical context {name} must be typed")
        if self.allowed_domain.coordinate_frame_id != self.coordinate_frame_id:
            raise ContractError("physical context domain frame mismatch")
        if not isinstance(self.coverage, tuple) or len(self.coverage) > 10000 or any(
            not isinstance(row, PhysicalGeometryCoverage) for row in self.coverage
        ):
            raise ContractError("physical coverage must contain at most 10000 typed entries")
        if len({row.source_entity_id for row in self.coverage}) != len(self.coverage):
            raise ContractError("physical coverage source IDs must be unique")


def _canonical_record(record: Any) -> Any:
    # Normalize only typed numeric representation, not geometry: this accounts
    # for Python int arguments to float fields without changing legacy JSON.
    from .model import _decode_dataclass, _encode
    return _encode(_decode_dataclass(type(record), _encode(record)))


def compute_physical_source_sha256(model: Any) -> str:
    """Hash strict normalized canonical source bytes, not a tolerance equivalence."""
    from .model import BuildingModel
    canonical = BuildingModel.from_dict(model.to_dict()).to_json(indent=None)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_physical_request_sha256(context: PhysicalRoutingContext, requirements: Any) -> str:
    snapshot = _canonical_record(context)
    snapshot.pop("request_sha256")
    payload = {"context": snapshot, "requirements": _canonical_record(requirements)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_request_binding(context: PhysicalRoutingContext, requirements: Any) -> None:
    if compute_physical_request_sha256(context, requirements) != context.request_sha256:
        raise PhysicalRoutingAdmissionError('physical request digest does not bind the full context and requirements', category='invalid_input', reason='stale_request_binding')


# Explicit inventory fields. Additions to BuildingModel require review, rather
# than silently dropping a new geometry collection from the proof boundary.
_INVENTORY_COLLECTIONS = frozenset({
    "walls", "slabs", "ceilings", "openings", "electrical_equipment", "electrical_devices",
    "electrical_boxes", "obstacles", "route_constraints", "structural_members",
    "assembly_components", "support_components", "routes", "fitting_selections",
})
_NON_GEOMETRY_COLLECTIONS = frozenset({
    "levels", "spaces", "ports", "route_fittings", "circuits", "conductors",
    "box_selection_outcomes", "raceway_selections", "raceway_selection_outcomes",
    "support_assemblies", "support_interfaces", "support_selection_outcomes",
    "fitting_interfaces", "fitting_selection_outcomes",
    "building_assemblies",
})


def physical_geometry_inventory(model: Any, route_type: str) -> dict[str, Any]:
    """Fresh source inventory only. No envelope generation or enclosure claim."""
    inventory = {}
    for descriptor in fields(model):
        name = descriptor.name
        value = getattr(model, name)
        if name in _INVENTORY_COLLECTIONS:
            for item in value:
                # Geometry-unspecified endpoint owners are not invented solids.
                if name in {"electrical_equipment", "electrical_devices", "fitting_selections"} and item.size is None:
                    continue
                if name == "route_constraints":
                    if item.applies_to and route_type not in item.applies_to and "*" not in item.applies_to:
                        continue
                    if not item.hard or item.constraint_type not in {"keepout", "keep_out", "no_go", "keep-out", "avoid"}:
                        raise PhysicalRoutingAdmissionError('unsupported applicable hard physical route constraint', category='unsupported_input', reason='active_constraint_adapter')
                inventory[item.id] = item
        elif name in _NON_GEOMETRY_COLLECTIONS or name in {
            "model_id", "name", "schema_version", "coordinate_system", "provenance", "attributes"
        }:
            continue
        elif value:
            raise PhysicalRoutingAdmissionError(f"unsupported populated model collection {name!r} in physical inventory", category="unsupported_input", reason="source_collection_adapter")
    return inventory


def _validate_physical_context_source(model: Any, route: Any, *, source_json: str | bytes | None = None) -> str:
    from .physical_routes import validate_physical_route_ports
    from .model import validate_model
    validate_model(model)
    route.__post_init__()
    ports = {port.id: port for port in model.ports}
    if route.start_port_id not in ports or route.end_port_id not in ports:
        raise ContractError("physical source is missing an endpoint port")
    validate_physical_route_ports(route, ports, model.coordinate_system.frame_id)
    return validate_physical_request_source(model, route.context, route.requirements,
                                           source_json=source_json)


def validate_physical_request_source(model: Any, context: PhysicalRoutingContext, requirements: Any, *, source_json: str | bytes | None = None) -> str:
    from .model import BuildingModel
    from .physical_routes import PhysicalRouteRequirements
    if type(model) is not BuildingModel or type(context) is not PhysicalRoutingContext or type(requirements) is not PhysicalRouteRequirements:
        raise ContractError("physical source admission requires typed model, context and requirements")
    if source_json is not None and not isinstance(source_json, (str, bytes)):
        raise ContractError("physical source_json must be text or bytes")
    try:
        return _validate_physical_request_source(model, context, requirements, source_json=source_json)
    except (AttributeError, KeyError, TypeError, OverflowError) as exc:
        raise ContractError("malformed physical request source or typed structure") from exc


def _validate_physical_request_source(model: Any, context: PhysicalRoutingContext, requirements: Any, *, source_json: str | bytes | None = None) -> str:
    """Validate exact CURRENT pre-route binding and declared coverage, not safety.

    The supplied model must be the pre-route snapshot. This function never
    removes routes implicitly and never returns a verified geometry status.
    Returns raw_json_lexemes or canonical_python_snapshot to distinguish original
    byte-origin admission from the already-rendered Python snapshot boundary.
    """
    from .entities import Obstacle, RouteConstraint
    from .structural import StructuralMember
    from .physical_routes import exact_ticks, point_ticks, PhysicalRouteRequirements
    from .model import validate_model
    validate_model(model)
    if type(context) is not PhysicalRoutingContext or type(requirements) is not PhysicalRouteRequirements:
        raise ContractError("physical request requires typed context and requirements")
    ports = {port.id: port for port in model.ports}
    if context.start_port_id not in ports or context.end_port_id not in ports:
        raise ContractError("physical source is missing an endpoint port")
    # Reconstruct nested canonical records to revalidate mutated frozen owners.
    from .model import _decode_dataclass
    context = _decode_dataclass(PhysicalRoutingContext, _canonical_record(context))
    requirements = _decode_dataclass(PhysicalRouteRequirements, _canonical_record(requirements))
    for identity in (context.start_port_id, context.end_port_id):
        port = ports[identity]
        point_ticks(port.pose.position, "source endpoint")
        if port.direction is None:
            raise ContractError("physical source endpoint direction is required")
        direction = (port.direction.x, port.direction.y, port.direction.z)
        if any(isinstance(v, bool) for v in direction) or direction not in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
            raise ContractError("physical source endpoint direction must be an exact signed unit axis")
    admission_mode = "canonical_python_snapshot"
    if source_json is not None:
        from decimal import Decimal
        from .model import BuildingModel
        from .physical_routes import _raw_box, _raw_point
        try:
            raw = json.loads(source_json, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("invalid physical source JSON") from exc
        if not isinstance(raw, dict):
            raise ContractError("physical source JSON must be an object")
        endpoint_ids = {context.start_port_id, context.end_port_id}
        for port in raw.get("ports", ()):
            if port.get("id") in endpoint_ids:
                _raw_point(port.get("pose", {}).get("position"), "source endpoint")
                values = tuple(port.get("direction", {}).get(axis) for axis in "xyz")
                if values not in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
                    raise ContractError("source physical port direction must be an exact signed unit axis")
        for name in ("obstacles", "route_constraints", "structural_members"):
            for item in raw.get(name, ()):
                if name == "route_constraints":
                    applies = item.get("applies_to", ())
                    if applies and context.route_type not in applies and "*" not in applies:
                        continue
                if name == "structural_members":
                    _raw_box({"pose": item.get("pose"), "size": item.get("size")})
                else:
                    _raw_box(item.get("geometry"))
                    exact_ticks(item.get("clearance_m", 0), "source blocker clearance", minimum="0", maximum="100")
        parsed = BuildingModel.from_json(source_json)
        if compute_physical_source_sha256(parsed) != compute_physical_source_sha256(model):
            raise PhysicalRoutingAdmissionError('raw physical source snapshot differs from supplied model', category='invalid_input', reason='raw_source_mismatch')
        admission_mode = "raw_json_lexemes"
    validate_request_binding(context, requirements)
    if context.source_model_schema_version != model.schema_version:
        raise PhysicalRoutingAdmissionError('physical source schema binding is stale', category='invalid_input', reason='stale_schema_binding')
    if context.coordinate_frame_id != model.coordinate_system.frame_id:
        raise PhysicalRoutingAdmissionError('physical source frame binding is stale', category='invalid_input', reason='stale_frame_binding')
    if context.source_model_sha256 != compute_physical_source_sha256(model):
        raise PhysicalRoutingAdmissionError('physical source model binding is stale; rebuild current coverage', category='invalid_input', reason='stale_source_binding')
    if requirements.route_id in {r.id for r in model.routes}:
        raise ContractError("physical source must be pre-route, not self-containing")
    inventory = physical_geometry_inventory(model, context.route_type)
    coverage = {row.source_entity_id: row for row in context.coverage}
    if set(coverage) != set(inventory):
        raise PhysicalRoutingAdmissionError('unsupported incomplete physical coverage for current source inventory', category='unsupported_input', reason='incomplete_coverage')
    obstacle_ids = {item.id for item in model.obstacles}
    blocker_ids = set()
    for identity, item in inventory.items():
        row = coverage[identity]
        mandatory = isinstance(item, (Obstacle, RouteConstraint, StructuralMember))
        if mandatory and row.decision == "outside_proof_scope":
            raise PhysicalRoutingAdmissionError('mandatory hard source cannot be excluded from physical coverage', category='invalid_input', reason='mandatory_source_excluded')
        if row.decision == "outside_proof_scope":
            continue
        if row.decision == "supplied_envelope":
            if not set(row.blocker_entity_ids).issubset(obstacle_ids):
                raise ContractError("supplied envelopes must reference canonical obstacles")
            raise PhysicalRoutingAdmissionError('unsupported envelope enclosure adapter; no original-geometry clearance claim', category='unsupported_input', reason='envelope_adapter')
        if row.blocker_entity_ids != (identity,):
            raise ContractError("direct coverage must bind exactly its own source identity")
        if isinstance(item, (Obstacle, RouteConstraint)):
            physical_box_bounds(item.geometry)
            exact_ticks(item.clearance_m, "blocker clearance", minimum="0", maximum="100")
        elif isinstance(item, StructuralMember):
            physical_box_bounds(Box3D(pose=item.pose, size=item.size))
        else:
            raise PhysicalRoutingAdmissionError('unsupported direct physical source geometry adapter', category='unsupported_input', reason='direct_source_adapter')
        blocker_ids.add(identity)
    if len(blocker_ids) > 4096:
        raise PhysicalRoutingAdmissionError('physical source exceeds 4096 resolved blocker boxes', category='unsupported_input', reason='blocker_count_profile')
    return admission_mode


def validate_physical_context_source(model: Any, route: Any, *, source_json: str | bytes | None = None) -> str:
    """Admit current source binding, returning its numeric admission mode only.

    Never certifies obstacle clearance. Byte-origin inputs must supply source_json
    to validate consumed original lexemes; omitted bytes mean Python-rendered input.
    """
    from .model import BuildingModel
    from .physical_routes import PhysicalRoute
    if not isinstance(model, BuildingModel) or not isinstance(route, PhysicalRoute):
        raise ContractError("physical source admission requires a BuildingModel and PhysicalRoute")
    if source_json is not None and not isinstance(source_json, (str, bytes)):
        raise ContractError("physical source_json must be text or bytes")
    try:
        return _validate_physical_context_source(model, route, source_json=source_json)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ContractError("malformed physical source JSON or typed structure") from exc
