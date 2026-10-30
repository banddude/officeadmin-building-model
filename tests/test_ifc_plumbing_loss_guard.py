"""Synthetic native plumbing must never vanish into a canonical-only export."""
from pathlib import Path

import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.util.element
import pytest

from oabm.ifc import IfcAdapterError, from_ifc, round_trip, to_ifc
from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]


def _model():
    return BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")


def _add(native, kind, key="synthetic-plumbing"):
    from oabm.ifc import canonical_id_to_ifc_guid
    entity = ifcopenshell.api.root.create_entity(native, ifc_class=kind)
    entity.GlobalId = canonical_id_to_ifc_guid(key)
    entity.Name = "Synthetic native fixture; not an electrical device"
    return entity


@pytest.mark.parametrize("kind", [
    "IfcPipeSegment", "IfcPipeFitting", "IfcSanitaryTerminal", "IfcWasteTerminal",
    "IfcPipeSegmentType", "IfcPipeFittingType", "IfcSanitaryTerminalType", "IfcWasteTerminalType",
])
@pytest.mark.parametrize("as_path", [False, True])
def test_native_plumbing_refused_without_mutating_source(kind, as_path, tmp_path):
    native = to_ifc(_model())
    addition = _add(native, kind)
    before = native.to_string()
    source = native
    if as_path:
        source = tmp_path / "synthetic.ifc"
        native.write(str(source))
        original_bytes = source.read_bytes()
    with pytest.raises(IfcAdapterError, match="would discard unsupported native plumbing") as error:
        from_ifc(source)
    assert kind in str(error.value)
    assert addition.GlobalId in str(error.value)
    assert addition.Name not in str(error.value)
    assert "Preserve the original IFC" in str(error.value)
    assert native.to_string() == before
    if as_path:
        assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("system_type", [
    "DOMESTICCOLDWATER", "DOMESTICHOTWATER", "DRAINAGE", "SEWAGE",
    "WASTEWATER", "WATERSUPPLY", "RAINWATER", "STORMWATER",
])
@pytest.mark.parametrize("kind", ["IfcDistributionSystem", "IfcDistributionPort"])
def test_typed_plumbing_system_or_port_cannot_disappear_alone(kind, system_type):
    native = to_ifc(_model())
    addition = _add(native, kind)
    if kind == "IfcDistributionPort":
        addition.SystemType = system_type
    else:
        addition.PredefinedType = system_type
    with pytest.raises(IfcAdapterError, match=kind):
        from_ifc(native)


def test_electrical_metadata_does_not_disguise_sanitary_class():
    native = to_ifc(_model())
    original_device = native.by_type("IfcOutlet")[0]
    metadata = ifcopenshell.util.element.get_pset(original_device, "OABM_Canonical")
    addition = _add(native, "IfcSanitaryTerminal")
    pset = ifcopenshell.api.pset.add_pset(native, product=addition, name="OABM_Canonical")
    ifcopenshell.api.pset.edit_pset(native, pset=pset,
                                  properties={k: v for k, v in metadata.items() if k != "id"})
    with pytest.raises(IfcAdapterError, match="unsupported native plumbing: IfcSanitaryTerminal"):
        from_ifc(native)


def test_unsupported_inventory_error_is_deterministic_and_bounded():
    native = to_ifc(_model())
    for index in range(25):
        _add(native, "IfcPipeSegment", f"synthetic-pipe:{index}")
    messages = []
    for _ in range(2):
        with pytest.raises(IfcAdapterError) as error:
            from_ifc(native)
        messages.append(str(error.value))
    assert messages[0] == messages[1]
    assert messages[0].count("IfcPipeSegment") == 20
    assert "5 additional objects" in messages[0]


def test_supported_architecture_electrical_roundtrip_remains_exact(tmp_path):
    model = _model()
    assert round_trip(model).to_dict() == model.to_dict()
    path = tmp_path / "electrical.ifc"
    to_ifc(model, path)
    assert from_ifc(str(path)).to_dict() == model.to_dict()
