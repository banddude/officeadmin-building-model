# Shared production chain

`python -m oabm.drawings.production_set REQUEST.json FRESH_OUTPUT` composes a
cover, supplied site drawing, optional canonical single-line, paginated panel facts and explicit
canonical detail recipes into one shared-profile SVG/PDF packet. Individual
consumer commands remain available. The executable public-safe proof is:

```sh
PYTHONPATH=src python examples/production_sheet_set.py /fresh/synthetic-chain
```

This first produces blank drawing sheets with populated title rails, then a
populated set from the same company/job inputs. Schedule pagination expands its
explicit template slot. The final cover index derives from that expanded list;
all pages are exported together by `create_company_sheet_set`, with one PDF
conversion/assembly path. There is no post-export manual PDF splicing.

`fixtures/production_set/v1/production.json` names the profile, job, canonical
model, cover, supplied site request, schedule layout settings and detail recipes.
Paths resolve beside that request. Site and schedule entries explicitly name
their sheet/template IDs. Every template sheet must have exactly one content
owner. The cover source index must match the initial ordered job list; the
coordinator retains it and emits a derived index after schedule pagination.
Continuation IDs must not collide with other sheets. Common project name,
location/address and project number must agree between cover and titleblock;
site titleblock scale must agree with the explicit site drawing denominator.
Per-sheet field/logo/template overrides still use the shared profile resolver.

Schedules and details consume the same validated canonical model bytes. The
site tool consumes separately qualified supplied drawing geometry; it does not
promote it into surveyed canonical geometry. Existing report arithmetic,
unknown/conflict reporting, detail guards and body overflow checks remain owned
by their existing tools. Raw inputs, portable expanded profile/job/assets,
cover/site maps, canonical schedule row bindings and projected drawing JSON are
retained. `PRODUCTION-MANIFEST.json` binds them and the nested shared delivery.
Keep private inputs and their resulting deliveries out of the public repo.

`single_line: null` retains the original partial-set mode with no single-line
sheet. An explicit `single_line: {"sheet_id": "E601"}` selects a template slot;
the existing single-line planner expands it into its deterministic page IDs and
the same canonical model supplies its topology, schedules and details. All
single-line sources/layout maps are retained, and its pages enter the same shared
PDF call and final cover index. No connections are inferred from site proximity.
Use `--compact` on the executable example to exercise that model plus explicit
`detail-sheet-set@2` slots placing three unchanged-scale details on one sheet.
The original one-detail-per-sheet mode remains supported. This presentation set
is not a permit-set completeness or engineering approval claim.
No demand/service sizing, installation fit, survey accuracy or engineering
approval is inferred. Content and pixel review remain necessary. Output is a
fresh attempt; failures never publish a completed top-level delivery. Keep
exclusive ownership throughout generation. Full font/PDF runtime requirements
are documented in [company-sheet-profile.md](company-sheet-profile.md).

The compact example can be run with
`PYTHONPATH=src python examples/production_sheet_set.py --compact /fresh/compact-chain`.
Schedule packing follows the schedule layout's explicit mode; no model facts,
view scales or notes are changed to reduce page count.

The demonstration defaults to the compact supplied residential topology: cover,
site, single-line, packed panel facts and a three-detail sheet. Pagination is
derived from current content and renderer capacity; a historical five-page proof
is not a fixed output contract.
`--legacy` selects the earlier two-panel/one-table-per-sheet input mode.
The generic production API still takes explicit job/model/layout inputs; it does
not infer project scope or silently change view scales.

Cover planning now runs after the other page counts are known and resolves its
own continuation/index fixed point. Returned cover IDs/titles/bodies are cloned
from the original shared template; all cover pages enter the single final index
and PDF. Source scope/contact text and layouts remain retained without truncation.

For an explicitly requested site fit, the derived shared job prints
`audit.presentation.selected_scale`; original request/job bytes remain intact.
The outer production manifest retains site transforms, coverage and presentation
limits. Fixed-scale requests still require the supplied rail denominator to agree.

`--commercial` exercises the complete 27-panel branching model through the same
API: cover, site, caller-numbered SL-020 and its required continuations, packed panel facts and three
unchanged-scale equipment details. The synthetic MAIN envelope is explicitly
supplied for detail projection; no manufacturer dimensions are inferred. The
example retains 31 schematic connections and 58 canonical schedule rows.
Caller numbering and reservations are passed to the single-line planner before
cover pagination, and conflicts with other lane IDs fail before publication.

## Explicit model-view sheets

`production-sheet-set-v2` adds `model_views` to the original keys. Unrequested
cover/site/schedules/details can explicitly be null, so physical architecture
views need no fake electrical/detail inputs. Supported cut-only model views use
the same shared sheet/PDF boundary, exact scale/titleblock checks and source
identity maps; requested covers retain the final index. Unsupported consumers
still refuse. See [physical assembly consumers](physical-assembly-consumers.md)
for the strict row contract and executable two-sheet example.


## Connected drafting review samples

The complete prepared-job example uses the same production API and existing
component renderers, with useful-panel selection and a wide feeder register:

```sh
PYTHONPATH=src:examples python examples/reference_production_set.py /fresh/representative --variant reference
PYTHONPATH=src:examples python examples/reference_production_set.py /fresh/residential --variant residential
PYTHONPATH=src:examples python examples/reference_production_set.py /fresh/commercial --variant commercial
# Edit retained input JSON, then regenerate into a different fresh attempt:
oabm-production-sheets /fresh/representative/inputs/production.json /fresh/revised-attempt
```

Use the existing project PDF extra/native Cairo and supported local fonts described
in the company-profile documentation. The example produces editable input JSON,
SVG sheets and a combined PDF under `attempt/delivery/company/delivery`, plus
`attempt/delivery/PRODUCTION-MANIFEST.json` and retained source/identity maps.
An optional `--profile /private/company.json` uses supplied private defaults/assets;
keep that input and its resulting delivery outside this public repository.
Per-job overrides do not update the saved company profile.

Each variant preserves its own canonical equipment/circuit identities. Synthetic
external envelopes, supports and one route are explicitly authored in that same
model for the detail recipes; they are not manufacturer or surveyed facts. The
site example is a separate explicitly qualified supplied ledger, with retained
graphics and source-frame warnings. It does not establish coordinate alignment
with schematic equipment. Caller IDs, final pagination and cover index are
resolved together. Empty panel tables are emitted only when requested; excluded
facts remain in the full canonical reports and feeder register.

These are review samples, not accepted permit sets. Actual electrical drafting
and full-page composition require independent pixel review; mechanical tests,
font bounds and reproducible output do not establish professional acceptance.

### Explicit automatic cover map acquisition

V2 requests may add `cover_map` with `mode` (`auto` or `cached`),
`cache_directory` (local path resolved beside the request) and `candidate_index`
(integer or null). When no supplied `location_map` exists, the coordinator uses
its cover owner's existing OfficeAdmin Apple-only adapter for the cover's explicit
project location. An ambiguous or nonmatching geocode requires a selected `candidate_index` plus
optional-key `candidate_confirmation`, the SHA256 fingerprint returned with the
choice. Confirmation binds the original query, Apple provider, exact candidate
label and coordinates; an index alone cannot authorize a changed result. `cached`
requires a valid retained cache entry; it does not acquire a fresh map. Omitting
this object preserves the existing supplied-map behavior and makes no network
request. Supplied location maps take precedence over automatic acquisition.

The resulting `CoverPage.images` pass through the shared typed PNG channel, and
visible Apple attribution remains part of the cover layout. The nested delivery
retains the exact PNG, hash and placement; `derived/cover-map-source.json` retains
provider/qualification metadata. Existing OfficeAdmin access owns authentication;
no credentials, authorization headers or signed provider URLs enter job receipts.
Map acquisition and exact framing remain separately reviewed capabilities; imagery
is vicinity context and does not establish surveyed site coordinates.

## Read-only request preflight

Before making a fresh attempt, `oabm-production-sheets --check REQUEST.json`
validates the existing production request, input files, shared profile/job,
canonical model, sheet ownership and selected consumer planning, including
actual detail-body source checks. Python callers can use
`preflight_production_sheet_set(request_path)`. Both paths reuse the production
coordinator and its validators; they do not create an attempt or fetch/cache a
cover map. A successful result is bounded: final composed bodies, PDF conversion,
map acquisition, visual review and engineering acceptance remain unverified.
The check does not claim every possible build failure has been ruled out. Input
path and consumer diagnostics require correction of the supplied facts, not
invented values. See the [operator guide](operator-start-here.md) for a runnable
workflow and private-data handling.


## Portable production rerun and verified rebuild

Every new production delivery retains the exact original request and named raw
snapshots under `inputs/`; original filenames and paths are provenance, not a
relocatable command. A separate `inputs/portable/production.json` uses fixed
local paths to pre-pagination copies of all requested inputs. Profile/job/sheet
field, template and logo precedence—including explicit null logo clearing—is
preserved; every referenced or overridden logo is snapshotted by content hash.
Optional v2 consumers, site graphics, model views and explicit composition stay
in the request. The manifest receipts cover those copies and assets.

For an **editable** rerun use:

```sh
oabm-production-sheets --check /private/delivery/inputs/portable/production.json
oabm-production-sheets /private/delivery/inputs/portable/production.json /private/fresh-attempt
```

For an **unedited, receipt-verified** rebuild use:

```sh
oabm-production-sheets --rebuild /private/delivery /private/fresh-rebuild
```

`rebuild_production_sheet_set(delivery, output_directory=...)` exposes the same
Python operation. It verifies all outer receipts, refuses symlink/escaping or
unreceipted portable files and live-map mode, then calls the existing production
coordinator. It is not a signature, authenticated history, locking protocol or
a hermetic runtime; preserve exclusive ownership. Rebuild output must be outside
the source delivery. For intentionally edited portable inputs use the ordinary
command, not the receipt-verified rebuild. Rebuilt artifacts match with identical
sources, runtime and fonts; provenance snapshots may differ because the rebuild's
request is the portable copy rather than the original source JSON. Older deliveries
without portable receipts do not support this operation.

Automatic/cached cover maps remain subject to the original Apple provider,
source, candidate and freshness checks. The portable request always uses
`cached` mode, and only its relevant verified cache receipt/image are retained.
`portable_map_status` in the production manifest records whether a fresh cache
was retained or why it could not be retained. A no-store/zero-lifetime response,
missing receipt or subsequently expired cache cannot rebuild that map offline;
it fails without fetching. `--check` remains bounded and does not inspect or
fetch the cache. Make an explicitly authorized new normal request if a new map
is needed. No cache expiry, provider or identity guard is bypassed.
