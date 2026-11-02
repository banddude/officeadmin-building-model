"""Explicit, evidence-bound field edits over immutable canonical snapshots.

This is a quick-edit transaction boundary, not an importer or a new model.
All replacement records use the existing canonical contract. Nothing is inferred
to be installed merely because it appeared in the design.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from oabm.model import BuildingModel, ContractError
from .as_built_admission import (MODEL_BYTES, MAX_TRANSACTIONS, admit_history,
                                admit_request, detached, json_text)

VERSION = "oabm-as-built-v1"
COLLECTIONS = frozenset({
    "electrical_devices", "ports", "routes", "route_fittings", "conductors",
    "electrical_boxes", "box_selection_outcomes", "raceway_selections",
    "raceway_selection_outcomes", "fitting_selections", "fitting_interfaces",
    "fitting_selection_outcomes",
})


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot(model):
    """Detach even mutable attributes before retaining a snapshot."""
    _require(type(model) is BuildingModel, "snapshot requires canonical BuildingModel")
    model = detached(model, byte_limit=MODEL_BYTES)
    try:
        text = json_text(model.to_json(indent=None), byte_limit=MODEL_BYTES)
        return BuildingModel.from_json(text).to_json(indent=None)
    except (RecursionError, TypeError, ValueError, AttributeError) as exc:
        raise ContractError("as-built admission: invalid canonical snapshot") from exc


def _object(value, keys, label):
    _require(type(value) is dict and set(value) == set(keys),
             f"{label}: exact fields required: {sorted(keys)}")


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label} required")


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value),
             "expected a lowercase SHA256")


def _index(document, collection):
    return {entity["id"]: entity for entity in document.get(collection, [])}


def start_history(design: BuildingModel):
    baseline = snapshot(design)
    return {"version": VERSION, "design_json": baseline,
            "design_sha256": _sha(baseline), "transactions": []}


def record_sha256(record):
    """Fingerprint a detached canonical entity record for optimistic editing."""
    return _sha(_json(detached(record, byte_limit=MODEL_BYTES)))


def preview_transaction(history, request):
    """Preview the exact admitted result before adding installation assertions.

    Call with installed_assertions=[]; then bind explicit field confirmations
    to the returned entity fingerprints. This does not persist anything.
    """
    return replay_history(apply_transaction(history, request))


def replay_history(history):
    """Validate all retained parents, outcomes and evidence by deterministic replay."""
    history = admit_history(history)
    _object(history, ("version", "design_json", "design_sha256", "transactions"), "history")
    _require(history["version"] == VERSION, "unsupported as-built history")
    _require(type(history["transactions"]) is list, "transactions must be a list")
    design = BuildingModel.from_json(history["design_json"])
    _require(snapshot(design) == history["design_json"] and
             _sha(history["design_json"]) == history["design_sha256"], "design snapshot mismatch")
    current = design
    seen = set()
    for retained in history["transactions"]:
        _object(retained, ("request", "result_json", "result_sha256"), "retained transaction")
        request = retained["request"]
        _require(request.get("transaction_id") not in seen, "duplicate transaction ID")
        current = _apply(current, history["design_sha256"], request)
        result = snapshot(current)
        _require(result == retained["result_json"] and _sha(result) == retained["result_sha256"],
                 "transaction replay/result mismatch")
        seen.add(request["transaction_id"])
    return current


def apply_transaction(history, request):
    """Return a detached history; failure mutates neither caller input nor baseline."""
    history, request = admit_history(history), admit_request(request)
    _require(len(history["transactions"]) < MAX_TRANSACTIONS, "as-built admission: transaction history is full")
    current = replay_history(history)
    _require(request.get("transaction_id") not in {
        item["request"]["transaction_id"] for item in history["transactions"]}, "duplicate transaction ID")
    revised = _apply(current, history["design_sha256"], deepcopy(request))
    result = snapshot(revised)
    output = deepcopy(history)
    output["transactions"].append({"request": deepcopy(request), "result_json": result,
                                   "result_sha256": _sha(result)})
    return admit_history(output)


def _apply(parent, design_sha, request):
    try:
        return _apply_admitted(parent, design_sha, request)
    except ContractError:
        raise
    except (RecursionError, KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ContractError("as-built transaction: malformed canonical edit or evidence") from exc


def _apply_admitted(parent, design_sha, request):
    _object(request, ("version", "transaction_id", "actor", "reason", "design_sha256",
                     "parent_sha256", "evidence", "edits", "method_reconciliations",
                     "installed_assertions"), "transaction")
    _require(request["version"] == VERSION, "unsupported transaction version")
    for key in ("transaction_id", "actor", "reason"):
        _text(request[key], key)
    for key in ("design_sha256", "parent_sha256"):
        _hash(request[key])
    _require(request["design_sha256"] == design_sha, "wrong immutable design baseline")
    _require(request["parent_sha256"] == _sha(snapshot(parent)), "stale parent snapshot")
    evidence = request["evidence"]
    _require(type(evidence) is list and bool(evidence), "supplied field evidence required")
    evidence_ids = set()
    for item in evidence:
        _object(item, ("id", "kind", "reference", "sha256", "statement"), "field evidence")
        for key in ("id", "reference", "statement"):
            _text(item[key], f"evidence.{key}")
        _hash(item["sha256"])
        _require(item["kind"] in {"quick_edit", "photo", "scan", "voice"}, "unsupported evidence kind")
        if item["kind"] == "quick_edit":
            _require(_sha(item["statement"]) == item["sha256"], "quick-edit statement digest mismatch")
        _require(item["id"] not in evidence_ids, "duplicate evidence ID")
        evidence_ids.add(item["id"])
    edits = request["edits"]
    _require(type(edits) is list and bool(edits), "nonempty edit list required")
    before = parent.to_dict()
    after = deepcopy(before)
    touched = set()
    changed_fields = {}
    for edit in edits:
        _object(edit, ("collection", "id", "expected_sha256", "record", "evidence_ids"), "edit")
        collection, identity = edit["collection"], edit["id"]
        _require(collection in COLLECTIONS, "unsupported field-edit collection")
        _text(identity, "edit.id")
        key = (collection, identity)
        _require(key not in touched, "duplicate entity edit")
        touched.add(key)
        refs = edit["evidence_ids"]
        _require(type(refs) is list and bool(refs) and len(set(refs)) == len(refs)
                 and set(refs) <= evidence_ids, "edit requires known unique field evidence")
        old = _index(before, collection).get(identity)
        _require(edit["expected_sha256"] == (None if old is None else _sha(_json(old))),
                 "stale entity or unexpected new ID")
        new = deepcopy(edit["record"])
        _require(new is not None or old is not None, "cannot delete absent entity")
        if new is not None:
            _require(type(new) is dict and new.get("id") == identity, "replacement must preserve stable ID")
            _require(new.get("provenance", []) == ([] if old is None else old.get("provenance", [])),
                     "retain existing provenance; field provenance is transaction-owned")
            _require(new.get("attributes", {}) == ({} if old is None else old.get("attributes", {})),
                     "quick edits cannot introduce arbitrary product semantics in attributes")
            scopes = sorted(k for k in new if k not in {"id", "provenance"}
                            and (old is None or old.get(k) != new[k]))
            changed_fields[key] = tuple(scopes)  # actual changes, never reaffirmation scopes
            removed = set(old or {}) - set(new)
            _require(not removed, "replace omitted optional values with canonical null rather than deleting fields")
            if not scopes:
                # Explicit unchanged conductor/fitting testimony is a valid
                # reconciliation, and must not require changing a label.
                scopes = sorted(set(new) - {"id", "provenance", "attributes", "confidence"})
            _require(bool(scopes), "replacement requires canonical field claims")
            new.setdefault("provenance", []).append({
                "source_kind": "as-built-field-edit", "source_id": request["transaction_id"],
                "method": "supplied-quick-edit", "derivation": "user", "confidence": 1.0,
                "scope_paths": scopes, "attributes": {"actor": request["actor"], "reason": request["reason"],
                    "evidence": [item for item in evidence if item["id"] in refs]}})
        rows = after.setdefault(collection, [])
        if old is None:
            rows.append(new)
        else:
            pos = next(i for i, row in enumerate(rows) if row["id"] == identity)
            if new is None:
                rows.pop(pos)
            else:
                rows[pos] = new
    _check_device_moves(before, after, touched, changed_fields)
    _check_affected_supports(before, after, touched)
    for identity, old in _index(before, "routes").items():
        new = _index(after, "routes").get(identity)
        if new is not None and old.get("centerline") != new.get("centerline"):
            for fitting in before.get("route_fittings", []):
                if fitting["route_id"] == identity:
                    _require(("route_fittings", fitting["id"]) in touched,
                             "rerouting requires explicit existing fitting reconciliation")
    _check_method_changes(before, after, touched, request["method_reconciliations"])
    # One admission after every dependent edit: never publish an intermediate
    # device pose, dangling route, stale fitting, or mismatched selected product.
    revised = BuildingModel.from_dict(detached(after, byte_limit=MODEL_BYTES))
    _check_installed_assertions(revised, request["installed_assertions"], evidence_ids)
    return revised


def _check_affected_supports(before, after, touched):
    """Refuse stale support testimony until atomic support editing exists.

    Canonical reference validity does not prove spatial contact. Keep support
    producers unchanged and refuse rather than guessing new hardware or letting
    unchanged support hashes preserve earlier installation confirmations.
    """
    affected = set()
    for collection, identity in touched:
        if collection not in {"electrical_devices", "ports", "routes", "route_fittings", "electrical_boxes",
                              "raceway_selections", "fitting_selections", "fitting_interfaces", "conductors"}:
            continue
        old = _index(before, collection).get(identity)
        new = _index(after, collection).get(identity)
        def facts(record):
            return None if record is None else {key: value for key, value in record.items()
                if key not in {"provenance", "confidence", "name", "attributes"}}
        if facts(old) == facts(new):
            continue
        affected.add(identity)
        for record in (old, new):
            if record is not None:
                if record.get("route_id") is not None:
                    affected.add(record["route_id"])
                affected.update(record.get("route_ids", []))
                if collection == "fitting_interfaces":
                    for document in (before, after):
                        selection = _index(document, "fitting_selections").get(record["selection_id"])
                        if selection is not None:
                            affected.add(selection["route_id"])
        if collection == "ports":
            for port in (old, new):
                if port is not None:
                    affected.add(port["owner_id"])
            for route in before.get("routes", []):
                if identity in {route["start_port_id"], route["end_port_id"]}:
                    affected.add(route["id"])
        if collection == "electrical_devices":
            affected.update(box["id"] for box in before.get("electrical_boxes", [])
                            if identity in box["occupant_ids"])
    for collection in ("support_assemblies", "support_selection_outcomes"):
        for row in before.get(collection, []):
            if affected.intersection(row["supported_object_ids"]):
                raise ContractError("as-built support reconciliation unavailable: affected supported object; "
                                    "refusing to retain stale hardware or installed assertions")


def _check_installed_assertions(model, assertions, evidence_ids):
    """Confirm exact current records, including unchanged installed design facts.

    This receipt is field testimony, not machine authentication of the source.
    It stays outside canonical product semantics and never overwrites them.
    """
    _require(type(assertions) is list, "installed_assertions must be a list")
    seen = set()
    document = model.to_dict()
    for item in assertions:
        _object(item, ("collection", "id", "record_sha256", "evidence_ids"), "installed assertion")
        collection, identity = item["collection"], item["id"]
        _require(collection in COLLECTIONS | {"electrical_equipment", "support_components",
                 "support_assemblies", "structural_members", "assembly_components"},
                 "unsupported installed assertion collection")
        _require((collection, identity) not in seen, "duplicate installed assertion")
        seen.add((collection, identity))
        record = _index(document, collection).get(identity)
        _require(record is not None and _sha(_json(record)) == item["record_sha256"],
                 "installed assertion must bind the exact revised canonical record")
        refs = item["evidence_ids"]
        _require(type(refs) is list and bool(refs) and len(set(refs)) == len(refs)
                 and set(refs) <= evidence_ids, "installed assertion requires supplied evidence")


def _check_device_moves(before, after, touched, changed_fields):
    from oabm.assembly_geometry import rotate_vector_xyzw
    def vector(record):
        return tuple(record[axis] for axis in ("x", "y", "z"))
    def quaternion(record):
        return tuple(record[axis] for axis in ("x", "y", "z", "w"))
    def multiply(a, b):
        x, y, z, w = a
        i, j, k, r = b
        return (w*i + x*r + y*k - z*j, w*j - x*k + y*r + z*i,
                w*k + x*j - y*i + z*r, w*r - x*i - y*j - z*k)
    def close(a, b, tolerance=1e-9):
        return all(abs(x-y) <= tolerance for x, y in zip(a, b))
    old_devices,new_devices=_index(before,"electrical_devices"),_index(after,"electrical_devices")
    old_ports,new_ports=_index(before,"ports"),_index(after,"ports")
    # Check existing device-owned ports even when their owner is unchanged.
    # Canonical endpoint validity alone permits a port and its route to move
    # together while leaving the device behind; that is not a rigid owner move.
    for port_id,old_port in old_ports.items():
        owner_id=old_port["owner_id"]
        if owner_id not in old_devices:
            continue
        new_port=new_ports.get(port_id);new_owner=new_devices.get(owner_id)
        if new_port is None and new_owner is None:
            continue  # whole owner/connection deletion still needs canonical admission
        _require(new_port is not None and new_port["owner_id"]==owner_id,
                 "existing device-owned ports cannot be independently removed or reassigned")
        if new_owner is not None and old_devices[owner_id]["pose"]==new_owner["pose"]:
            _require(old_port["pose"]==new_port["pose"] and old_port["direction"]==new_port["direction"],
                     "independent device-owned port pose/direction edits require a supported rigid device move")
    for identity, device in _index(before, "electrical_devices").items():
        new = _index(after, "electrical_devices").get(identity)
        if new is None or device["pose"] == new["pose"]:
            continue
        _require("pose" in changed_fields.get(("electrical_devices", identity), ()),
                 "device move requires an actual pose change, not reaffirmation testimony")
        owned = {p["id"] for p in before.get("ports", []) if p["owner_id"] == identity}
        old_rotation = quaternion(device["pose"]["rotation"])
        delta = multiply(quaternion(new["pose"]["rotation"]),
                         (-old_rotation[0], -old_rotation[1], -old_rotation[2], old_rotation[3]))
        for port_id in owned:
            _require(("ports", port_id) in touched, "moved device requires explicit reconciliation for every owned port")
            old_port = _index(before, "ports")[port_id]
            new_port = _index(after, "ports").get(port_id)
            _require(new_port is not None and new_port["owner_id"] == identity,
                     "rigid device moves must retain owned port identities")
            offset = tuple(a-b for a, b in zip(vector(old_port["pose"]["position"]), vector(device["pose"]["position"])))
            expected = tuple(a+b for a, b in zip(vector(new["pose"]["position"]), rotate_vector_xyzw(offset, delta)))
            expected_rotation = multiply(delta, quaternion(old_port["pose"]["rotation"]))
            actual_rotation = quaternion(new_port["pose"]["rotation"])
            _require(close(expected, vector(new_port["pose"]["position"])) and
                     (close(expected_rotation, actual_rotation, 1e-12) or
                      close(tuple(-value for value in expected_rotation), actual_rotation, 1e-12)) and
                     close(rotate_vector_xyzw(vector(old_port["direction"]), delta), vector(new_port["direction"])),
                     "port pose/direction contradict rigid device movement; unchanged world ports are valid only at invariant connection poses")
        for route in before.get("routes", []):
            if {route["start_port_id"], route["end_port_id"]} & owned:
                _require(("routes", route["id"]) in touched,
                         "moved device requires explicit connected route reconciliation")
        for box in before.get("electrical_boxes", []):
            if identity in box["occupant_ids"]:
                _require(("electrical_boxes", box["id"]) in touched,
                         "moved boxed device requires explicit box reconciliation")


def _check_method_changes(before, after, touched, declarations):
    _require(type(declarations) is list, "method reconciliations must be a list")
    changed = {identity for identity, old in _index(before, "routes").items()
               if identity in _index(after, "routes") and
               old["route_type"] != _index(after, "routes")[identity]["route_type"]}
    declared = set()
    for declaration in declarations:
        _object(declaration, ("route_id", "conductors", "route_fittings", "raceway_selections",
                             "raceway_selection_outcomes", "fitting_selections", "fitting_interfaces",
                             "fitting_selection_outcomes"), "method reconciliation")
        route_id = declaration["route_id"]
        _require(route_id in changed and route_id not in declared, "unexpected/duplicate method reconciliation")
        declared.add(route_id)
        route = _index(after, "routes")[route_id]
        _require("segments" not in route, "physical-route product changes need a future canonical product contract")
        for collection in set(declaration) - {"route_id"}:
            def related(document):
                if collection == "fitting_interfaces":
                    selections = {row["id"] for row in document.get("fitting_selections", [])
                                  if row["route_id"] == route_id}
                    return {row["id"] for row in document.get(collection, [])
                            if row["selection_id"] in selections}
                return {row["id"] for row in document.get(collection, [])
                        if row.get("route_id") == route_id or route_id in row.get("route_ids", [])}
            expected = related(before) | related(after)
            supplied = declaration[collection]
            _require(type(supplied) is list and len(set(supplied)) == len(supplied)
                     and set(supplied) == expected, "method change must enumerate all affected inventory")
            _require(all((collection, identity) in touched for identity in expected),
                     "method change must explicitly replace/remove affected inventory")
        selected = [s for s in after.get("raceway_selections", []) if s["route_id"] == route_id]
        _require(len(selected) == 1, "legacy method change requires supplied canonical product selection")
    _require(declared == changed, "every method change requires complete reconciliation")
