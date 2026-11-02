# Evidence-bound as-built quick edits (#238)

This source checkpoint is **unexecuted and unaccepted**. Laptop runtime, tests,
rendering, publication and CI remain held for the coordinated recovery window.
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

Plans use explicit generic location crosses plus supplied canonical envelopes,
and show all heights in the level rather than implying a hidden slice. They
remain review drawings. Layout, font appearance and source coverage need actual
runtime and visual verification before acceptance.

Materials partition the real canonical quantity report. Only exact current
field-confirmed source records enter installed lines. Product-bearing route and
fitting selections/interfaces also require confirmation. Unconfirmed design
facts and missing products remain visible unresolved lines, never actual-used
claims. Gross canonical quantities are not purchased stock. No prices, labor,
demolition, slack, waste or stock calculations are inferred.

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

No Python import, test, render, build or CI execution has been performed for this
checkpoint. After the coordinated opening: run the synthetic command; inspect
actual plans/panel sheets/materials/differences; run `tests/test_as_built.py` and
CI-equivalent checks; add and run physical-route and adversarial HTML/CSV cases;
check complete manifest hashes; review current producer receipts and changed
product variants independently. No merge, publication, formal approval or issue
closure is authorized by this checkpoint.
