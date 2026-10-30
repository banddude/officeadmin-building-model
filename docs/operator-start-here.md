# Operator start here

This is the local, file-based workflow for preparing and reviewing sheets and
model previews. It is not an OfficeAdmin production integration, design service,
or engineering approval. The current combined production tools described here
are on the development PR branch until merged; check out that branch before
running these commands. `main` may not yet contain them.

**Privacy:** the repository is public. Keep actual job inputs, customer plans,
logos, generated files and attempts in a private directory **outside the
checkout**. The examples below are synthetic. Do not use example geometry,
ratings, topology or site coordinates as real job facts. No automatic inference
of framing, penetrations, survey, calculations, manufacturer fit or approval is
implied. Full coordinated floor/ceiling views, physical elevations/details and
engineering/calculation scope require further real-job work and review. Assembly
drawings support explicit plane-cut plans/sections, not hidden-line assembly
elevations.

## 1. Install and check the local runtime

From the checked-out development branch, on a machine with Python 3.11 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[pdf]'
python -c 'import oabm.quantities, oabm.routing, oabm.drawings.production_set, cairosvg; print("imports OK")'
fc-match 'Liberation Sans'
```

The PDF extra installs CairoSVG, but native Cairo and Fontconfig with readable
fonts must be installed separately. See [local PDF requirements](sheet-pdf.md)
and [company profiles](company-sheet-profile.md). The latter explains font
fallback, glyph checks and PDF font verification. A writable Fontconfig cache
may be needed; on a restricted Linux environment, for example:

```sh
mkdir -p /tmp/oabm-font-cache
export XDG_CACHE_HOME=/tmp/oabm-font-cache
```

DXF is optional and requires `pip install -e '.[cad]'`. None of these commands
uploads job data. For a source checkout, the examples below explicitly set
`PYTHONPATH` to use that checkout's code rather than another installed copy.

## 2. Run a complete synthetic set

Run from the repository root; choose a root that **does not already exist**:

```sh
PYTHONPATH=src:examples python examples/reference_production_set.py /tmp/oabm-reference --variant reference
```

The sample writes editable synthetic inputs to `/tmp/oabm-reference/inputs/` and
an attempt under `/tmp/oabm-reference/attempt/`. On success, inspect:

- `attempt/delivery/company/delivery/sheet-set.pdf` — combined ordered PDF;
- `attempt/delivery/company/delivery/sheets/` — editable vector SVG sheets;
- `attempt/delivery/PRODUCTION-MANIFEST.json` — index, hashes and limitations;
- `attempt/delivery/inputs/` and `attempt/delivery/derived/` — retained inputs,
  expanded job and source/layout maps.

The example uses the same production coordinator and shared company renderer
as real requests: cover, supplied site, single-line/panel schedules and explicit
detail recipes. `--variant residential` and `--variant commercial` exercise
other synthetic models; each needs its own fresh output root. The visual results
are review samples, not permit-ready sets.

## 3. Prepare a real request privately

Copy the appropriate **synthetic structure**, not its facts, into a private
working directory, then replace every example value with explicitly supplied and
reviewed job data. The generated `/tmp/oabm-reference/inputs/production.json`
shows a complete v2 request and its relative files. The request references:

- a saved company profile and an ordered job with date, revision, formats,
  sheet IDs/titles and any job/per-sheet overrides;
- **one validated canonical `BuildingModel` JSON**, with explicit source-backed
  equipment, circuits, geometry and identities required by requested consumers;
- cover project facts and an initial index matching the job list; supplied site
  geometry and optional graphics; explicit single-line/composition or schedule
  settings; and explicit detail recipes/placements as applicable.

These are structured inputs; a PDF or drawing is not automatically converted
into a complete canonical model or engineering design by this command. Do not
insert fictitious facts to pass checks. v2 allows optional consumers to be null
and supports explicit cut-only model views. Follow the exact schemas and
consumer limits in [production chain](production-sheet-set.md),
[physical assembly consumers](physical-assembly-consumers.md),
[site plan](supplied-site-plan.md), [single-line](electrical-single-line.md),
[schedules](schedule-sheet-production.md), and [details](detail-sheet-production.md).
Paths in the request resolve beside the request file. Keep source evidence and
resulting output under the same appropriate private access controls.

Before creating an attempt, run the bounded, read-only preflight:

```sh
oabm-production-sheets --check /private/job/production.json
```

It validates the request and referenced inputs, shared profile/job, canonical
model, content ownership, and selected consumer planning/detail bodies using the
same production contracts. It does not create a delivery or fetch a cover map.
A pass is **not** a guarantee that final composition, map acquisition, PDF
conversion, visual review, or engineering review will succeed. Errors identify
the input or producer where possible. Correct the actual input, then recheck.

Build with a new path:

```sh
oabm-production-sheets /private/job/production.json /private/attempt-001
```

Review the final PDF **and** SVGs, index, source/identity maps, notices and
manifest. The final cover index includes pagination/continuations; all sheets
share the same titleblock and PDF assembly path. This is an explicit supplied
presentation, not evidence of plan-set completeness or approval.

## Company defaults, job overrides and blank sheets

A reusable profile stores company fields, template and optional local PNG logos.
Resolution precedence is profile → job → per-sheet fields/template/logos;
empty text clears a field and `null` clears a logo. Profile asset paths resolve
beside the profile; job and sheet asset paths resolve beside the job JSON.
Creating a job does **not** alter saved defaults. To intentionally change defaults,
use the hash-guarded `update-defaults` command described in
[company profiles](company-sheet-profile.md); preserve the private profile and
logo files. The `--profile /private/company.json` option on the synthetic
reference example demonstrates a profile override, but its resulting packet must
then also remain private.

For blank drawing areas with **populated shared company titleblocks**, prepare
private profile and job JSON using `fixtures/company_sheets/v1/profile.json` and
`job.json` as synthetic structure, then run:

```sh
oabm-company-sheets create /private/company.json /private/job.json /private/blank-attempt
```

Find the blank SVGs and, if requested in `formats`, `sheet-set.pdf` under
`/private/blank-attempt/delivery/`. No model or design is created. The separate
[blank-sheet tool](blank-sheet-set.md) offers a generic bottom titleblock and
optional DXF; use the shared company tool above for the shared right-rail profile.

## Model/GLB review delivery

The canonical model can also feed a separate plan/GLB review package. Try the
synthetic fixture at a fresh path:

```sh
PYTHONPATH=src python -m oabm.exports.model_delivery \
  fixtures/model_delivery/v1/model.json fixtures/model_delivery/v1/options.json \
  /tmp/oabm-model-review
```

Inspect `delivery/plans/`, `delivery/preview.glb`, `delivery/READ-ME.txt`, the
bend report and `MANIFEST.json`. For a real job, supply its private canonical
model and explicit options/level IDs and radii, as applicable. `require`,
`report`, and `skip` have distinct bend-check meanings; a successful review
package is not an engineering approval. The optional caller-radius curved GLB
is explicitly gated and does not alter canonical sharp routes or quantities; it
does not verify swept clearance or manufacturer fit. See [model delivery](model-delivery.md).

## Rerun or rebuild safely

Every attempt path must be fresh, including after failure. A failed attempt can
retain `.pending/` and `failure.json`; inspect those privately, correct the
inputs, and retry to a different path. Never mix old outputs into a new attempt.
Production deliveries retain the **exact original request** as provenance at
`delivery/inputs/production-request.json`; its original paths are not rewritten
and may not resolve after moving or deleting the job. For editing or rerunning,
use the separate pre-pagination portable request and its input files:

```sh
oabm-production-sheets --check /private/attempt-001/delivery/inputs/portable/production.json
oabm-production-sheets /private/attempt-001/delivery/inputs/portable/production.json /private/attempt-002
```

The portable request preserves profile → job → sheet override precedence and
retains content-addressed logo assets. Check the delivery's
`portable_map_status`: a requested cover map can be rerun only while its retained
verified cache remains fresh. The portable request forces offline cache mode;
it never silently fetches a new map. A no-store/zero-lifetime or missing cache,
or an expired cache, blocks that rerun. Explicitly create a new normal request
and authorize any desired map acquisition separately.

For an **unedited** delivery, verify receipts and rebuild offline into a fresh
path with `oabm-production-sheets --rebuild /private/attempt-001/delivery
/private/rebuild-002` (on one shell line). Editing the portable inputs invalidates
the original receipts; use the normal rerun command above, not `--rebuild`.
Receipts detect changes but are not signatures or protection from concurrent
writers. Matching engine, dependencies and fonts are needed for matching output
bytes. Older deliveries without this portable snapshot cannot use this rebuild.
Shared company deliveries additionally support hash-verified rebuild:

```sh
oabm-company-sheets rebuild /private/blank-attempt/delivery /private/blank-rebuild-002
```

Model delivery can be rerun from its retained `inputs/model.json` and
`inputs/options.json` to a fresh path. Identical output bytes require matching
engine sources, runtime and fonts as recorded in the manifests; this is not a
hermetic runtime. Keep exclusive ownership of attempt contents during the build.
