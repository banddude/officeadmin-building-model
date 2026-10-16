# AI electrical symbols to reviewed canonical objects

The primary interpretation path is `extract_symbol_review` in
`oabm.importers.pdf_electrical.symbol_stage`. It dispatches bounded symbol
requests through `proposals.symbols`, then builds a review handoff. It never
runs the legacy deterministic shape matcher. Existing recognition can supply
optional independent checks without blocking this path.

Provider selection, strong-model escalation, exact PDF/pixel verification and
append-only attempt receipts belong to the symbol adapter. The caller supplies
the actual PDFs, rendered images, configured policy, adapters and private
per-request journals. This module neither chooses provider/model aliases nor
makes network calls itself. Dispatch exceptions propagate; failed run statuses
and their receipts remain explicit in review coverage and the journals.

The stage is replaceable: preserve requests, immutable source digests, renders,
outputs, receipts and subsequent review decisions privately. Another recognizer
can later be evaluated against that frozen evidence. Evaluation labels and human
answer records must never be included in inference input.

## Two explicit steps

```python
from oabm.importers.pdf_electrical.symbol_stage import (
    extract_symbol_review, materialize_reviewed_symbols,
    SymbolRegistration, SymbolPlacement, SymbolDecision,
)

# One newly created AttemptJournal per request, keyed by request fingerprint.
# Each file records exactly that request's full escalation/attempt sequence.
journals = {request.fingerprint: journal_for_request(request) for request in requests}
journal_paths = {key: journal.path for key, journal in journals.items()}
extraction = extract_symbol_review(
    requests, policy, adapters=adapters, documents=documents,
    images=images, journals=journals,
)
# Present extraction.review and source pixels to an authorized reviewer.
# Construct explicit SymbolDecision and SymbolRegistration records separately.
outcome = materialize_reviewed_symbols(
    model, extraction.review, runs=extraction.runs,
    documents=documents, images=images, journal_paths=journal_paths,
    decisions=decisions, registrations=registrations,
)
reviewed_model = outcome.model
```

`build_symbol_review` can also rebuild a manifest from saved typed `SymbolRun`
records and caller-selected trusted journal paths keyed by request fingerprint.
Both building and consuming a manifest reverify source pixels through
`SymbolRun.proposals`, including equality with the persisted attempt receipts and raw
response in that journal. Each journal file contains exactly one run, including its escalation attempts.
Batch extraction rejects shared or incomplete journal mappings before calling
a provider. Each request receives its entire declared image/document context from
the batch union, without undeclared target tiles belonging only to another request.
Conflicting image identities, missing bytes and extra undeclared inputs fail
before dispatch. Journal paths are supplied separately by the trusted caller;
a saved result cannot choose or attest its own journal. Saved manifest hashes are bindings, not source
attestations. Consumption recomputes the entire manifest and rejects any change
in proposals, coverage or source context. Review decisions bind both the exact
manifest and complete proposal fingerprint. No confidence score accepts a mark.

A manifest retains every proposal, including unknown categories and conflicting
interpretations. It also retains full run envelopes containing context coverage,
scanned tiles, coverage notes and attempt identities. An empty result does not
prove an empty drawing or complete extraction.

## Review, corrections and overlaps

`SymbolDecision` records an explicit `accept`, `reject`, `defer` or `duplicate`
action, reviewer identity, derivation and rationale. Review is a separate caller
input, never a field accepted from the recognition adapter. Applications must
authorize that input; this library does not authenticate a human. A separately
configured AI review must use `reviewer_derivation="inferred"`; a user decision
uses `"user"`. Neither changes the original AI provenance to observed facts.

For an acceptance, `SymbolPlacement` supplies the corrected canonical entity
kind (`device` or `equipment`), type, displayed-page anchor, height above level
in metres, global yaw in radians, registration fingerprint and remaining
unknowns. These fields have no inferred defaults. Every proposal unknown must
be explicitly resolved or retained. Correcting a type or anchor preserves the
source mark's canonical ID. An anchor correction must stay within that mark's
source bounds; a wrong mark envelope requires a new source-bound proposal.

Intersecting source boxes on the same PDF digest, source alias and page are
potential duplicates even when proposed categories disagree. All such candidates
remain visible. Every member must receive a non-deferred disposition before a
survivor materializes. Exact instance labels repeated across pages also raise
a review question; repeated fixture/type tags do not. Multiple accepted overlapping marks require an explicit
rationale for treating each as distinct. A duplicate points to an explicitly
accepted survivor; if the survivor cannot materialize, the duplicate stays
pending too. No automatic suppression or cross-page merging occurs.

## Registration and physical limits

`SymbolRegistration` binds the original PDF digest, source alias, one-based page,
drawing-region bounds, canonical level and existing `PdfPageTransform`, plus
explicitly classified evidence. Displayed source points use bottom-left origin;
canonical geometry uses metres in the model's right-handed frame. The transform
frame must match the live model; its `z_m` datum must match the supplied level
elevation. The reviewed anchor must lie in the selected drawing region. Control
points and residuals can travel in the transform's existing registration record.

Missing registration leaves an accepted mark pending. Wrong source/page/frame,
missing levels, mismatched elevation datums and anchors outside the source mark
or drawing region fail without mutating the input model.

Materialized `ElectricalDevice` and `ElectricalEquipment` objects retain AI
confidence (further limited by weaker registration or level evidence) and all source provenance, registration evidence, review decisions,
original proposed geometry and unresolved facts. Explicit review can create a
corrected type while preserving the original proposal. Linear source polylines
remain source evidence; their length is not treated as physical route length or
fixture dimensions. Physical size, hosting and connectivity remain unknown.
There are no guessed ports, circuits, loads, conductor sizes or routes.

The returned canonical model records the full review manifest, registrations,
decisions and dispositions under `attributes.ai_symbol_stage_runs`. Unreviewed
or blocked marks remain there as pending evidence, separate from canonical
instance counts. Replaying an identical review is idempotent. A conflicting
existing canonical object is rejected rather than overwritten; subsequent model
editing belongs to an explicit edit workflow.

## Existing consumers

The output is the existing `BuildingModel`, not a new electrical representation.
Existing drawing generation derives plan symbols and schedules; quantities count
only materialized canonical instances. The focused synthetic test also exports
a GLB and builds the verified review bundle, retaining AI uncertainty and source
references. Review bundles require an explicitly supplied model schema 1.3 or
1.4; the stage never silently upgrades the model.

This slice does not certify completeness, physical installation or code
compliance. Missing physical facts and engineering rules remain explicit inputs
to subsequent stages. Private drawings, source evidence and generated review
artifacts stay outside the public repository and public CI.
