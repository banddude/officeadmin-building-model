"""#105: scope of work from sheet legends, and counts compared with takeoffs.

Scope cases start from the public synthetic notes-column fixture and edit its
content stream (a legend row or one field marker), then run the edited PDF
through the real extractor and importer. Comparison cases use plain numbers.
No private plan or takeoff data is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.qa.takeoff_comparison import (
    compare_counts,
    compare_with_references,
    device_scope_counts,
    reference_disagreements,
)
from oabm.model import validate_model

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "pdf_electrical" / "geometry-only-power-sheet-notes-column-legend.pdf"
# One field marker beside a device glyph, and the legend's R row.
FIELD_MARKER = b"1 0 0 1 416 504 Tm (N) Tj"
LEGEND_R_ROW = b"1 0 0 1 608 115 Tm (EXISTING TO BE REMOVED) Tj"


def _variant(tmp_path: Path, name: str, *edits: tuple[bytes, bytes], append: bytes = b"") -> Path:
    reader = PdfReader(FIXTURE)
    writer = PdfWriter(clone_from=reader)
    page = writer.pages[0]
    data = page.get_contents().get_data()
    for old, new in edits:
        assert data.count(old) == 1, old
        data = data.replace(old, new)
    stream = DecodedStreamObject()
    stream.set_data(data + append)
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / f"{name}.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


def _model(path: Path):
    model = ElectricalPdfImporter().import_document(extract_pdf(path, source_id=f"fixture:{path.stem}"))
    validate_model(model)
    return model


def _scopes(model) -> dict[tuple[str | None, str, str | None], int]:
    result: dict[tuple[str | None, str, str | None], int] = {}
    for device in model.electrical_devices:
        lane = device.attributes["pdf_electrical"]
        key = (lane.get("scope_marker"), lane["scope_status"], lane.get("scope_reason"))
        result[key] = result.get(key, 0) + 1
    return result


def test_legend_defines_new_and_existing_scope() -> None:
    model = _model(FIXTURE)
    assert _scopes(model) == {("E", "existing_to_remain", None): 8, ("N", "new", None): 8}
    device = next(
        item for item in model.electrical_devices
        if item.attributes["pdf_electrical"]["scope_status"] == "new"
    )
    lane = device.attributes["pdf_electrical"]
    assert lane["scope_method"] == "sheet status legend"
    assert lane["scope_legend_text"] == "NEW"
    assert len(lane["scope_legend_source_element_ids"]) == 2
    assert lane["scope_marker_source_element_id"]
    counts = device_scope_counts(model)
    assert counts["totals"]["new"] == 8
    assert counts["totals"]["existing_to_remain"] == 8
    assert counts["in_scope_total"] == 8
    assert counts["source_pages"] == [1]


def test_relocation_legend_makes_an_r_marker_relocated_scope(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "relocated",
        (FIELD_MARKER, FIELD_MARKER.replace(b"(N)", b"(R)")),
        (LEGEND_R_ROW, LEGEND_R_ROW.replace(b"EXISTING TO BE REMOVED", b"EXISTING TO BE REMOVED AND SALVAGED FOR RELOCATION")),
    )
    model = _model(path)
    assert _scopes(model)[("R", "relocated", None)] == 1
    counts = device_scope_counts(model)
    assert counts["totals"]["relocated"] == 1
    assert counts["totals"]["new"] == 7
    assert counts["in_scope_total"] == 8


def test_removal_legend_makes_an_r_marker_removed_and_out_of_scope(tmp_path: Path) -> None:
    path = _variant(tmp_path, "removed", (FIELD_MARKER, FIELD_MARKER.replace(b"(N)", b"(R)")))
    counts = device_scope_counts(_model(path))
    assert counts["totals"]["removed"] == 1
    assert counts["in_scope_total"] == 7


def test_marker_the_legend_does_not_define_stays_unresolved(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "undefined",
        (FIELD_MARKER, FIELD_MARKER.replace(b"(N)", b"(R)")),
        (LEGEND_R_ROW, LEGEND_R_ROW.replace(b"EXISTING TO BE REMOVED", b"SEE GENERAL NOTES")),
    )
    model = _model(path)
    assert _scopes(model)[("R", "unresolved", "scope_marker_undefined")] == 1
    counts = device_scope_counts(model)
    assert counts["unresolved_reasons"] == {"scope_marker_undefined": 1}
    assert counts["in_scope_total"] == 7


def test_conflicting_legend_rows_leave_the_marker_unresolved(tmp_path: Path) -> None:
    extra_row = (
        b"BT /F1 5.5 Tf 1 0 0 1 596 106 Tm (N) Tj ET\n"
        b"BT /F1 5.5 Tf 1 0 0 1 608 106 Tm (EXISTING TO REMAIN) Tj ET\n"
    )
    model = _model(_variant(tmp_path, "conflict", append=extra_row))
    assert _scopes(model) == {
        ("E", "existing_to_remain", None): 8,
        ("N", "unresolved", "scope_legend_conflict"): 8,
    }
    assert device_scope_counts(model)["in_scope_total"] == 0


def test_unmarked_device_is_unresolved_not_assumed_new(tmp_path: Path) -> None:
    model = _model(_variant(tmp_path, "unmarked", (FIELD_MARKER, FIELD_MARKER.replace(b"(N)", b"( )"))))
    assert _scopes(model)[(None, "unresolved", "no_scope_marker")] == 1
    assert device_scope_counts(model)["in_scope_total"] == 7


def test_scope_counts_are_deterministic_and_serializable(tmp_path: Path) -> None:
    path = _variant(tmp_path, "repeat", (FIELD_MARKER, FIELD_MARKER.replace(b"(N)", b"(R)")))
    first = device_scope_counts(_model(path))
    second = device_scope_counts(_model(path))
    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True)) == first
    assert list(first["by_type"]) == sorted(first["by_type"])


def test_exact_per_type_match() -> None:
    result = compare_counts({"a": 2, "b": 3}, {"a": 2, "b": 3}, comparable_types=("a", "b"))
    assert result["exact_match"] == {"numerator": 2, "denominator": 2, "rate": 1.0}
    assert [row["status"] for row in result["rows"]] == ["exact", "exact"]


def test_missing_extra_and_zero_reference_types() -> None:
    result = compare_counts(
        {"a": 3, "c": 4, "z": 9},
        {"a": 2, "b": 5, "y": 1},
        comparable_types=("a", "b", "c"),
    )
    rows = {row["device_type"]: row for row in result["rows"]}
    assert rows["a"] == {"device_type": "a", "extracted": 3, "reference": 2, "delta": 1, "absolute_delta": 1, "status": "over"}
    assert rows["b"]["status"] == "under" and rows["b"]["extracted"] == 0
    assert rows["c"]["reference"] == 0 and rows["c"]["status"] == "over"
    assert result["exact_match"] == {"numerator": 0, "denominator": 3, "rate": 0.0}
    assert result["absolute_delta_total"] == 1 + 5 + 4
    assert result["not_scored"] == {"extracted_only_types": ["z"], "reference_only_types": ["y"]}


def test_no_comparable_types_reports_an_empty_denominator() -> None:
    result = compare_counts({"a": 1}, {"a": 1}, comparable_types=())
    assert result["exact_match"] == {"numerator": 0, "denominator": 0, "rate": None}


def test_independent_references_that_disagree_are_called_out() -> None:
    disagreements = reference_disagreements(
        {"first": {"a": 2, "b": 1}, "second": {"a": 2, "b": 4}, "third": {"a": 2}},
        comparable_types=("a", "b"),
    )
    assert disagreements == [{"device_type": "b", "counts": {"first": 1, "second": 4, "third": 0}}]


def test_model_comparison_scores_only_new_and_relocated_scope(tmp_path: Path) -> None:
    model = _model(FIXTURE)
    types = sorted(device_scope_counts(model)["in_scope_by_type"])
    references = {
        "reference-a": {kind: 1 for kind in types},
        "reference-b": {kind: 2 for kind in types},
    }
    result = compare_with_references(model, references, supported_types=types)
    assert result["comparisons"]["reference-a"]["exact_match"]["numerator"] == len(types)
    assert result["comparisons"]["reference-b"]["exact_match"] == {
        "numerator": 0, "denominator": len(types), "rate": 0.0,
    }
    assert len(result["reference_disagreements"]) == len(types)


def test_types_neither_side_counts_are_not_scored(tmp_path: Path) -> None:
    model = _model(FIXTURE)  # in scope: one new device of each of eight types
    in_scope = device_scope_counts(model)["in_scope_by_type"]
    first = sorted(in_scope)[0]
    supported = [*sorted(in_scope), "evse", "luminaire"]
    references = {
        "broad": {first: 1, "luminaire": 4},
        "narrow": {first: 1},
    }
    result = compare_with_references(model, references, supported_types=supported)
    # EVSE is supported but neither side counts it; luminaire only counts for "broad".
    assert result["comparable_types"]["broad"] == sorted({*in_scope, "luminaire"})
    assert result["comparable_types"]["narrow"] == sorted(in_scope)
    assert "evse" not in result["comparable_types"]["broad"]
    narrow = result["comparisons"]["narrow"]["exact_match"]
    assert narrow == {"numerator": 1, "denominator": len(in_scope), "rate": 1 / len(in_scope)}
    assert {"device_type": "luminaire", "counts": {"broad": 4, "narrow": 0}} in result["reference_disagreements"]
    # A supported type the reference does not count is excluded from scoring,
    # while an unsupported one is never scored at all.
    unsupported = compare_with_references(model, {"r": {"exit_sign": 3}}, supported_types=supported)
    assert "exit_sign" not in unsupported["comparable_types"]["r"]
