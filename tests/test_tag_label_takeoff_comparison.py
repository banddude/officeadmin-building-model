"""#105 QA lane: takeoff comparison by fixture-tag label.

Label tallies are built from synthetic text observations and compared with
synthetic reference dictionaries through the pure helpers. No PDF
extraction, no private plan or takeoff data.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oabm.importers.pdf_electrical.importer import PdfTextObservation
from oabm.qa.takeoff_comparison import (
    compare_tag_labels,
    count_label_occurrences,
    normalize_tag,
)


def _observation(text: str, x_pt: float, y_pt: float, page: int | None = 1) -> dict:
    return {"text": text, "x_pt": x_pt, "y_pt": y_pt, "page": page}


def test_exact_match_on_base_scores_every_tag() -> None:
    result = compare_tag_labels(
        {
            "sheet-a": {"LF-12": 6, "LF-3A": 2},
            "sheet-b": {"LF-12": 1},
        },
        {"LF-12": 7, "LF-3A": 2},
        base_sheets=["sheet-a", "sheet-b"],
        alternate_sheets=[],
    )
    assert result["rows"] == [
        {
            "tag": "LF-12",
            "reference": 7,
            "base_labels": 7,
            "alternate_labels": 0,
            "base_plus_alternate": 7,
            "delta": 0,
            "exact_on_base": True,
            "exact_with_alternates": True,
            "status": "match_on_base",
        },
        {
            "tag": "LF-3A",
            "reference": 2,
            "base_labels": 2,
            "alternate_labels": 0,
            "base_plus_alternate": 2,
            "delta": 0,
            "exact_on_base": True,
            "exact_with_alternates": True,
            "status": "match_on_base",
        },
    ]
    assert result["exact_on_base"] == {"numerator": 2, "denominator": 2, "rate": 1.0}
    assert result["exact_with_alternates"] == {"numerator": 2, "denominator": 2, "rate": 1.0}
    assert result["totals"] == {
        "reference": 9,
        "base": 9,
        "alternate": 0,
        "base_plus_alternate": 9,
    }


def test_alternate_only_tag_matches_when_alternates_are_allowed() -> None:
    result = compare_tag_labels(
        {"sheet-a": {"LF-8": 4}, "sheet-b": {"LF-5": 3}},
        {"LF-8": 4, "LF-5": 3},
        base_sheets=["sheet-a"],
        alternate_sheets=["sheet-b"],
    )
    rows = {row["tag"]: row for row in result["rows"]}
    assert rows["LF-5"]["base_labels"] == 0
    assert rows["LF-5"]["alternate_labels"] == 3
    assert rows["LF-5"]["exact_on_base"] is False
    assert rows["LF-5"]["exact_with_alternates"] is True
    assert rows["LF-5"]["status"] == "match_with_alternates"
    assert rows["LF-8"]["exact_on_base"] is True
    assert result["exact_on_base"]["numerator"] == 1
    assert result["exact_with_alternates"]["numerator"] == 2


def test_over_and_under_rows_carry_signed_deltas_and_totals() -> None:
    result = compare_tag_labels(
        {"sheet-a": {"LF-12": 5}, "sheet-b": {"LF-7": 1}},
        {"LF-12": 4, "LF-7": 6},
        base_sheets=["sheet-a"],
        alternate_sheets=["sheet-b"],
    )
    rows = {row["tag"]: row for row in result["rows"]}
    assert rows["LF-12"]["status"] == "over"
    assert rows["LF-12"]["delta"] == 1
    assert rows["LF-7"]["status"] == "under"
    assert rows["LF-7"]["delta"] == -5
    assert rows["LF-7"]["exact_on_base"] is False
    assert rows["LF-7"]["exact_with_alternates"] is False
    assert result["totals"] == {
        "reference": 10,
        "base": 5,
        "alternate": 1,
        "base_plus_alternate": 6,
    }


def test_prefix_case_and_hyphen_variants_normalize_to_one_tag() -> None:
    assert normalize_tag("lf-12") == "LF-12"
    assert normalize_tag("  LF-12  ") == "LF-12"
    assert normalize_tag("(N) LF-12") == "LF-12"
    assert normalize_tag("(N)LF-12") == "LF-12"
    assert normalize_tag("(N)-LF-12") == "LF-12"
    assert normalize_tag("N-LF-12") == "LF-12"
    assert normalize_tag("lf – 12") == "LF-12"
    assert normalize_tag("lf - 12a") == "LF-12A"
    # A bare new-work token is never stripped into inventing a tag.
    assert normalize_tag("N") == "N"
    assert normalize_tag("(N)") == "(N)"
    assert normalize_tag("NORTH-4") == "NORTH-4"

    result = compare_tag_labels(
        {"sheet-a": {"lf – 12": 2, "(N) LF-12": 1}, "sheet-b": {"LF-12": 2}},
        {"lf-12": 5},
        base_sheets=["sheet-a", "sheet-b"],
        alternate_sheets=[],
    )
    assert [row["tag"] for row in result["rows"]] == ["LF-12"]
    assert result["rows"][0]["base_labels"] == 5
    assert result["exact_on_base"]["numerator"] == 1


def test_legend_box_exclusions_keep_the_legend_tag_list_out_of_the_tally() -> None:
    observations = [
        _observation("LF-12", 100.0, 300.0),
        _observation("LF-12", 130.0, 300.0),
        _observation("LF-8", 160.0, 300.0),
        # A legend row listing every tag, inside the exclusion box.
        _observation("LF-12 LF-8 LF-5 LF-3A", 60.0, 40.0),
        # The box is for page 1 only, so this label is still counted.
        _observation("LF-12", 55.0, 640.0, page=2),
    ]
    counts = count_label_occurrences(observations, exclude_boxes={1: [(40.0, 20.0, 200.0, 60.0)]})
    # The legend row is dropped whole: LF-5 and LF-3A appear nowhere else.
    assert counts == {"LF-12": 3, "LF-8": 1}
    assert count_label_occurrences(observations) == {
        "LF-12": 4,
        "LF-8": 2,
        "LF-5": 1,
        "LF-3A": 1,
    }


def test_observations_without_a_page_are_never_excluded() -> None:
    counts = count_label_occurrences(
        [_observation("LF-9", 50.0, 30.0, page=None)],
        exclude_boxes={1: [(0.0, 0.0, 100.0, 100.0)]},
    )
    assert counts == {"LF-9": 1}


def test_extractor_observations_are_accepted_directly() -> None:
    text_observation = PdfTextObservation(
        element_id="obs-1", page=1, text="LF-12", x_pt=90.0, y_pt=200.0
    )
    namespace_observation = SimpleNamespace(text="LF-8", x_pt=10.0, y_pt=10.0, page=1)
    counts = count_label_occurrences(
        [text_observation, namespace_observation],
        exclude_boxes={1: [(0.0, 0.0, 5.0, 5.0)]},
    )
    assert counts == {"LF-12": 1, "LF-8": 1}


def test_custom_tag_pattern_is_configurable() -> None:
    counts = count_label_occurrences(
        [_observation("HID-2 HID-2 HID-7X", 10.0, 10.0)],
        tag_pattern=r"(?<![A-Za-z0-9])HID-\d+[A-Z]?(?![A-Za-z0-9])",
    )
    assert counts == {"HID-2": 2, "HID-7X": 1}


def test_default_pattern_rejects_glued_and_overlong_spellings() -> None:
    counts = count_label_occurrences(
        [
            _observation("LF-4A", 1.0, 1.0),
            _observation("XLF-4", 2.0, 1.0),
            _observation("LF-4AB", 3.0, 1.0),
            _observation("RUN LF-4 (2)", 4.0, 1.0),
        ]
    )
    assert counts == {"LF-4A": 1, "LF-4": 1}


def test_malformed_observations_and_boxes_fail_closed() -> None:
    with pytest.raises(ValueError, match="without text"):
        count_label_occurrences([{"x_pt": 1.0, "y_pt": 2.0}])
    with pytest.raises(ValueError, match="inverted"):
        count_label_occurrences([], exclude_boxes={1: [(10.0, 10.0, 0.0, 0.0)]})


def test_undeclared_sheets_never_enter_the_scored_sums() -> None:
    result = compare_tag_labels(
        {"sheet-a": {"LF-12": 3}, "sheet-z": {"LF-12": 9}},
        {"LF-12": 3},
        base_sheets=["sheet-a", "sheet-missing"],
        alternate_sheets=[],
    )
    assert result["rows"][0]["base_labels"] == 3
    assert result["undeclared_sheets"] == ["sheet-z"]
    assert result["declared_without_observations"] == ["sheet-missing"]


def test_a_sheet_declared_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="both base and alternate"):
        compare_tag_labels(
            {"sheet-a": {"LF-12": 1}},
            {"LF-12": 1},
            base_sheets=["sheet-a"],
            alternate_sheets=["sheet-a"],
        )


def test_all_zero_tags_are_not_scored() -> None:
    result = compare_tag_labels(
        {"sheet-a": {"LF-12": 4}},
        {"LF-12": 4, "LF-30": 0},
        base_sheets=["sheet-a"],
        alternate_sheets=[],
    )
    assert result["not_scored"] == ["LF-30"]
    assert result["exact_on_base"] == {"numerator": 1, "denominator": 1, "rate": 1.0}


def test_output_is_deterministic_and_json_serializable() -> None:
    observations = [
        _observation("LF-12 LF-8", 100.0, 300.0),
        _observation("LF-3A", 60.0, 40.0),
        _observation("LF-12", 55.0, 640.0, page=2),
    ]
    first_counts = count_label_occurrences(observations)
    second_counts = count_label_occurrences(observations)
    assert first_counts == second_counts
    assert list(first_counts) == sorted(first_counts)

    inputs = ({"sheet-a": first_counts}, {"LF-12": 2, "LF-8": 1, "LF-3A": 1})
    first_result = compare_tag_labels(*inputs, base_sheets=["sheet-a"], alternate_sheets=[])
    second_result = compare_tag_labels(*inputs, base_sheets=["sheet-a"], alternate_sheets=[])
    serialized = json.dumps(first_result)
    assert json.dumps(second_result) == serialized
    assert [row["tag"] for row in first_result["rows"]] == sorted(
        row["tag"] for row in first_result["rows"]
    )
    assert json.loads(serialized) == first_result
