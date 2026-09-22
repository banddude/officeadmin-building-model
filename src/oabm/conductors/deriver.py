"""Deterministic derivation of individual conductors from canonical circuits.

Prime directive for this lane
-----------------------------
A plan that says ``LP-1,3,5`` states *circuits*.  It does not state how many
physical conductors are in the raceway, what they are made of, how big they
are, or what insulation they carry.  Turning a circuit into "two hots, a
neutral and a ground" is an **engineering inference**, never an observation.

This module therefore:

1. labels every conductor it derives from a rule as ``"inferred"``;
2. never labels its own rule output ``"observed-from-source"`` -- only an
   explicit caller override, carrying the caller's own provenance, may claim
   an observed or user-designed fact;
3. fails closed with a warning instead of guessing whenever a required input
   is absent (in particular it leaves ``size``/``material``/``insulation``
   ``None`` rather than emitting a plausible-looking wire size that a reader
   would mistake for a measurement);
4. never creates geometry.  ``route_ids`` are copied verbatim from the parent
   circuit; paths stay the routing lane's business.

The module is pure: it reads a validated :class:`~oabm.model.BuildingModel`
and returns canonical :class:`~oabm.model.Conductor` objects plus warnings.
Callers decide whether and how to attach them
(``dataclasses.replace(model, conductors=...)``), mirroring the routing lane.

Provenance note: the canonical contract has no first-class provenance
``derivation`` field on this branch, so the epistemic class is recorded as
``derivation_class`` inside entity attributes and provenance attributes.  When
a first-class field lands this module maps onto it; the hard invariant
(rule output is never ``observed-from-source``) does not depend on where the
label is stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from oabm.model import (
    BuildingModel,
    Circuit,
    Conductor,
    ContractError,
    Provenance,
    stable_id,
    validate_model,
)

ALGORITHM = "conductor-derivation-rule-v1"
SOURCE_KIND = "conductor-derivation"

ROLE_LINE = "line"
ROLE_NEUTRAL = "neutral"
ROLE_EQUIPMENT_GROUND = "equipment-ground"

#: Closed role vocabulary for v1.  Aliases (``hot``, ``ground``, ``L1``) are
#: rejected on purpose: ``role`` becomes a takeoff ``item_type`` downstream.
ROLE_VOCABULARY: tuple[str, ...] = (ROLE_LINE, ROLE_NEUTRAL, ROLE_EQUIPMENT_GROUND)

CLASS_INFERRED = "inferred"
CLASS_USER_DESIGNED = "user-designed"
CLASS_OBSERVED = "observed-from-source"
DERIVATION_CLASSES: tuple[str, ...] = (CLASS_INFERRED, CLASS_USER_DESIGNED, CLASS_OBSERVED)
_OVERRIDE_CLASSES: tuple[str, ...] = (CLASS_USER_DESIGNED, CLASS_OBSERVED)
_CLASS_STRENGTH = {CLASS_INFERRED: 0, CLASS_USER_DESIGNED: 1, CLASS_OBSERVED: 2}

#: Attribute key an upstream lane may use to record that several circuits share
#: one homerun/raceway.  Looked up at the top level of ``Circuit.attributes``
#: and one level down inside per-lane attribute mappings.
RACEWAY_GROUP_KEY = "shared_raceway_group"

#: Above this line-to-neutral voltage a circuit is not treated as a plausible
#: line-to-neutral branch circuit by the default neutral policy.
LINE_TO_NEUTRAL_MAX_V = 150.0

_ROLE_ORDER = {role: index for index, role in enumerate(ROLE_VOCABULARY)}


class ConductorDerivationError(ValueError):
    """Raised when canonical inputs are unsafe or inconsistent for derivation."""


@dataclass(frozen=True, slots=True)
class ConductorWarning:
    """A named, machine-readable reason output is incomplete or assumed."""

    code: str
    message: str
    source_entity_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "source_entity_ids": list(self.source_entity_ids),
        }


class PolesMissingPolicy(Enum):
    """What to do when ``Circuit.poles`` was never observed."""

    FAIL = "fail"
    INFER_FROM_VOLTAGE = "infer-from-voltage"
    ASSUME_ONE = "assume-one"


class NeutralPolicy(Enum):
    """When a grounded (neutral) conductor is derived."""

    NEVER = "never"
    INFER_FROM_VOLTAGE = "infer-from-voltage"


class EgcPolicy(Enum):
    """When an equipment grounding conductor is derived."""

    ALWAYS_ONE = "always-one"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class TableSizingPolicy:
    """Caller-supplied ampacity table.  No code table ships as a default.

    ``ampacity_table`` is ``((ampacity_a, size), ...)`` in strictly ascending
    ampacity order; the smallest entry whose ampacity is at least the required
    amperage wins.  Required amperage is ``load_va / voltage_v * load_factor``
    and is only computed when both canonical fields were populated.  Output is
    still labelled inferred: a table lookup over an inferred load is not an
    observation.
    """

    policy_id: str
    ampacity_table: tuple[tuple[float, str], ...]
    load_factor: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise ConductorDerivationError("TableSizingPolicy.policy_id must be a non-empty string")
        if not isinstance(self.ampacity_table, tuple) or not self.ampacity_table:
            raise ConductorDerivationError(
                "TableSizingPolicy.ampacity_table must be a non-empty tuple of (amps, size)"
            )
        previous = 0.0
        for entry in self.ampacity_table:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ConductorDerivationError(
                    "TableSizingPolicy.ampacity_table entries must be (amps, size) pairs"
                )
            amps, size = entry
            if isinstance(amps, bool) or not isinstance(amps, (int, float)):
                raise ConductorDerivationError("TableSizingPolicy ampacity must be a number")
            amps = float(amps)
            if not math.isfinite(amps) or amps <= 0.0:
                raise ConductorDerivationError("TableSizingPolicy ampacity must be finite and > 0")
            if amps <= previous:
                raise ConductorDerivationError(
                    "TableSizingPolicy.ampacity_table must ascend strictly by ampacity"
                )
            previous = amps
            if not isinstance(size, str) or not size:
                raise ConductorDerivationError("TableSizingPolicy size must be a non-empty string")
        if isinstance(self.load_factor, bool) or not isinstance(self.load_factor, (int, float)):
            raise ConductorDerivationError("TableSizingPolicy.load_factor must be a number")
        if not math.isfinite(float(self.load_factor)) or float(self.load_factor) <= 0.0:
            raise ConductorDerivationError("TableSizingPolicy.load_factor must be finite and > 0")

    def size_for_amps(self, amps: float) -> str | None:
        for ampacity, size in self.ampacity_table:
            if ampacity >= amps:
                return size
        return None


@dataclass(frozen=True, slots=True)
class CircuitConductorOverride:
    """Facts a caller actually has, injected in place of the count rules.

    This is the only channel by which a conductor is ever labelled anything
    other than ``inferred``.  ``derivation_class`` is required, and an
    ``observed-from-source`` override must carry the provenance of the thing
    that was read (a schedule row, for example).
    """

    derivation_class: str
    provenance: tuple[Provenance, ...] = ()
    material: str | None = None
    size: str | None = None
    insulation: str | None = None
    counts: Mapping[str, int] | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.derivation_class not in _OVERRIDE_CLASSES:
            raise ConductorDerivationError(
                "CircuitConductorOverride.derivation_class must be one of "
                f"{_OVERRIDE_CLASSES}; got {self.derivation_class!r}"
            )
        if not isinstance(self.provenance, tuple) or not all(
            isinstance(item, Provenance) for item in self.provenance
        ):
            raise ConductorDerivationError(
                "CircuitConductorOverride.provenance must be a tuple of Provenance"
            )
        if self.derivation_class == CLASS_OBSERVED and not self.provenance:
            raise ConductorDerivationError(
                "an observed-from-source override must cite provenance for what was read"
            )
        for label in ("material", "size", "insulation"):
            value = getattr(self, label)
            if value is not None and (not isinstance(value, str) or not value):
                raise ConductorDerivationError(
                    f"CircuitConductorOverride.{label} must be a non-empty string or None"
                )
        if self.counts is not None:
            if not isinstance(self.counts, Mapping):
                raise ConductorDerivationError(
                    "CircuitConductorOverride.counts must be a mapping of role to count"
                )
            normalized: dict[str, int] = {}
            for role in sorted(self.counts):
                _require_role(role, "CircuitConductorOverride.counts")
                count = self.counts[role]
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ConductorDerivationError(
                        f"CircuitConductorOverride.counts[{role!r}] must be an integer >= 1"
                    )
                normalized[role] = count
            object.__setattr__(self, "counts", MappingProxyType(normalized))
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise ConductorDerivationError("CircuitConductorOverride.confidence must be a number")
            value = float(self.confidence)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ConductorDerivationError(
                    "CircuitConductorOverride.confidence must be between 0 and 1"
                )


@dataclass(frozen=True, slots=True)
class ConductorDerivationOptions:
    """Engineering policy knobs.  None of these change the canonical contract."""

    poles_missing: PolesMissingPolicy = PolesMissingPolicy.FAIL
    neutral_policy: NeutralPolicy = NeutralPolicy.INFER_FROM_VOLTAGE
    egc_policy: EgcPolicy = EgcPolicy.ALWAYS_ONE
    default_material: str | None = None
    default_insulation: str | None = None
    sizing: TableSizingPolicy | None = None
    overrides: Mapping[str, CircuitConductorOverride] = field(
        default_factory=lambda: MappingProxyType({})
    )
    shared_conductors: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    confidence_full_inputs: float = 0.7
    confidence_defaulted_inputs: float = 0.4

    def __post_init__(self) -> None:
        if not isinstance(self.poles_missing, PolesMissingPolicy):
            raise ConductorDerivationError("poles_missing must be a PolesMissingPolicy")
        if not isinstance(self.neutral_policy, NeutralPolicy):
            raise ConductorDerivationError("neutral_policy must be a NeutralPolicy")
        if not isinstance(self.egc_policy, EgcPolicy):
            raise ConductorDerivationError("egc_policy must be an EgcPolicy")
        if self.sizing is not None and not isinstance(self.sizing, TableSizingPolicy):
            raise ConductorDerivationError("sizing must be a TableSizingPolicy or None")
        for label in ("default_material", "default_insulation"):
            value = getattr(self, label)
            if value is not None and (not isinstance(value, str) or not value):
                raise ConductorDerivationError(f"{label} must be a non-empty string or None")
        if not isinstance(self.overrides, Mapping):
            raise ConductorDerivationError("overrides must be a mapping of circuit id to override")
        for circuit_id in sorted(self.overrides):
            if not isinstance(self.overrides[circuit_id], CircuitConductorOverride):
                raise ConductorDerivationError(
                    f"overrides[{circuit_id!r}] must be a CircuitConductorOverride"
                )
        if not isinstance(self.shared_conductors, Mapping):
            raise ConductorDerivationError(
                "shared_conductors must be a mapping of raceway group to shared roles"
            )
        for group_id in sorted(self.shared_conductors):
            roles = self.shared_conductors[group_id]
            if not isinstance(roles, tuple) or not roles:
                raise ConductorDerivationError(
                    f"shared_conductors[{group_id!r}] must be a non-empty tuple of roles"
                )
            seen: set[str] = set()
            for role in roles:
                _require_role(role, f"shared_conductors[{group_id!r}]")
                if role in seen:
                    raise ConductorDerivationError(
                        f"shared_conductors[{group_id!r}] repeats role {role!r}"
                    )
                seen.add(role)
        for label in ("confidence_full_inputs", "confidence_defaulted_inputs"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConductorDerivationError(f"{label} must be a number")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ConductorDerivationError(f"{label} must be between 0 and 1")
        if float(self.confidence_defaulted_inputs) > float(self.confidence_full_inputs):
            raise ConductorDerivationError(
                "confidence_defaulted_inputs must not exceed confidence_full_inputs"
            )


@dataclass(frozen=True, slots=True)
class ConductorDerivation:
    """Derived conductors plus the reasons the output is what it is.

    Unpacks as ``conductors, warnings = derive_conductors(model)``.
    """

    conductors: tuple[Conductor, ...]
    warnings: tuple[ConductorWarning, ...]

    def __iter__(self) -> Iterator[Any]:
        yield self.conductors
        yield self.warnings


def _require_role(role: Any, label: str) -> None:
    if role not in ROLE_VOCABULARY:
        raise ConductorDerivationError(
            f"{label}: role {role!r} is outside the v1 vocabulary {ROLE_VOCABULARY}"
        )


def _raceway_group(circuit: Circuit) -> str | None:
    """Return the shared-raceway group id an upstream lane recorded, if any."""

    attributes = circuit.attributes or {}
    candidates: list[Any] = []
    if RACEWAY_GROUP_KEY in attributes:
        candidates.append(attributes[RACEWAY_GROUP_KEY])
    for key in sorted(attributes):
        nested = attributes[key]
        if isinstance(nested, Mapping) and RACEWAY_GROUP_KEY in nested:
            candidates.append(nested[RACEWAY_GROUP_KEY])
    if not candidates:
        return None
    for value in candidates:
        if not isinstance(value, str) or not value:
            raise ConductorDerivationError(
                f"{circuit.id}: {RACEWAY_GROUP_KEY} must be a non-empty string; got {value!r}"
            )
    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        raise ConductorDerivationError(
            f"{circuit.id}: conflicting {RACEWAY_GROUP_KEY} values {distinct}"
        )
    return distinct[0]


def _line_count(
    circuit: Circuit,
    options: ConductorDerivationOptions,
) -> tuple[int | None, list[str], bool, str | None]:
    """Return ``(count, basis, inputs_observed, warning_message)`` for hots."""

    if circuit.poles is not None:
        # "poles" is definitionally the number of ungrounded conductors, so
        # this is the one count rule that sits right next to the observation.
        return circuit.poles, ["poles-observed"], True, None

    policy = options.poles_missing
    message = (
        f"{circuit.id} has no observed poles; "
        f"ungrounded-conductor count policy {policy.value!r} applied"
    )
    if policy is PolesMissingPolicy.ASSUME_ONE:
        return 1, ["poles-missing:assume-one"], False, message
    if policy is PolesMissingPolicy.INFER_FROM_VOLTAGE:
        if circuit.voltage_v is not None and circuit.voltage_v <= LINE_TO_NEUTRAL_MAX_V:
            return (
                1,
                ["poles-missing:infer-from-voltage", "voltage-observed"],
                False,
                message,
            )
        return None, [], False, (
            f"{circuit.id} has no observed poles and no line-to-neutral voltage evidence; "
            "no ungrounded conductor was derived"
        )
    return None, [], False, (
        f"{circuit.id} has no observed poles; no ungrounded conductor was derived "
        "(fail-closed default)"
    )


def _neutral_count(
    circuit: Circuit,
    options: ConductorDerivationOptions,
    line_count: int | None,
) -> tuple[int | None, list[str], bool]:
    if options.neutral_policy is NeutralPolicy.NEVER:
        return None, [], True
    if line_count != 1:
        return None, [], True
    basis = ["neutral-policy:infer-from-voltage", "poles-observed" if circuit.poles is not None else "poles-assumed"]
    if circuit.voltage_v is None:
        basis.append("voltage-unknown")
        return 1, basis, False
    if circuit.voltage_v <= LINE_TO_NEUTRAL_MAX_V:
        basis.append("voltage-observed")
        return 1, basis, circuit.poles is not None
    return None, [], True


def _sized(
    circuit: Circuit,
    role: str,
    options: ConductorDerivationOptions,
) -> tuple[str | None, list[str], str | None]:
    """Return ``(size, basis, undetermined_reason)`` for a rule-derived size."""

    policy = options.sizing
    if policy is None:
        return None, [], "no sizing policy supplied"
    if role == ROLE_EQUIPMENT_GROUND:
        # Equipment grounding conductors are sized from the overcurrent device,
        # and the canonical contract has no overcurrent rating field.
        return None, [], "equipment-ground sizing needs an overcurrent rating the contract cannot carry"
    if circuit.load_va is None or circuit.voltage_v is None:
        return None, [], "load_va and voltage_v are required by the sizing policy"
    is_polyphase = (circuit.poles is not None and circuit.poles >= 3) or (
        isinstance(circuit.phase, str) and circuit.phase.strip().lower().startswith("3")
    )
    if is_polyphase:
        return None, [], "polyphase sizing is not implemented in this lane"
    amps = circuit.load_va / circuit.voltage_v * float(policy.load_factor)
    size = policy.size_for_amps(amps)
    if size is None:
        return None, [], "required ampacity exceeds the supplied sizing table"
    return size, [f"table-sizing:{policy.policy_id}"], None


def _build_conductor(
    *,
    model_id: str,
    circuit: Circuit,
    role: str,
    count: int,
    basis: list[str],
    inputs_observed: bool,
    options: ConductorDerivationOptions,
    override: CircuitConductorOverride | None,
    raceway_group: str | None,
    shared_with: tuple[str, ...],
    undetermined_reasons: set[str],
) -> Conductor:
    basis = list(basis)
    facts: dict[str, str] = {"count": CLASS_INFERRED}

    if override is not None and override.counts is not None and role in override.counts:
        count = override.counts[role]
        facts["count"] = override.derivation_class
        basis.append(f"override:{override.derivation_class}:count")

    material = options.default_material
    if material is not None:
        facts["material"] = CLASS_USER_DESIGNED
        basis.append("material:option-default")
    insulation = options.default_insulation
    if insulation is not None:
        facts["insulation"] = CLASS_USER_DESIGNED
        basis.append("insulation:option-default")

    size, size_basis, undetermined = _sized(circuit, role, options)
    basis.extend(size_basis)
    if size is not None:
        facts["size"] = CLASS_INFERRED

    if override is not None:
        if override.material is not None:
            material = override.material
            facts["material"] = override.derivation_class
            basis.append(f"override:{override.derivation_class}:material")
        if override.insulation is not None:
            insulation = override.insulation
            facts["insulation"] = override.derivation_class
            basis.append(f"override:{override.derivation_class}:insulation")
        if override.size is not None:
            size = override.size
            undetermined = None
            facts["size"] = override.derivation_class
            basis.append(f"override:{override.derivation_class}:size")

    if size is None and undetermined is not None:
        undetermined_reasons.add(undetermined)

    band = (
        options.confidence_full_inputs if inputs_observed else options.confidence_defaulted_inputs
    )
    if facts["count"] != CLASS_INFERRED:
        if override is not None and override.confidence is not None:
            band = float(override.confidence)
        elif override is not None and override.provenance:
            band = max(item.confidence for item in override.provenance)
        else:
            band = options.confidence_full_inputs
    # An inference about a circuit can never be more certain than the circuit.
    confidence = min(float(circuit.confidence), float(band))

    # The overall class is the weakest claim among the facts actually asserted.
    overall = min(facts.values(), key=lambda value: _CLASS_STRENGTH[value])

    derivation: dict[str, Any] = {
        "derivation_class": overall,
        "facts": dict(sorted(facts.items())),
        "basis": basis,
        "algorithm": ALGORITHM,
    }
    if raceway_group is not None:
        derivation["raceway_group"] = raceway_group
    if shared_with:
        derivation["shared_with"] = list(shared_with)

    provenance = Provenance(
        source_kind=SOURCE_KIND,
        source_id=model_id,
        source_element_id=circuit.id,
        method=ALGORITHM,
        confidence=confidence,
        attributes=dict(derivation),
    )
    provenances: tuple[Provenance, ...] = (provenance,)
    if override is not None and override.provenance:
        provenances = (*override.provenance, provenance)

    return Conductor(
        id=stable_id("conductor", f"{model_id}:{circuit.id}:{role}"),
        circuit_id=circuit.id,
        role=role,
        material=material,
        size=size,
        count=count,
        insulation=insulation,
        # Copied verbatim.  This lane never invents or extends a path.
        route_ids=tuple(circuit.route_ids),
        confidence=confidence,
        provenance=provenances,
        attributes={"derivation": derivation},
    )


def derive_conductors(
    model: BuildingModel,
    options: ConductorDerivationOptions | None = None,
) -> ConductorDerivation:
    """Derive canonical conductors from canonical circuits and their routes.

    Pure and deterministic.  Never creates ``Route`` geometry; copies
    ``circuit.route_ids`` verbatim; labels every rule-derived conductor
    inferred.  Callers decide whether and how to attach the result to a model
    document, matching the routing lane's contract.
    """

    if not isinstance(model, BuildingModel):
        raise ConductorDerivationError("derive_conductors requires a BuildingModel")
    if options is None:
        options = ConductorDerivationOptions()
    if not isinstance(options, ConductorDerivationOptions):
        raise ConductorDerivationError("options must be a ConductorDerivationOptions")

    # Boundary re-validation, like the quantities lane: a constructed model
    # already guarantees this, so the check protects against misuse.
    try:
        validate_model(model)
    except ContractError as exc:
        raise ConductorDerivationError(f"model is not derivable: {exc}") from exc

    circuits = tuple(sorted(model.circuits, key=lambda item: item.id))
    circuit_ids = {circuit.id for circuit in circuits}
    unknown_overrides = sorted(set(options.overrides) - circuit_ids)
    if unknown_overrides:
        raise ConductorDerivationError(
            f"overrides name unknown circuit ids {unknown_overrides}"
        )

    groups: dict[str, list[str]] = {}
    group_of: dict[str, str | None] = {}
    for circuit in circuits:
        group_id = _raceway_group(circuit)
        group_of[circuit.id] = group_id
        if group_id is not None:
            groups.setdefault(group_id, []).append(circuit.id)

    unknown_groups = sorted(set(options.shared_conductors) - set(groups))
    if unknown_groups:
        raise ConductorDerivationError(
            f"shared_conductors names raceway groups {unknown_groups} that no circuit declares"
        )

    warnings: list[ConductorWarning] = []
    conductors: list[Conductor] = []

    for group_id in sorted(groups):
        members = groups[group_id]
        if len(members) > 1 and group_id not in options.shared_conductors:
            warnings.append(
                ConductorWarning(
                    code="shared-neutral-requires-explicit-config",
                    message=(
                        f"circuits {sorted(members)} share raceway group {group_id!r}; the source "
                        "shows a shared path, not shared conductors, so each circuit derived its "
                        "own set (declare shared_conductors to model a shared neutral)"
                    ),
                    source_entity_ids=tuple(sorted(members)),
                )
            )

    # Pass 1: decide what each circuit needs, independently of any sharing.
    plans: dict[str, list[tuple[str, int, list[str], bool]]] = {}
    for circuit in circuits:
        planned: list[tuple[str, int, list[str], bool]] = []

        line_count, line_basis, line_observed, line_message = _line_count(circuit, options)
        if line_message is not None:
            warnings.append(
                ConductorWarning(
                    code="circuit-poles-unknown",
                    message=line_message,
                    source_entity_ids=(circuit.id,),
                )
            )
        if line_count is not None:
            planned.append((ROLE_LINE, line_count, line_basis, line_observed))

        neutral_count, neutral_basis, neutral_observed = _neutral_count(
            circuit, options, line_count
        )
        if neutral_count is not None:
            planned.append((ROLE_NEUTRAL, neutral_count, neutral_basis, neutral_observed))
        elif (
            options.neutral_policy is NeutralPolicy.INFER_FROM_VOLTAGE
            and line_count == 2
            and (circuit.voltage_v is None or circuit.voltage_v > LINE_TO_NEUTRAL_MAX_V)
        ):
            warnings.append(
                ConductorWarning(
                    code="neutral-policy-false-negative",
                    message=(
                        f"{circuit.id} derived no neutral because it is 2-pole; a 120/240 V "
                        "4-wire load does carry one and nothing in the model distinguishes the "
                        "two cases (supply an override if the source shows a neutral)"
                    ),
                    source_entity_ids=(circuit.id,),
                )
            )

        if options.egc_policy is EgcPolicy.ALWAYS_ONE:
            planned.append((ROLE_EQUIPMENT_GROUND, 1, ["egc-policy:always-one"], False))

        plans[circuit.id] = planned

    # Pass 2: resolve declared sharing.  The anchor of a shared role is the
    # lexicographically smallest circuit in the group that needs that role.
    anchors: dict[tuple[str, str], str] = {}
    for group_id in sorted(options.shared_conductors):
        for role in options.shared_conductors[group_id]:
            candidates = sorted(
                member
                for member in groups[group_id]
                if any(entry[0] == role for entry in plans[member])
            )
            if candidates:
                anchors[(group_id, role)] = candidates[0]

    # Pass 3: build.
    for circuit in circuits:
        override = options.overrides.get(circuit.id)
        group_id = group_of[circuit.id]
        members = sorted(groups[group_id]) if group_id is not None else []
        shared_roles = tuple(options.shared_conductors.get(group_id, ())) if group_id else ()
        planned = plans[circuit.id]

        undetermined_reasons: set[str] = set()
        unsized_ids: list[str] = []
        derived_ids: list[str] = []

        for role, count, basis, inputs_observed in planned:
            anchor = anchors.get((group_id, role)) if group_id is not None else None
            if role in shared_roles and anchor is not None and circuit.id != anchor:
                if override is not None and override.counts is not None and role in override.counts:
                    warnings.append(
                        ConductorWarning(
                            code="override-conflict",
                            message=(
                                f"{circuit.id} pins a {role!r} count by override while raceway "
                                f"group {group_id!r} declares {role!r} shared on anchor circuit "
                                f"{anchor!r}; the shared declaration was applied and the override "
                                "count was not"
                            ),
                            source_entity_ids=(circuit.id,),
                        )
                    )
                continue
            shared_with: tuple[str, ...] = ()
            if role in shared_roles and anchor == circuit.id:
                shared_with = tuple(
                    member
                    for member in members
                    if member != circuit.id
                    and any(entry[0] == role for entry in plans[member])
                )
            conductor = _build_conductor(
                model_id=model.model_id,
                circuit=circuit,
                role=role,
                count=count,
                basis=basis,
                inputs_observed=inputs_observed,
                options=options,
                override=override,
                raceway_group=group_id,
                shared_with=shared_with,
                undetermined_reasons=undetermined_reasons,
            )
            conductors.append(conductor)
            derived_ids.append(conductor.id)
            if conductor.size is None:
                unsized_ids.append(conductor.id)

        if unsized_ids:
            warnings.append(
                ConductorWarning(
                    code="size-undetermined",
                    message=(
                        f"{circuit.id}: conductor size was not determinable and was left null "
                        f"({'; '.join(sorted(undetermined_reasons))})"
                    ),
                    source_entity_ids=tuple(sorted(unsized_ids)),
                )
            )

        if derived_ids and not circuit.route_ids:
            warnings.append(
                ConductorWarning(
                    code="circuit-unrouted",
                    message=(
                        f"{circuit.id} has no route_ids; its conductors carry no route and no "
                        "length can be taken off (this lane never invents a path)"
                    ),
                    source_entity_ids=(circuit.id, *sorted(derived_ids)),
                )
            )

    ordered = tuple(
        sorted(conductors, key=lambda item: (item.circuit_id, _ROLE_ORDER[item.role]))
    )
    _assert_postconditions(model, ordered)
    return ConductorDerivation(
        conductors=ordered,
        warnings=tuple(
            sorted(warnings, key=lambda item: (item.code, item.source_entity_ids))
        ),
    )


def _assert_postconditions(model: BuildingModel, conductors: tuple[Conductor, ...]) -> None:
    """Fail closed rather than emit output that breaks this lane's promises."""

    circuits = {circuit.id: circuit for circuit in model.circuits}
    existing = {conductor.id for conductor in model.conductors}
    seen: set[str] = set()
    for conductor in conductors:
        if conductor.id in seen:
            raise ConductorDerivationError(f"duplicate derived conductor id {conductor.id!r}")
        seen.add(conductor.id)
        if conductor.id in existing:
            raise ConductorDerivationError(
                f"derived conductor id {conductor.id!r} collides with an existing conductor"
            )
        expected = stable_id(
            "conductor", f"{model.model_id}:{conductor.circuit_id}:{conductor.role}"
        )
        if conductor.id != expected:
            raise ConductorDerivationError(f"{conductor.id!r} is not the stable derived id")
        circuit = circuits[conductor.circuit_id]
        if tuple(conductor.route_ids) != tuple(circuit.route_ids):
            raise ConductorDerivationError(
                f"{conductor.id!r} route_ids must equal its circuit's route_ids verbatim"
            )
        derivation = conductor.attributes.get("derivation", {})
        facts = derivation.get("facts", {})
        if derivation.get("derivation_class") not in DERIVATION_CLASSES:
            raise ConductorDerivationError(f"{conductor.id!r} has no derivation class")
        if facts.get("count") == CLASS_OBSERVED and not any(
            item.source_kind != SOURCE_KIND for item in conductor.provenance
        ):
            raise ConductorDerivationError(
                f"{conductor.id!r} claims an observed count without caller provenance"
            )
