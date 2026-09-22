"""Tests for the conductor derivation lane.

The load-bearing claim under test is epistemic, not arithmetic: a plan that
says ``LP-1,3,5`` states circuits, so every conductor this lane produces is an
engineering inference and must say so.  Several tests below exist only to pin
that down (no invented wire sizes, no observed-from-source label on rule
output, no invented routes, no silent collapse of a shared raceway).

All inputs are synthetic and public-safe.  No PDF, no customer data, and no
paired expected-output document is fed back into any input.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.conductors import (
    ALGORITHM,
    CLASS_INFERRED,
    CLASS_OBSERVED,
    CLASS_USER_DESIGNED,
    RACEWAY_GROUP_KEY,
    ROLE_EQUIPMENT_GROUND,
    ROLE_LINE,
    ROLE_NEUTRAL,
    SOURCE_KIND,
    CircuitConductorOverride,
    ConductorDerivationError,
    ConductorDerivationOptions,
    EgcPolicy,
    NeutralPolicy,
    PolesMissingPolicy,
    TableSizingPolicy,
    derive_conductors,
)
from oabm.model import (
    BuildingModel,
    Circuit,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    Vector3,
    stable_id,
    validate_model,
)
from oabm.quantities import extract_quantities

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "conductors" / "v1" / "two-circuit-input-model.json"
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"

MODEL_ID = "model:conductor-test"
ROUTE_ID = "route:panel-load"
ROUTE_LENGTH_M = 7.0  # 1.0 up + 5.0 across + 1.0 down


def _ports() -> tuple[Port, ...]:
    return (
        Port(
            id="port:panel",
            owner_id="equipment:panel",
            domain="electrical",
            role="source",
            pose=Pose(position=Point3(x=0.0, y=0.0, z=1.5)),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
            nominal_diameter_m=0.021,
        ),
        Port(
            id="port:load",
            owner_id="device:load",
            domain="electrical",
            role="feed",
            pose=Pose(position=Point3(x=5.0, y=0.0, z=1.5)),
            direction=Vector3(x=-1.0, y=0.0, z=0.0),
            nominal_diameter_m=0.021,
        ),
    )


def _route() -> Route:
    return Route(
        id=ROUTE_ID,
        route_type="emt",
        start_port_id="port:panel",
        end_port_id="port:load",
        centerline=Polyline3D(
            points=(
                Point3(x=0.0, y=0.0, z=1.5),
                Point3(x=0.0, y=0.0, z=2.5),
                Point3(x=5.0, y=0.0, z=2.5),
                Point3(x=5.0, y=0.0, z=1.5),
            )
        ),
    )


def _circuit(
    circuit_id: str,
    *,
    poles: int | None = 1,
    voltage_v: float | None = 120.0,
    phase: str | None = "1ph",
    load_va: float | None = None,
    routed: bool = True,
    attributes: dict | None = None,
    confidence: float = 1.0,
) -> Circuit:
    return Circuit(
        id=circuit_id,
        source_port_id="port:panel",
        load_port_ids=("port:load",),
        circuit_number=circuit_id.rsplit(":", 1)[-1],
        voltage_v=voltage_v,
        poles=poles,
        phase=phase,
        load_va=load_va,
        route_ids=(ROUTE_ID,) if routed else (),
        confidence=confidence,
        attributes=attributes or {},
    )


def _model(*circuits: Circuit, model_id: str = MODEL_ID) -> BuildingModel:
    return BuildingModel(
        model_id=model_id,
        electrical_equipment=(
            ElectricalEquipment(
                id="equipment:panel",
                equipment_type="panelboard",
                pose=Pose(position=Point3(x=0.0, y=0.0, z=1.5)),
            ),
        ),
        electrical_devices=(
            ElectricalDevice(
                id="device:load",
                device_type="receptacle",
                pose=Pose(position=Point3(x=5.0, y=0.0, z=1.5)),
            ),
        ),
        ports=_ports(),
        routes=(_route(),),
        circuits=circuits,
    )


def _roles(conductors) -> list[tuple[str, str, int]]:
    return [(item.circuit_id, item.role, item.count) for item in conductors]


def _codes(warnings) -> list[str]:
    return [warning.code for warning in warnings]


def _derivation(conductor) -> dict:
    return conductor.attributes["derivation"]


# ---------------------------------------------------------------------------
# normal valid input
# ---------------------------------------------------------------------------


def test_single_pole_120v_circuit_derives_hot_neutral_and_ground():
    conductors, warnings = derive_conductors(_model(_circuit("circuit:2")))

    assert _roles(conductors) == [
        ("circuit:2", ROLE_LINE, 1),
        ("circuit:2", ROLE_NEUTRAL, 1),
        ("circuit:2", ROLE_EQUIPMENT_GROUND, 1),
    ]
    assert all(item.circuit_id == "circuit:2" for item in conductors)
    assert all(item.route_ids == (ROUTE_ID,) for item in conductors)
    assert "circuit-unrouted" not in _codes(warnings)


def test_two_pole_240v_circuit_derives_two_hots_and_no_neutral():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:4", poles=2, voltage_v=240.0))
    )

    assert _roles(conductors) == [
        ("circuit:4", ROLE_LINE, 2),
        ("circuit:4", ROLE_EQUIPMENT_GROUND, 1),
    ]
    # The 120/240 V 4-wire load that *does* carry a neutral is not
    # distinguishable from this model, so the gap is reported, not papered over.
    assert "neutral-policy-false-negative" in _codes(warnings)


def test_three_pole_circuit_derives_three_hots_and_no_neutral():
    conductors, _ = derive_conductors(
        _model(_circuit("circuit:7", poles=3, voltage_v=208.0, phase="3ph"))
    )
    assert _roles(conductors) == [
        ("circuit:7", ROLE_LINE, 3),
        ("circuit:7", ROLE_EQUIPMENT_GROUND, 1),
    ]


def test_egc_policy_never_models_a_legacy_install_without_a_ground():
    conductors, _ = derive_conductors(
        _model(_circuit("circuit:2")),
        ConductorDerivationOptions(egc_policy=EgcPolicy.NEVER),
    )
    assert ROLE_EQUIPMENT_GROUND not in {item.role for item in conductors}


def test_neutral_policy_never_suppresses_the_neutral():
    conductors, _ = derive_conductors(
        _model(_circuit("circuit:2")),
        ConductorDerivationOptions(neutral_policy=NeutralPolicy.NEVER),
    )
    assert ROLE_NEUTRAL not in {item.role for item in conductors}


# ---------------------------------------------------------------------------
# edge input: the signal the plan does not carry
# ---------------------------------------------------------------------------


def test_missing_poles_fails_closed_by_default():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:9", poles=None, voltage_v=None))
    )

    # No hot is invented, and the ground that code practice does imply is still
    # derived and still labelled inferred.
    assert _roles(conductors) == [("circuit:9", ROLE_EQUIPMENT_GROUND, 1)]
    assert "circuit-poles-unknown" in _codes(warnings)


def test_missing_poles_infer_from_voltage_is_opt_in_and_lower_confidence():
    model = _model(_circuit("circuit:9", poles=None, voltage_v=120.0))
    options = ConductorDerivationOptions(
        poles_missing=PolesMissingPolicy.INFER_FROM_VOLTAGE
    )
    conductors, warnings = derive_conductors(model, options)

    line = next(item for item in conductors if item.role == ROLE_LINE)
    assert line.count == 1
    assert line.confidence == pytest.approx(options.confidence_defaulted_inputs)
    assert "poles-missing:infer-from-voltage" in _derivation(line)["basis"]
    assert "circuit-poles-unknown" in _codes(warnings)


def test_missing_poles_infer_from_voltage_still_fails_without_voltage():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:9", poles=None, voltage_v=None)),
        ConductorDerivationOptions(poles_missing=PolesMissingPolicy.INFER_FROM_VOLTAGE),
    )
    assert ROLE_LINE not in {item.role for item in conductors}
    assert "circuit-poles-unknown" in _codes(warnings)


def test_missing_poles_assume_one_is_opt_in_and_labelled():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:9", poles=None, voltage_v=None)),
        ConductorDerivationOptions(poles_missing=PolesMissingPolicy.ASSUME_ONE),
    )
    line = next(item for item in conductors if item.role == ROLE_LINE)
    assert line.count == 1
    assert "poles-missing:assume-one" in _derivation(line)["basis"]
    assert "circuit-poles-unknown" in _codes(warnings)


def test_conductor_confidence_never_exceeds_its_circuit_confidence():
    conductors, _ = derive_conductors(_model(_circuit("circuit:2", confidence=0.25)))
    assert [item.confidence for item in conductors] == [0.25, 0.25, 0.25]


# ---------------------------------------------------------------------------
# routes: copied verbatim, never invented
# ---------------------------------------------------------------------------


def test_unrouted_circuit_still_derives_counts_but_no_path():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:2", routed=False))
    )

    assert len(conductors) == 3
    assert all(item.route_ids == () for item in conductors)
    assert "circuit-unrouted" in _codes(warnings)


def test_unrouted_derived_conductors_produce_no_length_downstream():
    model = _model(_circuit("circuit:2", routed=False))
    conductors, _ = derive_conductors(model)
    report = extract_quantities(replace(model, conductors=conductors))

    assert [item.category for item in report.items if item.category == "conductor_length"] == []
    assert sorted(
        warning.code for warning in report.warnings if warning.code == "unrouted_conductor"
    ) == ["unrouted_conductor"] * 3


def test_route_ids_are_copied_verbatim_and_no_geometry_is_created():
    model = _model(_circuit("circuit:2"))
    conductors, _ = derive_conductors(model)

    circuit = model.circuits[0]
    assert all(item.route_ids == circuit.route_ids for item in conductors)
    # Nothing in this lane may manufacture a path or a point.
    attached = replace(model, conductors=conductors)
    assert attached.routes == model.routes
    assert attached.route_fittings == model.route_fittings
    for conductor in conductors:
        assert "centerline" not in conductor.attributes
        assert "points" not in json.dumps(conductor.attributes)


def test_known_answer_length_multiplies_count_by_route_length():
    model = _model(_circuit("circuit:4", poles=2, voltage_v=240.0))
    conductors, _ = derive_conductors(model)
    report = extract_quantities(replace(model, conductors=conductors))

    line_items = [
        item
        for item in report.items
        if item.category == "conductor_length" and item.item_type == ROLE_LINE
    ]
    assert len(line_items) == 1
    assert line_items[0].quantity == pytest.approx(2 * ROUTE_LENGTH_M)


# ---------------------------------------------------------------------------
# the LP-1,3,5 case: a shared homerun is not shared conductors
# ---------------------------------------------------------------------------


def _shared_group_model(group: str = "homerun:lp1-1-3-5") -> BuildingModel:
    return _model(
        _circuit("circuit:1", attributes={RACEWAY_GROUP_KEY: group}),
        _circuit("circuit:3", attributes={"pdf_electrical": {RACEWAY_GROUP_KEY: group}}),
        _circuit("circuit:5", attributes={RACEWAY_GROUP_KEY: group}),
    )


def test_shared_homerun_does_not_collapse_conductors_by_default():
    conductors, warnings = derive_conductors(_shared_group_model())

    # Three circuits, three full sets: the callout evidences a shared path,
    # not a shared neutral, and guessing either way would be an invention.
    assert len(conductors) == 9
    assert {item.role for item in conductors} == {
        ROLE_LINE,
        ROLE_NEUTRAL,
        ROLE_EQUIPMENT_GROUND,
    }
    assert all(
        _derivation(item)["raceway_group"] == "homerun:lp1-1-3-5" for item in conductors
    )
    assert "shared-neutral-requires-explicit-config" in _codes(warnings)


def test_shared_conductors_configuration_collapses_one_neutral_onto_the_anchor():
    model = _shared_group_model()
    options = ConductorDerivationOptions(
        shared_conductors={"homerun:lp1-1-3-5": (ROLE_NEUTRAL,)}
    )
    conductors, warnings = derive_conductors(model, options)

    neutrals = [item for item in conductors if item.role == ROLE_NEUTRAL]
    assert len(neutrals) == 1
    assert neutrals[0].circuit_id == "circuit:1"
    assert _derivation(neutrals[0])["shared_with"] == ["circuit:3", "circuit:5"]
    assert "shared-neutral-requires-explicit-config" not in _codes(warnings)

    report = extract_quantities(replace(model, conductors=conductors))
    neutral_items = [
        item
        for item in report.items
        if item.category == "conductor_length" and item.item_type == ROLE_NEUTRAL
    ]
    assert neutral_items[0].quantity == pytest.approx(ROUTE_LENGTH_M)


def test_shared_conductors_for_an_undeclared_group_is_rejected():
    with pytest.raises(ConductorDerivationError, match="no circuit declares"):
        derive_conductors(
            _shared_group_model(),
            ConductorDerivationOptions(shared_conductors={"homerun:absent": (ROLE_NEUTRAL,)}),
        )


def test_conflicting_raceway_group_values_on_one_circuit_are_rejected():
    circuit = _circuit(
        "circuit:1",
        attributes={
            RACEWAY_GROUP_KEY: "homerun:a",
            "pdf_electrical": {RACEWAY_GROUP_KEY: "homerun:b"},
        },
    )
    with pytest.raises(ConductorDerivationError, match="conflicting"):
        derive_conductors(_model(circuit))


# ---------------------------------------------------------------------------
# sizing: never a plausible-looking guess
# ---------------------------------------------------------------------------


def test_size_material_and_insulation_are_null_and_warned_by_default():
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:2", load_va=1800.0))
    )

    assert all(item.size is None for item in conductors)
    assert all(item.material is None for item in conductors)
    assert all(item.insulation is None for item in conductors)
    assert "size-undetermined" in _codes(warnings)


def test_table_sizing_policy_is_opt_in_and_leaves_the_ground_unsized():
    policy = TableSizingPolicy(
        policy_id="synthetic-known-answer",
        ampacity_table=((20.0, "SIZE-A"), (30.0, "SIZE-B"), (55.0, "SIZE-C")),
    )
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:2", load_va=1800.0)),
        ConductorDerivationOptions(sizing=policy),
    )
    by_role = {item.role: item for item in conductors}

    # 1800 VA / 120 V = 15 A -> the first table entry that carries it.
    assert by_role[ROLE_LINE].size == "SIZE-A"
    assert by_role[ROLE_NEUTRAL].size == "SIZE-A"
    assert f"table-sizing:{policy.policy_id}" in _derivation(by_role[ROLE_LINE])["basis"]
    # Equipment grounding conductors are sized from the overcurrent device and
    # the canonical contract has no field for it, so the size stays null.
    assert by_role[ROLE_EQUIPMENT_GROUND].size is None
    assert "size-undetermined" in _codes(warnings)


def test_table_sizing_policy_declines_without_a_load():
    policy = TableSizingPolicy(
        policy_id="synthetic-known-answer", ampacity_table=((20.0, "SIZE-A"),)
    )
    conductors, warnings = derive_conductors(
        _model(_circuit("circuit:2", load_va=None)),
        ConductorDerivationOptions(sizing=policy),
    )
    assert all(item.size is None for item in conductors)
    assert "size-undetermined" in _codes(warnings)


def test_table_sizing_policy_declines_for_polyphase_circuits():
    policy = TableSizingPolicy(
        policy_id="synthetic-known-answer", ampacity_table=((100.0, "SIZE-D"),)
    )
    conductors, _ = derive_conductors(
        _model(
            _circuit("circuit:7", poles=3, voltage_v=208.0, phase="3ph", load_va=10000.0)
        ),
        ConductorDerivationOptions(sizing=policy),
    )
    assert all(item.size is None for item in conductors)


def test_sizing_table_must_ascend_strictly():
    with pytest.raises(ConductorDerivationError, match="ascend"):
        TableSizingPolicy(
            policy_id="bad", ampacity_table=((30.0, "SIZE-B"), (20.0, "SIZE-A"))
        )


def test_option_defaults_for_material_are_labelled_user_designed():
    conductors, _ = derive_conductors(
        _model(_circuit("circuit:2")),
        ConductorDerivationOptions(
            default_material="copper", default_insulation="THHN/THWN-2"
        ),
    )
    line = next(item for item in conductors if item.role == ROLE_LINE)
    assert line.material == "copper"
    assert _derivation(line)["facts"]["material"] == CLASS_USER_DESIGNED
    # The count is still an inference, so the record as a whole stays inferred.
    assert _derivation(line)["derivation_class"] == CLASS_INFERRED


# ---------------------------------------------------------------------------
# provenance: the point of the task
# ---------------------------------------------------------------------------


def test_every_rule_derived_conductor_is_labelled_inferred():
    models = (
        _model(_circuit("circuit:2")),
        _model(_circuit("circuit:4", poles=2, voltage_v=240.0)),
        _shared_group_model(),
        _model(_circuit("circuit:9", poles=None, voltage_v=None)),
    )
    for model in models:
        conductors, _ = derive_conductors(model)
        assert conductors
        for conductor in conductors:
            derivation = _derivation(conductor)
            assert derivation["derivation_class"] == CLASS_INFERRED
            assert derivation["facts"]["count"] == CLASS_INFERRED
            assert derivation["algorithm"] == ALGORITHM
            assert [item.source_kind for item in conductor.provenance] == [SOURCE_KIND]
            assert all(
                item.attributes["derivation_class"] != CLASS_OBSERVED
                for item in conductor.provenance
            )


def test_observed_override_pins_facts_and_keeps_the_caller_provenance():
    schedule = Provenance(
        source_kind="panel-schedule",
        source_id="sheet:E1.1",
        source_element_id="row:2",
        page=3,
        method="hand-entered-schedule-row",
        confidence=0.95,
    )
    override = CircuitConductorOverride(
        derivation_class=CLASS_OBSERVED,
        provenance=(schedule,),
        material="copper",
        size="SIZE-A",
        insulation="THHN/THWN-2",
        counts={ROLE_LINE: 1, ROLE_NEUTRAL: 1, ROLE_EQUIPMENT_GROUND: 1},
    )
    conductors, _ = derive_conductors(
        _model(_circuit("circuit:2")),
        ConductorDerivationOptions(overrides={"circuit:2": override}),
    )
    line = next(item for item in conductors if item.role == ROLE_LINE)

    assert line.size == "SIZE-A"
    assert line.material == "copper"
    assert _derivation(line)["derivation_class"] == CLASS_OBSERVED
    assert _derivation(line)["facts"]["count"] == CLASS_OBSERVED
    assert schedule in line.provenance
    assert line.confidence == pytest.approx(0.95)


def test_an_observed_override_must_cite_provenance():
    with pytest.raises(ConductorDerivationError, match="cite provenance"):
        CircuitConductorOverride(derivation_class=CLASS_OBSERVED)


def test_override_class_outside_the_vocabulary_is_rejected():
    with pytest.raises(ConductorDerivationError, match="derivation_class"):
        CircuitConductorOverride(derivation_class="observed")


def test_override_role_outside_the_vocabulary_is_rejected():
    with pytest.raises(ConductorDerivationError, match="outside the v1 vocabulary"):
        CircuitConductorOverride(
            derivation_class=CLASS_USER_DESIGNED, counts={"hot": 2}
        )


def test_override_for_an_unknown_circuit_is_rejected():
    override = CircuitConductorOverride(derivation_class=CLASS_USER_DESIGNED)
    with pytest.raises(ConductorDerivationError, match="unknown circuit"):
        derive_conductors(
            _model(_circuit("circuit:2")),
            ConductorDerivationOptions(overrides={"circuit:404": override}),
        )


def test_override_conflicting_with_a_shared_declaration_warns_and_does_not_double_count():
    override = CircuitConductorOverride(
        derivation_class=CLASS_USER_DESIGNED, counts={ROLE_NEUTRAL: 1}
    )
    conductors, warnings = derive_conductors(
        _shared_group_model(),
        ConductorDerivationOptions(
            shared_conductors={"homerun:lp1-1-3-5": (ROLE_NEUTRAL,)},
            overrides={"circuit:3": override},
        ),
    )
    assert len([item for item in conductors if item.role == ROLE_NEUTRAL]) == 1
    assert "override-conflict" in _codes(warnings)


# ---------------------------------------------------------------------------
# invalid or broken references
# ---------------------------------------------------------------------------


def test_broken_route_reference_raises_instead_of_emitting_output():
    model = _model(_circuit("circuit:2"))
    # Bypass the frozen dataclass to simulate a model assembled outside the
    # contract; the lane must refuse rather than emit a dangling reference.
    object.__setattr__(model, "routes", ())
    with pytest.raises(ConductorDerivationError, match="not derivable"):
        derive_conductors(model)


def test_non_model_input_is_rejected():
    with pytest.raises(ConductorDerivationError, match="BuildingModel"):
        derive_conductors({"model_id": "model:not-a-model"})


def test_bad_options_type_is_rejected():
    with pytest.raises(ConductorDerivationError, match="ConductorDerivationOptions"):
        derive_conductors(_model(_circuit("circuit:2")), options={"egc_policy": "always"})


def test_confidence_bands_must_be_ordered():
    with pytest.raises(ConductorDerivationError, match="must not exceed"):
        ConductorDerivationOptions(
            confidence_full_inputs=0.3, confidence_defaulted_inputs=0.9
        )


# ---------------------------------------------------------------------------
# determinism, identity, serialization
# ---------------------------------------------------------------------------


def test_repeated_derivation_is_byte_identical_and_order_independent():
    circuits = (
        _circuit("circuit:1"),
        _circuit("circuit:3", poles=2, voltage_v=240.0),
        _circuit("circuit:5"),
    )
    forward = _model(*circuits)
    reversed_model = _model(*reversed(circuits))

    first, first_warnings = derive_conductors(forward)
    second, _ = derive_conductors(forward)
    shuffled, shuffled_warnings = derive_conductors(reversed_model)

    def dump(conductors):
        return json.dumps(
            [replace(forward, conductors=conductors).to_dict()["conductors"]], sort_keys=True
        )

    assert dump(first) == dump(second)
    assert dump(first) == dump(shuffled)
    assert [item.to_dict() for item in first_warnings] == [
        item.to_dict() for item in shuffled_warnings
    ]


def test_ids_are_the_recomputed_stable_id():
    conductors, _ = derive_conductors(_model(_circuit("circuit:2")))
    for conductor in conductors:
        assert conductor.id == stable_id(
            "conductor", f"{MODEL_ID}:{conductor.circuit_id}:{conductor.role}"
        )
    assert len({item.id for item in conductors}) == len(conductors)


def test_ids_change_with_the_model_but_not_between_runs():
    one, _ = derive_conductors(_model(_circuit("circuit:2")))
    other, _ = derive_conductors(_model(_circuit("circuit:2"), model_id="model:other"))
    assert {item.id for item in one}.isdisjoint({item.id for item in other})


def test_attached_model_validates_round_trips_and_matches_the_schema():
    model = _model(_circuit("circuit:2"), _circuit("circuit:4", poles=2, voltage_v=240.0))
    conductors, _ = derive_conductors(model)
    attached = replace(model, conductors=conductors)

    validate_model(attached)
    restored = BuildingModel.from_json(attached.to_json())
    assert restored.to_json() == attached.to_json()
    assert restored.conductors == attached.conductors

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(attached.to_dict())


def test_derived_conductor_route_ids_are_a_subset_of_their_circuit_routes():
    model = _model(_circuit("circuit:2"), _circuit("circuit:4", routed=False))
    conductors, _ = derive_conductors(model)
    by_circuit = {circuit.id: circuit for circuit in model.circuits}
    for conductor in conductors:
        assert set(conductor.route_ids) <= set(by_circuit[conductor.circuit_id].route_ids)


def test_deriving_twice_over_an_already_attached_model_is_rejected():
    model = _model(_circuit("circuit:2"))
    conductors, _ = derive_conductors(model)
    attached = replace(model, conductors=conductors)
    with pytest.raises(ConductorDerivationError, match="collides"):
        derive_conductors(attached)


# ---------------------------------------------------------------------------
# public-safe fixture known answer
# ---------------------------------------------------------------------------


def test_fixture_model_derives_a_known_answer():
    model = BuildingModel.load(FIXTURE)
    assert model.conductors == ()  # the fixture is input only

    conductors, warnings = derive_conductors(model)

    assert _roles(conductors) == [
        ("circuit:lp1-2", ROLE_LINE, 1),
        ("circuit:lp1-2", ROLE_NEUTRAL, 1),
        ("circuit:lp1-2", ROLE_EQUIPMENT_GROUND, 1),
        ("circuit:lp1-4", ROLE_LINE, 2),
        ("circuit:lp1-4", ROLE_EQUIPMENT_GROUND, 1),
    ]
    assert [item.route_ids for item in conductors] == [
        ("route:panel-receptacle",),
        ("route:panel-receptacle",),
        ("route:panel-receptacle",),
        (),
        (),
    ]
    assert all(item.size is None for item in conductors)
    assert sorted(set(_codes(warnings))) == [
        "circuit-unrouted",
        "neutral-policy-false-negative",
        "size-undetermined",
    ]
    validate_model(replace(model, conductors=conductors))
