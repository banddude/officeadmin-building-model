"""Assumed level heights carry scoped inferred provenance.

A plan that prints no level or ceiling height still yields a level, walls,
and spaces: the importer assigns a low-confidence default so geometric wall
faces can materialize. The entities' unscoped records describe what the sheet
showed (centerlines, thicknesses, footprints) and stay observed; the one
assumption is stated separately, inferred and scoped to ``height_m``. These
tests pin that split on synthetic generated PDFs through the real
``extract_pdf``/``import_architectural_pdf`` path, the ``provenance_applies_to``
scope rule, and the exported ``OABM_Provenance`` property set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ifcopenshell.util.element
import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from oabm.ifc import canonical_id_to_ifc_guid, to_ifc
from oabm.importers.pdf_architecture import import_architectural_pdf
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    validate_model,
)
from oabm.model import provenance_applies_to

SOURCE_ID = "fixture:assumed-height-provenance"

# Synthetic sheet annotations. The wording reuses only the importer's own
# parser vocabulary; the drawing, values, and placement are invented here.
TEXT_LINES = (
    "A7.3 FLOOR PLAN",
    "SCALE: 1:100",
    "LEVEL: GROUND",
)
PRINTED_HEIGHT_LINE = "LEVEL CEILING HEIGHT: 10'-0\""
PRINTED_HEIGHT_M = 3.048
ASSUMED_HEIGHT_M = 2.7432


def _write_wall_face_plan(path: Path, *, printed_height: bool) -> None:
    """A synthetic CAD-like sheet: two nested wall-face rectangles as lines.

    The face pairs are 4 pt apart and meet at the corners, the same topology
    the geometric pairing path accepts as one closed room with four walls.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=340, height=260)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        # Without an explicit encoding the extractor reads the font's
        # StandardEncoding, where 0x27 is a typographic apostrophe and the
        # feet-inches dimension pattern no longer matches.
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    commands = [
        # South face pair
        "50 50 m 250 50 l S", "50 54 m 250 54 l S",
        # East face pair
        "246 50 m 246 150 l S", "250 50 m 250 150 l S",
        # North face pair
        "50 146 m 250 146 l S", "50 150 m 250 150 l S",
        # West face pair
        "50 50 m 50 150 l S", "54 50 m 54 150 l S",
    ]
    commands.extend(
        f"BT /F1 10 Tf 1 0 0 1 20 {230 - index * 14} Tm ({line}) Tj ET"
        for index, line in enumerate(TEXT_LINES)
    )
    if printed_height:
        commands.append(
            f"BT /F1 10 Tf 1 0 0 1 20 {230 - len(TEXT_LINES) * 14} Tm "
            f"({PRINTED_HEIGHT_LINE}) Tj ET"
        )
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _import_plan(tmp_path: Path, name: str, *, printed_height: bool) -> Any:
    source = tmp_path / name
    _write_wall_face_plan(source, printed_height=printed_height)
    return import_architectural_pdf(source, source_id=SOURCE_ID)


def _height_assumption(entity: Any) -> list[Any]:
    return [
        record
        for record in entity.provenance
        if record.derivation == DERIVATION_INFERRED
        and record.scope_paths == ("height_m",)
    ]


def _unscoped_records(entity: Any) -> list[Any]:
    return [record for record in entity.provenance if record.scope_paths is None]


def _ambiguity_codes(model: Any) -> set[str]:
    return {
        item["code"]
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    }


def test_plan_without_printed_height_states_the_assumption_on_every_height_user(
    tmp_path: Path,
) -> None:
    source = tmp_path / "assumed-height-plan.pdf"
    _write_wall_face_plan(source, printed_height=False)
    extracted = extract_pdf(source, source_id=SOURCE_ID)
    assert extracted.pages[0].lines, "the synthetic plan must carry vector lines"

    model = import_architectural_pdf(source, source_id=SOURCE_ID)
    validate_model(model)

    assert len(model.levels) == 1
    assert model.walls
    assert model.spaces
    assert "level_height_default_assumed" in _ambiguity_codes(model)
    assert model.levels[0].height_m == pytest.approx(ASSUMED_HEIGHT_M)

    entities: tuple[Any, ...] = (*model.levels, *model.walls, *model.spaces)
    for entity in entities:
        assumed = _height_assumption(entity)
        assert len(assumed) == 1, entity.id
        record = assumed[0]
        assert record.confidence == pytest.approx(0.45)
        assert record.attributes["assumed_value_m"] == pytest.approx(
            ASSUMED_HEIGHT_M
        )
        assert "assumed default level height" in (record.method or "")
        # The claims the sheet DID show stay observed and entity-wide.
        unscoped = _unscoped_records(entity)
        assert unscoped, entity.id
        assert {
            record.derivation for record in unscoped
        } == {DERIVATION_OBSERVED}, entity.id

    for wall in model.walls:
        assert wall.height_m == pytest.approx(ASSUMED_HEIGHT_M)

    # Deterministic output, and provenance does not disturb identity.
    repeat = import_architectural_pdf(source, source_id=SOURCE_ID)
    assert repeat.to_json() == model.to_json()


def test_scope_rule_covers_height_and_nothing_else(tmp_path: Path) -> None:
    model = _import_plan(
        tmp_path, "scope-rule-plan.pdf", printed_height=False
    )
    record = _height_assumption(model.walls[0])[0]

    assert provenance_applies_to(record, ("height_m",)) is True
    assert provenance_applies_to(record, ("centerline",)) is False
    assert provenance_applies_to(record, ("centerline", "height_m")) is True


def test_plan_with_printed_height_gets_no_assumption_record(
    tmp_path: Path,
) -> None:
    model = _import_plan(
        tmp_path, "printed-height-plan.pdf", printed_height=True
    )
    validate_model(model)

    assert model.levels[0].height_m == pytest.approx(PRINTED_HEIGHT_M)
    assert all(
        wall.height_m == pytest.approx(PRINTED_HEIGHT_M)
        for wall in model.walls
    )
    assert "level_height_default_assumed" not in _ambiguity_codes(model)
    for entity in (*model.levels, *model.walls, *model.spaces):
        assert _height_assumption(entity) == [], entity.id


def test_ifc_pset_reads_observed_with_named_height_claim(
    tmp_path: Path,
) -> None:
    model = _import_plan(
        tmp_path, "ifc-assumed-height-plan.pdf", printed_height=False
    )
    wall = model.walls[0]

    ifc = to_ifc(model)
    exported = ifc.by_guid(canonical_id_to_ifc_guid(wall.id))
    psets = ifcopenshell.util.element.get_psets(exported).get(
        "OABM_Provenance", {}
    )
    assert psets, "the wall must export the provenance property set"
    # The unscoped records still classify the wall as observed; the one
    # assumption surfaces as the named per-claim scope beside it.
    assert psets["Derivation"] == "observed"
    assert psets["InferredClaims"] == "height_m"


def test_caller_default_height_cannot_be_reported_as_observed_quantity(tmp_path: Path) -> None:
    from oabm.importers.pdf_architecture import ImportOptions
    from oabm.quantities import extract_quantities

    source = tmp_path / "caller-default-height.pdf"
    _write_wall_face_plan(source, printed_height=False)
    model = import_architectural_pdf(source, source_id=SOURCE_ID,
        options=ImportOptions(default_wall_height_m=3.25))
    validate_model(model)
    for entity in (*model.levels, *model.walls, *model.spaces):
        assert entity.height_m == pytest.approx(3.25)
        records = _height_assumption(entity)
        assert len(records) == 1
        assert "ImportOptions.default_wall_height_m" in records[0].method
    rows = extract_quantities(model).to_dict()["items"]
    faces = [row for row in rows if row["category"] == "wall_face_area"]
    assert faces and all(row["quantity_derivation"] == "inferred" for row in faces)
    lengths = [row for row in rows if row["category"] == "wall_length"]
    assert lengths and all(row["quantity_derivation"] == "observed" for row in lengths)


def test_unused_caller_default_does_not_taint_printed_height_quantities(tmp_path: Path) -> None:
    from oabm.importers.pdf_architecture import ImportOptions
    from oabm.quantities import extract_quantities

    source = tmp_path / "printed-wins-over-default.pdf"
    _write_wall_face_plan(source, printed_height=True)
    model = import_architectural_pdf(source, source_id=SOURCE_ID,
        options=ImportOptions(default_wall_height_m=3.25))
    assert all(w.height_m == pytest.approx(PRINTED_HEIGHT_M) for w in model.walls)
    rows = extract_quantities(model).to_dict()["items"]
    faces = [row for row in rows if row["category"] == "wall_face_area"]
    assert faces and all(row["quantity_derivation"] == "observed" for row in faces)


def test_explicit_level_height_override_is_user_evidence_not_observed(tmp_path: Path) -> None:
    from oabm.importers.pdf_architecture import ImportOptions, LevelOverride
    from oabm.quantities import extract_quantities

    source = tmp_path / "explicit-height-override.pdf"
    _write_wall_face_plan(source, printed_height=True)
    options = ImportOptions(level_overrides=(LevelOverride(
        page_number=1, elevation_m=0, name="GROUND", height_m=3.25,
        note="synthetic caller-selected height",
    ),))
    model = import_architectural_pdf(source, source_id=SOURCE_ID, options=options)
    validate_model(model)
    for entity in (*model.levels, *model.walls, *model.spaces):
        assert entity.height_m == pytest.approx(3.25)
        records = [p for p in entity.provenance if p.derivation == "user" and p.scope_paths == ("height_m",)]
        assert len(records) == 1
    rows = extract_quantities(model).to_dict()["items"]
    faces = [row for row in rows if row["category"] == "wall_face_area"]
    assert faces and all(row["quantity_derivation"] == "user" for row in faces)
    lengths = [row for row in rows if row["category"] == "wall_length"]
    assert lengths and all(row["quantity_derivation"] == "observed" for row in lengths)
