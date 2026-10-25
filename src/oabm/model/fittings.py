"""Explicit, exact-product route-fitting selections (canonical contract 1.5)."""

from dataclasses import dataclass
import re

from .common import ContractError, Entity, Provenance, _positive, _validate_id
from .physical import CatalogProduct
from .raceway import RuleReference
from .common import Size3

FITTING_TYPES = frozenset({"coupling", "connector"})
FITTING_ROUTE_TYPE = "emt"
FITTING_INTERFACE_ROLES = frozenset({"raceway", "enclosure"})
FITTING_CONNECTION_METHODS = frozenset({
    "set_screw", "compression", "threaded", "clamp", "snap_in",
})
FITTING_WIRING_METHODS = frozenset({"raceway", "cable"})
FITTING_GEOMETRY_BASES = frozenset({"dimension_derived_envelope", "metadata_only"})
_INTERFACE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
FITTING_SELECTION_REASONS = frozenset({
    "nonmaterial_fitting", "unsupported_fitting_type", "wrong_route_type", "missing_requested_product",
    "missing_catalog_product", "missing_rule_reference", "product_not_permitted",
    "missing_route_product", "fitting_port_missing", "counterpart_port_missing",
    "port_connectivity_missing", "counterpart_interface_unknown", "mating_pair_unknown",
    "mating_pair_conflict", "interface_port_owner_mismatch", "interface_geometry_mismatch",
    "unsupported_joint_topology", "incompatible_wiring_method",
    "actual_od_unknown", "environment_unknown", "environment_conflict",
    "listing_reference_missing",
})
def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ContractError(f"{label} must be non-empty")


def _tokens(values: tuple[str, ...], allowed: frozenset[str], label: str, *, nonempty: bool = True) -> None:
    if (not isinstance(values, tuple) or (nonempty and not values) or
            any(not isinstance(value, str) or value not in allowed for value in values) or
            len(values) != len(set(values))):
        raise ContractError(f"{label} must contain unique supported tokens")


def _provenance_tuple(values: tuple[Provenance, ...], label: str) -> None:
    if not isinstance(values, tuple) or any(not isinstance(value, Provenance) for value in values):
        raise ContractError(f"{label} must be a tuple of Provenance records")


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptedODRange:
    """Catalog-stated accepted actual outside-diameter range, in metres.

    This records product evidence only. Contract 1.5 has no actual-OD input on
    routes or ports and never compares this range.
    """

    min_m: float
    max_m: float

    def __post_init__(self) -> None:
        _positive(self.min_m, "AcceptedODRange.min_m")
        _positive(self.max_m, "AcceptedODRange.max_m")
        if self.min_m > self.max_m:
            raise ContractError("AcceptedODRange.min_m must be <= max_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingSelection(Entity):
    """One explicit exact-product choice for one existing material fitting."""

    intent_id: str
    fitting_id: str
    route_id: str
    product: CatalogProduct
    route_type: str
    fitting_type: str
    basis: RuleReference
    geometry_basis: str
    size: Size3 | None = None

    def __post_init__(self) -> None:
        super(FittingSelection, self).__post_init__()
        _provenance_tuple(self.provenance, f"{self.id}.provenance")
        for name in ("intent_id", "fitting_id", "route_id"):
            _validate_id(getattr(self, name), f"{self.id}.{name}")
        if not isinstance(self.product, CatalogProduct) or not isinstance(self.basis, RuleReference):
            raise ContractError(f"{self.id} requires a typed product and caller rule reference")
        if not isinstance(self.route_type, str) or self.route_type != FITTING_ROUTE_TYPE:
            raise ContractError(f"{self.id}.route_type is unsupported in contract 1.5")
        if not isinstance(self.fitting_type, str) or self.fitting_type not in FITTING_TYPES:
            raise ContractError(f"{self.id}.fitting_type is unsupported in contract 1.5")
        if not isinstance(self.geometry_basis, str) or self.geometry_basis not in FITTING_GEOMETRY_BASES:
            raise ContractError(f"{self.id}.geometry_basis must be an emitted envelope or metadata_only")
        if self.size is not None and not isinstance(self.size, Size3):
            raise ContractError(f"{self.id}.size must be a typed Size3 or null")
        if (self.geometry_basis == "dimension_derived_envelope") != (self.size is not None):
            raise ContractError(f"{self.id}.size must match its emitted geometry_basis")


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingInterface(Entity):
    """One sourced interface plus caller-bound, explicit canonical mate ports."""

    selection_id: str
    interface_key: str
    role: str
    connection_method: str
    permitted_wiring_methods: tuple[str, ...]
    port_id: str
    mating_port_id: str
    mating_product: CatalogProduct
    mating_interface_key: str
    mating_pair_id: str
    nominal_designation: str | None = None
    actual_accepted_od_m: AcceptedODRange | None = None
    dry_location: bool | None = None
    wet_location: bool | None = None
    raintight: bool | None = None
    listing_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(FittingInterface, self).__post_init__()
        _provenance_tuple(self.provenance, f"{self.id}.provenance")
        for name in ("selection_id", "port_id", "mating_port_id"):
            _validate_id(getattr(self, name), f"{self.id}.{name}")
        for name in ("interface_key", "mating_interface_key", "mating_pair_id"):
            _text(getattr(self, name), f"{self.id}.{name}")
        if (not _INTERFACE_KEY.fullmatch(self.interface_key) or
                not _INTERFACE_KEY.fullmatch(self.mating_interface_key) or
                not _INTERFACE_KEY.fullmatch(self.mating_pair_id)):
            raise ContractError(f"{self.id} interface keys must be stable tokens")
        if self.port_id == self.mating_port_id:
            raise ContractError(f"{self.id} cannot mate a port to itself")
        if not isinstance(self.role, str) or self.role not in FITTING_INTERFACE_ROLES:
            raise ContractError(f"{self.id}.role is unsupported in contract 1.5")
        if not isinstance(self.connection_method, str) or self.connection_method not in FITTING_CONNECTION_METHODS:
            raise ContractError(f"{self.id}.connection_method is unsupported")
        _tokens(self.permitted_wiring_methods, FITTING_WIRING_METHODS,
                f"{self.id}.permitted_wiring_methods")
        if not isinstance(self.mating_product, CatalogProduct):
            raise ContractError(f"{self.id}.mating_product must be a CatalogProduct")
        if self.nominal_designation is not None:
            _text(self.nominal_designation, f"{self.id}.nominal_designation")
        if self.actual_accepted_od_m is not None and not isinstance(self.actual_accepted_od_m, AcceptedODRange):
            raise ContractError(f"{self.id}.actual_accepted_od_m must be an AcceptedODRange")
        for name in ("dry_location", "wet_location", "raintight"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ContractError(f"{self.id}.{name} must be true, false, or unknown")
        if (not isinstance(self.listing_refs, tuple) or
                any(not isinstance(reference, str) or not reference.strip() for reference in self.listing_refs) or
                len(self.listing_refs) != len(set(self.listing_refs))):
            raise ContractError(f"{self.id}.listing_refs must be unique")
        for reference in self.listing_refs:
            _text(reference, f"{self.id}.listing_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class FittingSelectionOutcome(Entity):
    """One selected or unresolved product/interface decision for one intent."""

    intent_id: str
    fitting_id: str
    route_id: str
    status: str
    selected_selection_id: str | None = None
    requested_product: CatalogProduct | None = None
    requested_basis: RuleReference | None = None
    reason_codes: tuple[str, ...] = ()
    actual_od_check_performed: bool = False

    def __post_init__(self) -> None:
        super(FittingSelectionOutcome, self).__post_init__()
        _provenance_tuple(self.provenance, f"{self.id}.provenance")
        for name in ("intent_id", "fitting_id", "route_id"):
            _validate_id(getattr(self, name), f"{self.id}.{name}")
        if not isinstance(self.status, str) or self.status not in {"selected", "unresolved"}:
            raise ContractError(f"{self.id}.status must be selected or unresolved")
        if self.selected_selection_id is not None:
            _validate_id(self.selected_selection_id, f"{self.id}.selected_selection_id")
        if self.requested_product is not None and not isinstance(self.requested_product, CatalogProduct):
            raise ContractError(f"{self.id}.requested_product must be a CatalogProduct or null")
        if self.requested_basis is not None and not isinstance(self.requested_basis, RuleReference):
            raise ContractError(f"{self.id}.requested_basis must be a RuleReference or null")
        if (not isinstance(self.reason_codes, tuple) or
                any(not isinstance(reason, str) or reason not in FITTING_SELECTION_REASONS
                    for reason in self.reason_codes) or
                len(self.reason_codes) != len(set(self.reason_codes))):
            raise ContractError(f"{self.id}.reason_codes must be unique supported tokens")
        if self.actual_od_check_performed is not False:
            raise ContractError("contract 1.5 never performs an actual-OD check")
        if self.status == "selected":
            if (self.selected_selection_id is None or self.requested_product is None or
                    self.requested_basis is None or self.reason_codes):
                raise ContractError(f"{self.id} selected outcome requires a product/rule/selection and no reasons")
        elif self.selected_selection_id is not None or not self.reason_codes:
            raise ContractError(f"{self.id} unresolved outcome requires reasons and no selection")
