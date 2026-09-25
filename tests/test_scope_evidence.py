"""#72: scope evidence for devices without a legend-resolved marker.

Every case is a generated PDF run through the real extractor and importer:
either the public synthetic notes-column legend fixture with an edited content
stream (a general note, a field marker, an added comment annotation), or a
small generated lighting sheet. No private plan data is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_electrical import (
    ElectricalPdfImporter,
    UserScopeAssumption,
    extract_pdf,
)
from oabm.model import BuildingModel, is_observed, validate_model
from oabm.qa.takeoff_comparison import device_scope_counts

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "pdf_electrical" / "geometry-only-power-sheet-notes-column-legend.pdf"
# A synthetic stand-in for a caller's explicit rule. The library carries no
# real person's decision about a real document set; the caller supplies both
# strings, and the importer records them verbatim with user derivation.
ASSUMPTION = UserScopeAssumption(
    rule="Synthetic test rule: unmarked items on these generated sheets are new work, except items marked (E)",
    source="synthetic test fixture",
)
# The N marker of the quadruplex outlet drawn at (406, 500), and the N marker
# of the power J-box at (190, 280).
QUAD_MARKER = b"1 0 0 1 416 504 Tm (N) Tj"
JBOX_MARKER = b"1 0 0 1 200 284 Tm (N) Tj"
QUAD_POSITION = (406.0, 500.0)
JBOX_POSITION = (190.0, 280.0)
# The two general-note lines of the fixture's notes column.
NOTE_LINE_1 = b"BT /F1 5.5 Tf 1 0 0 1 596 494 Tm (All geometry is synthetic.) Tj ET"
NOTE_LINE_2 = b"BT /F1 5.5 Tf 1 0 0 1 596 486 Tm (Fixture for legend matching.) Tj ET"
UNMARK_QUAD = (QUAD_MARKER, QUAD_MARKER.replace(b"(N)", b"( )"))
UNMARK_JBOX = (JBOX_MARKER, JBOX_MARKER.replace(b"(N)", b"( )"))


def _runs(y: float, *runs: tuple[float, str]) -> bytes:
    """One note line as separate text runs on one baseline, as CAD exports it."""

    return b"\n".join(
        f"BT /F1 5.5 Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({text}) Tj ET".encode("ascii")
        for x, text in runs
    )


def _notes(line_1: bytes, line_2: bytes) -> tuple[tuple[bytes, bytes], tuple[bytes, bytes]]:
    return (NOTE_LINE_1, line_1), (NOTE_LINE_2, line_2)


def _square_comment(
    writer: PdfWriter, x: float, y: float, contents: str, author: str
) -> DictionaryObject:
    return writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Square"),
                NameObject("/Rect"): ArrayObject(
                    [FloatObject(x - 2), FloatObject(y - 3), FloatObject(x + 2), FloatObject(y + 3)]
                ),
                NameObject("/Contents"): TextStringObject(contents),
                NameObject("/T"): TextStringObject(author),
                NameObject("/F"): NumberObject(64),
                NameObject("/Border"): ArrayObject([NumberObject(0)] * 3),
                NameObject("/NM"): TextStringObject(f"synthetic-{x:.0f}-{y:.0f}"),
            }
        )
    )


def _variant(
    tmp_path: Path,
    name: str,
    *edits: tuple[bytes, bytes],
    append: bytes = b"",
    comments: tuple[tuple[float, float, str, str], ...] = (),
) -> Path:
    writer = PdfWriter(clone_from=PdfReader(FIXTURE))
    page = writer.pages[0]
    data = page.get_contents().get_data()
    for old, new in edits:
        assert data.count(old) == 1, old
        data = data.replace(old, new)
    stream = DecodedStreamObject()
    stream.set_data(data + b"\n" + append)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if comments:
        page[NameObject("/Annots")] = ArrayObject(
            _square_comment(writer, *comment) for comment in comments
        )
    path = tmp_path / f"{name}.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _model(path: Path, assumption: UserScopeAssumption | None = None):
    model = ElectricalPdfImporter(user_scope_assumption=assumption).import_document(
        extract_pdf(path, source_id=f"fixture:{path.stem}")
    )
    validate_model(model)
    return model


def _lane_at(model, position: tuple[float, float]) -> dict:
    matches = [
        device.attributes["pdf_electrical"]
        for device in model.electrical_devices
        if abs(device.attributes["pdf_electrical"]["source_position_pt"]["x"] - position[0]) < 1.0
        and abs(device.attributes["pdf_electrical"]["source_position_pt"]["y"] - position[1]) < 1.0
    ]
    assert len(matches) == 1, position
    return matches[0]


def _scope_totals(model) -> dict[str, int]:
    return {key: value for key, value in device_scope_counts(model)["totals"].items() if value}


def test_note_naming_new_and_existing_keeps_unmarked_outlets_unresolved(tmp_path: Path) -> None:
    # A numbered note naming both scopes, split into four runs on one baseline
    # and continuing onto a second line, the way CAD exports often print it.
    path = _variant(
        tmp_path,
        "note-new-or-existing",
        UNMARK_QUAD,
        *_notes(
            _runs(
                494,
                (596, "4."),
                (604, "ALL OUTLETS SHOWN ON THIS SHEET ARE"),
                (706, "NEW / EXISTING"),
                (749, "U.O.N. FIELD VERIFY"),
            ),
            _runs(486, (604, "EACH LOCATION BEFORE ROUGH-IN.")),
        ),
    )
    model = _model(path)
    lane = _lane_at(model, QUAD_POSITION)
    assert lane["scope_status"] == "unresolved"
    assert lane["scope_reason"] == "scope_default_note_ambiguous"
    assert lane["scope_note_ambiguity"] == "names_new_and_existing"
    assert lane["scope_note_family"] == "outlets"
    assert lane["scope_note_text"].startswith(
        "4. ALL OUTLETS SHOWN ON THIS SHEET ARE NEW / EXISTING U.O.N."
    )
    assert "BEFORE ROUGH-IN" in lane["scope_note_text"]
    assert len(lane["scope_note_source_element_ids"]) == 5
    assert "scope_marker" not in lane
    counts = device_scope_counts(model)
    assert counts["unresolved_reasons"] == {"scope_default_note_ambiguous": 1}
    assert counts["in_scope_total"] == 7


@pytest.mark.parametrize(
    ("scope_word", "expected", "in_scope"),
    (("EXISTING", "existing_to_remain", 6), ("NEW", "new", 7)),
)
def test_outlet_note_default_applies_to_unmarked_outlets_only(
    tmp_path: Path, scope_word: str, expected: str, in_scope: int
) -> None:
    # The clause wraps onto a second line; the next numbered note ends it.
    path = _variant(
        tmp_path,
        f"note-{scope_word.lower()}",
        UNMARK_QUAD,
        UNMARK_JBOX,
        *_notes(
            _runs(494, (596, "4."), (604, "ALL OUTLETS SHOWN ON THIS SHEET")),
            _runs(486, (604, f"ARE {scope_word} U.O.N."))
            + b"\n"
            + _runs(478, (596, "5."), (604, "EXISTING CEILING TO REMAIN.")),
        ),
    )
    model = _model(path)
    quad = _lane_at(model, QUAD_POSITION)
    assert quad["scope_status"] == expected
    assert quad["scope_method"] == "sheet general note default for unmarked devices"
    assert quad["scope_note_family"] == "outlets"
    assert quad["scope_note_text"] == f"4. ALL OUTLETS SHOWN ON THIS SHEET ARE {scope_word} U.O.N."
    assert "scope_marker" not in quad
    # A J-box is not an outlet: the note does not reach it.
    jbox = _lane_at(model, JBOX_POSITION)
    assert (jbox["scope_status"], jbox["scope_reason"]) == ("unresolved", "no_scope_marker")
    # Marked outlets keep their own marker's scope: the default is only U.O.N.
    marked = [
        device.attributes["pdf_electrical"]
        for device in model.electrical_devices
        if device.attributes["pdf_electrical"].get("scope_marker")
    ]
    assert len(marked) == 14
    assert all(lane["scope_method"] == "sheet status legend" for lane in marked)
    assert device_scope_counts(model)["in_scope_total"] == in_scope


def test_note_default_yields_to_scope_wording_beside_the_device(tmp_path: Path) -> None:
    # "Unless otherwise noted": "(N) ..." printed beside the unmarked quad notes
    # it otherwise, so the EXISTING default must not reach it. The other
    # unmarked outlet, with nothing beside it, still takes the default.
    path = _variant(
        tmp_path,
        "note-otherwise-noted",
        UNMARK_QUAD,
        (b"1 0 0 1 308 504 Tm (E) Tj", b"1 0 0 1 308 504 Tm ( ) Tj"),
        *_notes(
            _runs(494, (596, "4."), (604, "ALL OUTLETS SHOWN ON THIS SHEET")),
            _runs(486, (604, "ARE EXISTING U.O.N.")),
        ),
        append=b"BT /F1 5 Tf 1 0 0 1 396 484 Tm ((N) SYNTHETIC CALLOUT) Tj ET\n",
    )
    model = _model(path)
    quad = _lane_at(model, QUAD_POSITION)
    assert quad["scope_status"] == "unresolved"
    assert quad["scope_reason"] == "scope_default_note_otherwise_noted"
    assert len(quad["scope_otherwise_noted_source_element_ids"]) == 1
    other = _lane_at(model, (298.0, 500.0))
    assert other["scope_status"] == "existing_to_remain"
    assert other["scope_method"] == "sheet general note default for unmarked devices"
    # The assumption does not override the sheet's own "otherwise noted".
    on = _lane_at(_model(path, ASSUMPTION), QUAD_POSITION)
    assert on["scope_reason"] == "scope_default_note_otherwise_noted"
    assert not any(key.startswith("scope_assumption_") for key in on)


def test_note_default_qualified_by_its_own_text_stays_unresolved(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "note-qualified",
        UNMARK_QUAD,
        *_notes(
            _runs(494, (596, "4."), (604, "ALL OUTLETS SHOWN ON PLAN ARE NEW U.O.N.")),
            _runs(486, (604, "REUSE EXISTING OUTLETS WHERE AVAILABLE.")),
        ),
    )
    lane = _lane_at(_model(path), QUAD_POSITION)
    assert lane["scope_status"] == "unresolved"
    assert lane["scope_reason"] == "scope_default_note_ambiguous"
    assert lane["scope_note_ambiguity"] == "default_qualified_by_note_text"


def test_conflicting_family_notes_and_non_notes_do_not_default(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "note-conflict",
        UNMARK_QUAD,
        UNMARK_JBOX,
        *_notes(
            _runs(494, (596, "4. ALL OUTLETS SHOWN ON PLAN ARE NEW U.O.N.")),
            _runs(486, (596, "5. RECEPTACLES SHOWN ON PLAN ARE EXISTING U.O.N."))
            + b"\n"
            # Neither a named electrical family nor "unless otherwise noted".
            + _runs(478, (596, "6. PLUMBING FIXTURES SHOWN ON PLAN ARE EXISTING U.O.N."))
            + b"\n"
            + _runs(470, (596, "7. ALL J-BOXES SHOWN ARE EXISTING.")),
        ),
    )
    model = _model(path)
    quad = _lane_at(model, QUAD_POSITION)
    assert quad["scope_status"] == "unresolved"
    assert quad["scope_reason"] == "scope_default_note_conflict"
    assert len(quad["scope_note_texts"]) == 2
    jbox = _lane_at(model, JBOX_POSITION)
    assert jbox["scope_reason"] == "no_scope_marker"


def test_tied_marker_letters_stay_ambiguous_and_block_the_note_default(tmp_path: Path) -> None:
    # An E exactly as far from the quad's centre as its N, on the other side.
    path = _variant(
        tmp_path,
        "marker-tie",
        *_notes(
            _runs(494, (596, "4. ALL OUTLETS SHOWN ON PLAN ARE EXISTING U.O.N.")),
            _runs(486, (596, "5. SYNTHETIC.")),
        ),
        append=b"BT /F2 6 Tf 1 0 0 1 396 496 Tm (E) Tj ET\n",
    )
    model = _model(path)
    lane = _lane_at(model, QUAD_POSITION)
    assert lane["scope_status"] == "unresolved"
    assert lane["scope_reason"] == "scope_marker_ambiguous"
    assert [item["marker"] for item in lane["scope_marker_candidates"]] == ["E", "N"]
    assert "scope_note_text" not in lane
    assert device_scope_counts(model)["in_scope_total"] == 7


@pytest.mark.parametrize(
    ("author", "expected_marker", "expected_scope"),
    (
        # AutoCAD's read-only text proxy of a printed SHX letter: drawing text.
        ("AutoCAD SHX Text", "E", "existing_to_remain"),
        # Any other comment is a markup, not the drawing's own marker.
        ("Reviewer", "N", "new"),
    ),
)
def test_shx_text_proxy_letter_is_a_printed_status_marker(
    tmp_path: Path, author: str, expected_marker: str, expected_scope: str
) -> None:
    # An E printed in an SHX font 7 pt under the quad, closer than the text N
    # beside it. Only the text proxy comment makes that E readable.
    path = _variant(
        tmp_path,
        f"shx-{expected_marker}",
        comments=((406.0, 493.0, "E", author),),
    )
    model = _model(path)
    lane = _lane_at(model, QUAD_POSITION)
    assert lane["scope_marker"] == expected_marker
    assert lane["status"] == expected_marker
    assert lane["scope_status"] == expected_scope


def test_shx_text_proxy_letter_marks_a_device_with_no_text_marker(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "shx-only",
        UNMARK_QUAD,
        comments=((406.0, 493.0, "E", "AutoCAD SHX Text"),),
    )
    model = _model(path)
    lane = _lane_at(model, QUAD_POSITION)
    assert (lane["scope_marker"], lane["scope_status"]) == ("E", "existing_to_remain")
    assert lane["scope_marker_source_element_id"].endswith(":text")
    assert lane["scope_legend_text"] == "EXISTING TO REMAIN"
    proxies = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("metadata", {}).get("text_proxy") == "autocad_shx_text"
    ]
    assert all("author" not in item["metadata"] for item in proxies)


FIXTURE_SHAPE = ((-7.0, -5.0), (6.0, -5.0), (6.0, 1.0), (1.0, 1.0), (1.0, 6.0), (-7.0, 6.0))


def _write_lighting_scope_sheet(path: Path) -> None:
    """A lighting sheet with the structure the lighting path recognizes: a
    legend of at least three rows and a fixture schedule keyed by type tag,
    plus a status legend ("FOR ALL:") and one general note that defaults the
    sheet's unmarked fixtures to existing."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=1200, height=700)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    commands: list[str] = []

    def text(x: float, y: float, value: str, size: float = 8.0) -> None:
        commands.append(f"BT /F1 {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({value}) Tj ET")

    def fixture(x: float, y: float) -> None:
        points = [(x + px, y + py) for px, py in FIXTURE_SHAPE]
        commands.append(
            " ".join(
                [f"{points[0][0]:.1f} {points[0][1]:.1f} m"]
                + [f"{px:.1f} {py:.1f} l" for px, py in points[1:]]
                + ["h S"]
            )
        )

    text(760, 655, "LIGHTING FIXTURE LEGEND", 11.0)
    for tag, y in (("A", 615.0), ("A1", 585.0), ("B", 555.0)):
        fixture(770, y)
        text(792, y, tag)
    text(620, 465, "LIGHTING FIXTURE SCHEDULE", 11.0)
    for x, label in ((620.0, "TYPE"), (675.0, "DESCRIPTION"), (790.0, "LAMP")):
        text(x, 445, label, 7.0)
    for row, y in (
        (("A", "2X4 LED", "LED"), 420.0),
        (("A1", "DOWNLIGHT", "LED"), 395.0),
        (("B", "LINEAR LED", "LED"), 370.0),
    ):
        for x, value in zip((620.0, 675.0, 790.0), row):
            text(x, y, value, 7.0)
    text(80, 300, "FOR ALL:", 6.0)
    text(80, 290, "R", 6.0)
    text(92, 290, "EXISTING TO BE REMOVED AND SALVAGED FOR RELOCATION", 6.0)
    text(80, 281, "E", 6.0)
    text(92, 281, "EXISTING TO REMAIN", 6.0)
    text(80, 250, "3.", 6.0)
    text(88, 250, "LIGHT FIXTURES SHOWN ON THIS SHEET ARE EXISTING U.O.N.", 6.0)
    for x, markers in (
        (120.0, ()),  # unmarked: the note's default
        (280.0, (("R", 0.0, -14.0),)),  # its own marker: relocated
        (440.0, (("E", 0.0, -14.0), ("R", 0.0, 14.0))),  # tied letters
    ):
        fixture(x, 560.0)
        text(x + 18.0, 560.0, "A")
        for letter, dx, dy in markers:
            text(x + dx, 560.0 + dy, letter, 6.0)
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_light_fixture_note_default_reaches_only_unmarked_luminaires(tmp_path: Path) -> None:
    path = tmp_path / "lighting-scope.pdf"
    _write_lighting_scope_sheet(path)
    model = _model(path)
    by_x = {
        round(device.attributes["pdf_electrical"]["source_position_pt"]["x"]): device.attributes[
            "pdf_electrical"
        ]
        for device in model.electrical_devices
        if device.device_type == "luminaire"
    }
    assert sorted(by_x) == [120, 280, 440]
    unmarked, relocated, tied = by_x[120], by_x[280], by_x[440]
    assert unmarked["scope_status"] == "existing_to_remain"
    assert unmarked["scope_method"] == "sheet general note default for unmarked devices"
    assert unmarked["scope_note_family"] == "light_fixtures"
    assert unmarked["scope_note_text"] == "3. LIGHT FIXTURES SHOWN ON THIS SHEET ARE EXISTING U.O.N."
    assert (relocated["scope_marker"], relocated["scope_status"]) == ("R", "relocated")
    assert relocated["scope_marker_method"] == "status letter beside device position"
    assert tied["scope_reason"] == "scope_marker_ambiguous"
    counts = device_scope_counts(model)
    assert counts["by_type"]["luminaire"]["relocated"] == 1
    assert counts["in_scope_total"] == 1
    again = device_scope_counts(_model(path))
    assert again == counts
    assert json.loads(json.dumps(counts, sort_keys=True)) == counts


def _device_at(model, position: tuple[float, float]):
    matches = [
        device
        for device in model.electrical_devices
        if abs(device.attributes["pdf_electrical"]["source_position_pt"]["x"] - position[0]) < 1.0
        and abs(device.attributes["pdf_electrical"]["source_position_pt"]["y"] - position[1]) < 1.0
    ]
    assert len(matches) == 1, position
    return matches[0]


def _user_records(device) -> list:
    return [record for record in device.provenance if record.derivation == "user"]


def _assumption_keys(lane: dict) -> list[str]:
    return sorted(key for key in lane if key.startswith("scope_assumption_"))


# The E-marked quadruplex outlet west of the quad, at (298, 500), and its marker.
EAST_POSITION = (298.0, 500.0)
EAST_MARKER = b"1 0 0 1 308 504 Tm (E) Tj"
UNMARK_EAST = (EAST_MARKER, EAST_MARKER.replace(b"(E)", b"( )"))
LEGEND_E_LETTER = b"1 0 0 1 596 133 Tm (E) Tj"


def test_user_scope_assumption_is_off_by_default(tmp_path: Path) -> None:
    path = _variant(tmp_path, "assumption-off", UNMARK_QUAD)
    model = _model(path)
    lane = _lane_at(model, QUAD_POSITION)
    assert (lane["scope_status"], lane["scope_reason"]) == ("unresolved", "no_scope_marker")
    assert not _assumption_keys(lane)
    assert not any(_user_records(device) for device in model.electrical_devices)
    assert all(is_observed(device.provenance) for device in model.electrical_devices)


def test_user_scope_assumption_resolves_unmarked_devices_as_new_with_user_provenance(
    tmp_path: Path,
) -> None:
    path = _variant(tmp_path, "assumption-new", UNMARK_QUAD)
    off = _model(path)
    on = _model(path, ASSUMPTION)
    lane = _lane_at(on, QUAD_POSITION)
    assert lane["scope_status"] == "new"
    assert "scope_reason" not in lane
    assert lane["scope_method"] == "user scope assumption"
    assert lane["scope_assumption_derivation"] == "user"
    assert lane["scope_assumption_rule"] == ASSUMPTION.rule
    assert lane["scope_assumption_source"] == ASSUMPTION.source
    # derivation, not the lane keys, is the canonical record: one user record
    # scoped by name to scope_status, carrying the rule and source verbatim.
    device = _device_at(on, QUAD_POSITION)
    (record,) = _user_records(device)
    assert record.source_kind == "caller-scope-assumption"
    assert record.method == "user scope assumption"
    assert record.attributes == {
        "assumed_attribute": "scope_status",
        "scope_status": "new",
        "rule": ASSUMPTION.rule,
        "source": ASSUMPTION.source,
    }
    assert not is_observed(device.provenance)
    # The device's own sheet evidence is still recorded as observed.
    assert any(item.derivation == "observed" for item in device.provenance)
    assert is_observed(_device_at(off, QUAD_POSITION).provenance)
    assert device_scope_counts(on)["in_scope_total"] == (
        device_scope_counts(off)["in_scope_total"] + 1
    )
    # A marker the sheet's own legend resolves keeps its legend provenance.
    # Only the quad was unmarked here; the J-box still carries its own N.
    marked = [
        device
        for device in on.electrical_devices
        if device.attributes["pdf_electrical"].get("scope_marker")
    ]
    assert len(marked) == 15
    assert all(
        device.attributes["pdf_electrical"]["scope_method"] == "sheet status legend"
        and not _user_records(device)
        for device in marked
    )
    # Deterministic across runs, including serialization.
    again = _model(path, ASSUMPTION)
    assert _lane_at(again, QUAD_POSITION) == lane
    assert again.to_json() == on.to_json()
    # The user record survives the canonical JSON round trip.
    reparsed = BuildingModel.from_json(on.to_json())
    assert _user_records(_device_at(reparsed, QUAD_POSITION)) == [record]


def test_user_scope_assumption_e_exception_marks_the_nearest_device_existing(
    tmp_path: Path,
) -> None:
    # A callout "(E) ..." within the association radius of the unmarked quad.
    near = _variant(
        tmp_path,
        "assumption-e-near",
        UNMARK_QUAD,
        append=b"BT /F1 6 Tf 1 0 0 1 406 490 Tm ((E) EXISTING UNIT TO REMAIN) Tj ET\n",
    )
    model = _model(near, ASSUMPTION)
    lane = _lane_at(model, QUAD_POSITION)
    assert lane["scope_status"] == "existing_to_remain"
    assert lane["scope_method"] == "user scope assumption, (E) exception"
    assert lane["scope_assumption_derivation"] == "user"
    (callout_id,) = lane["scope_assumption_existing_marker_source_element_ids"]
    (record,) = _user_records(_device_at(model, QUAD_POSITION))
    assert record.source_element_id == callout_id
    assert record.attributes["scope_status"] == "existing_to_remain"
    assert record.attributes["existing_marker_source_element_ids"] == [callout_id]
    # Beyond the association radius the exception cannot reach the device,
    # which keeps the rule's default.
    far = _variant(
        tmp_path,
        "assumption-e-far",
        UNMARK_QUAD,
        append=b"BT /F1 6 Tf 1 0 0 1 406 350 Tm ((E) EXISTING UNIT TO REMAIN) Tj ET\n",
    )
    far_lane = _lane_at(_model(far, ASSUMPTION), QUAD_POSITION)
    assert far_lane["scope_status"] == "new"
    assert far_lane["scope_method"] == "user scope assumption"


def test_user_scope_assumption_e_callout_between_two_devices_stays_unresolved(
    tmp_path: Path,
) -> None:
    # One "(E)" callout equally near two unmarked devices. Which one it marks is
    # unknown, so neither may default to new and neither may be guessed existing.
    path = _variant(
        tmp_path,
        "assumption-e-tie",
        UNMARK_QUAD,
        UNMARK_EAST,
        append=b"BT /F1 6 Tf 1 0 0 1 352 530 Tm ((E) SYNTHETIC CALLOUT) Tj ET\n",
    )
    model = _model(path, ASSUMPTION)
    for position in (QUAD_POSITION, EAST_POSITION):
        lane = _lane_at(model, position)
        assert lane["scope_status"] == "unresolved"
        assert lane["scope_reason"] == "scope_assumption_exception_ambiguous"
        assert len(lane["scope_assumption_existing_marker_candidate_source_element_ids"]) == 1
        assert "scope_assumption_derivation" not in lane
        assert not _user_records(_device_at(model, position))


@pytest.mark.parametrize(
    "definition",
    (
        b"BT /F1 6 Tf 1 0 0 1 400 490 Tm ((E) = EXISTING) Tj ET\n",
        b"BT /F1 6 Tf 1 0 0 1 400 490 Tm ((E)) Tj ET\n"
        b"BT /F1 6 Tf 1 0 0 1 412 490 Tm (EXISTING) Tj ET\n",
    ),
)
def test_user_scope_assumption_ignores_an_e_abbreviation_definition(
    tmp_path: Path, definition: bytes
) -> None:
    # "(E) = EXISTING" defines the abbreviation; it marks no item.
    path = _variant(tmp_path, "assumption-e-definition", UNMARK_QUAD, append=definition)
    lane = _lane_at(_model(path, ASSUMPTION), QUAD_POSITION)
    assert (lane["scope_status"], lane["scope_method"]) == ("new", "user scope assumption")


@pytest.mark.parametrize(
    ("author", "expected_scope"),
    (
        # A markup comment is not the sheet marking the item.
        ("Reviewer", "new"),
        # An SHX text proxy is the drawing's own printed callout.
        ("AutoCAD SHX Text", "existing_to_remain"),
    ),
)
def test_user_scope_assumption_e_exception_reads_only_drawing_text(
    tmp_path: Path, author: str, expected_scope: str
) -> None:
    path = _variant(
        tmp_path,
        f"assumption-e-comment-{expected_scope}",
        UNMARK_QUAD,
        comments=((406.0, 490.0, "(E) EXISTING UNIT TO REMAIN", author),),
    )
    lane = _lane_at(_model(path, ASSUMPTION), QUAD_POSITION)
    assert lane["scope_status"] == expected_scope


def test_markup_comment_is_not_a_sheet_note_default(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "note-markup",
        UNMARK_QUAD,
        comments=((620.0, 470.0, "ALL OUTLETS SHOWN ON THIS SHEET ARE EXISTING U.O.N.", "Reviewer"),),
    )
    lane = _lane_at(_model(path), QUAD_POSITION)
    assert (lane["scope_status"], lane["scope_reason"]) == ("unresolved", "no_scope_marker")
    assert "scope_note_text" not in lane


def test_user_scope_assumption_never_overrides_sheet_evidence(tmp_path: Path) -> None:
    # A legend-resolved marker keeps its scope ...
    path = _variant(tmp_path, "assumption-legend-wins")
    lane = _lane_at(_model(path, ASSUMPTION), QUAD_POSITION)
    assert (lane["scope_marker"], lane["scope_status"]) == ("N", "new")
    assert lane["scope_method"] == "sheet status legend"
    assert not _assumption_keys(lane)
    # ... even when the rule's (E) exception points at it.
    marked_near_callout = _variant(
        tmp_path,
        "assumption-legend-wins-over-e",
        append=b"BT /F1 6 Tf 1 0 0 1 406 490 Tm ((E) EXISTING UNIT TO REMAIN) Tj ET\n",
    )
    lane = _lane_at(_model(marked_near_callout, ASSUMPTION), QUAD_POSITION)
    assert (lane["scope_status"], lane["scope_method"]) == ("new", "sheet status legend")
    assert not _assumption_keys(lane)
    # ... and tied markers are a conflict that stays unresolved.
    tied = _variant(
        tmp_path,
        "assumption-conflict",
        append=b"BT /F2 6 Tf 1 0 0 1 396 496 Tm (E) Tj ET\n",
    )
    tied_lane = _lane_at(_model(tied, ASSUMPTION), QUAD_POSITION)
    assert tied_lane["scope_status"] == "unresolved"
    assert tied_lane["scope_reason"] == "scope_marker_ambiguous"
    assert not _assumption_keys(tied_lane)


def test_user_scope_assumption_keeps_a_note_naming_both_scopes_unresolved(
    tmp_path: Path,
) -> None:
    path = _variant(
        tmp_path,
        "assumption-note-new-or-existing",
        UNMARK_QUAD,
        *_notes(
            _runs(494, (596, "4."), (604, "ALL OUTLETS SHOWN ON THIS SHEET ARE NEW / EXISTING U.O.N.")),
            _runs(486, (596, "5. SYNTHETIC.")),
        ),
    )
    model = _model(path, ASSUMPTION)
    lane = _lane_at(model, QUAD_POSITION)
    assert lane["scope_status"] == "unresolved"
    assert lane["scope_reason"] == "scope_default_note_ambiguous"
    assert not _assumption_keys(lane)
    assert not _user_records(_device_at(model, QUAD_POSITION))


def test_user_scope_assumption_never_reads_a_letter_the_legend_does_not_define(
    tmp_path: Path,
) -> None:
    # Remove the legend's E row letter: every E-marked device now carries a
    # marker the sheet does not define. That is the sheet's own evidence of a
    # status the importer cannot read, so the rule must not call it new.
    path = _variant(
        tmp_path,
        "assumption-undefined-marker",
        (LEGEND_E_LETTER, LEGEND_E_LETTER.replace(b"(E)", b"( )")),
    )
    off = _model(path)
    on = _model(path, ASSUMPTION)
    undefined = [
        device.attributes["pdf_electrical"]
        for device in on.electrical_devices
        if device.attributes["pdf_electrical"].get("scope_reason") == "scope_marker_undefined"
    ]
    assert undefined
    assert all(lane["scope_marker"] == "E" for lane in undefined)
    assert all(not _assumption_keys(lane) for lane in undefined)
    assert device_scope_counts(on) == device_scope_counts(off)
