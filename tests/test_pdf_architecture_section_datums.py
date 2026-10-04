from dataclasses import replace
import pytest
from oabm.importers.pdf_architecture.importer import _find_dimension
from oabm.importers.pdf_architecture.section_datums import section_floor_datums
from oabm.importers.pdf_architecture.types import PdfPageObservation, PdfTextObservation


def text(key, value, x, y, width=80):
    return PdfTextObservation(key, value, (x, y, x + width, y + 10), font_size_pt=10)


def section(value="3.0 M", *, name="MEZZANINE", title="BUILDING SECTIONS"):
    return PdfPageObservation(
        3,
        800,
        600,
        texts=(
            text("role", title, 400, 20, 140),
            text("upper-ff", "FINISHED FLOOR (E)", 500, 300),
            text("upper-name", name, 500, 288),
            text("upper-value", value, 500, 276),
            text("zero-ff", "FINISHED FLOOR", 500, 130),
            text("zero-value", "0'-0\"", 500, 118),
        ),
    )


def read(page):
    return section_floor_datums(page, _find_dimension)


@pytest.mark.parametrize(
    ("value", "expected"), [("3.0 M", 3.0), ("3050 MM", 3.05), ("10'-2 1/2\"", 3.1115)]
)
def test_split_named_floor_and_zero_datum(value, expected):
    rows, refused = read(section(value))
    assert not refused and len(rows) == 1
    assert rows[0].level_name == "Mezzanine"
    assert rows[0].elevation_m == pytest.approx(expected)
    assert {t.element_id for t in rows[0].sources} == {
        "role",
        "upper-ff",
        "upper-name",
        "upper-value",
        "zero-ff",
        "zero-value",
    }


def test_verify_in_field_is_not_promoted_to_high_confidence():
    rows, _ = read(section("3.0 M VIF"))
    assert rows[0].confidence == 0.75


@pytest.mark.parametrize(
    "value", ["9'-0\" AFF", "SEE DETAIL 3", "10 M OR 11 M", "3'-1/0\"", "NAN M"]
)
def test_refuses_relative_notes_and_malformed_dimensions(value):
    assert not read(section(value))[0]


def test_ceiling_and_parapet_are_not_floor_elevations():
    page = section()
    for value in ("FINISHED CEILING", "T.O. PARAPET", "U/S SOFFIT"):
        candidate = replace(
            page,
            texts=tuple(
                replace(t, text=value) if t.element_id == "upper-ff" else t
                for t in page.texts
            ),
        )
        assert not read(candidate)[0]


def test_room_elevations_cannot_establish_building_levels():
    assert not read(section(title="INTERIOR ELEVATIONS"))[0]


def test_remote_column_or_missing_zero_datum_does_not_anchor_a_level():
    page = section()
    for offset in (100, 300):
        candidate = replace(
            page,
            texts=tuple(
                (
                    replace(
                        t,
                        bbox_pt=(
                            t.bbox_pt[0] + offset,
                            t.bbox_pt[1],
                            t.bbox_pt[2] + offset,
                            t.bbox_pt[3],
                        ),
                    )
                    if t.element_id.startswith("zero")
                    else t
                )
                for t in page.texts
            ),
        )
        rows, refused = read(candidate)
        assert not rows and refused[0]["code"] == "section_datum_reference_unresolved"


def test_observation_order_does_not_change_evidence():
    page = section()
    assert read(page) == read(replace(page, texts=tuple(reversed(page.texts))))


def test_signed_lower_level_uses_a_zero_above_it():
    page = section("-2.8 M", name="LEVEL: BASEMENT")
    swapped = replace(
        page,
        texts=tuple(
            (
                replace(
                    t,
                    bbox_pt=(
                        t.bbox_pt[0],
                        t.bbox_pt[1] - 200,
                        t.bbox_pt[2],
                        t.bbox_pt[3] - 200,
                    ),
                )
                if t.element_id.startswith("upper")
                else (
                    replace(
                        t,
                        bbox_pt=(
                            t.bbox_pt[0],
                            t.bbox_pt[1] + 100,
                            t.bbox_pt[2],
                            t.bbox_pt[3] + 100,
                        ),
                    )
                    if t.element_id.startswith("zero")
                    else t
                )
            )
            for t in page.texts
        ),
    )
    rows, refused = read(swapped)
    assert not refused and rows[0].level_name == "Basement"
    assert rows[0].elevation_m == pytest.approx(-2.8)


@pytest.mark.parametrize("value", ["--10'-0\"", "+-3'-0\""])
def test_multiple_signs_are_not_a_valid_elevation(value):
    assert not read(section(value))[0]


def plan(number, name, *, elevation=None, include_height=True):
    from oabm.importers.pdf_architecture.types import PdfRectObservation

    texts = [
        text(f"p{number}:title", "A-100 FLOOR PLAN", 10, 180),
        text(f"p{number}:scale", "SCALE: 1:100", 10, 165),
        text(f"p{number}:name", f"LEVEL: {name}", 10, 140),
        text(f"p{number}:room", "ROOM: STUDIO", 70, 70),
    ]
    if include_height:
        texts.append(
            text(f"p{number}:height", "TYPICAL CEILING HEIGHT: 8'-0\"", 10, 116)
        )
    if elevation is not None:
        texts.append(text(f"p{number}:elev", f"ELEVATION: {elevation} M", 10, 128))
    return PdfPageObservation(
        number,
        300,
        220,
        texts=tuple(texts),
        rects=(
            PdfRectObservation(f"p{number}:outer", (20, 20, 220, 120)),
            PdfRectObservation(f"p{number}:inner", (24, 24, 216, 116)),
        ),
    )


def imported(*pages, registered=False):
    from oabm.importers.pdf_architecture.importer import import_observations
    from oabm.importers.pdf_architecture.types import PdfDocumentObservation
    from oabm.importers.pdf_architecture import ImportOptions, RegistrationHint

    hints = (
        tuple(
            RegistrationHint(
                p.page_number, (0, 0), (100, 0), (0, 0), (100 * 100 * 0.0254 / 72, 0)
            )
            for p in pages
            if any("FLOOR PLAN" in t.text for t in p.texts)
        )
        if registered
        else ()
    )
    return import_observations(
        PdfDocumentObservation("synthetic:section-datums", "a" * 64, tuple(pages)),
        options=ImportOptions(registrations=hints),
    )


def test_later_section_resolves_plan_level_without_becoming_plan_geometry():
    from oabm.model import validate_model

    pages = (plan(1, "GROUND", elevation=0), plan(2, "MEZZANINE"), section())
    model = imported(*pages, registered=True)
    validate_model(model)
    levels = {level.name: level for level in model.levels}
    assert levels["Mezzanine"].elevation_m == 3
    assert levels["Ground"].elevation_m == 0
    # No section height is reinterpreted as a wall height.
    assert levels["Mezzanine"].height_m != 3
    provenance = levels["Mezzanine"].provenance
    assert any(
        p.page == 3
        and p.source_element_id == "upper-value"
        and p.scope_paths == ("elevation_m",)
        for p in provenance
    )
    assert any(p.source_element_id == "zero-value" for p in provenance)
    lane = model.attributes["pdf_architecture"]
    assert (
        next(p for p in lane["pages"] if p["page"] == 3)["status"]
        == "ignored_non_architectural_plan"
    )
    assert model.to_json() == imported(*reversed(pages), registered=True).to_json()
    assert any(
        wall.level_id == levels["Mezzanine"].id and wall.centerline.points[0].z == 3
        for wall in model.walls
    )


def test_conflicting_sections_never_choose_by_page_order():
    second = replace(
        section("3.2 M"),
        page_number=4,
        texts=tuple(
            replace(t, element_id="second:" + t.element_id)
            for t in section("3.2 M").texts
        ),
    )
    model = imported(
        plan(1, "GROUND", elevation=0), plan(2, "MEZZANINE"), section(), second
    )
    assert "Mezzanine" not in {level.name for level in model.levels}
    assert any(
        item["code"] == "section_level_elevation_conflict"
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    )


def test_agreeing_sections_keep_all_source_pages_and_vif_confidence():
    second = replace(
        section("3.0 M VIF"),
        page_number=4,
        texts=tuple(
            replace(t, element_id="second:" + t.element_id)
            for t in section("3.0 M VIF").texts
        ),
    )
    model = imported(
        plan(1, "GROUND", elevation=0), plan(2, "MEZZANINE"), second, section()
    )
    level = next(level for level in model.levels if level.name == "Mezzanine")
    assert level.elevation_m == 3
    assert {p.page for p in level.provenance if p.scope_paths == ("elevation_m",)} == {
        3,
        4,
    }
    assert all(
        p.confidence == 0.75
        for p in level.provenance
        if p.scope_paths == ("elevation_m",)
    )


def test_missing_datum_blocks_an_assumed_zero_but_not_explicit_plan_evidence():
    page = section()
    page = replace(
        page, texts=tuple(t for t in page.texts if not t.element_id.startswith("zero"))
    )
    unresolved = imported(plan(1, "MEZZANINE"), page)
    assert not unresolved.levels
    resolved = imported(plan(1, "MEZZANINE", elevation=3), page)
    assert resolved.levels[0].elevation_m == 3


def test_plan_and_section_disagreement_refuses_the_plan():
    model = imported(
        plan(1, "GROUND", elevation=0), plan(2, "MEZZANINE", elevation=4), section()
    )
    assert "Mezzanine" not in {level.name for level in model.levels}


def test_a_printed_floor_elevation_does_not_supply_missing_wall_height():
    model = imported(
        plan(1, "GROUND", elevation=0, include_height=False),
        plan(2, "MEZZANINE", include_height=False),
        section(),
    )
    level = next(level for level in model.levels if level.name == "Mezzanine")
    assert level.elevation_m == 3 and level.height_m is None
    assert not model.walls


def test_floor_datums_do_not_bypass_horizontal_registration():
    model = imported(plan(1, "GROUND", elevation=0), plan(2, "MEZZANINE"), section())
    assert (
        next(level for level in model.levels if level.name == "Mezzanine").elevation_m
        == 3
    )
    assert any(
        item["code"] == "registration_unresolved" and item["page"] == 2
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    )


def test_a_known_floor_with_unreadable_elevation_cannot_fall_back_to_zero():
    page = section("NOT READABLE")
    rows, refused = read(page)
    assert not rows
    assert refused and refused[0]["level_name"] == "Mezzanine"
    model = imported(plan(1, "MEZZANINE"), page)
    assert not model.levels


def test_a_split_verify_in_field_qualifier_is_preserved():
    page = section()
    page = replace(
        page, texts=page.texts + (text("qualifier", "VERIFY IN FIELD", 500, 264),)
    )
    rows, refused = read(page)
    assert not refused
    assert rows[0].confidence == 0.75
    assert "qualifier" in {t.element_id for t in rows[0].sources}
