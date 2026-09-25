"""Generated-PDF tests for general-legend lighting and linear fixtures.

Every fixture here is synthetic: invented legend wording, tags, row order,
coordinates and sizes. Together they exercise four CAD drawing conventions
that kept ceiling-plan lighting from being recognized:

- a legend whose title carries no lighting cue word (a plain symbols
  legend) and mixes fixture rows with other symbols, so it is read as a
  lighting legend only through rows whose own description names a
  luminaire;
- fixture rows printed well below that title, past the lighting-legend
  vertical span cap, inside one continuous legend body;
- fixture outlines stroked as polylines that return to their start point
  without a closepath operator;
- linear fixtures drawn to scale as long, thin stroked rectangles past the
  small-glyph size cap, whose tags associate with a run by the tag's
  printed text box, never by its anchor point alone.
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

_HEADING = "OVERHEAD PLAN SYMBOLS"
_HEADING_SIZE = 10.0
_HEADING_X = 820.0
_HEADING_Y = 760.0
_SYMBOL_X = 832.0
_LABEL_X = 846.0
_DESCRIPTION_X = 878.0
_TAG_SIZE = 7.5
_DESCRIPTION_SIZE = 6.5
_NOTES_TITLE = "SHEET NOTES"
_NOTES_Y = 300.0


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
    """Displayed-space drawing commands for one generated ceiling-plan page."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: float = _TAG_SIZE,
    ) -> None:
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

    def heading(self) -> None:
        self.text(_HEADING_X, _HEADING_Y, _HEADING, size=_HEADING_SIZE)

    def row_text(self, cy: float, label: str, description: str) -> None:
        self.text(_LABEL_X, cy, label)
        self.text(_DESCRIPTION_X, cy, description, size=_DESCRIPTION_SIZE)

    def legend_square(self, cy: float, *, side: float = 7.0) -> None:
        self.path(_square_closed(_SYMBOL_X, cy, side), close=True)

    def legend_bar(self, cy: float, length: float, width: float) -> None:
        self.path(
            _bar_rect(
                _SYMBOL_X - length / 2.0,
                _SYMBOL_X + length / 2.0,
                cy - width / 2.0,
                cy + width / 2.0,
            ),
            # A stroked polyline returning to its start point, with no
            # closepath operator.
            repeat_start=True,
        )


def _other_symbol_rows() -> tuple[tuple[float, str, str], ...]:
    """Invented non-luminaire rows: tag-like labels, no luminaire word."""

    return (
        (716.0, "TX", "TEXTURED PLASTER SOFFIT"),
        (628.0, "DF", "SUPPLY AIR DIFFUSER"),
        (496.0, "RG", "RETURN AIR GRILLE"),
        (452.0, "HT", "CEILING HATCH"),
        (364.0, "WS", "EGRESS SIGN, WALL"),
    )


def _draw_standard_legend(content: _RcpContent) -> None:
    """A symbols legend mixing fixture rows with other symbols."""

    content.heading()
    for y, label, description in _other_symbol_rows():
        content.legend_square(y)
        content.row_text(y, label, description)
    # A status marker row names a luminaire but defines scope, never a
    # fixture type.
    content.row_text(584.0, "N", "DENOTES NEW LUMINAIRE")
    # The glyph fixture row: an unclosed square that returns to its start.
    content.path(_square_closed(_SYMBOL_X, 672.0, 10.0), repeat_start=True)
    content.row_text(672.0, "K7", "SQUARE RECESSED DOWNLIGHT")
    content.legend_bar(540.0, 66.0, 5.0)
    content.row_text(540.0, "KW-2", "SURFACE LINEAR LUMINAIRE")
    # This row sits farther below the title than the lighting-legend span
    # cap; only the continuous legend body admits it.
    content.legend_bar(408.0, 80.0, 3.5)
    content.row_text(408.0, "KW-8C", "PENDANT SLOT LUMINAIRE")
    # The next section title ends the legend body.
    content.text(_HEADING_X, _NOTES_Y, _NOTES_TITLE, size=9.0)


def _draw_field_fixtures(content: _RcpContent) -> None:
    """Invented field instances for the legend above."""

    # Unclosed fixture squares drawn back to their start point.
    for cx, cy in ((140.0, 700.0), (300.0, 660.0), (460.0, 700.0)):
        content.path(_square_closed(cx, cy, 10.0), repeat_start=True)
        content.text(cx + 15.0, cy, "K7")
    # Linear runs drawn to scale, tags printed along the run.
    content.path(_bar_rect(80.0, 212.0, 560.0, 565.0), repeat_start=True)
    content.text(104.0, 571.0, "KW-2")
    # This tag's anchor point sits beside a grid double line crossing the
    # run, while the run itself lies under the tag's printed text box.
    content.path(_bar_rect(300.0, 404.0, 480.0, 483.5), repeat_start=True)
    content.text(322.0, 489.0, "KW-8C")
    content.path(((323.7, 460.0), (323.7, 520.0)))
    content.path(((325.1, 460.0), (325.1, 520.0)))
    # A run whose width matches no legend bar stays unresolved.
    content.path(_bar_rect(480.0, 558.0, 560.0, 569.0), repeat_start=True)
    content.text(492.0, 575.0, "KW-2")
    # A tag printed between two runs at the same distance stays unresolved.
    content.path(_bar_rect(80.0, 212.0, 380.0, 385.0), repeat_start=True)
    content.path(_bar_rect(80.0, 212.0, 408.25, 413.25), repeat_start=True)
    content.text(104.0, 394.0, "KW-2")
    # A tag whose printed size is not a plausible drawn size fails closed.
    content.commands.append(
        "BT /F1 0.400 Tf 1 0 0 1 520.000 640.000 Tm (KW-2) Tj ET"
    )


def _draw_fixture_schedule(content: _RcpContent) -> None:
    content.text(80.0, 220.0, "LUMINAIRE SCHEDULE", size=10.0)
    columns = (80.0, 130.0, 250.0, 305.0, 370.0, 440.0)
    for x, label in zip(
        columns,
        ("MARK", "DESC", "LAMPS", "WATTAGE", "MOUNT", "MFR"),
    ):
        content.text(x, 200.0, label, size=_DESCRIPTION_SIZE)
    for row_index, row in enumerate(
        (
            ("KW-2", "SURFACE LINEAR", "LED", "18W", "CEILING", "MAKER A"),
            ("KW-8C", "PENDANT SLOT", "LED", "36W", "STEM", "MAKER A"),
        )
    ):
        y = 178.0 - row_index * 22.0
        for x, value in zip(columns, row):
            content.text(x, y, value, size=_DESCRIPTION_SIZE)


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
        rows = (*rows, ("KW-11", 50.0, 4.0))
    content.heading()
    y = 700.0
    for tag, length, width in rows:
        content.legend_bar(y, length, width)
        content.row_text(y, tag, "STRIP LUMINAIRE")
        y -= 52.0


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


@pytest.mark.parametrize("page_rotation", [0, 90, 270])
def test_general_legend_recovers_fixture_rows_and_linear_runs(
    tmp_path: Path,
    page_rotation: int,
) -> None:
    pdf_path = tmp_path / f"general-legend-{page_rotation}.pdf"
    _write_rcp_pdf(pdf_path, _full_page_content(), page_rotation=page_rotation)

    extracted = extract_pdf(
        pdf_path,
        source_id=f"fixture:general-legend:{page_rotation}",
    )
    assert extracted == extract_pdf(
        pdf_path,
        source_id=f"fixture:general-legend:{page_rotation}",
    )

    model = ElectricalPdfImporter().import_document(extracted)
    luminaires = _luminaires(model)
    assert Counter(device.name for device in luminaires) == Counter(
        {"K7": 3, "KW-2": 1, "KW-8C": 1}
    )

    glyph = [
        device
        for device in luminaires
        if device.name == "K7"
    ]
    assert len(glyph) == 3
    for device in glyph:
        recognition = device.attributes["pdf_electrical"]["lighting_recognition"]
        assert recognition["method"] == "lighting-fixture-letter-tag"
        assert recognition["fixture_tag"] == "K7"
        assert recognition["tag_is_primary_type_evidence"] is True

    linear = {
        device.name: device
        for device in luminaires
        if device.attributes["pdf_electrical"]["lighting_recognition"]["method"]
        == "lighting-linear-fixture-tag"
    }
    assert set(linear) == {"KW-2", "KW-8C"}
    expected_runs = {
        "KW-2": (132.0, 5.0, 146.0, 562.5),
        "KW-8C": (104.0, 3.5, 352.0, 481.75),
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

    # The grid double line beside the KW-8C tag anchor must not be treated
    # as the run and must not steal the association.
    assert linear["KW-8C"].attributes["pdf_electrical"]["lighting_recognition"][
        "linear_run"
    ]["tag_gap_pt"] == pytest.approx(5.5)

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
    assert mismatched["run_width_pt"] == 9.0
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
    assert lighting["fixture_tags"] == ["K7", "KW-2", "KW-8C"]
    region = lighting["legend_regions"][0]
    assert region["heading_kind"] == "general-legend"
    assert region["heading_text"] == _HEADING
    assert region["row_count"] == 3
    assert region["tags"] == ["K7", "KW-2", "KW-8C"]
    assert region["linear_tags"] == ["KW-2", "KW-8C"]
    assert region["body_span_pt"] == 401.0
    assert region["heading_x_pt"] == 820.0
    assert region["grid_rows_y_pt"] == [
        716.0, 672.0, 628.0, 584.0, 540.0,
        496.0, 452.0, 408.0, 364.0,
    ]
    assert lighting["fixture_schedules"][0]["row_count"] == 2

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_general_legend_recognition_is_stable(tmp_path: Path) -> None:
    pdf_path = tmp_path / "general-legend-stable.pdf"
    _write_rcp_pdf(pdf_path, _full_page_content())
    extracted = extract_pdf(pdf_path, source_id="fixture:general-legend-stable")

    first = ElectricalPdfImporter().import_document(extracted)
    second = ElectricalPdfImporter().import_document(extracted)
    first_ids = sorted(device.id for device in first.electrical_devices)
    second_ids = sorted(device.id for device in second.electrical_devices)
    assert first_ids == second_ids
    assert len(first_ids) == 5


def test_tiny_font_linear_tag_fails_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "linear-tiny-font.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("KW-2", 66.0, 5.0))
    content.path(_bar_rect(80.0, 212.0, 560.0, 565.0), repeat_start=True)
    content.commands.append(
        "BT /F1 0.400 Tf 1 0 0 1 104.000 571.000 Tm (KW-2) Tj ET"
    )
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-tiny-font")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_linear_tag_extent_unknown"
    ]
    assert unresolved[0]["font_size_pt"] == pytest.approx(0.4)


def test_linear_tag_between_two_equal_runs_fails_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "linear-ambiguous.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("KW-2", 66.0, 5.0))
    content.path(_bar_rect(80.0, 212.0, 380.0, 385.0), repeat_start=True)
    content.path(_bar_rect(80.0, 212.0, 408.25, 413.25), repeat_start=True)
    content.text(104.0, 394.0, "KW-2")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-ambiguous")
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
    pdf_path = tmp_path / "linear-width.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("KW-2", 66.0, 5.0))
    content.path(_bar_rect(80.0, 158.0, 240.0, 249.0), repeat_start=True)
    content.text(92.0, 255.0, "KW-2")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-width")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_fixture_symbol_mismatch"
    ]
    assert unresolved[0]["run_width_pt"] == 9.0
    assert unresolved[0]["prototype_width_pt"] == 5.0


def test_two_tags_on_one_linear_run_fail_closed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "linear-two-tags.pdf"
    content = _RcpContent()
    _minimal_linear_legend(
        content,
        ("KW-2", 66.0, 5.0),
        ("KW-9", 80.0, 3.5),
    )
    content.path(_bar_rect(80.0, 212.0, 560.0, 565.0), repeat_start=True)
    content.text(104.0, 571.0, "KW-2")
    content.text(150.0, 549.0, "KW-9")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-two-tags")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    unresolved = _lighting_unresolved(model)
    assert [item["reason_code"] for item in unresolved] == [
        "lighting_fixture_tag_ambiguous"
    ]
    assert unresolved[0]["candidate_tags"] == ["KW-2", "KW-9"]


def test_linear_tag_printed_away_from_any_run_is_not_evidence(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "linear-tag-far.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("KW-2", 66.0, 5.0))
    content.path(_bar_rect(80.0, 212.0, 560.0, 565.0), repeat_start=True)
    content.text(104.0, 590.0, "KW-2")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-tag-far")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []


def test_short_legend_bar_recognizes_its_scaled_field_run(
    tmp_path: Path,
) -> None:
    """A legend bar under the glyph cap still acts as a linear prototype."""

    pdf_path = tmp_path / "linear-short-bar.pdf"
    content = _RcpContent()
    _minimal_linear_legend(content, ("KW-9", 44.0, 5.0))
    content.path(_bar_rect(80.0, 212.0, 560.0, 565.0), repeat_start=True)
    content.text(104.0, 571.0, "KW-9")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-short-bar")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    luminaires = _luminaires(model)
    assert [device.name for device in luminaires] == ["KW-9"]
    recognition = luminaires[0].attributes["pdf_electrical"][
        "lighting_recognition"
    ]
    assert recognition["method"] == "lighting-linear-fixture-tag"
    assert recognition["linear_run"]["printed_length_pt"] == 132.0
    assert recognition["linear_run"]["printed_width_pt"] == 5.0


def test_closepath_with_repeated_start_recognized_as_rectangle(
    tmp_path: Path,
) -> None:
    """The same rectangle with an explicit closepath and repeated start."""

    pdf_path = tmp_path / "linear-repeat-start.pdf"
    content = _RcpContent()
    content.heading()
    content.path(
        _bar_rect(_SYMBOL_X - 33.0, _SYMBOL_X + 33.0, 687.5, 692.5),
        close=True,
        repeat_start=True,
    )
    content.row_text(690.0, "KW-2", "STRIP LUMINAIRE")
    content.path(
        _bar_rect(_SYMBOL_X - 25.0, _SYMBOL_X + 25.0, 635.5, 640.5),
        close=True,
        repeat_start=True,
    )
    content.row_text(638.0, "KW-11", "STRIP LUMINAIRE")
    content.path(
        _bar_rect(80.0, 212.0, 560.0, 565.0),
        close=True,
        repeat_start=True,
    )
    content.text(104.0, 571.0, "KW-2")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:linear-repeat-start")
    )
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert [region["row_count"] for region in lighting["legend_regions"]] == [2]
    luminaires = _luminaires(model)
    assert [device.name for device in luminaires] == ["KW-2"]
    recognition = luminaires[0].attributes["pdf_electrical"][
        "lighting_recognition"
    ]
    assert recognition["method"] == "lighting-linear-fixture-tag"


def test_non_luminaire_general_legend_rows_stay_out_of_lighting(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "general-legend-other-rows-only.pdf"
    content = _RcpContent()
    content.heading()
    for y, label, description in _other_symbol_rows():
        content.legend_square(y)
        content.row_text(y, label, description)
    content.text(_HEADING_X, _NOTES_Y, _NOTES_TITLE, size=9.0)
    # A field tag beside a symbol never makes a fixture type on its own.
    content.path(_square_closed(140.0, 520.0, 10.0), close=True)
    content.text(155.0, 520.0, "TX")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:general-legend-other-rows-only")
    )
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["recognized_fixture_count"] == 0
    assert lighting["fixture_tags"] == []


def test_status_marker_row_never_becomes_a_fixture_prototype(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "general-legend-status-row.pdf"
    content = _RcpContent()
    content.heading()
    content.row_text(700.0, "R", "RELOCATED LUMINAIRE, REUSE HOUSING")
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:general-legend-status-row")
    )
    assert _luminaires(model) == []
    assert _lighting_unresolved(model) == []
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["recognized_fixture_count"] == 0
    assert "R" not in lighting["fixture_tags"]


def _power_legend_content(*, field_triangles: tuple[tuple[float, float, bool], ...]) -> _RcpContent:
    """A geometry-only power sheet: a symbol legend plus field triangles.

    Each field triangle is (x, y, closed). A closed one is drawn with the
    closepath operator; an open one is a stroked polyline that returns to its
    start point without it.
    """

    def triangle(cx: float, cy: float) -> tuple[tuple[float, float], ...]:
        return ((cx, cy + 7.0), (cx + 7.0, cy - 7.0), (cx - 7.0, cy - 7.0))

    content = _RcpContent()
    content.text(370.0, 365.0, "ELECTRICAL SYMBOL LEGEND", size=11.0)
    # GFCI: a square with a centre line.
    content.path(_square_closed(395.0, 320.0, 10.0), close=True)
    content.path(((388.0, 320.0), (402.0, 320.0)))
    content.text(425.0, 317.0, "GFCI", size=9.0)
    # JBOX: one closed triangle.
    content.path(triangle(395.0, 272.0), close=True)
    content.text(425.0, 269.0, "JBOX", size=9.0)
    for x, y, closed in field_triangles:
        content.path(triangle(x, y), close=closed, repeat_start=not closed)
    return content


def test_power_legend_path_keeps_unclosed_outlines_out_of_glyphs(
    tmp_path: Path,
) -> None:
    """Unclosed outlines are glyphs only where a readable tag types them.

    On the power-device path the legend geometry alone assigns the type, so a
    stroked polyline that merely returns to its start stays out of glyph
    matching exactly as before; only the tag-confirmed lighting path reads it
    as an outline. The closed triangle is the positive control.
    """

    pdf_path = tmp_path / "power-unclosed-outline.pdf"
    content = _power_legend_content(
        field_triangles=((120.0, 200.0, True), (220.0, 200.0, False)),
    )
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:power-unclosed-outline")
    )
    devices = [
        device
        for device in model.electrical_devices
        if device.attributes["pdf_electrical"]
        .get("shape_recognition", {})
        .get("method")
        == "sheet-legend-geometry-match"
    ]
    assert [device.device_type for device in devices] == ["junction_box"]
    assert devices[0].pose.position.x == pytest.approx(120.0 * POINT_TO_M)
    assert _luminaires(model) == []


def _circle_points(cx: float, cy: float, radius: float) -> tuple[tuple[float, float], ...]:
    import math

    return tuple(
        (
            cx + radius * math.cos(2.0 * math.pi * index / 16),
            cy + radius * math.sin(2.0 * math.pi * index / 16),
        )
        for index in range(16)
    )


def test_unconfirmed_general_legend_leaves_its_rows_to_the_power_path(
    tmp_path: Path,
) -> None:
    """A power symbol legend is not a lighting legend unless it confirms.

    The JB row's label reads like a fixture tag, and the light row printed
    just below lends it a luminaire word, so the lighting path finds the JB
    label with two equally near glyphs. That one ambiguous row never
    confirms a lighting legend, so it must not claim the JB label: the power
    path reads the legend exactly as it would without the lighting pass and
    types the field boxes, and no lighting legend diagnostics are reported
    for a legend that is not one.
    """

    pdf_path = tmp_path / "power-legend-light-neighbour.pdf"
    content = _RcpContent()
    content.text(370.0, 400.0, "ELECTRICAL SYMBOL LEGEND", size=11.0)
    content.path(_square_closed(385.0, 360.0, 8.0), close=True)
    content.text(398.0, 357.0, "JB", size=6.0)
    content.path(_circle_points(385.0, 346.0, 4.0), close=True)
    content.text(400.0, 343.0, "RECESSED LIGHT", size=6.0)
    for x in (100.0, 160.0):
        content.path(_square_closed(x, 200.0, 8.0), close=True)
    _write_rcp_pdf(pdf_path, content)

    model = ElectricalPdfImporter().import_document(
        extract_pdf(pdf_path, source_id="fixture:power-legend-light-neighbour")
    )
    boxes = sorted(
        device.pose.position.x
        for device in model.electrical_devices
        if device.device_type == "junction_box"
    )
    assert boxes == pytest.approx([100.0 * POINT_TO_M, 160.0 * POINT_TO_M])
    assert _luminaires(model) == []
    assert [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if str(item.get("kind", "")).startswith("lighting_legend")
    ] == []
    lighting = model.attributes["pdf_electrical"]["lighting_recognition"]
    assert lighting["legend_regions"] == []
