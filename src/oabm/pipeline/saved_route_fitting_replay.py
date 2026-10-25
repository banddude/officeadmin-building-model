"""Strict local byte-bound replay for the independent v1.5 fitting slice.

This reader reruns the fitting selector, quantity extraction and artifact
exporters from retained inputs. It performs no network/file writes, never reuses cached generated
outputs, and treats source authenticity, code/listing compliance, native CAD,
and engineering approval as unverified.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping

from oabm.drawings import PlanSpec, generate_drawing_set
from oabm.electrical.catalog_manifest import CatalogManifest
from oabm.electrical.fittings import (
    FittingIntent, FittingPortBinding, FittingRule, FittingRulePermission,
    select_route_fittings,
)
from oabm.exports.gltf import build_glb_bytes
from oabm.ifc.adapter import to_ifc
from oabm.model import (
    BuildingModel, CatalogProduct, ContractError, FITTING_SCHEMA_VERSION,
    Provenance, RuleReference,
)
from oabm.quantities import extract_quantities

VERSION = "saved-route-fitting-replay-v1"
_INPUT_KEYS = {"base_model", "catalog_manifest", "intents", "rules", "drawing_plans"}
_OUTPUT_KEYS = {"canonical_model", "quantities", "glb", "ifc", "drawings"}
_INTENT_KEYS = {"intent_id", "fitting_id", "actor_id", "requested_product", "basis", "bindings"}
_BINDING_KEYS = {"interface_key", "mating_interface_key", "fitting_port_id", "counterpart_port_id"}
_RULE_KEYS = {"reference", "permitted_products", "permitted_topologies", "check_actual_od",
              "required_environment", "required_listing_refs", "provenance"}
_PROVENANCE_KEYS = {"source_kind", "source_id", "source_element_id", "page", "method",
                    "confidence", "derivation", "scope_paths", "attributes"}
_PLAN_KEYS = {"id", "level_id", "cut_height_m", "view_depth_below_m", "view_depth_above_m", "scale"}
_MAX_FILE_BYTES = 32 * 1024 * 1024


def _fail(message: str) -> None:
    raise ContractError(message)


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    _fail("non-finite JSON number")


def _float_token(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        _fail("non-finite JSON number")
    return result


def _parse_json(data: bytes, label: str):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float_token)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"{label} is malformed JSON") from exc
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_local_with_identity(path: Path) -> tuple[bytes, tuple[int, int]]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            _fail("saved replay references must identify regular local files")
        payload = stream.read(_MAX_FILE_BYTES + 1)
        if len(payload) > _MAX_FILE_BYTES:
            _fail("saved replay input/output file exceeds the bounded byte limit")
        return payload, (status.st_dev, status.st_ino)


def _canonical_relative_path(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or "://" in value or "\x00" in value):
        _fail(f"{label} must use a relative local path")
    parsed = PurePosixPath(value)
    parts = parsed.parts
    if parsed.is_absolute() or not parts or any(part in {".", ".."} for part in parts):
        _fail(f"{label} path escapes the replay directory")
    canonical = "/".join(parts)
    if value != canonical:
        _fail(f"{label} path must be canonical (no dot, repeated, or trailing separators)")
    return canonical


def _keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail(f"{label} requires exactly these keys: {sorted(expected)}")
    return value


def _reference(root: Path, value: Any, label: str) -> tuple[bytes, dict[str, Any], tuple[int, int]]:
    row = _keys(value, {"path", "bytes", "sha256"}, f"{label} file reference")
    path_value = _canonical_relative_path(row["path"], label)
    parts = PurePosixPath(path_value).parts
    if (isinstance(row["bytes"], bool) or not isinstance(row["bytes"], int) or row["bytes"] < 1
            or not isinstance(row["sha256"], str) or len(row["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in row["sha256"])):
        _fail(f"{label} requires an exact byte count and lowercase SHA-256")
    candidate = root.joinpath(*parts)
    # Reject symlinks at every component as well as resolved paths outside the
    # selected directory; no downloader or ambient absolute path is consulted.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            _fail(f"{label} path contains a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{label} file is missing") from exc
    if not resolved.is_relative_to(root) or resolved != candidate.absolute():
        _fail(f"{label} path escapes the replay directory")
    payload, identity = _read_local_with_identity(candidate)
    if len(payload) != row["bytes"] or _sha(payload) != row["sha256"]:
        _fail(f"{label} byte/hash receipt mismatch")
    return payload, {"path": path_value, "bytes": len(payload), "sha256": _sha(payload)}, identity


def _product(value: Any, label: str) -> CatalogProduct | None:
    if value is None:
        return None
    return CatalogProduct(**_keys(value, {"catalog_id", "catalog_revision", "product_id"}, label))


def _basis(value: Any, label: str) -> RuleReference | None:
    if value is None:
        return None
    return RuleReference(**_keys(value, {"authority", "revision", "rule_id"}, label))


def _provenance(value: Any) -> Provenance:
    row = dict(_keys(value, _PROVENANCE_KEYS, "rule provenance"))
    scopes = row["scope_paths"]
    if scopes is not None:
        if not isinstance(scopes, list):
            _fail("rule provenance scope_paths must be an array or null")
        row["scope_paths"] = tuple(scopes)
    derivation = row.pop("derivation")
    return Provenance(**row, derivation=derivation)


def _intent(value: Any) -> FittingIntent:
    row = _keys(value, _INTENT_KEYS, "fitting intent")
    bindings = row["bindings"]
    if not isinstance(bindings, list):
        _fail("fitting intent bindings must be an array")
    typed_bindings = tuple(FittingPortBinding(**_keys(binding, _BINDING_KEYS, "fitting port binding"))
                           for binding in bindings)
    return FittingIntent(intent_id=row["intent_id"], fitting_id=row["fitting_id"],
        actor_id=row["actor_id"], requested_product=_product(row["requested_product"], "requested product"),
        basis=_basis(row["basis"], "requested rule basis"), bindings=typed_bindings)


def _rule(value: Any) -> FittingRule:
    row = _keys(value, _RULE_KEYS, "fitting rule")
    products, topologies, provenance = row["permitted_products"], row["permitted_topologies"], row["provenance"]
    environment, listing_refs = row["required_environment"], row["required_listing_refs"]
    if (not isinstance(products, list) or not isinstance(topologies, list) or
            not isinstance(provenance, list) or not isinstance(environment, list) or
            not isinstance(listing_refs, list)):
        _fail("fitting rule products, topologies, environment, listings, and provenance must be arrays")
    return FittingRule(
        reference=_basis(row["reference"], "rule reference"),
        permitted_products=tuple(_product(item, "permitted product") for item in products),
        permitted_topologies=tuple(FittingRulePermission(**_keys(item, {"route_type", "fitting_type"},
                                                                "permitted topology")) for item in topologies),
        check_actual_od=row["check_actual_od"],
        required_environment=tuple(environment),
        required_listing_refs=tuple(listing_refs),
        provenance=tuple(_provenance(item) for item in provenance),
    )


def _plan(value: Any) -> PlanSpec:
    row = _keys(value, _PLAN_KEYS, "drawing plan")
    return PlanSpec(**row)


def replay_saved_route_fitting(manifest_path: str | Path) -> dict[str, Any]:
    """Regenerate a complete fitting-selection artifact set from saved bytes.

    The versioned manifest has exact keys ``version``, ``inputs`` and
    ``expected_outputs``. Every nested file reference has exactly ``path``,
    ``bytes`` and ``sha256``; all paths are local and root-confined.
    """
    manifest_file = Path(manifest_path).absolute()
    if manifest_file.is_symlink():
        _fail("saved replay manifest may not be a symlink")
    root = manifest_file.parent.resolve(strict=True)
    manifest_payload, manifest_identity = _read_local_with_identity(manifest_file)
    manifest = _keys(_parse_json(manifest_payload, "saved replay manifest"),
                     {"version", "inputs", "expected_outputs"}, "saved replay manifest")
    if manifest["version"] != VERSION:
        _fail(f"unsupported saved route fitting replay version {manifest['version']!r}")
    inputs = _keys(manifest["inputs"], _INPUT_KEYS, "saved replay inputs")
    expected = _keys(manifest["expected_outputs"], _OUTPUT_KEYS, "saved replay expected outputs")
    all_references = [*inputs.values(), *expected.values()]
    paths = []
    for row in all_references:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            _fail("saved replay file references require string paths")
        paths.append(_canonical_relative_path(row["path"], "saved replay"))
    if len(paths) != len(set(paths)):
        _fail("saved replay file paths must be unique")
    payloads, receipts = {}, {}
    identities = {manifest_identity}
    for key in sorted(_INPUT_KEYS):
        payload, receipt, identity = _reference(root, inputs[key], "input " + key)
        if identity in identities:
            _fail("saved replay references must use distinct physical files")
        identities.add(identity)
        payloads[key], receipts[key] = payload, receipt
    base = BuildingModel.from_dict(_parse_json(payloads["base_model"], "base model"))
    if base.schema_version != FITTING_SCHEMA_VERSION:
        _fail("saved route fitting replay requires canonical schema 1.5.0")
    catalog = CatalogManifest(payloads["catalog_manifest"])
    intent_doc = _keys(_parse_json(payloads["intents"], "fitting intents"),
                       {"version", "items"}, "fitting intents document")
    rule_doc = _keys(_parse_json(payloads["rules"], "fitting rules"),
                     {"version", "items"}, "fitting rules document")
    plans_doc = _keys(_parse_json(payloads["drawing_plans"], "drawing plans"),
                      {"version", "items"}, "drawing plans document")
    if intent_doc["version"] != "saved-route-fitting-intents-v1" or rule_doc["version"] != "saved-route-fitting-rules-v1" or plans_doc["version"] != "saved-route-fitting-plans-v1":
        _fail("saved fitting intent/rule/plan document version is unsupported")
    if any(not isinstance(doc["items"], list) for doc in (intent_doc, rule_doc, plans_doc)):
        _fail("saved fitting intents/rules/plans items must be arrays")
    intents = tuple(_intent(item) for item in intent_doc["items"])
    rules = tuple(_rule(item) for item in rule_doc["items"])
    plans = tuple(_plan(item) for item in plans_doc["items"])
    if not intents or not plans:
        _fail("saved fitting replay requires explicit intents and at least one drawing plan")
    if len({plan.id for plan in plans}) != len(plans):
        _fail("drawing plan ids must be unique")

    # Run the selector on pinned catalog bytes. The canonical base model may
    # contain an earlier output from this same producer; that inventory is
    # explicitly reported below, then the selector revalidates/reapplies it.
    # Generated expected artifacts are never used as cached outputs.
    selected = select_route_fittings(base, intents=intents, catalog=catalog, rules=rules)
    from oabm.drawings import generate_drawing_set
    from oabm.ifc.adapter import to_ifc
    actual = {
        "canonical_model": selected.to_json(indent=None).encode("utf-8"),
        "quantities": extract_quantities(selected).to_json(indent=None).encode("utf-8"),
        "glb": build_glb_bytes(selected),
        "ifc": to_ifc(selected).to_string().encode("utf-8"),
        "drawings": generate_drawing_set(selected, plans=plans).to_json(indent=None).encode("utf-8"),
    }
    prior_inventory = (base.fitting_selections, base.fitting_interfaces,
                       base.fitting_selection_outcomes)
    has_prior_inventory = any(prior_inventory)
    inventory_unchanged = (base.fitting_selections == selected.fitting_selections and
                           base.fitting_interfaces == selected.fitting_interfaces and
                           base.fitting_selection_outcomes == selected.fitting_selection_outcomes)
    if not has_prior_inventory:
        application_mode = "first_application"
    elif inventory_unchanged:
        application_mode = "idempotent_reapplication"
    else:
        application_mode = "reapplication_with_changes"
    output_receipts = {}
    for key in sorted(_OUTPUT_KEYS):
        expected_payload, receipt, identity = _reference(root, expected[key], "expected output " + key)
        if identity in identities:
            _fail("saved replay references must use distinct physical files")
        identities.add(identity)
        output_receipts[key] = {"expected": receipt, "actual": {"bytes": len(actual[key]), "sha256": _sha(actual[key])}}
        if actual[key] != expected_payload:
            _fail(f"freshly regenerated {key} bytes differ from the saved expected output")
    return {
        "version": VERSION,
        "manifest_sha256": _sha(manifest_payload),
        "input_receipts": receipts,
        "output_receipts": output_receipts,
        "freshly_regenerated_outputs": sorted(actual),
        "application_mode": application_mode,
        "base_fitting_inventory": {
            "route_fittings": len(base.route_fittings),
            "selections": len(base.fitting_selections),
            "interfaces": len(base.fitting_interfaces),
            "outcomes": len(base.fitting_selection_outcomes),
        },
        "comparison": "exact fresh byte comparison; no cached output reuse",
        "ifc_header_timestamp_policy": "existing IFC exporter pins a deterministic header value; it is not replay/event time",
        "source_authenticity_verified": False,
        "product_fit_verified": False,
        "code_or_listing_compliance_verified": False,
        "native_cad_imported": False,
        "engineering_approval": False,
    }
