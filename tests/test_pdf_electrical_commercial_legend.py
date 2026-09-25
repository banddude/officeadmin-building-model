"""Generated-PDF tests for the commercial reflected-ceiling recognition gap.

The commercial pilot's reflected ceiling plan recognized nothing because of
four stacked facts, each mimicked here by a generated public-safe PDF:

- its legend title carries no lighting cue word (a reflected ceiling
  legend), so it was never read as a lighting legend;
- its fixture rows sit hundreds of points below that title, far past the
  lighting-legend vertical span cap;
- the fixture squares are stroked polylines that return to their start
  point without a closepath operator, so they were never glyphs;
- the linear fixtures are drawn to scale as long, thin stroked rectangles
  far past the small-glyph size cap, and their tags must be associated
  with a run by the tag's printed text box, never by its anchor point
  alone.
"""

import json
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.pdf_electrical import (
    POINT_TO_M,
    ElectricalPdfImporter,
    extract_pdf,
)
from oabm.model import validate_model

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"

_DISPLAYED_WIDTH = 1224.0
_DISPLAYED_HEIGHT = 792.0

_HEADING = "REFLECTED CEILING LEGEND"
_HEADING_X = 700.0
_HEADING_Y = 700.0
_SYMBOL_X = 712.0
_LABEL_X = 724.0
_DESCRIPTION_X = 752.0


def _schema_validator() -> Draft202012Validator:
    with SCHEMA_PATH.open() as handle:
        return Draft202012Validator(json.load(handle))


def _square_closed(cx: float, cy: float, side: float) -> tuple[tuple[float, float], ...]:
    half = side / 2.0
    return (
        (cx - half, cy - half),
        (cx + half, cy - half),
        (cx + half, cy + half),
        (cx - half, cy + half),
    )


def _bar_rect(
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
) -> tuple[tuple[float, float], ...]:
    return (
        (x_start, y_start),
        (x_end, y_start),
        (x_end, y_end),
        (x_start, y_end),
    )


class _RcpContent:
    """Displayed-space drawing commands for one generated RCP page."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def text(self, x: float, y: float, value: str, *, size: float = 8.0) -> None:
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.commands.append(
            f"BT /F1 {size:.3f} Tf 1 0 0 1 {x:.3f} {y:.3f} Tm ({escaped}) Tj ET"
        )

    def path(
        self,
        points: tuple[tuple[float, float], ...],
        *,
        close: bool = False,
        repeat_start: bool = False,
    ) -> None:
        drawn = list(points)
        if repeat_start:
            drawn.append(drawn[0])
        parts = [f"{drawn[0][0]:.3f} {drawn[0][1]:.3f} m"]
        parts.extend(f"{x:.3f} {y:.3f} l" for x, y in drawn[1:])
        parts.append("h S" if close else "S")
        self.commands.append(" ".join(parts))

    def legend_square(self, cy: float, *, side: float = 8.0) -> None:
        self.path(_square_closed(_SYMBOL_X, cy, side), close=True)

    def legend_bar(self, cy: float, length: float, width: float) -> None:
        self.path(
            _bar_rect(
                _SYMBOL_X - length / 2.0,
                _SYMBOL_X + length / 2.0,
                cy - width / 2.0,
                cy + width / 2.0,
            ),
            # The commercial sheets draw outlines as stroked polylines that
            # return to their start point without a closepath operator.
            repeat_start=True,
        )


def _finish_rows() -> tuple[tuple[float, str, str], ...]:
    return (
        (655.0, "P1", "PAINTED GYP CEILING"),
        (605.0, "AC", "SUSPENDED ACOUSTICAL CEILING TILE"),
        (555.0, "EX", "ILLUMINATED EXIT SIGN"),
        (455.0, "MX", "SUSPENDED CEILING GRID MATRIX"),
        (405.0, "RF", "CEILING ACCESS PANEL"),
        (355.0, "SP", "SOUND SYSTEM SPEAKER"),
        (150.0, "CT", "FIRE-TREATED CEILING POCKET"),
    )


def _draw_standard_legend(content: _RcpContent) -> None:
    """Legend block mimicking the commercial RCP's printed structure."""

    content.text(_HEADING_X, _HEADING_Y, _HEADING, size=11.0)
    for y, label, description in _finish_rows():
        content.legend_square(y)
        content.text(_LABEL_X, y, label)
        content.text(_DESCRIPTION_X, y, description, size=7.0)
    # A status marker row names a light fixture but defines scope, never a
    # fixture type.
    content.text(_LABEL_X, 505.0, "E")
    content.text(
        _DESCRIPTION_X,
        505.0,
        "INDICATES EXISTING LIGHT FIXTURE TO REMAIN",
        size=7.0,
    )
    # The fixture rows sit 400 to 500 points below the legend title.
    content.path(_square_closed(_SYMBOL_X, 300.0, 12.0), repeat_start=True)
    content.text(_LABEL_X, 300.0, "LF-2")
    content.text(_DESCRIPTION_X, 300.0, "RECESSED LIGHT FIXTURE", size=7.0)
    content.legend_bar(250.0, 60.0, 6.0)
    content.text(_LABEL_X, 250.0, "LF-3")
    content.text(_DESCRIPTION_X, 250.0, "LINEAR LIGHT FIXTURE", size=7.0)
    content.legend_bar(200.0, 72.0, 4.0)
    content.text(_LABEL_X, 200.0, "LF-5A")
    content.text(
        _DESCRIPTION_X,
        200.0,
        "CONTINUOUS LINEAR LIGHT FIXTURE",
        size=7.0,
    )
    # The next section title ends the legend body.
    content.text(_HEADING_X, 100.0, "GENERAL NOTES", size=9.0)


def _draw_field_fixtures(content: _RcpContent) -> None:
    """Field fixtures mimicking the commercial RCP's drawn instances."""

    # Unclosed fixture squares drawn to their start point, no closepath.
    for cx, cy in ((120.0, 560.0), (260.0, 480.0), (180.0, 350.0)):
        content.path(_square_closed(cx, cy, 12.0), repeat_start=True)
        content.text(cx + 18.0, cy, "LF-2")
    # Linear runs drawn to scale, tags printed along the run. One tag's
    # anchor deliberately sits 1.1 pt from a ceiling-grid double line while
    # its run is under the far end of the printed text.
    content.path(_bar_rect(80.0, 200.0, 620.0, 626.0), repeat_start=True)
    content.text(110.0, 632.0, "LF-3")
    content.path(_bar_rect(300.0, 396.0, 560.0, 564.0), repeat_start=True)
    content.text(320.0, 570.0, "LF-5A")
    content.path(((321.1, 540.0), (321.1, 600.0)))
    content.path(((322.3, 540.0), (322.3, 600.0)))
    # A run whose width matches no legend bar stays unresolved.
    content.path(_bar_rect(80.0, 146.0, 240.0, 250.0), repeat_start=True)
    content.text(90.0, 256.0, "LF-3")
    # A tag printed between two runs at the same distance stays unresolved.
    content.path(_bar_rect(200.0, 320.0, 300.0, 306.0), repeat_start=True)
    content.path(_bar_rect(200.0, 320.0, 330.0, 336.0), repeat_start=True)
    content.text(230.0, 315.2, "LF-3")
    # A tag whose printed size is not a plausible drawn size fails closed.
    content.commands.append(
        "BT /F1 0.250 Tf 1 0 0 1 430.000 640.000 Tm (LF-3) Tj ET"
    )


def _draw_fixture_schedule(content: _RcpContent) -> None:
    content.text(70.0, 100.0, "LIGHTING FIXTURE SCHEDULE", size=11.0)
    for x, label in (
        (70.0, "TYPE"),
        (125.0, "DESCRIPTION"),
        (240.0, "LAMP"),
        (300.0, "WATTS"),
        (355.0, "MOUNTING"),
        (450.0, "MANUFACTURER"),
    ):
        content.text(x, 80.0, label, size=7.0)
    for row_index, row in enumerate(
        (
            ("LF-3", "LINEAR LED", "LED", "24W", "SURFACE", "BETA"),
            ("LF-5A", "CONT LINEAR LED", "LED", "30W", "SURFACE", "BETA"),
        )
    ):
        y = 55.0 - row_index * 25.0
        for x, value in zip((70.0, 125.0, 240.0, 300.0, 355.0, 450.0), row):
            content.text(x, y, value, size=7.0)


def _write_rcp_pdf(
    path: Path,
    content: _RcpContent,
    *,
    page_rotation: int = 0,
) -> None:
    if page_rotation not in {0, 90, 180, 270}:
        raise AssertionError("page_rotation must be a quarter turn")
    if page_rotation in {90, 270}:
        source_width = _DISPLAYED_HEIGHT
        source_height = _DISPLAYED_WIDTH
    else:
        source_width = _DISPLAYED_WIDTH
        source_height = _DISPLAYED_HEIGHT

    writer = PdfWriter()
    page = writer.add_blank_page(width=source_width, height=source_height)
    if page_rotation:
        page[NameObject("/Rotate")] = NumberObject(page_rotation)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )

    def raw_point(x: float, y: float) -> tuple[float, float]:
        if page_rotation == 0:
            return x, y
        if page_rotation == 90:
            return source_width - y, x
        if page_rotation == 180:
            return source_width - x, source_height - y
        return y, source_height - x

    raw_commands: list[str] = []
    for command in content.commands:
        parts = command.split()
        rebuilt: list[str] = []
        index = 0
        while index < len(parts):
            token = parts[index]
            if token in {"m", "l"} and index >= 2:
                x, y = float(parts[index - 2]), float(parts[index - 1])
                rx, ry = raw_point(x, y)
                rebuilt[-2:] = [f"{rx:.3f}", f"{ry:.3f}", token]
                index += 1
            elif token == "Tm" and index >= 6:
                tx, ty = float(parts[index - 2]), float(parts[index - 1])
                rtx, rty = raw_point(tx, ty)
                rebuilt[-2:] = [f"{rtx:.3f}", f"{rty:.3f}", token]
                index += 1
            else:
                rebuilt.append(token)
                index += 1
        raw_commands.append(" ".join(rebuilt))

    stream = DecodedStreamObject()
    stream.set_data("\n".join(raw_commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)

    with path.open("wb") as handle:
        writer.write(handle)


def _full_page_content() -> _RcpContent:
    content = _RcpContent()
    _draw_standard_legend(content)
    _draw_field_fixtures(content)
    _draw_fixture_schedule(content)
    return content


def _minimal_linear_legend(content: _RcpContent, *rows: tuple[str, float, float]) -> None:
    """Heading plus at least two linear fixture rows.

    Rows are (tag, bar length, bar width). A legend needs two confirmed
    rows before its entries count, so a lone row is paired with an
    unrelated second fixture row that has no field instances.
    """

    if len(rows) < 2:
        rows = (*rows, ("LF-6", 48.0, 5.0))
    content.text(_HEADING_X, _HEADING_Y, _HEADING, size=11.0)
    y = 660.0
    for tag, length, width in rows:
        content.legend_bar(y, length, width)
        content.text(_LABEL_X, y, tag)
        content.text(
            _DESCRIPTION_X,
            y,
            "LINEAR LIGHT FIXTURE",
            size=7.0,
        )
        y -= 60.0


def _luminaires(model) -> list:
    return [
        device
        for device in model.electrical_devices
        if device.device_type == "luminaire"
    ]


def _lighting_unresolved(model) -> list[dict]:
    return [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("kind") == "lighting_fixture"
    ]


@pytest.mark.parametrize("page_rotation", [0, 270])
def test_reflected_ceiling_legend_recovers_fixture_rows_and_linear_runs(
    tmp_path: Path,
    page_rotation: int,
) -> None:
    pdf_path = tmp_path / f"commercial-rcp-{page_rotation}.pdf"
    _write_rcp_pdf(pdf_path, _full_page_content(), page_rotation=page_rotation)

    extracted = extract_pdf(
        pdf_path,
        source_id=f"fixture:commercial-rcp:{page_rotation}",
    )
    assert extracted == extract_pdf(
        pdf_path,
        source_id=f"fixture:commercial-rcp:{page_rotation}",
    )

    model = ElectricalPdfImporter().import_document(extracted)
    luminaires = _luminaires(model)
    assert Counter(device.name for device in luminaires) == Counter(
        {"LF-2": 3, "LF-3": 1, "LF-5A": 1}
    )

    glyph = [
        device
        for device in luminaires
        if device.name == "LF-2"
    ]
    assert len(glyph) == 3
    for device in glyph:
        recognition = device.attributes["pdf_electrical"]["lighting_recognition"]
        assert recognition["method"] == "lighting-fixture-letter-tag"
        assert recognition["fixture_tag"] == "LF-2"
        assert recognition["tag_is_primary_type_evidence"] is True

    linear = {
        device.name: device
        for device in luminaires
        if device.attributes["pdf_electrical"]["lighting_recognition"]["method"]
        == "lighting-linear-fixture-tag"
    }
    assert set(linear) == {"LF-3", "LF-5A"}
    expected_runs = {
        "LF-3": (120.0, 6.0, 140.0, 623.0),
        "LF-5A": (96.0, 4.0, 348.0, 562.0),
    }
    for tag, device in linear.items():
        length, width, x, y = expected_runs[tag]
        recognition = device.attributes["pdf_electrical"]["lighting_recognition"]
        run = recognition["linear_run"]
        assert run["printed_length_pt"] == length
        assert run["printed_width_pt"] == width
        assert recognition["tag_is_primary_type_evidence"] is True
        assert recognition["legend"]["prototype_geometry_key"]
        assert device.pose.position.x == pytest.approx(x * POINT_TO_M)
        assert device.pose.position.y == pytest.approx(y * POINT_TO_M)
        methods = {item.method for item in device.provenance}
        assert {
            "pdf-lighting-fixture-geometry",
            "pdf-lighting-fixture-tag",
            "pdf-lighting-legend-tag",
            "pdf-lighting-fixture-schedule",
        } <= methods
        assert recognition["fixture_schedule"]["tag"] == tag

    # The grid double line beside the LF-5A tag anchor must not be treated
    # as the run and must not steal the association.
    assert linear["LF-5A"].attributes["pdf_electrical"]["lighting_recognition"][
        "linear_run"
    ]["tag_gap_pt"] == pytest.approx(6.0)

    unresolved = _lighting_unresolved(model)
    assert {item["reason_code"] for item in unresolved} == {
        "lighting_fixture_symbol_mismatch",
        "lighting_fixture_tag_association_ambiguous",
        "lighting_linear_tag_extent_unknown",
    }
    mismatched = next(
        item
        for item in unresolved
        if item["reason_code"] == "lighting_fixture_symbol_mismatch"
    )
    assert mismatched["run_width_pt"] == 10.0
    ambiguous = next(
        item
        for item in unresolved
        if item["reason_code"] == "lighting_fixture_tag_association_ambiguous"
    )
    assert len(ambiguous["candidate_geometry_keys"]) == 2

    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["legend_type"] == "lighting"
    assert lighting["recognized_fixture_count"] == 5
    assert lighting["recognized_linear_fixture_count"] == 2
    assert lighting["recognized_switch_count"] == 0
    assert lighting["unresolved_fixture_count"] == 3
    assert lighting["fixture_tags"] == ["LF-2", "LF-3", "LF-5A"]
    region = lighting["legend_regions"][0]
    assert region["heading_kind"] == "general-legend"
    assert region["heading_text"] == _HEADING
    assert region["row_count"] == 3
    assert region["tags"] == ["LF-2", "LF-3", "LF-5A"]
    assert region["linear_tags"] == ["LF-3", "LF-5A"]
    assert region["body_span_pt"] == 555.0
    assert region["heading_x_pt"] == 700.0
    assert region["grid_rows_y_pt"] == [
        655.0, 605.0, 555.0, 505.0, 455.0, 405.0,
        355.0, 300.0, 250.0, 200.0, 150.0,
    ]
    assert lighting["fixture_schedules"][0]["row_count"] == 2

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_reflected_ceiling_legend_recognition_is_stable(tmp_path: Path) -> None:
    pdf_path = tmp_path / "commercial-rcp-stable.pdf"
    _write_rcp_pdf(pdf_path, _full_page_content())
    extracted = extract_pdf(pdf_path, source_id="fixture:commercial-rcp-stable")

    first = ElectricalPdfImporter().import_document(extracted)
    second = ElectricalPdfImporter().import_document(extracted)
    first_ids = sorted(device.id for device in first.electrical_devices)
    second_ids = sorted(device.id for device in second.electrical_devices)
    assert first_ids == second_ids
    assert len(first_ids) == 5


def test_tiny_font_linear_tag_fails_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "commercial-rcp-tiny-font.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("LF-3", 60.0, 6.0))
    content.path(_bar_rect(80.0, 200.0, 620.0, 626.0), repeat_start=True)
    content.commands.append(
        "BT /F1 0.250 Tf 1 0 0 1 110.000 632.000 Tm (LF-3) Tj ET"
    )
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-tiny-font")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_linear_tag_extent_unknown"
    ]
    assert unresolved[0]["font_size_pt"] == pytest.approx(0.25)


def test_linear_tag_between_two_equal_runs_fails_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "commercial-rcp-ambiguous.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("LF-3", 60.0, 6.0))
    content.path(_bar_rect(200.0, 320.0, 300.0, 306.0), repeat_start=True)
    content.path(_bar_rect(200.0, 320.0, 330.0, 336.0), repeat_start=True)
    content.text(230.0, 315.2, "LF-3")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-ambiguous")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_fixture_tag_association_ambiguous"
    ]
    assert len(unresolved[0]["candidate_geometry_keys"]) == 2


def test_linear_run_of_wrong_width_fails_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "commercial-rcp-width.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("LF-3", 60.0, 6.0))
    content.path(_bar_rect(80.0, 146.0, 240.0, 250.0), repeat_start=True)
    content.text(90.0, 256.0, "LF-3")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-width")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_fixture_symbol_mismatch"
    ]
    assert unresolved[0]["run_width_pt"] == 10.0
    assert unresolved[0]["prototype_width_pt"] == 6.0


def test_two_tags_on_one_linear_run_fail_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "commercial-rcp-two-tags.pdf"
    content = _RcpContent()
    _minimal_linear_legend(
        content,
        ("LF-3", 60.0, 6.0),
        ("LF-4", 72.0, 4.0),
    )
    content.path(_bar_rect(80.0, 200.0, 620.0, 626.0), repeat_start=True)
    content.text(110.0, 632.0, "LF-3")
    content.text(150.0, 610.0, "LF-4")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-two-tags")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_fixture_tag_ambiguous"
    ]
    assert unresolved[0]["candidate_tags"] == ["LF-3", "LF-4"]


def test_linear_tag_printed_away_from_any_run_is_not_evidence(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "commercial-rcp-tag-far.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("LF-3", 60.0, 6.0))
    content.path(_bar_rect(80.0, 200.0, 620.0, 626.0), repeat_start=True)
    content.text(110.0, 650.0, "LF-3")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-tag-far")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []


def test_short_legend_bar_recognizes_its_scaled_field_run(
    tmp_path: Path,
) -> None:
    """A legend bar under the glyph cap still acts as a linear prototype."""

    pdf_path = tmp_path / "commercial-rcp-short-bar.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("LF-4", 40.0, 6.0))
    content.path(_bar_rect(80.0, 200.0, 620.0, 626.0), repeat_start=True)
    content.text(110.0, 632.0, "LF-4")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-short-bar")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    luminaires = _luminaires(model)
    assert [device.name for device in luminaires] == ["LF-4"]
    recognition = luminaires[0].attributes["pdf_electrical"][
        "lighting_recognition"
    ]
    assert recognition["method"] == "lighting-linear-fixture-tag"
    assert recognition["linear_run"]["printed_length_pt"] == 120.0
    assert recognition["linear_run"]["printed_width_pt"] == 6.0


def test_closepath_with_repeated_start_recognized_as_rectangle(
    tmp_path: Path,
) -> None:
    """The same rectangle with an explicit closepath and repeated start."""

    pdf_path = tmp_path / "commercial-rcp-repeat-start.pdf"
    content = _RcpContent()
    content.text(_HEADING_X, _HEADING_Y, _HEADING, size=11.0)
    content.path(
        _bar_rect(_SYMBOL_X - 30.0, _SYMBOL_X + 30.0, 657.0, 663.0),
        close=True,
        repeat_start=True,
    )
    content.text(_LABEL_X, 660.0, "LF-3")
    content.text(_DESCRIPTION_X, 660.0, "LINEAR LIGHT FIXTURE", size=7.0)
    content.path(
        _bar_rect(_SYMBOL_X - 24.0, _SYMBOL_X + 24.0, 597.0, 603.0),
        close=True,
        repeat_start=True,
    )
    content.text(_LABEL_X, 600.0, "LF-6")
    content.text(_DESCRIPTION_X, 600.0, "LINEAR LIGHT FIXTURE", size=7.0)
    content.path(
        _bar_rect(80.0, 200.0, 620.0, 626.0),
        close=True,
        repeat_start=True,
    )
    content.text(110.0, 632.0, "LF-3")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-repeat-start")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    luminaires = _luminaires(model)
    assert [device.name for device in luminaires] == ["LF-3"]
    recognition = luminaires[0].attributes["pdf_electrical"][
        "lighting_recognition"
    ]
    assert recognition["method"] == "lighting-linear-fixture-tag"


def test_non_luminaire_general_legend_rows_stay_out_of_lighting(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "commercial-rcp-finish-only.pdf"
    content = _RcpContent()
    content.text(_HEADING_X, _HEADING_Y, _HEADING, size=11.0)
    for y, label, description in _finish_rows():
        content.legend_square(y)
        content.text(_LABEL_X, y, label)
        content.text(_DESCRIPTION_X, y, description, size=7.0)
    content.text(_HEADING_X, 100.0, "GENERAL NOTES", size=9.0)
    # A field tag beside a symbol never makes a fixture type on its own.
    content.path(_square_closed(120.0, 560.0, 12.0), close=True)
    content.text(138.0, 560.0, "P1")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-finish-only")
    )
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["recognized_fixture_count"] == 0
    assert lighting["fixture_tags"] == []


def test_status_marker_row_never_becomes_a_fixture_prototype(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "commercial-rcp-status-row.pdf"
    content = _RcpContent()
    content.text(_HEADING_X, _HEADING_Y, _HEADING, size=11.0)
    content.text(_LABEL_X, 660.0, "E")
    content.text(
        _DESCRIPTION_X,
        660.0,
        "INDICATES EXISTING LIGHT FIXTURE TO REMAIN",
        size=7.0,
    )
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:commercial-rcp-status-row")
    )
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["recognized_fixture_count"] == 0
    assert "E" not in lighting["fixture_tags"]
