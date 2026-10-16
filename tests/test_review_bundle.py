from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.exports import to_glb
from oabm.exports.gltf import build_glb_bytes
from oabm.exports.review_bundle import _canonical_entity_index, _node_index, _parse_glb_document
from oabm.exports.review_bundle import (
    ReviewBundle,
    build_review_bundle,
    canonical_model_sha256,
)
from oabm.model import (
    BuildingModel,
    Level,
    Opening,
    Entity,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Provenance,
    Size3,
    Wall,
)
from oabm.qa import load_golden_cases, load_golden_model
from oabm.qa.confidence_review import ReviewConfig, build_confidence_review


ROOT = Path(__file__).resolve().parents[1]


def _synthetic_model() -> BuildingModel:
    source = Provenance(
        source_kind="synthetic_plan",
        source_id="plan-a",
        page=1,
        method="synthetic fixture",
        confidence=0.45,
        derivation="inferred",
        attributes={"fixture": True},
    )
    level = Level(id="level:review", elevation_m=0.0)
    wall = Wall(
        id="wall:review",
        level_id=level.id,
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0),
            Point3(x=2.0, y=0.0, z=0.0),
        )),
        thickness_m=0.1,
        height_m=2.5,
        confidence=0.95,
        provenance=(source,),
    )
    opening = Opening(
        id="opening:review",
        host_id=wall.id,
        opening_type="door",
        pose=Pose(position=Point3(x=1.0, y=0.0, z=1.0)),
        size=Size3(x=0.9, y=0.1, z=2.0),
    )
    return BuildingModel(
        model_id="model:review-bundle-synthetic",
        levels=(level,),
        walls=(wall,),
        openings=(opening,),
    )


def _artifacts(model: BuildingModel, tmp_path: Path):
    glb_path = tmp_path / "model.glb"
    to_glb(model, glb_path)
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.8))
    return glb_path.read_bytes(), manifest


def _build(
    model: BuildingModel, glb_bytes: bytes, manifest, *, source_references=None, glb_options=None,
):
    return build_review_bundle(
        model,
        glb_bytes=glb_bytes,
        model_reference="fixtures/synthetic/model.json",
        glb_reference="fixtures/synthetic/model.glb",
        confidence_manifest=manifest,
        confidence_reference="fixtures/synthetic/confidence-review.json",
        source_reference_mapping_reference="fixtures/synthetic/source-map.json",
        source_references=(
            {("synthetic_plan", "plan-a"): "fixtures/synthetic/plan-a.pdf"}
            if source_references is None else source_references
        ),
        glb_options=glb_options,
    )


def _rewrite_glb(payload: bytes, mutate) -> bytes:
    magic, version, _ = struct.unpack_from("<III", payload, 0)
    offset = 12
    chunks: list[tuple[int, bytes]] = []
    while offset < len(payload):
        length, kind = struct.unpack_from("<II", payload, offset)
        offset += 8
        chunks.append((kind, payload[offset:offset + length]))
        offset += length
    document = json.loads(chunks[0][1].decode("utf-8").rstrip(" \t\r\n\0"))
    mutate(document)
    json_chunk = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    encoded = bytearray(struct.pack("<III", magic, version, 0))
    encoded.extend(struct.pack("<II", len(json_chunk), chunks[0][0]))
    encoded.extend(json_chunk)
    for kind, chunk in chunks[1:]:
        encoded.extend(struct.pack("<II", len(chunk), kind))
        encoded.extend(chunk)
    struct.pack_into("<I", encoded, 8, len(encoded))
    return bytes(encoded)


def _mutate_glb_binary(payload: bytes) -> bytes:
    json_length, json_kind = struct.unpack_from("<II", payload, 12)
    assert json_kind == 0x4E4F534A
    bin_header = 20 + json_length
    bin_length, bin_kind = struct.unpack_from("<II", payload, bin_header)
    assert bin_kind == 0x004E4942 and bin_length > 0
    mutated = bytearray(payload)
    mutated[bin_header + 8] ^= 0x01
    return bytes(mutated)


def test_bundle_joins_current_review_and_canonical_source_confidence(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)

    decoded_model = BuildingModel.from_json(model.to_json(indent=None))
    assert decoded_model.to_json(indent=None) == model.to_json(indent=None)
    bundle = _build(model, glb_bytes, manifest).to_dict()
    assert bundle["kind"] == "oabm.review-bundle"
    assert bundle["version"] == "1.0.0"
    assert bundle["artifacts"]["canonical_model"]["sha256"] == canonical_model_sha256(model)
    assert bundle["artifacts"]["glb"]["sha256"] == hashlib.sha256(glb_bytes).hexdigest()
    assert bundle["artifacts"]["confidence_review"]["sha256"] == hashlib.sha256(
        manifest.to_json(indent=None).encode("utf-8")
    ).hexdigest()
    source_mapping_bytes = json.dumps(
        bundle["artifacts"]["source_reference_mapping"]["items"],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    assert bundle["artifacts"]["source_reference_mapping"]["sha256"] == hashlib.sha256(
        source_mapping_bytes
    ).hexdigest()

    by_id = {item["item_id"]: item for item in bundle["items"]}
    wall_item = by_id["review:entity:wall:review"]
    wall = wall_item["entities"][0]
    assert wall["canonical_id"] == "wall:review"
    assert wall["confidence"] == 0.95
    assert wall["rendered_node_indices"] == [0]
    assert wall["provenance"][0]["confidence"] == 0.45
    assert wall["provenance"][0]["source_reference"] == "fixtures/synthetic/plan-a.pdf"
    assert wall_item["review"]["provenance"][0]["confidence"] == 0.45

    # Opening and level have canonical review items even though the GLB does
    # not render them. Their canonical IDs are explicit and their node lists
    # are explicitly empty rather than inferred from names.
    opening = by_id["review:entity:opening:review"]["entities"][0]
    level = by_id["review:entity:level:review"]["entities"][0]
    assert opening["rendered_node_indices"] == []
    assert level["rendered_node_indices"] == []


def test_bundle_json_roundtrip_and_identity_are_deterministic(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)

    first = _build(model, glb_bytes, manifest)
    second = _build(model, glb_bytes, manifest)
    assert first.to_json() == second.to_json()
    decoded = ReviewBundle.from_dict(first.to_dict())
    assert decoded.to_json() == first.to_json()
    malformed = first.to_dict()
    malformed["artifacts"]["source_reference_mapping"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 does not match"):
        ReviewBundle.from_dict(malformed)

    changed_refs = build_review_bundle(
        model,
        glb_bytes=glb_bytes,
        model_reference="fixtures/synthetic/other-model.json",
        glb_reference="fixtures/synthetic/model.glb",
        confidence_manifest=manifest,
        confidence_reference="fixtures/synthetic/confidence-review.json",
        source_reference_mapping_reference="fixtures/synthetic/source-map.json",
        source_references={("synthetic_plan", "plan-a"): "fixtures/synthetic/plan-a.pdf"},
    )
    assert changed_refs.to_json() != first.to_json()



def test_bundle_accepts_one_to_many_conductor_wire_nodes(tmp_path: Path) -> None:
    golden_root = ROOT / "fixtures" / "golden" / "v1"
    case = next(case for case in load_golden_cases(golden_root) if case.name == "synthetic-garage")
    model = load_golden_model(case)
    model = replace(
        model,
        conductors=(replace(model.conductors[0], count=2), *model.conductors[1:]),
    )
    glb_path = tmp_path / "garage.glb"
    to_glb(model, glb_path)
    glb_bytes = glb_path.read_bytes()
    json_length, _ = struct.unpack_from("<II", glb_bytes, 12)
    gltf = json.loads(glb_bytes[20:20 + json_length])
    wires = [
        node for node in gltf["nodes"]
        if node.get("name", "").startswith("conductor:")
        and "#route:" in node.get("name", "")
    ]
    wire_ids = [node["extras"]["canonical_id"] for node in wires]
    assert wires and len(wire_ids) > len(set(wire_ids))

    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.95))
    entities = {
        entity.id: entity
        for field_name in model.__dataclass_fields__
        if isinstance(getattr(model, field_name), tuple)
        for entity in getattr(model, field_name)
        if isinstance(entity, Entity)
    }
    used_sources = {
        (record.source_kind, record.source_id)
        for item in manifest.items
        for entity_id in item.entity_ids
        for record in entities[entity_id].provenance
    }
    source_references = {
        pair: f"fixtures/synthetic/source-{index}.pdf"
        for index, pair in enumerate(sorted(used_sources))
    }
    bundle = build_review_bundle(
        model,
        glb_bytes=glb_bytes,
        model_reference="fixtures/synthetic/garage.json",
        glb_reference="fixtures/synthetic/garage.glb",
        confidence_manifest=manifest,
        confidence_reference="fixtures/synthetic/garage-review.json",
        source_reference_mapping_reference="fixtures/synthetic/garage-source-map.json",
        source_references=source_references,
    )
    assert ReviewBundle.from_dict(bundle.to_dict()).to_json() == bundle.to_json()

def test_rejects_glb_from_a_different_canonical_snapshot(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)
    changed = replace(model, walls=(replace(model.walls[0], thickness_m=0.2),))
    changed_manifest = build_confidence_review(changed, ReviewConfig(confidence_threshold=0.8))

    with pytest.raises(ValueError, match="canonical_model_sha256"):
        _build(changed, glb_bytes, changed_manifest)


def test_rejects_a_stale_confidence_review_manifest(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)
    stale_document = manifest.to_dict()
    stale_document["items"] = []

    with pytest.raises(ValueError, match="stale or mismatched"):
        _build(model, glb_bytes, stale_document)



def test_build_glb_bytes_matches_exporter_and_explicit_display_reexport(tmp_path: Path) -> None:
    model = _synthetic_model()
    canonical_identity = canonical_model_sha256(model)
    default_bytes = build_glb_bytes(model)
    path = tmp_path / "from-to-glb.glb"
    to_glb(model, path)
    assert default_bytes == path.read_bytes()

    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.8))
    display_bytes = build_glb_bytes(model, min_display_wall_thickness_m=0.2)
    assert display_bytes != default_bytes
    assert canonical_model_sha256(model) == canonical_identity
    bundle = _build(
        model, display_bytes, manifest,
        glb_options={"min_display_wall_thickness_m": 0.2},
    ).to_dict()
    assert bundle["artifacts"]["canonical_model"]["sha256"] == canonical_identity
    assert bundle["artifacts"]["glb"]["export_options"]["min_display_wall_thickness_m"] == 0.2


def test_rejects_removed_rendered_node_swapped_ids_and_changed_geometry(tmp_path: Path) -> None:
    model = _synthetic_model()
    second_wall = replace(
        model.walls[0],
        id="wall:review-2",
        centerline=Polyline3D(points=(Point3(x=0.0, y=1.0, z=0.0), Point3(x=2.0, y=1.0, z=0.0))),
    )
    model = replace(model, walls=(model.walls[0], second_wall))
    glb_bytes, manifest = _artifacts(model, tmp_path)

    def remove_expected_wall(document):
        document["nodes"] = [
            node for node in document["nodes"]
            if node.get("extras", {}).get("canonical_id") != "wall:review"
        ]

    def swap_wall_ids(document):
        first = next(node for node in document["nodes"] if node.get("extras", {}).get("canonical_id") == "wall:review")
        second = next(node for node in document["nodes"] if node.get("extras", {}).get("canonical_id") == "wall:review-2")
        first["extras"]["canonical_id"], second["extras"]["canonical_id"] = (
            second["extras"]["canonical_id"], first["extras"]["canonical_id"],
        )

    with pytest.raises(ValueError, match="canonical node coverage"):
        _build(model, _rewrite_glb(glb_bytes, remove_expected_wall), manifest)
    for forged in (
        _rewrite_glb(glb_bytes, swap_wall_ids),
        _mutate_glb_binary(glb_bytes),
    ):
        with pytest.raises(ValueError, match="trusted canonical export"):
            _build(model, forged, manifest)


def test_wire_route_must_be_owned_by_its_canonical_conductor(tmp_path: Path) -> None:
    golden_root = ROOT / "fixtures" / "golden" / "v1"
    case = next(case for case in load_golden_cases(golden_root) if case.name == "synthetic-garage")
    rendered_model = load_golden_model(case)
    path = tmp_path / "garage-route-ownership.glb"
    to_glb(rendered_model, path)
    document = _parse_glb_document(path.read_bytes())

    conductor = rendered_model.conductors[0]
    detached_model = replace(
        rendered_model,
        conductors=(replace(conductor, route_ids=()), *rendered_model.conductors[1:]),
    )
    entities, _ = _canonical_entity_index(detached_model)
    with pytest.raises(ValueError, match="route it does not carry"):
        _node_index(document, entities)


def test_rejects_missing_unknown_and_duplicate_glb_canonical_ids(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)

    def missing(document):
        node = next(node for node in document["nodes"] if node.get("name") == "wall:review")
        del node["extras"]

    def unknown(document):
        node = next(node for node in document["nodes"] if node.get("name") == "wall:review")
        node["extras"]["canonical_id"] = "wall:not-in-model"

    def duplicate(document):
        node = next(node for node in document["nodes"] if node.get("name") == "wall:review")
        document["nodes"].append(dict(node, name="wall:duplicate-visual"))

    with pytest.raises(ValueError, match="missing extras.canonical_id"):
        _build(model, _rewrite_glb(glb_bytes, missing), manifest)
    with pytest.raises(ValueError, match="unknown canonical_id"):
        _build(model, _rewrite_glb(glb_bytes, unknown), manifest)
    with pytest.raises(ValueError, match="duplicate GLB canonical_id"):
        _build(model, _rewrite_glb(glb_bytes, duplicate), manifest)



def test_rejects_self_asserted_glb_review_summaries(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)

    def misleading_summary(document):
        node = next(node for node in document["nodes"] if node.get("name") == "wall:review")
        node["extras"]["confidence"] = 0.01
        node["extras"]["provenance"] = [{
            "source_kind": "synthetic_plan",
            "source_id": "plan-a",
            "confidence": 0.01,
        }]

    with pytest.raises(ValueError, match="trusted canonical export"):
        _build(model, _rewrite_glb(glb_bytes, misleading_summary), manifest)


def test_rejects_unresolved_or_unused_source_references(tmp_path: Path) -> None:
    model = _synthetic_model()
    glb_bytes, manifest = _artifacts(model, tmp_path)

    with pytest.raises(ValueError, match="unresolved canonical source references"):
        build_review_bundle(
            model,
            glb_bytes=glb_bytes,
            model_reference="fixtures/synthetic/model.json",
            glb_reference="fixtures/synthetic/model.glb",
            confidence_manifest=manifest,
            confidence_reference="fixtures/synthetic/confidence-review.json",
            source_reference_mapping_reference="fixtures/synthetic/source-map.json",
            source_references={},
        )
    with pytest.raises(ValueError, match="unused entries"):
        build_review_bundle(
            model,
            glb_bytes=glb_bytes,
            model_reference="fixtures/synthetic/model.json",
            glb_reference="fixtures/synthetic/model.glb",
            confidence_manifest=manifest,
            confidence_reference="fixtures/synthetic/confidence-review.json",
            source_reference_mapping_reference="fixtures/synthetic/source-map.json",
            source_references={
                ("synthetic_plan", "plan-a"): "fixtures/synthetic/plan-a.pdf",
                ("synthetic_plan", "unused"): "fixtures/synthetic/unused.pdf",
            },
        )


def test_rejects_duplicate_canonical_ids_across_entity_collections(tmp_path: Path) -> None:
    model = _synthetic_model()
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.8))
    duplicate_opening = replace(model.openings[0], id=model.walls[0].id)
    object.__setattr__(model, "openings", (duplicate_opening,))
    glb_path = tmp_path / "duplicate-model.glb"
    to_glb(model, glb_path)

    with pytest.raises(ValueError, match="duplicate canonical entity id"):
        _build(model, glb_path.read_bytes(), manifest)

def _artifacts_at_threshold(model: BuildingModel, tmp_path: Path, config: ReviewConfig):
    glb_path = tmp_path / "model-at-threshold.glb"
    to_glb(model, glb_path)
    manifest = build_confidence_review(model, config)
    return glb_path.read_bytes(), manifest


def test_rejects_missing_model_entity_id_even_if_unrendered_or_reviewed(tmp_path: Path) -> None:
    model = _synthetic_model()
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.8))
    wall = model.walls[0]
    object.__setattr__(wall, "id", "")
    glb_path = tmp_path / "missing-id-model.glb"
    to_glb(model, glb_path)

    with pytest.raises(ValueError, match="missing id"):
        _build(model, glb_path.read_bytes(), manifest)
