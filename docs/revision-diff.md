# Canonical revision diff

`oabm.qa.build_revision_diff(base, target)` compares two explicitly
comparable `BuildingModel` snapshots (#274, under #235) and reports what a
plan revision actually changed — so a revision stops looking like a new job
with every object brand new.

## API

```python
from oabm.qa import build_revision_diff

diff = build_revision_diff(base, target)
diff.to_json()                       # portable, deterministic
RevisionDiff.from_dict(document)     # strict decode
```

## Comparability is required, never guessed

The supported scope is two snapshots of the same model lineage: same
`model_id`, same coordinate frame and units, same schema version. Any other
combination is a refusal naming the reason — a different identity, a
different frame, a mismatched version (migrate both snapshots first). There
is no nearest-geometry matching, no ID mapping, and no fallback that reports
everything as added or removed. Cross-source ID reconciliation stays out of
scope under #235.

## What the report carries

- `added` / `removed` records carry the full canonical document of the
  object; `changed` records carry explicit field paths with before/after
  values, classified as `geometry`, `product`, `graph` (electrical
  connectivity and hosts), `provenance`, or `attribute`, so a reviewer can
  tell a moved wall from a swapped product from a rewired circuit.
- Objects match by stable canonical ID and collection. Reordering an entity
  collection is not a change; order inside geometry (a wall centerline) is.
  Provenance record order inside an entity is normalized, values verbatim.
- Signed quantity deltas come from the real `extract_quantities` run on
  both snapshots, matched on complete item identity (category, type,
  variant, unit). A size change that re-keys the variant reports the old
  line removed and the new line added — auditable additions and removals,
  never a silent in-place mutation. Unchanged scope emits no line; one-side
  lines are explicit `added`/`removed` with nulls on the absent side.
  Nothing prices anything.

## Purity and determinism

Neither snapshot is mutated. Item and delta identifiers are stable
(`revision:changed:<collection>:<id>`,
`revision:quantity:<category>:<type>:<digest>`), records are sorted, and
identical snapshot pairs produce byte-identical JSON regardless of input
order. Strict decoding rejects unknown keys, wrong versions/kinds, counts
that disagree with the records, and non-finite quantities.

## Limits

No PDF registration or cross-source ID reconciliation, no priced change
orders, no OA writes, no production revision viewer, and no claim that the
diff captures intent — it reports canonical object and quantity movement.
#235 and #227 stay open.
