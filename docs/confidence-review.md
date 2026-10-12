# Confidence review manifest

`oabm.qa.build_confidence_review(model, config)` builds a deterministic,
read-only list of what a canonical model is unsure about (#273, under
#241). It exists so every consumer — takeoff review, revision diffs,
3D presentation — can ask one question the same way: *what should a human
look twice at?*

## API

```python
from oabm.qa import ReviewConfig, build_confidence_review

config = ReviewConfig(confidence_threshold=0.9)          # explicit, no default
manifest = build_confidence_review(model, config)
manifest.to_json()                                       # portable JSON
ConfidenceReviewManifest.from_dict(document)             # strict decode
```

- `ReviewConfig.confidence_threshold` is the only policy knob, supplied by
  the caller every time: a finite number in [0, 1]. Booleans, non-numbers,
  non-finite values and out-of-range values are rejected. There is no
  hidden threshold and the canonical schema does not change.
- `ReviewConfig.from_dict` / `ConfidenceReviewManifest.from_dict` are
  strict: unknown keys, wrong `version`/`kind` and invalid values fail, so
  documents written by one process decode exactly in another.

## What becomes an item

| Reason code | Meaning |
|---|---|
| `low_entity_confidence` | the entity's own `confidence` is below the threshold |
| `low_supporting_provenance_confidence` | a supporting provenance record's confidence is below the threshold, even when the entity's own confidence is high — a confident record never hides a weak source |
| `missing_provenance` | the entity carries no provenance records at all |
| `unresolved_selection` | a box/raceway (and, once merged, support) selection outcome whose `status` is `unresolved`; the outcome's own `reason_codes` appear in the detail |
| `quantity_warning_<code>` | an actual warning from the real `extract_quantities` run, attached to each source entity the warning names |

Item ids are stable (`review:entity:<id>`,
`review:warning:<code>:<source ids>`), and items are sorted by id, so the
same model always yields byte-identical JSON regardless of input order.
The manifest retains the model's actual confidence and provenance values
verbatim — source kind/ID/element/page, field scopes and derivation
classes. Nothing is relabeled as observed, and no confidence is ever
raised to pass the threshold.

## Crop and evidence references

An optional `evidence` mapping attaches caller-supplied crop or evidence
reference ids to entities: `evidence={"wall:x": ("crop:page-2-a",)}`.
References are validated against the model's current entity ids —
dangling (unknown), stale (removed in this model) and duplicate
references raise `ValueError`. Entities the caller did not name carry no
crop references: missing crops stay missing, never invented. No private
plan is read and no external service is called.

## Determinism and purity

Entity collections are discovered by reflecting over the model's own
dataclass fields, so collections main merges later (support hardware,
further typed physical objects) are reviewed without a change here.
The input model is only read: its serialized bytes are identical before
and after, and building the manifest twice gives identical bytes.

## Limits

This is a review surface, not a completeness claim: it does not implement
importer self-checks, crop extraction, price-at-risk, bid pricing, a
production viewer, or OA integration, and it does not detect every kind
of uncertainty — only the ones the canonical signals above can support.
The parents #241/#227 stay open for those.
