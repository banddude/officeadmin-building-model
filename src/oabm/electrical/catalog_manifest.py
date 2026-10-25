"""Offline, source-bound catalog input. Does not select parts or import CAD."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from oabm.model import CatalogProduct, ContractError, Provenance, Size3
from .boxes import BoxCatalogEntry
from .raceways import RacewayCatalogEntry
from .supports import SupportCatalogEntry

VERSION = "electrical-catalog-v1"
_FIELDS = {
    "box": {"box_type", "size", "listed_volume_m3"},
    "raceway": {"route_type", "wiring_method", "designation_kind", "designation", "nominal_diameter_m"},
    "support": {"category", "component_type", "size"},
    "reference": {"category", "designation"},
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")


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
        try:
            url = urlsplit(source["url"])
            _require(url.scheme == "https" and bool(url.hostname) and url.username is None
                     and url.password is None and not url.fragment, "source must be an HTTPS reference")
        except ValueError as error:
            raise ContractError("catalog manifest: malformed URL") from error
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
            if record["kind"] != "reference":
                self.selector_entry(record["product_id"])

    def to_json(self):
        return self._normalized

    def search(self, query="", *, kind=None, manufacturer=None):
        _require(isinstance(query, str) and len(query) <= 2048, "invalid query")
        _require(kind is None or isinstance(kind, str) and kind in _FIELDS, "unsupported kind filter")
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
        _require(entry["kind"] != "reference", "reference-only fitting/accessory has no selector contract")
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
    parser.add_argument("--kind", choices=sorted(_FIELDS))
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
    print(_json({"version": VERSION, "manifest_sha256": manifest.sha256, "entries": rows,
                 "automatic_selection_performed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
