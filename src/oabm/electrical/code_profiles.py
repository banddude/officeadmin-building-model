"""Jurisdiction and permit-date code-profile selection with source provenance.

This module selects a published electrical-code edition and its adoption
references. It does not evaluate electrical design or determine compliance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import re
from typing import Final

from oabm.model import RuleReference


REGISTRY_AS_OF: Final = date(2026, 10, 16)
FIRST_REGISTERED_APPLICATION_DATE: Final = date(2023, 1, 1)
STATE_TRANSITION_DATE: Final = date(2026, 1, 1)
STATE_OLD_CUTOFF: Final = date(2025, 12, 31)
CITY_TRANSITION_DAY: Final = date(2025, 12, 31)
NEXT_KNOWN_SUPPLEMENT_DATE: Final = date(2027, 7, 1)

JURISDICTION_CALIFORNIA: Final = "ca-state"
JURISDICTION_LA_COUNTY: Final = "la-county"
JURISDICTION_LA_CITY: Final = "la-city"

_JURISDICTION_ALIASES = {
    "ca-state": JURISDICTION_CALIFORNIA,
    "california": JURISDICTION_CALIFORNIA,
    "state of california": JURISDICTION_CALIFORNIA,
    "la-county": JURISDICTION_LA_COUNTY,
    "los-angeles-county": JURISDICTION_LA_COUNTY,
    "los angeles county": JURISDICTION_LA_COUNTY,
    "county of los angeles": JURISDICTION_LA_COUNTY,
    "la-city": JURISDICTION_LA_CITY,
    "city-of-los-angeles": JURISDICTION_LA_CITY,
    "city of los angeles": JURISDICTION_LA_CITY,
}
_AMBIGUOUS_JURISDICTION_NAMES = frozenset({"los angeles", "la"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DateInterval:
    """Inclusive calendar-date interval; a missing bound means unspecified."""

    starts_on: date | None
    ends_on: date | None

    def __post_init__(self) -> None:
        for field_name in ("starts_on", "ends_on"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, date) or isinstance(value, datetime)
            ):
                raise ValueError(f"{field_name} must be a date or None")
        if (
            self.starts_on is not None
            and self.ends_on is not None
            and self.ends_on < self.starts_on
        ):
            raise ValueError("interval end must not precede its start")

    def contains(self, value: date) -> bool:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError("value must be a date")
        return (
            (self.starts_on is None or value >= self.starts_on)
            and (self.ends_on is None or value <= self.ends_on)
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "starts_on": self.starts_on.isoformat() if self.starts_on else None,
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
        }


@dataclass(frozen=True, slots=True)
class CodeSourceReference:
    """Immutable locator for a published adoption, errata, or cycle source."""

    source_id: str
    title: str
    authority: str
    uri: str
    document_id: str
    revision: str
    evidence_status: str
    sha256: str | None = None
    basis: RuleReference | None = None
    published_on: date | None = None
    effective_on: date | None = None
    operative_on: date | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_id", "title", "authority", "document_id", "revision",
            "evidence_status",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be nonempty")
        if not isinstance(self.uri, str) or not self.uri.startswith("https://"):
            raise ValueError("source uri must be an https URL")
        if self.sha256 is not None and not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        if self.basis is not None and not isinstance(self.basis, RuleReference):
            raise ValueError("basis must be a RuleReference or None")
        for field_name in ("published_on", "effective_on", "operative_on"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, date) or isinstance(value, datetime)
            ):
                raise ValueError(f"{field_name} must be a date or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "authority": self.authority,
            "uri": self.uri,
            "document_id": self.document_id,
            "revision": self.revision,
            "evidence_status": self.evidence_status,
            "sha256": self.sha256,
            "basis": _rule_reference_to_dict(self.basis),
            "published_on": _date_to_text(self.published_on),
            "effective_on": _date_to_text(self.effective_on),
            "operative_on": _date_to_text(self.operative_on),
        }


@dataclass(frozen=True, slots=True)
class CodeProfile:
    """Selected code edition with separate code, permit, and ordinance dates."""

    profile_id: str
    jurisdiction_id: str
    edition: str
    underlying_nec_edition: str | None
    adoption_authority: str
    adoption_reference: RuleReference
    application_interval: DateInterval
    code_effective_interval: DateInterval
    code_operative_interval: DateInterval
    local_adoption_effective_interval: DateInterval | None
    local_adoption_operative_interval: DateInterval | None
    local_amendment_references: tuple[CodeSourceReference, ...]
    errata_references: tuple[CodeSourceReference, ...]
    supplement_references: tuple[CodeSourceReference, ...]
    scheduled_supplement_references: tuple[CodeSourceReference, ...]
    boundary_references: tuple[CodeSourceReference, ...]
    local_amendments_complete: bool

    def __post_init__(self) -> None:
        for field_name in (
            "profile_id", "jurisdiction_id", "edition", "adoption_authority",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be nonempty")
        if self.underlying_nec_edition is not None and (
            not isinstance(self.underlying_nec_edition, str)
            or not self.underlying_nec_edition.strip()
        ):
            raise ValueError("underlying_nec_edition must be nonempty or None")
        if not isinstance(self.adoption_reference, RuleReference):
            raise ValueError("adoption_reference must be a RuleReference")
        for field_name in (
            "application_interval", "code_effective_interval",
            "code_operative_interval",
        ):
            if not isinstance(getattr(self, field_name), DateInterval):
                raise ValueError(f"{field_name} must be a DateInterval")
        for field_name in (
            "local_adoption_effective_interval",
            "local_adoption_operative_interval",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, DateInterval):
                raise ValueError(f"{field_name} must be a DateInterval or None")
        for field_name in (
            "local_amendment_references", "errata_references",
            "supplement_references", "scheduled_supplement_references",
            "boundary_references",
        ):
            refs = getattr(self, field_name)
            if not isinstance(refs, tuple) or any(
                not isinstance(ref, CodeSourceReference) for ref in refs
            ):
                raise ValueError(f"{field_name} must be a tuple of source references")
        if type(self.local_amendments_complete) is not bool:
            raise ValueError("local_amendments_complete must be a bool")

    def source_references(self) -> tuple[CodeSourceReference, ...]:
        return _dedupe_sources((
            *self.boundary_references,
            *self.local_amendment_references,
            *self.errata_references,
            *self.supplement_references,
            *self.scheduled_supplement_references,
        ))

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "jurisdiction_id": self.jurisdiction_id,
            "edition": self.edition,
            "underlying_nec_edition": self.underlying_nec_edition,
            "adoption_authority": self.adoption_authority,
            "adoption_reference": _rule_reference_to_dict(self.adoption_reference),
            "application_interval": self.application_interval.to_dict(),
            "code_effective_interval": self.code_effective_interval.to_dict(),
            "code_operative_interval": self.code_operative_interval.to_dict(),
            "local_adoption_effective_interval": (
                self.local_adoption_effective_interval.to_dict()
                if self.local_adoption_effective_interval else None
            ),
            "local_adoption_operative_interval": (
                self.local_adoption_operative_interval.to_dict()
                if self.local_adoption_operative_interval else None
            ),
            "local_amendment_references": [
                source.to_dict() for source in self.local_amendment_references
            ],
            "errata_references": [source.to_dict() for source in self.errata_references],
            "supplement_references": [
                source.to_dict() for source in self.supplement_references
            ],
            "scheduled_supplement_references": [
                source.to_dict() for source in self.scheduled_supplement_references
            ],
            "boundary_references": [
                source.to_dict() for source in self.boundary_references
            ],
            "local_amendments_complete": self.local_amendments_complete,
        }


@dataclass(frozen=True, slots=True)
class CodeProfileResolution:
    """A code-profile selection envelope; it never certifies the design."""

    jurisdiction_id: str | None
    permit_application_date: date | None
    occupancy_class: str | None
    project_class: str | None
    work_type: str | None
    profile: CodeProfile | None
    candidate_profile_ids: tuple[str, ...]
    source_references: tuple[CodeSourceReference, ...]
    selection_rationale: tuple[str, ...]
    unresolved_reasons: tuple[str, ...]
    registry_as_of: date = REGISTRY_AS_OF
    code_verification_performed: bool = False

    def __post_init__(self) -> None:
        if self.permit_application_date is not None and (
            not isinstance(self.permit_application_date, date)
            or isinstance(self.permit_application_date, datetime)
        ):
            raise ValueError("permit_application_date must be a date or None")
        if not isinstance(self.candidate_profile_ids, tuple) or any(
            not isinstance(item, str) or not item for item in self.candidate_profile_ids
        ):
            raise ValueError("candidate_profile_ids must be a tuple of nonempty strings")
        if not isinstance(self.source_references, tuple) or any(
            not isinstance(ref, CodeSourceReference) for ref in self.source_references
        ):
            raise ValueError("source_references must be a tuple of source references")
        if not isinstance(self.selection_rationale, tuple) or any(
            not isinstance(item, str) or not item for item in self.selection_rationale
        ):
            raise ValueError("selection_rationale must be a tuple of nonempty strings")
        if not isinstance(self.unresolved_reasons, tuple) or any(
            not isinstance(item, str) or not item for item in self.unresolved_reasons
        ):
            raise ValueError("unresolved_reasons must be a tuple of nonempty strings")
        if self.profile is not None and not isinstance(self.profile, CodeProfile):
            raise ValueError("profile must be a CodeProfile or None")
        if self.code_verification_performed is not False:
            raise ValueError("profile resolution cannot perform code verification")

    @property
    def profile_selection_status(self) -> str:
        return "selected" if self.profile is not None else "unresolved"

    @property
    def profile_selection_complete(self) -> bool:
        return self.profile is not None and not self.unresolved_reasons

    def to_dict(self) -> dict[str, object]:
        return {
            "registry_as_of": self.registry_as_of.isoformat(),
            "scope": {
                "jurisdiction_id": self.jurisdiction_id,
                "permit_application_date": _date_to_text(self.permit_application_date),
                "occupancy_class": self.occupancy_class,
                "project_class": self.project_class,
                "work_type": self.work_type,
            },
            "profile_selection_status": self.profile_selection_status,
            "profile": self.profile.to_dict() if self.profile else None,
            "candidate_profile_ids": list(self.candidate_profile_ids),
            "source_references": [
                source.to_dict() for source in self.source_references
            ],
            "selection_rationale": list(self.selection_rationale),
            "unresolved_reasons": list(self.unresolved_reasons),
            "code_verification_performed": False,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


# Published sources are kept as immutable records. SHA-256 values pin the
# downloaded official PDFs; source pages and the future-cycle notice are
# identified by their stable official URLs and instrument IDs.
_BSC_CODES = CodeSourceReference(
    source_id="ca-bsc-title24-codes",
    title="California Building Standards Code, Title 24, codes catalog",
    authority="California Building Standards Commission",
    uri="https://www.dgs.ca.gov/BSC/Codes",
    document_id="2022 and 2025 Title 24, Part 3 code catalog",
    revision="catalog reference",
    evidence_status="official_catalog_reference",
    basis=RuleReference(
        authority="California Building Standards Commission",
        revision="Title 24, Part 3",
        rule_id="California Electrical Code",
    ),
)
_BSC_BULLETIN_25_01 = CodeSourceReference(
    source_id="ca-bsc-bulletin-25-01",
    title="BSC Information Bulletin 25-01: 2025 California Building Standards Code",
    authority="California Building Standards Commission",
    uri="https://www.dgs.ca.gov/-/media/Divisions/BSC/06-News/Information-Bulletins/2025/BSC-Bulletin-25-01-FINAL.pdf",
    document_id="BSC Bulletin 25-01",
    revision="2025-07-01",
    evidence_status="official_pdf_sha256_pinned",
    sha256="923fbd5e984d02e8b95a1b6c95fc05a718e284600df61740ff30817d74efa99b",
    basis=RuleReference(
        authority="California Building Standards Commission",
        revision="BSC Bulletin 25-01",
        rule_id="2025 Title 24 permit application transition",
    ),
    published_on=date(2025, 7, 1),
)
_NFPA_2025_CEC_BASIS = CodeSourceReference(
    source_id="nfpa-2025-cec-nec-basis",
    title="NFPA NEC enforcement map, California Electrical Code basis",
    authority="National Fire Protection Association",
    uri="https://www.nfpa.org/education-and-research/electrical/nec-enforcement-maps",
    document_id="California 2025 Electrical Code",
    revision="2025 CEC based on 2023 NEC",
    evidence_status="official_page_indexed",
    basis=RuleReference(
        authority="National Fire Protection Association",
        revision="2023 NEC",
        rule_id="California 2025 Electrical Code basis",
    ),
)
_BSC_ERRATA_2026 = CodeSourceReference(
    source_id="ca-cec-2025-errata-2026-01-01",
    title="2025 California Electrical Code errata, issued 2026-01-01",
    authority="California Building Standards Commission",
    uri="https://www.dgs.ca.gov/-/media/Divisions/BSC/02-Codes/Errta-01012026-Pt3-CEC-locked.pdf",
    document_id="Title 24 Part 3 CEC errata",
    revision="2026-01-01",
    evidence_status="official_pdf_sha256_pinned",
    sha256="f96d7c6b1aa11ddbbf02299f076d43d0321635e545bbeb535b4d92485ed8f543",
    basis=RuleReference(
        authority="California Building Standards Commission",
        revision="2025 CEC errata",
        rule_id="2026-01-01 Part 3 errata",
    ),
    published_on=date(2026, 1, 1),
    )
_LA_COUNTY_ORDINANCE_2025_0048 = CodeSourceReference(
    source_id="la-county-ordinance-2025-0048",
    title="Los Angeles County Ordinance 2025-0048, Title 27 Electrical Code",
    authority="Los Angeles County Board of Supervisors",
    uri="https://file.lacounty.gov/SDSInter/bos/supdocs/210269.pdf",
    document_id="Ordinance No. 2025-0048",
    revision="adopted 2025-11-25",
    evidence_status="official_pdf_sha256_pinned",
    sha256="9be7239ed61959d32002804a5451a42c7707d352a107fd08450cd3f0727f5e41",
    basis=RuleReference(
        authority="Los Angeles County Board of Supervisors",
        revision="Ordinance 2025-0048",
        rule_id="Title 27, Section 80-1.5",
    ),
    effective_on=date(2025, 12, 26),
    operative_on=date(2026, 1, 1),
)
_LA_CITY_COUNCIL_FILE_25_1217 = CodeSourceReference(
    source_id="la-city-council-file-25-1217",
    title="City of Los Angeles Council File 25-1217, final Ordinance 188797 record",
    authority="Los Angeles City Clerk",
    uri="https://cityclerk.lacity.org/lacityclerkconnect/index.cfm?cfnumber=25-1217&fa=ccfi.viewrecord",
    document_id="Council File 25-1217; Ordinance 188797",
    revision="final ordinance record",
    evidence_status="official_record_reviewed",
    basis=RuleReference(
        authority="Los Angeles City Council",
        revision="Ordinance 188797",
        rule_id="Council File 25-1217",
    ),
    effective_on=date(2025, 12, 31),
)
_LA_CITY_ORDINANCE_PDF = CodeSourceReference(
    source_id="la-city-ordinance-188797-published-packet",
    title="City of Los Angeles Council File 25-1217 published packet",
    authority="Los Angeles City Clerk",
    uri="https://cityclerk.lacity.org/onlinedocs/2025/25-1217_misc_12-31-25.pdf",
    document_id="Council File 25-1217; Ordinance 188797",
    revision="published packet dated 2025-12-31",
    evidence_status="official_pdf_sha256_pinned",
    sha256="ee12f040c819985ba8d4195c7b8180037a860fc4b3b491456330f78063750a37",
    basis=RuleReference(
        authority="Los Angeles City Council",
        revision="Ordinance 188797",
        rule_id="2025 Title 24 local adoption",
    ),
    effective_on=date(2025, 12, 31),
)
_BSC_2025_INTERVENING_CYCLE = CodeSourceReference(
    source_id="ca-bsc-2025-intervening-cycle",
    title="2025 Intervening Code Adoption Cycle",
    authority="California Building Standards Commission",
    uri="https://www.dgs.ca.gov/BSC/Rulemaking/2025-Intervening-Cycle",
    document_id="2025 Intervening Code Adoption Cycle",
    revision="supplement effective 2027-07-01",
    evidence_status="official_rulemaking_page_reviewed",
    effective_on=date(2027, 7, 1),
)
_BSC_FALL_2026_CYCLE_STATUS = CodeSourceReference(
    source_id="ca-bsc-calcode-quarterly-fall-2026",
    title="CALCode Quarterly, Fall 2026, 2025 Intervening Cycle status",
    authority="California Building Standards Commission",
    uri="https://www.dgs.ca.gov/BSC/News/Page-Content/CALCode-Quarterly-Newsletters-Archive/CALCode-Quarterly-Fall-2026",
    document_id="2025 Intervening Code Adoption Cycle status",
    revision="fall 2026 update",
    evidence_status="official_newsletter_reviewed",
    effective_on=date(2027, 7, 1),
)


def resolve_code_profile(
    *,
    jurisdiction_id: str | None,
    permit_application_date: date | str | None,
    occupancy_class: str | None,
    project_class: str | None,
    work_type: str | None,
) -> CodeProfileResolution:
    """Resolve a named AHJ and permit date to a sourced code profile.

    Jurisdiction IDs and exact published names are matched explicitly. This
    function has no address input and never infers City versus County.
    """
    reasons: list[str] = []
    normalized_jurisdiction = _normalize_jurisdiction(jurisdiction_id, reasons)
    normalized_occupancy = _required_text(occupancy_class, "occupancy_class", reasons)
    normalized_project = _required_text(project_class, "project_class", reasons)
    normalized_work = _required_text(work_type, "work_type", reasons)
    parsed_date = _parse_application_date(permit_application_date, reasons)

    if reasons:
        return _resolution(
            jurisdiction_id=normalized_jurisdiction,
            permit_application_date=parsed_date,
            occupancy_class=normalized_occupancy,
            project_class=normalized_project,
            work_type=normalized_work,
            profile=None,
            candidate_profile_ids=(),
            source_references=_sources_for_unresolved(
                normalized_jurisdiction, parsed_date
            ),
            selection_rationale=(
                "Required scope data or an unambiguous registered jurisdiction is missing.",
                "No code profile was selected because this registry does not infer an AHJ or fill missing permit inputs.",
            ),
            unresolved_reasons=tuple(_dedupe_strings(reasons)),
        )

    assert normalized_jurisdiction is not None
    assert parsed_date is not None

    if parsed_date < FIRST_REGISTERED_APPLICATION_DATE:
        refs = _sources_for_unresolved(normalized_jurisdiction, parsed_date)
        return _resolution(
            jurisdiction_id=normalized_jurisdiction,
            permit_application_date=parsed_date,
            occupancy_class=normalized_occupancy,
            project_class=normalized_project,
            work_type=normalized_work,
            profile=None,
            candidate_profile_ids=(),
            source_references=refs,
            selection_rationale=(
                f"The registry begins at {FIRST_REGISTERED_APPLICATION_DATE.isoformat()} and has no earlier edition mapping.",
                "The resolver did not apply the 2022 or 2025 edition to an older permit.",
            ),
            unresolved_reasons=("historical_code_profile_not_registered",),
        )

    if parsed_date > NEXT_KNOWN_SUPPLEMENT_DATE:
        return _resolution(
            jurisdiction_id=normalized_jurisdiction,
            permit_application_date=parsed_date,
            occupancy_class=normalized_occupancy,
            project_class=normalized_project,
            work_type=normalized_work,
            profile=None,
            candidate_profile_ids=(),
            source_references=_sources_for_unresolved(normalized_jurisdiction, parsed_date),
            selection_rationale=(
                f"The registry only records the next known transition through {NEXT_KNOWN_SUPPLEMENT_DATE.isoformat()}.",
                "No later edition or jurisdictional transition was inferred.",
            ),
            unresolved_reasons=("future_code_timeline_not_registered",),
        )

    if normalized_jurisdiction == JURISDICTION_LA_CITY and parsed_date == CITY_TRANSITION_DAY:
        return _resolution(
            jurisdiction_id=normalized_jurisdiction,
            permit_application_date=parsed_date,
            occupancy_class=normalized_occupancy,
            project_class=normalized_project,
            work_type=normalized_work,
            profile=None,
            candidate_profile_ids=(
                "la-city-cec-2022",
                "la-city-cec-2025",
            ),
            source_references=_dedupe_sources((
                _BSC_CODES,
                _BSC_BULLETIN_25_01,
                _LA_CITY_COUNCIL_FILE_25_1217,
                _LA_CITY_ORDINANCE_PDF,
            )),
            selection_rationale=(
                "The statewide bulletin generally maps applications received by 2025-12-31 to the 2022 code.",
                "Los Angeles City Ordinance 188797 is recorded as effective on 2025-12-31.",
                "The exact City permit cutoff for this one-day transition is not established by the reviewed sources, so both editions remain candidates.",
            ),
            unresolved_reasons=("city_permit_cutoff_unverified",),
        )

    edition_year = 2022 if parsed_date <= STATE_OLD_CUTOFF else 2025
    if (
        normalized_jurisdiction == JURISDICTION_LA_CITY
        and parsed_date < CITY_TRANSITION_DAY
    ):
        edition_year = 2022
    profile = _make_profile(normalized_jurisdiction, edition_year, parsed_date)

    rationale = [
        f"Explicit jurisdiction {normalized_jurisdiction!r} selected; no address-based jurisdiction inference was performed.",
        f"Permit application date {parsed_date.isoformat()} falls in {profile.application_interval.starts_on.isoformat()} through "
        f"{profile.application_interval.ends_on.isoformat() if profile.application_interval.ends_on else 'the registered open interval'}.",
        f"The selected edition is {profile.edition}.",
        "Occupancy class, project class, and work type are retained as required scope inputs; this resolver does not apply work-specific engineering rules.",
        "Profile selection is separate from engineering verification.",
    ]
    if normalized_jurisdiction == JURISDICTION_LA_COUNTY:
        rationale.append(
            "County Ordinance 2025-0048 is legally effective 2025-12-26 and states that it becomes operative 2026-01-01."
        )
    if normalized_jurisdiction == JURISDICTION_LA_CITY:
        rationale.append(
            "City Ordinance 188797 is recorded as effective 2025-12-31; its operative date and complete local amendment inventory remain unresolved."
        )

    unresolved: list[str] = []
    if not profile.local_amendments_complete:
        unresolved.append("local_amendment_inventory_incomplete")
    if (
        normalized_jurisdiction == JURISDICTION_LA_CITY
        and edition_year == 2025
        and profile.local_adoption_operative_interval is None
    ):
        unresolved.append("city_local_operative_date_unverified")

    return _resolution(
        jurisdiction_id=normalized_jurisdiction,
        permit_application_date=parsed_date,
        occupancy_class=normalized_occupancy,
        project_class=normalized_project,
        work_type=normalized_work,
        profile=profile,
        candidate_profile_ids=(profile.profile_id,),
        source_references=profile.source_references(),
        selection_rationale=tuple(rationale),
        unresolved_reasons=tuple(unresolved),
    )


def _make_profile(
    jurisdiction_id: str, edition_year: int, application_date: date
) -> CodeProfile:
    is_2022 = edition_year == 2022
    edition = (
        "2022 California Electrical Code"
        if is_2022 else "2025 California Electrical Code"
    )
    application_interval = DateInterval(
        starts_on=FIRST_REGISTERED_APPLICATION_DATE
        if is_2022 else STATE_TRANSITION_DATE,
        ends_on=(
            date(2025, 12, 30)
            if is_2022 and jurisdiction_id == JURISDICTION_LA_CITY
            else STATE_OLD_CUTOFF if is_2022 else None
        ),
    )
    code_interval = DateInterval(
        starts_on=FIRST_REGISTERED_APPLICATION_DATE
        if is_2022 else STATE_TRANSITION_DATE,
        ends_on=STATE_OLD_CUTOFF if is_2022 else None,
    )

    local_effective: DateInterval | None = None
    local_operative: DateInterval | None = None
    amendment_refs: tuple[CodeSourceReference, ...] = ()
    boundary_refs: tuple[CodeSourceReference, ...] = (_BSC_CODES, _BSC_BULLETIN_25_01)
    local_amendments_complete = True
    authority = "California Building Standards Commission"
    adoption_reference = RuleReference(
        authority=authority,
        revision=f"{edition_year} CEC",
        rule_id="California Code of Regulations, Title 24, Part 3",
    )

    if jurisdiction_id == JURISDICTION_LA_COUNTY:
        authority = "Los Angeles County Board of Supervisors"
        adoption_reference = RuleReference(
            authority=authority,
            revision="Ordinance 2025-0048",
            rule_id="Los Angeles County Code, Title 27, Section 80-1.5",
        )
        local_effective = DateInterval(date(2025, 12, 26), None)
        local_operative = DateInterval(date(2026, 1, 1), None)
        boundary_refs = (*boundary_refs, _LA_COUNTY_ORDINANCE_2025_0048)
        if not is_2022:
            amendment_refs = (_LA_COUNTY_ORDINANCE_2025_0048,)
        local_amendments_complete = False

    elif jurisdiction_id == JURISDICTION_LA_CITY:
        authority = "Los Angeles City Council"
        adoption_reference = RuleReference(
            authority=authority,
            revision="Ordinance 188797",
            rule_id="Council File 25-1217",
        )
        local_effective = DateInterval(date(2025, 12, 31), None)
        local_operative = None
        boundary_refs = (
            *boundary_refs,
            _LA_CITY_COUNCIL_FILE_25_1217,
            _LA_CITY_ORDINANCE_PDF,
        )
        if not is_2022:
            amendment_refs = (
                _LA_CITY_COUNCIL_FILE_25_1217,
                _LA_CITY_ORDINANCE_PDF,
            )
        local_amendments_complete = False

    errata_refs: tuple[CodeSourceReference, ...] = ()
    supplement_refs: tuple[CodeSourceReference, ...] = ()
    scheduled_supplements: tuple[CodeSourceReference, ...] = ()
    if not is_2022:
        boundary_refs = (*boundary_refs, _NFPA_2025_CEC_BASIS)
        errata_refs = (_BSC_ERRATA_2026,)
        if application_date >= NEXT_KNOWN_SUPPLEMENT_DATE:
            supplement_refs = (_BSC_2025_INTERVENING_CYCLE, _BSC_FALL_2026_CYCLE_STATUS)
        else:
            scheduled_supplements = (
                _BSC_2025_INTERVENING_CYCLE,
                _BSC_FALL_2026_CYCLE_STATUS,
            )

    return CodeProfile(
        profile_id=f"{jurisdiction_id}-cec-{edition_year}",
        jurisdiction_id=jurisdiction_id,
        edition=edition,
        underlying_nec_edition=None if is_2022 else "2023 NEC",
        adoption_authority=authority,
        adoption_reference=adoption_reference,
        application_interval=application_interval,
        code_effective_interval=code_interval,
        code_operative_interval=code_interval,
        local_adoption_effective_interval=local_effective,
        local_adoption_operative_interval=local_operative,
        local_amendment_references=amendment_refs,
        errata_references=errata_refs,
        supplement_references=supplement_refs,
        scheduled_supplement_references=scheduled_supplements,
        boundary_references=boundary_refs,
        local_amendments_complete=local_amendments_complete,
    )


def _sources_for_unresolved(
    jurisdiction_id: str | None, application_date: date | None
) -> tuple[CodeSourceReference, ...]:
    refs: tuple[CodeSourceReference, ...] = (_BSC_CODES, _BSC_BULLETIN_25_01)
    if jurisdiction_id == JURISDICTION_LA_COUNTY:
        refs = (*refs, _LA_COUNTY_ORDINANCE_2025_0048)
    elif jurisdiction_id == JURISDICTION_LA_CITY:
        refs = (*refs, _LA_CITY_COUNCIL_FILE_25_1217, _LA_CITY_ORDINANCE_PDF)
    if application_date is not None and application_date >= STATE_TRANSITION_DATE:
        refs = (*refs, _NFPA_2025_CEC_BASIS, _BSC_ERRATA_2026)
        if application_date < NEXT_KNOWN_SUPPLEMENT_DATE:
            refs = (*refs, _BSC_2025_INTERVENING_CYCLE, _BSC_FALL_2026_CYCLE_STATUS)
    return _dedupe_sources(refs)


def _resolution(
    *,
    jurisdiction_id: str | None,
    permit_application_date: date | None,
    occupancy_class: str | None,
    project_class: str | None,
    work_type: str | None,
    profile: CodeProfile | None,
    candidate_profile_ids: tuple[str, ...],
    source_references: tuple[CodeSourceReference, ...],
    selection_rationale: tuple[str, ...],
    unresolved_reasons: tuple[str, ...],
) -> CodeProfileResolution:
    return CodeProfileResolution(
        jurisdiction_id=jurisdiction_id,
        permit_application_date=permit_application_date,
        occupancy_class=occupancy_class,
        project_class=project_class,
        work_type=work_type,
        profile=profile,
        candidate_profile_ids=candidate_profile_ids,
        source_references=_dedupe_sources(source_references),
        selection_rationale=selection_rationale,
        unresolved_reasons=unresolved_reasons,
    )


def _normalize_jurisdiction(
    value: str | None, reasons: list[str]
) -> str | None:
    text = _required_text(value, "jurisdiction_id", reasons)
    if text is None:
        return None
    key = text.casefold()
    if key in _AMBIGUOUS_JURISDICTION_NAMES:
        reasons.append("ambiguous_jurisdiction")
        return text
    canonical = _JURISDICTION_ALIASES.get(key)
    if canonical is None:
        reasons.append("unknown_jurisdiction_id")
        return text
    return canonical


def _required_text(value: str | None, field_name: str, reasons: list[str]) -> str | None:
    if value is None:
        reasons.append(f"missing_{field_name}")
        return None
    if not isinstance(value, str):
        reasons.append(f"invalid_{field_name}")
        return None
    text = value.strip()
    if not text:
        reasons.append(f"missing_{field_name}")
        return None
    return text


def _parse_application_date(
    value: date | str | None, reasons: list[str]
) -> date | None:
    if value is None:
        reasons.append("missing_permit_application_date")
        return None
    if isinstance(value, datetime):
        reasons.append("invalid_permit_application_date")
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        reasons.append("invalid_permit_application_date")
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        reasons.append("invalid_permit_application_date")
        return None


def _dedupe_sources(
    refs: tuple[CodeSourceReference, ...]
) -> tuple[CodeSourceReference, ...]:
    seen: set[str] = set()
    unique: list[CodeSourceReference] = []
    for ref in refs:
        if ref.source_id not in seen:
            seen.add(ref.source_id)
            unique.append(ref)
    return tuple(unique)


def _dedupe_strings(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _date_to_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rule_reference_to_dict(
    value: RuleReference | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "authority": value.authority,
        "revision": value.revision,
        "rule_id": value.rule_id,
    }
