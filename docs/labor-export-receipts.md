# Read-only exported labor and rental receipts (#234)

This is a **proposed generic interchange contract**, not a claim that OfficeAdmin
already exports this schema or that live authenticated integration is complete.
It adds no OA connection, credentials, writes, invoices, quotes or payroll actions.
The source checkpoint at `56bf81871edd98a060a740980b656528a9411905` passed the
parent's scoped source review; runtime acceptance remains pending on PR #396 and
a coordinated serial hosted run. This adapter is a separate unexecuted source step.

## Consumer entry point

```python
from oabm.labor import read_labor_json, generate_receipted_labor_report

request = read_labor_json(private_request_path)
receipts = read_labor_json(private_export_path)
report = generate_receipted_labor_report(canonical_model, request, receipts)
report.to_json()
report.to_csv()
report.validate_against(canonical_model, request, receipts)
```

The file reader caps bytes before decoding, rejects duplicate keys/nonfinite
constants and applies detached structural admission. API callers can supply plain
JSON documents directly. Outputs retain the original request, receipt export,
canonical snapshot and their bindings. Keep private files and generated reports
outside the public checkout. No files are written by this adapter.

A complete fictitious normalized export is `receipt_export_for` in
`tests/test_labor_reality.py`. None of its rates, fees or terms are defaults.
The top-level envelope has version `external-labor-export-v1`, canonical
`source_sha256`, exact original `request_sha256`, versioned provider metadata,
`standards`, `labor_prices`, and `rental_prices`. Receipt rows have unique IDs,
exact target ownership, `available`/`unresolved` state, explicit `missing_inputs`,
revision and source. Missing known receipt facts become unresolved; unknown keys
and contradictory supplied facts are refused. Missing interchange version or
provider revision is unresolved rather than certified pricing.

## Per-unit productivity and labor pricing

A standard names an operation, measurement unit and `person_hours_per_unit`.
The unit must match the operation. Absent/unresolved standards remove caller
fallback productivity; they never become zero hours. Each receipt preserves its
revision/source. This maps OA's verified `laborHours` hours-per-unit concept,
but authenticated exporting, assembly/component mapping and scope remain unproven.
The existing OA PATCH editing capability is source evidence only and is not called.

The core calculates quantity, conditions, effort, supplied crew/window capacity
and shared-resource ownership. It now exposes pricing-only missing inputs apart
from other unresolved work facts so an external price receipt cannot hide a missing
height, crew, work window, productivity standard or required lift. The adapter
clears request-local rates; it never introduces synthetic zero rates to obtain a
calculated result. The nested engine report remains visible with those missing
internal rates; top-level `provider-reported` rows show accepted external amounts.

A labor receipt binds the exact original work row and crew/rate selections by
`work_sha256`, declares `cost_bucket: labor`, and prices exact adjusted person-hours:

- `one-hour-unit`: unit `person-hour`, quantity equal to priced person-hours,
  amount equal to quantity × integer unit cents, rounded once half up.
- `total-hours-in-one-unit`: unit `operation`, quantity exactly one, and unit cents
  already include all priced hours. Amount equals those cents, with no additional
  multiplication by hours.

A whole assembly price containing materials, fixtures, subcontract or rental
buckets is refused as labor-only input. The exporter must obtain the labor bucket
from the correct org-scoped assembly/component expansion; this adapter does not
invent that mapping or assert org ownership from a supplied label.

## Rental occurrence receipts

Each receipt binds one original rental occurrence, including assignments,
mobilization, capacity and explicit hire/off-hire period, by `occurrence_sha256`.
It declares `cost_bucket: rental`, `price_basis: provider-occurrence-total`, unit
`occurrence`, quantity one, currency, revision and source. The final sell amount
is charged once regardless of assigned objects or billable duration.

Retain `priced_elapsed_hours` including overnight/shutdown gaps. `duration_basis`
explicitly distinguishes exact `elapsed-24-hour-days` from
`started-24-hour-days` (ceiling of elapsed hours / 24). `local-calendar-days`
currently remains unresolved: offset timestamps alone do not establish the
vendor's billing timezone, local day boundaries or partial-day contract. No
silent conversion at DST is permitted.

The proposed normalized export preserves the verified private interface concepts
`durationDays`, `mix`, `coverageDays`, `timeCents`, `deliveryPickupCents`, `fuelCents`,
`damageWaiverCents`, `environmentalCents`, `costCents`, `markupBps`, `markupCents`
and `sellCents`. This public code validates receipts instead of copying or running
the private rental-pricing implementation:

- Export explicit `tier_coverage_days` for day/week/month. Seven- and 28-day terms
  are one provider contract, never universal weeks/calendar months inferred here.
- Normalize `mix` into rows with unit, integer quantity, coverageDays and timeCents.
  Coverage must equal quantity × that provider's declared tier coverage; row time
  amounts must sum to timeCents. Total coverage must cover supplied durationDays.
- Cost equals time plus the four fee amounts. With declared
  `rounding: half-up-integer-cents`, markup equals cost × markupBps / 10000 rounded
  once, and sell equals cost plus markup. Integers and cumulative output are bounded.
- No alternate vendor mix, rate optimization, repricing or fee-rate inference is
  performed. In particular, time/fee totals are not converted to a per-day rate.

Fictitious source acceptance: 25 elapsed hours → two started 24-hour days. Time
600 + damage 84 + environmental 12 + delivery/pickup 100 + fuel 20 = cost 816;
2500 bps markup = 204; sell = 1020 cents, charged once for two assigned objects.
Four person-hours at 100 cents/hour cost 400; an already-total-hours 400-cent
receipt remains 400. These cases are source only and have not been executed.

## Exact remaining contracts

The verified OA source provides editable per-unit laborHours and org-scoped price
resolution/assembly expansion, with explicit missing inputs and optional hours.
Its current assembly snapshot omits some required unit/rule/rental evidence.
The full native export mapping is not verified. In particular, native `mix` row
shape, provider tier terms, fee basis, markup rounding declaration and explicit
hire-duration receipt binding must be exported/accepted before live integration.
The proposed normalized exporter must leave these missing fields unresolved; it
must not assume the native response already matches this public schema.

No real company/vendor/statutory rates, private production data or private source
implementation were copied. Independent source re-review, executed synthetic
outputs, authenticated read-only exporting and real-job acceptance remain pending.
This work does not close #234.
