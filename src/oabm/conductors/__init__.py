"""Derivation of individual conductors from canonical circuits.

Every conductor this lane produces is an engineering inference, never an
observation of concealed as-built wiring.  See ``deriver`` for the rules and
the epistemic labelling they carry.
"""

from .deriver import (
    ALGORITHM,
    CLASS_INFERRED,
    CLASS_OBSERVED,
    CLASS_USER_DESIGNED,
    DERIVATION_CLASSES,
    LINE_TO_NEUTRAL_MAX_V,
    RACEWAY_GROUP_KEY,
    ROLE_EQUIPMENT_GROUND,
    ROLE_LINE,
    ROLE_NEUTRAL,
    ROLE_VOCABULARY,
    SOURCE_KIND,
    CircuitConductorOverride,
    ConductorDerivation,
    ConductorDerivationError,
    ConductorDerivationOptions,
    ConductorWarning,
    EgcPolicy,
    NeutralPolicy,
    PolesMissingPolicy,
    TableSizingPolicy,
    derive_conductors,
)

__all__ = [
    "ALGORITHM",
    "CLASS_INFERRED",
    "CLASS_OBSERVED",
    "CLASS_USER_DESIGNED",
    "DERIVATION_CLASSES",
    "LINE_TO_NEUTRAL_MAX_V",
    "RACEWAY_GROUP_KEY",
    "ROLE_EQUIPMENT_GROUND",
    "ROLE_LINE",
    "ROLE_NEUTRAL",
    "ROLE_VOCABULARY",
    "SOURCE_KIND",
    "CircuitConductorOverride",
    "ConductorDerivation",
    "ConductorDerivationError",
    "ConductorDerivationOptions",
    "ConductorWarning",
    "EgcPolicy",
    "NeutralPolicy",
    "PolesMissingPolicy",
    "TableSizingPolicy",
    "derive_conductors",
]
