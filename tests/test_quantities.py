import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.model import (
    BuildingModel,
    ContractError,
    Point3,
    Polyline3D,
    Route,
)
from oabm.quantities import QuantityError, extract_quantities

ROOT = Path(__file__).resolve().parents[1]
MODEL_FIXTURE = ROOT / "fixtures" / "quantities" / "v1" / "known-answer-model.json"
REPORT_FIXTURE = ROOT / "fixtures" / "quantities" / "v1" / "known-answer-report.json"
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"


def _model() -> BuildingModel:
    return BuildingModel.load(MODEL_FIXTURE)


def _item(report, *, category: str, measure: str, role: str | None = None, item_type: str | None = None):
    matches = []
    for item in report.items:
        props = dict(item.properties)
        if item.category != category or item.measure != measure:
            continue
        if role is not None and props.get("role") != role:
            continue
        if item_type is not None and item.item_type != item_type:
            continue
        matches.append(item)
    assert len(matches) == 1
    return matches[0]


def test_known_answer_fixture_matches_checked_in_report() -> None:
    report = extract_quantities(_model())
    expected = json.loads(REPORT_FIXTURE.read_text(encoding="utf-8"))
    assert report.to_dict() == expected

    assert _item(report, category="route", measure="length", item_type="emt").quantity == 7.0
    assert _item(report, category="fitting", measure="count", item_type="elbow-90").quantity == 2
    assert _item(report, category="conductor", measure="count", role="line").quantity == 2
    assert _item(report, category="conductor", measure="length", role="line").quantity == 14.0
    assert _item(report, category="conductor", measure="length", role="equipment-ground").quantity == 7.0
    assert _item(report, category="box", measure="count", item_type="junction-box").quantity == 1
    assert _item(report, category="device", measure="count", item_type="evse").quantity == 1
    assert _item(report, category="equipment", measure="count", item_type="panelboard").quantity == 1


def test_quantity_fixture_is_valid_canonical_schema_and_cross_references() -> None:
    document = json.loads(MODEL_FIXTURE.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)

    model = BuildingModel.from_dict(document)
    assert BuildingModel.from_json(model.to_json()).to_dict() == model.to_dict()


def test_units_are_explicit_and_consistent() -> None:
    report = extract_quantities(_model())
    assert report.length_unit == "m"
    assert report.count_unit == "ea"
    for item in report.items:
        assert item.unit == ("m" if item.measure == "length" else "ea")


def test_repeatability_is_independent_of_collection_order() -> None:
    model = _model()
    first = extract_quantities(model).to_dict()
    reordered = replace(
        model,
        routes=tuple(reversed(model.routes)),
        route_fittings=tuple(reversed(model.route_fittings)),
        conductors=tuple(reversed(model.conductors)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
    )
    assert extract_quantities(reordered).to_dict() == first
    for _ in range(10):
        assert extract_quantities(model).to_dict() == first


def test_duplicate_conductor_route_references_do_not_double_count() -> None:
    model = _model()
    line = next(item for item in model.conductors if item.id == "conductor:lines")
    duplicate = replace(
        line,
        route_ids=("route:panel-jbox", "route:panel-jbox", "route:jbox-evse", "route:jbox-evse"),
    )
    duplicated_model = replace(
        model,
        conductors=tuple(duplicate if item.id == duplicate.id else item for item in model.conductors),
    )
    report = extract_quantities(duplicated_model)
    assert _item(report, category="conductor", measure="length", role="line").quantity == 14.0


def test_duplicate_route_fitting_reference_does_not_double_count_fitting_entity() -> None:
    model = _model()
    route = next(item for item in model.routes if item.id == "route:panel-jbox")
    duplicate = replace(route, fitting_ids=("fitting:panel-rise", "fitting:panel-rise"))
    duplicated_model = replace(
        model,
        routes=tuple(duplicate if item.id == duplicate.id else item for item in model.routes),
    )
    report = extract_quantities(duplicated_model)
    assert _item(report, category="fitting", measure="count", item_type="elbow-90").quantity == 2


def test_malformed_reference_is_revalidated_at_extraction_boundary() -> None:
    model = _model()
    conductor = next(item for item in model.conductors if item.id == "conductor:lines")
    object.__setattr__(conductor, "route_ids", ("route:missing",))
    with pytest.raises(ContractError, match="references missing id"):
        extract_quantities(model)


def test_empty_model_has_empty_report() -> None:
    report = extract_quantities(BuildingModel(model_id="model:empty"))
    assert report.items == ()
    assert report.to_dict() == {
        "model_id": "model:empty",
        "schema_version": "1.0.0",
        "length_unit": "m",
        "count_unit": "ea",
        "items": [],
    }


def test_cable_route_uses_canonical_route_centerline_without_special_geometry() -> None:
    model = _model()
    start = next(port for port in model.ports if port.id == "port:panel-load")
    end = next(port for port in model.ports if port.id == "port:jbox-in")
    cable = Route(
        id="route:data-cable",
        route_type="cable",
        start_port_id=start.id,
        end_port_id=end.id,
        centerline=Polyline3D(
            points=(start.pose.position, Point3(x=0, y=0, z=2), end.pose.position)
        ),
    )
    cable_model = replace(model, routes=(*model.routes, cable))
    item = _item(
        extract_quantities(cable_model),
        category="route",
        measure="length",
        item_type="cable",
    )
    assert item.quantity == 4.0
    assert item.unit == "m"
    assert item.source_ids == ("route:data-cable",)


def test_conductor_without_explicit_route_emits_count_but_not_inferred_length() -> None:
    model = _model()
    line = next(item for item in model.conductors if item.id == "conductor:lines")
    unrouted = replace(line, route_ids=())
    updated = replace(
        model,
        conductors=tuple(unrouted if item.id == unrouted.id else item for item in model.conductors),
    )
    report = extract_quantities(updated)
    assert _item(report, category="conductor", measure="count", role="line").quantity == 2
    assert not [
        item
        for item in report.items
        if item.category == "conductor"
        and item.measure == "length"
        and dict(item.properties).get("role") == "line"
    ]


def test_provenance_and_assembly_hook_are_preserved() -> None:
    report = extract_quantities(_model())
    evse = _item(report, category="device", measure="count", item_type="evse")
    assert evse.assembly_key == "evse-wall"
    assert evse.source_ids == ("device:evse",)
    assert [(p.source_kind, p.source_id, p.source_element_id) for p in evse.provenance] == [
        ("synthetic", "fixture:quantity-known-answer-v1", "evse")
    ]


def test_invalid_assembly_key_is_rejected_deterministically() -> None:
    model = _model()
    evse = next(item for item in model.electrical_devices if item.id == "device:evse")
    bad = replace(evse, attributes={"assembly_key": 123})
    bad_model = replace(
        model,
        electrical_devices=tuple(bad if item.id == bad.id else item for item in model.electrical_devices),
    )
    with pytest.raises(QuantityError, match="assembly_key"):
        extract_quantities(bad_model)
