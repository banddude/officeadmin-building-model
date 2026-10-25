# Electrical code profile resolver

The resolver identifies a code edition and preserves the jurisdiction and permit-date evidence used for that selection. It does not size electrical systems, apply normative electrical tables, interpret a plan, or certify engineering or code compliance. It always returns `code_verification_performed: false`. A selected profile can still contain unresolved local-amendment questions.

The API is `oabm.electrical.code_profiles.resolve_code_profile`. It requires an explicit jurisdiction, permit-application date, occupancy class, project class, and work type. The accepted jurisdiction IDs are `ca-state`, `la-county`, and `la-city`. A small set of exact published names is normalized to those IDs. “Los Angeles” is ambiguous and remains unresolved. There is no address parameter and no City-versus-County inference.

## Registered boundaries

The registry is versioned as of 2026-10-16.

| Jurisdiction | Permit-application interval | Selected edition | Additional date evidence |
| --- | --- | --- | --- |
| California statewide | 2023-01-01 through 2025-12-31 | 2022 California Electrical Code | BSC Bulletin 25-01 says applications received on or before 2025-12-31 generally use the prior code cycle. |
| California statewide | 2026-01-01 through 2027-06-30 inclusive | 2025 California Electrical Code | Effective and operative from 2026-01-01. The underlying NEC edition is recorded as 2023 NEC, verified against NFPA's California NEC reference. |
| Los Angeles County | Through 2025-12-31 | 2022 California Electrical Code | Historical local adoption instrument and dates remain unverified; the later 2025 ordinance is not assigned to this edition. |
| Los Angeles County | From 2026-01-01 through 2027-06-30 inclusive | 2025 California Electrical Code | Ordinance 2025-0048 adopts the 2025 CEC. It says the ordinance's added provisions are administrative and that prior Title 27 provisions continue unless changed or repealed. |
| City of Los Angeles | Through 2025-12-30 | 2022 California Electrical Code | The available City Council record does not establish the City's exact permit cutoff for the one-day transition. |
| City of Los Angeles | 2025-12-31 | Unresolved | The statewide bulletin generally places applications received by this date on the previous code cycle, while Ordinance 188797 is recorded effective on 2025-12-31. The resolver returns both candidate editions and no selected profile. |
| City of Los Angeles | From 2026-01-01 through 2027-06-30 inclusive | 2025 California Electrical Code | Ordinance 188797 is effective 2025-12-31. Its operative date and complete local amendment inventory are unresolved here. |

The 2025 CEC errata source is attached to profiles selected from 2026-01-01. The intervening-cycle supplement is recorded separately as scheduled, with effective date 2027-07-01. A scheduled cycle reference is never promoted to an active supplement solely because that date arrives. Application intervals for 2025 profiles end on 2027-06-30 inclusive; this registry coverage boundary does not invent a supersession date for the separate code-effective or code-operative intervals. Permit dates on or after 2027-07-01 remain unresolved until the applicable adopted supplement inventory and jurisdictional timeline are registered.

This first snapshot only maps applications from 2023-01-01 onward. Earlier permits stay unresolved instead of inheriting the 2022 or 2025 edition. The 2022 CEC underlying NEC edition is left unset because this slice did not verify that basis from a primary source.

Occupancy class, project class, and work type are required and preserved in the result. This first registry does not contain occupancy-specific adoption rules or work-type engineering rules. Missing or invalid required data, unknown jurisdictions, and ambiguous jurisdiction names return no profile. Local City and County profiles are marked incomplete while their complete amendment histories are not registered. For their 2022 profiles, adoption_reference and local adoption dates are null, with historical_local_adoption_unverified explicitly retained. The known jurisdiction authority is not evidence of a verified historical adopting instrument.

## Provenance and serialization

Profiles, date intervals, and source references are frozen dataclasses. The result stores source references in tuples and serializes them as deterministic JSON. Code effective and operative intervals are separate fields; local ordinance effective and operative intervals are recorded separately when known. Source references carry the issuing authority, instrument identifier, revision, exact URL, and SHA-256 digest where a pinned PDF was available. No code-table contents are embedded.

The recorded PDF digests are:

- BSC Information Bulletin 25-01: `923fbd5e984d02e8b95a1b6c95fc05a718e284600df61740ff30817d74efa99b`
- 2025 CEC errata issued 2026-01-01: `f96d7c6b1aa11ddbbf02299f076d43d0321635e545bbeb535b4d92485ed8f543`
- Los Angeles County Ordinance 2025-0048: `9be7239ed61959d32002804a5451a42c7707d352a107fd08450cd3f0727f5e41`
- Los Angeles City Clerk packet for Council File 25-1217: `ee12f040c819985ba8d4195c7b8180037a860fc4b3b491456330f78063750a37`

Landing pages do not have content hashes in this snapshot. Historical profiles are retained by the registry logic rather than rewritten in place when a later edition becomes effective.

## Pinned source references

Primary sources:

- [California Building Standards Commission code catalog](https://www.dgs.ca.gov/BSC/Codes)
- [BSC Information Bulletin 25-01](https://www.dgs.ca.gov/-/media/Divisions/BSC/06-News/Information-Bulletins/2025/BSC-Bulletin-25-01-FINAL.pdf), permit applications received on or before 2025-12-31
- [2025 California Electrical Code errata](https://www.dgs.ca.gov/-/media/Divisions/BSC/02-Codes/Errta-01012026-Pt3-CEC-locked.pdf), published 2026-01-01
- [2025 Intervening Code Adoption Cycle](https://www.dgs.ca.gov/BSC/Rulemaking/2025-Intervening-Cycle), supplements effective 2027-07-01
- [CBSC Fall 2026 cycle update](https://www.dgs.ca.gov/BSC/News/Page-Content/CALCode-Quarterly-Newsletters-Archive/CALCode-Quarterly-Fall-2026), supplement publication and adoption-cycle status
- [NFPA Codes and Standards with Jurisdictional Changes](https://www.nfpa.org/for-professionals/codes-and-standards/standards-development/nfpa-codes-and-standards-with-jurisdictional-changes), 2025 CEC based on the 2023 NEC
- [Los Angeles County Ordinance 2025-0048](https://file.lacounty.gov/SDSInter/bos/supdocs/210269.pdf), including its stated effective and operative dates
- [Los Angeles City Council File 25-1217](https://cityclerk.lacity.org/lacityclerkconnect/index.cfm?cfnumber=25-1217&fa=ccfi.viewrecord), final Ordinance 188797 and its recorded effective date
- [Los Angeles City Clerk published packet](https://cityclerk.lacity.org/onlinedocs/2025/25-1217_misc_12-31-25.pdf)

The County's current-codes page was not used to choose the 2025 code profile. The final adopting ordinance controls the County transition. City permit cutoff evidence for 2025-12-31 remains open, so the resolver deliberately does not choose a profile on that date.
