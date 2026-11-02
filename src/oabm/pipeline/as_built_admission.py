"""Finite admission budgets before as-built parsing, capture or replay."""
from __future__ import annotations

import json
import os
import stat

from oabm.model import ContractError
from oabm.routing.physical_capture import bounded_source_json, capture
from oabm.routing.physical_verify_types import VerificationStop

MODEL_BYTES = 4 * 1024 * 1024
REQUEST_BYTES = 512 * 1024
HISTORY_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 48
MAX_NODES = 100_000
MAX_TRANSACTIONS = 32
MAX_EDITS = 256
MAX_EVIDENCE = 64
MAX_ASSERTIONS = 1024
MAX_HISTORY_EDITS = 2048
MAX_HISTORY_EVIDENCE = 512
MAX_HISTORY_ASSERTIONS = 4096


def detached(value, *, byte_limit):
    """Existing canonical capture charges aliases and rejects cycles first."""
    try:
        return capture(value, byte_limit=byte_limit, node_limit=MAX_NODES, depth_limit=MAX_DEPTH)
    except VerificationStop as exc:
        raise ContractError(f"as-built admission {exc.status}: {exc.reason}") from exc
    except (RecursionError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        raise ContractError("as-built admission: invalid or changing input") from exc


def json_text(raw, *, byte_limit):
    """Bound immutable text before parsing; scan JSON depth/tokens/nodes too."""
    if type(raw) not in (bytes, str) or len(raw) > byte_limit:
        raise ContractError("as-built admission: JSON byte limit/type")
    if type(raw) is str:
        size = 0
        for offset in range(0, len(raw), 4096):
            try:
                size += len(raw[offset:offset + 4096].encode("utf-8"))
            except UnicodeError as exc:
                raise ContractError("as-built admission: invalid UTF-8") from exc
            if size > byte_limit:
                raise ContractError("as-built admission: JSON byte limit")
    try:
        return bounded_source_json(raw)
    except (VerificationStop, RecursionError, ValueError) as exc:
        raise ContractError("as-built admission: invalid or over-budget JSON") from exc


def decode(raw, *, byte_limit):
    text = json_text(raw, byte_limit=byte_limit)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("as-built admission: duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ContractError("as-built admission: nonfinite JSON")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, ValueError, TypeError, UnicodeError) as exc:
        raise ContractError("as-built admission: invalid JSON") from exc
    return detached(value, byte_limit=byte_limit)


def read_bytes(path, *, byte_limit):
    """Never use an unbounded read, including on a growing file or special file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            facts = os.fstat(descriptor)
            if not stat.S_ISREG(facts.st_mode) or facts.st_size > byte_limit:
                raise ContractError("as-built admission: regular bounded input file required")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(byte_limit + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContractError("as-built admission: unreadable input file") from exc
    if len(raw) > byte_limit:
        raise ContractError("as-built admission: file byte limit")
    return raw


def _list_limit(document, key, maximum):
    value = document.get(key)
    if type(value) is not list or len(value) > maximum:
        raise ContractError(f"as-built admission: {key} list limit/type")
    return len(value)


def request_limits(request):
    if type(request) is not dict:
        raise ContractError("as-built admission: transaction object required")
    identity = request.get("transaction_id")
    if type(identity) is not str or not identity.strip():
        raise ContractError("as-built admission: transaction_id must be a nonempty string")
    return (_list_limit(request, "edits", MAX_EDITS),
            _list_limit(request, "evidence", MAX_EVIDENCE),
            _list_limit(request, "installed_assertions", MAX_ASSERTIONS),
            _list_limit(request, "method_reconciliations", MAX_EDITS))


def admit_request(request):
    request = detached(request, byte_limit=REQUEST_BYTES)
    request_limits(request)
    return request


def admit_history(history):
    history = detached(history, byte_limit=HISTORY_BYTES)
    if type(history) is not dict:
        raise ContractError("as-built admission: history object required")
    _list_limit(history, "transactions", MAX_TRANSACTIONS)
    json_text(history.get("design_json"), byte_limit=MODEL_BYTES)
    edits = evidence = assertions = 0
    for retained in history["transactions"]:
        if type(retained) is not dict:
            raise ContractError("as-built admission: retained transaction object required")
        counts = request_limits(retained.get("request"))
        # Per-request budgets apply to old transactions too, before any replay.
        detached(retained["request"], byte_limit=REQUEST_BYTES)
        edits += counts[0]
        evidence += counts[1]
        assertions += counts[2]
        if edits > MAX_HISTORY_EDITS or evidence > MAX_HISTORY_EVIDENCE or assertions > MAX_HISTORY_ASSERTIONS:
            raise ContractError("as-built admission: cumulative history work limit")
        json_text(retained.get("result_json"), byte_limit=MODEL_BYTES)
    return history
