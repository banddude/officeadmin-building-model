import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.importers.pdf_convergence import (
    PdfConvergenceError,
    converge_pdf_models,
)
from oabm.importers.pdf_electrical import (
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfPageTransform,
)
from oabm.model import BuildingModel, Point3, Pose, validate_model


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT / "fixtures" / "pdf_convergence" / "v1" / "simple-garage-convergence.json"
)
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"


def _load_case() -> tuple[dict, BuildingModel, PdfElectricalDocument, BuildingModel]:
    case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    architecture = BuildingModel.from_json(
        (ROOT / case["architecture_model"]).read_text(encoding="utf-8")
    )
    electrical_document = PdfElectricalDocument.from_dict(
        json.loads(
            (ROOT / case["electrical_observations"]).read_text(encoding="utf-8")
        )
    )
    electrical = ElectricalPdfImporter().import_document(
        electrical_document,
        page_transforms={
            1: PdfPageTransform(**case["page_transform"]),
        },
    )
    return case, architecture, electrical_document, electrical


def _by_name(items, name):
    return next(item for item in items if item.name == name)


def test_registered_pdf_lanes_converge_into_one_hosted_canonical_model() -> None:
    case, architecture, _, electrical = _load_case()

    result = converge_pdf_models(architecture, electrical)

    assert [item.id for item in result.levels] == [
        item.id for item in architecture.levels
    ]
    assert [item.id for item in result.spaces] == [
        item.id for item in architecture.spaces
    ]
    assert [item.id for item in result.walls] == [
        item.id for item in architecture.walls
    ]

    input_panel = _by_name(electrical.electrical_equipment, "LP")
    input_evse = _by_name(electrical.electrical_devices, "EVSE-1")
    panel = _by_name(result.electrical_equipment, "LP")
    evse = _by_name(result.electrical_devices, "EVSE-1")
    level = _by_name(result.levels, case["expected"]["evse"]["level"])
    space = _by_name(result.spaces, case["expected"]["evse"]["space"])
    host = next(item for item in result.walls if item.id == evse.host_id)

    assert panel.id == input_panel.id
    assert evse.id == input_evse.id
    assert [item.id for item in result.ports] == [
        item.id for item in electrical.ports
    ]
    assert [item.id for item in result.circuits] == [
        item.id for item in electrical.circuits
    ]

    assert panel.level_id == level.id
    assert panel.space_id == space.id
    assert panel.host_id is None
    assert (
        panel.pose.position.x,
        panel.pose.position.y,
        panel.pose.position.z,
    ) == pytest.approx(case["expected"]["panel"]["position_m"])

    assert evse.level_id == level.id
    assert evse.space_id == space.id
    assert host.attributes["pdf_architecture"]["source_side"] == case["expected"]["evse"][
        "host_source_side"
    ]
    assert (
        evse.pose.position.x,
        evse.pose.position.y,
        evse.pose.position.z,
    ) == pytest.approx(case["expected"]["evse"]["position_m"])

    input_evse_port = next(
        item for item in electrical.ports if item.owner_id == input_evse.id
    )
    evse_port = next(item for item in result.ports if item.id == input_evse_port.id)
    assert evse_port.owner_id == evse.id
    assert evse_port.pose.position == evse.pose.position

    circuit = result.circuits[0]
    assert circuit.source_port_id in {item.id for item in result.ports}
    assert set(circuit.load_port_ids).issubset({item.id for item in result.ports})

    assert all(item in evse.provenance for item in input_evse.provenance)
    assert any(item.source_kind == "pdf-convergence" for item in evse.provenance)
    assert evse.confidence <= input_evse.confidence
    assert evse.confidence <= level.confidence
    assert evse.confidence <= space.confidence
    assert evse.confidence <= host.confidence

    assert result.attributes["pdf_architecture"] == architecture.attributes[
        "pdf_architecture"
    ]
    assert result.attributes["pdf_electrical"] == electrical.attributes[
        "pdf_electrical"
    ]
    assert result.attributes["pdf_convergence"]["ambiguities"] == []
    attachment = next(
        item
        for item in result.attributes["pdf_convergence"]["attachments"]
        if item["entity_id"] == evse.id
    )
    assert attachment["status"] == "hosted"

    validate_model(result)
    reparsed = BuildingModel.from_json(result.to_json())
    assert reparsed.to_dict() == result.to_dict()

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(result.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_convergence_is_deterministic_across_input_collection_order() -> None:
    _, architecture, _, electrical = _load_case()

    first = converge_pdf_models(architecture, electrical)
    reordered_architecture = replace(
        architecture,
        levels=tuple(reversed(architecture.levels)),
        spaces=tuple(reversed(architecture.spaces)),
        walls=tuple(reversed(architecture.walls)),
        slabs=tuple(reversed(architecture.slabs)),
        ceilings=tuple(reversed(architecture.ceilings)),
        openings=tuple(reversed(architecture.openings)),
    )
    reordered_electrical = replace(
        electrical,
        electrical_equipment=tuple(reversed(electrical.electrical_equipment)),
        electrical_devices=tuple(reversed(electrical.electrical_devices)),
        ports=tuple(reversed(electrical.ports)),
        circuits=tuple(reversed(electrical.circuits)),
    )

    second = converge_pdf_models(reordered_architecture, reordered_electrical)

    assert first.to_json() == second.to_json()


def test_unregistered_electrical_page_local_geometry_is_rejected() -> None:
    _, architecture, electrical_document, _ = _load_case()
    unregistered = ElectricalPdfImporter().import_document(electrical_document)

    with pytest.raises(PdfConvergenceError, match="explicitly registered"):
        converge_pdf_models(architecture, unregistered)


def test_equally_plausible_wall_hosts_remain_explicit_ambiguity() -> None:
    _, architecture, _, _ = _load_case()
    space = _by_name(architecture.spaces, "GARAGE")
    corner = space.footprint.points[0]

    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:gate-d-wall-ambiguity",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "evse-label",
                    "page": 1,
                    "text": 'EVSE-1 +48" AFF WALL MTD',
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
            ],
        }
    )
    electrical = ElectricalPdfImporter().import_document(
        document,
        page_transforms={
            1: PdfPageTransform(
                frame_id=architecture.coordinate_system.frame_id,
                m11_m_per_pt=0.01,
                m22_m_per_pt=0.01,
                tx_m=corner.x - 1.0,
                ty_m=corner.y - 1.0,
            )
        },
    )

    result = converge_pdf_models(architecture, electrical)
    evse = _by_name(result.electrical_devices, "EVSE-1")

    assert evse.level_id == architecture.levels[0].id
    assert evse.space_id == space.id
    assert evse.host_id is None
    assert evse.pose.position.z == pytest.approx(1.2192)
    assert "wall_host_ambiguous" in evse.attributes["pdf_convergence"][
        "ambiguity_codes"
    ]

    ambiguity = next(
        item
        for item in result.attributes["pdf_convergence"]["ambiguities"]
        if item["entity_id"] == evse.id and item["code"] == "wall_host_ambiguous"
    )
    candidate_ids = {item["host_id"] for item in ambiguity["candidate_hosts"]}
    candidate_sides = {
        wall.attributes["pdf_architecture"]["source_side"]
        for wall in architecture.walls
        if wall.id in candidate_ids
    }
    assert candidate_sides == {"south", "west"}
    validate_model(result)
