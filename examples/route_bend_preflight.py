"""Run from repo root: PYTHONPATH=src python examples/route_bend_preflight.py.

No render, network access, manufacturer catalog or customer input is needed.
"""
import json
from pathlib import Path

from oabm.model import BuildingModel
from oabm.qa.route_bends import check_route_bends

fixtures = Path(__file__).resolve().parents[1] / "fixtures/routing/bend-preflight"
radii = json.loads((fixtures / "radii.json").read_text())
for name, expected in (("valid", True), ("short-offset", False)):
    model = BuildingModel.load(fixtures / f"{name}.json")
    original = model.to_json()
    result = check_route_bends(model, radii_m=radii)
    assert result.ready == expected
    assert model.to_json() == original
    assert result.to_json() == (fixtures / f"{name}.expected.json").read_text()
    print(f"{name}: ready={result.ready}, bends={len(result.bends)}, "
          f"diagnostics={[d.code for d in result.diagnostics]}")
    for diagnostic in result.diagnostics:
        if diagnostic.code == "short_straight":
            print(f"  {diagnostic.route_id} segment at vertex {diagnostic.index}: "
                  f"available={diagnostic.available_m:.3f} m, required={diagnostic.required_m:.3f} m")
