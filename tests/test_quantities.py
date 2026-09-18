from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import BuildingModel, ContractError, ElectricalDevice, Point3, Pose, Size3
from oabm.quantities import COUNT_UNIT, LENGTH_UNIT, QuantityError, extract_quantities

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


def test_empty_model_has_empty_takeoff() -> None:
    report = extract_quantities(BuildingModel(model_id="model:empty"))
    assert report.items == ()
    assert report.warnings == ()
