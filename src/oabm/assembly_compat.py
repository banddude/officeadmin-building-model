"""Shared fail-closed checks for consumers without building-assembly support.

The canonical model may carry the local 1.6 assembly collections before a
consumer has implemented their semantics. Such consumers must refuse a
populated model rather than silently dropping physical children or treating an
aggregate host as ordinary geometry. Empty collections remain compatible,
including on an explicitly upgraded 1.6 model.
"""

from __future__ import annotations

from typing import Type


def require_no_populated_building_assemblies(
    model: object,
    consumer: str,
    *,
    error_type: Type[Exception] = ValueError,
) -> None:
    """Refuse assembly-bearing models at unsupported consumer boundaries.

    The check is intentionally based on record presence rather than the model
    version: old and empty upgraded models keep their existing behavior, while
    either populated collection is enough to make an unsupported projection
    lossy or unsafe.
    """

    assemblies = getattr(model, "building_assemblies", ())
    components = getattr(model, "assembly_components", ())
    if assemblies or components:
        raise error_type(
            f"{consumer} does not support populated canonical building assemblies "
            "(building_assemblies/assembly_components, schema 1.6); refusing to "
            "produce a partial result"
        )
