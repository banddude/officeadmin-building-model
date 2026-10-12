"""#274 public tests: deterministic canonical revision diff."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.importers.roomplan.importer import load_captured_room
from oabm.model import (
    BuildingModel,
    ContractError,
    Point3,
    Provenance,
)
from oabm.qa import (
    REVISION_DIFF_VERSION,
    RevisionDiff,
    build_revision_diff,
)
from oabm.quantities import extract_quantities

ROOT = Path(__file__).resolve().parents[1]


def _routed_garage() -> BuildingModel:
    return BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")


def _garage_with(**changes) -> BuildingModel:
    return replace(_routed_garage(), **changes)


def _entries(diff: RevisionDiff, change: str) -> list:
    return [entry for entry in diff.changes if entry.change == change]


def _delta(diff: RevisionDiff, category: str, item_type: str) -> list:
    return [
        entry for entry in diff.quantity_deltas
        if entry.category == category and entry.item_type == item_type
    ]


def test_unchanged_snapshots_produce_an_empty_diff() -> None:
    model = _routed_garage()
    diff = build_revision_diff(model, model)
    assert diff.added_count == diff.removed_count == diff.changed_count == 0
    assert diff.changes == () and diff.quantity_deltas == ()
    # Deterministic bytes across rebuilds.
    assert diff.to_json() == build_revision_diff(model, model).to_json()


def test_added_and_removed_devices_carry_documents_and_count_deltas() -> None:
    model = _routed_garage()
    from oabm.model import ElectricalDevice
    standalone = ElectricalDevice(
        id="device:standalone", device_type="receptacle_duplex",
        pose=model.electrical_devices[0].pose, level_id=model.levels[0].id,
        provenance=model.electrical_devices[0].provenance,
    )
    with_extra = _garage_with(
        electrical_devices=(*model.electrical_devices, standalone),
    )
    # Base -> target adds it; target -> base removes it.
    added_diff = build_revision_diff(model, with_extra)
    removed_diff = build_revision_diff(with_extra, model)

    added = _entries(added_diff, "added")
    removed = _entries(removed_diff, "removed")
    assert [entry.entity_id for entry in added] == [standalone.id]
    assert [entry.entity_id for entry in removed] == [standalone.id]
    # Added and removed records carry their full canonical document.
    assert removed[0].document["id"] == standalone.id
    assert added[0].document["id"] == standalone.id
    assert added_diff.added_count == 1 and removed_diff.removed_count == 1

    # Signed count deltas: the added direction reports an added line, the
    # reverse direction a removed one, matched by type identity.
    added_lines = [
        entry for entry in added_diff.quantity_deltas
        if entry.category == "device" and entry.status == "added"
    ]
    removed_lines = [
        entry for entry in removed_diff.quantity_deltas
        if entry.category == "device" and entry.status == "removed"
    ]
    assert added_lines and removed_lines
    assert any(
        entry.before is None and entry.after is not None and entry.delta is None
        for entry in added_lines
    )
    assert any(
        entry.before is not None and entry.after is None and entry.delta is None
        for entry in removed_lines
    )
    # Every delta line names its delta id deterministically.
    assert all(
        entry.delta_id.startswith("revision:quantity:")
        for entry in (*added_diff.quantity_deltas, *removed_diff.quantity_deltas)
    )


def test_moved_wall_is_a_geometry_change_with_length_delta() -> None:
    model = _routed_garage()
    wall = model.walls[0]
    moved_centerline = replace(
        wall.centerline,
        points=(Point3(x=0.5, y=0.0, z=0.0), *wall.centerline.points[1:]),
    )
    moved = _garage_with(walls=(
        replace(wall, centerline=moved_centerline), *model.walls[1:],
    ))
    diff = build_revision_diff(model, moved)

    changed = _entries(diff, "changed")
    assert [entry.entity_id for entry in changed] == [wall.id]
    entry = changed[0]
    assert entry.change_classes == ("geometry",)
    assert [change.path for change in entry.field_changes] == ["centerline"]
    assert entry.field_changes[0].before != entry.field_changes[0].after
    assert entry.document is None

    # Signed length delta: moving the wall 0.5 m changes its face area and
    # length lines, matched on complete item identity.
    length_deltas = _delta(diff, "wall_length", wall.route_type if False else "wall")
    assert length_deltas, "wall_length delta lines must exist"
    for line in length_deltas:
        assert line.status == "changed"
        assert line.before is not None and line.after is not None
        assert line.delta == pytest.approx(line.after - line.before)
        assert line.delta < 0


def test_size_change_is_geometry_with_auditable_quantity_lines() -> None:
    # Thickness is part of the takeoff line's variant identity: a thicker
    # wall is a different line, so the audit gets an explicit removal of the
    # old variant and an explicit addition of the new one, never a silent
    # in-place mutation and never a fabricated zero.
    model = _routed_garage()
    wall = model.walls[0]
    thicker = _garage_with(walls=(
        replace(wall, thickness_m=wall.thickness_m + 0.05), *model.walls[1:],
    ))
    diff = build_revision_diff(model, thicker)
    entry = _entries(diff, "changed")[0]
    assert "geometry" in entry.change_classes
    paths = {change.path for change in entry.field_changes}
    assert "thickness_m" in paths
    volume = _delta(diff, "wall_volume", "wall")
    # The new variant appears as an addition; the old variant's volume is
    # lost either through a removed line or (when other walls share the old
    # variant) a negative changed line. Both are auditable; none is zero.
    added_line = next(line for line in volume if line.status == "added")
    assert added_line.before is None and added_line.after > 0
    lost = [line for line in volume if line.status != "added"]
    assert lost
    assert all(
        (line.before or 0) > (line.after or 0) for line in lost
    )


def test_product_change_is_distinguishable_from_geometry() -> None:
    from oabm.model import (
        CatalogProduct, RuleReference, upgrade_to_v1_2,
    )
    from oabm.electrical import (
        RacewayCatalogEntry, RacewayIntent, RacewayRule, select_raceways,
    )

    base = upgrade_to_v1_2(_routed_garage())
    route = base.routes[0]
    product_a = CatalogProduct(
        catalog_id="synthetic", catalog_revision="1", product_id="conduit-a",
    )
    product_b = CatalogProduct(
        catalog_id="synthetic", catalog_revision="1", product_id="conduit-b",
    )
    basis = RuleReference(
        authority="synthetic-company", revision="1", rule_id="explicit-product",
    )
    provenance = (
        Provenance(source_kind="synthetic", source_id="fixture:raceway", derivation="user"),
    )

    def entry_for(product):
        return RacewayCatalogEntry(
            product=product, route_type=route.route_type, wiring_method="raceway",
            designation_kind="trade_size", designation="synthetic trade A",
            nominal_diameter_m=route.nominal_diameter_m, provenance=provenance,
        )

    rule_a = RacewayRule(
        reference=basis, product=product_a, route_type=route.route_type,
        provenance=provenance,
    )
    rule_b = replace(rule_a, product=product_b)
    intent_a = RacewayIntent(
        intent_id="intent:raceway", route_id=route.id, actor_id="synthetic-reviewer",
        product=product_a, basis=basis,
    )
    # The producer's idempotent upsert keeps the selection identity when the
    # caller reselects with a different (permitted) product.
    before = select_raceways(
        base, intents=(intent_a,), catalog=(entry_for(product_a),), rules=(rule_a,),
    )
    intent_b = replace(intent_a, product=product_b)
    after = select_raceways(
        base, intents=(intent_b,), catalog=(entry_for(product_b),), rules=(rule_b,),
    )
    selection_before = before.raceway_selections[0]
    selection_after = after.raceway_selections[0]
    assert selection_before.id == selection_after.id

    diff = build_revision_diff(before, after)
    # Both the selection and its outcome change, and each carries the
    # product class on its own record.
    selection_entry = next(
        entry for entry in _entries(diff, "changed")
        if entry.entity_id == selection_after.id
    )
    # The selection's product and the provenance shadow of the reselect both
    # moved; the product class is present and separable from provenance.
    assert "product" in selection_entry.change_classes
    assert "provenance" in selection_entry.change_classes
    assert set(selection_entry.change_classes) == {"product", "provenance"}
    paths = {change.path for change in selection_entry.field_changes}
    assert "product" in paths
    outcome_entry = next(
        entry for entry in _entries(diff, "changed")
        if entry.entity_id == after.raceway_selection_outcomes[0].id
    )
    assert "product" in outcome_entry.change_classes


def test_graph_reference_edit_is_a_graph_change() -> None:
    model = _routed_garage()
    device = model.electrical_devices[0]
    other_wall = next(
        wall for wall in model.walls if wall.id != device.host_id
    )
    rehosted = _garage_with(electrical_devices=(
        replace(device, host_id=other_wall.id), *model.electrical_devices[1:],
    ))
    diff = build_revision_diff(model, rehosted)
    entry = _entries(diff, "changed")[0]
    assert entry.entity_id == device.id
    assert entry.change_classes == ("graph",)
    assert entry.field_changes[0].path == "host_id"
    assert entry.field_changes[0].before == device.host_id
    assert entry.field_changes[0].after == other_wall.id


def test_provenance_only_change_does_not_read_as_geometry() -> None:
    model = _routed_garage()
    wall = model.walls[0]
    record = replace(
        wall.provenance[0], method=wall.provenance[0].method + "-revised",
    )
    revised = _garage_with(walls=(
        replace(wall, provenance=(record,)), *model.walls[1:],
    ))
    diff = build_revision_diff(model, revised)
    entry = _entries(diff, "changed")[0]
    assert entry.change_classes == ("provenance",)
    assert {change.path for change in entry.field_changes} == {"provenance"}


def test_collection_ordering_is_not_a_change_but_geometry_order_is() -> None:
    model = _routed_garage()
    reordered = _garage_with(
        walls=tuple(reversed(model.walls)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        routes=tuple(reversed(model.routes)),
    )
    diff = build_revision_diff(model, reordered)
    assert diff.added_count == diff.removed_count == diff.changed_count == 0
    assert diff.quantity_deltas == ()

    # Order INSIDE one entity's geometry is meaningful: a wall centerline
    # run from the other end is a detected geometry change, not noise.
    wall = model.walls[0]
    flipped_wall = replace(
        wall, centerline=replace(wall.centerline, points=tuple(
            reversed(wall.centerline.points)
        )),
    )
    flipped = _garage_with(walls=(
        flipped_wall, *model.walls[1:],
    ))
    wall_diff = build_revision_diff(model, flipped)
    entry = _entries(wall_diff, "changed")[0]
    assert entry.change_classes == ("geometry",)
    assert entry.entity_id == wall.id


def test_incompatible_snapshots_are_refused_never_guessed() -> None:
    model = _routed_garage()
    other_identity = _garage_with(model_id="model:other-job")
    with pytest.raises(ValueError, match="different model identities"):
        build_revision_diff(model, other_identity)

    from oabm.model import CoordinateSystem
    other_frame = _garage_with(
        coordinate_system=CoordinateSystem(
            frame_id="frame:site-north", crs="EPSG:2222",
        ),
    )
    with pytest.raises(ValueError, match="coordinate frames or units"):
        build_revision_diff(model, other_frame)
    # The canonical contract pins metres, so a caller cannot even build a
    # feet-unit snapshot to sneak past the refusal.
    with pytest.raises(ContractError):
        CoordinateSystem(length_unit="ft")

    other_version = replace(model, schema_version="1.2.0")
    with pytest.raises(ValueError, match="mismatched schema versions"):
        build_revision_diff(model, other_version)


def test_snapshots_are_never_mutated() -> None:
    model = _routed_garage()
    before_base = model.to_json()
    moved = _garage_with(walls=(
        replace(model.walls[0], thickness_m=model.walls[0].thickness_m + 0.05),
        *model.walls[1:],
    ))
    before_target = moved.to_json()
    build_revision_diff(model, moved)
    assert model.to_json() == before_base
    assert moved.to_json() == before_target


def test_strict_report_decoding() -> None:
    model = _routed_garage()
    moved = _garage_with(walls=(
        replace(model.walls[0], thickness_m=model.walls[0].thickness_m + 0.05),
        *model.walls[1:],
    ))
    diff = build_revision_diff(model, moved)
    decoded = RevisionDiff.from_dict(diff.to_dict())
    assert decoded == diff
    assert decoded.to_json() == diff.to_json()

    document = diff.to_dict()
    with pytest.raises(ValueError, match="unknown revision diff keys"):
        RevisionDiff.from_dict({**document, "extra": True})
    with pytest.raises(ValueError, match="kind"):
        RevisionDiff.from_dict({**document, "kind": "takeoff_report"})
    with pytest.raises(ValueError, match="version"):
        RevisionDiff.from_dict({**document, "version": "9.9.9"})
    with pytest.raises(ValueError, match="one model identity"):
        RevisionDiff.from_dict({**document, "target_model_id": "model:other"})
    with pytest.raises(ValueError, match="added_count"):
        RevisionDiff.from_dict({**document, "added_count": -1})
    # Counts must agree with the records they summarize.
    with pytest.raises(ValueError, match="added_count"):
        RevisionDiff.from_dict({**document, "added_count": 2})
    with pytest.raises(ValueError, match="unknown diff change keys"):
        RevisionDiff.from_dict({
            **document,
            "changes": [{**document["changes"][0], "surprise": 1}],
        })
    with pytest.raises(ValueError, match="changed record carries field changes"):
        changed_entry = next(
            entry for entry in document["changes"] if entry["change"] == "changed"
        )
        RevisionDiff.from_dict({
            **document,
            "changes": [{**changed_entry, "document": {"id": "x"}}],
        })
    with pytest.raises(ValueError, match="carry their document"):
        RevisionDiff.from_dict({
            **document,
            "changes": [{**changed_entry, "change": "added", "document": None}],
        })
    bad_delta = {
        **document,
        "quantity_deltas": [{**document["quantity_deltas"][0], "delta": "lots"}],
    }
    with pytest.raises(ValueError, match="finite number"):
        RevisionDiff.from_dict(bad_delta)


def test_deltas_are_deterministic_under_input_permutations() -> None:
    model = _routed_garage()
    wall = model.walls[0]
    moved_a = _garage_with(walls=(
        replace(wall, thickness_m=wall.thickness_m + 0.05), *model.walls[1:],
    ))
    # Same semantic target, entities supplied in a different order.
    moved_b = replace(moved_a, electrical_devices=tuple(
        reversed(moved_a.electrical_devices)
    ))
    assert (
        build_revision_diff(model, moved_a).to_json()
        == build_revision_diff(model, moved_b).to_json()
    )


def test_real_importer_output_flows_through_snapshots_to_deltas() -> None:
    """The real RoomPlan pipeline: import, revise, diff, measure deltas."""

    model = load_captured_room(
        ROOT / "fixtures" / "roomplan" / "captured-room-3d.json",
        source_id="fixture:captured-room-3d",
    )
    wall = model.walls[0]
    revised = replace(model, walls=(
        replace(wall, thickness_m=wall.thickness_m + 0.1), *model.walls[1:],
    ))
    diff = build_revision_diff(model, revised)

    entry = _entries(diff, "changed")[0]
    assert entry.entity_id == wall.id
    assert "geometry" in entry.change_classes
    # Thickness is part of the line variant identity: the audit gets the old
    # variant removed and the new one added, both with real quantities.
    volume = _delta(diff, "wall_volume", "wall")
    assert volume, "volume delta lines must exist"
    added_line = next(line for line in volume if line.status == "added")
    assert added_line.before is None and added_line.after > 0
    lost = [line for line in volume if line.status != "added"]
    assert all((line.before or 0) > (line.after or 0) for line in lost)
    # No fabricated zeros: every delta line reports both real quantities or
    # an explicit one-sided status.
    for line in diff.quantity_deltas:
        assert line.status in {"added", "removed", "changed"}
        if line.status == "changed":
            assert None not in (line.before, line.after, line.delta)


def test_version_constant_is_portable_metadata() -> None:
    assert REVISION_DIFF_VERSION == "1.0.0"
    model = _routed_garage()
    document = build_revision_diff(model, model).to_dict()
    assert document["version"] == REVISION_DIFF_VERSION
