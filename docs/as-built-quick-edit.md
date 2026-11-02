# Evidence-bound as-built quick edits (#238)

This source checkpoint is **unexecuted and unaccepted**. Laptop runtime, tests,
rendering and publication remain held for coordinated source acceptance. Hosted
CI has reopened independently; this branch has not been authorized to publish
or trigger checks yet.
Do not close #238 from the existence of these source files or tests.

The workflow uses the canonical `BuildingModel`; it does not change its schema,
reinterpret saved importer reviews, or alter the saved-revision comparator.
`pipeline/as_built.py` is the independent quick-edit transaction boundary.

## Operator flow

Prepare a validated canonical baseline with the schema features needed by the
installation (at least 1.2 for boxes and route products). Any explicit schema
upgrade happens **before** starting history, never inside a field transaction.
`start_history(model)` retains immutable normalized design bytes and their
SHA256. Original source documents remain separate source evidence.

Each transaction supplies exactly:

- `version: oabm-as-built-v1`, unique `transaction_id`, `actor`, and `reason`;
- `design_sha256` and the expected current `parent_sha256`;
- `evidence`: unique records with `id`, `kind`, `reference`, `sha256`, and
  `statement`. Quick-edit statements are retained inline and their UTF-8 digest
  is checked. Photo/scan/voice references and digests are retained testimony;
  this command does not read, authenticate, transcribe or interpret those files;
- `edits`: exact `collection`, stable `id`, `expected_sha256` (null only for a
  new entity), complete canonical `record` (null for explicit removal), and
  nonempty `evidence_ids`;
- `method_reconciliations`: an entry for every changed route type, containing
  the route ID and the complete before/after ID union for conductors, geometric
  fittings, raceway selections/outcomes and fitting selections/interfaces/outcomes;
- `installed_assertions`: exact current canonical record hashes with collection,
  ID and supplied field evidence IDs. An empty list leaves installation unconfirmed.

Use `record_sha256()` for entity fingerprints. `preview_transaction()` validates
the proposed transaction without saving it; start with an empty assertion list,
review the returned canonical result, then bind actual field installation
testimony to those exact record hashes. `apply_transaction()` returns a new
history. `replay_history()` verifies every parent, transaction and result.
JSON save/reload retains the complete requests, snapshots and history.

Derive edit records, expected entity hashes and parent hashes from
`replay_history(history)` or the typed canonical model loaded from the retained
`design_json`. A directly constructed `Point3(x=2, ...)` can still hold an
integer while typed JSON admission stores `2.0`; hashing the pre-admission
constructor object does not identify the retained parent. The example follows
the retained parent. Entity hash equality and stale-record refusal remain exact.

Existing provenance and source attributes cannot be rewritten by a quick edit.
The transaction appends user-derived provenance scoped to the actual changed
canonical fields, retaining actor, reason and supplied evidence. Explicit
unchanged dependent records can be reaffirmed without changing their labels.
No field note is promoted into a code-verification claim.

A device move requires explicit new poses for all owned ports and explicit
connected route and containing-box reconciliation. Canonical validation checks
the resulting endpoints, references and ownership only after all edits are
present. A wiring-method change requires a real supplied typed legacy-route
product selection, conductors and fittings to be reconciled together. Adding a
box edits that canonical inventory record and its outcome; it does not rerun
`select_electrical_boxes` with a partial intent list and erase unrelated boxes.
The existing whole-inventory producers remain unchanged.

Device moves currently support a rigid owner/port relationship: port position,
orientation and direction must equal the old connection transformed by the
device's supplied old-to-new rigid transform (1e-9 m/vector and 1e-12 quaternion
tolerances; whole-quaternion sign equivalence is allowed). A port's absolute
position may stay unchanged only when it is invariant under that transform,
such as a device rotating around that connection point; the required orientation
and direction must still agree. Reaffirmed evidence scopes are separate from
actual changed fields and cannot satisfy a pose-change guard. Independent
internal port relocation is outside this bounded quick-edit scope.

Edits affecting any supported route, device or box, including endpoint and
product/conductor dependencies, currently fail closed. Support assemblies and
selection-outcome membership are checked before canonical admission. Spatial
support contact is not established by reference validity alone; unchanged
hardware hashes and old installation assertions cannot authorize retaining
hardware after the supported object's geometry changes. Atomic support edits
remain unsupported; no support inventory is dropped or guessed.

## Admission limits

The existing bounded detached-capture utility runs before model serialization,
caller-object copying and replay. It rejects cycles, custom object types,
nonfinite numbers, and excessive depth/nodes with controlled `ContractError`
results. Models have a 4 MiB capture/JSON budget; requests have 512 KiB; complete
history has 16 MiB. Capture uses a conservative escaped-JSON byte charge, not
just raw file size. Depth is capped at 48 and capture nodes at 100,000; JSON is
also scanned before parsing for nesting, tokens and node limits. File admission
uses bounded reads and requires regular files, including a post-open file check.

There are at most 32 retained transactions, 256 edits/reconciliations, 64 evidence
records and 1,024 assertions per request. Retained history cumulatively allows
2,048 edits, 512 evidence records and 4,096 assertions. Every retained request
and embedded snapshot is preflighted before any replay; appending also checks
the resulting complete history. Exceeding a limit is an explicit refusal, not
permission to trim history or reset the design baseline.
Transaction IDs must be nonempty strings; this is checked for both new and
retained requests before any duplicate-ID hashing or membership operations.

## Delivery

Once execution reopens, the installed command is:

```text
oabm-as-built history.json transaction.json --output-directory /absolute/fresh/attempt
```

The synthetic four-edit example is:

```text
python examples/as_built_quick_edit.py /absolute/fresh/synthetic-attempt
```

It supplies a moved device, moved port, changed cable route, added box,
reconciled separate conductors/fittings and selected listed cable product. An
unrelated producer-owned box stays intact. All fixture facts are synthetic.

The fresh attempt contains one atomically published `delivery/` directory:
retained raw input JSON; design, parent and revised canonical snapshots;
replayable history; actual scaled vector plan and paginated panel schedule SVGs;
panel source facts; JSON and spreadsheet-safe CSV materials; and separate
parent/current and design/current canonical revision diffs using the existing
`build_revision_diff` API. Its real quantity deltas preserve old/new product
variants and units. Every artifact is hashed and bound to the same revised
snapshot in `MANIFEST.json`. SVG/HTML text is escaped. No export failure produces
a successful manifest. Existing output directories are never overwritten.

Plans use explicit generic location crosses plus supplied canonical envelopes.
Each plan requires an explicit positive story height, uses finite elevation
bounds capped by the next story, and opts into `traversed_stories` route selection.
Legacy routes and their fittings cannot appear on an unrelated floor merely
because a view has an oversized depth range. Endpoint levels must be known, but
they do not limit a route's geometry to those two stories: each actually
traversed finite story slice includes its clipped route portion, including an
intermediate story with neither endpoint and a run that leaves and returns to
the same story. A purely vertical clipped fragment has no XY line length, so it
gets an explicit location cross, a visible "Vertical route" label, canonical
source ID and clipped source-Z range in its metadata. This marker does not
represent a diameter, fitting or purchased component. The plans
remain review drawings. Layout, font appearance and source coverage need actual
runtime and visual verification before acceptance.

### Physical-sheet presentation

As-built plans select from finite standard paper/scale pairs rather than always
placing a small model on A0: 1:25 on A4/A3, 1:50 through A2, or 1:100 through A0.
The sheet prints its selected scale. Only derived view scale, annotations,
dimension offsets, stroke styles and presentation bounds change; canonical
geometry, endpoints, measured dimension values, IDs and quantities do not.

Full names are retained in a margin legend with canonical owner IDs. Short
callouts and owner-linked leaders are placed with the actual physical sheet's
DejaVu Sans measurements at 3 mm, its declared millimetre symbol envelope,
dimension text baseline gap and stroke clearance. The pixel-oriented
`layout_view_labels` pass is not applied. Callout rectangles avoid symbols,
outline strokes, dimension strings and each other. Leaders may exit their own
typed host/occupant envelope, but must avoid other labels and unrelated strokes.
Dimensions use separate outside rails for walls and openings; measured values
remain unchanged. If a finite placement search cannot place a callout, the
legend retains the complete label/source ID, explicitly says the callout needs
review, and the layout receipt lists it. No text is silently truncated or lost.

Supplied routes use a derived heavy blue dashed style, matching source-linked
route callouts and the legend key. The changed cable's centerline is the same
canonical centerline used by quantities. `drawings/plan-layout.json` retains
measured callout boxes, dimension text boxes, font, paper/scale and any unresolved
callouts. Each physical SVG also has its renderer manifest and source mapping.

Panel sheets use the existing drafted panel-card presentation on A3 with 3 mm
body text and measured heading styles, not the oversized fourteen-column fact
ledger. Unknown circuit positions, phases and protection stay explicitly
unknown; no slots, spares or protective ratings are invented. Logical row
coverage is checked exactly against the complete retained fact schedules;
`drawings/panel-layout.json` keeps that source-row audit.

The shared physical renderer's optional semantic stroke palette and bounded
primitive font styles are enabled explicitly here. Their defaults remain
unchanged for unrelated callers. This presentation is source work pending
runtime and actual visual acceptance, not a claim that every placement is clear.

Materials classify real canonical source contributions before aggregation,
preserving product variants, quantity/provenance bases and bid-group ownership.
Two identical boxes with only one field-confirmed produce one installed and one
unresolved box, rather than declaring both unresolved. Only exact current
field-confirmed source records enter installed lines. Product-bearing route and
fitting selections/interfaces also require confirmation. Unconfirmed design
facts and missing products remain visible unresolved lines, never actual-used
claims. Gross canonical quantities are not purchased stock. No prices, labor,
demolition, slack, waste or stock calculations are inferred.

## Synthetic reference-packet companion

The normal `examples/reference_production_set.py` reference-variant command also
generates the independent four-edit synthetic as-built example under
`attempt/as-built-example/delivery/`. It calls the same example helper and
canonical delivery workflow as the standalone command. A companion failure
propagates; the normal example command cannot report successful completion of
both examples if the as-built exporter failed.

This is a separate synthetic canonical model and a separate delivery, beside
the original production coordinator's `attempt/delivery/`. Their quantities,
totals and source claims are not combined. The original production model,
request, receipts and sealed delivery remain separate. The outer
`attempt/AS-BUILT-COMPANION.txt` identifies both receipts and explicitly marks
actual visual acceptance pending. The companion's `MANIFEST.json` binds its
own snapshots, history, actual SVG plans/panels, materials and differences.

A third, separate geometric-only model is retained under
`attempt/as-built-example/three-story-views/delivery/`, with its own complete
manifest, three actual per-story SVG plans and source snapshots. The middle
story has neither route endpoint: its vertical projection marker retains the
actual clipped 3–5.8 m source-Z range. All installation assertions are empty and
materials stay unresolved; these quantities are not added to either other
example. This exposes the multi-story rendering to actual artifact inspection
rather than keeping its only output in a test temporary directory. Generation
and visual inspection of the new proof remain pending for the repair head.

The existing CI reference-example invocation already creates this review tree.
Its existing `SHA256SUMS.txt` covers all files below `attempt`, including the
companion manifest, and the existing `synthetic-production-review` artifact
upload retains the enclosing review directory. No workflow, runner, credential,
permission, upload destination or artifact contract change is required. The
existing production PDF-to-PNG step is unchanged; the companion previews below
are generated by the normal example helper. Actual editable SVGs and retained
images still need inspection on an approved surface.
Retaining these files or passing CI does not establish visual acceptance,
engineering approval or whole-issue completion. All support, physical-product
and nonhorizontal physical-plan refusals described below continue to apply.

The PDF-enabled normal reference example now also requests full-sheet PNGs
rendered from each exact retained SVG. They live beside each independent
delivery in `preview-images/`, including the separate three-story example.
`PREVIEW-MANIFEST.json` binds every PNG to the original SVG hash, canonical model
hash, source delivery manifest, physical page dimensions and renderer scale.
These are complete generated pages, not cropped/enlarged screenshots or model
edits. Preview generation has finite pixel budgets and uses the already
configured CairoSVG extra; no new runner, workflow or destination is introduced.
All images remain explicitly unreviewed until independent actual inspection.

## Existing contract and consumer gaps

PhysicalRoute still refuses legacy raceway/fitting product selections. This
workflow preserves that refusal and reports physical-route lengths only as
geometric measurements with unresolved installed products. It does not hide a
product in source attributes. A physical-route wiring-method product edit cannot
complete under the existing canonical product contract.

The existing shared physical-sheet plan consumer supports horizontal physical
routes only. A nonhorizontal physical-route delivery currently fails visibly;
the dedicated all-height physical-route review adapter is separate and is not
silently substituted for a complete building plan. Whole-issue acceptance must
resolve this boundary or explicitly accept the supported legacy-route workflow
with geometric-only unresolved physical routes. No new contract issue is opened
from this lane without parent coordination.

## Pending verification

Hosted run [41479090133](https://github.com/banddude/officeadmin-building-model/actions/runs/41479090133)
passed at head `b38f75d` and retained its original synthetic packet. Independent
inspection then found a real false EVSE credit: provenance split a two-unit
quantity row into two one-unit rows and the shared revision mapper kept only
one. This repair totals original measured contributions once, without mixing variants, units or bases,
and adds the separate retained three-story example. The old packet remains
immutable evidence; green tests did not establish artifact correctness.

Repair run [41491309497](https://github.com/banddude/officeadmin-building-model/actions/runs/41491309497)
at `4038c26` was terminal **cancelled**, but its completed full-suite step failed:
1 failed, 6,220 passed, 13 skipped and 1 xfailed. The three-story example's raw
constructor record hash differed from its retained typed parent; its factory
now derives the proposal from replay of that parent. The stale-input guard is
unchanged. Packet generation and retention were skipped, so regenerated repair
visuals and multilevel artifact acceptance remain unverified.

No local Python import, test, render or build has been performed. Runtime,
hosted CI and visual acceptance of this repair head remain pending. After the
coordinated opening: run the synthetic command; inspect
actual plans/panel sheets/materials/differences; run `tests/test_as_built.py`,
`tests/test_as_built_review_regressions.py` and
CI-equivalent checks; add and run physical-route and adversarial HTML/CSV cases;
check complete manifest hashes; review current producer receipts and changed
product variants independently. No merge, publication, formal approval or issue
closure is authorized by this checkpoint.
