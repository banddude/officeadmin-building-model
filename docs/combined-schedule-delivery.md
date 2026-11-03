# Combined inventories and panel facts

`create_combined_schedule_sheet_set` in `oabm.drawings.schedule_set` is the
explicit whole schedule delivery entrypoint. It takes the canonical model,
shared company profile, one-template shared job, and layout settings. Use
`version: "combined-schedule-sheet-set-v1"` with the same `layout` shape as the
panel sheet settings; see `contracts/combined-schedule-sheet-settings-v1.schema.json`.
The job must request both `svg` and `pdf`. The CLI is:

```sh
PYTHONPATH=src python -m oabm.drawings.schedule_set MODEL PROFILE JOB SETTINGS FRESH_OUTPUT --combined
```

One captured source supplies both existing generators: `generate_schedules`
provides rooms, openings, equipment, devices, circuits and supported physical
inventories/selection outcomes; `generate_panel_schedule_report` and
`panel_report_to_drawing_schedules` provide every panel's supplied circuit facts
and connected-load completeness. The shared fact-ledger paginator preserves
schedule IDs, row source IDs, complete cells, repeated headers and readable
paper-sized text; the existing company-sheet renderer creates SVG and combined
vector PDF. Oversized content refuses delivery rather than truncating rows.
The panel-only entrypoint and its drafted-card default remain compatible.
The existing versioned review-plan and saved-sheet pipeline remains panel-only
and byte-compatible; its strict panel-fact coherence guard is unchanged. Use
the explicit combined entrypoint for ordinary inventories and panel schedules
together. Default drawings retain their existing schedule set, with invalid
references now refused.

The retained `schedule-source/model.json` is byte-for-byte the input. Editable
`drawing-schedules.json`, `panel-facts.json`, individual page drawing JSON and
SVG bodies accompany the final sheets. `SCHEDULE-MANIFEST.json` binds all files
by exact byte count/hash, the source bytes by SHA256, and the panel report by
its canonical fingerprint. Its row receipts bind each logical row to its
physical sheet, complete cells, wrapped cells and primitive IDs. `row_sources`
retains full canonical owner records, including confidence, original attributes
and field-qualified provenance; the ordinary DrawingSet source index is only a
legacy projection of that evidence. Units and references are validated through
the canonical contract before delivery. The source is never edited by delivery.

`verify_combined_schedule_delivery(path, model_path=current_model_path)` checks
the receipts and regenerates both editable schedules and physical pagination
from retained canonical source and layout inputs. It compares complete row,
page, primitive, wrapped-cell and panel-total receipts, retained page drawings
and SVG bodies, and rendered SVG schedule geometry. Architecture-only and
assembly inventories retain their complete canonical owner records. Original
external logo paths are not reopened during replay. Stale report/source
combinations, altered files and modified manifest mappings are refused. The optional
current source path additionally requires exact source bytes. Regenerate into a
fresh output attempt after edits; prior attempts are not overwritten. This is
an integrity check, not a signature or independent visual acceptance.

The circuit inventory and panel circuit tables intentionally show the same
canonical circuits in different views. Do not add their row counts as quantities.
Box selection outcomes are decision records, not extra physical boxes. The
physical box inventory counts selected boxes once; unresolved decisions retain
their reasons and do not acquire invented boxes. Empty panels remain explicit.
Unsupported panel-source owners remain unassigned fact rows. Populated
site/service inventory is explicitly refused rather than silently dropped.
Other canonical versions and physical inventories retain their existing
consumer support boundaries; this change adds no new engineering algorithms.

Unknown loads stay unknown. Duplicate opaque printed labels and canonical
circuit IDs remain visible. Known connected VA is arithmetic on supplied facts,
not demand, breaker selection, phase assignment, capacity or code verification.
No NEC calculations, voltage drop/fault-current analysis, Title 24, permit
readiness or OA production document integration is claimed. Historical #282 is
the panel-facts boundary; broader #233 remains open.

## Focused cloud acceptance (not run on the source-only laptop)

In the prepared cloud environment with the repository dependencies available:

```sh
PYTHONPATH=src pytest -q tests/test_combined_schedule_delivery.py tests/test_schedule_sheet_set.py tests/test_drafted_panel_schedules.py tests/test_panel_schedule_report.py tests/test_assembly_schedules.py tests/test_review_electrical_plans.py tests/test_review_drawings_integration.py
PYTHONPATH=src python examples/combined_schedule_delivery.py /tmp/combined-schedule-proof
```

The fictional building includes a room and opening, two devices, two panels,
one selected box and an unresolved selection. Panel A contains an explicitly
supplied 1,800-VA circuit and an unknown-load circuit with the same printed
label. Both panel reports and all populated ordinary schedules must appear.
The `after` revision changes only a device name and the supplied circuit load
to 2,400 VA. Stable schedule/row IDs and unrelated rows must remain unchanged;
the ordinary circuit row, panel fact row and known subtotal must update.
Tests also exercise malformed references, units, stale reports, changed files,
empty panels, assembly source indexing and unsupported site refusal.

Render every delivered PDF page for independent visual review using the actual
PDF runtime (run in cloud after the example, not on the source-only laptop):

```sh
python - <<'PY'
from pathlib import Path
import hashlib
import json
import pypdfium2
root = Path('/tmp/combined-schedule-proof')
for revision in ('before', 'after'):
    delivery = root / revision / 'output/delivery'
    manifest = json.loads((delivery / 'SCHEDULE-MANIFEST.json').read_text())
    images = root / revision / 'page-images'
    images.mkdir()
    receipts = []
    with pypdfium2.PdfDocument(delivery / 'sheet-set.pdf') as pdf:
        assert len(pdf) == len(manifest['sheet_order'])
        for number, identity in enumerate(manifest['sheet_order']):
            path = images / f'{number + 1:03d}.png'
            pdf[number].render(scale=1.5).to_pil().convert('RGB').save(path)
            receipts.append({'sheet': identity, 'image': path.name,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    (images / 'receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
PY
```

Review all page images for legibility, complete headers, row wrapping, page
continuations and frame clearance. The built-in SVG/PDF glyph readback checks
content preservation; they do not replace the independent page-image review.
