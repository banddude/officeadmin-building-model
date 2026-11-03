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

## Opt-in scope plan connection (bounded #231 slice)

`oabm.pipeline.scope_review_plan.generate_scope_review_plan(model, config,
policy=policy, ledger=None)` consumes a `ReviewDrawingConfig` and the same explicit
`ScopePolicy`. A supplied saved `ScopeLedger` must validate against the full model
and policy before drawing. No schema, importer or default drawing behavior changes.
The source model is never rewritten; callers retain their original source bytes.
Nested device/equipment `source_scope` attributes are refused, including AI payloads:
reconcile them explicitly into supported source evidence before using this API.

The existing `generate_review_drawings` backend supplies actual plan geometry,
source index and panel schedules. Scope presentation adds canonical-ID-bound marks
at supplied poses only where backend geometry is present and the anchor is inside
the requested window. Omitted objects remain in the complete ledger and schedules;
`status['scope']['views']` lists marked and unmarked IDs. It does not move an object
into view or infer its level/height. Underlying drawing-set geometry is unchanged.

Visible marks use shape, letters and color: double square **N** for installation,
gray square **E** for retained context, crossed square **D** with real dashed
geometry strokes for demolition, dashed square **R** for relocation, and heavy
diamond **?** for unresolved/conflicting scope. The footer shows all five classes
and complete-model counts. Relocated items show only their supplied position;
origin/destination geometry is explicitly unavailable. No arrows are invented.
The two existing full scope schedules accompany the plans in both drawing JSON
and a readable wrapped vector table. IDs remain literal in plan labels and table
cells, and are machine-readable in SVG `data-source-ids`, ledger and drawing JSON.
Symbols are annotations, not physical extents. Crowded source geometry can overlap
annotations; this bounded review is not automatic construction-sheet composition.

```python
from oabm.pipeline.scope_review_plan import (
    generate_scope_review_plan, render_scope_review_artifacts,
)
payloads, status = generate_scope_review_plan(model, config, policy=policy)
# payloads: drawing_set, panel_facts, review_plan:<id>, scope_ledger, scope_schedule
# Explicit optional CairoSVG runtime; no installation or conversion on import:
rendered = render_scope_review_artifacts(payloads)
# rendered keys: review_plan:<id>:pdf/:png and scope_schedule:pdf/:png
```

Limits: 200 devices/equipment, 16 requested plans, 32–200 pixels/metre and 4096
pixels per plan axis before the legend. The existing backend's unsupported-model
refusals still apply. Circuit/raceway scope, safe-off, pricing, topology, relocation
pairs, tie-ins, engineering and permit readiness are outside this connection.
Issue #231 remains open for its broader requirements.

Cloud acceptance commands (do not run on the source-only laptop):

```sh
python -m pytest tests/test_scope_review_plan.py tests/test_scope_ledger.py tests/test_review_electrical_plans.py
python examples/scope_review_plan.py /tmp/fresh-scope-review
```

The dedicated example uses six synthetic canonical objects covering all five
classes, includes retained equipment, and creates matching SVG/PDF/PNG plans and
full schedules with a hash manifest. Its revised model changes the same removed
canonical ID to retained and regenerates SVG/PDF/PNG, ledger and schedule to demonstrate
both visible and count changes. Focused tests cover exact supplied XY anchors,
unchanged backend geometry/default outputs/source bytes, stale model/policy
rejection, unresolved/conflicting evidence, incompatible AI fields, omitted
geometry coverage, actual PDF text and PNG pixels. Runtime and independent visual
acceptance must run in the parent cloud environment before publication.
