"""Analytic path measurement is independent of presentation tessellation."""
from dataclasses import replace
import math

import pytest

from oabm.model import Polyline3D, Route
from oabm.quantities import extract_quantities
from test_physical_route_schedule import measured_model


@pytest.mark.parametrize("odd,two,expected", [
    (False,False,3 + .125*math.pi),
    (True,False,3 + .1250000005*math.pi),
    (False,True,3 + .25*math.pi),
])
def test_physical_length_analytic_whole_model_pure_and_traceable(odd,two,expected):
    model = measured_model(odd=odd,two=two)
    before = model.to_json()
    report = extract_quantities(model)
    rows = [row for row in report.items if row.category == "route_length"]
    assert len(rows) == 1
    row = rows[0]
    assert row.quantity == pytest.approx(expected,abs=1e-12)
    assert row.source_entity_ids == (model.routes[0].id,)
    assert dict(row.variant)["outside_diameter_m"] == .02
    assert dict(row.variant)["nominal_diameter_m"] is None
    assert row.quantity_provenance
    evidence = row.provenance
    assert all(record in evidence for segment in model.routes[0].segments for record in segment.provenance)
    assert not [row for row in report.items if row.category == "fitting"]
    assert len([row for row in report.items if row.category == "device"]) > 0
    assert {w.code for w in report.warnings} >= {
        "physical_route_geometric_length_only", "physical_route_overlap_not_evaluated"}
    assert model.to_json() == before
    assert extract_quantities(model).to_json() == report.to_json()


def test_mixed_routes_have_separate_analytic_basis_and_explicit_groups():
    model = measured_model()
    physical = model.routes[0]
    legacy = Route(id="route:legacy",route_type=physical.route_type,
        start_port_id=physical.start_port_id,end_port_id=physical.end_port_id,
        centerline=Polyline3D(points=(physical.joints[0].position,physical.joints[-1].position)))
    model = replace(model,routes=(physical,legacy))
    report = extract_quantities(model,groups={"review":(physical.id,)})
    rows = [row for row in report.items if row.category == "route_length"]
    assert len(rows) == 2
    assert next(row for row in rows if row.group == "review").quantity == pytest.approx(3+.125*math.pi)
    assert next(row for row in rows if row.group is None).quantity == pytest.approx(math.hypot(1.25,2.25))
    assert extract_quantities(replace(model,routes=(legacy,physical)),groups={"review":(physical.id,)}).to_json() == report.to_json()
