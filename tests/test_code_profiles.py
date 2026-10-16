from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
import json

import pytest

from oabm.electrical.code_profiles import (
    JURISDICTION_CALIFORNIA,
    JURISDICTION_LA_CITY,
    JURISDICTION_LA_COUNTY,
    NEXT_KNOWN_SUPPLEMENT_DATE,
    STATE_TRANSITION_DATE,
    CodeSourceReference,
    resolve_code_profile,
)


def resolve(jurisdiction: str, application_date: str | date):
    return resolve_code_profile(
        jurisdiction_id=jurisdiction,
        permit_application_date=application_date,
        occupancy_class="Group B",
        project_class="tenant improvement",
        work_type="branch-circuit alteration",
    )


def test_statewide_selection_is_deterministic_at_and_around_cutoff() -> None:
    before = resolve(JURISDICTION_CALIFORNIA, "2025-12-31")
    after = resolve(JURISDICTION_CALIFORNIA, STATE_TRANSITION_DATE)

    assert before.profile is not None
    assert before.profile.edition == "2022 California Electrical Code"
    assert before.profile.underlying_nec_edition is None
    assert before.profile.application_interval.ends_on == date(2025, 12, 31)

    assert after.profile is not None
    assert after.profile.edition == "2025 California Electrical Code"
    assert after.profile.underlying_nec_edition == "2023 NEC"
    nfpa_basis = next(
        item for item in after.profile.boundary_references
        if item.source_id == "nfpa-2025-cec-nec-basis"
    )
    assert nfpa_basis.uri == (
        "https://www.nfpa.org/for-professionals/codes-and-standards/"
        "standards-development/nfpa-codes-and-standards-with-jurisdictional-changes"
    )
    assert after.profile.application_interval.starts_on == date(2026, 1, 1)
    assert after.profile.code_effective_interval == after.profile.code_operative_interval
    assert after.profile.errata_references[0].published_on == date(2026, 1, 1)
    assert after.profile.supplement_references == ()
    assert {item.source_id for item in after.profile.scheduled_supplement_references} == {
        "ca-bsc-2025-intervening-cycle",
        "ca-bsc-calcode-quarterly-fall-2026",
    }
    assert before.profile == resolve(JURISDICTION_CALIFORNIA, "2025-12-31").profile
    assert after.to_json() == resolve(JURISDICTION_CALIFORNIA, STATE_TRANSITION_DATE).to_json()
    assert after.code_verification_performed is False


def test_historical_date_outside_registered_edition_range_stays_unresolved() -> None:
    result = resolve(JURISDICTION_CALIFORNIA, "2022-12-31")

    assert result.profile is None
    assert result.profile_selection_status == "unresolved"
    assert result.unresolved_reasons == ("historical_code_profile_not_registered",)
    assert result.to_dict()["scope"]["permit_application_date"] == "2022-12-31"


def test_city_and_county_are_distinct_at_the_statewide_transition() -> None:
    county = resolve(JURISDICTION_LA_COUNTY, "2025-12-31")
    city = resolve(JURISDICTION_LA_CITY, "2025-12-31")

    assert county.profile is not None
    assert county.profile.edition == "2022 California Electrical Code"
    assert county.profile.local_adoption_effective_interval is not None
    assert county.profile.local_adoption_effective_interval.starts_on == date(2025, 12, 26)
    assert county.profile.local_adoption_operative_interval is not None
    assert county.profile.local_adoption_operative_interval.starts_on == date(2026, 1, 1)
    assert "local_amendment_inventory_incomplete" in county.unresolved_reasons

    assert city.profile is None
    assert city.candidate_profile_ids == ("la-city-cec-2022", "la-city-cec-2025")
    assert city.unresolved_reasons == ("city_permit_cutoff_unverified",)
    assert "2025-12-31" in " ".join(city.selection_rationale)


def test_city_profile_on_either_side_preserves_local_date_uncertainty() -> None:
    old = resolve(JURISDICTION_LA_CITY, "2025-12-30")
    new = resolve(JURISDICTION_LA_CITY, "2026-01-01")

    assert old.profile is not None
    assert old.profile.edition == "2022 California Electrical Code"
    assert old.profile.application_interval.ends_on == date(2025, 12, 30)

    assert new.profile is not None
    assert new.profile.edition == "2025 California Electrical Code"
    assert new.profile.local_adoption_effective_interval is not None
    assert new.profile.local_adoption_effective_interval.starts_on == date(2025, 12, 31)
    assert new.profile.local_adoption_operative_interval is None
    assert "city_local_operative_date_unverified" in new.unresolved_reasons
    assert "local_amendment_inventory_incomplete" in new.unresolved_reasons
    assert {ref.source_id for ref in new.profile.local_amendment_references} == {
        "la-city-council-file-25-1217",
        "la-city-ordinance-188797-published-packet",
    }


def test_future_supplement_is_not_active_before_its_effective_date() -> None:
    before = resolve(JURISDICTION_CALIFORNIA, "2026-10-16")
    at_effective_date = resolve(JURISDICTION_CALIFORNIA, NEXT_KNOWN_SUPPLEMENT_DATE)

    assert before.profile is not None
    assert before.profile.supplement_references == ()
    assert before.profile.scheduled_supplement_references
    assert at_effective_date.profile is not None
    assert {
        item.source_id for item in at_effective_date.profile.supplement_references
    } == {
        "ca-bsc-2025-intervening-cycle",
        "ca-bsc-calcode-quarterly-fall-2026",
    }
    assert at_effective_date.profile.scheduled_supplement_references == ()

    too_far_ahead = resolve(JURISDICTION_CALIFORNIA, "2027-07-02")
    assert too_far_ahead.profile is None
    assert too_far_ahead.unresolved_reasons == ("future_code_timeline_not_registered",)


@pytest.mark.parametrize(
    "jurisdiction",
    [JURISDICTION_CALIFORNIA, JURISDICTION_LA_COUNTY, JURISDICTION_LA_CITY],
)
def test_2025_application_interval_is_bounded_through_inclusive_registry_cutoff(
    jurisdiction: str,
) -> None:
    day_before = resolve(jurisdiction, date(2027, 6, 30))
    cutoff = resolve(jurisdiction, NEXT_KNOWN_SUPPLEMENT_DATE)
    day_after = resolve(jurisdiction, date(2027, 7, 2))

    assert day_before.profile is not None
    assert day_before.profile.edition == "2025 California Electrical Code"
    assert day_before.profile.application_interval.ends_on == NEXT_KNOWN_SUPPLEMENT_DATE
    assert day_before.profile.application_interval.contains(date(2027, 6, 30))

    assert cutoff.profile is not None
    assert cutoff.profile.edition == "2025 California Electrical Code"
    assert cutoff.profile.application_interval.ends_on == NEXT_KNOWN_SUPPLEMENT_DATE
    assert cutoff.profile.application_interval.contains(NEXT_KNOWN_SUPPLEMENT_DATE)
    assert cutoff.profile.code_effective_interval.ends_on is None
    assert cutoff.profile.code_operative_interval.ends_on is None

    assert day_after.profile is None
    assert day_after.unresolved_reasons == ("future_code_timeline_not_registered",)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("jurisdiction_id", "missing_jurisdiction_id"),
        ("permit_application_date", "missing_permit_application_date"),
        ("occupancy_class", "missing_occupancy_class"),
        ("project_class", "missing_project_class"),
        ("work_type", "missing_work_type"),
    ],
)
def test_missing_required_input_returns_no_profile(field: str, reason: str) -> None:
    values = {
        "jurisdiction_id": "ca-state",
        "permit_application_date": "2026-01-01",
        "occupancy_class": "Group B",
        "project_class": "tenant improvement",
        "work_type": "branch-circuit alteration",
    }
    values[field] = None

    result = resolve_code_profile(**values)

    assert result.profile is None
    assert reason in result.unresolved_reasons
    assert result.code_verification_performed is False


def test_unknown_or_ambiguous_local_authority_never_falls_back_to_statewide() -> None:
    ambiguous = resolve("Los Angeles", "2026-01-01")
    unknown = resolve("orange-county", "2026-01-01")

    assert ambiguous.profile is None
    assert "ambiguous_jurisdiction" in ambiguous.unresolved_reasons
    assert unknown.profile is None
    assert "unknown_jurisdiction_id" in unknown.unresolved_reasons


def test_profile_serialization_and_source_provenance_are_immutable() -> None:
    result = resolve(JURISDICTION_LA_COUNTY, "2026-01-01")

    assert result.profile is not None
    assert result.to_json() == resolve(JURISDICTION_LA_COUNTY, "2026-01-01").to_json()
    payload = json.loads(result.to_json())
    assert payload["profile_selection_status"] == "selected"
    assert payload["code_verification_performed"] is False
    assert payload["scope"]["occupancy_class"] == "Group B"
    assert payload["profile"]["application_interval"]["starts_on"] == "2026-01-01"

    source = next(
        item for item in result.source_references
        if item.source_id == "la-county-ordinance-2025-0048"
    )
    assert isinstance(result.source_references, tuple)
    assert isinstance(source, CodeSourceReference)
    assert source.sha256 == "9be7239ed61959d32002804a5451a42c7707d352a107fd08450cd3f0727f5e41"
    with pytest.raises(FrozenInstanceError):
        source.uri = "https://example.invalid/changed"
    with pytest.raises(FrozenInstanceError):
        source.basis.revision = "changed"

    payload["source_references"][0]["uri"] = "https://example.invalid/changed"
    assert result.source_references[0].uri != "https://example.invalid/changed"
    assert "compliant" not in result.to_json().lower()
    assert "approved" not in result.to_json().lower()
