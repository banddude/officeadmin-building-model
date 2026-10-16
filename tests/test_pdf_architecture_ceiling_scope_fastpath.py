"""Ceiling-scope fast paths preserve ordered evidence and canonical output."""
from dataclasses import replace

import pytest
import oabm.importers.pdf_architecture.importer as engine
from oabm.importers.pdf_architecture.types import PdfPageObservation, PdfTextObservation
from test_pdf_architecture_importer import _plan_page, _document, _text

def _reference_scopes(
    page: engine.PdfPageObservation,
) -> tuple[tuple[engine.PdfTextObservation, float, str, str | None], ...]:
    rooms = engine._room_labels(page)
    boxes = engine._room_scope_boxes(page, rooms)
    result: list[tuple[engine.PdfTextObservation, float, str, str | None]] = []
    for observation, height_m in engine._ceiling_height_notes(page):
        upper = engine._clean_text(observation.text).upper()
        if engine._is_explicit_global_ceiling_height(observation.text):
            result.append((observation, height_m, "global", None))
            continue

        named = [room.anchor for room in rooms if room.name.upper() in upper]
        if len(named) == 1:
            result.append((observation, height_m, "room", named[0]))
            continue
        if len(named) > 1:
            result.append((observation, height_m, "unresolved", None))
            continue

        contained = [
            anchor
            for anchor, bbox in boxes.items()
            if engine._inside(bbox, observation.center_pt)
        ]
        if len(contained) == 1:
            result.append((observation, height_m, "room", contained[0]))
        elif len(contained) > 1:
            result.append((observation, height_m, "unresolved", None))
        elif len(rooms) == 1 and rooms[0].anchor in boxes:
            # With exactly one resolved room, an otherwise-unqualified height
            # note can be scoped to that room. It is never promoted to Level.
            result.append((observation, height_m, "room", rooms[0].anchor))
        else:
            result.append((observation, height_m, "unresolved", None))
    return tuple(result)


def _page(kind):
    page = _plan_page(include_height=False)
    values = {
        "none": [],
        "global": [("g1", "LEVEL CEILING HEIGHT: 9'-0\"", 10, 115)],
        "globals": [("g1", "LEVEL CEILING HEIGHT: 9'-0\"", 10, 115),
                    ("g2", "LEVEL CEILING HEIGHT: 10'-0\"", 10, 104)],
        "room": [("r1", "CEILING HEIGHT: 9'-0\"", 70, 90)],
        "mixed": [("g1", "LEVEL CEILING HEIGHT: 10'-0\"", 10, 115),
                  ("r1", "CEILING HEIGHT: 9'-0\"", 70, 90)],
        "unresolved": [("u1", "CEILING HEIGHT: 9'-0\"", 270, 210)],
    }[kind]
    if kind == "unresolved":
        page = replace(page, texts=tuple(t for t in page.texts if t.element_id != "p1:room"))
    return replace(page, texts=(*page.texts, *(_text(i,t,x,y) for i,t,x,y in values)))


@pytest.mark.parametrize("kind", ["none", "global", "globals", "room", "mixed", "unresolved"])
def test_ordered_scope_and_full_model_bytes_match_reference(kind, monkeypatch):
    page = _page(kind)
    expected = _reference_scopes(page)
    assert engine._ceiling_height_scopes(page) == expected
    if kind in {"global", "globals"}:
        assert [(row[0].element_id,row[2],row[3]) for row in expected] == [
            ("g1", "global", None), *([("g2", "global", None)] if kind == "globals" else [])]
    if kind == "none":
        assert expected == ()
    if kind == "room":
        assert [(row[0].element_id,row[2],row[3]) for row in expected] == [("r1","room","office")]
    optimized = engine.import_observations(_document(page)).to_json()
    monkeypatch.setattr(engine, "_ceiling_height_scopes", _reference_scopes)
    assert optimized == engine.import_observations(_document(page)).to_json()


@pytest.mark.parametrize("kind", ["none", "global", "globals"])
def test_fast_path_never_scans_room_geometry(kind, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("fast path must not scan rooms or enclosure geometry")
    monkeypatch.setattr(engine, "_room_labels", forbidden)
    monkeypatch.setattr(engine, "_room_scope_boxes", forbidden)
    rows = engine._ceiling_height_scopes(_page(kind))
    assert len(rows) == {"none": 0, "global": 1, "globals": 2}[kind]


def test_mixed_notes_retain_room_scans_and_source_order(monkeypatch):
    page = _page("mixed")
    calls = []
    original = engine._room_labels
    def observed(p):
        calls.append(p.page_number)
        return original(p)
    monkeypatch.setattr(engine, "_room_labels", observed)
    rows = engine._ceiling_height_scopes(page)
    assert calls == [1]
    assert [(r[0].element_id,r[2],r[3]) for r in rows] == [
        ("g1","global",None),("r1","room","office")]
