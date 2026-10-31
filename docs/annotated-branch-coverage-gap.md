# Annotated unclassified branch acceptance probe (#76)

Current implementation status: [Source-backed branch ownership](branch-source-ownership.md)
adds a bounded explicit-legend producer and whole-component refusal in the strict
architecture-handoff path. The original negative source now passes without xfail.
The investigation below records the original measured gap; full #76 and private
real-plan acceptance remain open.

This investigation leaves production behavior unchanged. It does not close #76
or #72, and does not alter the reviewed #322 branch or the #320/#321 files.

## Source-PDF result

Reproduced on current main `87f9bcff14988b270da47fa85938d5fd39628f41`,
which includes the independently reviewed #322 fix.

`tests/test_pdf_branch_annotated_unclassified.py` generates actual PDF bytes
through the existing public-safe sheet generator. Both owning extractors and
the actual `source_wall_evidence` producer run. There is no expected JSON,
pre-extracted answer fixture, provider call or customer artifact.

The sheet carries a recognized panel LP, a valid LP-1 annotation by a homerun
arrow, and EVSE-1 on a six-segment branch. A separate pair of wall faces is
positively classified elsewhere on the sheet. Two single-stroke open segments
form an L touching the branch and reaching EVSE-2 and EVSE-3.

| Actual source input | Circuits | Ports | Circuit loads |
| --- | ---: | ---: | --- |
| L starts 20 pt away from the branch | 1 | 2 | EVSE-1 |
| L endpoint touches the branch | 1 | 4 | EVSE-1, EVSE-2, EVSE-3 |
| Same touching bytes interpreted as a lawful three-load branch | 1 | 4 | Same observable input and result |

The sole circuit annotation names LP and circuit 1. It does not name EVSE-2 or
EVSE-3. Their circuit membership comes from the geometric traversal.

The touching case reports `eligible` and `supplied_positive`, with no conflicting
wall-chain IDs and zero unresolved circuits. The actual architecture source IDs
for the local L are absent from the producer's positively classified chains.
All branch and L strokes share observed style: straight open black paths,
1 pt wide, painted with S. No annotation, layer or style in this fixture says
whether the L is wiring or architecture.

The two author interpretations can have byte-identical PDFs and identical
electrical observations, architectural page observations and classified chains.
Their intended meanings are not input evidence. This is a missing source signal,
not a connectivity threshold that a smaller code change can recover.

## Provenance boundary already available

The importer currently records the LP-1 annotation as
`pdf-homerun-annotation`, `derivation=observed`, without `scope_paths`.
The canonical `provenance_applies_to(record, ("load_port_ids",))` therefore
treats that record as applying to the entire expanded membership.
The synthesized ports separately have inferred provenance; that does not narrow
the circuit's unscoped observed claim.

The canonical contract already supports scoped provenance. No canonical schema
extension is needed to distinguish an observed annotation fact, such as a circuit
number, from membership inferred through unclassified geometric adjacency.
A later correction must scope observed evidence to the facts it actually states
and carry geometric association uncertainty on the membership claim.

Changing only the provenance label does not prevent extra load IDs from joining
a canonical circuit. It cannot be presented as completion of the classification
or membership gate.

## Minimum missing classification boundary

Before topology can materialize source-supported membership, the owning evidence
lane needs to provide source-bound semantic ownership for the traversed branch,
rather than just candidate eligibility and positive architecture elsewhere:

1. Bind every claim to exact PDF bytes, page, displayed drawing frame and actual
   vector/source observation IDs. Retain the owning producer, method/version,
   confidence and the source text/legend/style evidence supporting the claim.
2. Distinguish positively supported electrical branch geometry, positively
   classified architecture, and unresolved geometry in the local component.
   Absence from a wall list is not a positive electrical classification.
   `supplied_positive`, a drawing bounding box and an empty handoff are not
   coverage evidence.
3. Retain actual recognition ownership. A manually supplied expected-ID list,
   whole-page clearance flag, nearest-device association, or unbound proposal
   must not turn unresolved geometry into observed wiring.
4. Refuse conflicting or unresolved membership before canonical materialization,
   preserving the complete component and its uncertainty across homerun,
   direct-tag, named-text and topology passes. Do not trim unknown strokes and
   certify the remainder as a complete source circuit.
5. Prove lawful annotated single-load, multi-tap, long and densely segmented
   branches through the actual owning producer. No vector-count, distance,
   degree, shape or paint-value cutoff can be used as a substitute for semantics.

This is a boundary requirement, not a new implemented handoff or classifier.
The existing `SourceWallEvidence` intentionally supplies only positive wall
claims. Extending that status vocabulary to imply local coverage would contradict
its contract. The electrical candidate list currently includes plausible open
straight vectors; it does not provide the missing positive electrical ownership.

The style observations can support a future source-grounded rule when a source
actually defines differing styles or layers. They do not discriminate this
same-style probe. Any automated producer needs independently measured positives
and negatives before its output can satisfy this gate.

## Executable acceptance status

The disconnected annotated positive control and byte-identical-input boundary
test pass. The unsatisfied zero-membership check is a strict expected failure,
limited to a dedicated `UnclassifiedBranchMembership` exception. Extraction,
binding, classification/source-ID and fixture assertions use ordinary failures;
they cannot be hidden as the expected failure. A future implementation that
satisfies the membership check produces a strict XPASS, requiring review and
removal of the marker.

The expected failure records an open requirement. It is not a passing negative
acceptance case, a safe-to-merge implementation, or evidence that #76 is complete.
Existing lawful branch tests and the #322 fallback regression remain required.

Private raw and minimal-help counts, source-specific architecture spot checks,
local semantic-coverage measurements and the real-panel acceptance gate remain
outstanding. No private result or before/after classification count is claimed.
