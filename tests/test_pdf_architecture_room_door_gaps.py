"""Layered room flood: door gaps close only with door evidence.

Synthetic generated source PDFs only: every case draws its own layers, wall
lines, door leaves, and opening rectangles, then runs the real ``extract_pdf``
and importer.
"""

from __future__ import annotations

import math
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_architecture.types import ImportOptions, ScaleOverride

_QUARTER_INCH_SCALE_M_PER_POINT = 0.016933333333


def _arc_commands(
    center: tuple[float, float],
    radius: float,
    start_degrees: float,
    stop_degrees: float,
) -> list[str]:
    """A door swing arc as short door-layer chords, ending short of the jamb."""

    points = []
    for step in range(5):
        angle = math.radians(
            start_degrees
            + (stop_degrees - start_degrees) * step / 4
        )
        points.append((
            center[0] + radius * math.cos(angle),
            center[1] + radius * math.sin(angle),
        ))
    commands = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        commands.append(f"{ax:.2f} {ay:.2f} m {bx:.2f} {by:.2f} l S")
    return commands


def _write_room_source(
    path: Path,
    *,
    door: str = "leaf",
    garage: bool = False,
    wall_rect_bottom: bool = False,
    two_labels: bool = False,
) -> None:
    """One synthetic room with a door opening in its bottom wall.

    ``door`` selects the door-layer graphics: ``leaf`` anchors a door leaf at
    one jamb with a swing arc that stops short of the other jamb, and
    ``misanchored`` draws leaf and arc shifted away from both jambs. ``garage``
    replaces the door gap with a wider one closed by an opening-layer
    rectangle. ``wall_rect_bottom`` draws the bottom wall as two thin
    wall-layer rectangles instead of lines.
    """

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    wall_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-WALL"),
    })
    door_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-DR.WND"),
    })
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    wall_ref = writer._add_object(wall_group)
    door_ref = writer._add_object(door_group)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({
            NameObject("/WALL"): wall_ref,
            NameObject("/DOOR"): door_ref,
        }),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([wall_ref, door_ref]),
        NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
    })
    gap_low, gap_high = (200, 300) if garage else (200, 252)
    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (X51 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 720 Tm (SCALE: 1/4\" = 1'-0\") Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 700 Tm (LEVEL: GROUND) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 160 330 Tm (ROOM: DEN) Tj ET",
    ]
    if two_labels:
        commands.append("BT /F1 10 Tf 1 0 0 1 280 330 Tm (ROOM: NOOK) Tj ET")
    commands.append("/OC /WALL BDC")
    if wall_rect_bottom:
        commands.extend((
            f"100 217 {gap_low - 100} 6 re S",
            f"{gap_high} 217 {360 - gap_high} 6 re S",
        ))
    else:
        commands.extend((
            f"100 220 m {gap_low} 220 l S",
            f"{gap_high} 220 m 360 220 l S",
        ))
    commands.extend((
        "100 430 m 360 430 l S",
        "100 220 m 100 430 l S",
        "360 220 m 360 430 l S",
        "EMC",
    ))
    if garage:
        commands.extend((
            "/OC /DOOR BDC",
            f"{gap_low} 220 {gap_high - gap_low} 7 re S",
            f"{(gap_low + gap_high) / 2:.0f} 220 m {(gap_low + gap_high) / 2:.0f} 227 l S",
            "EMC",
        ))
    elif door == "leaf":
        # Leaf anchored at the left jamb; the swing arc stops short of the
        # right jamb, so only the leaf anchors the opening.
        commands.append("/OC /DOOR BDC")
        commands.append(f"{gap_low} 220 m {gap_low} 272 l S")
        commands.extend(_arc_commands((gap_low, 220), 52, 90.0, 20.0))
        commands.append("EMC")
    elif door == "misanchored":
        # Door-scale graphics, but anchored away from both jambs.
        commands.append("/OC /DOOR BDC")
        commands.append(f"{gap_low - 10} 230 m {gap_low - 10} 282 l S")
        commands.extend(_arc_commands((gap_low - 10, 230), 52, 90.0, 20.0))
        commands.append("EMC")
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _import_options() -> ImportOptions:
    return ImportOptions(
        scale_overrides=(
            ScaleOverride(1, _QUARTER_INCH_SCALE_M_PER_POINT),
        ),
        default_wall_height_m=3.0,
    )


def test_room_with_door_leaf_and_arc_is_recovered(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-door-leaf.pdf"
    _write_room_source(source)
    assert not source.with_suffix(".expected.json").exists()

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-door-leaf"),
        options=_import_options(),
    )
    assert len(model.spaces) == 1
    space = model.spaces[0]
    assert space.name == "DEN"
    attributes = space.attributes["pdf_architecture"]
    assert attributes["recognition"] == "layered_wall_opening_region"
    # The door-leaf closure, not an invented barrier, closed the opening.
    assert space.provenance[0].attributes["opening_closure_count"] >= 1
    assert space.provenance[0].attributes["source_opening_elements"]


def test_room_with_unqualified_door_graphics_still_leaks(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-door-misanchored.pdf"
    _write_room_source(source, door="misanchored")

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-door-misanchored"),
        options=_import_options(),
    )
    assert model.spaces == ()


def test_room_without_any_door_graphics_still_leaks(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-door-none.pdf"
    _write_room_source(source, door="none")

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-door-none"),
        options=_import_options(),
    )
    assert model.spaces == ()


def test_garage_door_rectangle_closes_its_opening(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-garage-rect.pdf"
    _write_room_source(source, garage=True)

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-garage-rect"),
        options=_import_options(),
    )
    assert len(model.spaces) == 1
    assert model.spaces[0].name == "DEN"
    assert model.spaces[0].attributes["pdf_architecture"]["recognition"] == (
        "layered_wall_opening_region"
    )


def test_two_label_open_plan_stays_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-two-labels.pdf"
    _write_room_source(source, two_labels=True)

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-two-labels"),
        options=_import_options(),
    )
    assert model.spaces == ()


def test_wall_layer_rectangles_seal_the_room_mask(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-wall-rect.pdf"
    _write_room_source(source, wall_rect_bottom=True)

    model = import_observations(
        extract_pdf(source, source_id="fixture:room-wall-rect"),
        options=_import_options(),
    )
    assert len(model.spaces) == 1
    assert model.spaces[0].name == "DEN"


def test_room_door_gap_import_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-room-door-determinism.pdf"
    _write_room_source(source)

    first = import_observations(
        extract_pdf(source, source_id="fixture:room-door-determinism"),
        options=_import_options(),
    )
    second = import_observations(
        extract_pdf(source, source_id="fixture:room-door-determinism"),
        options=_import_options(),
    )
    assert first.to_json() == second.to_json()
