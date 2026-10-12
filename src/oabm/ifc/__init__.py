"""IFC4 / Bonsai interoperability for the canonical OABM model."""

from .comparison import IFC_LINEAR_TOLERANCE_M, IFC_QUATERNION_TOLERANCE, round_trip_differences

from .adapter import (
    ADAPTER_PSET,
    CANONICAL_PSET,
    IFC_SCHEMA,
    IfcAdapterError,
    canonical_id_to_ifc_guid,
    from_ifc,
    round_trip,
    to_ifc,
)

__all__ = [
    "IFC_LINEAR_TOLERANCE_M",
    "IFC_QUATERNION_TOLERANCE",
    "round_trip_differences",
    "ADAPTER_PSET",
    "CANONICAL_PSET",
    "IFC_SCHEMA",
    "IfcAdapterError",
    "canonical_id_to_ifc_guid",
    "from_ifc",
    "round_trip",
    "to_ifc",
]

# Explicit generic native intake is separate from the strict metadata reader.
from oabm.importers.ifc_native import (NativeIfcImportError, NativeIfcDiagnostic,
    NativeIfcImportResult, import_native_ifc)
__all__ += ['NativeIfcImportError', 'NativeIfcDiagnostic', 'NativeIfcImportResult', 'import_native_ifc']
