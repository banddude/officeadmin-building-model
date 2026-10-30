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
- Sample and classified strokes use observed black DeviceGray and the same positive
  displayed width. Only opaque, solid, open, straight `S` strokes with supported
  default line-end conventions and uniform orthogonal transforms qualify.
  Dash, line-cap/join/miter operators, extended graphics states, non-DeviceGray
  colour spaces and unsupported paint refuse this producer's classification.

## Enforceable paint and resource subset

The extraction records `PdfTextObservation.branch_legend_paint_supported` only
after visiting the complete page. True requires every actual operation to belong
to the explicit plain page-level stroke/text grammar in `_make_page_visitors`;
an unknown operator or state leaves the page's evidence unsupported. False and
missing values mean unproven, not a claim that all page text is invisible.

The admitted text uses standard nonembedded Type1 Helvetica without custom
encoding, ToUnicode, widths, descriptor or glyph program. Each text object has
one printable normalized ASCII literal `Tj`, its own explicit font and position,
finite positive size, supported
nondegenerate transforms and an upright displayed baseline. Text state must be
`Tr=0`, `Tz=100`, `Ts=Tc=Tw=0`. Default settings and explicit equivalent settings
are supported. `Tj` must be followed directly by `ET`; geometry/graphics changes
inside a text object are unsupported. Delayed callbacks, inherited font state
and later state resets cannot manufacture supported paint evidence.

Other admitted paint consists of opaque black DeviceGray stroke paths with
finite positive width, default line ends and supported uniform transforms.
Forms (including their implicit BBox clipping), images, inline images, shadings,
fill paints, custom fonts, annotations, optional-content/marked-content groups,
nontrivial CropBox, clipping, transparency and unknown resource/container semantics
are outside this subset. Page groups and DefaultGray overrides also refuse it.
An explicit CropBox equal to MediaBox remains supported. Legacy extraction and
recognition continue; these restrictions govern evidence admission only.

Heading, row and sample paint bounds must lie inside the displayed page and be
clear of every other painted text/path bound. Path bounds include stroke width
and curve control hulls. Text bounds conservatively include Helvetica advances,
descenders and a margin. Overlap or an unknown bound refuses the support region
regardless of paint order, including thick-stroke overpaint. No visibility is
inferred from extracted strings alone. Joined and closed paths use a conservative
full default-miter-limit envelope (10 stroke widths) to cover acute joins.
This is a deliberately narrow admission
proof, not a PDF renderer. Broader sources require separate rendered validation
or a source-bound reviewed-evidence boundary.

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
Cycle/enclosure and wall vetoes run in component preflight before every
materializer. A loop assembled from separate source-owned open strokes is still
rejected; electrical style ownership does not override geometry safety. Strict
rejection propagates vector, affected-load and nearby annotation identities so
a later direct tag cannot rescue a rejected homerun or unarrowed component.

`branch_ownership_checks` retains full candidate/source IDs and per-stroke
claims. `branch_ownership_unresolved` explains ownership misses. The geometry
helper's `eligible` and architecture availability vocabulary remain unchanged;
eligibility is not admission. Alternate materializers report rejection rather
than rescuing membership. Recognition of panels/devices remains independent.

Observed printed numbers are scoped to `circuit_number`; observed voltage, poles
and phase retain their own scopes when supplied in the supporting source text.
Synthesized source/load membership and ports carry inferred provenance. Supported branch
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
