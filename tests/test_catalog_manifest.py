"""Anonymous catalog bytes, not manufacturer content or installation approval."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from oabm.electrical.boxes import BoxIntent, select_electrical_boxes
from oabm.electrical.catalog_manifest import CatalogManifest, VERSION
from oabm.model import BuildingModel, ContractError, Point3, Pose, upgrade_to_v1_1


def record(identity, kind, fields):
    return {"product_id": identity, "manufacturer": "Synthetic Maker", "part_number": identity,
            "kind": kind, "description": "Synthetic " + identity,
            "geometry_basis": "dimension_derived_envelope" if fields.get("size") is not None else "metadata_only",
            "assets": [], "fields": fields,
            "evidence": [{"field": key, "source_id": "source-a", "derivation": "observed", "confidence": .8}
                         for key in ["product", *(k for k, v in fields.items() if v is not None)]]}


@pytest.fixture
def document():
    return {"version": VERSION, "catalog_id": "synthetic-catalog", "catalog_revision": "revision-a",
            "sources": [{"id": "source-a", "url": "https://example.org/synthetic.pdf", "title": "Synthetic test",
                         "revision": None, "locator": "page 1", "retrieved_at": "2026-01-01T00:00:00Z", "sha256": "a" * 64}],
            "entries": [
                record("box-a", "box", {"box_type": "device_box", "size": {"x": .3, "y": .12, "z": .5}, "listed_volume_m3": .01}),
                record("cable-a", "raceway", {"route_type": "cable", "wiring_method": "cable", "designation_kind": "listed_assembly",
                                              "designation": "synthetic 12/2 assembly", "nominal_diameter_m": None}),
                record("support-a", "support", {"category": "support", "component_type": "synthetic-clamp", "size": {"x": .1, "y": .2, "z": .3}}),
                record("connector-a", "reference", {"category": "synthetic connector", "designation": "1/2 trade"}),
            ]}


def encoded(doc):
    return json.dumps(doc).encode()


def test_existing_selector_types_and_explicit_identity(document):
    manifest = CatalogManifest(encoded(document))
    assert manifest.sha256 == hashlib.sha256(encoded(document)).hexdigest()
    box = manifest.selector_entry("box-a")
    assert box.size.x == .3 and box.listed_volume_m3 == .01
    assert box.product.catalog_revision == "revision-a"
    cable = manifest.selector_entry("cable-a")
    assert cable.route_type == "cable" and cable.designation_kind == "listed_assembly"
    assert cable.nominal_diameter_m is None
    assert manifest.selector_entry("support-a").category == "support"
    with pytest.raises(ContractError, match="reference-only"):
        manifest.selector_entry("connector-a")
    with pytest.raises(ContractError, match="unknown product"):
        manifest.selector_entry("not-here")


def test_search_does_not_substitute_or_mutate_catalog(document):
    manifest = CatalogManifest(encoded(document))
    assert len(manifest.search("synthetic", manufacturer="SYNTHETIC MAKER")) == 4
    assert [r["product_id"] for r in manifest.search("CABLE", kind="raceway")] == ["cable-a"]
    assert manifest.search("missing") == ()
    rows = manifest.search()
    rows[0]["fields"]["size"]["x"] = 99
    assert manifest.selector_entry("box-a").size.x == .3
    entry = manifest.selector_entry("box-a")
    entry.provenance[0].attributes["source"]["url"] = "changed"
    assert manifest.selector_entry("box-a").provenance[0].attributes["source"]["url"] != "changed"
    document["entries"].clear()
    assert len(manifest.search()) == 4


def test_permutation_normalization_and_raw_identity(document):
    first = CatalogManifest(encoded(document))
    document["entries"].reverse()
    for entry in document["entries"]:
        entry["evidence"].reverse()
    second = CatalogManifest(encoded(document))
    assert first.to_json() == second.to_json()
    assert first.sha256 != second.sha256  # exact input bytes still pinned
    assert CatalogManifest(first.to_json().encode()).to_json() == first.to_json()


def test_field_evidence_and_asset_do_not_claim_native_output(document):
    doc = document["entries"][0]
    doc["assets"] = [{"source_id": "source-a", "format": "STEP", "units": "in", "validation": "header_only",
                      "origin": "manufacturer_native_cad", "rights": "unknown"}]
    m = CatalogManifest(encoded(document))
    e = m.selector_entry("box-a")
    for p in e.provenance:
        assert len(p.scope_paths) == 1
        assert p.confidence == .8 and p.derivation == "observed"
        assert p.attributes["emitted_geometry_basis"] == "dimension_derived_envelope"
        assert p.attributes["source_authenticity_verified"] is False
        assert p.attributes["compatibility_verified"] is False
        assert p.attributes["manifest_sha256"] == m.sha256
    candidates = m.asset_candidates("box-a", format="STEP")
    assert len(candidates) == 1 and candidates[0]["source"]["sha256"] == "a" * 64
    assert candidates[0]["usage_permission_verified"] is False
    assert candidates[0]["acquisition_performed"] is False
    candidates[0]["source"]["url"] = "changed"
    assert m.asset_candidates("box-a")[0]["source"]["url"] != "changed"
    assert m.asset_candidates("box-a", format="IFC") == ()


def test_no_native_asset_is_not_replaced_by_reference_or_another_part(document):
    document["entries"][0]["assets"] = [{"source_id": "source-a", "format": "PDF", "units": "unknown",
        "validation": "uninspected", "origin": "manufacturer_reference", "rights": "unknown"}]
    manifest = CatalogManifest(encoded(document))
    assert manifest.asset_candidates("box-a") == ()
    assert manifest.asset_candidates("connector-a") == ()
    with pytest.raises(ContractError):
        manifest.asset_candidates("absent")
    with pytest.raises(ContractError):
        manifest.asset_candidates("box-a", format="PDF")


def test_actual_selector_consumes_loader_and_preserves_route(document):
    base = BuildingModel.load(Path(__file__).parents[1] / "fixtures/model/v1/garage-route.json")
    entry = CatalogManifest(encoded(document)).selector_entry("box-a")
    intent = BoxIntent(intent_id="catalog-test:box", actor_id="synthetic-reviewer",
                       occupant_ids=(base.electrical_devices[0].id,), product=entry.product,
                       host_id="wall:south", mounting="flush", pose=Pose(position=Point3(x=5.2, y=-.01, z=1.2)))
    model = select_electrical_boxes(upgrade_to_v1_1(base), intents=(intent,), catalog=(entry,))
    assert model.routes == base.routes
    assert model.electrical_boxes[0].product == entry.product
    assert select_electrical_boxes(model, intents=(intent,), catalog=(entry,)).to_json() == model.to_json()
    from oabm.quantities import extract_quantities
    assert any(item.category == "electrical_box" for item in extract_quantities(model).items)


@pytest.mark.parametrize("value", [False, True, 0, -1, 1e400, 10**400, "1", [], {}])
def test_invalid_dimensions_fail_closed(document, value):
    document["entries"][0]["fields"]["size"]["x"] = value
    with pytest.raises(ContractError):
        CatalogManifest(encoded(document))


@pytest.mark.parametrize("change", [
    lambda d: d.update(extra=1),
    lambda d: d.update(version="future"),
    lambda d: d["entries"].append(deepcopy(d["entries"][0])),
    lambda d: d["sources"].append(deepcopy(d["sources"][0])),
    lambda d: d["entries"][0]["evidence"].pop(),
    lambda d: d["entries"][0]["evidence"].append(deepcopy(d["entries"][0]["evidence"][0])),
    lambda d: d["entries"][0]["evidence"][0].update(source_id="absent"),
    lambda d: d["entries"][0]["evidence"][0].update(confidence=True),
    lambda d: d["entries"][0]["evidence"][0].update(confidence=10**400),
    lambda d: d["entries"][0].update(geometry_basis="manufacturer_native_cad"),
    lambda d: d["entries"][0]["fields"].update(listed_volume_m3=100),
    lambda d: d["entries"][1]["fields"].update(wiring_method="nm"),
    lambda d: d["entries"][1]["fields"].update(designation_kind="trade_size"),
    lambda d: d["sources"][0].update(url="file:///private/source.pdf"),
    lambda d: d["sources"][0].update(url="https://user:secret@example.org/source"),
    lambda d: d["sources"][0].update(retrieved_at="2026-02-30T00:00:00Z"),
    lambda d: d["sources"][0].update(sha256="no"),
])
def test_invalid_manifest_shapes_and_evidence(document, change):
    change(document)
    with pytest.raises(ContractError):
        CatalogManifest(encoded(document))


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"null", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'\xff'])
def test_malformed_bytes(raw):
    with pytest.raises(ContractError):
        CatalogManifest(raw)


def test_missing_dimensions_are_not_invented(document):
    entry = document["entries"][0]
    entry["fields"]["size"] = None
    entry["geometry_basis"] = "metadata_only"
    entry["evidence"] = [e for e in entry["evidence"] if e["field"] != "size"]
    typed = CatalogManifest(encoded(document)).selector_entry("box-a")
    assert typed.size is None  # Existing selector reports missing_catalog_size.


def test_revision_preserved_and_legacy_module_unmodified(document):
    m = CatalogManifest(encoded(document))
    document["catalog_revision"] = "revision-b"
    other = CatalogManifest(encoded(document))
    assert m.selector_entry("box-a").product != other.selector_entry("box-a").product
    assert m.to_json() != other.to_json()


def test_day_precision_is_preserved_without_inventing_time(document):
    document["sources"][0]["retrieved_at"] = "2026-01-01"
    entry = CatalogManifest(encoded(document)).selector_entry("cable-a")
    assert entry.provenance[0].attributes["source"]["retrieved_at"] == "2026-01-01"


@pytest.mark.parametrize('url', ['https://exa mple.org/x', 'https://example.org:bad/x',
    'https://example.org:999999/x', 'https://example.org:0/x', 'https://-bad.example/x',
    'https://example..org/x', 'https://999.999.999.999/x', 'https://example.org/%oops'])
def test_malformed_https_sources_rejected(document, url):
    document['sources'][0]['url'] = url
    with pytest.raises(ContractError):
        CatalogManifest(encoded(document))


@pytest.mark.parametrize('url', ['https://example.org:8443/x', 'https://[2001:db8::1]/x',
    'https://münchen.example/spec%20sheet.pdf', 'https://example.org./x'])
def test_supported_https_forms_remain_unchanged(document, url):
    document['sources'][0]['url'] = url
    entry = CatalogManifest(encoded(document)).selector_entry('box-a')
    assert entry.provenance[0].source_id == url


def test_cli_search_hash_pin_and_no_source_writes(document, tmp_path, capsys):
    from oabm.electrical.catalog_manifest import main
    path = tmp_path / "catalog.json"
    path.write_bytes(encoded(document))
    before = path.read_bytes(), path.stat().st_mtime_ns
    pin = hashlib.sha256(before[0]).hexdigest()
    assert main([str(path), "--query", "cable", "--expected-sha256", pin]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["manifest_sha256"] == pin and len(result["entries"]) == 1
    assert result["automatic_selection_performed"] is False
    with pytest.raises(SystemExit) as error:
        main([str(path), "--expected-sha256", "f" * 64])
    assert error.value.code == 2
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_box_selected_from_manifest_reaches_existing_exports(document, tmp_path):
    from oabm.ifc import to_ifc, from_ifc, round_trip_differences
    from oabm.exports.gltf import to_glb
    from oabm.drawings.generator import generate_schedules
    base = BuildingModel.load(Path(__file__).parents[1] / "fixtures/model/v1/garage-route.json")
    manifest = CatalogManifest(encoded(document))
    entry = manifest.selector_entry("box-a")
    intent = BoxIntent(intent_id="catalog-test:export", actor_id="synthetic-reviewer",
                       occupant_ids=(base.electrical_devices[0].id,), product=entry.product,
                       host_id="wall:south", mounting="flush", pose=Pose(position=Point3(x=5.2, y=-.01, z=1.2)))
    model = select_electrical_boxes(upgrade_to_v1_1(base), intents=(intent,), catalog=(entry,))
    assert round_trip_differences(model, from_ifc(to_ifc(model))) == ()
    to_glb(model, tmp_path / "catalog.glb")
    assert (tmp_path / "catalog.glb").stat().st_size > 0
    assert generate_schedules(model)
    assert manifest.sha256 in model.to_json()
