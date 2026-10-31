"""Bounded, exact, derived verifier values. Not a second canonical model."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from typing import Callable, TypeVar

Point = tuple[Fraction, Fraction, Fraction]
Box = tuple[tuple[Fraction, Fraction], tuple[Fraction, Fraction], tuple[Fraction, Fraction]]
ExactFacts = tuple[tuple[str, Fraction], ...]
PROFILE = "physical-route-verifier-v1"
MAX_DIAGNOSTICS = 256


class VerificationStop(Exception):
    def __init__(self, status: str, reason: str, ids: tuple[str, ...] = (),
                 exact: ExactFacts = ()):
        super().__init__(reason)
        self.status, self.reason, self.ids, self.exact = status, reason, ids, exact


class WorkLimit(VerificationStop):
    def __init__(self, kind: str):
        super().__init__("resource_limit", "proof_work_limit", (kind,))


@dataclass(frozen=True)
class Primitive:
    segment_id: str
    kind: str
    start: Point
    end: Point
    start_tangent: tuple[int, int, int]
    end_tangent: tuple[int, int, int]
    tube_radius: Fraction
    center: Point
    axis: int | None = None
    axial: tuple[Fraction, Fraction] | None = None
    plane_axes: tuple[int, int] | None = None
    signs: tuple[int, int] | None = None
    major_radius: Fraction | None = None

    @property
    def bounds(self) -> Box:
        r = self.tube_radius
        if self.kind == "line":
            return tuple(self.axial if i == self.axis else
                         (self.center[i] - r, self.center[i] + r) for i in range(3))
        result = [(c-r, c+r) for c in self.center]
        for axis, sign in zip(self.plane_axes, self.signs):
            c = self.center[axis]
            # R > r. Entire quarter sweeps from its terminal normal disc at
            # local coordinate zero to the attained outward R+r support.
            result[axis] = (c, c+self.major_radius+r) if sign == 1 else (c-self.major_radius-r, c)
        return tuple(result)


def exact_json(value: Fraction) -> dict[str, str]:
    value = Fraction(value)
    # Check bit length before decimal conversion; 3400 bits < 1024 digits.
    if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > 3400:
        raise VerificationStop("resource_limit", "rational_encoding_limit")
    numerator, denominator = str(value.numerator), str(value.denominator)
    if len(numerator.lstrip("-")) > 1024 or len(denominator) > 1024:
        raise VerificationStop("resource_limit", "rational_encoding_limit")
    return {"numerator": numerator, "denominator": denominator}


@dataclass(frozen=True)
class Diagnostic:
    kind: str
    ids: tuple[str, ...]
    exact: ExactFacts

    def to_dict(self):
        return {"kind": self.kind, "ids": list(self.ids),
                "exact": {name: exact_json(value) for name, value in self.exact}}


class WorkLedger:
    """Charged/reserved units control limits. Actual sort comparisons do not."""
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.actual_sort_comparisons = 0
        self._counts: dict[str, int] = {}
        self._trace = hashlib.sha256()
        self._diagnostics: list[Diagnostic] = []
        self.records = 0
        self._diagnostic_bytes = 0
        self.cursor = Diagnostic("proof_start", (), ())

    def set_cursor(self, stage: str, ids: tuple[str, ...] = (), *, exact: ExactFacts = ()) -> None:
        # Fixed bounded operation bookkeeping, not a new geometric check.
        if len(ids) > 16 or len(exact) > 16:
            raise VerificationStop("resource_limit", "cursor_size_limit")
        self.cursor = Diagnostic(stage, tuple(ids), tuple(exact))

    def debit(self, kind: str, units: int = 1) -> None:
        if type(units) is not int or units < 0:
            raise ValueError("invalid work charge")
        if units > self.limit - self.used:
            raise WorkLimit(kind)
        self.used += units
        self._counts[kind] = self._counts.get(kind, 0) + units

    def record(self, kind: str, ids: tuple[str, ...], *, exact: ExactFacts = ()) -> None:
        self.debit("trace_record")
        if len(exact) > 16:
            raise VerificationStop("resource_limit", "diagnostic_fact_limit")
        record = Diagnostic(kind, tuple(ids), tuple(exact))
        encoded = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("ascii")
        self._trace.update(len(encoded).to_bytes(8, "big"))
        self._trace.update(encoded)
        self.records += 1
        if (len(self._diagnostics) < MAX_DIAGNOSTICS
                and self._diagnostic_bytes + len(encoded) <= 2*1024*1024):
            self._diagnostics.append(record)
            self._diagnostic_bytes += len(encoded)

    @property
    def counters(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._counts.items()))

    @property
    def trace_sha256(self) -> str:
        return self._trace.hexdigest()

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(self._diagnostics)


T = TypeVar("T")


def reserved_sort(values: tuple[T, ...] | list[T], ledger: WorkLedger,
                  key: Callable[[T], object] = lambda x: x) -> list[T]:
    n = len(values)
    bound = n * (n - 1).bit_length() if n > 1 else 0
    ledger.debit("sort_reserved_comparisons", bound)
    ledger.debit("sort_items", n)
    keyed = [(key(value), value) for value in values]
    actual = 0
    width = 1
    while width < n:
        merged = []
        for start in range(0, n, 2*width):
            left, right = start, min(start+width, n)
            left_end, right_end = right, min(start+2*width, n)
            while left < left_end and right < right_end:
                actual += 1
                if keyed[left][0] <= keyed[right][0]:
                    merged.append(keyed[left]); left += 1
                else:
                    merged.append(keyed[right]); right += 1
            merged.extend(keyed[left:left_end])
            merged.extend(keyed[right:right_end])
        keyed = merged
        width *= 2
    assert actual <= bound
    ledger.actual_sort_comparisons += actual
    return [value for _, value in keyed]
