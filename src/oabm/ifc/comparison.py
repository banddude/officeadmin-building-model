"""Explicit numerical equivalence for IFC interchange, never serialization.

IFC placements are decomposed/recomposed through matrices. Only fields actually
read back from native IFC receive tolerance; metadata and topology stay exact.
"""
from __future__ import annotations

import math
from typing import Any

from oabm.model import BuildingModel

IFC_LINEAR_TOLERANCE_M = 1e-9
IFC_QUATERNION_TOLERANCE = 1e-12
_POSE_COLLECTIONS = frozenset({
    "electrical_equipment", "electrical_devices", "openings", "ports", "route_fittings",
})


def round_trip_differences(before: BuildingModel, after: BuildingModel) -> tuple[str, ...]:
    """Return deterministic JSON-pointer paths that exceed IFC equivalence.

    No relative tolerance, reordering, rounding, mutation, or shadow restoration
    is performed. A quaternion and its whole-vector negation encode the same
    orientation. Arbitrary attributes/provenance never receive numeric slack.
    """
    differences: list[str] = []

    def pointer(path: tuple[str | int, ...]) -> str:
        return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)

    def near(a: Any, b: Any, tolerance: float) -> bool:
        return (type(a) in (int, float) and type(b) in (int, float)
                and math.isfinite(a) and math.isfinite(b)
                and math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance))

    def linear(path: tuple[str | int, ...]) -> bool:
        if len(path) < 3 or not isinstance(path[1], int):
            return False
        collection, _, *field = path
        return (
            (collection == "levels" and field == ["elevation_m"])
            or (collection in _POSE_COLLECTIONS and len(field) == 3
                and field[:2] == ["pose", "position"] and field[2] in ("x", "y", "z"))
            or (collection in ("walls", "routes") and len(field) == 4
                and field[:2] == ["centerline", "points"]
                and isinstance(field[2], int) and field[3] in ("x", "y", "z"))
        )

    def visit(a: Any, b: Any, path: tuple[str | int, ...]) -> None:
        rotation = (len(path) == 4 and path[0] in _POSE_COLLECTIONS
                    and isinstance(path[1], int) and path[2:] == ("pose", "rotation"))
        if rotation and isinstance(a, dict) and isinstance(b, dict):
            keys = ("x", "y", "z", "w")
            if set(a) == set(b) == set(keys) and any(
                all(near(a[k], sign * b[k], IFC_QUATERNION_TOLERANCE) for k in keys)
                for sign in (1, -1)
            ):
                return
            differences.append(pointer(path))
            return
        if linear(path) and near(a, b, IFC_LINEAR_TOLERANCE_M):
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                child = (*path, key)
                if key not in a or key not in b:
                    differences.append(pointer(child))
                else:
                    visit(a[key], b[key], child)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                differences.append(pointer(path))
            else:
                for index, (left, right) in enumerate(zip(a, b)):
                    visit(left, right, (*path, index))
        elif type(a) is bool or type(b) is bool:
            if type(a) is not type(b) or a != b:
                differences.append(pointer(path))
        elif a != b:
            differences.append(pointer(path))

    visit(before.to_dict(), after.to_dict(), ())
    return tuple(differences)
