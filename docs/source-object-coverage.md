# Source-object coverage gate

`oabm.reports.source_coverage` is an opt-in, read-only accounting check. The
caller supplies an independently prepared inventory of source object IDs and a
SHA-256 of the exact source version, along with explicit mappings to canonical
entity IDs or documented omissions. No files are read or executed. The existing
scope ledger instead reports scope facts on canonical electrical entities; the
canonical handoff validator checks the model, not source-object completeness.

```python
from oabm.reports import inventory_fingerprint, model_fingerprint, source_coverage

source_sha256 = 'a' * 64  # Replace with the digest of the exact source bytes.
ids = ['A', 'B', 'C']
mapping = {
    'inventory_sha256': inventory_fingerprint(source_sha256, ids),
    'model_sha256': model_fingerprint(model),
    'entries': [
        {'source_id': 'A', 'canonical_ids': ['canonical:one']},
        {'source_id': 'B', 'canonical_ids': ['canonical:one']},
        {'source_id': 'C', 'omission_reason': 'Outside declared deliverable scope'},
    ],
}
report = source_coverage(model, source_sha256=source_sha256, object_ids=ids,
                         mapping=mapping, strict=True)
```

Missing entries are unresolved in a non-strict report and fail a strict handoff.
Duplicate source IDs or mapping entries, unknown source or canonical IDs, empty
canonical target sets, and blank omission reasons fail. Many sources may map to
the same canonical ID when explicitly declared. The binding includes a sorted
inventory and source hash, and the existing normalized model-report fingerprint;
a change to either invalidates the mapping. Results are deterministic by source
ID. Regenerate against both current inputs before each handoff; this API does not
provide an authenticated or independently verifiable saved-report format.

The caller must independently compute the source digest and establish that the
inventory is complete for its declared scope. A matching hash does not prove
that claim or authenticate the source. Coverage is accounting only: it does not
prove geometric equivalence, field accuracy, completeness beyond the inventory,
or engineering acceptance. This gate does not alter default exporters or models.
