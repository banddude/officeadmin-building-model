# Explicit object scope ledger

The opt-in `oabm.reports.scope_ledger` report consumes the existing
`attributes.pdf_electrical` scope classification on canonical electrical devices
and equipment. It does not recognize printed markers again. Every object appears
once, identified by its canonical ID and collection/type. Original attributes,
marker and legend IDs, pages, confidence and owner-relative evidence remain in
an unchanged canonical `SourceSnapshot`. Model-wide evidence stays in the shared
`ModelSourceContext`; the source utilities and binding algorithm are the same as
[panel facts](panel-schedule-facts.md).

```python
from oabm.model import Provenance, RuleReference
from oabm.reports import ScopePolicy, generate_scope_ledger
from oabm.drawings.scope_ledger import scope_ledger_to_drawing_schedules

policy = ScopePolicy(
    version='1.0.0', mode='preserve_unresolved',
    basis=RuleReference(authority='caller', revision='r1', rule_id='scope-policy'),
    actor='explicit reviewer',
    provenance=(Provenance(source_kind='user', source_id='fixture:policy',
                           derivation='user'),),
)
ledger = generate_scope_ledger(model, policy=policy)
ledger.validate_against(model, policy)
schedules = scope_ledger_to_drawing_schedules(ledger)
```

There is no implicit policy. `preserve_unresolved` retains unknown scope.
`new_when_confirmed_unmarked` applies only when the source explicitly says
`scope_status='unresolved'`, `scope_reason='no_scope_marker'` and contains no
printed marker or marker/legend evidence. Missing classification, unsupported
statuses, undefined markers, conflicting legends and structurally malformed or
contradictory evidence never receive that fallback. Malformed facts remain in
the original snapshot; effective scope is unresolved with a diagnostic.
An eligible fallback is labeled `inferred_caller_policy`, retains its original
unresolved reason, and references the exact policy fingerprint. It is not an
observed legend conclusion. Original input provenance is retained separately;
qualifiers are never remapped to qualify the effective decision.

Effective confidence is bounded by source entity confidence and applicable
source scope evidence, model-wide electrical collection evidence and (for a
fallback) applicable policy mode/actor/rule evidence. Unrelated pose or coordinate
records retain their original meaning and do not qualify scope. If no policy
record covers its consumed decision fields, fallback confidence is zero rather
than invented certainty. A missing
classification remains unknown even when the canonical entity confidence is 1.

Groups are deterministic by collection/type and the five existing statuses:
new, relocated, existing_to_remain, removed and unresolved. Each group includes
its distinct sorted IDs and count, including empty status groups for listed
types. These are object counts. Relocation is not a purchased new product;
removal is visible separately. No labor, disposal, safe-off, procurement,
engineering approval, price or verified code claim is produced.

## Portable binding and views

The standalone policy and ledger version is `1.0.0`:
[policy schema](../contracts/scope-policy-v1.schema.json),
[ledger schema](../contracts/scope-ledger-v1.schema.json).
Readers reject unknown fields/versions, malformed typed arrays, nonfinite/bool
confidence, inconsistent summaries/decisions, duplicate identities and stale
snapshot/policy hashes. Constructors enforce the same typed shapes. Source and
policy snapshots are detached from caller dictionaries; serialization rechecks
nested state, so mutation cannot silently keep stale hashes.

The full model binding normalizes only unordered top-level entity collections
and typed provenance qualifier sets. Geometry, attributes arrays and ordered
provenance records remain ordered. Policy qualifier sets are similarly sorted.
These fingerprints establish content consistency, not external authentication.
`validate_against(model, policy)` compares a regenerated complete report to both
supplied sources; use it before consuming a saved report after revisions.
Valid unnamed models and all shipped canonical versions 1.0–1.3 are supported.
No canonical schema migration is needed for this derived report.

The explicit drawing adapter returns identified object rows and separate scope
count summaries. Missing original scope/page remains `unknown`. It does not
participate in default drawing generation. Importer, QA counts, quantities,
canonical serialization, IFC and GLB remain unchanged. Public tests edit a
synthetic PDF content stream and run `extract_pdf -> ElectricalPdfImporter ->
ledger -> portable reader -> drawing adapter`, including unmarked, undefined,
conflicting, removal and relocation cases. No prefilled expected-answer sidecar
or private plan is used.

This closes only the bounded ledger child #284. Existing-system modeling,
relocation/demo geometry, tie-ins, safe-off and production OA work on #231/#227
remain open. This schedule is not an OA-issued or delivered customer document.
