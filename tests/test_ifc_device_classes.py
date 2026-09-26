"""Every canonical device type exports as the right IFC4 class and round-trips.

The mapping under test lives in ``oabm.ifc.adapter._device_ifc_type``. Each
mapped canonical ``device_type`` must export as its IFC4 class and
``PredefinedType``, keep the canonical type in ``ObjectType``, and come back
through ``from_ifc`` with the same canonical ``device_type``. The model is
synthetic and generated in-code; nothing here is drawn from a real sheet.
"""

from pathlib import Path

import ifcopenshell

from oabm.ifc import canonical_id_to_ifc_guid, from_ifc, to_ifc
from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]
MODEL_FIXTURE = ROOT / "fixtures" / "model" / "v1" / "garage-route.json"

# (device_type, ifc_class, PredefinedType or None)
EXPECTED_MAPPINGS = [
    ("receptacle", "IfcOutlet", "POWEROUTLET"),
    ("receptacle_duplex", "IfcOutlet", "POWEROUTLET"),
    ("receptacle_quad", "IfcOutlet", "POWEROUTLET"),
    ("combination_outlet", "IfcOutlet", "POWEROUTLET"),
    ("special_purpose_outlet", "IfcOutlet", "POWEROUTLET"),
    ("evse", "IfcOutlet", "POWEROUTLET"),
    ("outlet", "IfcOutlet", "POWEROUTLET"),
    ("data_outlet", "IfcOutlet", "DATAOUTLET"),
    ("catv_outlet", "IfcOutlet", "AUDIOVISUALOUTLET"),
    ("telephone_outlet", "IfcOutlet", "TELEPHONEOUTLET"),
    ("junction_box", "IfcJunctionBox", None),
    ("junction_box_power", "IfcJunctionBox", "POWER"),
    ("junction_box_data", "IfcJunctionBox", "DATA"),
    ("luminaire", "IfcLightFixture", None),
    ("exit_sign", "IfcLightFixture", "SECURITYLIGHTING"),
    ("switch", "IfcSwitchingDevice", None),
    ("disconnect", "IfcSwitchingDevice", None),
    ("occupancy_sensor", "IfcSensor", "MOVEMENTSENSOR"),
    ("smoke_alarm", "IfcSensor", "SMOKESENSOR"),
    ("smoke_co_alarm", "IfcSensor", "SMOKESENSOR"),
    ("heat_detector", "IfcSensor", "HEATSENSOR"),
    ("access_control_device", "IfcSensor", "IDENTIFIERSENSOR"),
    ("speaker", "IfcAudioVisualAppliance", "SPEAKER"),
    ("exhaust_fan", "IfcFan", None),
    ("ceiling_fan", "IfcFan", None),
    ("panelboard", "IfcElectricDistributionBoard", "DISTRIBUTIONBOARD"),
    # Unknown canonical types stay generic appliances.
    ("mystery_gizmo", "IfcElectricAppliance", None),
]


def _device_model() -> BuildingModel:
    """The garage fixture reshaped into one device per canonical type."""

    template = BuildingModel.load(MODEL_FIXTURE).to_dict()
    devices = []
    for index, (device_type, _ifc_class, _predefined) in enumerate(EXPECTED_MAPPINGS):
        devices.append(
            {
                "id": f"device:map-{index:02d}",
                "name": f"TEST DEVICE {device_type}",
                "device_type": device_type,
                "level_id": "level:ground",
                "host_id": "wall:south",
                "pose": {
                    "position": {"x": 1.0 + index, "y": 0.2, "z": 1.2},
                    "rotation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0},
                },
                "confidence": 1.0,
                "provenance": [
                    {
                        "source_kind": "synthetic",
                        "source_id": "fixture:ifc-device-classes",
                        "source_element_id": None,
                        "page": None,
                        "method": "generated test model",
                        "confidence": 1.0,
                        "attributes": {},
                    }
                ],
                "attributes": {},
            }
        )
    template["name"] = "IFC device class mapping test model"
    template["electrical_devices"] = devices
    # Endpoint ports and circuits only exist on the garage route; drop them so
    # the trimmed model holds the equipment and the mapped devices alone.
    template["ports"] = []
    template["circuits"] = []
    template["conductors"] = []
    template["routes"] = []
    template["route_fittings"] = []
    return BuildingModel.from_dict(template)


def test_every_device_type_exports_its_ifc4_class_and_predefined_type() -> None:
    model = _device_model()
    ifc = to_ifc(model)

    # Seven power-outlet types, one each data, CATV/TV and telephone; two
    # luminaire-family types with the exit sign carrying SECURITYLIGHTING.
    assert len(ifc.by_type("IfcOutlet")) == 10
    assert len(ifc.by_type("IfcLightFixture")) == 2
    exit_sign = next(
        item for item in ifc.by_type("IfcLightFixture")
        if item.ObjectType == "exit_sign"
    )
    assert exit_sign.PredefinedType == "SECURITYLIGHTING"
    for device_type, ifc_class, predefined in EXPECTED_MAPPINGS:
        device = next(
            item for item in model.electrical_devices if item.device_type == device_type
        )
        product = ifc.by_guid(canonical_id_to_ifc_guid(device.id))
        assert product.is_a(ifc_class), device_type
        if predefined is None:
            assert product.PredefinedType in (None, "NOTDEFINED"), device_type
        else:
            assert product.PredefinedType == predefined, device_type
        # The canonical type is preserved in ObjectType either way.
        assert product.ObjectType == device_type, device_type


def test_every_device_type_round_trips_with_its_canonical_type() -> None:
    model = _device_model()
    round_tripped = from_ifc(to_ifc(model))

    assert round_tripped.to_dict() == model.to_dict()
    for device in model.electrical_devices:
        match = next(
            item for item in round_tripped.electrical_devices if item.id == device.id
        )
        assert match.device_type == device.device_type, device.id


def test_device_class_export_is_deterministic(tmp_path: Path) -> None:
    model = _device_model()
    first_path = tmp_path / "first.ifc"
    second_path = tmp_path / "second.ifc"
    to_ifc(model, first_path)
    to_ifc(model, second_path)

    def inventory(path: Path) -> list[tuple[str, str, str | None, str | None]]:
        ifc = ifcopenshell.open(str(path))
        rows = []
        for entity in sorted(
            ifc.by_type("IfcDistributionElement"), key=lambda item: item.GlobalId
        ):
            rows.append(
                (
                    entity.GlobalId,
                    entity.is_a(),
                    getattr(entity, "PredefinedType", None),
                    getattr(entity, "ObjectType", None),
                )
            )
        return rows

    assert inventory(first_path) == inventory(second_path)
    assert from_ifc(first_path).to_dict() == from_ifc(second_path).to_dict()
