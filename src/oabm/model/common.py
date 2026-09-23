from __future__ import annotations

import json
import math
import re
import types
import uuid
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TypeVar, Union, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "1.0.0"
ID_NAMESPACE = uuid.UUID("7ec97126-df4d-5bf7-b1b8-6c1f0b1265de")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SCOPE_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_EPS = 1e-9


class ContractError(ValueError):
    """Raised when a document violates the canonical model contract."""


class UnsupportedSchemaVersion(ContractError):
    """Raised when this model implementation cannot read a schema version."""


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ContractError(f"{label} must be finite")
    return value


def _positive(value: float, label: str) -> float:
    value = _finite(value, label)
    if value <= 0:
        raise ContractError(f"{label} must be > 0")
    return value


def _nonnegative(value: float, label: str) -> float:
    value = _finite(value, label)
    if value < 0:
        raise ContractError(f"{label} must be >= 0")
    return value


def _validate_id(value: str, label: str = "id") -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractError(
            f"{label} must match {_ID_RE.pattern!r}; got {value!r}"
        )


def _validate_json_value(value: Any, path: str = "attributes") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _finite(value, path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{path} keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ContractError(f"{path} must contain only JSON-compatible values")


def stable_id(kind: str, source_key: str) -> str:
    """Return a deterministic canonical ID for a stable source key.

    Importers should prefer a stable native source identifier when one exists.
    This helper is for sources that need a deterministic opaque ID. Never use a
    list index, transient memory address, or mutable geometry as ``source_key``.
    """

    if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,31}", kind):
        raise ContractError("kind must be a short stable token")
    if not isinstance(source_key, str) or not source_key:
        raise ContractError("source_key must be a non-empty string")
    return f"{kind}:{uuid.uuid5(ID_NAMESPACE, f'{kind}:{source_key}').hex}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Point3:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        _finite(self.x, "Point3.x")
        _finite(self.y, "Point3.y")
        _finite(self.z, "Point3.z")


@dataclass(frozen=True, slots=True, kw_only=True)
class Vector3:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        _finite(self.x, "Vector3.x")
        _finite(self.y, "Vector3.y")
        _finite(self.z, "Vector3.z")

    @property
    def magnitude(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)


@dataclass(frozen=True, slots=True, kw_only=True)
class Size3:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        _positive(self.x, "Size3.x")
        _positive(self.y, "Size3.y")
        _positive(self.z, "Size3.z")


@dataclass(frozen=True, slots=True, kw_only=True)
class Quaternion:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    def __post_init__(self) -> None:
        values = tuple(_finite(v, "Quaternion component") for v in (self.x, self.y, self.z, self.w))
        norm = math.sqrt(sum(v * v for v in values))
        if abs(norm - 1.0) > 1e-5:
            raise ContractError("Quaternion must be normalized")


@dataclass(frozen=True, slots=True, kw_only=True)
class Pose:
    position: Point3
    rotation: Quaternion = field(default_factory=Quaternion)


@dataclass(frozen=True, slots=True, kw_only=True)
class Polyline3D:
    kind: str = field(default="polyline3d", init=False)
    points: tuple[Point3, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ContractError("Polyline3D requires at least two points")
        for a, b in zip(self.points, self.points[1:]):
            if _distance(a, b) <= _EPS:
                raise ContractError("Polyline3D cannot contain consecutive duplicate points")


@dataclass(frozen=True, slots=True, kw_only=True)
class Polygon3D:
    kind: str = field(default="polygon3d", init=False)
    points: tuple[Point3, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ContractError("Polygon3D requires at least three points")
        if _distance(self.points[0], self.points[-1]) <= _EPS:
            raise ContractError("Polygon3D closure is implicit; do not repeat the first point")


@dataclass(frozen=True, slots=True, kw_only=True)
class Box3D:
    kind: str = field(default="box3d", init=False)
    pose: Pose
    size: Size3


Geometry3D = Box3D | Polyline3D | Polygon3D


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordinateSystem:
    frame_id: str = "model"
    handedness: str = "right"
    up_axis: str = "+Z"
    length_unit: str = "m"
    angle_unit: str = "rad"
    crs: str | None = None
    origin_in_crs: Point3 | None = None
    true_north_radians: float | None = None

    def __post_init__(self) -> None:
        _validate_id(self.frame_id, "frame_id")
        if self.handedness != "right":
            raise ContractError("v1 requires a right-handed coordinate system")
        if self.up_axis != "+Z":
            raise ContractError("v1 requires +Z up")
        if self.length_unit != "m":
            raise ContractError("v1 canonical length unit is metres (m)")
        if self.angle_unit != "rad":
            raise ContractError("v1 canonical angular unit is radians (rad)")
        if self.true_north_radians is not None:
            _finite(self.true_north_radians, "true_north_radians")


#: How an element came to exist, as distinct from where its evidence came from.
#:
#: ``source_kind`` says which input produced a record. It cannot say whether we
#: *saw* the thing or *synthesized* it: a port invented so a circuit has an
#: endpoint still carries the importer's own ``source_kind``. A consumer reading
#: only ``source_kind`` would grade that synthesized port as observed, which is
#: exactly the claim a 3D visual must never make.
DERIVATION_OBSERVED = "observed"
DERIVATION_USER = "user"
DERIVATION_INFERRED = "inferred"
DERIVATION_CLASSES: frozenset[str] = frozenset(
    {DERIVATION_OBSERVED, DERIVATION_USER, DERIVATION_INFERRED}
)


def is_observed(provenance: Sequence["Provenance"]) -> bool:
    """True only when every record states it was observed from a source.

    Fails closed: an unset ``derivation`` is not a claim of observation, and a
    mixed set is not either. A renderer must not present anything this returns
    ``False`` for as if it were seen in the source.

    ``derivation`` is the single authoritative location for this fact.  Do not
    determine it by reading an entity's ``attributes``.  Lane attributes such
    as ``inferred_for_circuit_semantics`` are diagnostics, not the source of
    truth: ``attributes`` is free-form, so where a fact lives has no single
    answer, and a consumer that guesses the wrong nesting level finds nothing.
    Absent reads as "not inferred", which renders as observed -- the failure is
    silent and biased toward the dangerous direction.  Two lanes shipped that
    exact bug before this field existed.
    """
    return bool(provenance) and all(
        record.derivation == DERIVATION_OBSERVED for record in provenance
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    source_kind: str
    source_id: str
    source_element_id: str | None = None
    page: int | None = None
    method: str | None = None
    confidence: float = 1.0
    derivation: str | None = None
    scope_paths: tuple[str, ...] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_kind:
            raise ContractError("Provenance.source_kind is required")
        if not self.source_id:
            raise ContractError("Provenance.source_id is required")
        if self.derivation is not None and self.derivation not in DERIVATION_CLASSES:
            raise ContractError(
                "Provenance.derivation must be one of "
                f"{sorted(DERIVATION_CLASSES)!r}, got {self.derivation!r}"
            )
        if self.scope_paths is not None:
            if (
                not isinstance(self.scope_paths, tuple)
                or not self.scope_paths
                or any(
                    not isinstance(path, str)
                    or _SCOPE_PATH_RE.fullmatch(path) is None
                    for path in self.scope_paths
                )
                or len(set(self.scope_paths)) != len(self.scope_paths)
            ):
                raise ContractError(
                    "Provenance.scope_paths must be a nonempty tuple of unique field paths"
                )
        if self.page is not None:
            if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
                raise ContractError("Provenance.page is a 1-based integer")
        confidence = _finite(self.confidence, "Provenance.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ContractError("Provenance.confidence must be between 0 and 1")
        _validate_json_value(self.attributes)


def provenance_applies_to(record: Provenance, consumed_paths: Iterable[str]) -> bool:
    """Whether a record's canonical scope covers a consumed claim path.

    An absent scope covers the whole owner. A stated scope covers its exact
    field or a parent/child path. Empty and invalid scopes are rejected when
    the record/model is constructed, never interpreted as permission to drop
    an inferred record.
    """
    if record.scope_paths is None:
        return True
    return any(
        scope == consumed
        or scope.startswith(consumed + ".")
        or consumed.startswith(scope + ".")
        for scope in record.scope_paths
        for consumed in consumed_paths
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Entity:
    id: str
    name: str | None = None
    confidence: float = 1.0
    provenance: tuple[Provenance, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id(self.id)
        confidence = _finite(self.confidence, f"{self.id}.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ContractError(f"{self.id}.confidence must be between 0 and 1")
        _validate_json_value(self.attributes)


def _distance(a: Point3, b: Point3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)
