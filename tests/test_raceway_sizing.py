"""Tests for the raceway/cable sizing lane.

The load-bearing claim under test is epistemic, not arithmetic: a drawing that
shows a homerun states a path, so every raceway this lane selects is an
engineering inference and must say so.  Several tests below exist only to pin
that down (no assumed fill percentage, no assumed trade size, no assumed
raceway kind, no observed label on rule output, no overwriting a dimension a
source stated, no geometry edits).

All table numbers in this file are SYNTHETIC and chosen to make each test
discriminate.  They are deliberately not real conduit areas or real fill
percentages: this lane ships no code table, and its tests must not smuggle one
in either.  Designations are opaque tokens ("SIZE-A", "T1", "INSUL-X") for the
same reason, matching the conductor lane's "synthetic-known-answer" convention.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    BuildingModel,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    Vector3,
    is_observed,
    validate_model,
)
from oabm.raceways import (
    ALGORITHM,
    KIND_CABLE,
    KIND_CONDUIT,
    RACEWAY_ATTRIBUTE_KEY,
    SOURCE_KIND,
    UNRESOLVED_CABLE_NOT_IN_CATALOG,
    UNRESOLVED_CONDUCTOR_DIMENSION_UNKNOWN,
    UNRESOLVED_CONDUCTOR_INSULATION_UNKNOWN,
    UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN,
    UNRESOLVED_FILL_LIMIT_UNSTATED,
    UNRESOLVED_MIXED_CABLE_SIZES,
    UNRESOLVED_NO_CONDUCTORS,
    UNRESOLVED_NO_DIMENSION_TABLE,
    UNRESOLVED_NO_FILL_RULE,
    UNRESOLVED_NO_TRADE_SIZE_CARRIES_FILL,
    UNRESOLVED_RACEWAY_TYPE_UNKNOWN,
    CableCatalog,
    CableEntry,
    ConductorDimensionTable,
    FillRule,
    RacewayCatalog,
    RacewayObservation,
    RacewaySizingError,
    RacewaySizingOptions,
    TradeSize,
    apply_raceway_sizing,
    size_raceways,
)

MODEL_ID = "model:raceway-test"
ROUTE_ID = "route:panel-load"
CIRCUIT_ID = "circuit:2"

# --- synthetic reference tables -------------------------------------------
# Areas are round numbers so the expected selection is checkable by hand.
AREA_A = 1.0e-5
AREA_B = 4.0e-5
AREA_C = 2.5e-5

DIMENSIONS = ConductorDimensionTable(
    table_id="synthetic-known-answer",
    source="synthetic test table; not a published standard",
    entries=(
        (("INSUL-X", "SIZE-A"), AREA_A),
        (("INSUL-X", "SIZE-B"), AREA_B),
        (("INSUL-X", "SIZE-C"), AREA_C),
    ),
)

# Exact rows for 1 and 2 conductors, an "over 2" row for the rest: the shape
# published fill tables actually take.
FILL = FillRule(
    rule_id="synthetic-known-answer",
    source="synthetic test rule; not a published standard",
    exact_limits=((1, 0.5), (2, 0.3)),
    over_limit=(2, 0.4),
)

T1_INTERIOR = 1.0e-4
T2_INTERIOR = 2.0e-4
T3_INTERIOR = 4.0e-4

CONDUIT = RacewayCatalog(
    catalog_id="synthetic-conduit",
    source="synthetic test catalog; not a published standard",
    trade_sizes=(
        TradeSize(designation="T1", interior_area_m2=T1_INTERIOR, nominal_diameter_m=0.012),
        TradeSize(designation="T2", interior_area_m2=T2_INTERIOR, nominal_diameter_m=0.018),
        TradeSize(designation="T3", interior_area_m2=T3_INTERIOR, nominal_diameter_m=0.024),
    ),
)

CABLE = CableCatalog(
    catalog_id="synthetic-cable",
    source="synthetic test catalog; not a published standard",
    entries=(
        CableEntry(
            designation="CABLE-3xA",
            conductor_count=3,
            conductor_size="SIZE-A",
            outside_diameter_m=0.011,
        ),
    ),
)


def _options(**kwargs) -> RacewaySizingOptions:
    base = {
        "catalogs": {"emt": CONDUIT, "mc": CABLE},
        "dimensions": DIMENSIONS,
        "fill_rule": FILL,
    }
    base.update(kwargs)
    return RacewaySizingOptions(**base)


def _ports() -> tuple[Port, ...]:
    return (
        Port(
            id="port:panel",
            owner_id="equipment:panel",
            domain="electrical",
            role="source",
            pose=Pose(position=Point3(x=0.0, y=0.0, z=1.5)),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
        ),
        Port(
            id="port:load",
            owner_id="device:load",
            domain="electrical",
            role="feed",
            pose=Pose(position=Point3(x=5.0, y=0.0, z=1.5)),
            direction=Vector3(x=-1.0, y=0.0, z=0.0),
        ),
    )


def _route(
    *,
    route_type: str = "emt",
    nominal_diameter_m: float | None = None,
    attributes: dict | None = None,
) -> Route:
    return Route(
        id=ROUTE_ID,
        route_type=route_type,
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
        nominal_diameter_m=nominal_diameter_m,
        attributes=attributes or {},
    )


def _conductor(
    name: str,
    *,
    size: str | None = "SIZE-A",
    insulation: str | None = "INSUL-X",
    count: int = 1,
    routed: bool = True,
    confidence: float = 1.0,
) -> Conductor:
    return Conductor(
        id=f"conductor:{name}",
        circuit_id=CIRCUIT_ID,
        role="line",
        material="copper",
        size=size,
        insulation=insulation,
        count=count,
        route_ids=(ROUTE_ID,) if routed else (),
        confidence=confidence,
    )


def _model(*conductors: Conductor, route: Route | None = None) -> BuildingModel:
    return BuildingModel(
        model_id=MODEL_ID,
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
        routes=(route if route is not None else _route(),),
        circuits=(
            Circuit(
                id=CIRCUIT_ID,
                source_port_id="port:panel",
                load_port_ids=("port:load",),
                circuit_number="2",
                voltage_v=120.0,
                poles=1,
                phase="1ph",
                route_ids=(ROUTE_ID,),
            ),
        ),
        conductors=conductors,
    )


def _verdict(route: Route) -> dict:
    return route.attributes[RACEWAY_ATTRIBUTE_KEY]


def _only(routes) -> Route:
    assert len(routes) == 1
    return routes[0]


def _codes(warnings) -> list[str]:
    return [warning.code for warning in warnings]


# ---------------------------------------------------------------------------
# the clean case
# ---------------------------------------------------------------------------


def test_conduit_route_sizes_to_the_smallest_trade_size_that_carries_the_fill():
    """Five SIZE-A conductors need 5.0e-5 m2 at the 'over 2' limit of 0.4.

    T1 allows 1.0e-4 * 0.4 = 4.0e-5 -- too small.
    T2 allows 2.0e-4 * 0.4 = 8.0e-5 -- the smallest that carries it.

    This bites twice: an implementation that ignored the fill limit and
    compared against raw interior area would pick T1 (1.0e-4 >= 5.0e-5), and
    one that took the last rather than the first fitting size would pick T3.
    """

    model = _model(*(_conductor(name) for name in "abcde"))
    routes, warnings = size_raceways(model, _options())
    verdict = _verdict(_only(routes))

    assert verdict["resolved"] is True
    assert verdict["kind"] == KIND_CONDUIT
    assert verdict["designation"] == "T2"
    assert verdict["conductor_count"] == 5
    assert verdict["required_area_m2"] == pytest.approx(5.0e-5)
    assert verdict["fill_limit_fraction"] == pytest.approx(0.4)
    assert verdict["algorithm"] == ALGORITHM
    assert warnings == ()


def test_the_exact_conductor_count_row_wins_over_the_over_count_row():
    """Two conductors (SIZE-A + SIZE-C) need 3.5e-5 m2 and take the exact row.

    At the exact-2 limit of 0.3: T1 allows 3.0e-5 (too small), T2 allows
    6.0e-5 -> T2.  At the 'over 2' limit of 0.4 T1 would allow 4.0e-5 and win.
    So an implementation that reached for the over-count row, or that rounded
    to the nearest stated row, returns T1 and this test fails.
    """

    model = _model(_conductor("a", size="SIZE-A"), _conductor("c", size="SIZE-C"))
    routes, _ = size_raceways(model, _options())
    verdict = _verdict(_only(routes))

    assert verdict["designation"] == "T2"
    assert verdict["fill_limit_fraction"] == pytest.approx(0.3)


def test_a_conductor_count_multiplies_its_area_and_the_fill_count():
    """count=5 on one conductor must size the same as five conductors."""

    single = _model(_conductor("a", count=5))
    many = _model(*(_conductor(name) for name in "abcde"))

    one_verdict = _verdict(_only(size_raceways(single, _options()).routes))
    many_verdict = _verdict(_only(size_raceways(many, _options()).routes))

    assert one_verdict["conductor_count"] == 5
    assert one_verdict["designation"] == many_verdict["designation"]
    assert one_verdict["required_area_m2"] == pytest.approx(
        many_verdict["required_area_m2"]
    )


def test_the_chosen_trade_size_diameter_is_written_when_the_catalog_states_one():
    model = _model(*(_conductor(name) for name in "abcde"))
    route = _only(size_raceways(model, _options()).routes)
    assert route.nominal_diameter_m == pytest.approx(0.018)  # T2


def test_no_diameter_is_invented_when_the_catalog_states_none():
    """Interior area and nominal diameter are different published quantities."""

    catalog = RacewayCatalog(
        catalog_id="areas-only",
        source="synthetic test catalog; not a published standard",
        trade_sizes=(TradeSize(designation="T1", interior_area_m2=T1_INTERIOR),),
    )
    model = _model(_conductor("a"))
    route = _only(
        size_raceways(model, _options(catalogs={"emt": catalog})).routes
    )
    assert _verdict(route)["designation"] == "T1"
    assert route.nominal_diameter_m is None


# ---------------------------------------------------------------------------
# refusals: every one of these must record a reason, not pick a default
# ---------------------------------------------------------------------------


def _assert_refused(route: Route, reason: str) -> None:
    verdict = _verdict(route)
    assert verdict["resolved"] is False
    assert verdict["unresolved_reason"] == reason
    assert verdict["detail"]
    assert "designation" not in verdict
    assert route.nominal_diameter_m is None


def test_unknown_route_type_is_refused_rather_than_guessed_from_the_token():
    """``route_type`` is a free string; 'flex' could be conduit or cable."""

    model = _model(_conductor("a"), route=_route(route_type="flex"))
    routes, warnings = size_raceways(model, _options())

    _assert_refused(_only(routes), UNRESOLVED_RACEWAY_TYPE_UNKNOWN)
    assert UNRESOLVED_RACEWAY_TYPE_UNKNOWN in _codes(warnings)


def test_conductor_without_a_size_refuses_the_route():
    """The conductor lane leaves an undeterminable size null; so do we."""

    model = _model(_conductor("a"), _conductor("b", size=None))
    routes, warnings = size_raceways(model, _options())

    _assert_refused(_only(routes), UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN)
    assert "conductor:b" in _only(routes).attributes[RACEWAY_ATTRIBUTE_KEY]["detail"]
    assert UNRESOLVED_CONDUCTOR_SIZE_UNKNOWN in _codes(warnings)


def test_conductor_without_insulation_refuses_the_route():
    """Outer area is a property of the insulation system, not of the copper."""

    model = _model(_conductor("a", insulation=None))
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_CONDUCTOR_INSULATION_UNKNOWN)


def test_conductor_absent_from_the_dimension_table_is_refused():
    model = _model(_conductor("a", size="SIZE-Z"))
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_CONDUCTOR_DIMENSION_UNKNOWN)


def test_missing_dimension_table_refuses_rather_than_estimating_an_area():
    model = _model(_conductor("a"))
    routes, _ = size_raceways(model, _options(dimensions=None))
    _assert_refused(_only(routes), UNRESOLVED_NO_DIMENSION_TABLE)


def test_missing_fill_rule_refuses_rather_than_assuming_a_fill_percentage():
    """There is no built-in 40%.  Without a stated rule the route is refused."""

    model = _model(*(_conductor(name) for name in "abcde"))
    routes, _ = size_raceways(model, _options(fill_rule=None))
    _assert_refused(_only(routes), UNRESOLVED_NO_FILL_RULE)


def test_a_conductor_count_the_fill_rule_does_not_cover_is_refused():
    """No rounding to the nearest stated row."""

    sparse = FillRule(
        rule_id="one-row-only",
        source="synthetic test rule; not a published standard",
        exact_limits=((1, 0.5),),
    )
    model = _model(_conductor("a"), _conductor("b"), _conductor("c"))
    routes, _ = size_raceways(model, _options(fill_rule=sparse))
    _assert_refused(_only(routes), UNRESOLVED_FILL_LIMIT_UNSTATED)


def test_a_bundle_no_trade_size_carries_is_refused_not_clamped_to_the_largest():
    model = _model(_conductor("a", size="SIZE-B", count=20))
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_NO_TRADE_SIZE_CARRIES_FILL)


def test_a_route_with_no_conductors_is_refused_not_given_a_minimum_size():
    model = _model(_conductor("a", routed=False))
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_NO_CONDUCTORS)


# ---------------------------------------------------------------------------
# cable runs
# ---------------------------------------------------------------------------


def test_cable_route_selects_the_listed_assembly():
    model = _model(
        *(_conductor(name) for name in "abc"), route=_route(route_type="mc")
    )
    routes, warnings = size_raceways(model, _options())
    verdict = _verdict(_only(routes))

    assert verdict["resolved"] is True
    assert verdict["kind"] == KIND_CABLE
    assert verdict["designation"] == "CABLE-3xA"
    assert verdict["conductor_count"] == 3
    # A cable is an assembly, not a fill calculation.
    assert "fill_limit_fraction" not in verdict
    assert warnings == ()


def test_cable_not_in_the_catalog_is_refused_not_substituted_upward():
    """A 2-conductor run must not be served the listed 3-conductor cable."""

    model = _model(
        _conductor("a"), _conductor("b"), route=_route(route_type="mc")
    )
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_CABLE_NOT_IN_CATALOG)


def test_cable_with_mixed_conductor_sizes_is_refused():
    model = _model(
        _conductor("a", size="SIZE-A"),
        _conductor("b", size="SIZE-B"),
        _conductor("c", size="SIZE-A"),
        route=_route(route_type="mc"),
    )
    routes, _ = size_raceways(model, _options())
    _assert_refused(_only(routes), UNRESOLVED_MIXED_CABLE_SIZES)


def test_a_cable_run_needs_no_fill_rule_or_dimension_table():
    """Cable sizing must not be blocked by conduit-only inputs being absent."""

    model = _model(
        *(_conductor(name) for name in "abc"), route=_route(route_type="mc")
    )
    routes, _ = size_raceways(
        model, _options(dimensions=None, fill_rule=None)
    )
    assert _verdict(_only(routes))["designation"] == "CABLE-3xA"


# ---------------------------------------------------------------------------
# provenance: the point of the task
# ---------------------------------------------------------------------------


def test_every_rule_derived_sizing_is_inferred_and_never_observed():
    """The central claim.  Resolved or refused, rule output is an inference."""

    models = (
        _model(*(_conductor(name) for name in "abcde")),  # resolved conduit
        _model(*(_conductor(name) for name in "abc"), route=_route(route_type="mc")),
        _model(_conductor("a"), route=_route(route_type="flex")),  # refused
        _model(_conductor("a", size=None)),  # refused
    )
    for model in models:
        routes, _ = size_raceways(model, _options())
        route = _only(routes)
        own = [item for item in route.provenance if item.source_kind == SOURCE_KIND]

        assert len(own) == 1
        # Asserted against the CANONICAL vocabulary, not this lane's aliases:
        # a test that read the module's own CLASS_INFERRED alias would still pass if
        # that constant were redefined to "observed", which is the exact bug
        # this test exists to catch.
        assert own[0].derivation == DERIVATION_INFERRED
        assert own[0].derivation != DERIVATION_OBSERVED
        assert own[0].method == ALGORITHM
        # And nothing in the record may render as seen-in-the-source.
        assert is_observed(route.provenance) is False
        verdict = _verdict(route)
        assert verdict["derivation_class"] == DERIVATION_INFERRED
        assert verdict["as_built"] is False


def test_a_resolved_sizing_cites_the_source_of_every_table_it_used():
    model = _model(*(_conductor(name) for name in "abcde"))
    verdict = _verdict(_only(size_raceways(model, _options()).routes))

    assert CONDUIT.source in verdict["table_sources"]
    assert DIMENSIONS.source in verdict["table_sources"]
    assert FILL.source in verdict["table_sources"]
    assert all(text.strip() for text in verdict["table_sources"])


def test_an_observed_raceway_must_cite_provenance():
    with pytest.raises(RacewaySizingError, match="cite provenance"):
        RacewayObservation(
            derivation_class=DERIVATION_OBSERVED, designation="T2", kind=KIND_CONDUIT
        )


def test_an_observed_raceway_is_labelled_observed_and_keeps_caller_provenance():
    riser = Provenance(
        source_kind="riser-diagram",
        source_id="sheet:E2.1",
        source_element_id="note:7",
        page=4,
        method="hand-entered-note",
        confidence=0.95,
        derivation=DERIVATION_OBSERVED,
    )
    observation = RacewayObservation(
        derivation_class=DERIVATION_OBSERVED,
        designation="T3",
        kind=KIND_CONDUIT,
        provenance=(riser,),
    )
    model = _model(*(_conductor(name) for name in "abcde"))
    routes, _ = size_raceways(model, _options(observations={ROUTE_ID: observation}))
    route = _only(routes)
    verdict = _verdict(route)

    # The source said T3; the fill rule would have derived T2. The source wins.
    assert verdict["designation"] == "T3"
    assert verdict["derivation_class"] == DERIVATION_OBSERVED
    assert riser in route.provenance
    assert route.confidence == pytest.approx(1.0)
    own = [item for item in route.provenance if item.source_kind == SOURCE_KIND]
    assert own[0].derivation == DERIVATION_OBSERVED
    assert own[0].confidence == pytest.approx(0.95)


def test_an_observation_for_an_unknown_route_is_rejected():
    observation = RacewayObservation(
        derivation_class="user", designation="T1", kind=KIND_CONDUIT
    )
    with pytest.raises(RacewaySizingError, match="unknown route ids"):
        size_raceways(
            _model(_conductor("a")),
            _options(observations={"route:nope": observation}),
        )


@pytest.mark.parametrize("value", ["inferred", "observed-from-source", "", "OBSERVED"])
def test_observation_class_outside_the_vocabulary_is_rejected(value: str):
    """``inferred`` is rejected too: this channel exists for stronger claims."""

    with pytest.raises(RacewaySizingError, match="derivation_class"):
        RacewayObservation(
            derivation_class=value, designation="T1", kind=KIND_CONDUIT
        )


def test_sizing_confidence_never_exceeds_its_least_certain_conductor():
    model = _model(_conductor("a"), _conductor("b", confidence=0.25))
    route = _only(size_raceways(model, _options()).routes)
    assert route.provenance[-1].confidence == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# never presented as as-built
# ---------------------------------------------------------------------------


def test_sizing_never_overwrites_a_nominal_diameter_a_source_stated():
    stated = 0.027
    model = _model(
        *(_conductor(name) for name in "abcde"),
        route=_route(nominal_diameter_m=stated),
    )
    routes, warnings = size_raceways(model, _options())
    route = _only(routes)

    # T2 in the catalog is 0.018; the stated 0.027 survives untouched.
    assert _verdict(route)["designation"] == "T2"
    assert route.nominal_diameter_m == pytest.approx(stated)
    assert "route-diameter-not-overwritten" in _codes(warnings)


def test_sizing_never_alters_route_geometry_or_type():
    model = _model(*(_conductor(name) for name in "abcde"))
    before = model.routes[0]
    after = _only(size_raceways(model, _options()).routes)

    assert after.id == before.id
    assert after.centerline == before.centerline
    assert after.route_type == before.route_type
    assert after.start_port_id == before.start_port_id
    assert after.end_port_id == before.end_port_id
    assert after.fitting_ids == before.fitting_ids


def test_rerunning_the_lane_refuses_rather_than_overwriting_its_own_verdict():
    """A second pass must not silently replace an existing raceway statement."""

    model = _model(*(_conductor(name) for name in "abcde"))
    sized = apply_raceway_sizing(model, size_raceways(model, _options()))

    with pytest.raises(RacewaySizingError, match="refusing to overwrite"):
        size_raceways(sized, _options())


def test_existing_route_attributes_from_other_lanes_are_preserved():
    model = _model(
        *(_conductor(name) for name in "abcde"),
        route=_route(attributes={"routing_engine": "deterministic-rectilinear-v1"}),
    )
    route = _only(size_raceways(model, _options()).routes)
    assert route.attributes["routing_engine"] == "deterministic-rectilinear-v1"
    assert _verdict(route)["designation"] == "T2"


def test_applying_the_sizing_produces_a_model_that_still_validates():
    model = _model(*(_conductor(name) for name in "abcde"))
    sizing = size_raceways(model, _options())
    updated = apply_raceway_sizing(model, sizing)

    validate_model(updated)
    assert sizing.resolved_ids == (ROUTE_ID,)
    assert sizing.unresolved_ids == ()
    assert _verdict(updated.routes[0])["designation"] == "T2"
    # The original is untouched; this lane is pure.
    assert model.routes[0].attributes == {}


def test_unresolved_ids_report_the_refused_routes():
    model = _model(_conductor("a"), route=_route(route_type="flex"))
    sizing = size_raceways(model, _options())
    assert sizing.unresolved_ids == (ROUTE_ID,)
    assert sizing.resolved_ids == ()


# ---------------------------------------------------------------------------
# the reference tables themselves must be trustworthy inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_dimension_table_without_a_stated_source_is_rejected(blank: str):
    with pytest.raises(RacewaySizingError, match="source"):
        ConductorDimensionTable(
            table_id="anonymous", source=blank, entries=((("INSUL-X", "SIZE-A"), AREA_A),)
        )


def test_a_fill_rule_without_a_stated_source_is_rejected():
    with pytest.raises(RacewaySizingError, match="source"):
        FillRule(rule_id="anonymous", source="", exact_limits=((1, 0.5),))


def test_a_conduit_catalog_without_a_stated_source_is_rejected():
    with pytest.raises(RacewaySizingError, match="source"):
        RacewayCatalog(
            catalog_id="anonymous",
            source="",
            trade_sizes=(TradeSize(designation="T1", interior_area_m2=T1_INTERIOR),),
        )


def test_a_catalog_must_ascend_strictly_by_interior_area():
    with pytest.raises(RacewaySizingError, match="ascend"):
        RacewayCatalog(
            catalog_id="unordered",
            source="synthetic test catalog; not a published standard",
            trade_sizes=(
                TradeSize(designation="T2", interior_area_m2=T2_INTERIOR),
                TradeSize(designation="T1", interior_area_m2=T1_INTERIOR),
            ),
        )


def test_a_fill_fraction_above_one_is_rejected():
    with pytest.raises(RacewaySizingError, match="fraction"):
        FillRule(
            rule_id="impossible",
            source="synthetic test rule; not a published standard",
            exact_limits=((1, 1.5),),
        )


def test_a_fill_rule_stating_no_limit_at_all_is_rejected():
    with pytest.raises(RacewaySizingError, match="no limit"):
        FillRule(
            rule_id="empty", source="synthetic test rule; not a published standard"
        )


def test_a_dimension_table_may_not_repeat_an_entry():
    with pytest.raises(RacewaySizingError, match="repeats"):
        ConductorDimensionTable(
            table_id="dupes",
            source="synthetic test table; not a published standard",
            entries=(
                (("INSUL-X", "SIZE-A"), AREA_A),
                (("INSUL-X", "SIZE-A"), AREA_B),
            ),
        )


def test_a_cable_catalog_may_not_repeat_an_assembly():
    with pytest.raises(RacewaySizingError, match="repeats"):
        CableCatalog(
            catalog_id="dupes",
            source="synthetic test catalog; not a published standard",
            entries=(
                CableEntry(designation="X", conductor_count=3, conductor_size="SIZE-A"),
                CableEntry(designation="Y", conductor_count=3, conductor_size="SIZE-A"),
            ),
        )


def test_size_raceways_rejects_a_non_model():
    with pytest.raises(RacewaySizingError, match="BuildingModel"):
        size_raceways(object())  # type: ignore[arg-type]


def test_a_model_with_no_routes_yields_no_verdicts():
    model = replace(
        _model(),
        routes=(),
        circuits=(
            Circuit(
                id=CIRCUIT_ID,
                source_port_id="port:panel",
                load_port_ids=("port:load",),
                circuit_number="2",
            ),
        ),
    )
    routes, warnings = size_raceways(model, _options())
    assert routes == ()
    assert warnings == ()
