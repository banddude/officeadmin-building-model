"""Independent source-PDF floor aliases must retain genuine conflicts."""
from pathlib import Path

import pytest

from test_pdf_architecture_legend_levels import _source
from oabm.model import stable_id
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import drawing_level_names, import_observations


@pytest.mark.parametrize("ordinal,word", [
    ("1ST", "FIRST"), ("2ND", "SECOND"), ("3RD", "THIRD"),
    ("4TH", "FOURTH"), ("5TH", "FIFTH"), ("6TH", "SIXTH"),
])
def test_matching_floor_title_aliases_share_one_level(tmp_path: Path, ordinal: str, word: str) -> None:
    source = _source(tmp_path / "aliases.pdf", titles=(f"{ordinal} FLOOR PLAN", f"{word} FLOOR PLAN"))
    document = extract_pdf(source, source_id="fixture:floor-aliases")
    assert drawing_level_names(document.pages[0]) == (f"{word.capitalize()} Floor",)
    # Both original source labels remain available, unchanged.
    assert {f"{ordinal} FLOOR PLAN", f"{word} FLOOR PLAN"} <= {item.text for item in document.pages[0].texts}
    model = import_observations(document)
    assert [level.name for level in model.levels] == [f"{word.capitalize()} Floor"]
    assert model.levels[0].id == stable_id("level", f"fixture:floor-aliases|level:{word.lower()}-floor")
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    assert model.to_json() == import_observations(document).to_json()


@pytest.mark.parametrize("titles", [
    ("1ST FLOOR PLAN", "SECOND FLOOR PLAN"),
    ("2ND FLOOR PLAN", "THIRD FLOOR PLAN"),
    ("SECOND FLOOR PLAN", "LEVEL: 2"),
    ("GROUND FLOOR PLAN", "MAIN FLOOR PLAN"),
    ("2TH FLOOR PLAN", "SECOND FLOOR PLAN"),
])
def test_non_alias_titles_remain_conflicting(tmp_path: Path, titles: tuple[str, ...]) -> None:
    document = extract_pdf(_source(tmp_path / "conflict.pdf", titles=titles))
    assert len(drawing_level_names(document.pages[0])) == 2
    model = import_observations(document)
    assert not model.levels
    assert "level_ambiguous" in {row["code"] for row in model.attributes["pdf_architecture"]["ambiguities"]}


def test_single_numeric_floor_title_keeps_existing_name(tmp_path: Path) -> None:
    document = extract_pdf(
        _source(tmp_path / "single.pdf", titles=("2ND FLOOR PLAN",)),
        source_id="fixture:floor-aliases",
    )
    model = import_observations(document)
    assert drawing_level_names(document.pages[0]) == ("2nd Floor",)
    assert [level.name for level in model.levels] == ["2nd Floor"]
    assert model.levels[0].id == stable_id("level", "fixture:floor-aliases|level:2nd-floor")


def test_separate_sheet_aliases_do_not_inherit_the_other_floor_datum(tmp_path: Path) -> None:
    # Deliberate scope limit: co-present aliases are resolved, but this change
    # does not migrate the canonical identity of an already resolved sheet.
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    for index, title in enumerate(("2ND FLOOR PLAN", "SECOND FLOOR PLAN")):
        source = _source(tmp_path / f"sheet-{index}.pdf", titles=(title,))
        writer.add_page(PdfReader(source).pages[0])
        if index:
            # A different room width prevents identical-drawing suppression.
            page = writer.pages[-1]
            stream = DecodedStreamObject()
            stream.set_data(page["/Contents"].get_object().get_data()
                            .replace(b"300 200 re", b"360 200 re")
                            .replace(b"282 182 re", b"342 182 re"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    source = tmp_path / "separate-sheets.pdf"
    with source.open("wb") as handle:
        writer.write(handle)
    document = extract_pdf(source, source_id="fixture:cross-sheet-alias-limit")
    assert drawing_level_names(document.pages[0]) == ("2nd Floor",)
    assert drawing_level_names(document.pages[1]) == ("Second Floor",)
    model = import_observations(document)
    assert [level.name for level in model.levels] == ["2nd Floor"]
    assert {level.id for level in model.levels} == {
        stable_id("level", "fixture:cross-sheet-alias-limit|level:2nd-floor"),
    }
    assert any(
        row["page"] == 2 and row["code"] == "level_elevation_unresolved"
        and "Second Floor" in row["detail"]
        for row in model.attributes["pdf_architecture"]["ambiguities"]
    )
