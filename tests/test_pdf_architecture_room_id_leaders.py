"""Source-bound layered room tags and glazing boundaries from generated PDFs."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)
import pytest

from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import (
    _pair_adjacent_room_number_and_name_unambiguous,
    _room_id_leader_for_tag,
    _room_labels,
    import_observations,
)
from oabm.importers.pdf_architecture.layered_rooms import (
    LayeredRoomSeed,
    find_layered_room_regions,
)
from oabm.importers.pdf_architecture.types import ImportOptions, ScaleOverride
from oabm.model import validate_model


@dataclass(frozen=True, slots=True)
class _Tag:
    name: str
    number: str
    center_y: float
    leader_layer: str | None = "A-ANNO-RM-IDEN"
    extra_leader_offsets: tuple[float, ...] = ()
    extra_branches: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = ()
    text_x: float = 420.0
    draw_arrowhead: bool = True
    arrowhead_tip_offsets: tuple[tuple[float, float], tuple[float, float]] | None = None
    room_tip_marker_radius: float = 0.0
    extra_tip_marker_offsets: tuple[tuple[float, float], ...] = ()
    nested_tip_marker_radius: float = 0.0
    room_tip_marker_spokes: bool = False


@dataclass(frozen=True, slots=True)
class _Room:
    bottom: float
    top: float
    tags: tuple[_Tag, ...] = ()
    right_boundary: str = "wall"
    left: float = 100.0
    right: float = 300.0
    divider_y: float | None = None


_SCALE = 0.016933333333


def _write_source(
    path: Path,
    rooms: tuple[_Room, ...],
    *,
    inside_label: str | None = None,
    add_annotation_rectangles: bool = False,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    layer_names = (
        "A-WALL", "A-DOOR", "A-ANNO-RM-IDEN", "A-ANNO-TEXT",
        "AE-GLAZ", "AE-GLAZ-MULL", "A-GLAZ-CUSTOM",
    )
    references = {
        name: writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/OCG"),
            NameObject("/Name"): TextStringObject(name),
        }))
        for name in layer_names
    }
    property_names = {
        name: f"/L{index}" for index, name in enumerate(layer_names)
    }
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({
            NameObject(property_name): references[layer_name]
            for layer_name, property_name in property_names.items()
        }),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject(list(references.values())),
        NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
    })

    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (A210 FLOOR PLAN) Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 720 Tm (SCALE: 1/4\" = 1'-0\") Tj ET",
        "BT /F1 10 Tf 1 0 0 1 20 700 Tm (LEVEL: GROUND) Tj ET",
    ]

    def layer_begin(name: str) -> str:
        return f"/OC {property_names[name]} BDC"

    def add_line(layer: str, first: tuple[float, float], second: tuple[float, float]) -> None:
        commands.extend((
            layer_begin(layer),
            f"{first[0]} {first[1]} m {second[0]} {second[1]} l S",
            "EMC",
        ))

    for room in rooms:
        left, right = room.left, room.right
        if room.right_boundary in ("connected-glazing", "connected-glazing-extended"):
            right_wall_end = right - 3.0
        else:
            right_wall_end = right
        door_low, door_high = room.bottom + 60.0, room.bottom + 100.0
        commands.extend((
            layer_begin("A-WALL"),
            f"{left} {room.bottom} m {right_wall_end} {room.bottom} l S",
            f"{left} {room.top} m {right_wall_end} {room.top} l S",
            f"{left} {room.bottom} m {left} {door_low} l S",
            f"{left} {door_high} m {left} {room.top} l S",
        ))
        if room.right_boundary == "wall":
            commands.append(f"{right} {room.bottom} m {right} {room.top} l S")
        if room.divider_y is not None:
            commands.append(f"{left} {room.divider_y} m {right} {room.divider_y} l S")
        commands.append("EMC")

        # Existing source-supported door leaf: this is evidence for the wall
        # opening, not a synthetic closure segment.
        add_line("A-DOOR", (left, door_low), (left + 20, door_low))
        add_line("A-DOOR", (left + 20, door_low), (left + 20, door_high))
        add_line("A-DOOR", (left + 20, door_high), (left, door_high))

        if room.right_boundary in ("connected-glazing", "connected-glazing-extended"):
            glazing_x = right + 60.0 if room.right_boundary == "connected-glazing-extended" else right
            add_line("AE-GLAZ", (glazing_x, room.bottom), (glazing_x, room.top))
            add_line("AE-GLAZ-MULL", (right_wall_end, room.bottom), (glazing_x, room.bottom))
            add_line("AE-GLAZ-MULL", (right_wall_end, room.top), (glazing_x, room.top))
        elif room.right_boundary == "disconnected-glazing":
            # The facade detail is intentionally separated from the room wall.
            add_line("AE-GLAZ", (right + 30, room.bottom), (right + 30, room.top))
        elif room.right_boundary == "unsupported-glazing":
            # A plausible but unsupported layer name cannot close the opening.
            add_line("A-GLAZ-CUSTOM", (right, room.bottom), (right, room.top))

        for tag in room.tags:
            commands.extend((
                f"BT /F1 10 Tf 1 0 0 1 {tag.text_x} {tag.center_y + 20} Tm ({tag.name}) Tj ET",
                f"BT /F1 10 Tf 1 0 0 1 {tag.text_x} {tag.center_y} Tm ({tag.number}) Tj ET",
            ))
            if tag.leader_layer is not None:
                add_line(tag.leader_layer, (290, tag.center_y), (tag.text_x - 3, tag.center_y))
                if tag.draw_arrowhead:
                    # Small source arrowhead strokes mark the room-side end.
                    tips = tag.arrowhead_tip_offsets or ((6, 3), (6, -3))
                    for dx, dy in tips:
                        add_line(
                            tag.leader_layer,
                            (290, tag.center_y),
                            (290 + dx, tag.center_y + dy),
                        )
                if tag.room_tip_marker_radius > 0:
                    centers = (
                        (290.0, tag.center_y - tag.room_tip_marker_radius),
                        *(
                            (290.0 + dx, tag.center_y - tag.room_tip_marker_radius + dy)
                            for dx, dy in tag.extra_tip_marker_offsets
                        ),
                    )
                    for center in centers:
                        radii = (tag.room_tip_marker_radius,)
                        if tag.nested_tip_marker_radius > 0:
                            radii = (*radii, tag.nested_tip_marker_radius)
                        for radius in radii:
                            points = tuple(
                                (
                                    center[0] + radius * math.cos(2 * math.pi * i / 24 + math.pi / 2),
                                    center[1] + radius * math.sin(2 * math.pi * i / 24 + math.pi / 2),
                                )
                                for i in range(24)
                            )
                            for first, second in zip(points, (*points[1:], points[0])):
                                add_line(tag.leader_layer, first, second)
                            if tag.room_tip_marker_spokes and radius == tag.room_tip_marker_radius:
                                for point in points:
                                    add_line(tag.leader_layer, point, center)
                        if tag.nested_tip_marker_radius > 0:
                            add_line(
                                tag.leader_layer,
                                (center[0] + tag.room_tip_marker_radius, center[1]),
                                (center[0] + tag.nested_tip_marker_radius, center[1]),
                            )
            for offset in tag.extra_leader_offsets:
                if tag.leader_layer is None:
                    continue
                add_line(
                    tag.leader_layer,
                    (280, tag.center_y + offset),
                    (tag.text_x - 3, tag.center_y),
                )
            if tag.leader_layer is not None:
                for first, second in tag.extra_branches:
                    add_line(tag.leader_layer, first, second)

    if inside_label is not None:
        commands.append(f"BT /F1 10 Tf 1 0 0 1 180 285 Tm (ROOM: {inside_label}) Tj ET")
    if add_annotation_rectangles:
        commands.extend((
            layer_begin("A-ANNO-TEXT"),
            "520 80 8 6 re S",
            "540 100 8 6 re S",
            "560 120 8 6 re S",
            "EMC",
        ))

    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _import(path: Path):
    observations = extract_pdf(path, source_id="fixture:generated-room-id-leaders")
    model = import_observations(
        observations,
        options=ImportOptions(scale_overrides=(ScaleOverride(1, _SCALE),)),
    )
    validate_model(model)
    return observations, model


def test_closed_wall_and_supported_door_remain_a_room(tmp_path: Path) -> None:
    source = tmp_path / "generated-closed-room.pdf"
    _write_source(
        source,
        (_Room(180, 350),),
        inside_label="LABORATORY",
    )

    observations, model = _import(source)

    assert any("A-WALL" in line.source_layers for line in observations.pages[0].lines)
    assert any("A-DOOR" in line.source_layers for line in observations.pages[0].lines)
    assert [space.name for space in model.spaces] == ["LABORATORY"]
    assert not model.spaces[0].provenance[0].attributes.get("source_glazing_elements")


def test_repeated_room_name_and_outside_number_leaders_seed_distinct_regions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-repeated-room-names.pdf"
    _write_source(source, (
        _Room(180, 350, (_Tag("NORTH SUITE", "301", 260),)),
        _Room(390, 560, (_Tag("NORTH SUITE", "302", 500),)),
    ))

    observations, model = _import(source)

    assert any(
        "A-ANNO-RM-IDEN" in layer
        for line in observations.pages[0].lines
        for layer in line.source_layers
    )
    assert {space.name for space in model.spaces} == {"NORTH SUITE 301", "NORTH SUITE 302"}
    assert len(model.spaces) == 2
    for space in model.spaces:
        evidence = space.provenance[0].attributes
        assert evidence["source_wall_elements"]
        assert evidence["source_opening_elements"]
        assert evidence["source_room_id_leader_elements"]
        assert evidence["spatial_interpretation"] == "inferred_from_observed_vectors"
        assert space.height_m is None
    diagnostics = model.attributes["pdf_architecture"]["ambiguities"]
    for space in model.spaces:
        diagnostic = next(
            item for item in diagnostics
            if item.get("code") == "layered_room_3d_extent_unresolved"
            and item.get("room_anchor") == space.attributes["pdf_architecture"]["identity_anchor"]
        )
        assert diagnostic["source_wall_elements"]
        assert diagnostic["source_opening_elements"]
        assert diagnostic["source_room_id_leader_elements"]
        assert diagnostic["drawing_region_id"]


def test_room_id_text_on_unrelated_annotation_layer_does_not_anchor_room(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-unrelated-annotation.pdf"
    _write_source(
        source,
        (_Room(180, 350, (_Tag("WORKROOM", "401", 260, leader_layer="A-ANNO-TEXT"),)),),
    )

    observations, model = _import(source)

    assert any("A-ANNO-TEXT" in line.source_layers for line in observations.pages[0].lines)
    assert model.spaces == ()
    assert any(
        item.get("code") == "room_label_candidates_rejected"
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    )


def test_branching_room_id_leader_stays_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "generated-branching-room-id-leader.pdf"
    _write_source(
        source,
        (_Room(180, 350, (
            _Tag("WORKROOM", "402", 260, extra_leader_offsets=(5.0,)),
        )),),
    )

    _, model = _import(source)

    assert model.spaces == ()


def test_midshaft_branch_to_another_bounded_region_invalidates_leader_component(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-midshaft-leader-branch.pdf"
    _write_source(source, (
        _Room(180, 350, (_Tag(
            "WORKROOM", "406", 260,
            extra_branches=(((350, 260), (350, 390)),),
        ),)),
        # This second loop is the bounded region reached by the long branch.
        _Room(370, 540, left=320, right=460),
    ))

    observations, model = _import(source)
    labels, ambiguous = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    assert not ambiguous
    assert len(labels) == 1
    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert len(leader[1]) >= 4  # shaft, two arrowhead marks, and branch
    assert model.spaces == ()
    assert any(
        item.get("code") == "room_id_leader_ambiguous"
        for item in model.attributes["pdf_architecture"]["ambiguities"]
    )


def test_back_facing_arrowhead_marks_do_not_anchor_a_room_tag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-back-facing-room-id-arrowhead.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "BACK-FACING SUITE", "405", 260,
            arrowhead_tip_offsets=((-6, 3), (-6, -3)),
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_near_transverse_crossbar_marks_do_not_count_as_arrowhead(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-crossbar-room-id-marks.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "CROSSBAR SUITE", "412", 260,
            arrowhead_tip_offsets=((0.01, 3), (0.01, -3)),
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_forward_arrowhead_geometry_remains_supported(tmp_path: Path) -> None:
    source = tmp_path / "generated-forward-room-id-arrowhead.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("FORWARD SUITE", "413", 260),),
    ),))

    observations, model = _import(source)
    labels, ambiguous = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    assert not ambiguous
    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] == pytest.approx((290.0, 260.0))
    assert [space.name for space in model.spaces] == ["FORWARD SUITE 413"]


def test_compact_room_tip_marker_is_part_of_one_unique_leader_endpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-compact-room-tip-marker.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "MARKED SUITE", "407", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
        ),),
    ),))

    observations, model = _import(source)
    labels, ambiguous = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    assert not ambiguous
    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] == pytest.approx((290.0, 255.5))
    assert len(leader[1]) >= 25  # shaft and compact source marker
    assert [space.name for space in model.spaces] == ["MARKED SUITE 407"]
    label_source_ids = sorted(item.element_id for item in labels[0].source_observations)
    assert model.spaces[0].provenance[0].attributes["source_room_label_elements"] == label_source_ids
    assert model.spaces[0].attributes["pdf_architecture"]["label_source_element_ids"] == label_source_ids


def test_compact_marker_contour_seed_wins_when_wall_separates_stem_endpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-marker-target-across-divider.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "MARKED SUITE", "417", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
        ),),
        divider_y=258,
    ),))
    observations = extract_pdf(source, source_id="fixture:marker-target-across-divider")
    page = observations.pages[0]
    labels, ambiguous = _pair_adjacent_room_number_and_name_unambiguous(
        page, _room_labels(page),
    )

    assert not ambiguous
    leader = _room_id_leader_for_tag(page, labels[0])
    assert leader is not None and leader[0] == pytest.approx((290.0, 255.5))
    marker_region = find_layered_room_regions(
        page,
        _SCALE,
        (LayeredRoomSeed("417", leader[0], leader[1]),),
    )
    endpoint_region = find_layered_room_regions(
        page,
        _SCALE,
        (LayeredRoomSeed("417", (290.0, 260.0), leader[1]),),
    )

    assert len(marker_region) == 1
    assert len(endpoint_region) == 1
    assert max(y for _, y in marker_region[0].polygon_pt) <= 259
    assert min(y for _, y in endpoint_region[0].polygon_pt) >= 257
    assert marker_region[0].polygon_pt != endpoint_region[0].polygon_pt


def test_room_marker_and_facade_import_is_stable_under_observation_permutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-permuted-marker-facade.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "PERMUTED STUDIO", "418", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
            room_tip_marker_spokes=True,
        ),),
        right_boundary="connected-glazing",
    ),), add_annotation_rectangles=True)
    observations = extract_pdf(source, source_id="fixture:permuted-marker-facade")
    page = observations.pages[0]
    assert len(page.rects) >= 3

    reordered_page = replace(
        page,
        texts=tuple(reversed(page.texts)),
        lines=tuple(reversed(page.lines)),
        rects=tuple(reversed(page.rects)),
        curves=tuple(reversed(page.curves)),
    )
    options = ImportOptions(scale_overrides=(ScaleOverride(1, _SCALE),))
    original = import_observations(observations, options=options)
    permuted = import_observations(
        replace(observations, pages=(reordered_page,)),
        options=options,
    )

    assert len(original.spaces) == 1
    assert original.spaces[0].name == "PERMUTED STUDIO 418"
    evidence = original.spaces[0].provenance[0].attributes
    assert evidence["source_room_id_leader_elements"]
    assert evidence["source_glazing_elements"]
    assert evidence["source_mullion_elements"]
    assert original.to_json().encode("utf-8") == permuted.to_json().encode("utf-8")


def test_midshaft_crossing_stays_ambiguous_even_with_compact_tip_marker(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-midshaft-crossing-room-id-leader.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "CROSSING SUITE", "408", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
            extra_branches=(((350, 240), (350, 280)),),
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_off_tip_continuation_inside_marker_radius_is_not_a_closed_glyph(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-off-tip-marker-continuation.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "CONTINUATION SUITE", "409", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
            extra_branches=(((290, 251), (294, 249)),),
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_two_compact_endpoint_markers_on_one_stem_stay_ambiguous(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-two-tip-markers.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "TWO MARKER SUITE", "410", 260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
            extra_tip_marker_offsets=((5.0, 0.0),),
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_nested_competing_closed_marker_contour_stays_ambiguous(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-nested-competing-tip-markers.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag(
            "TWO MARKERS SUITE", "411",
            260,
            draw_arrowhead=False,
            room_tip_marker_radius=4.5,
            nested_tip_marker_radius=2.0,
        ),),
    ),))

    observations, model = _import(source)
    labels, _ = _pair_adjacent_room_number_and_name_unambiguous(
        observations.pages[0], _room_labels(observations.pages[0]),
    )

    leader = _room_id_leader_for_tag(observations.pages[0], labels[0])
    assert leader is not None and leader[0] is None
    assert model.spaces == ()


def test_two_distinct_room_tags_claiming_one_component_stay_unresolved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-two-room-tags-one-component.pdf"
    _write_source(source, (_Room(180, 350, (
        _Tag("EDIT BAY", "403", 220),
        _Tag("SUPPLY BAY", "404", 315),
    )),))

    _, model = _import(source)

    assert model.spaces == ()


def test_source_connected_glazing_and_mullion_vectors_can_bound_a_leader_seeded_room(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-connected-glazing-room.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("GLASS STUDIO", "501", 260),),
        right_boundary="connected-glazing",
    ),))

    observations, model = _import(source)

    assert len(model.spaces) == 1
    space = model.spaces[0]
    assert space.name == "GLASS STUDIO 501"
    assert any("AE-GLAZ" in line.source_layers for line in observations.pages[0].lines)
    evidence = space.provenance[0].attributes
    assert evidence["source_glazing_elements"]
    assert evidence["source_mullion_elements"]
    assert evidence["source_room_id_leader_elements"]
    assert "inferred" in space.provenance[0].derivation
    assert "facade vectors" in space.provenance[0].method


def test_text_only_seed_cannot_borrow_facade_closure_from_other_leader_tag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-mixed-leader-and-text-only-rooms.pdf"
    _write_source(source, (
        _Room(
            180, 350,
            (_Tag("GLASS STUDIO", "506", 260),),
            right_boundary="connected-glazing",
        ),
        _Room(
            390, 560,
            (_Tag("INTERIOR LAB", "607", 480, leader_layer=None, text_x=180),),
            right_boundary="connected-glazing",
        ),
    ))

    _, model = _import(source)

    assert [space.name for space in model.spaces] == ["GLASS STUDIO 506"]


def test_connected_facade_extending_beyond_wall_mask_pad_is_not_clipped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-extended-connected-facade.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("GLASS STUDIO", "508", 260),),
        right_boundary="connected-glazing-extended",
    ),))

    observations, model = _import(source)

    assert len(model.spaces) == 1
    points = model.spaces[0].footprint.points
    spans = (
        max(point.x for point in points) - min(point.x for point in points),
        max(point.y for point in points) - min(point.y for point in points),
    )
    assert max(spans) == pytest.approx((360.0 - 100.0) * _SCALE, abs=0.12)
    evidence = model.spaces[0].provenance[0].attributes
    assert evidence["source_glazing_elements"]
    assert evidence["source_mullion_elements"]
    assert any(
        "AE-GLAZ" in line.source_layers
        for line in observations.pages[0].lines
    )


def test_disconnected_glazing_detail_does_not_close_open_room_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-disconnected-glazing-room.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("GLASS STUDIO", "502", 260),),
        right_boundary="disconnected-glazing",
    ),))

    _, model = _import(source)

    assert model.spaces == ()


def test_unsupported_glazing_layer_does_not_close_open_room_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated-unsupported-glazing-room.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("GLASS STUDIO", "505", 260),),
        right_boundary="unsupported-glazing",
    ),))

    observations, model = _import(source)

    assert any("A-GLAZ-CUSTOM" in line.source_layers for line in observations.pages[0].lines)
    assert model.spaces == ()


def test_open_leader_seeded_wall_loop_stays_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "generated-open-room-boundary.pdf"
    _write_source(source, (_Room(
        180,
        350,
        (_Tag("OPEN STUDIO", "503", 260),),
        right_boundary="open",
    ),))

    _, model = _import(source)

    assert model.spaces == ()


def test_layered_region_api_keeps_leader_source_ids(tmp_path: Path) -> None:
    source = tmp_path / "generated-leader-seed-source-ids.pdf"
    _write_source(source, (_Room(180, 350, (_Tag("MATERIALS BAY", "504", 260),)),))
    observations = extract_pdf(source, source_id="fixture:leader-seed-ids")
    page = observations.pages[0]
    leader = next(
        line for line in page.lines
        if "A-ANNO-RM-IDEN" in line.source_layers
        and abs(line.end_pt[0] - line.start_pt[0]) > 100
    )
    result = find_layered_room_regions(
        page,
        _SCALE,
        (LayeredRoomSeed("504", (290, 260), (leader.element_id,)),),
    )

    assert len(result) == 1
    assert result[0].source_leader_ids == (leader.element_id,)
