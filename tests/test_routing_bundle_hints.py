"""Bundle hints: an optional router input that lets a route follow earlier runs."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import (
    DERIVATION_INFERRED,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Vector3,
    stable_id,
)
import oabm.routing.router as router
from oabm.routing import BundleHints, RoutingError, route_between_ports

UP = Vector3(x=0, y=0, z=1)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "routing" / "v1"

NORMAL_DIGEST = "066b10b8faf26a0360d9d3976ebc49bee56f974576b80e7702279cfeaf928c14"
ALTERNATE_DIGEST = "8429a8ef4a276aee86af1c976c798b43316cdf99cb576cffb7262bdd1a2211b1"
CEILING_DIGEST = "7a067c47386ad07647b8883fe7251e4872e94494741eeca9e4745d436f2cc29c"


def _ceiling_model(loads):
    panel = ElectricalEquipment(id="equipment:panel", equipment_type="panelboard",
                                pose=Pose(position=Point3(x=0, y=0, z=2.7)))
    ports = [Port(id="port:panel", owner_id=panel.id, domain="power", role="source",
                  pose=Pose(position=Point3(x=0, y=0, z=2.7)), direction=UP, nominal_diameter_m=0.021)]
    devices = []
    for name, (x, y) in loads:
        d = ElectricalDevice(id=f"device:{name}", device_type="luminaire",
                             pose=Pose(position=Point3(x=x, y=y, z=2.7)))
        devices.append(d)
        ports.append(Port(id=f"port:{name}", owner_id=d.id, domain="power", role="sink",
                          pose=Pose(position=Point3(x=x, y=y, z=2.7)), direction=UP, nominal_diameter_m=0.021))
    return BuildingModel(model_id="model:bundle-hints-test", electrical_equipment=(panel,),
                         electrical_devices=tuple(devices), ports=tuple(ports))


def _pl(*xyz):
    return Polyline3D(points=tuple(Point3(x=x, y=y, z=z) for x, y, z in xyz))


def _points(route):
    return tuple((p.x, p.y, p.z) for p in route.centerline.points)


def _json(model, route, fittings):
    return replace(model, routes=(route,), route_fittings=tuple(fittings)).to_json()


def _sha(model, route, fittings):
    return hashlib.sha256(_json(model, route, fittings).encode()).hexdigest()


def _no_bundle_attributes(route):
    return [key for key in route.attributes if key.startswith("bundle_hint")]


def test_option_unset_gives_byte_identical_routes_to_main():
    cases = [
        ("normal", BuildingModel.load(FIXTURE_DIR / "normal.json"), "port:source", "port:load", NORMAL_DIGEST),
        ("alternate", BuildingModel.load(FIXTURE_DIR / "alternate-obstacle.json"), "port:source", "port:load", ALTERNATE_DIGEST),
        ("ceiling", _ceiling_model([("a", (10, 2))]), "port:panel", "port:a", CEILING_DIGEST),
    ]
    trunk = _pl((0, 1, 2.85), (10, 1, 2.85))
    hint_calls = [
        {},
        {"bundle_hints": None},
        {"bundle_hints": BundleHints()},
        {"bundle_hints": BundleHints(paths=(trunk,), discount=0.0)},
        {"bundle_hints": BundleHints(paths=(_pl((0, 0, 0), (3, 4, 0)),))},
    ]
    for _, model, start, end, digest in cases:
        base_route, base_fittings = route_between_ports(model, start, end, "emt")
        assert _sha(model, base_route, base_fittings) == digest
        assert _no_bundle_attributes(base_route) == []
        for kwargs in hint_calls:
            route, fittings = route_between_ports(model, start, end, "emt", **kwargs)
            assert route == base_route
            assert fittings == base_fittings
            assert _sha(model, route, fittings) == digest
            assert _no_bundle_attributes(route) == []


def test_route_follows_a_trunk_known_answer():
    model = _ceiling_model([("a", (10, 2))])

    base_route, base_fittings = route_between_ports(model, "port:panel", "port:a", "emt")
    assert _points(base_route) == (
        (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (0.0, 2.0, 2.85), (10.0, 2.0, 2.85), (10.0, 2.0, 2.7),
    )
    assert len(base_fittings) == 3

    trunk = _pl((0, 1, 2.85), (10, 1, 2.85))
    for discount in (0.25, 0.5):
        route, fittings = route_between_ports(
            model, "port:panel", "port:a", "emt",
            bundle_hints=BundleHints(paths=(trunk,), discount=discount),
        )
        assert _points(route) == (
            (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (0.0, 1.0, 2.85),
            (10.0, 1.0, 2.85), (10.0, 2.0, 2.85), (10.0, 2.0, 2.7),
        )
        assert len(fittings) == 4
        assert all(fitting.fitting_type == "elbow-90" for fitting in fittings)
        assert route.attributes["bundle_hint_shared_m"] == 10.0
        assert route.attributes["bundle_hint_discount"] == discount
        assert route.attributes["length_m"] == 12.3


def test_sequential_home_runs_share_one_trunk_known_answer():
    model = _ceiling_model([("a", (12, 0)), ("b", (8, 3)), ("c", (4, -2))])
    h1 = _pl((0, 0, 2.85), (12, 0, 2.85))
    h2 = _pl((0, 0, 2.85), (8, 0, 2.85), (8, 3, 2.85))

    route_a, _ = route_between_ports(model, "port:panel", "port:a", "emt")
    assert _points(route_a) == ((0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (12.0, 0.0, 2.85), (12.0, 0.0, 2.7))

    route_b, _ = route_between_ports(model, "port:panel", "port:b", "emt")
    assert _points(route_b) == (
        (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (0.0, 3.0, 2.85), (8.0, 3.0, 2.85), (8.0, 3.0, 2.7),
    )
    route_b_hinted, _ = route_between_ports(
        model, "port:panel", "port:b", "emt", bundle_hints=BundleHints(paths=(h1,)),
    )
    assert _points(route_b_hinted) == (
        (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (8.0, 0.0, 2.85), (8.0, 3.0, 2.85), (8.0, 3.0, 2.7),
    )
    assert route_b_hinted.attributes["bundle_hint_shared_m"] == 8.0

    route_c, _ = route_between_ports(model, "port:panel", "port:c", "emt")
    assert _points(route_c) == (
        (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (0.0, -2.0, 2.85), (4.0, -2.0, 2.85), (4.0, -2.0, 2.7),
    )
    route_c_hinted, _ = route_between_ports(
        model, "port:panel", "port:c", "emt", bundle_hints=BundleHints(paths=(h1, h2)),
    )
    assert _points(route_c_hinted) == (
        (0.0, 0.0, 2.7), (0.0, 0.0, 2.85), (4.0, 0.0, 2.85), (4.0, -2.0, 2.85), (4.0, -2.0, 2.7),
    )
    assert route_c_hinted.attributes["bundle_hint_shared_m"] == 4.0

    def _payload(paths):
        route, fittings = route_between_ports(
            model, "port:panel", "port:c", "emt", bundle_hints=BundleHints(paths=paths),
        )
        return _json(model, route, fittings)

    assert _payload((h1, h2)) == _payload((h2, h1)) == _payload((h1, h2, h1))


def test_along_semantics_only():
    index = router._bundle_index(BundleHints(paths=(_pl((0, 0, 0), (10, 0, 0)),)), 9)

    def _along(ax, ay, az, bx, by, bz):
        return router._along_bundle(Point3(x=ax, y=ay, z=az), Point3(x=bx, y=by, z=bz), index, 9)

    assert _along(2, 0, 0, 6, 0, 0) is True
    assert _along(6, 0, 0, 2, 0, 0) is True
    assert _along(8, 0, 0, 12, 0, 0) is False  # runs past the interval end
    assert _along(2, 0.05, 0, 6, 0.05, 0) is False  # parallel offset
    assert _along(5, -1, 0, 5, 1, 0) is False  # perpendicular crossing

    model = BuildingModel.load(FIXTURE_DIR / "normal.json")
    route, _ = route_between_ports(
        model, "port:source", "port:load", "emt",
        bundle_hints=BundleHints(paths=(_pl((2, -1, 0), (2, 1, 0)),)),
    )
    assert _points(route) == ((0.0, 0.0, 0.0), (4.0, 0.0, 0.0))
    assert route.attributes["bundle_hint_shared_m"] == 0.0


def test_index_merges_touching_intervals():
    index = router._bundle_index(
        BundleHints(paths=(_pl((0, 0, 0), (4, 0, 0)), _pl((4, 0, 0), (9, 0, 0)))), 9,
    )
    assert index.lookup == {(0, 0.0, 0.0): ((0.0, 9.0),)}
    assert index.vertices == ((0.0, 0.0, 0.0), (9.0, 0.0, 0.0))

    assert router._bundle_index(
        BundleHints(paths=(_pl((0, 0, 0), (4, 0, 0)), _pl((4, 0, 0), (9, 0, 0))), discount=0.0), 9,
    ) is None
    assert router._bundle_index(BundleHints(paths=(_pl((0, 0, 0), (3, 4, 0)),)), 9) is None


def test_bundle_hints_validation():
    h1 = _pl((0, 0, 2.85), (12, 0, 2.85))
    for discount in (-0.1, 1.0, float("nan"), True):
        with pytest.raises(RoutingError):
            BundleHints(paths=(h1,), discount=discount)
    with pytest.raises(RoutingError):
        BundleHints(paths=[h1])
    with pytest.raises(RoutingError):
        BundleHints(paths=("x",))


def test_identity_and_provenance_unchanged_by_hints():
    model = _ceiling_model([("a", (10, 2))])
    trunk = _pl((0, 1, 2.85), (10, 1, 2.85))

    base_route, _ = route_between_ports(model, "port:panel", "port:a", "emt")
    route, fittings = route_between_ports(
        model, "port:panel", "port:a", "emt",
        bundle_hints=BundleHints(paths=(trunk,), discount=0.25),
    )

    assert route.id == stable_id("route", "model:bundle-hints-test:emt:port:panel:port:a")
    assert route.provenance == base_route.provenance
    assert route.provenance[0].method == "deterministic-rectilinear-v1"
    assert route.provenance[0].derivation == DERIVATION_INFERRED
    for fitting in fittings:
        assert fitting.id == stable_id(
            "fitting", f"{route.id}:turn:{fitting.attributes['routing_turn_signature']}"
        )
    base_keys = set(base_route.attributes)
    assert set(route.attributes) - base_keys == {"bundle_hint_discount", "bundle_hint_shared_m"}


def test_hinted_route_is_repeatable():
    model = _ceiling_model([("a", (10, 2))])
    trunk = _pl((0, 1, 2.85), (10, 1, 2.85))
    hints = BundleHints(paths=(trunk,), discount=0.25)

    first = route_between_ports(model, "port:panel", "port:a", "emt", bundle_hints=hints)
    second = route_between_ports(model, "port:panel", "port:a", "emt", bundle_hints=hints)

    assert _json(model, *first) == _json(model, *second)
