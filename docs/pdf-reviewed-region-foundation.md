# PDF reviewed-region foundation

Issue #381, first bounded implementation slice. This is not production PDF
admission, a titleblock detector, or a fix to actual room/device counts.

## Implemented boundary

The isolated module `oabm.importers.pdf_region_review` provides:

- `PdfByteSnapshot.read(path)`: one immutable bytes capture. Subsequent internal
  extraction and preview callbacks receive that same captured bytes value.
- Internal issuance machinery for deeply immutable extracted inventories and
  preview receipts. A context includes exact source SHA-256, original page
  frames, adapter/version and the complete supplied JSON observation inventory.
- Strict portable review parsing and atomic source/frame/extraction/render
  validation. Unknown fields, duplicate JSON keys, invalid coordinates, stale
  bindings, contradictory region interiors and unissued capabilities refuse.
- A pure evaluator over explicitly complete, adapter-certified candidate
  support. It returns decision records, not canonical entities.
- Explicit incomplete-support and hint-conflict failures. Identity references
  are typed triples (original page, observation kind, source ID); contextual
  references can point to a different original page without enlarging identity
  geometry. Every reference must exist in the retained inventory.

No canonical model, importer options, CLI, existing API or default behavior is
changed. Neither existing importer imports this module.

## Production support intentionally unavailable

`extract_review_snapshot(path, adapter)` and
`issue_default_review_preview(snapshot, page_number)` refuse with
`review_adapter_support_incomplete`. Neither production extraction adapters nor
a verified default-view renderer are admitted in this slice. There is no
caller-selectable fixture adapter or renderer. Synthetic producers exist only
in the test module.

The private `_extract_snapshot` and `_issue_preview` helpers are implementation
boundaries for future verified, hard-wired producers, not caller plugins.
Tests use those boundaries to exercise issuance and validation with invented
complete inventories and synthetic raster bytes. These tests do not prove
actual PDF extraction, preview visibility or rendering policy compliance.

A byte capture alone does not authenticate an extracted observation document.
A serializable frame or review record alone does not confer runtime authority.
Runtime capabilities are accepted only by identity in process-local issuance
registries. They cannot be copied, pickled or reconstructed from review JSON.
All nested inventory and review values are copied and frozen.

This is an in-process API discipline, not a sandbox against arbitrary hostile
Python code. Code with access to modify private module state is outside the
trust boundary. Production adapters must be independently reviewed before
being connected to the private machinery.

## Binding and review limits

Inventories preserve the complete producer-supplied payload, including unknown
payload fields, and reject duplicate (page, kind, source ID) identities. Their
versioned fingerprint includes normalized page frames and observations sorted
by stable identity. Future adapters must prove that they supply all relevant
extracted data; this generic mechanism cannot discover omitted observations.

Preview receipts bind exact bytes, frame, renderer/version, default View profile,
default document optional-content configuration, default visible annotations,
disabled external content and no interactive overrides. They also bind actual
producer-returned raster bytes, dimensions and source-to-raster transform.
Only a verified producer may promise those policies; writing the fields does
not implement them. Stable receipt IDs hash content and are separate from
nonserializable runtime authority.

Review author IDs and timestamps are assertions, not proof that a person looked
at a preview. The validator rejects stale supplied bindings. If a trusted
caller regenerates every binding and issued preview but reuses an old human
decision, the validator cannot detect that. Human re-review remains the
caller/workflow's responsibility. Opaque basis references are retained as
inert text and never fetched or executed.

## Pure geometry semantics

Coordinates are normalized displayed-bottom-origin PDF points, before model
registration. Version 1 accepts finite axis-aligned review rectangles wholly
within the verified visible page extent.

The candidate's complete conservative support bbox, not its anchor, determines
membership. Containment must be strictly interior by more than 1e-6 pt on every
side. Contact and epsilon-near contact are ambiguous, never snapped inside.
Degenerate point/line candidate support is permitted, but review rectangles
must have positive area.

Different-disposition rectangle interiors may not overlap, even when no
candidate is present. For valid input:

1. Contact with different dispositions yields unresolved ownership.
2. Otherwise one same-disposition rectangle containing the entire candidate
   authorizes that disposition. Crossing a smaller, nested rectangle of the
   same disposition does not veto the larger containing authorization.
3. Any remaining contact is unresolved. Adjacent rectangles are not unioned
   into a larger authorization.
4. Outside every reviewed rectangle, behavior remains unchanged.

Plan review leaves eligibility unchanged; it does not recognize a device,
raise confidence, assign a circuit or force materialization.
Non-plan review returns `reviewed_exclusion`; unresolved review returns
`unresolved`. Neither result deletes an observation or changes a model.
An explicit hint contacting non-plan or unresolved review raises
`review_hint_conflict`, rather than silently choosing one assertion. A complete
explicit hint contacting only plan regions remains unchanged even at a plan
boundary: plan review scopes automatic recognition, not explicit caller intent.
An incompletely supported hint contacting review cannot silently become an
unresolved automatic candidate; it raises `review_adapter_support_incomplete`.

An automatic candidate with incomplete but safely localized conservative support
can only yield unresolved when it intersects review. Without safe localization on a reviewed
page, evaluation rejects adapter readiness. The completeness flag is an
adapter assertion, not machine proof. No production support adapters currently
supply it.

## Deferred acceptance

This foundation does not close #381 or the broader PDF acceptance issues.
Required later work includes:

- Single-byte-snapshot architecture and electrical extractor adapters, with
  complete frame provenance and independently specified fingerprint versions.
- A verified default-view preview producer and explicit review workflow.
- Complete support bounds for every materializer, including text, curved,
  thick/mitered strokes and transformed Forms.
- All-candidate admission and downstream dependency closure, including ports,
  circuits, room boundaries, openings and repeated drawing-region provenance.
- Full source-bound audit retention and importer integration, preserving
  existing opt-out outputs byte-for-byte.

No private plans, source text, customer geometry or actual reviewed regions
are included in the implementation or tests.
