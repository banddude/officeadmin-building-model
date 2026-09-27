"""Per-token materials and the glazing style on IFC export.

A wall with a canonical ``construction`` token gets a standard material
association (one shared ``IfcMaterial`` per token, one
``IfcRelAssociatesMaterial`` per token with a GlobalId derived from the
token), so Bonsai and Revit can filter by construction with no OABM
knowledge. Glazed walls also carry one shared translucent surface style on
their Body items. These tests pin the exact material mapping, the glass
style, the glazed-wall-without-Body edge, the untouched round trip, byte
determinism, strict EXPRESS validity, and the default-off guarantee: a model
whose walls all lack a token produces no material or style entities at all.
All content is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.validate
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, round_trip, to_ifc
from oabm.ifc.adapter import _WALL_MATERIAL_KEY_PREFIX
from oabm.model import (
    BuildingModel,
    Level,
    Point3,
    Polyline3D,
    Wall,
    validate_model,
)
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"

# token -> (IfcMaterial.Name, IfcMaterial.Category), per the issue mapping.
_MATERIAL_BY_TOKEN = {
    "glazed": ("Glass", "glass"),
    "masonry": ("Masonry", "masonry"),
    "concrete": ("Concrete", "concrete"),
    "framed": ("Framed partition", "framing"),
}
# The GlobalId key prefix under test, split across lines exactly like the
# adapter's constant so the expected values stay literal.
_KEY_PREFIX = (
    "wall-construction"
    "-material:"
)
_GLASS_TRANSPARENCY = 0.65
_GLASS_COLOUR = (0.80, 0.86, 0.90)


def _wall(wall_id: str, *, token: str | None = None, points=None) -> Wall:
    values: dict[str, object] = dict(
        id=wall_id,
        level_id="level:ground",
        centerline=Polyline3D(
            points=points
            or (Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0))
        ),
        thickness_m=0.12,
        height_m=2.7,
    )
    if token is not None:
        values["construction"] = token
    return Wall(**values)  # type: ignore[arg-type]


def _model(*walls: Wall) -> BuildingModel:
    return BuildingModel(
        model_id="model:ifc-construction",
        name="Synthetic Construction Model",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=walls,
    )


def _four_token_model() -> BuildingModel:
    return _model(
        _wall("wall:glazed", token="glazed"),
        _wall("wall:masonry", token="masonry"),
        _wall("wall:concrete", token="concrete"),
        _wall("wall:framed", token="framed"),
        _wall("wall:plain"),
    )


def _garage():
    case = next(
        item for item in load_golden_cases(GOLDEN_ROOT) if item.name == "synthetic-garage"
    )
    return load_golden_model(case)


def _product(ifc, canonical_id: str):
    return ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))


def _body(product):
    for shape in product.Representation.Representations:
        if shape.RepresentationIdentifier == "Body":
            return shape
    raise AssertionError(f"{product.is_a()} {product.GlobalId} has no Body representation")


def _adapter_pset(product) -> dict:
    for rel in product.IsDefinedBy:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if not definition.is_a("IfcPropertySet") or definition.Name != "OABM_Adapter":
            continue
        return {
            prop.Name: (prop.NominalValue.wrappedValue if prop.NominalValue else None)
            for prop in definition.HasProperties
        }
    raise AssertionError("product carries no OABM_Adapter property set")


def test_tokenless_models_write_no_materials_or_styles(tmp_path: Path) -> None:
    """No token anywhere: no IfcMaterial or IfcRelAssociatesMaterial, no style.

    Nothing new is created on this path, so the written bytes stay the plain
    default export's bytes (the #148 determinism tests keep pinning those
    bytes across repeated runs).
    """
    for model in (_garage(), _model(_wall("wall:plain"))):
        ifc = to_ifc(model, tmp_path / "plain.ifc")
        assert ifc.by_type("IfcMaterial") == []
        assert ifc.by_type("IfcRelAssociatesMaterial") == []
        assert ifc.by_type("IfcSurfaceStyle") == []
        assert ifc.by_type("IfcStyledItem") == []


def test_one_material_per_used_token_with_exact_names_and_categories() -> None:
    ifc = to_ifc(_four_token_model())

    materials = ifc.by_type("IfcMaterial")
    assert [(material.Name, material.Category) for material in materials] == [
        ("Concrete", "concrete"),  # sorted token order: concrete,
        ("Framed partition", "framing"),  # framed,
        ("Glass", "glass"),  # glazed,
        ("Masonry", "masonry"),  # masonry.
    ]

    rels = ifc.by_type("IfcRelAssociatesMaterial")
    assert len(rels) == 4
    by_material = {rel.RelatingMaterial.Name: rel for rel in rels}
    assert set(by_material) == {"Concrete", "Framed partition", "Glass", "Masonry"}
    for token, (name, _category) in _MATERIAL_BY_TOKEN.items():
        rel = by_material[name]
        assert rel.GlobalId == canonical_id_to_ifc_guid(
            f"{_KEY_PREFIX}{token}"
        )
        assert [item.GlobalId for item in rel.RelatedObjects] == [
            canonical_id_to_ifc_guid(f"wall:{token}")
        ]

    # The tokenless wall carries no material association.
    plain = _product(ifc, "wall:plain")
    assert not any(
        rel.is_a("IfcRelAssociatesMaterial") for rel in plain.HasAssociations
    )


def test_two_glazed_walls_share_one_glass_material() -> None:
    ifc = to_ifc(
        _model(
            _wall("wall:glazed-a", token="glazed"),
            _wall("wall:glazed-b", token="glazed"),
        )
    )

    materials = ifc.by_type("IfcMaterial")
    assert len(materials) == 1
    assert (materials[0].Name, materials[0].Category) == ("Glass", "glass")
    (rel,) = ifc.by_type("IfcRelAssociatesMaterial")
    assert rel.GlobalId == canonical_id_to_ifc_guid(f"{_KEY_PREFIX}glazed")
    assert [item.GlobalId for item in rel.RelatedObjects] == [
        canonical_id_to_ifc_guid("wall:glazed-a"),
        canonical_id_to_ifc_guid("wall:glazed-b"),
    ]


def test_glazed_body_carries_the_shared_translucent_style() -> None:
    # The glazed wall has two centerline segments, so its Body holds two
    # solids: both must carry the one shared style.
    ifc = to_ifc(
        _model(
            _wall(
                "wall:glazed",
                token="glazed",
                points=(Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0), Point3(x=4, y=3, z=0)),
            ),
            _wall("wall:framed", token="framed"),
        )
    )

    styles = ifc.by_type("IfcSurfaceStyle")
    assert len(styles) == 1
    style = styles[0]
    (rendering,) = [item for item in style.Styles if item.is_a("IfcSurfaceStyleRendering")]
    assert rendering.Transparency == pytest.approx(_GLASS_TRANSPARENCY)
    assert (rendering.SurfaceColour.Red, rendering.SurfaceColour.Green, rendering.SurfaceColour.Blue) == pytest.approx(
        _GLASS_COLOUR
    )

    glazed_items = _body(_product(ifc, "wall:glazed")).Items
    assert len(glazed_items) == 2
    for item in glazed_items:
        styled = item.StyledByItem
        assert len(styled) == 1
        assert list(styled[0].Styles) == [style]

    # A framed wall gets no style: the translucent surface is glazing only.
    for item in _body(_product(ifc, "wall:framed")).Items:
        assert item.StyledByItem == ()


def test_glazed_wall_without_body_gets_material_but_no_style() -> None:
    # This centerline is vertical, so the wall gets no Body: nothing to
    # style, but the material association still applies.
    ifc = to_ifc(
        _model(
            _wall(
                "wall:glazed-void",
                token="glazed",
                points=(Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=3)),
            )
        )
    )

    (rel,) = ifc.by_type("IfcRelAssociatesMaterial")
    assert rel.RelatingMaterial.Name == "Glass"
    assert [item.GlobalId for item in rel.RelatedObjects] == [
        canonical_id_to_ifc_guid("wall:glazed-void")
    ]

    product = _product(ifc, "wall:glazed-void")
    assert _adapter_pset(product)["Body"] == "no"
    assert "vertical" in str(_adapter_pset(product)["BodyReason"])
    assert product.Representation is None or not _has_body_representation(product)
    assert ifc.by_type("IfcSurfaceStyle") == []
    assert ifc.by_type("IfcStyledItem") == []


def _has_body_representation(product) -> bool:
    if product.Representation is None:
        return False
    return any(
        shape.RepresentationIdentifier == "Body"
        for shape in product.Representation.Representations
    )


def test_round_trip_preserves_tokens() -> None:
    model = _four_token_model()
    result = round_trip(model)
    validate_model(result)
    assert result.to_dict() == model.to_dict()
    assert {wall.id: wall.construction for wall in result.walls} == {
        "wall:glazed": "glazed",
        "wall:masonry": "masonry",
        "wall:concrete": "concrete",
        "wall:framed": "framed",
        "wall:plain": None,
    }


def test_wall_material_key_prefix_is_the_plain_literal() -> None:
    assert _WALL_MATERIAL_KEY_PREFIX == "wall-construction-material:"


def test_token_exports_are_byte_identical(tmp_path: Path) -> None:
    model = _four_token_model()
    to_ifc(model, tmp_path / "first.ifc")
    to_ifc(model, tmp_path / "second.ifc")
    assert (tmp_path / "first.ifc").read_bytes() == (tmp_path / "second.ifc").read_bytes()


def test_material_file_passes_strict_validation(tmp_path: Path) -> None:
    ifc = to_ifc(_four_token_model(), tmp_path / "materials.ifc")
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []
