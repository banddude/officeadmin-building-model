"""Bounded detached capture before canonical validation or serialization."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import math

from oabm import model as canonical
from .physical_verify_types import VerificationStop

SOURCE_BYTES = 32 * 1024 * 1024
CANDIDATE_BYTES = 16 * 1024 * 1024
COMBINED_BYTES = 48 * 1024 * 1024
REPORT_BYTES = 4 * 1024 * 1024
MAX_NODES = 250_000
MAX_DEPTH = 64
_CANONICAL_TYPES = frozenset(value for value in vars(canonical).values()
                             if isinstance(value, type) and is_dataclass(value))


def _invalid(reason):
    raise VerificationStop("invalid_input", reason)


class Capture:
    """Every serialized alias occurrence is charged, even immutable scalars."""
    def __init__(self, byte_limit: int, *, node_limit=MAX_NODES, depth_limit=MAX_DEPTH):
        self.byte_limit, self.node_limit, self.depth_limit = byte_limit, node_limit, depth_limit
        self.bytes = self.nodes = 0
        self._ancestors: set[int] = set()

    def _charge(self, size, *, node=True):
        if self.bytes + size > self.byte_limit:
            raise VerificationStop("resource_limit", "capture_byte_limit")
        if node and self.nodes == self.node_limit:
            raise VerificationStop("resource_limit", "capture_node_limit")
        self.bytes += size
        self.nodes += int(node)

    def copy(self, value, depth=0):
        if depth > self.depth_limit:
            raise VerificationStop("resource_limit", "capture_depth_limit")
        cls = type(value)
        if cls is str:
            # ensure_ascii JSON uses at most two six-byte surrogate escapes.
            self._charge(12*len(value)+2)
            return value
        if value is None or cls is bool:
            self._charge(5)
            return value
        if cls is int:
            if value.bit_length() > 4096:
                raise VerificationStop("resource_limit", "capture_integer_limit")
            self._charge(value.bit_length()//3+3)
            return value
        if cls is float:
            if not math.isfinite(value):
                _invalid("nonfinite_number")
            self._charge(32)
            return value
        if cls not in (dict, list, tuple) and cls not in _CANONICAL_TYPES:
            _invalid("unsupported_capture_type")
        self._charge(2)
        identity = id(value)
        if identity in self._ancestors:
            _invalid("cyclic_input")
        self._ancestors.add(identity)
        try:
            if cls is dict:
                copied = {}
                for key, child in value.items():
                    if type(key) is not str:
                        _invalid("nonstring_json_key")
                    self._charge(4, node=False)
                    copied[self.copy(key, depth+1)] = self.copy(child, depth+1)
                return copied
            if cls in (tuple, list):
                copied = []
                for child in value:
                    self._charge(2, node=False)
                    copied.append(self.copy(child, depth+1))
                return tuple(copied) if cls is tuple else copied
            copied = {}
            for descriptor in fields(cls):
                self._charge(12*len(descriptor.name)+4, node=False)
                copied[descriptor.name] = self.copy(getattr(value, descriptor.name), depth+1)
            result = cls(**{f.name: copied[f.name] for f in fields(cls) if f.init})
            for descriptor in fields(cls):
                if not descriptor.init and getattr(result, descriptor.name) != copied[descriptor.name]:
                    _invalid("fixed_canonical_field_mutated")
            return result
        except RuntimeError as exc:
            raise VerificationStop("invalid_input", "input_mutated_during_capture") from exc
        finally:
            self._ancestors.remove(identity)


def capture(value, *, byte_limit, node_limit=MAX_NODES, depth_limit=MAX_DEPTH):
    return Capture(byte_limit, node_limit=node_limit, depth_limit=depth_limit).copy(value)


def bounded_source_json(value):
    """Immutable text admission before normal JSON parsing, no whole UTF8 copy."""
    if value is None:
        return None
    if type(value) is bytes:
        if len(value) > SOURCE_BYTES:
            raise VerificationStop("resource_limit", "source_json_byte_limit")
        try:
            text = value.decode("utf-8")
        except UnicodeError as exc:
            raise VerificationStop("invalid_input", "source_json_encoding") from exc
    elif type(value) is str:
        if len(value) > SOURCE_BYTES:
            raise VerificationStop("resource_limit", "source_json_byte_limit")
        size = 0
        for offset in range(0, len(value), 4096):
            try:
                size += len(value[offset:offset+4096].encode("utf-8"))
            except UnicodeError as exc:
                raise VerificationStop("invalid_input", "source_json_encoding") from exc
            if size > SOURCE_BYTES:
                raise VerificationStop("resource_limit", "source_json_byte_limit")
        text = value
    else:
        _invalid("source_json_type")
    # Conservative token/node counter. Syntax is still checked by canonical JSON
    # admission; this scan bounds its allocations/depth before that parser runs.
    depth = nodes = token_length = 0
    quoted = escaped = scalar = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            nodes += 1
            quoted = True
            scalar = False
        elif char in "[{":
            depth += 1
            nodes += 1
            scalar = False
            if depth > MAX_DEPTH:
                raise VerificationStop("resource_limit", "source_json_depth_limit")
        elif char in "]}":
            depth -= 1
            scalar = False
            if depth < 0:
                _invalid("source_json_syntax")
        elif char in ",: \t\r\n":
            scalar = False
        else:
            if not scalar:
                nodes += 1
                token_length = 0
                scalar = True
            token_length += 1
            if token_length > 128:
                raise VerificationStop("resource_limit", "source_json_token_limit")
        if nodes > MAX_NODES:
            raise VerificationStop("resource_limit", "source_json_node_limit")
    if depth or quoted:
        _invalid("source_json_syntax")
    return text
