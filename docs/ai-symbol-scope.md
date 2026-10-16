# Source scope through AI symbol review

The optional `SymbolMark.scope` / `SymbolProposal.scope` boundary preserves quoted
source assertions through the existing verified symbol run and reviewed canonical
intake. It adds no canonical entities or model schema version. Source assertions
are inferred interpretations of pixels; a fake transport test does not establish
live extraction accuracy or verify that a quote was interpreted correctly.

`oabm.proposals.symbol_scope` exposes:

- `SymbolScopeEvidence(image_id, bounds, quote)`: a nonempty source quote and
  positive-area normalized bounds in the cited image's crop. Coordinates use the
  existing top-left crop convention. The image may be the target, legend,
  schedule or notes, and must belong to the request.
- `SymbolScopeClaim(dimension, value, evidence, confidence, alternate_id=None)`:
  `work_status` reuses `new`, `relocated`, `existing_to_remain`, `removed`
  from the existing electrical importer, plus `not_in_contract` for N.I.C.
  `bid_scope` accepts `base` or `alternate`. An alternate requires its
  printed source identifier. All claims require bounded quoted evidence.
- `SymbolScope(claims, version="1.0.0")`: preserves every assertion. Its
  `work_status` and `bid_scope` properties are `unresolved` without assertions
  for that dimension, or `conflict` when values or alternate IDs disagree.
  No confidence ranking chooses a winner.
- `symbol_scope_schema(request)`: an optional JSON-schema property definition
  for a provider transport. Use it as the optional mark.scope property.
  Strict typed parsing additionally checks bounds ordering, distinct evidence
  and all source bindings.

A caller's source-only extraction question should request explicit source scope
quotes and their locations, preserve contradictory statements, and omit claims
when their meaning or applicability is unclear. A repeated type tag or a bare
E/N/R letter is insufficient. Read its actual source definition. This boundary
does not implement another marker classifier, infer salvage purchases, assume
unmarked work is new, select an alternate, or certify completeness.

For example, source text explicitly describing salvage for relocation can
produce a `relocated` claim with both the symbol's identifying annotation and
the applicable legend/note cited. Do not turn that text into a `new` claim.
A base/alternate assertion similarly requires applicable source text; absence
of an alternate note does not establish base scope. Unknown and conflicting
source facts remain usable inventory with those explicit states.

## Binding and review

Every scope citation is checked against the supplied request. Exported
`scope_sources` contains the complete existing verified `SourceImage` records
for exactly the cited images, including PDF digest/page and render/crop
manifests. The existing journal and source-pixel verification protect these
assertions with the same raw-output binding as geometry and type proposals.
Canonical identity still follows native source-mark geometry, independent of
scope or corrected type. Reviewing a type or resolving a free-text unknown
cannot discard a scope conflict.

Reviewed intake writes the original assertions under
`attributes.pdf_electrical.scope_assertions`, reuses
`attributes.pdf_electrical.scope_status`, and preserves `bid_scope` /
`alternate_id` there. Each citation contributes inferred, field-scoped
provenance with its actual source/page, image/PDF digests, exact claim/quote,
source image crop bounds and request/result fingerprints. Its confidence is
bounded by the original mark and assertion. Placement review never relabels
those source interpretations as observed or user-authored.

The existing scope ledger's five-state vocabulary remains unchanged:
N.I.C., conflicts and absent work assertions adapt to `unresolved` with
distinct reasons `not_in_contract`, `ai_scope_conflict` and
`ai_scope_unresolved`. Its original source snapshot retains the full facts.
These reasons are not `no_scope_marker`, so the existing optional confirmed
unmarked policy cannot silently turn them into new work.

Legacy symbol output without `scope` stays readable and reserializes with that
optional field absent. Legacy proposal keys/fingerprints remain unchanged.
Canonical models and source journals require no migration. A new extraction
cannot overwrite a different existing object: use the explicit edit workflow.
Repeated identical review/intake remains idempotent.

## Quantities

The existing quantity engine still counts canonical objects once. Reviewed AI
symbols additionally expose `work_status`, `bid_scope` and `alternate_id`
in their line variants. Line confidence is also bounded by consumed assertion
and applicable scope evidence confidence. Only explicit `new` plus explicit `base` can enter the
ungrouped base. Reuse/retained, relocation, removal, N.I.C., alternates, conflicts
and unknowns go into deterministic identified groups with the prefix
`ai-symbol-scope:` and JSON scope labels. Caller bid groups are preserved in
those labels and cannot override source status or wash an alternate into base.
Every AI inventory report carries a warning that these are source-scoped object
counts, not automatically selected alternates or purchase approval.

Old reviewed AI objects with no scope metadata remain unknown and get a
separate unresolved group. Contradictory canonical metadata versus preserved
source assertions raises `QuantityError`. Non-AI canonical objects and existing
caller group behavior keep their previous serialized outputs.

A consumer must retain groups, line variants, provenance and warnings. Summing
all inventory rows across groups does not produce a base purchase list. There is
no automatic substitution/deduction logic for alternatives, no demo labor,
safe-off, disposal, tie-in engineering or production OfficeAdmin integration.
Those broader acceptance criteria remain on #231.

## Synthetic verification

`tests/test_symbol_scope.py` authors an independent two-page PDF, verifies its
scope phrases are present, renders actual source images, dispatches a fake
transport, verifies its journal, reviews canonical devices/equipment, and
consumes the scope ledger and quantity engine. It covers new versus salvage,
retained/removal/N.I.C., an unselected alternate, contradictory tags,
ambiguous letters, source/evidence validation, serialization and repeated
imports. Every source, quote and identifier is synthetic.
