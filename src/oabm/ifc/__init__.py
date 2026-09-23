"""IFC4 / Bonsai interoperability for the canonical OABM model."""

from .adapter import (
    ADAPTER_PSET,
    CANONICAL_PSET,
    IFC_SCHEMA,
    GEOMETRIC_TOLERANCE,
    IfcAdapterError,
    geometry_matches,
    canonical_id_to_ifc_guid,
    from_ifc,
    round_trip,
    to_ifc,
)

__all__ = [
    "ADAPTER_PSET",
    "CANONICAL_PSET",
    "IFC_SCHEMA",
    "GEOMETRIC_TOLERANCE",
    "IfcAdapterError",
    "geometry_matches",
    "canonical_id_to_ifc_guid",
    "from_ifc",
    "round_trip",
    "to_ifc",
]
