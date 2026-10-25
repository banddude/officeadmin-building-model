# Explicit support-station coverage screen

`oabm.reports.support_coverage.report_support_coverage` is an opt-in, read-only
report. It checks supplied spacing/end-distance limits against explicit stations
on existing canonical routes and support components. It does not place supports,
invent attachment hardware, alter quantities or certify an installation.

Supply the current `support_model_fingerprint(model)`, typed
`SupportSpacingRule` records (route ID/type, explicit `RuleReference`, maximum
between-support span and maximum distance from either end, in metres), and typed
`SupportStation` records (route ID, existing component ID, distance along the
route, in metres). Both inputs require explicit provenance. Retain/review the
input origin: a matching hash binds a chosen snapshot, not authenticity.

The exact canonical serialization is the model binding; changes including
revision/header changes require new explicitly selected inputs. The report
captures existing source snapshots with owner-relative provenance and retains
input evidence without widening its scope or confidence.

Each station must refer to a support (not a fastener), whose actual assembly
names that route as supported. Its centerline point must fall within the
component's oriented catalog envelope using the existing geometric tolerance.
This is a conservative geometric screen, not exact profile contact. For example,
a valid rail beneath a pipe can fail this centerline test; use an explicitly
modeled clamp/contact component or retain the unresolved result. Do not enlarge
envelopes or invent stations merely to pass the screen. One component cannot
provide multiple stations on the same route; shared assemblies can support
multiple routes without creating new quantities.

Results are `gaps_found`, `unresolved`, or `within_supplied_limits`. Missing rules,
stations, unresolved support selections, wrong route types and rejected contact
claims remain visible. Gaps include their arclength intervals and caller-supplied
limits; arclength, not endpoint distance, is used on bent routes. An empty model
does not produce a pass. Malformed, duplicate, dangling or stale inputs fail.

Even `within_supplied_limits` verifies only this supplied-station screen.
Physical contact, attachment capacity, code compliance and installation
completeness always remain unverified. No default NEC spacing, special-location
exception, fitting allowance, support load or fastener specification is guessed.
Rule applicability and engineering acceptance remain external responsibilities.

This is ephemeral report input, not a new canonical contact representation or
an automatic materials-list completion flag. Persisted contact geometry and
automatic support placement require separate reviewed contract work. Existing
model versions, selectors, saved pipelines and default exports are unchanged.

Tests use the existing public synthetic shared-trapeze fixture and check source
binding, missing/invalid inputs, field evidence, geometric screening, spacing,
bent-route arclength, determinism and unchanged canonical quantities.
