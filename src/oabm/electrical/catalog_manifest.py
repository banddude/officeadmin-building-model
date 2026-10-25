"""Offline, source-bound catalog input. Does not select parts or import CAD."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

from oabm.model import (AcceptedODRange, CatalogProduct, ContractError, Provenance, Size3)
from .boxes import BoxCatalogEntry
from .raceways import RacewayCatalogEntry
from .supports import SupportCatalogEntry

VERSION = "electrical-catalog-v1"
VERSION_V2 = "electrical-catalog-v2"
_FIELDS = {
    "box": {"box_type", "size", "listed_volume_m3"},
    "raceway": {"route_type", "wiring_method", "designation_kind", "designation", "nominal_diameter_m"},
    "support": {"category", "component_type", "size"},
    "reference": {"category", "designation"},
}
_V2_FIELDS = {**_FIELDS, "fitting": {"route_type", "fitting_type", "size"}}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


def _require(ok, message):
    if not ok:
        raise ContractError("catalog manifest: " + message)


def _keys(value, expected):
    _require(isinstance(value, dict) and set(value) == set(expected), "unsupported fields")


def _text(value):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 2048
             and all(ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF for c in value), "invalid text")


def _number(value):
    _require(_finite(value) and value > 0, "invalid positive number")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _hash(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), "invalid SHA-256")


def _field_provenance(records, allowed, label):
    _require(isinstance(records, tuple) and bool(records) and
             all(isinstance(record, Provenance) and record.derivation is not None and
                 isinstance(record.scope_paths, tuple) and len(record.scope_paths) == 1 and
                 record.scope_paths[0] in allowed for record in records),
             f"{label} requires field-scoped evidence")
    identities = [(record.source_id, record.scope_paths[0]) for record in records]
    _require(len(identities) == len(set(identities)), f"{label} has duplicate same-field/source evidence")
    return {record.scope_paths[0] for record in records}


def _https_url(value):
    _require(not any(c.isspace() for c in value) and not re.search(r'%(?![0-9A-Fa-f]{2})', value),
             "malformed URL")
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port  # Access validates nonnumeric and out-of-range ports.
        _require(url.scheme == "https" and bool(host) and url.username is None
                 and url.password is None and not url.fragment and (port is None or port > 0),
                 "source must be an HTTPS reference")
        _require('%' not in host, "invalid source hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # A colon or dotted all-numeric host is an invalid IP literal, not a
            # DNS name to fall back to. Other hosts use bounded IDNA/DNS labels.
            _require(':' not in host and not re.fullmatch(r'[0-9.]+', host), "invalid source hostname")
            ascii_host = host.encode('idna').decode('ascii').removesuffix('.')
            _require(len(ascii_host) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                for label in ascii_host.split('.')), "invalid source hostname")
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError("catalog manifest: malformed URL") from error


def _decode(raw):
    def pairs(rows):
        out = {}
        for key, value in rows:
            _require(key not in out, "duplicate JSON key")
            out[key] = value
        return out

    def constant(_):
        raise ContractError("catalog manifest: nonfinite JSON")

    def floating(value):
        result = float(value)
        _require(math.isfinite(result), "nonfinite JSON")
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError("catalog manifest: malformed JSON") from error


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _validate(document):
    if isinstance(document, dict) and document.get("version") == VERSION_V2:
        return _validate_v2(document)
    _keys(document, {"version", "catalog_id", "catalog_revision", "sources", "entries"})
    _require(document["version"] == VERSION, "unsupported version")
    for key in ("catalog_id", "catalog_revision"):
        _text(document[key])
    sources, entries = document["sources"], document["entries"]
    _require(isinstance(sources, list) and 0 < len(sources) <= 1000, "invalid sources")
    _require(isinstance(entries, list) and 0 < len(entries) <= 5000, "invalid entries")
    source_ids = set()
    for source in sources:
        _keys(source, {"id", "url", "title", "revision", "locator", "retrieved_at", "sha256"})
        for key in ("id", "url", "title", "locator", "retrieved_at"):
            _text(source[key])
        if source["revision"] is not None:
            _text(source["revision"])
        _hash(source["sha256"])
        _https_url(source["url"])
        _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?", source["retrieved_at"]),
                 "retrieval date or UTC timestamp required")
        try:
            datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ContractError("catalog manifest: invalid retrieval timestamp") from error
        _require(source["id"] not in source_ids, "duplicate source identity")
        source_ids.add(source["id"])
    product_ids = set()
    for entry in entries:
        _keys(entry, {"product_id", "manufacturer", "part_number", "kind", "description",
                      "geometry_basis", "assets", "fields", "evidence"})
        for key in ("product_id", "manufacturer", "part_number", "description", "kind", "geometry_basis"):
            _text(entry[key])
        _require(entry["product_id"] not in product_ids, "duplicate product identity")
        product_ids.add(entry["product_id"])
        kind = entry["kind"]
        _require(kind in _FIELDS, "unsupported entry kind")
        fields = entry["fields"]
        _keys(fields, _FIELDS[kind])
        for key, value in fields.items():
            if key == "size":
                if value is not None:
                    _keys(value, {"x", "y", "z"})
                    for dimension in value.values():
                        _number(dimension)
            elif key in {"listed_volume_m3", "nominal_diameter_m"}:
                if value is not None:
                    _number(value)
            elif value is not None:
                _text(value)
        if kind == "reference":
            _text(fields["category"])
        size = fields.get("size")
        _require(entry["geometry_basis"] == ("dimension_derived_envelope" if size is not None else "metadata_only"),
                 "emitted representation must match known envelope; native CAD is a separate asset")
        assets = entry["assets"]
        _require(isinstance(assets, list) and len(assets) <= 32, "invalid assets")
        asset_keys = set()
        for asset in assets:
            _keys(asset, {"source_id", "format", "units", "validation", "origin", "rights"})
            _require(isinstance(asset["source_id"], str) and asset["source_id"] in source_ids, "unknown asset source")
            for key, allowed in (("format", {"STEP", "DXF", "DWG", "RFA", "IFC", "GLB", "PDF"}),
                                 ("units", {"m", "mm", "in", "unknown"}),
                                 ("validation", {"uninspected", "header_only", "geometry_inspected"}),
                                 ("origin", {"manufacturer_native_cad", "manufacturer_reference", "derived"}),
                                 ("rights", {"unknown", "redistribution_permitted"})):
                _require(isinstance(asset[key], str) and asset[key] in allowed, "unsupported asset metadata")
            _require(asset["source_id"] not in asset_keys, "duplicate asset source")
            asset_keys.add(asset["source_id"])
        evidence = entry["evidence"]
        _require(isinstance(evidence, list) and 0 < len(evidence) <= 64, "invalid field evidence")
        required = {"product"} | {key for key, value in fields.items() if value is not None}
        covered = set()
        evidence_keys = set()
        for row in evidence:
            _keys(row, {"field", "source_id", "derivation", "confidence"})
            _require(isinstance(row["field"], str) and row["field"] in required, "evidence must qualify a supplied field")
            _require(isinstance(row["source_id"], str) and row["source_id"] in source_ids, "unknown evidence source")
            _require(isinstance(row["derivation"], str) and row["derivation"] in {"observed", "user", "inferred"}, "invalid derivation")
            c = row["confidence"]
            _require(_finite(c) and 0 <= c <= 1, "invalid confidence")
            identity = (row["field"], row["source_id"])
            _require(identity not in evidence_keys, "duplicate field evidence")
            evidence_keys.add(identity)
            covered.add(row["field"])
        _require(covered == required, "every supplied selector field requires explicit evidence")
        entry["assets"] = sorted(assets, key=lambda a: a["source_id"])
        entry["evidence"] = sorted(evidence, key=lambda r: (r["field"], r["source_id"]))
    document["sources"] = sorted(sources, key=lambda s: s["id"])
    document["entries"] = sorted(entries, key=lambda e: e["product_id"])
    return document


def _validate_v2(document):
    """Validate the additive v2 schema while retaining all v1 entry shapes."""
    _keys(document, {"version", "catalog_id", "catalog_revision", "sources", "entries"})
    _require(document["version"] == VERSION_V2, "unsupported version")
    for key in ("catalog_id", "catalog_revision"):
        _text(document[key])
    sources, entries = document["sources"], document["entries"]
    _require(isinstance(sources, list) and 0 < len(sources) <= 1000, "invalid sources")
    _require(isinstance(entries, list) and 0 < len(entries) <= 5000, "invalid entries")
    source_ids = set()
    for source in sources:
        _keys(source, {"id", "url", "title", "revision", "locator", "retrieved_at", "sha256"})
        for key in ("id", "url", "title", "locator", "retrieved_at"):
            _text(source[key])
        if source["revision"] is not None:
            _text(source["revision"])
        _hash(source["sha256"])
        _https_url(source["url"])
        _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?", source["retrieved_at"]),
                 "retrieval date or UTC timestamp required")
        try:
            datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ContractError("catalog manifest: invalid retrieval timestamp") from error
        _require(source["id"] not in source_ids, "duplicate source identity")
        source_ids.add(source["id"])

    def evidence_rows(rows, required_fields, label):
        _require(isinstance(rows, list) and 0 < len(rows) <= 128, f"invalid {label} evidence")
        covered, identities = set(), set()
        for row in rows:
            _keys(row, {"field", "source_id", "derivation", "confidence"})
            _require(isinstance(row["field"], str) and row["field"] in required_fields,
                     f"{label} evidence must qualify a supplied field")
            _require(isinstance(row["source_id"], str) and row["source_id"] in source_ids,
                     "unknown evidence source")
            _require(isinstance(row["derivation"], str) and
                     row["derivation"] in {"observed", "user", "inferred"}, "invalid derivation")
            confidence = row["confidence"]
            _require(_finite(confidence) and 0 <= confidence <= 1, "invalid confidence")
            identity = (row["field"], row["source_id"])
            _require(identity not in identities, "duplicate field evidence")
            identities.add(identity)
            covered.add(row["field"])
        _require(covered == set(required_fields), f"{label} requires evidence for every supplied field")
        return sorted(rows, key=lambda row: (row["field"], row["source_id"]))

    product_ids = set()
    for entry in entries:
        base_keys = {"product_id", "manufacturer", "part_number", "kind", "description",
                     "geometry_basis", "assets", "fields", "evidence"}
        if isinstance(entry, dict) and entry.get("kind") == "fitting":
            _keys(entry, base_keys | {"interfaces", "mating_pairs"})
        else:
            _keys(entry, base_keys)
        for key in ("product_id", "manufacturer", "part_number", "description", "kind", "geometry_basis"):
            _text(entry[key])
        _require(entry["product_id"] not in product_ids, "duplicate product identity")
        product_ids.add(entry["product_id"])
        kind = entry["kind"]
        _require(kind in _V2_FIELDS, "unsupported entry kind")
        fields = entry["fields"]
        _keys(fields, _V2_FIELDS[kind])
        for key, value in fields.items():
            if key == "size":
                if value is not None:
                    _keys(value, {"x", "y", "z"})
                    for dimension in value.values():
                        _number(dimension)
            elif key in {"listed_volume_m3", "nominal_diameter_m"}:
                if value is not None:
                    _number(value)
            elif value is not None:
                _text(value)
        if kind == "reference":
            _text(fields["category"])
        if kind == "fitting":
            _require(fields["route_type"] == "emt", "v2 fitting route_type must be emt")
            _require(fields["fitting_type"] in {"coupling", "connector"}, "unsupported fitting type")
        size = fields.get("size")
        _require(entry["geometry_basis"] == ("dimension_derived_envelope" if size is not None else "metadata_only"),
                 "emitted representation must match known envelope; native CAD is a separate asset")
        assets = entry["assets"]
        _require(isinstance(assets, list) and len(assets) <= 32, "invalid assets")
        asset_keys = set()
        for asset in assets:
            _keys(asset, {"source_id", "format", "units", "validation", "origin", "rights"})
            _require(isinstance(asset["source_id"], str) and asset["source_id"] in source_ids, "unknown asset source")
            for key, allowed in (("format", {"STEP", "DXF", "DWG", "RFA", "IFC", "GLB", "PDF"}),
                                 ("units", {"m", "mm", "in", "unknown"}),
                                 ("validation", {"uninspected", "header_only", "geometry_inspected"}),
                                 ("origin", {"manufacturer_native_cad", "manufacturer_reference", "derived"}),
                                 ("rights", {"unknown", "redistribution_permitted"})):
                _require(isinstance(asset[key], str) and asset[key] in allowed, "unsupported asset metadata")
            _require(asset["source_id"] not in asset_keys, "duplicate asset source")
            asset_keys.add(asset["source_id"])
        required = {"product"} | {key for key, value in fields.items() if value is not None}
        entry["assets"] = sorted(assets, key=lambda asset: asset["source_id"])
        entry["evidence"] = evidence_rows(entry["evidence"], required, "product")

        if kind != "fitting":
            continue
        interfaces = entry["interfaces"]
        _require(isinstance(interfaces, list) and len(interfaces) == 2,
                 "a fitting requires exactly two explicit catalog interfaces")
        interface_by_key, pair_ids = {}, set()
        for interface in interfaces:
            _keys(interface, {"key", "role", "connection_method", "permitted_wiring_methods",
                              "nominal_designation", "actual_accepted_od_m", "dry_location",
                              "wet_location", "raintight", "listing_refs", "evidence"})
            _require(isinstance(interface["key"], str) and _STABLE_KEY.fullmatch(interface["key"]),
                     "invalid or duplicate interface key")
            _require(interface["key"] not in interface_by_key, "duplicate interface key")
            _require(isinstance(interface["role"], str) and interface["role"] in {"raceway", "enclosure"},
                     "unsupported interface role")
            _require(isinstance(interface["connection_method"], str) and interface["connection_method"] in
                     {"set_screw", "compression", "threaded", "clamp", "snap_in"},
                     "unsupported interface method")
            methods = interface["permitted_wiring_methods"]
            _require(isinstance(methods, list) and bool(methods) and
                     all(isinstance(method, str) and method in {"raceway", "cable"} for method in methods) and
                     len(methods) == len(set(methods)),
                     "invalid permitted wiring methods")
            if interface["nominal_designation"] is not None:
                _text(interface["nominal_designation"])
            accepted = interface["actual_accepted_od_m"]
            if accepted is not None:
                _keys(accepted, {"min_m", "max_m"})
                _number(accepted["min_m"]); _number(accepted["max_m"])
                _require(accepted["min_m"] <= accepted["max_m"], "invalid actual-OD range")
            for fact in ("dry_location", "wet_location", "raintight"):
                _require(interface[fact] is None or type(interface[fact]) is bool,
                         f"{fact} must be true, false, or unknown")
            refs = interface["listing_refs"]
            _require(isinstance(refs, list) and all(isinstance(ref, str) for ref in refs) and
                     len(refs) == len(set(refs)), "invalid listing references")
            for ref in refs:
                _text(ref)
            supplied = {"key", "role", "connection_method", "permitted_wiring_methods"}
            supplied |= {name for name in ("nominal_designation", "actual_accepted_od_m", "dry_location",
                                            "wet_location", "raintight") if interface[name] is not None}
            if refs:
                supplied.add("listing_refs")
            interface["evidence"] = evidence_rows(interface["evidence"], supplied, "interface")
            interface["permitted_wiring_methods"] = sorted(methods)
            interface["listing_refs"] = sorted(refs)
            interface_by_key[interface["key"]] = interface

        pairs = entry["mating_pairs"]
        _require(isinstance(pairs, list) and len(pairs) >= 2,
                 "fitting requires source-evidenced exact mating-pair facts")
        pair_interfaces = set()
        for pair in pairs:
            _keys(pair, {"id", "interface_key", "mating_product", "mating_interface_key", "evidence"})
            _require(isinstance(pair["id"], str) and _STABLE_KEY.fullmatch(pair["id"]), "invalid mating-pair id")
            _require(pair["id"] not in pair_ids, "duplicate mating-pair id")
            pair_ids.add(pair["id"])
            _require(isinstance(pair["interface_key"], str) and pair["interface_key"] in interface_by_key,
                     "pair references an unknown fitting interface")
            pair_interfaces.add(pair["interface_key"])
            mate = pair["mating_product"]
            _keys(mate, {"catalog_id", "catalog_revision", "product_id"})
            for key in ("catalog_id", "catalog_revision", "product_id"):
                _text(mate[key])
            _require(isinstance(pair["mating_interface_key"], str) and
                     _STABLE_KEY.fullmatch(pair["mating_interface_key"]), "invalid mating interface key")
            pair["evidence"] = evidence_rows(pair["evidence"],
                {"id", "interface_key", "mating_product", "mating_interface_key"}, "mating pair")
        expected_roles = {"raceway"} if fields["fitting_type"] == "coupling" else {"raceway", "enclosure"}
        _require({interface["role"] for interface in interfaces} == expected_roles,
                 "fitting catalog interfaces contradict supported topology")
        _require(pair_interfaces == set(interface_by_key), "every fitting interface requires a mating-pair fact")
        entry["interfaces"] = sorted(interfaces, key=lambda interface: interface["key"])
        entry["mating_pairs"] = sorted(pairs, key=lambda pair: pair["id"])
    document["sources"] = sorted(sources, key=lambda source: source["id"])
    document["entries"] = sorted(entries, key=lambda entry: entry["product_id"])
    return document


@dataclass(frozen=True, slots=True)
class FittingCatalogInterface:
    key: str
    role: str
    connection_method: str
    permitted_wiring_methods: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    nominal_designation: str | None = None
    actual_accepted_od_m: AcceptedODRange | None = None
    dry_location: bool | None = None
    wet_location: bool | None = None
    raintight: bool | None = None
    listing_refs: tuple[str, ...] = ()

    def __post_init__(self):
        _require(isinstance(self.key, str) and _STABLE_KEY.fullmatch(self.key), "invalid fitting interface key")
        _require(isinstance(self.role, str) and self.role in {"raceway", "enclosure"}, "unsupported fitting interface role")
        _require(isinstance(self.connection_method, str) and self.connection_method in
                 {"set_screw", "compression", "threaded", "clamp", "snap_in"},
                 "unsupported fitting connection method")
        _require(isinstance(self.permitted_wiring_methods, tuple) and bool(self.permitted_wiring_methods) and
                 all(isinstance(method, str) for method in self.permitted_wiring_methods) and
                 len(self.permitted_wiring_methods) == len(set(self.permitted_wiring_methods)) and
                 all(method in {"raceway", "cable"} for method in self.permitted_wiring_methods),
                 "invalid fitting wiring methods")
        covered = _field_provenance(self.provenance, {"interface_key", "role", "connection_method",
            "permitted_wiring_methods", "nominal_designation", "actual_accepted_od_m",
            "dry_location", "wet_location", "raintight", "listing_refs"}, "fitting interface")
        required = {"interface_key", "role", "connection_method", "permitted_wiring_methods"}
        required |= {name for name in ("nominal_designation", "actual_accepted_od_m", "dry_location",
                                        "wet_location", "raintight") if getattr(self, name) is not None}
        if self.listing_refs:
            required.add("listing_refs")
        _require(covered == required, "fitting interface evidence must cover every supplied fact exactly")
        _require(self.nominal_designation is None or isinstance(self.nominal_designation, str) and
                 bool(self.nominal_designation.strip()), "invalid nominal designation")
        _require(self.actual_accepted_od_m is None or isinstance(self.actual_accepted_od_m, AcceptedODRange),
                 "invalid accepted OD evidence")
        _require(all(value is None or type(value) is bool for value in
                     (self.dry_location, self.wet_location, self.raintight)),
                 "environment facts must be true, false, or unknown")
        _require(isinstance(self.listing_refs, tuple) and all(isinstance(ref, str) for ref in self.listing_refs) and
                 len(self.listing_refs) == len(set(self.listing_refs)),
                 "listing references must be unique")
        for ref in self.listing_refs:
            _text(ref)


@dataclass(frozen=True, slots=True)
class FittingMatingPair:
    id: str
    interface_key: str
    mating_product: CatalogProduct
    mating_interface_key: str
    provenance: tuple[Provenance, ...]

    def __post_init__(self):
        _require(isinstance(self.id, str) and _STABLE_KEY.fullmatch(self.id), "invalid mating pair id")
        _require(isinstance(self.interface_key, str) and _STABLE_KEY.fullmatch(self.interface_key),
                 "invalid fitting interface key on mating pair")
        _require(isinstance(self.mating_product, CatalogProduct), "mating pair requires full product identity")
        _require(isinstance(self.mating_interface_key, str) and _STABLE_KEY.fullmatch(self.mating_interface_key),
                 "invalid counterpart interface key")
        covered = _field_provenance(self.provenance, {"mating_pair_id", "interface_key", "mating_product",
            "mating_interface_key"}, "mating pair")
        _require(covered == {"mating_pair_id", "interface_key", "mating_product", "mating_interface_key"},
                 "mating pair evidence must cover every exact pair field")


@dataclass(frozen=True, slots=True)
class FittingCatalogEntry:
    product: CatalogProduct
    route_type: str
    fitting_type: str
    geometry_basis: str
    interfaces: tuple[FittingCatalogInterface, ...]
    mating_pairs: tuple[FittingMatingPair, ...]
    provenance: tuple[Provenance, ...]
    manifest_sha256: str
    size: Size3 | None = None

    def __post_init__(self):
        _require(isinstance(self.product, CatalogProduct), "fitting catalog entry requires a product")
        _require(isinstance(self.route_type, str) and self.route_type == "emt" and
                 isinstance(self.fitting_type, str) and self.fitting_type in {"coupling", "connector"},
                 "fitting catalog entry is outside the supported EMT topology")
        _require(isinstance(self.geometry_basis, str) and
                 self.geometry_basis in {"dimension_derived_envelope", "metadata_only"},
                 "unsupported fitting geometry basis")
        _require(self.size is None or isinstance(self.size, Size3), "fitting envelope must be a Size3 or unknown")
        _require((self.geometry_basis == "dimension_derived_envelope") == (self.size is not None),
                 "fitting envelope does not match geometry basis")
        _require(isinstance(self.interfaces, tuple) and len(self.interfaces) == 2 and
                 all(isinstance(item, FittingCatalogInterface) for item in self.interfaces),
                 "fitting entry requires exactly two typed interfaces")
        keys = {item.key for item in self.interfaces}
        _require(len(keys) == 2, "duplicate fitting interface key")
        _require(isinstance(self.mating_pairs, tuple) and len(self.mating_pairs) >= 2 and
                 all(isinstance(pair, FittingMatingPair) and pair.interface_key in keys for pair in self.mating_pairs),
                 "fitting entry requires pair facts for known interfaces")
        _require(len({pair.id for pair in self.mating_pairs}) == len(self.mating_pairs),
                 "duplicate fitting mating-pair id")
        expected_roles = {"raceway"} if self.fitting_type == "coupling" else {"raceway", "enclosure"}
        _require({item.role for item in self.interfaces} == expected_roles,
                 "fitting interfaces contradict the supported topology")
        _require({pair.interface_key for pair in self.mating_pairs} == keys,
                 "every fitting interface requires a mating-pair fact")
        covered = _field_provenance(self.provenance, {"product", "route_type", "fitting_type", "size"},
                                    "fitting catalog entry")
        required = {"product", "route_type", "fitting_type"} | ({"size"} if self.size is not None else set())
        _require(covered == required, "fitting catalog entry evidence must cover every supplied field")
        _hash(self.manifest_sha256)


@dataclass(frozen=True, slots=True)
class CatalogManifest:
    """Immutable byte snapshot; every returned record is a detached copy.

    Source hashes are declarations, not verification that documents were read.
    The caller must retain/trust these bytes. This class performs no I/O.
    """
    raw: bytes = field(repr=False)
    sha256: str = field(init=False)
    _normalized: str = field(init=False, repr=False)

    def __post_init__(self):
        _require(type(self.raw) is bytes and 0 < len(self.raw) <= 8 * 1024 * 1024, "bounded immutable bytes required")
        normalized = _json(_validate(_decode(self.raw)))
        object.__setattr__(self, "sha256", hashlib.sha256(self.raw).hexdigest())
        object.__setattr__(self, "_normalized", normalized)
        # Enforce existing typed selector contracts on ingestion, not at dispatch.
        for record in self.search():
            if record["kind"] == "fitting":
                self.fitting_entry(record["product_id"])
            elif record["kind"] != "reference":
                self.selector_entry(record["product_id"])

    def to_json(self):
        return self._normalized

    def search(self, query="", *, kind=None, manufacturer=None):
        _require(isinstance(query, str) and len(query) <= 2048, "invalid query")
        allowed_kinds = _FIELDS if self.version == VERSION else _V2_FIELDS
        _require(kind is None or isinstance(kind, str) and kind in allowed_kinds, "unsupported kind filter")
        _require(manufacturer is None or isinstance(manufacturer, str), "invalid manufacturer filter")
        words = query.casefold().split()
        rows = json.loads(self._normalized)["entries"]
        return tuple(row for row in rows if (kind is None or row["kind"] == kind)
                     and (manufacturer is None or row["manufacturer"].casefold() == manufacturer.casefold())
                     and all(word in (row["manufacturer"] + " " + row["part_number"] + " " + row["description"]).casefold()
                             for word in words))

    def selector_entry(self, product_id):
        """Return an existing typed input; no automatic substitution/selection."""
        _text(product_id)
        doc = json.loads(self._normalized)
        entry = next((e for e in doc["entries"] if e["product_id"] == product_id), None)
        _require(entry is not None, "unknown product identity")
        if entry["kind"] == "reference":
            _require(False, "reference-only fitting/accessory has no selector contract")
        _require(entry["kind"] != "fitting", "fitting entries require their explicit fitting selector contract")
        product = CatalogProduct(catalog_id=doc["catalog_id"], catalog_revision=doc["catalog_revision"], product_id=product_id)
        sources = {s["id"]: s for s in doc["sources"]}
        records = tuple(Provenance(source_kind="catalog-manifest", source_id=sources[r["source_id"]]["url"],
            source_element_id=entry["part_number"], method="explicit-catalog-field-reference",
            derivation=r["derivation"], confidence=r["confidence"], scope_paths=(r["field"],),
            attributes={"manifest_sha256": self.sha256, "source": sources[r["source_id"]],
                        "manufacturer": entry["manufacturer"], "part_number": entry["part_number"],
                        "emitted_geometry_basis": entry["geometry_basis"], "asset_references": entry["assets"],
                        "source_authenticity_verified": False, "compatibility_verified": False})
                        for r in entry["evidence"])
        fields = entry["fields"]
        if "size" in fields and fields["size"] is not None:
            fields["size"] = Size3(**fields["size"])
        factory = {"box": BoxCatalogEntry, "raceway": RacewayCatalogEntry, "support": SupportCatalogEntry}[entry["kind"]]
        return factory(product=product, provenance=records, **fields)

    def fitting_entry(self, product_id):
        """Return a typed v2 fitting entry with explicit interface/pair facts."""
        _require(self.version == VERSION_V2, "fitting selector requires electrical-catalog-v2")
        _text(product_id)
        doc = json.loads(self._normalized)
        entry = next((row for row in doc["entries"] if row["product_id"] == product_id), None)
        _require(entry is not None and entry["kind"] == "fitting", "unknown fitting product identity")
        sources = {source["id"]: source for source in doc["sources"]}
        product = CatalogProduct(catalog_id=doc["catalog_id"],
            catalog_revision=doc["catalog_revision"], product_id=product_id)

        def records(evidence, part_number, field_map=None):
            result = []
            for row in evidence:
                field_name = (field_map or {}).get(row["field"], row["field"])
                source = sources[row["source_id"]]
                result.append(Provenance(source_kind="catalog-manifest", source_id=source["url"],
                    source_element_id=part_number, method="explicit-catalog-field-reference",
                    derivation=row["derivation"], confidence=row["confidence"],
                    scope_paths=(field_name,), attributes={"manifest_sha256": self.sha256,
                        "source_id": source["id"], "source_locator": source["locator"],
                        "catalog_field": row["field"], "source": source,
                        "part_number": part_number, "source_authenticity_verified": False,
                        "compatibility_verified": False}))
            return tuple(result)

        raw_interfaces = []
        for item in entry["interfaces"]:
            mapped = {"key": "interface_key"}
            raw_interfaces.append(FittingCatalogInterface(
                key=item["key"], role=item["role"], connection_method=item["connection_method"],
                permitted_wiring_methods=tuple(item["permitted_wiring_methods"]),
                nominal_designation=item["nominal_designation"],
                actual_accepted_od_m=None if item["actual_accepted_od_m"] is None else
                    AcceptedODRange(**item["actual_accepted_od_m"]),
                dry_location=item["dry_location"], wet_location=item["wet_location"],
                raintight=item["raintight"], listing_refs=tuple(item["listing_refs"]),
                provenance=records(item["evidence"], entry["part_number"], mapped)))
        raw_pairs = tuple(FittingMatingPair(
            id=item["id"], interface_key=item["interface_key"],
            mating_product=CatalogProduct(**item["mating_product"]),
            mating_interface_key=item["mating_interface_key"],
            provenance=records(item["evidence"], entry["part_number"],
                {"id": "mating_pair_id", "interface_key": "interface_key",
                 "mating_product": "mating_product", "mating_interface_key": "mating_interface_key"}))
            for item in entry["mating_pairs"])
        base = entry["fields"]
        size = None if base["size"] is None else Size3(**base["size"])
        base_fields = {"product": "product", "route_type": "route_type",
                       "fitting_type": "fitting_type", "size": "size"}
        return FittingCatalogEntry(product=product, route_type=base["route_type"],
            fitting_type=base["fitting_type"], size=size,
            geometry_basis=entry["geometry_basis"], interfaces=tuple(raw_interfaces),
            mating_pairs=raw_pairs,
            provenance=records(entry["evidence"], entry["part_number"], base_fields),
            manifest_sha256=self.sha256)

    @property
    def version(self):
        return json.loads(self._normalized)["version"]

    def asset_candidates(self, product_id, *, format=None):
        """Exact-part links for on-demand acquisition, never a download grant.

        No discovery/crawling, network call, credential use or cache mutation.
        PDF reference drawings are not substituted for native model assets.
        """
        _text(product_id)
        _require(format is None or isinstance(format, str) and format in
                 {"STEP", "DXF", "DWG", "RFA", "IFC", "GLB"}, "unsupported model format")
        doc = json.loads(self._normalized)
        entry = next((e for e in doc["entries"] if e["product_id"] == product_id), None)
        _require(entry is not None, "unknown product identity")
        sources = {s["id"]: s for s in doc["sources"]}
        return tuple({"product": {"catalog_id": doc["catalog_id"], "catalog_revision": doc["catalog_revision"],
                                   "product_id": product_id},
                      "manifest_sha256": self.sha256, "asset": asset, "source": sources[asset["source_id"]],
                      "acquisition_performed": False, "usage_permission_verified": False}
                     for asset in entry["assets"] if asset["format"] != "PDF"
                     and (format is None or asset["format"] == format))


def main(argv=None):
    """Search a local manifest. Never downloads sources or writes model files."""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--query", default="")
    parser.add_argument("--kind", choices=sorted(_V2_FIELDS))
    parser.add_argument("--manufacturer")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    try:
        # Bound the read itself, rather than reading an arbitrary binary first.
        with args.manifest.open("rb") as stream:
            manifest = CatalogManifest(stream.read(8 * 1024 * 1024 + 1))
        if args.expected_sha256 is not None:
            _hash(args.expected_sha256)
            _require(manifest.sha256 == args.expected_sha256, "pinned manifest hash mismatch")
        rows = manifest.search(args.query, kind=args.kind, manufacturer=args.manufacturer)
    except (OSError, ContractError) as error:
        parser.error(str(error))
    print(_json({"version": manifest.version, "manifest_sha256": manifest.sha256, "entries": rows,
                 "automatic_selection_performed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
