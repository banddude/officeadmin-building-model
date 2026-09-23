"""Deterministic selection of the raceway or cable that carries a route.

Prime directive for this lane
-----------------------------
A drawing that shows a homerun states a *path*.  It does not state that the
path is 3/4" EMT, that the EMT is 40% full, or that the run is really MC cable
in a joist bay.  Choosing the containment for a set of conductors is an
**engineering inference** built on top of another inference (C4's conductors),
so it is at best as certain as its weakest input and is never as-built.

This module therefore:

1. labels every sizing it derives from a rule as ``"inferred"``;
2. never labels its own rule output ``"observed"`` -- only an explicit
   :class:`RacewayObservation`, carrying the caller's own provenance for the
   thing that was read, may claim an observed or user-designed fact;
3. **refuses a route rather than defaulting it.**  If the raceway kind, a
   conductor's outer dimension, or the fill basis is not stated by the model
   and the caller's reference tables, the route is left unsized and an
   explicit machine-readable reason is recorded *on the route*.  There is no
   "assume 3/4 inch", no "assume THHN", no "assume 40%";
4. never invents or edits geometry.  ``centerline``, ``start_port_id``,
   ``end_port_id`` and ``fitting_ids`` are passed through untouched, and an
   observed ``nominal_diameter_m`` is never overwritten by an inference.

Why no code table ships with this module
----------------------------------------
Conduit fill percentages, conduit interior areas and conductor outer areas are
published in NFPA 70 (NEC) Chapter 9, Tables 1, 4 and 5.  That content is
copyrighted and this repository carries no licence to redistribute it, so
**this module encodes none of it.**  It follows the precedent C4 already set
for ampacity with ``TableSizingPolicy``: the numbers are a caller-supplied,
source-stamped input, validated on the way in, and their absence is a refusal
rather than a default.  Every table object below requires a non-empty
``source`` string precisely so that a derived size can always be traced back
to the edition and table the operator stood behind.

The module is pure: it reads a validated :class:`~oabm.model.BuildingModel`
and returns rewritten :class:`~oabm.model.Route` objects plus warnings.
Callers decide whether and how to attach them, either with
``dataclasses.replace(model, routes=...)`` or the
:func:`apply_raceway_sizing` helper, mirroring the conductor and routing lanes.

Contract note
-------------
The canonical contract has no first-class raceway entity and no ``Route``
field for a trade size, so this lane records its verdict under a single
lane-owned key, ``Route.attributes["raceway"]``, exactly as the importer and
convergence lanes own ``"pdf_electrical"`` and ``"pdf_convergence"``, and
refuses a collision on that key rather than choosing a winner silently.  This
is deliberately not a competing representation of a route: geometry, identity
and ``route_type`` remain the routing lane's, untouched.  The authoritative
statement of the epistemic class is still the canonical
``Provenance.derivation`` field; the attributes carry only the reasoning.  If
and when a first-class raceway field lands, this module maps onto it and the
hard invariants (rule output is never ``observed``; an unresolvable route is
refused with a stated reason) do not depend on where the verdict is stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from oabm.model import (
    DERIVATION_CLASSES,
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    BuildingModel,
    Conductor,
    ContractError,
    Provenance,
    Route,
    validate_model,
)

ALGORITHM = "raceway-sizing-rule-v1"
SOURCE_KIND = "raceway-sizing"

#: Key under which this lane records its verdict on ``Route.attributes``.
RACEWAY_ATTRIBUTE_KEY = "raceway"

KIND_CONDUIT = "conduit"
KIND_CABLE = "cable"
RACEWAY_KINDS: tuple[str, ...] = (KIND_CONDUIT, KIND_CABLE)

CLASS_INFERRED = DERIVATION_INFERRED
CLASS_USER_DESIGNED = DERIVATION_USER
CLASS_OBSERVED = DERIVATION_OBSERVED
_OVERRIDE_CLASSES: tuple[str, ...] = (CLASS_USER_DESIGNED, CLASS_OBSERVED)

# --- refusal vocabulary ----------------------------------------------------
# Every one of these means "this lane declined to size the route", and each
# names the specific missing statement.  A consumer must treat a route
# carrying any of them as UNSIZED, never as "probably fine".
UNRESOLVED_NO_CONDUCTORS = "no-conductors-on-route"
UNRESOLVED_RACEWAY_TYPE_UNKNOWN = "raceway-type-unknown"
UNRESOLVED_NO_DIMENSION_TABLE = "no-conductor-dimension-table"
UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN = "conductor-size-unknown"
UNRESOLVED_CONDUCTOR_INSULATION_UNKNOWN = "conductor-insulation-unknown"
UNRESOLVED_CONDUCTOR_DIMENSION_UNKNOWN = "conductor-dimension-not-in-table"
UNRESOLVED_NO_FILL_RULE = "no-fill-rule"
UNRESOLVED_FILL_LIMIT_UNSTATED = "fill-limit-unstated-for-conductor-count"
UNRESOLVED_NO_TRADE_SIZE_CARRIES_FILL = "no-trade-size-carries-the-fill"
UNRESOLVED_MIXED_CABLE_SIZES = "cable-needs-one-conductor-size"
UNRESOLVED_CABLE_NOT_IN_CATALOG = "cable-not-in-catalog"

UNRESOLVED_REASONS: tuple[str, ...] = (
    UNRESOLVED_CABLE_NOT_IN_CATALOG,
    UNRESOLVED_CONDUCTOR_DIMENSION_UNKNOWN,
    UNRESOLVED_CONDUCTOR_INSULATION_UNKNOWN,
    UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN,
    UNRESOLVED_FILL_LIMIT_UNSTATED,
    UNRESOLVED_MIXED_CABLE_SIZES,
    UNRESOLVED_NO_CONDUCTORS,
    UNRESOLVED_NO_DIMENSION_TABLE,
    UNRESOLVED_NO_FILL_RULE,
    UNRESOLVED_NO_TRADE_SIZE_CARRIES_FILL,
    UNRESOLVED_RACEWAY_TYPE_UNKNOWN,
)


class RacewaySizingError(ValueError):
    """Raised when canonical inputs are unsafe or inconsistent for sizing."""


@dataclass(frozen=True, slots=True)
class RacewayWarning:
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


def _require_source(value: Any, label: str) -> str:
    """Every reference table must name where its numbers came from."""

    if not isinstance(value, str) or not value.strip():
        raise RacewaySizingError(
            f"{label}.source must be a non-empty string naming the published table "
            "these numbers come from (edition, table number); this lane ships no "
            "table of its own and will not accept anonymous numbers"
        )
    return value


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RacewaySizingError(f"{label} must be a non-empty string")
    return value


def _require_area(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RacewaySizingError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise RacewaySizingError(f"{label} must be finite and > 0")
    return value


@dataclass(frozen=True, slots=True)
class ConductorDimensionTable:
    """Outer cross-sectional area of an insulated conductor, by insulation and size.

    Caller-supplied.  ``entries`` is ``(((insulation, size), area_m2), ...)``.
    Keyed by *both* insulation and size because the outer area of a conductor
    is a property of the insulation system, not of the copper: the same "12 AWG"
    is a different obstruction in different insulations.  A lane that keyed on
    size alone would be silently wrong for every run that is not the insulation
    the table's author had in mind.
    """

    table_id: str
    source: str
    entries: tuple[tuple[tuple[str, str], float], ...]

    def __post_init__(self) -> None:
        _require_id(self.table_id, "ConductorDimensionTable.table_id")
        _require_source(self.source, "ConductorDimensionTable")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise RacewaySizingError(
                "ConductorDimensionTable.entries must be a non-empty tuple of "
                "((insulation, size), area_m2)"
            )
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise RacewaySizingError(
                    "ConductorDimensionTable.entries items must be ((insulation, size), area_m2)"
                )
            key, area = entry
            if not isinstance(key, tuple) or len(key) != 2:
                raise RacewaySizingError(
                    "ConductorDimensionTable entry key must be an (insulation, size) pair"
                )
            insulation, size = key
            _require_id(insulation, "ConductorDimensionTable insulation")
            _require_id(size, "ConductorDimensionTable size")
            _require_area(area, f"ConductorDimensionTable area for {key!r}")
            if key in seen:
                raise RacewaySizingError(
                    f"ConductorDimensionTable repeats entry {key!r}"
                )
            seen.add(key)

    def area_for(self, insulation: str, size: str) -> float | None:
        for (entry_insulation, entry_size), area in self.entries:
            if entry_insulation == insulation and entry_size == size:
                return float(area)
        return None


@dataclass(frozen=True, slots=True)
class FillRule:
    """The permitted fraction of a raceway's interior area, by conductor count.

    Caller-supplied and source-stamped.  Published fill tables are shaped as a
    few exact counts plus an "over N conductors" row, so this mirrors that
    shape rather than inventing a curve:

    * ``exact_limits`` -- ``((count, fraction), ...)`` for counts stated
      individually;
    * ``over_limit``   -- ``(threshold, fraction)`` for "more than *threshold*
      conductors", or ``None`` if the caller's table states no such row.

    A conductor count that neither covers is **refused**, not rounded to the
    nearest stated row.  ``fraction`` is a fraction of 1, not a percentage.
    """

    rule_id: str
    source: str
    exact_limits: tuple[tuple[int, float], ...] = ()
    over_limit: tuple[int, float] | None = None

    def __post_init__(self) -> None:
        _require_id(self.rule_id, "FillRule.rule_id")
        _require_source(self.source, "FillRule")
        if not isinstance(self.exact_limits, tuple):
            raise RacewaySizingError("FillRule.exact_limits must be a tuple")
        seen: set[int] = set()
        for entry in self.exact_limits:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise RacewaySizingError(
                    "FillRule.exact_limits items must be (count, fraction) pairs"
                )
            count, fraction = entry
            _require_count(count, "FillRule.exact_limits count")
            _require_fraction(fraction, f"FillRule.exact_limits fraction for {count}")
            if count in seen:
                raise RacewaySizingError(f"FillRule repeats a limit for count {count}")
            seen.add(count)
        if self.over_limit is not None:
            if not isinstance(self.over_limit, tuple) or len(self.over_limit) != 2:
                raise RacewaySizingError(
                    "FillRule.over_limit must be a (threshold, fraction) pair or None"
                )
            threshold, fraction = self.over_limit
            _require_count(threshold, "FillRule.over_limit threshold")
            _require_fraction(fraction, "FillRule.over_limit fraction")
        if not self.exact_limits and self.over_limit is None:
            raise RacewaySizingError(
                "FillRule states no limit at all; supply exact_limits, over_limit, or both"
            )

    def limit_for(self, count: int) -> tuple[float, str] | None:
        """Return ``(fraction, basis)`` for *count* conductors, or ``None``."""

        for entry_count, fraction in self.exact_limits:
            if entry_count == count:
                return float(fraction), f"fill-limit:{self.rule_id}:exact:{count}"
        if self.over_limit is not None:
            threshold, fraction = self.over_limit
            if count > threshold:
                return (
                    float(fraction),
                    f"fill-limit:{self.rule_id}:over:{threshold}",
                )
        return None


def _require_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RacewaySizingError(f"{label} must be an integer >= 1")
    return value


def _require_fraction(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RacewaySizingError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 < value <= 1.0:
        raise RacewaySizingError(f"{label} must be a fraction in (0, 1]")
    return value


@dataclass(frozen=True, slots=True)
class TradeSize:
    """One orderable raceway size and the interior area the caller's table states.

    ``nominal_diameter_m`` is optional and is only written onto a Route when the
    caller's catalog actually states it.  This lane will not back-compute a
    diameter from an area: interior area and nominal trade diameter are
    different published quantities and conflating them invents a dimension.
    """

    designation: str
    interior_area_m2: float
    nominal_diameter_m: float | None = None

    def __post_init__(self) -> None:
        _require_id(self.designation, "TradeSize.designation")
        _require_area(self.interior_area_m2, f"TradeSize {self.designation} interior_area_m2")
        if self.nominal_diameter_m is not None:
            _require_area(
                self.nominal_diameter_m, f"TradeSize {self.designation} nominal_diameter_m"
            )


@dataclass(frozen=True, slots=True)
class RacewayCatalog:
    """An ordered run of trade sizes for one kind of conduit/tubing."""

    catalog_id: str
    source: str
    trade_sizes: tuple[TradeSize, ...]
    kind: str = KIND_CONDUIT

    def __post_init__(self) -> None:
        _require_id(self.catalog_id, "RacewayCatalog.catalog_id")
        _require_source(self.source, "RacewayCatalog")
        if self.kind != KIND_CONDUIT:
            raise RacewaySizingError("RacewayCatalog.kind must be 'conduit'")
        if not isinstance(self.trade_sizes, tuple) or not self.trade_sizes:
            raise RacewaySizingError(
                "RacewayCatalog.trade_sizes must be a non-empty tuple of TradeSize"
            )
        previous = 0.0
        seen: set[str] = set()
        for entry in self.trade_sizes:
            if not isinstance(entry, TradeSize):
                raise RacewaySizingError("RacewayCatalog.trade_sizes items must be TradeSize")
            if entry.interior_area_m2 <= previous:
                raise RacewaySizingError(
                    "RacewayCatalog.trade_sizes must ascend strictly by interior_area_m2 "
                    "so that 'the smallest that carries the fill' is well defined"
                )
            previous = entry.interior_area_m2
            if entry.designation in seen:
                raise RacewaySizingError(
                    f"RacewayCatalog repeats trade size {entry.designation!r}"
                )
            seen.add(entry.designation)


@dataclass(frozen=True, slots=True)
class CableEntry:
    """One orderable cable: a conductor count and size, as a manufactured assembly."""

    designation: str
    conductor_count: int
    conductor_size: str
    outside_diameter_m: float | None = None

    def __post_init__(self) -> None:
        _require_id(self.designation, "CableEntry.designation")
        _require_count(self.conductor_count, f"CableEntry {self.designation} conductor_count")
        _require_id(self.conductor_size, f"CableEntry {self.designation} conductor_size")
        if self.outside_diameter_m is not None:
            _require_area(
                self.outside_diameter_m, f"CableEntry {self.designation} outside_diameter_m"
            )


@dataclass(frozen=True, slots=True)
class CableCatalog:
    """Manufactured cable assemblies, selected by conductor count and size.

    A cable is not sized by fill: it is an assembly that either exists in the
    stated count/size combination or does not.  A combination the catalog does
    not list is refused, never rounded up to the next listed cable.
    """

    catalog_id: str
    source: str
    entries: tuple[CableEntry, ...]
    kind: str = KIND_CABLE

    def __post_init__(self) -> None:
        _require_id(self.catalog_id, "CableCatalog.catalog_id")
        _require_source(self.source, "CableCatalog")
        if self.kind != KIND_CABLE:
            raise RacewaySizingError("CableCatalog.kind must be 'cable'")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise RacewaySizingError(
                "CableCatalog.entries must be a non-empty tuple of CableEntry"
            )
        seen: set[tuple[int, str]] = set()
        for entry in self.entries:
            if not isinstance(entry, CableEntry):
                raise RacewaySizingError("CableCatalog.entries items must be CableEntry")
            key = (entry.conductor_count, entry.conductor_size)
            if key in seen:
                raise RacewaySizingError(
                    f"CableCatalog repeats the {key[0]}-conductor {key[1]!r} assembly"
                )
            seen.add(key)

    def entry_for(self, conductor_count: int, conductor_size: str) -> CableEntry | None:
        for entry in self.entries:
            if entry.conductor_count == conductor_count and entry.conductor_size == conductor_size:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class RacewayObservation:
    """A raceway a source actually stated, injected in place of the sizing rules.

    This is the only channel by which a route's raceway is ever labelled
    anything other than ``inferred``.  ``derivation_class`` is required, and an
    ``observed`` observation must carry the provenance of the thing that was
    read (a riser note, a field survey row).
    """

    derivation_class: str
    designation: str
    kind: str
    provenance: tuple[Provenance, ...] = ()
    nominal_diameter_m: float | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.derivation_class not in _OVERRIDE_CLASSES:
            raise RacewaySizingError(
                "RacewayObservation.derivation_class must be one of "
                f"{_OVERRIDE_CLASSES}; got {self.derivation_class!r}"
            )
        _require_id(self.designation, "RacewayObservation.designation")
        if self.kind not in RACEWAY_KINDS:
            raise RacewaySizingError(
                f"RacewayObservation.kind must be one of {RACEWAY_KINDS}; got {self.kind!r}"
            )
        if not isinstance(self.provenance, tuple) or not all(
            isinstance(item, Provenance) for item in self.provenance
        ):
            raise RacewaySizingError(
                "RacewayObservation.provenance must be a tuple of Provenance"
            )
        if self.derivation_class == CLASS_OBSERVED and not self.provenance:
            raise RacewaySizingError(
                "an observed raceway must cite provenance for what was read"
            )
        if self.nominal_diameter_m is not None:
            _require_area(self.nominal_diameter_m, "RacewayObservation.nominal_diameter_m")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise RacewaySizingError("RacewayObservation.confidence must be a number")
            value = float(self.confidence)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise RacewaySizingError(
                    "RacewayObservation.confidence must be between 0 and 1"
                )


@dataclass(frozen=True, slots=True)
class RacewaySizingOptions:
    """Engineering policy knobs.  None of these change the canonical contract.

    ``catalogs`` maps a ``Route.route_type`` token onto the catalog that
    governs it.  There is deliberately no fallback: ``route_type`` is a free
    string in the canonical contract, so this lane cannot tell whether an
    unmapped token like ``"emt"``, ``"mc"`` or ``"j-hook"`` means conduit,
    cable or neither.  Guessing from the spelling is exactly the kind of
    default this lane exists to refuse.
    """

    catalogs: Mapping[str, RacewayCatalog | CableCatalog] = field(
        default_factory=lambda: MappingProxyType({})
    )
    dimensions: ConductorDimensionTable | None = None
    fill_rule: FillRule | None = None
    observations: Mapping[str, RacewayObservation] = field(
        default_factory=lambda: MappingProxyType({})
    )
    confidence_full_inputs: float = 0.7

    def __post_init__(self) -> None:
        if not isinstance(self.catalogs, Mapping):
            raise RacewaySizingError("catalogs must be a mapping of route_type to catalog")
        for route_type in sorted(self.catalogs):
            if not isinstance(route_type, str) or not route_type:
                raise RacewaySizingError("catalogs keys must be non-empty route_type strings")
            if not isinstance(self.catalogs[route_type], (RacewayCatalog, CableCatalog)):
                raise RacewaySizingError(
                    f"catalogs[{route_type!r}] must be a RacewayCatalog or CableCatalog"
                )
        if self.dimensions is not None and not isinstance(
            self.dimensions, ConductorDimensionTable
        ):
            raise RacewaySizingError("dimensions must be a ConductorDimensionTable or None")
        if self.fill_rule is not None and not isinstance(self.fill_rule, FillRule):
            raise RacewaySizingError("fill_rule must be a FillRule or None")
        if not isinstance(self.observations, Mapping):
            raise RacewaySizingError(
                "observations must be a mapping of route id to RacewayObservation"
            )
        for route_id in sorted(self.observations):
            if not isinstance(self.observations[route_id], RacewayObservation):
                raise RacewaySizingError(
                    f"observations[{route_id!r}] must be a RacewayObservation"
                )
        value = self.confidence_full_inputs
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RacewaySizingError("confidence_full_inputs must be a number")
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise RacewaySizingError("confidence_full_inputs must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RacewaySizing:
    """Rewritten routes plus the reasons the output is what it is.

    Unpacks as ``routes, warnings = size_raceways(model)``.  ``routes`` is the
    model's full route tuple in its original order, with this lane's verdict
    recorded on each one; routes it refused carry an explicit unresolved
    reason rather than a size.
    """

    routes: tuple[Route, ...]
    warnings: tuple[RacewayWarning, ...]

    def __iter__(self) -> Iterator[Any]:
        yield self.routes
        yield self.warnings

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(
            route.id
            for route in self.routes
            if route.attributes.get(RACEWAY_ATTRIBUTE_KEY, {}).get("resolved") is True
        )

    @property
    def unresolved_ids(self) -> tuple[str, ...]:
        return tuple(
            route.id
            for route in self.routes
            if route.attributes.get(RACEWAY_ATTRIBUTE_KEY, {}).get("resolved") is False
        )


def _conductors_on_route(
    route_id: str, conductors: Sequence[Conductor]
) -> tuple[Conductor, ...]:
    return tuple(
        sorted(
            (item for item in conductors if route_id in tuple(item.route_ids)),
            key=lambda item: item.id,
        )
    )


def _total_conductor_count(conductors: Sequence[Conductor]) -> int:
    return sum(int(item.count) for item in conductors)


@dataclass(frozen=True, slots=True)
class _Refusal:
    reason: str
    detail: str
    entity_ids: tuple[str, ...] = ()


def _required_area(
    conductors: Sequence[Conductor],
    dimensions: ConductorDimensionTable | None,
) -> tuple[float, list[str]] | _Refusal:
    """Sum the caller's stated outer areas, or refuse naming the first gap."""

    if dimensions is None:
        return _Refusal(
            UNRESOLVED_NO_DIMENSION_TABLE,
            "no conductor dimension table was supplied, so no conductor's outer area "
            "is known; this lane ships no table and will not estimate one",
        )
    total = 0.0
    basis: list[str] = [f"conductor-dimensions:{dimensions.table_id}"]
    for conductor in conductors:
        if conductor.size is None:
            return _Refusal(
                UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN,
                f"conductor {conductor.id} has no size, so its outer dimension cannot "
                "be looked up (the conductor lane leaves an undeterminable size null "
                "rather than guessing, and this lane will not guess on its behalf)",
                (conductor.id,),
            )
        if conductor.insulation is None:
            return _Refusal(
                UNRESOLVED_CONDUCTOR_INSULATION_UNKNOWN,
                f"conductor {conductor.id} has no insulation; outer area is a property "
                "of the insulation system, so size alone cannot resolve a dimension",
                (conductor.id,),
            )
        area = dimensions.area_for(conductor.insulation, conductor.size)
        if area is None:
            return _Refusal(
                UNRESOLVED_CONDUCTOR_DIMENSION_UNKNOWN,
                f"conductor {conductor.id} is {conductor.size!r} in "
                f"{conductor.insulation!r}, which table {dimensions.table_id!r} does "
                "not list",
                (conductor.id,),
            )
        total += area * int(conductor.count)
    return total, basis


def _select_conduit(
    catalog: RacewayCatalog,
    required_area_m2: float,
    conductor_count: int,
    fill_rule: FillRule | None,
) -> tuple[TradeSize, float, list[str]] | _Refusal:
    if fill_rule is None:
        return _Refusal(
            UNRESOLVED_NO_FILL_RULE,
            "no fill rule was supplied, so the permitted fraction of the raceway "
            "interior is unknown; this lane ships no fill table and will not assume one",
        )
    limit = fill_rule.limit_for(conductor_count)
    if limit is None:
        return _Refusal(
            UNRESOLVED_FILL_LIMIT_UNSTATED,
            f"fill rule {fill_rule.rule_id!r} states no limit for {conductor_count} "
            "conductors, and this lane will not reuse the nearest stated row",
        )
    fraction, limit_basis = limit
    for trade_size in catalog.trade_sizes:
        # No tolerance is applied on purpose.  Rounding belongs to the caller's
        # published table, not to an epsilon invented here.
        if required_area_m2 <= trade_size.interior_area_m2 * fraction:
            return (
                trade_size,
                fraction,
                [f"conduit-catalog:{catalog.catalog_id}", limit_basis],
            )
    return _Refusal(
        UNRESOLVED_NO_TRADE_SIZE_CARRIES_FILL,
        f"{conductor_count} conductors need {required_area_m2:.9g} m2 of fill, which "
        f"exceeds every trade size in catalog {catalog.catalog_id!r} at a "
        f"{fraction:.9g} fill limit",
    )


def _select_cable(
    catalog: CableCatalog, conductors: Sequence[Conductor]
) -> tuple[CableEntry, list[str]] | _Refusal:
    sizes = {conductor.size for conductor in conductors}
    if None in sizes:
        missing = sorted(item.id for item in conductors if item.size is None)
        return _Refusal(
            UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN,
            f"conductors {missing} have no size, so no cable assembly can be selected",
            tuple(missing),
        )
    if len(sizes) != 1:
        return _Refusal(
            UNRESOLVED_MIXED_CABLE_SIZES,
            f"the conductors on this route are {sorted(str(item) for item in sizes)}; a "
            "cable assembly is listed by a single conductor size and this lane will not "
            "pick which size the cable is",
        )
    size = sizes.pop()
    assert size is not None  # narrowed by the None check above
    count = _total_conductor_count(conductors)
    entry = catalog.entry_for(count, size)
    if entry is None:
        return _Refusal(
            UNRESOLVED_CABLE_NOT_IN_CATALOG,
            f"catalog {catalog.catalog_id!r} lists no {count}-conductor {size!r} "
            "assembly, and this lane will not substitute the next listed cable",
        )
    return entry, [f"cable-catalog:{catalog.catalog_id}"]


def _record(
    route: Route,
    *,
    model_id: str,
    payload: dict[str, Any],
    derivation_class: str,
    confidence: float,
    extra_provenance: tuple[Provenance, ...] = (),
    nominal_diameter_m: float | None = None,
) -> Route:
    """Return *route* rewritten with this lane's verdict, geometry untouched."""

    if RACEWAY_ATTRIBUTE_KEY in route.attributes:
        # Matches the convergence lane: a lane owns exactly one attribute key
        # and refuses a collision rather than choosing a winner silently. If an
        # importer already recorded a stated raceway here, an inference must
        # not quietly replace it.
        raise RacewaySizingError(
            f"{route.id} already carries {RACEWAY_ATTRIBUTE_KEY!r} attributes; refusing "
            "to overwrite an existing raceway statement with a derived one"
        )

    payload = dict(payload)
    payload["algorithm"] = ALGORITHM
    payload["derivation_class"] = derivation_class
    # A derived raceway is a proposal, not a record of what is in the wall.
    # Stated explicitly so a renderer has to opt in to lying rather than
    # merely forget to check.
    payload["as_built"] = False

    provenance = Provenance(
        source_kind=SOURCE_KIND,
        source_id=model_id,
        source_element_id=route.id,
        method=ALGORITHM,
        confidence=confidence,
        # The canonical field is the authoritative statement; attributes keep
        # the reasoning. No consumer should have to read attributes to learn
        # the class.
        derivation=derivation_class,
        attributes=dict(payload),
    )

    attributes = dict(route.attributes)
    attributes[RACEWAY_ATTRIBUTE_KEY] = payload

    changes: dict[str, Any] = {
        "attributes": attributes,
        "provenance": (*route.provenance, *extra_provenance, provenance),
    }
    if nominal_diameter_m is not None and route.nominal_diameter_m is None:
        changes["nominal_diameter_m"] = nominal_diameter_m
    return replace(route, **changes)


def size_raceways(
    model: BuildingModel,
    options: RacewaySizingOptions | None = None,
) -> RacewaySizing:
    """Select the raceway or cable for every route in *model*.

    Pure and deterministic.  Never edits geometry, never overwrites a
    ``nominal_diameter_m`` a source already stated, and labels every
    rule-derived sizing inferred.  Routes whose sizing cannot be resolved from
    what the model and the caller's tables actually state are recorded with an
    explicit unresolved reason instead of a default.
    """

    if not isinstance(model, BuildingModel):
        raise RacewaySizingError("size_raceways requires a BuildingModel")
    if options is None:
        options = RacewaySizingOptions()
    if not isinstance(options, RacewaySizingOptions):
        raise RacewaySizingError("options must be a RacewaySizingOptions")

    # Boundary re-validation, like the conductor and quantities lanes.
    try:
        validate_model(model)
    except ContractError as exc:
        raise RacewaySizingError(f"model is not sizable: {exc}") from exc

    route_ids = {route.id for route in model.routes}
    unknown = sorted(set(options.observations) - route_ids)
    if unknown:
        raise RacewaySizingError(f"observations name unknown route ids {unknown}")

    warnings: list[RacewayWarning] = []
    rewritten: list[Route] = []

    for route in model.routes:
        conductors = _conductors_on_route(route.id, model.conductors)
        observation = options.observations.get(route.id)

        if observation is not None:
            confidence = (
                float(observation.confidence)
                if observation.confidence is not None
                else (
                    max(item.confidence for item in observation.provenance)
                    if observation.provenance
                    else options.confidence_full_inputs
                )
            )
            payload: dict[str, Any] = {
                "resolved": True,
                "kind": observation.kind,
                "designation": observation.designation,
                "basis": [f"observation:{observation.derivation_class}"],
                "conductor_ids": [item.id for item in conductors],
            }
            rewritten.append(
                _record(
                    route,
                    model_id=model.model_id,
                    payload=payload,
                    derivation_class=observation.derivation_class,
                    confidence=confidence,
                    extra_provenance=observation.provenance,
                    nominal_diameter_m=observation.nominal_diameter_m,
                )
            )
            if (
                observation.nominal_diameter_m is not None
                and route.nominal_diameter_m is not None
            ):
                warnings.append(
                    RacewayWarning(
                        code="route-diameter-not-overwritten",
                        message=(
                            f"{route.id} already states a nominal_diameter_m; the stated "
                            "raceway was recorded in attributes but the existing diameter "
                            "was left alone"
                        ),
                        source_entity_ids=(route.id,),
                    )
                )
            continue

        refusal: _Refusal | None = None
        payload = {}
        chosen_diameter: float | None = None

        if not conductors:
            refusal = _Refusal(
                UNRESOLVED_NO_CONDUCTORS,
                f"{route.id} carries no conductors, so there is nothing to size; a "
                "raceway on an empty path would be pure invention",
            )
        else:
            catalog = options.catalogs.get(route.route_type)
            if catalog is None:
                refusal = _Refusal(
                    UNRESOLVED_RACEWAY_TYPE_UNKNOWN,
                    f"route_type {route.route_type!r} is not mapped to a catalog, so "
                    "this lane cannot tell whether it is conduit, cable or neither, and "
                    "will not infer the kind from the spelling of the token",
                )
            elif isinstance(catalog, CableCatalog):
                selected = _select_cable(catalog, conductors)
                if isinstance(selected, _Refusal):
                    refusal = selected
                else:
                    entry, basis = selected
                    chosen_diameter = entry.outside_diameter_m
                    payload = {
                        "resolved": True,
                        "kind": KIND_CABLE,
                        "designation": entry.designation,
                        "conductor_count": entry.conductor_count,
                        "conductor_size": entry.conductor_size,
                        "basis": basis,
                        "conductor_ids": [item.id for item in conductors],
                        "table_sources": [catalog.source],
                    }
            else:
                area = _required_area(conductors, options.dimensions)
                if isinstance(area, _Refusal):
                    refusal = area
                else:
                    required_area_m2, basis = area
                    count = _total_conductor_count(conductors)
                    selected = _select_conduit(
                        catalog, required_area_m2, count, options.fill_rule
                    )
                    if isinstance(selected, _Refusal):
                        refusal = selected
                    else:
                        trade_size, fraction, select_basis = selected
                        chosen_diameter = trade_size.nominal_diameter_m
                        sources = [catalog.source]
                        if options.dimensions is not None:
                            sources.append(options.dimensions.source)
                        if options.fill_rule is not None:
                            sources.append(options.fill_rule.source)
                        payload = {
                            "resolved": True,
                            "kind": KIND_CONDUIT,
                            "designation": trade_size.designation,
                            "conductor_count": count,
                            "required_area_m2": required_area_m2,
                            "interior_area_m2": trade_size.interior_area_m2,
                            "fill_limit_fraction": fraction,
                            "fill_fraction_used": required_area_m2
                            / trade_size.interior_area_m2,
                            "basis": [*basis, *select_basis],
                            "conductor_ids": [item.id for item in conductors],
                            "table_sources": sources,
                        }

        if refusal is not None:
            payload = {
                "resolved": False,
                "unresolved_reason": refusal.reason,
                "detail": refusal.detail,
                "conductor_ids": [item.id for item in conductors],
            }
            warnings.append(
                RacewayWarning(
                    code=refusal.reason,
                    message=f"{route.id}: {refusal.detail}",
                    source_entity_ids=tuple(sorted({route.id, *refusal.entity_ids})),
                )
            )

        # An inference about a route's contents can never be more certain than
        # the least certain thing it contains, nor than the route itself.
        band = options.confidence_full_inputs
        confidences = [float(route.confidence), float(band)]
        confidences.extend(float(item.confidence) for item in conductors)
        confidence = min(confidences)

        rewritten.append(
            _record(
                route,
                model_id=model.model_id,
                payload=payload,
                derivation_class=CLASS_INFERRED,
                confidence=confidence,
                nominal_diameter_m=chosen_diameter,
            )
        )

        if (
            chosen_diameter is not None
            and route.nominal_diameter_m is not None
            and route.nominal_diameter_m != chosen_diameter
        ):
            warnings.append(
                RacewayWarning(
                    code="route-diameter-not-overwritten",
                    message=(
                        f"{route.id} already states nominal_diameter_m "
                        f"{route.nominal_diameter_m!r}; the derived size implies "
                        f"{chosen_diameter!r} and the existing value was left alone "
                        "(an inference never overwrites a stated dimension)"
                    ),
                    source_entity_ids=(route.id,),
                )
            )

    ordered = tuple(rewritten)
    _assert_postconditions(model, ordered, options)
    return RacewaySizing(
        routes=ordered,
        warnings=tuple(
            sorted(warnings, key=lambda item: (item.code, item.source_entity_ids))
        ),
    )


def apply_raceway_sizing(model: BuildingModel, sizing: RacewaySizing) -> BuildingModel:
    """Attach *sizing* to *model*, re-validating the result.

    Provided because "record it on the model" is this lane's job; the caller
    still chooses when to do it, matching the conductor lane's contract.
    """

    if not isinstance(sizing, RacewaySizing):
        raise RacewaySizingError("apply_raceway_sizing requires a RacewaySizing")
    updated = replace(model, routes=sizing.routes)
    try:
        validate_model(updated)
    except ContractError as exc:  # pragma: no cover - defensive
        raise RacewaySizingError(f"sized model is not valid: {exc}") from exc
    return updated


def _assert_postconditions(
    model: BuildingModel,
    routes: tuple[Route, ...],
    options: RacewaySizingOptions,
) -> None:
    """Fail closed rather than emit output that breaks this lane's promises."""

    before = {route.id: route for route in model.routes}
    if len(routes) != len(before):
        raise RacewaySizingError("raceway sizing must not add or drop routes")
    for route in routes:
        original = before.get(route.id)
        if original is None:
            raise RacewaySizingError(f"raceway sizing invented route {route.id!r}")
        # Geometry is the routing lane's business. This lane never touches it.
        if (
            route.centerline != original.centerline
            or route.start_port_id != original.start_port_id
            or route.end_port_id != original.end_port_id
            or tuple(route.fitting_ids) != tuple(original.fitting_ids)
            or route.route_type != original.route_type
        ):
            raise RacewaySizingError(
                f"{route.id!r}: raceway sizing must not alter route geometry or type"
            )
        if (
            original.nominal_diameter_m is not None
            and route.nominal_diameter_m != original.nominal_diameter_m
        ):
            raise RacewaySizingError(
                f"{route.id!r}: an inferred raceway must not overwrite a stated "
                "nominal_diameter_m"
            )

        payload = route.attributes.get(RACEWAY_ATTRIBUTE_KEY)
        if not isinstance(payload, dict):
            raise RacewaySizingError(f"{route.id!r} carries no raceway verdict")
        if payload.get("as_built") is not False:
            raise RacewaySizingError(
                f"{route.id!r} must state as_built=False; a derived raceway is never as-built"
            )
        resolved = payload.get("resolved")
        if resolved is False:
            reason = payload.get("unresolved_reason")
            if reason not in UNRESOLVED_REASONS:
                raise RacewaySizingError(
                    f"{route.id!r} is unresolved without a known reason; got {reason!r}"
                )
            if route.nominal_diameter_m != original.nominal_diameter_m:
                raise RacewaySizingError(
                    f"{route.id!r} is unresolved but a diameter was written anyway"
                )
        elif resolved is not True:
            raise RacewaySizingError(f"{route.id!r} has no resolved flag")

        own = [item for item in route.provenance if item.source_kind == SOURCE_KIND]
        if len(own) != 1 or own[0].derivation not in DERIVATION_CLASSES:
            raise RacewaySizingError(
                f"{route.id!r} has no single canonical raceway derivation class"
            )
        observation = options.observations.get(route.id)
        if own[0].derivation != CLASS_INFERRED:
            # Only an explicit observation may make a stronger claim, and an
            # observed one must carry the caller's own provenance.
            if observation is None:
                raise RacewaySizingError(
                    f"{route.id!r} claims {own[0].derivation!r} without an observation"
                )
            if own[0].derivation == CLASS_OBSERVED and not any(
                item.source_kind != SOURCE_KIND for item in route.provenance
            ):
                raise RacewaySizingError(
                    f"{route.id!r} claims an observed raceway without caller provenance"
                )
