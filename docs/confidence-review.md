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
| `unresolved_selection` | a box/raceway (and support) selection outcome whose `status` is `unresolved`; the outcome's own `reason_codes` appear in the detail |
| `quantity_warning_<code>` | an actual warning from the real `extract_quantities` run, attached to each source entity the warning names. Warning identity normalizes source ids and includes the message digest, so distinct warnings over the same code and sources never collide, and input order cannot change the output |

Item ids are stable (`review:entity:<id>`,
`review:warning:<code>:<source ids>:<message digest>`), and items are sorted by id, so the
same model always yields byte-identical JSON regardless of input order.
The manifest retains the model's actual confidence and provenance values
verbatim — source kind/ID/element/page, field scopes and derivation
classes. Nothing is relabeled as observed, and no confidence is ever
raised to pass the threshold.

## Crop and evidence references

Evidence is typed: an `EvidenceReference` binds a caller artifact (a page
crop, a scan excerpt) to one actual provenance record of a current entity.

```python
from oabm.qa import build_confidence_review
from oabm.qa.confidence_review import EvidenceReference, fingerprint_provenance

record = model.walls[0].provenance[0]
reference = EvidenceReference(
    ref_id="crop:page-2-a",
    entity_id=model.walls[0].id,
    source_kind=record.source_kind,
    source_id=record.source_id,
    page=record.page,
    fingerprint=fingerprint_provenance(record, entity_id=model.walls[0].id),
)
manifest = build_confidence_review(model, config, evidence=[reference])
```

At build time every reference is checked against the model's current
state: the entity must exist, the named source must be one that entity
actually carries, the page must match, and the record's content
fingerprint must still equal the one the reference carries — a record
edited or replaced after the reference was made fails as stale, which is
what makes stale evidence detectable without contacting any store.
Reference ids must be unique. Entities the caller did not name carry no
crop references: missing evidence stays missing, never invented. No
private plan is read and no external service is called.

The fingerprint covers every identifying field of the bound record
(kind, id, element, page, method, confidence, derivation, scopes and complete attributes) plus the canonical owner entity ID, so
bindings are exact, and the bound values and owner travel on the item where a
reviewer can audit them. Construction and portable decoding recompute each digest
from the retained original facts; a valid-length wrong digest still refuses.
Warning evidence selects the exact fingerprint-matching record, including when
several records share source and page. Scope qualifier order is normalized as a
set; arbitrary attribute arrays keep their order. Confidence normalizes to a
finite float without rounding, consistently before hashing and decoding.

This is logical content/owner binding, not authentication of crop bytes or a
remote source. No crop is fetched or decoded. An entirely fabricated document
with all matching hashes cannot be authenticated by this offline API. Manifest
facts are detached and nested values are immutable; exported dictionaries are
independent copies. Repeating a reference ID across entity/warning items is
allowed only with exactly the same owner and binding. Entity items always name
exactly their own canonical ID; genuine model-level warnings keep empty IDs.

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

## Opt-in physical-route review

For the bounded trusted-geometry viewer slice, call
`build_confidence_review(model, config, physical_routes=True)`. The default
still refuses physical routes. The opt-in retains every canonical collection
and reviews the same confidence, provenance, missing-source and unresolved
selection facts, including mixed legacy/physical route models.

If any physical route is present, `extract_quantities` is unsupported for the
whole model. The review does **not** remove physical routes, alter references,
or fabricate a partial takeoff. Instead it emits
`review:warning:physical_route_quantities_unsupported`, with reason
`physical_route_quantities_unsupported`, naming the canonical physical route
IDs and retaining their provenance and supplied evidence. Its detail explicitly
states that the entire quantity pass, including legacy routes and other
objects, was not evaluated. This is a capability limitation, not a
`quantity_warning_*` result from a takeoff. Models without physical routes
retain the existing quantity warning pass and output bytes.

Populated building assemblies remain unsupported. Physical-route fitting
references remain forbidden by canonical validation. Display does not establish live obstacle clearance,
verification status, code compliance, or engineering approval.
