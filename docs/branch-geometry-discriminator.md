# Source-bound branch geometry checks (#76)

`oabm.importers.pdf_electrical.branch_geometry.discriminate_branch_geometry`
is an opt-in, read-only boundary for the integration owner. It consumes the
existing `PdfElectricalDocument`, actual arrowhead/eligible-vector IDs from the
recognition lane, owned symbol-outline IDs, and the architecture lane's existing
`SourceWallEvidence` plus its actual `PdfDocumentObservation` extraction.
It creates no canonical objects and changes no default importer behavior.

The canonical `ElectricalDevice`, `ElectricalEquipment`, `Port`, `Circuit`,
`Route` and `Conductor` contracts remain authoritative. Unknown assignments stay
diagnostics: a geometry result cannot supply missing source/load ports, circuit
labels, panel identity, loads, ratings, conductor design, routes or lengths.

## Decision contract

The traversal follows endpoint contacts using the existing electrical geometry
helpers, without any vector-count or reach limit. A spatial bounding-box sweep
narrows candidate comparisons; it does not decide semantic eligibility. Multiple
supplied arrowheads must reach the same connected component. All returned IDs
are actual source observations, sorted for deterministic output.

Architectural semantics come exclusively from `source_wall_evidence`; there is
no second wall classifier, layer guess, shape/degree threshold or manual wall-ID
whitelist. Interior crossings as well as endpoint contacts with classified wall
segments reject the entire component as `enters_classified_wall_chain`, retaining
chain IDs and conflicting source observation IDs. Even the wall's own absorbed
strokes remain conflicting evidence. Removing those strokes first can hide an
entirely coincident run and incorrectly approve the remainder.

An entire classified segment coincident with one owned closed symbol edge is
exempted. Endpoint containment on the same edge is required; a wall merely
passing through a symbol midpoint does not receive that exemption. Ownership
must come from the existing recognition lane, not nearest-symbol association.

Binding requires the electrical extraction's default `sha256:<digest>` identity,
the matching architecture byte digest/source identity, an existing page,
actual architecture observation IDs, displayed page dimensions/coordinate space,
and a drawing frame within that displayed page. Custom logical electrical source
IDs are deliberately refused here: matching logical names alone cannot prove a
PDF revision. Callers with custom IDs must retain/re-extract the byte-bound source
rather than manufacture a matching ID.

`eligible` means only that this boundary found no supported semantic rejection.
It is not observed connectivity or a safe-to-materialize instruction.
`architecture_evidence_status` has exactly this vocabulary:

| Value | Supplied evidence |
| --- | --- |
| `missing_handoff` | No `SourceWallEvidence` was supplied. |
| `supplied_empty` | A validated handoff was supplied, but its `chains` tuple is empty. |
| `supplied_positive` | A validated handoff carries at least one positively classified wall chain. |

None proves full-sheet or branch-local classification coverage. In particular,
`supplied_empty` does not establish an architecture-free sheet, and
`supplied_positive` may coexist with unclassified architecture elsewhere.
Positive evidence can produce either an eligible run away from its classified
walls or an unresolved run contacting them. A missing or empty handoff can leave
unclassified wall-like linework eligible at this geometry boundary; consumers
must retain that uncertainty and the existing materialization checks.

Migration from PR #306: the former `applied` value is split into
`supplied_empty` and `supplied_positive`; the former
`unavailable_unclassified_architecture_not_excluded` value becomes
`missing_handoff`. These are diagnostic vocabulary changes only. Traversal
`status` (`eligible`/`unresolved`), reason codes, component membership and source
binding rules do not change. Consumers must not treat any evidence-status value
as permission to materialize a circuit or as completion of #76/#72.

## Integration instructions

1. Retain the existing candidate-vector and recognized-symbol ownership filters.
   Supply those IDs, one arrow seed (or demonstrably common seeds), the original
   byte-bound electrical extraction, and the corresponding architecture extraction
   and `SourceWallEvidence` from the same source/page.
2. Use the returned complete `vector_ids` when assessing load/annotation contact;
   do not restrict association to the first arrow-adjacent segment. This preserves
   long and densely segmented runs.
3. If unresolved, keep the component unresolved, recording `reason_code`, source,
   page, vector IDs, chain IDs and wall source IDs. Create no circuit, source/load
   ports or membership for that component; do not rescue it via direct-tag or
   topology passes.
4. If eligible, still run the existing enclosure-edge/area guards and every
   device, annotation, recognized-panel, schedule, ambiguous-annotation and
   conflicting-intent check. This module does not replace those checks.
5. Only the existing materialization owner may emit canonical `Circuit` and
   `Port` instances with real references and unchanged source provenance.

The module imports existing private geometry helpers from `importer.py`. Wire
it through a local import after importer definitions have loaded (or move the
shared primitives in a separately coordinated refactor); avoid a top-level
circular import. No importer/schedule/schema edits are included in this PR.

## Acceptance status

| #76 criterion | Evidence and remaining scope |
| --- | --- |
| Crossing/touching architecture refuses | New source-PDF open-chain interior-crossing and absorbed-wall tests. The separate module rejects; importer call-site wiring remains with the integrator. Unclassified architecture remains a gap. |
| Long run >=300 pt resolves | Generated 350 pt/six-segment source PDF resolves on current baseline importer to circuit 1 and two ports; module retains its entire run. No new long-run fix is claimed. |
| Many compact segments resolve | 65 segments over 80 pt, plus 350 segments over 350 pt, resolve through the actual importer and module without count caps. |
| Existing enclosure guard preserved | No importer changes; existing guard suite remains required. |
| Non-device negative probes | Independently generated rectangle/triangle/section-like marks and dimension-style arrow yield no devices, circuits or ports. |
| Real source PDFs, no expected JSON | New tests generate PDFs with pypdf, invoke both `extract_pdf` boundaries and `source_wall_evidence`, and assert no paired `.expected.json`. |
| Full suite | See exact PR validation output. |
| Private raw/minimal-help counts, architecture spot checks | Private raw evaluation is separate from public fixtures. Explicit unit-preserving page tiles are an additional diagnostic mode, not a validated minimal-help registration. No integrated after-count or architecture-rejected-component count is claimed before wiring. |

Other unresolved acceptance includes real-panel gating, complete unclassified
architecture coverage, private source review and a fully integrated rerun.
Neither #76 nor #72 is closed by this bounded module. No load sizing or
code-compliance certification is performed.
