"""Source-PDF regression: elevation-tag legend samples are not building levels."""

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import drawing_level_names, import_observations


def _source(
    path: Path,
    *,
    heading: bool = True,
    placeholder: str | None = "+/- XXXX",
    label: bool = True,
    sample_x: float = 600,
    sample_y: float = 400,
) -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(width=1000, height=700)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })

    def text(x: float, y: float, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET"

    commands = [
        text(80, 50, "FIRST FLOOR PLAN"),
        text(80, 70, "SCALE: 1/4\" = 1'-0\""),
        text(80, 90, "LEVEL CEILING HEIGHT: 9'-0\""),
        text(160, 240, "ROOM: BEDROOM"),
        "100 180 300 200 re S",
        "109 189 282 182 re S",
        text(sample_x, sample_y, "Level 2"),
    ]
    if heading:
        commands.append(text(580, 470, "TAGS"))
    if placeholder:
        commands.append(text(600, 414, placeholder))
    if label:
        commands.append(text(665, 404, "FINISH FLOOR ELEV."))
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


def test_labeled_placeholder_legend_does_not_invent_a_second_level(tmp_path: Path) -> None:
    source = _source(tmp_path / "legend-level.pdf")
    document = extract_pdf(source, source_id="fixture:legend-level")
    assert drawing_level_names(document.pages[0]) == ("First Floor",)
    model = import_observations(document)
    assert [level.name for level in model.levels] == ["First Floor"]
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    assert all(wall.thickness_m == pytest.approx(0.1524) for wall in model.walls)
    assert all(wall.height_m == pytest.approx(2.7432) for wall in model.walls)
    lane = model.attributes["pdf_architecture"]
    assert "level_ambiguous" not in {row["code"] for row in lane["ambiguities"]}
    assert any(text.text == "Level 2" for text in document.pages[0].texts)
    excluded = lane["pages"][0]["excluded_level_examples"]
    assert len(excluded) == 1
    assert excluded[0]["source_text"] == "Level 2"
    assert excluded[0]["source_element_id"] in {text.element_id for text in document.pages[0].texts}
    assert excluded[0]["reason_code"] == "labeled_placeholder_elevation_tag_in_legend"
    repeated = import_observations(extract_pdf(source, source_id="fixture:legend-level"))
    assert model.to_json() == repeated.to_json()


@pytest.mark.parametrize("changes", [
    {"heading": False},
    {"placeholder": None},
    {"placeholder": "+12'-0\""},
    {"label": False},
    {"sample_x": 250, "sample_y": 300},
])
def test_incomplete_or_nonlocal_legend_evidence_preserves_real_level_conflict(
    tmp_path: Path, changes: dict,
) -> None:
    document = extract_pdf(_source(tmp_path / "real-conflict.pdf", **changes))
    assert set(drawing_level_names(document.pages[0])) == {"First Floor", "2"}
    model = import_observations(document)
    assert model.levels == ()
    assert "level_ambiguous" in {
        row["code"] for row in model.attributes["pdf_architecture"]["ambiguities"]
    }
