# Branch safety: source-bound wall evidence at circuit materialization

Bounded implementation under issue #276, related to #72 and #227. Parent #76
remains a hard gate on real panelboard recognition and stays open.

## What exists

The architecture lane owns the only wall classification:
`oabm.importers.pdf_architecture.sheet_wall_evidence`. The new producer
`oabm.importers.pdf_architecture.source_wall_evidence(document, page_number=,
confidence=, ...)` calls exactly that classifier, groups the classified
segments into connected chains, records whether each chain is open or closed
from pure segment topology (a chain with a free end is open; one where every
end is met by another classified segment is closed), and binds every claim to
the producing extraction: source id, content sha-256, page number, drawing
index and frame, meters-per-point, evidence kind, classifier name and version
(`pdf_architecture.sheet_wall_evidence` / `1`), the caller's stated
confidence, and every segment's source observation ids. Every referenced id
is verified against the page's actual observations before the handoff is
built; the types in `oabm.importers.pdf_wall_evidence` refuse segments outside
the claimed frame, non-hex fingerprints, duplicated chain ids and empty
evidence.

The electrical lane consumes the handoff read-only:
`ElectricalPdfImporter.import_document(..., wall_evidence=...)` (and
`import_pdf`, and the module-level `import_document`/`import_pdf`). At entry
the binding is verified: the handoff must carry the same source id or the
same content sha-256 as the electrical extraction (both lanes hash the same
PDF bytes), and its page must exist in the document. Stale source, foreign
source, or an out-of-range page is a hard `ElectricalPdfError` with the
offending values named.

At circuit materialization, both sinks that assemble branch runs (the
homerun-annotation pass and the panel-topology pass) consult
`_wall_chain_conflict`: if any vector of the run's own linework contacts a
positively classified wall chain, the run stays unresolved with
`reason_code`/`reason` `enters_classified_wall_chain`, naming the chain id,
the conflicting source evidence element ids and the handoff's content
sha-256. No circuit, ports or load membership is created from a rejected run.
Two contacts are not conflicts: the wall's own absorbed strokes (connectivity
assembly pulls them into the run; they are recognized as chain strokes and
skipped), and the run's own recognized device symbol outline (a branch
legitimately terminates at its device's mark). Shape, degree, vector counts
and reach are never consulted: the discriminator is the source evidence
alone, so a lawful multi-tap branch with the same tree topology as an open
wall chain resolves when it touches no classified geometry.

The model's `pdf_electrical` attributes always carry an explicit
`branch_wall_evidence` report: `applied` (with chain counts and rejected-run
counts) when a handoff was supplied, or `absent` with a note that running
without source classification leaves open architectural chains
indistinguishable from wiring (#76), so the limitation is stated in every
import instead of silently implying the gate is covered. The four embedded
import-byte baselines in `tests/test_pdf_electrical_importer_performance.py`
were updated deliberately for exactly this attribute and nothing else.

Generated test PDFs go through `extract_pdf` on both lanes; there are no
pre-extracted answer fixtures.

## Supported evidence scope

Same-source only: the handoff binds one page of one fingerprinted PDF.
Cross-source or cross-registration evidence is out of scope and refused.
Coordinates are displayed sheet points within the claimed drawing frame.

## Remaining gaps, stated plainly

- Unclassified architecture: geometry the classifier does not claim (for
  example single-line diagrams of walls, or evidence kinds this classifier
  does not emit) still cannot be separated from wiring.
- The chaining tolerance is a displayed-points constant tuned for generated
  paired-face drawings; real CAD sheets may need per-sheet calibration.
- Long-run recognition, the private pilot, and the full real-panel gate are
  untouched: #76 is NOT satisfied by this guard, and no real panelboard
  recognition is enabled by it.
