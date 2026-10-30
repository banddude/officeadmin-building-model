# Source-backed branch ownership (#377)

This bounded evidence contract supplies source claims, not a second electrical
model. Canonical circuits, ports and field-scoped provenance are unchanged.
It does not close #76 or establish private real-plan acceptance.

## Contract and owning producer

`oabm.importers.pdf_branch_evidence.SourceBranchEvidence` is an immutable,
read-only envelope. It binds claims to the PDF SHA-256, a deterministic digest
of all actual extraction observations, page, displayed page frame, producer and
version. Each `BranchStrokeClaim` names one actual vector, its classification
(`electrical`, `architectural`, `conflicting`, `unresolved`), source support IDs
and confidence. It has no circuit/member IDs or page-clearance flag.

The owning electrical producer is `source_branch_evidence(document, page=...)`.
`evidence.validate(document)` replays that producer and compares the entire
envelope. Stale observations, fingerprints, frames, pages and altered claims
refuse. Manually listing expected vectors cannot establish ownership.

The initial producer supports one explicit same-page legend format:

- Exact heading `LINE LEGEND`.
- Exact row label `ELECTRICAL BRANCH WIRING` or `ARCHITECTURAL LINEWORK`, under
  one heading within 120 displayed points, with label/heading X difference at
  most 80 points.
- One horizontal straight sample, 10 to 80 displayed points long, strictly
  left of that label by at most 20 points and within 3 points of its baseline.
  Multiple possible samples or headings leave the row unsupported.
- Sample and classified strokes use the same observed DeviceGray and positive
  displayed width. Only opaque, solid, open, straight `S` strokes with supported
  default line-end conventions and uniform orthogonal transforms qualify.
  Dash, line-cap/join/miter operators, extended graphics states, non-DeviceGray
  colour spaces and unsupported paint refuse this producer's classification.

Heading/label text and samples must lie inside the displayed page frame.
This initial visibility boundary conservatively refuses page-wide legend text
semantics if the content uses nonzero text rendering modes, extended graphics
states, clipping or unsupported non-black text paint. It does not assume that
extracted text was visibly painted, nor try to infer visibility after clipping
or transparency. Delayed text callbacks cannot reset a hidden run into evidence.

These bounded dimensions locate a legend row; they do not classify branch runs
by their shape, length, junction degree or vector count. The electrical meaning
comes from the actual heading, row text and sample. An unlabeled stroke of the
same style establishes no ownership. Contradictory electrical/architectural
legend samples of the same style yield `conflicting` claims. An unrelated wall
claim or an empty wall list never creates electrical evidence.

Support IDs retain the heading, row text and sample. Confidence is 0.9 for
supported source-style claims, zero for unresolved strokes. This confidence is
an explicit producer limit, not a probability calibrated on real projects.

## Admission and uncertainty

The existing strict integration path is an import supplying both
`architecture_document` and its bound `wall_evidence`. That path now evaluates
every complete contacting candidate component before any circuit materializer.
Coverage includes unsupported curves and unclaimed closed paths, not merely
the straight strokes understood by later materializers. Recognized symbol
outlines retain their existing ownership exemptions.
Every stroke must have positive electrical ownership. Unresolved, conflicting
or architectural strokes reject the whole component; unknown strokes are never
trimmed out. Candidate/device/annotation IDs propagate this refusal through
homerun, direct-tag, named-text and topology paths, including unarrowed runs.
The existing positive architecture-conflict and enclosure guards still apply.

`branch_ownership_checks` retains full candidate/source IDs and per-stroke
claims. `branch_ownership_unresolved` explains ownership misses. The geometry
helper's `eligible` and architecture availability vocabulary remain unchanged;
eligibility is not admission. Alternate materializers report rejection rather
than rescuing membership. Recognition of panels/devices remains independent.

Observed printed numbers are scoped to `circuit_number`; observed voltage, poles
and phase retain their own scopes when supplied in the supporting source text.
Synthesized
source/load membership and ports carry inferred provenance. Supported branch
vectors retain their legend support IDs, producer/version and confidence.
Ports retain the supporting circuit evidence with port-relative scopes. Every
admitted materializer, including direct-tag, named-text and topology, attaches
the actual component's ownership support; this is not homerun-only evidence.

## Compatibility and acceptance

Calls without the architecture handoff retain their legacy behavior. This is
not a claim that legacy imports reject all unclassified linework. No new
panel-recognition release or full #76 acceptance follows from this slice.
Unknown input may now produce fewer circuits in the strict path; this is the
intentional uncertainty-preserving migration. Supplying arbitrary IDs or a
whole-page clearance flag cannot restore them.

The source-PDF positive controls add the supported actual legend. Raw ambiguous
PDFs remain unchanged in the negative control. The original annotated-unclassified
acceptance now passes without xfail: it emits no circuit or ports and retains an
explicit unresolved diagnostic. No source signal was added to that negative.
Single-load, multitap, 350-point, 65-segment and 350-segment branches remain
supported through the real producer. Tests also cover unsupported styles,
conflicting legends, touching unknown/architectural strokes, alternate paths,
stale evidence, permutation and canonical serialization.

Broader legend formats, source-specific validation, private raw/minimal-help
counts and architecture spot checks remain separate. Missing semantics are
reported, not guessed. No customer data or private historical branch is used.
