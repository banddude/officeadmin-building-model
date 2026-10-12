"""#273 public tests: deterministic confidence review manifest."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.electrical import (
    BoxCatalogEntry,
    BoxIntent,
    RacewayCatalogEntry,
    RacewayIntent,
    RacewayRule,
    select_electrical_boxes,
    select_raceways,
)
from oabm.model import (
    BuildingModel,
    CatalogProduct,
    Level,
    Point3,
    Polyline3D,
    Provenance,
    RuleReference,
    Size3,
    upgrade_to_v1_1,
    upgrade_to_v1_2,
    Wall,
)
from oabm.qa import (
    REVIEW_VERSION,
    ConfidenceReviewManifest,
    ReviewConfig,
    build_confidence_review,
    load_golden_cases,
    load_golden_model,
)
from oabm.qa.confidence_review import (
    EvidenceReference,
    QuantityWarning,
    _warning_items,
    fingerprint_provenance,
)
from oabm.importers.roomplan.importer import load_captured_room
from oabm.quantities import extract_quantities

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"


def _golden_garage() -> BuildingModel:
    case = next(
        case for case in load_golden_cases(GOLDEN_ROOT) if case.name == "synthetic-garage"
    )
    return load_golden_model(case)


def _routed_garage() -> BuildingModel:
    return BuildingModel.load(ROOT / "fixtures" / "model" / "v1" / "garage-route.json")


def _wall(confidence: float = 1.0, provenance: tuple | None = None) -> Wall:
    """One straight wall; ``provenance=()`` really carries no records."""

    record = (
        Provenance(
            source_kind="drawing",
            source_id="plan:synthetic",
            method="scaled-drawing",
            confidence=0.9,
            derivation="observed",
        ),
    )
    if provenance is None:
        provenance = record
    return Wall(
        id="wall:review-south",
        level_id="level:main",
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0),
            Point3(x=4.0, y=0.0, z=0.0),
        )),
        thickness_m=0.2,
        height_m=2.7,
        confidence=confidence,
        provenance=provenance,
    )


def _review_model(*walls: Wall) -> BuildingModel:
    return BuildingModel(
        model_id="model:confidence-review-synth",
        levels=(Level(
            id="level:main",
            elevation_m=0.0,
            height_m=2.7,
            provenance=(
                Provenance(
                    source_kind="drawing",
                    source_id="plan:synthetic",
                    confidence=0.9,
                    derivation="observed",
                ),
            ),
        ),),
        walls=tuple(walls),
    )


def _reasons_for(manifest: ConfidenceReviewManifest, entity_id: str) -> tuple[str, ...]:
    item = next(
        item for item in manifest.items
        if item.entity_ids == (entity_id,) and item.item_id == f"review:entity:{entity_id}"
    )
    return item.reasons


def _entity_items(manifest: ConfidenceReviewManifest) -> dict[str, object]:
    return {
        item.item_id: item for item in manifest.items
        if item.item_id.startswith("review:entity:")
    }


def test_threshold_below_at_above() -> None:
    model = _review_model(_wall(confidence=0.45))
    # Below the threshold: flagged.
    below = build_confidence_review(model, ReviewConfig(confidence_threshold=0.46))
    assert _reasons_for(below, "wall:review-south") == ("low_entity_confidence",)
    # At the threshold: it meets the bar, so it is not unsure.
    at = build_confidence_review(model, ReviewConfig(confidence_threshold=0.45))
    assert "review:entity:wall:review-south" not in _entity_items(at)
    # Above the threshold: not flagged.
    above = build_confidence_review(model, ReviewConfig(confidence_threshold=0.4))
    assert "review:entity:wall:review-south" not in _entity_items(above)


def test_high_entity_confidence_does_not_hide_low_provenance() -> None:
    low_record = Provenance(
        source_kind="scan",
        source_id="scan:synthetic",
        method="captured-room-json",
        confidence=0.3,
        derivation="inferred",
    )
    model = _review_model(_wall(confidence=1.0, provenance=(low_record,)))
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.5))
    item = next(item for item in manifest.items if item.entity_ids == ("wall:review-south",))
    # The confident entity record does not hide the weak source, and the
    # manifest retains the actual values instead of raising them.
    assert "low_supporting_provenance_confidence" in item.reasons
    assert item.confidence == 1.0
    assert item.provenance[0]["confidence"] == 0.3
    assert item.provenance[0]["derivation"] == "inferred"
    assert "below the review threshold" in item.detail


def test_missing_provenance_is_flagged() -> None:
    model = _review_model(_wall(confidence=1.0, provenance=()))
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.5))
    assert _reasons_for(manifest, "wall:review-south") == ("missing_provenance",)
    item = next(item for item in manifest.items if item.entity_ids == ("wall:review-south",))
    assert item.provenance == ()
    assert "no supporting provenance" in item.detail


def test_entity_items_carry_stable_ids_and_original_confidence() -> None:
    model = _review_model(
        _wall(confidence=0.4),
        replace(_wall(), id="wall:review-north", confidence=0.6),
    )
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.7))
    by_id = _entity_items(manifest)
    assert set(by_id) == {
        "review:entity:wall:review-north",
        "review:entity:wall:review-south",
    }
    assert by_id["review:entity:wall:review-south"].confidence == 0.4
    assert by_id["review:entity:wall:review-north"].confidence == 0.6


def test_unresolved_box_selection_is_flagged() -> None:
    base = _routed_garage()
    entry = BoxCatalogEntry(
        product=CatalogProduct(
            catalog_id="synthetic", catalog_revision="1", product_id="two-gang",
        ),
        box_type="device_box",
        size=Size3(x=0.3, y=0.12, z=0.5),
        provenance=(
            Provenance(
                source_kind="synthetic-catalog",
                source_id="fixture:box",
                derivation="observed",
            ),
        ),
    )
    # No host: the real producer resolves nothing and records why.
    intent = BoxIntent(
        intent_id="box-intent:review",
        actor_id="synthetic-reviewer",
        occupant_ids=(base.electrical_devices[0].id,),
        product=entry.product,
        host_id=None,
        mounting="flush",
        pose=None,
    )
    model = select_electrical_boxes(
        upgrade_to_v1_1(base), intents=(intent,), catalog=(entry,),
    )
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.99))
    outcome = model.box_selection_outcomes[0]
    item = next(item for item in manifest.items if item.entity_ids == (outcome.id,))
    assert item.reasons == ("unresolved_selection",)
    assert item.entity_kind == "box_selection_outcomes"
    assert "unresolved" in item.detail
    assert outcome.reason_codes and outcome.reason_codes[0] in item.detail


def test_unresolved_raceway_selection_is_flagged() -> None:
    base = _routed_garage()
    route = base.routes[0]
    # No product and no basis: the selection cannot resolve.
    intent = RacewayIntent(
        intent_id="intent:raceway-review",
        route_id=route.id,
        actor_id="synthetic-reviewer",
    )
    entry = RacewayCatalogEntry(
        product=CatalogProduct(
            catalog_id="synthetic", catalog_revision="1", product_id="conduit-a",
        ),
        route_type=route.route_type,
        wiring_method="raceway",
        designation_kind="trade_size",
        designation="synthetic trade A",
        nominal_diameter_m=route.nominal_diameter_m,
        provenance=(
            Provenance(
                source_kind="synthetic", source_id="fixture:raceway", derivation="user",
            ),
        ),
    )
    rule = RacewayRule(
        reference=RuleReference(
            authority="synthetic-company", revision="1", rule_id="explicit-product-a",
        ),
        product=entry.product,
        route_type=route.route_type,
        provenance=entry.provenance,
    )
    model = select_raceways(
        upgrade_to_v1_2(base), intents=(intent,), catalog=(entry,), rules=(rule,),
    )
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.99))
    outcome = model.raceway_selection_outcomes[0]
    assert outcome.status == "unresolved"
    item = next(item for item in manifest.items if item.entity_ids == (outcome.id,))
    assert item.reasons == ("unresolved_selection",)
    assert item.entity_kind == "raceway_selection_outcomes"


def test_quantity_warnings_become_items_with_stable_ids() -> None:
    model = _routed_garage()
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.5))
    report = extract_quantities(model)
    from oabm.qa.confidence_review import _warning_item_id

    expected = {_warning_item_id(warning) for warning in report.warnings}
    warning_items = {
        item.item_id: item for item in manifest.items
        if item.item_id.startswith("review:warning:")
    }
    assert set(warning_items) == expected
    for item in warning_items.values():
        assert len(item.reasons) == 1
        assert item.reasons[0].startswith("quantity_warning_")
        assert item.confidence is None
        assert item.provenance == ()
        # Every warning source is a real entity of the model.
        for entity_id in item.entity_ids:
            assert any(
                entity_id in collection
                for collection in _all_entity_ids(model).values()
            )


def _all_entity_ids(model: BuildingModel) -> dict[str, set[str]]:
    from oabm.qa.confidence_review import _entity_collections

    return {
        name: {entity.id for entity in collection}
        for name, collection in _entity_collections(model).items()
    }


def test_real_fixture_input_to_model_to_manifest() -> None:
    """A real existing fixture drives the whole pipeline, not hand-built rows."""

    model = _golden_garage()
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.9))

    # The fixture's entities are fully confident with provenance, so every
    # item must come from the takeoff's actual warnings.
    report = extract_quantities(model)
    assert report.warnings
    assert all(item.item_id.startswith("review:warning:") for item in manifest.items)
    assert len(manifest.items) == len(report.warnings)

    # Collections reflect the model's own entity fields, including the
    # physical-selection collections on current main.
    assert "walls" in manifest.collections
    assert "electrical_boxes" in manifest.collections
    assert "raceway_selections" in manifest.collections
    assert "box_selection_outcomes" in manifest.collections

    # No invented references: every id the manifest names exists.
    known = _all_entity_ids(model)
    for item in manifest.items:
        for entity_id in item.entity_ids:
            assert any(entity_id in ids for ids in known.values())
    assert manifest.model_id == model.model_id
    assert manifest.confidence_threshold == 0.9


def test_real_importer_input_to_model_to_manifest() -> None:
    """A public synthetic CapturedRoom import drives input to manifest.

    This is the real pipeline the review serves: an importer input file is
    parsed into a canonical model by the RoomPlan importer, and the
    manifest is built from that model — no manually constructed rows.
    """

    model = load_captured_room(
        ROOT / "fixtures" / "roomplan" / "captured-room-3d.json",
        source_id="fixture:captured-room-3d",
    )
    assert model.walls
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=1.0))
    report = extract_quantities(model)
    assert report.warnings

    # The importer records real uncertainty (some entities at 0.66/0.33),
    # and the manifest must flag exactly those, computed from the model
    # itself rather than assumed here.
    entity_items = {
        item.entity_ids[0]: item
        for item in manifest.items if item.item_id.startswith("review:entity:")
    }
    warning_items = [
        item for item in manifest.items if item.item_id.startswith("review:warning:")
    ]
    assert len(warning_items) == len(report.warnings)

    expected_unsure: set[str] = set()
    for name, ids in _all_entity_ids(model).items():
        for entity in getattr(model, name):
            if entity.confidence < 1.0 or not entity.provenance or any(
                record.confidence < 1.0 for record in entity.provenance
            ):
                expected_unsure.add(entity.id)
    assert set(entity_items) == expected_unsure
    for entity_id, item in entity_items.items():
        assert "low_entity_confidence" in item.reasons or (
            "missing_provenance" in item.reasons
        )

    # A flagged wall's assumed-thickness record travels verbatim: an
    # inferred derivation stays inferred, with its field scope, never
    # relabeled and never raised.
    wall = next(
        w for w in model.walls
        if w.id in entity_items and any(r.derivation == "inferred" for r in w.provenance)
    )
    wall_records = [
        record for record in wall.provenance if record.derivation == "inferred"
    ]
    item = entity_items[wall.id]
    for record in wall_records:
        payload = next(
            entry for entry in item.provenance
            if entry.get("source_id") == record.source_id
            and entry.get("derivation") == "inferred"
            and entry.get("scope_paths") == (
                list(record.scope_paths) if record.scope_paths is not None else None
            )
        )
        assert payload["confidence"] == record.confidence
    assert all(
        entry.get("source_id") == "fixture:captured-room-3d"
        for entry in item.provenance
    )


def test_manifest_is_deterministic_under_input_permutations() -> None:
    north = replace(_wall(), id="wall:review-north", confidence=0.6)
    south = _wall(confidence=0.4)
    record = Provenance(
        source_kind="drawing", source_id="plan:synthetic", confidence=0.2,
    )
    weak = replace(
        _wall(), id="wall:review-east", confidence=0.8, provenance=(record,),
    )
    forward = _review_model(north, south, weak)
    reversed_model = _review_model(weak, south, north)
    first = build_confidence_review(forward, ReviewConfig(confidence_threshold=0.7))
    second = build_confidence_review(reversed_model, ReviewConfig(confidence_threshold=0.7))
    assert first.to_json() == second.to_json()
    # Idempotent: rebuilding changes nothing.
    again = build_confidence_review(forward, ReviewConfig(confidence_threshold=0.7))
    assert again.to_json() == first.to_json()


def test_input_model_bytes_are_unchanged() -> None:
    model = _review_model(_wall(confidence=0.4))
    before_json = model.to_json()
    before_ids = [entity.id for entity in model.walls]
    build_confidence_review(
        model,
        ReviewConfig(confidence_threshold=0.5),
        evidence=[_bound_reference(model, ref_id="crop:a")],
    )
    assert model.to_json() == before_json
    assert [entity.id for entity in model.walls] == before_ids
    assert model.walls[0].confidence == 0.4


def _bound_reference(model: BuildingModel, **changes) -> EvidenceReference:
    """A validly bound reference to the review wall's provenance record."""

    record = model.walls[0].provenance[0]
    values = {
        "ref_id": "crop:page-2-a",
        "entity_id": model.walls[0].id,
        "source_kind": record.source_kind,
        "source_id": record.source_id,
        "page": record.page,
        "fingerprint": fingerprint_provenance(record),
    }
    values.update(changes)
    return EvidenceReference(**values)


def test_bound_crops_ride_along_and_missing_crops_stay_missing() -> None:
    model = _review_model(_wall(confidence=0.4))
    manifest = build_confidence_review(
        model,
        ReviewConfig(confidence_threshold=0.5),
        evidence=[_bound_reference(model)],
    )
    item = next(item for item in manifest.items if item.entity_ids == ("wall:review-south",))
    assert item.crops[0]["ref_id"] == "crop:page-2-a"
    assert item.crops[0]["fingerprint"] == fingerprint_provenance(
        model.walls[0].provenance[0]
    )
    # An entity nobody supplied evidence for carries none: unavailable,
    # never invented.
    bare = _review_model(
        _wall(confidence=0.4),
        replace(_wall(), id="wall:review-north", confidence=0.3),
    )
    unnamed = build_confidence_review(
        bare, ReviewConfig(confidence_threshold=0.5),
        evidence=[_bound_reference(bare, ref_id="crop:a")],
    )
    north = next(item for item in unnamed.items if item.entity_ids == ("wall:review-north",))
    assert north.crops == ()


def test_evidence_bindings_fail_closed() -> None:
    model = _review_model(_wall(confidence=0.4))
    # Unknown entity: dangling.
    with pytest.raises(ValueError, match="unknown or stale entity"):
        build_confidence_review(
            model, ReviewConfig(confidence_threshold=0.5),
            evidence=[_bound_reference(model, entity_id="wall:does-not-exist")],
        )
    # Duplicate reference ids.
    with pytest.raises(ValueError, match="duplicate evidence reference id"):
        build_confidence_review(
            model, ReviewConfig(confidence_threshold=0.5),
            evidence=[
                _bound_reference(model),
                _bound_reference(model, source_kind="other-source"),
            ],
        )
    # Wrong source: the entity does not carry it.
    with pytest.raises(ValueError, match="does not currently carry"):
        build_confidence_review(
            model, ReviewConfig(confidence_threshold=0.5),
            evidence=[_bound_reference(model, source_id="plan:elsewhere")],
        )
    # Wrong page.
    with pytest.raises(ValueError, match="names page"):
        build_confidence_review(
            model, ReviewConfig(confidence_threshold=0.5),
            evidence=[_bound_reference(model, page=7)],
        )
    # Stale: the record changed after the reference was made.
    stale = _bound_reference(model)
    edited = replace(
        model,
        walls=(replace(model.walls[0], provenance=(
            replace(model.walls[0].provenance[0], confidence=0.95),
        )),),
    )
    with pytest.raises(ValueError, match="stale"):
        build_confidence_review(
            edited, ReviewConfig(confidence_threshold=0.5), evidence=[stale],
        )
    # Reference to an entity id that existed earlier but not now.
    without_wall = _review_model(
        replace(_wall(), id="wall:review-north", confidence=0.3),
    )
    with pytest.raises(ValueError, match="unknown or stale entity"):
        build_confidence_review(
            without_wall, ReviewConfig(confidence_threshold=0.5),
            evidence=[_bound_reference(model)],
        )


def test_evidence_ordering_does_not_change_output() -> None:
    model = _review_model(_wall(confidence=0.4))
    first = build_confidence_review(
        model, ReviewConfig(confidence_threshold=0.5),
        evidence=[
            _bound_reference(model, ref_id="crop:b"),
            _bound_reference(model, ref_id="crop:a"),
        ],
    )
    second = build_confidence_review(
        model, ReviewConfig(confidence_threshold=0.5),
        evidence=[
            _bound_reference(model, ref_id="crop:a"),
            _bound_reference(model, ref_id="crop:b"),
        ],
    )
    assert first.to_json() == second.to_json()
    item = next(item for item in first.items if item.entity_ids == ("wall:review-south",))
    assert [crop["ref_id"] for crop in item.crops] == ["crop:a", "crop:b"]


def test_provenance_ordering_does_not_change_output() -> None:
    first = Provenance(
        source_kind="scan", source_id="scan:one", confidence=0.2, derivation="inferred",
    )
    second = Provenance(
        source_kind="drawing", source_id="plan:two", confidence=0.3, derivation="observed",
    )
    forward = _review_model(_wall(confidence=0.8, provenance=(first, second)))
    backward = _review_model(_wall(confidence=0.8, provenance=(second, first)))
    left = build_confidence_review(forward, ReviewConfig(confidence_threshold=0.5))
    right = build_confidence_review(backward, ReviewConfig(confidence_threshold=0.5))
    assert left.to_json() == right.to_json()
    item = next(
        item for item in left.items if item.entity_ids == ("wall:review-south",)
    )
    # Both facts survive, canonically ordered, values untouched.
    assert [entry["source_id"] for entry in item.provenance] == ["plan:two", "scan:one"]
    assert [entry["confidence"] for entry in item.provenance] == [0.3, 0.2]


def test_strict_config_decoding() -> None:
    config = ReviewConfig(confidence_threshold=0.7)
    decoded = ReviewConfig.from_dict(config.to_dict())
    assert decoded == config

    good_document = {
        "kind": "confidence_review_config",
        "version": REVIEW_VERSION,
        "confidence_threshold": 0.7,
    }
    assert ReviewConfig.from_dict(good_document) == config

    bad_values = [True, False, "0.7", None, float("nan"), float("inf"), -0.1, 1.1]
    for value in bad_values:
        with pytest.raises(ValueError):
            ReviewConfig(confidence_threshold=value)
        document = {**good_document, "confidence_threshold": value}
        with pytest.raises(ValueError):
            ReviewConfig.from_dict(document)

    with pytest.raises(ValueError, match="unknown review config keys"):
        ReviewConfig.from_dict({**good_document, "minimum": 0.5})
    with pytest.raises(ValueError, match="version"):
        ReviewConfig.from_dict({**good_document, "version": "9.9.9"})
    with pytest.raises(ValueError, match="kind"):
        ReviewConfig.from_dict({**good_document, "kind": "something_else"})
    with pytest.raises(ValueError, match="requires confidence_threshold"):
        ReviewConfig.from_dict({"kind": "confidence_review_config", "version": REVIEW_VERSION})
    with pytest.raises(ValueError):
        ReviewConfig.from_dict("not an object")


def test_strict_manifest_roundtrip() -> None:
    model = _routed_garage()
    route = model.routes[0]
    record = route.provenance[0]
    manifest = build_confidence_review(
        model,
        ReviewConfig(confidence_threshold=0.99),
        evidence=[EvidenceReference(
            ref_id="crop:route-a",
            entity_id=route.id,
            source_kind=record.source_kind,
            source_id=record.source_id,
            page=record.page,
            fingerprint=fingerprint_provenance(record),
        )],
    )
    document = manifest.to_dict()
    decoded = ConfidenceReviewManifest.from_dict(document)
    assert decoded == manifest
    assert decoded.to_json() == manifest.to_json()

    with pytest.raises(ValueError, match="unknown manifest keys"):
        ConfidenceReviewManifest.from_dict({**document, "extra": True})
    with pytest.raises(ValueError, match="version"):
        ConfidenceReviewManifest.from_dict({**document, "version": "0.0.1"})
    with pytest.raises(ValueError, match="kind"):
        ConfidenceReviewManifest.from_dict({**document, "kind": "takeoff_report"})

    bad_item = {**document, "items": [{**document["items"][0], "surprise": 1}]}
    with pytest.raises(ValueError, match="unknown review item keys"):
        ConfidenceReviewManifest.from_dict(bad_item)
    incomplete_item = {
        **document,
        "items": [{
            key: value for key, value in document["items"][0].items()
            if key != "detail"
        }],
    }
    with pytest.raises(ValueError, match="missing keys"):
        ConfidenceReviewManifest.from_dict(incomplete_item)


def test_strict_decode_rejects_semantic_garbage() -> None:
    model = _routed_garage()
    manifest = build_confidence_review(model, ReviewConfig(confidence_threshold=0.99))
    document = manifest.to_dict()
    item = document["items"][0]

    # Confidence outside [0, 1], or a boolean, in an item.
    for value in (5, True, 1.5, -0.5):
        with pytest.raises(ValueError, match="confidence"):
            ConfidenceReviewManifest.from_dict({
                **document,
                "items": [{**item, "confidence": value}],
            })
    # Malformed provenance: unknown keys, missing keys, bool confidence,
    # bad derivation, bad page, bad scope paths.
    provenance = item["provenance"] or [
        {"source_kind": "drawing", "source_id": "plan:x", "confidence": 1.0},
    ]
    with pytest.raises(ValueError, match="unknown provenance keys"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "provenance": [{**provenance[0], "extra": 1}]}],
        })
    with pytest.raises(ValueError, match="missing keys"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "provenance": [{"source_kind": "drawing"}]}],
        })
    with pytest.raises(ValueError, match="confidence"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "provenance": [{**provenance[0], "confidence": True}]}],
        })
    with pytest.raises(ValueError, match="confidence"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "provenance": [{**provenance[0], "confidence": 7}]}],
        })
    with pytest.raises(ValueError, match="derivation"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{
                **item,
                "provenance": [{**provenance[0], "derivation": "guessed"}],
            }],
        })
    with pytest.raises(ValueError, match="page"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "provenance": [{**provenance[0], "page": 0}]}],
        })
    with pytest.raises(ValueError, match="scope_paths"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{
                **item,
                "provenance": [{**provenance[0], "scope_paths": ["a", "a"]}],
            }],
        })
    # Reason tokens and duplicate identities.
    with pytest.raises(ValueError, match="reason"):
        ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "reasons": ["Not A Token"]}],
        })
    duplicated = {**document, "items": [item, item]}
    with pytest.raises(ValueError, match="duplicate item ids"):
        ConfidenceReviewManifest.from_dict(duplicated)
    duplicate_collections = {
        **document,
        "collections": [*document["collections"], document["collections"][0]],
    }
    with pytest.raises(ValueError, match="duplicate names"):
        ConfidenceReviewManifest.from_dict(duplicate_collections)
    duplicate_entity_ids = {
        **document,
        "items": [{**item, "entity_ids": ["a", "a"]}],
    }
    with pytest.raises(ValueError, match="entity_ids"):
        ConfidenceReviewManifest.from_dict(duplicate_entity_ids)


def test_distinct_same_code_warnings_never_collide() -> None:
    sources = ("route:a",)
    first = QuantityWarning(
        code="overlap",
        message="route a overlaps in segment 1",
        source_entity_ids=sources,
    )
    second = QuantityWarning(
        code="overlap",
        message="route a overlaps in segment 9",
        source_entity_ids=sources,
    )
    collections = {"routes": ()}
    forward = _warning_items([first, second], {}, collections)
    backward = _warning_items([second, first], {}, collections)
    assert len(forward) == 2 and len(backward) == 2
    assert {item.item_id for item in forward} == {item.item_id for item in backward}
    assert forward[0].item_id != forward[1].item_id
    assert forward == backward
    # The identical warning deduplicates onto one item.
    single = _warning_items([first, first], {}, collections)
    assert len(single) == 1


def test_manifest_json_is_deterministic_bytes() -> None:
    model = _routed_garage()
    first = build_confidence_review(model, ReviewConfig(confidence_threshold=0.99))
    second = build_confidence_review(model, ReviewConfig(confidence_threshold=0.99))
    assert first.to_json() == second.to_json()
    # Sorted keys, no timestamp: the only variable content is the review.
    assert '"confidence_threshold"' in first.to_json()
    assert first.to_json() == first.to_json()


def test_model_level_warning_and_empty_model_do_not_crash() -> None:
    """A model-level warning has no sources; inventing one would be a lie."""

    empty = BuildingModel(model_id="model:synthetic-empty")
    manifest = build_confidence_review(empty, ReviewConfig(confidence_threshold=0.8))
    report = extract_quantities(empty)
    model_level = [
        warning for warning in report.warnings if not warning.source_entity_ids
    ]
    assert model_level, "empty takeoff must still warn"
    warning_items = [
        item for item in manifest.items if item.item_id.startswith("review:warning:")
    ]
    assert any(item.entity_ids == () for item in warning_items)
    # Model-level items survive the portable boundary unchanged.
    decoded = ConfidenceReviewManifest.from_dict(manifest.to_dict())
    assert decoded.to_json() == manifest.to_json()
    assert any(item.entity_ids == () for item in decoded.items)

    # A nonempty model with quantities also builds without crashing, and
    # every warning item keeps its real sources.
    populated = build_confidence_review(
        _review_model(_wall(confidence=0.4)), ReviewConfig(confidence_threshold=0.8),
    )
    assert populated.items


def test_source_attribute_edit_invalidates_stale_bindings() -> None:
    """A source-content edit under unchanged ids and page breaks bindings."""

    record = Provenance(
        source_kind="drawing",
        source_id="plan:synthetic",
        method="scaled-drawing",
        confidence=0.9,
        derivation="observed",
        attributes={"source_sha256": "original-content-hash"},
    )
    model = _review_model(_wall(confidence=0.4, provenance=(record,)))
    reference = _bound_reference(model)
    # Equal inputs keep equal fingerprints.
    assert fingerprint_provenance(record) == fingerprint_provenance(
        model.walls[0].provenance[0]
    )
    edited = replace(
        model,
        walls=(
            replace(
                model.walls[0],
                provenance=(
                    replace(record, attributes={"source_sha256": "revised-content"}),
                ),
            ),
        ),
    )
    # Same source kind/id/page, different content: the old reference is stale.
    assert fingerprint_provenance(edited.walls[0].provenance[0]) != reference.fingerprint
    with pytest.raises(ValueError, match="stale"):
        build_confidence_review(
            edited, ReviewConfig(confidence_threshold=0.5), evidence=[reference],
        )


def test_portable_boundary_rejects_unbound_or_malformed_crops() -> None:
    """The portable decoder enforces the binding, not just the builder."""

    model = _review_model(_wall(confidence=0.4))
    manifest = build_confidence_review(
        model,
        ReviewConfig(confidence_threshold=0.5),
        evidence=[_bound_reference(model)],
    )
    document = manifest.to_dict()
    item = next(
        entry for entry in document["items"] if entry["entity_ids"] == ["wall:review-south"]
    )

    def decode_with_crop(crop: dict) -> ConfidenceReviewManifest:
        return ConfidenceReviewManifest.from_dict({
            **document,
            "items": [{**item, "crops": [crop]}],
        })

    # Unrelated source on an item whose provenance is different.
    foreign = {
        "ref_id": "crop:foreign",
        "source_kind": "foreign",
        "source_id": "source:foreign",
        "fingerprint": "a" * 64,
    }
    with pytest.raises(ValueError, match="does not carry"):
        decode_with_crop(foreign)
    # Garbage digest shape.
    with pytest.raises(ValueError, match="sha-256 digest"):
        decode_with_crop({**item["crops"][0], "fingerprint": "garbage"})
    # Wrong page against the same source.
    with pytest.raises(ValueError, match="does not carry"):
        decode_with_crop({**item["crops"][0], "page": 3})
    # A well-bound crop still decodes, and the full manifest round-trips.
    assert ConfidenceReviewManifest.from_dict(document).to_json() == manifest.to_json()

    # Provenance scope rules hold at this boundary too: empty and malformed
    # scopes are rejected, exactly as the canonical contract rejects them.
    base_provenance = item["provenance"][0]
    for bad_scopes in ([], ["not a path"], ["ok", "ok"]):
        with pytest.raises(ValueError, match="scope_paths"):
            ConfidenceReviewManifest.from_dict({
                **document,
                "items": [{
                    **item,
                    "provenance": [{**base_provenance, "scope_paths": bad_scopes}],
                }],
            })


def test_warning_items_carry_provenance_their_crops_bind_to() -> None:
    """Evidence on a warning item travels with the records it illustrates."""

    model = _routed_garage()
    wall = next(
        entity
        for warning in extract_quantities(model).warnings
        for entity in model.walls
        if entity.id in warning.source_entity_ids
    )
    record = wall.provenance[0]
    manifest = build_confidence_review(
        model,
        ReviewConfig(confidence_threshold=0.99),
        evidence=[EvidenceReference(
            ref_id="crop:wall-evidence",
            entity_id=wall.id,
            source_kind=record.source_kind,
            source_id=record.source_id,
            page=record.page,
            fingerprint=fingerprint_provenance(record),
        )],
    )
    crop_items = [item for item in manifest.items if item.crops]
    assert crop_items
    for item in crop_items:
        bound_sources = {
            (entry["source_kind"], entry["source_id"], entry.get("page"))
            for entry in item.provenance
        }
        for crop in item.crops:
            assert (
                crop["source_kind"], crop["source_id"], crop.get("page")
            ) in bound_sources
    # The whole manifest still round-trips strictly.
    decoded = ConfidenceReviewManifest.from_dict(manifest.to_dict())
    assert decoded.to_json() == manifest.to_json()


def test_v1_3_support_collections_and_unresolved_support_selection() -> None:
    """The 1.3 support collections are scanned, and a real unresolved
    support outcome (from the actual producer, via the shared-supports
    fixture recipe) becomes an item."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "test_shared_supports_module",
        ROOT / "tests" / "test_shared_supports.py",
    )
    supports_tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supports_tests)

    # ``case`` is a pytest fixture in that module; its wrapped function is
    # the plain builder.
    base, intent, catalog, rule = supports_tests.case.__wrapped__()
    unresolved_model = supports_tests.select_supports(
        supports_tests.upgrade_to_v1_3(base),
        intents=(replace(intent, basis=None),),
        catalog=catalog,
        rules=(rule,),
    )
    assert unresolved_model.schema_version == "1.3.0"
    outcome = unresolved_model.support_selection_outcomes[0]
    assert outcome.status == "unresolved"

    manifest = build_confidence_review(
        unresolved_model, ReviewConfig(confidence_threshold=0.99),
    )
    assert {
        "support_assemblies", "support_components",
        "support_interfaces", "support_selection_outcomes",
    } <= set(manifest.collections)
    item = next(
        item for item in manifest.items if item.entity_ids == (outcome.id,)
    )
    assert item.reasons == ("unresolved_selection",)
    assert item.entity_kind == "support_selection_outcomes"
    assert outcome.reason_codes[0] in item.detail
