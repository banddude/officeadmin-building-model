from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.importers.roomplan import import_captured_room
from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    Box3D,
    BuildingModel,
    Ceiling,
    ContractError,
    ElectricalDevice,
    Level,
    Obstacle,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Provenance,
    Size3,
    Slab,
    Space,
    Wall,
)
from oabm.quantities import (
    AREA_UNIT,
    COUNT_UNIT,
    LENGTH_UNIT,
    MEASURABLE_KINDS,
    VOLUME_UNIT,
    QuantityError,
    extract_quantities,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "quantities" / "synthetic-route-model.json"


def _model() -> BuildingModel:
    return BuildingModel.load(FIXTURE)


def _item(report, category: str, item_type: str, **variant):
    matches = [
        item
        for item in report.items
        if item.category == category
        and item.item_type == item_type
        and all(dict(item.variant).get(key) == value for key, value in variant.items())
    ]
    assert len(matches) == 1
    return matches[0]


def test_known_answer_quantities_come_from_canonical_semantics() -> None:
    report = extract_quantities(_model())

    # 3-4-5 route plus a 2 m vertical route; the separate cable route is 1 m.
    emt = _item(report, "route_length", "emt", nominal_diameter_m=0.021)
    cable = _item(report, "route_length", "cable", nominal_diameter_m=None)
    assert emt.quantity == pytest.approx(7.0)
    assert cable.quantity == pytest.approx(1.0)

    elbows = _item(
        report,
        "fitting",
        "elbow",
        nominal_diameter_m=0.021,
        angle_radians=1.5707963267948966,
    )
    assert elbows.quantity == 2

    # The line conductor has count=2 and traverses both EMT routes: 2 * (5 + 2).
    line = _item(report, "conductor_length", "line", size="#6 AWG")
    egc = _item(report, "conductor_length", "equipment-ground", size="#10 AWG")
    assert line.quantity == pytest.approx(14.0)
    assert egc.quantity == pytest.approx(7.0)

    assert _item(report, "device", "junction-box").quantity == 1
    assert _item(report, "device", "evse").quantity == 1
    assert _item(report, "device", "sensor").quantity == 1
    assert _item(report, "equipment", "panelboard").quantity == 1


def test_units_are_explicit_and_canonical() -> None:
    report = extract_quantities(_model())
    assert report.length_unit == LENGTH_UNIT == "m"
    assert report.count_unit == COUNT_UNIT == "ea"
    assert all(
        item.unit == "m"
        for item in report.items
        if item.category in {"route_length", "conductor_length"}
    )
    assert all(
        item.unit == "ea"
        for item in report.items
        if item.category in {"fitting", "device", "equipment"}
    )


def test_provenance_sources_and_confidence_are_retained() -> None:
    model = _model()
    low_route = replace(model.routes[0], confidence=0.65)
    low_line = replace(model.conductors[1], confidence=0.8)
    model = replace(
        model,
        routes=(low_route, *model.routes[1:]),
        conductors=(model.conductors[0], low_line),
    )

    report = extract_quantities(model)
    route_item = _item(report, "route_length", "emt", nominal_diameter_m=0.021)
    line_item = _item(report, "conductor_length", "line", size="#6 AWG")

    assert route_item.confidence == pytest.approx(0.65)
    assert line_item.confidence == pytest.approx(0.65)
    assert "route:emt-a" in line_item.source_entity_ids
    assert "cond:line" in line_item.source_entity_ids
    assert line_item.provenance[0].source_id == "fixture:quantities-v1"


def test_repeatability_is_independent_of_collection_order() -> None:
    model = _model()
    shuffled = replace(
        model,
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        ports=tuple(reversed(model.ports)),
        routes=tuple(reversed(model.routes)),
        route_fittings=tuple(reversed(model.route_fittings)),
        circuits=tuple(reversed(model.circuits)),
        conductors=tuple(reversed(model.conductors)),
    )

    first = extract_quantities(model).to_json(indent=None)
    second = extract_quantities(shuffled).to_json(indent=None)
    third = extract_quantities(model).to_json(indent=None)
    assert first == second == third


def test_identical_device_variants_aggregate_without_losing_sources() -> None:
    model = _model()
    first_box = next(item for item in model.electrical_devices if item.device_type == "junction-box")
    second_box = ElectricalDevice(
        id="device:box-b",
        name="J-box B",
        device_type=first_box.device_type,
        pose=Pose(position=Point3(x=8, y=8, z=1)),
        size=Size3(x=0.1, y=0.1, z=0.1),
        provenance=first_box.provenance,
    )
    model = replace(model, electrical_devices=(*model.electrical_devices, second_box))

    item = _item(extract_quantities(model), "device", "junction-box")
    assert item.quantity == 2
    assert item.source_entity_ids == ("device:box-a", "device:box-b")


def test_different_material_variants_are_not_collapsed() -> None:
    model = _model()
    first, second, third = model.routes
    wider = replace(second, nominal_diameter_m=0.027)
    model = replace(model, routes=(first, wider, third))

    report = extract_quantities(model)
    emt_items = [item for item in report.items if item.category == "route_length" and item.item_type == "emt"]
    assert [(dict(item.variant)["nominal_diameter_m"], item.quantity) for item in emt_items] == [
        (0.021, 5.0),
        (0.027, 2.0),
    ]


def test_duplicate_conductor_route_reference_is_rejected_instead_of_double_counted() -> None:
    model = _model()
    conductor = model.conductors[0]
    duplicate = replace(conductor, route_ids=(conductor.route_ids[0], conductor.route_ids[0]))
    model = replace(model, conductors=(duplicate, *model.conductors[1:]))

    with pytest.raises(QuantityError, match="duplicate references"):
        extract_quantities(model)


def test_duplicate_fitting_reference_is_rejected_instead_of_double_counted() -> None:
    model = _model()
    route = model.routes[0]
    duplicate = replace(route, fitting_ids=(route.fitting_ids[0], route.fitting_ids[0]))
    model = replace(model, routes=(duplicate, *model.routes[1:]))

    with pytest.raises(QuantityError, match="duplicate references"):
        extract_quantities(model)


def test_malformed_reference_is_revalidated_at_quantity_boundary() -> None:
    model = _model()
    # Simulate corruption after normal model construction. The quantity boundary
    # must not assume callers preserved canonical reference integrity.
    object.__setattr__(model.conductors[0], "route_ids", ("route:missing",))

    with pytest.raises(ContractError, match="references missing id"):
        extract_quantities(model)


def test_unrouted_conductor_is_reported_and_not_inferred_from_circuit_routes() -> None:
    model = _model()
    line = next(item for item in model.conductors if item.role == "line")
    unrouted = replace(line, route_ids=())
    conductors = tuple(unrouted if item.id == line.id else item for item in model.conductors)
    model = replace(model, conductors=conductors)

    report = extract_quantities(model)
    assert not [item for item in report.items if item.category == "conductor_length" and item.item_type == "line"]
    assert len(report.warnings) == 1
    assert report.warnings[0].code == "unrouted_conductor"
    assert report.warnings[0].source_entity_ids == (line.id,)


def test_assembly_resolver_is_a_downstream_hook_not_model_metadata() -> None:
    model = _model()

    def resolver(category, entity):
        if category == "device" and getattr(entity, "device_type", None) == "evse":
            return "assembly:evse-wall"
        if category == "route_length" and getattr(entity, "route_type", None) == "emt":
            return "assembly:emt"
        return None

    report = extract_quantities(model, assembly_resolver=resolver)
    assert _item(report, "device", "evse").assembly_key == "assembly:evse-wall"
    assert _item(report, "route_length", "emt", nominal_diameter_m=0.021).assembly_key == "assembly:emt"
    assert _item(report, "equipment", "panelboard").assembly_key is None
    assert "assembly" not in model.attributes


def test_invalid_assembly_resolver_value_is_rejected() -> None:
    with pytest.raises(QuantityError, match="non-empty string"):
        extract_quantities(_model(), assembly_resolver=lambda category, entity: "")


def test_empty_model_says_why_it_is_empty_instead_of_staying_silent() -> None:
    report = extract_quantities(BuildingModel(model_id="model:empty"))
    assert report.items == ()
    assert [warning.code for warning in report.warnings] == ["empty_takeoff"]
    assert report.scope.empty_reason == (
        "the model contains no entities, so there is nothing to measure"
    )
    assert report.scope.present_kinds == ()


# --- issue #80: architectural quantities, scope reporting, per-claim provenance ---

ROOMPLAN_FIXTURE = ROOT / "fixtures" / "roomplan" / "captured-room-3d.json"

LEVEL = Level(id="level:ground", elevation_m=0.0)

_OBSERVED = Provenance(
    source_kind="test", source_id="scan:1", derivation=DERIVATION_OBSERVED
)
#: The shape a scanner-backed importer emits for a dimension it had to supply:
#: a second record, scoped by name to the one dimension it assumed.
_ASSUMED_THICKNESS = Provenance(
    source_kind="test",
    source_id="scan:1",
    derivation=DERIVATION_INFERRED,
    attributes={"assumed_dimension": "thickness_m"},
)


def _wall(
    *,
    wall_id: str = "wall:a",
    length_m: float = 4.0,
    height_m: float = 3.0,
    thickness_m: float = 0.2,
    provenance: tuple[Provenance, ...] = (_OBSERVED,),
) -> Wall:
    return Wall(
        id=wall_id,
        level_id=LEVEL.id,
        centerline=Polyline3D(
            points=(Point3(x=0.0, y=0.0, z=0.0), Point3(x=length_m, y=0.0, z=0.0))
        ),
        thickness_m=thickness_m,
        height_m=height_m,
        provenance=provenance,
    )


def _rect(width: float, depth: float, z: float = 0.0) -> Polygon3D:
    return Polygon3D(
        points=(
            Point3(x=0.0, y=0.0, z=z),
            Point3(x=width, y=0.0, z=z),
            Point3(x=width, y=depth, z=z),
            Point3(x=0.0, y=depth, z=z),
        )
    )


def _architectural_model(**kwargs) -> BuildingModel:
    return BuildingModel(model_id="model:architectural", levels=(LEVEL,), **kwargs)


def test_building_without_electrical_content_still_produces_a_takeoff() -> None:
    """The issue's headline: 20 architectural entities used to yield items: 0."""

    model = _architectural_model(
        walls=(_wall(),),
        slabs=(
            Slab(
                id="slab:a",
                level_id=LEVEL.id,
                footprint=_rect(4.0, 3.0),
                thickness_m=0.1,
                provenance=(_OBSERVED,),
            ),
        ),
        spaces=(
            Space(
                id="space:a",
                level_id=LEVEL.id,
                footprint=_rect(4.0, 3.0),
                height_m=2.5,
                usage="kitchen",
                provenance=(_OBSERVED,),
            ),
        ),
    )
    report = extract_quantities(model)

    assert report.items != ()
    assert _item(report, "wall_length", "wall").quantity == pytest.approx(4.0)
    assert _item(report, "wall_face_area", "wall", faces="single").quantity == pytest.approx(12.0)
    assert _item(report, "wall_volume", "wall").quantity == pytest.approx(2.4)
    assert _item(report, "slab_area", "slab").quantity == pytest.approx(12.0)
    assert _item(report, "slab_volume", "slab").quantity == pytest.approx(1.2)
    assert _item(report, "space_floor_area", "kitchen").quantity == pytest.approx(12.0)
    assert _item(report, "space_volume", "kitchen").quantity == pytest.approx(30.0)
    assert report.scope.empty_reason is None


def test_wall_face_area_is_measured_while_its_volume_is_inferred_in_one_report() -> None:
    """The distinction issue #80 turns on.

    The wall's position and extent were scanned; its thickness is a default the
    importer chose.  Face area = length x height consumes only observed
    dimensions, so it is a measurement.  Volume multiplies the assumed
    thickness in, so it is an inference.  Tainting both because the entity
    carries one inferred record would report a measured area as a guess.
    """

    wall = _wall(provenance=(_OBSERVED, _ASSUMED_THICKNESS))
    report = extract_quantities(_architectural_model(walls=(wall,)))

    assert _item(report, "wall_face_area", "wall", faces="single").derivation == DERIVATION_OBSERVED
    assert _item(report, "wall_volume", "wall").derivation == DERIVATION_INFERRED
    # The length never multiplied a thickness in either, even though thickness
    # is in its variant so materially different walls stay on separate lines.
    assert _item(report, "wall_length", "wall").derivation == DERIVATION_OBSERVED
    assert dict(_item(report, "wall_length", "wall").variant)["thickness_m"] == 0.2


def test_assumed_dimension_reaches_only_the_quantities_that_consume_it() -> None:
    """A slab's area is measured from its footprint; its volume is not."""

    slab = Slab(
        id="slab:a",
        level_id=LEVEL.id,
        footprint=_rect(4.0, 3.0),
        thickness_m=0.001,
        provenance=(_OBSERVED, _ASSUMED_THICKNESS),
    )
    report = extract_quantities(_architectural_model(slabs=(slab,)))

    assert _item(report, "slab_area", "slab").derivation == DERIVATION_OBSERVED
    assert _item(report, "slab_volume", "slab").derivation == DERIVATION_INFERRED


def test_a_count_is_not_tainted_by_an_assumed_dimension() -> None:
    """Counting an opening asserts only that it exists, not how big it is."""

    wall = _wall()
    opening = Opening(
        id="opening:a",
        host_id=wall.id,
        opening_type="door",
        pose=Pose(position=Point3(x=1.0, y=0.0, z=1.0)),
        size=Size3(x=0.9, y=0.2, z=2.1),
        provenance=(
            _OBSERVED,
            Provenance(
                source_kind="test",
                source_id="scan:1",
                derivation=DERIVATION_INFERRED,
                attributes={"assumed_dimension": "size"},
            ),
        ),
    )
    report = extract_quantities(_architectural_model(walls=(wall,), openings=(opening,)))

    assert _item(report, "opening_count", "door").derivation == DERIVATION_OBSERVED


def test_unstated_provenance_never_reads_as_observed() -> None:
    """Fail closed: a record that never said how it came to exist is not a sighting."""

    unstated = Provenance(source_kind="test", source_id="scan:1")
    assert unstated.derivation is None
    report = extract_quantities(_architectural_model(walls=(_wall(provenance=(unstated,)),)))

    derivation = _item(report, "wall_length", "wall").derivation
    assert derivation != DERIVATION_OBSERVED
    assert derivation is None


def test_a_quantity_with_no_provenance_at_all_is_not_observed() -> None:
    report = extract_quantities(_architectural_model(walls=(_wall(provenance=()),)))
    assert _item(report, "wall_length", "wall").derivation is None


@pytest.mark.parametrize(
    ("derivations", "expected"),
    [
        ((DERIVATION_OBSERVED,), DERIVATION_OBSERVED),
        ((DERIVATION_USER,), DERIVATION_USER),
        # user outranks observed, inferred outranks both, unstated sits between
        # inferred and user so it can never be mistaken for a sighting.
        ((DERIVATION_OBSERVED, DERIVATION_USER), DERIVATION_USER),
        ((DERIVATION_OBSERVED, DERIVATION_USER, None), None),
        ((DERIVATION_OBSERVED, None, DERIVATION_INFERRED), DERIVATION_INFERRED),
        ((DERIVATION_USER, DERIVATION_INFERRED), DERIVATION_INFERRED),
    ],
)
def test_derivation_precedence_is_inferred_then_unstated_then_user_then_observed(
    derivations, expected
) -> None:
    provenance = tuple(
        Provenance(source_kind="test", source_id=f"scan:{index}", derivation=value)
        for index, value in enumerate(derivations)
    )
    report = extract_quantities(_architectural_model(walls=(_wall(provenance=provenance),)))
    assert _item(report, "wall_length", "wall").derivation == expected


def test_aggregated_line_takes_the_weakest_derivation_of_its_parts() -> None:
    """Two identical walls, one with an assumed thickness, land on one line."""

    measured = _wall(wall_id="wall:a", provenance=(_OBSERVED,))
    assumed = _wall(wall_id="wall:b", provenance=(_OBSERVED, _ASSUMED_THICKNESS))
    report = extract_quantities(_architectural_model(walls=(measured, assumed)))

    volume = _item(report, "wall_volume", "wall")
    assert volume.source_entity_ids == ("wall:a", "wall:b")
    assert volume.quantity == pytest.approx(4.8)
    assert volume.derivation == DERIVATION_INFERRED
    # The face areas of both walls are still measurements.
    assert _item(report, "wall_face_area", "wall", faces="single").derivation == DERIVATION_OBSERVED


def test_polygon_area_is_computed_for_any_plane_not_a_horizontal_projection() -> None:
    """A pitched ceiling: 20 m2 of surface whose XY shadow is only 12 m2."""

    ceiling = Ceiling(
        id="ceiling:pitched",
        level_id=LEVEL.id,
        footprint=Polygon3D(
            points=(
                Point3(x=0.0, y=0.0, z=0.0),
                Point3(x=4.0, y=0.0, z=0.0),
                Point3(x=4.0, y=3.0, z=4.0),
                Point3(x=0.0, y=3.0, z=4.0),
            )
        ),
        provenance=(_OBSERVED,),
    )
    report = extract_quantities(_architectural_model(ceilings=(ceiling,)))

    area = _item(report, "ceiling_area", "ceiling").quantity
    assert area == pytest.approx(20.0)
    assert area != pytest.approx(12.0)


def test_wall_face_area_states_whether_it_is_one_face_or_both() -> None:
    report = extract_quantities(_architectural_model(walls=(_wall(),)))
    item = _item(report, "wall_face_area", "wall", faces="single")
    assert dict(item.variant)["faces"] == "single"
    assert item.quantity == pytest.approx(12.0)
    assert item.unit == AREA_UNIT == "m2"


def test_materially_different_walls_stay_on_separate_lines() -> None:
    thin = _wall(wall_id="wall:thin", thickness_m=0.1)
    thick = _wall(wall_id="wall:thick", thickness_m=0.3)
    tall = _wall(wall_id="wall:tall", thickness_m=0.1, height_m=4.0)
    report = extract_quantities(_architectural_model(walls=(thin, thick, tall)))

    lines = [item for item in report.items if item.category == "wall_volume"]
    assert len(lines) == 3
    assert {dict(item.variant)["thickness_m"] for item in lines} == {0.1, 0.3}
    assert {dict(item.variant)["height_m"] for item in lines} == {3.0, 4.0}


def test_units_cover_area_and_volume() -> None:
    report = extract_quantities(
        _architectural_model(
            walls=(_wall(),),
            slabs=(
                Slab(
                    id="slab:a",
                    level_id=LEVEL.id,
                    footprint=_rect(4.0, 3.0),
                    thickness_m=0.1,
                    provenance=(_OBSERVED,),
                ),
            ),
        )
    )
    assert report.area_unit == AREA_UNIT == "m2"
    assert report.volume_unit == VOLUME_UNIT == "m3"
    assert all(
        item.unit == "m2"
        for item in report.items
        if item.category in {"wall_face_area", "slab_area", "ceiling_area", "space_floor_area"}
    )
    assert all(
        item.unit == "m3"
        for item in report.items
        if item.category in {"wall_volume", "slab_volume", "space_volume"}
    )


def test_openings_are_counted_and_the_host_wall_area_is_not_deducted() -> None:
    """Fail closed on semantics: count what is certain, decline what is not."""

    wall = _wall(length_m=4.0, height_m=3.0)
    openings = tuple(
        Opening(
            id=f"opening:{index}",
            host_id=wall.id,
            opening_type=opening_type,
            pose=Pose(position=Point3(x=0.5 + index, y=0.0, z=1.0)),
            size=Size3(x=0.9, y=0.2, z=2.1),
            provenance=(_OBSERVED,),
        )
        for index, opening_type in enumerate(("door", "window", "window"))
    )
    report = extract_quantities(_architectural_model(walls=(wall,), openings=openings))

    assert _item(report, "opening_count", "door").quantity == 1
    assert _item(report, "opening_count", "window").quantity == 2
    # Full 4 m x 3 m face, with nothing taken out for the three openings in it.
    assert _item(report, "wall_face_area", "wall", faces="single").quantity == pytest.approx(12.0)

    declined = {entry.subject: entry.reason for entry in report.scope.declined_derivations}
    assert "opening_area_deducted_from_host_wall" in declined
    assert "contract does not state" in declined["opening_area_deducted_from_host_wall"]


def test_scope_names_what_is_measurable_present_measured_and_deliberately_not() -> None:
    wall = _wall()
    model = _architectural_model(
        walls=(wall,),
        obstacles=(
            Obstacle(
                id="obstacle:a",
                level_id=LEVEL.id,
                geometry=Box3D(
                    pose=Pose(position=Point3(x=0.0, y=0.0, z=0.0)),
                    size=Size3(x=1.0, y=1.0, z=1.0),
                ),
            ),
        ),
    )
    report = extract_quantities(model)
    scope = report.scope

    assert "wall" in scope.measurable_kinds
    assert set(scope.measurable_kinds) == set(MEASURABLE_KINDS)
    assert scope.present_kinds == ("level", "obstacle", "wall")
    assert scope.measured_kinds == ("wall",)

    reasons = {entry.subject: entry.reason for entry in scope.unmeasured_kinds}
    assert set(reasons) == {"level", "obstacle"}
    assert "organizing datum" in reasons["level"]
    assert "keep clear when routing" in reasons["obstacle"]


def test_every_kind_that_is_not_a_quantity_states_a_reason() -> None:
    """level, port, obstacle, route_constraint and circuit each need an answer."""

    model = BuildingModel.load(FIXTURE)
    report = extract_quantities(model)
    reasons = {entry.subject: entry.reason for entry in report.scope.unmeasured_kinds}

    for kind in ("port", "circuit"):
        assert kind in report.scope.present_kinds
        assert reasons[kind].strip() != ""
    # A circuit's work is already on the conductor and route lines.
    assert "double-count" in reasons["circuit"]
    assert "conductors and routes" in reasons["circuit"]


def test_model_this_lane_cannot_measure_warns_rather_than_returning_silence() -> None:
    model = _architectural_model(
        obstacles=(
            Obstacle(
                id="obstacle:a",
                level_id=LEVEL.id,
                geometry=Box3D(
                    pose=Pose(position=Point3(x=0.0, y=0.0, z=0.0)),
                    size=Size3(x=1.0, y=1.0, z=1.0),
                ),
            ),
        ),
    )
    report = extract_quantities(model)

    assert report.items == ()
    codes = [warning.code for warning in report.warnings]
    assert "empty_takeoff" in codes
    reason = report.scope.empty_reason
    assert reason is not None
    assert "does not measure any kind present" in reason
    assert "obstacle" in reason
    # and it names what it *would* have measured, so the caller can tell the
    # two failure modes apart.
    assert "wall" in reason


def test_a_measurable_kind_that_produced_nothing_is_warned_about() -> None:
    model = BuildingModel.load(FIXTURE)
    stripped = replace(
        model,
        conductors=tuple(replace(item, route_ids=()) for item in model.conductors),
    )
    report = extract_quantities(stripped)

    kind_warnings = [w for w in report.warnings if w.code == "kind_not_measured"]
    assert [w.message.split(":")[0] for w in kind_warnings] == ["conductor"]
    assert "conductor" not in report.scope.measured_kinds


def test_scope_survives_the_json_round_trip() -> None:
    report = extract_quantities(_architectural_model(walls=(_wall(),)))
    document = json.loads(report.to_json())

    assert document["scope"]["measured_kinds"] == ["wall"]
    assert document["area_unit"] == "m2"
    assert document["volume_unit"] == "m3"
    assert {item["category"] for item in document["items"]} == {
        "wall_length",
        "wall_face_area",
        "wall_volume",
    }
    assert {item["derivation"] for item in document["items"]} == {DERIVATION_OBSERVED}


def test_scanned_wall_reports_measured_face_area_and_inferred_volume() -> None:
    """The same rule, end to end on importer output rather than a hand-built wall.

    RoomPlan reports surfaces with no thickness, so the importer supplies one
    and records that it did.  This is what stops the feature from being true
    only in a test.
    """

    model = import_captured_room(json.loads(ROOMPLAN_FIXTURE.read_text(encoding="utf-8")))
    scanned = [wall for wall in model.walls if wall.thickness_m == 0.001]
    assert scanned, "fixture no longer exercises a defaulted wall thickness"
    assert any(
        record.attributes.get("assumed_dimension") == "thickness_m"
        and record.derivation == DERIVATION_INFERRED
        for record in scanned[0].provenance
    )

    report = extract_quantities(model)
    assert _item(report, "wall_face_area", "wall", faces="single").derivation == DERIVATION_OBSERVED
    assert _item(report, "wall_volume", "wall").derivation == DERIVATION_INFERRED
    assert _item(report, "slab_area", "slab").derivation == DERIVATION_OBSERVED
    assert _item(report, "slab_volume", "slab").derivation == DERIVATION_INFERRED
