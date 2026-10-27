# One-command canonical model review delivery

Use the existing plan and GLB exporters together from one validated canonical
model, without manually chaining the bend gate and exports:

```sh
PYTHONPATH=src python -m oabm.exports.model_delivery \
  fixtures/model_delivery/v1/model.json \
  fixtures/model_delivery/v1/options.json \
  /tmp/synthetic-model-attempt
```

The destination must not exist. This command produces a `delivery/` directory
containing requested plan SVGs and/or the GLB preview, the canonical model,
exact input snapshots, the bend report when requested, a readiness notice and
`MANIFEST.json`. It never fetches source evidence or changes the model. Existing
exporter APIs and their output compatibility remain unchanged.
CLI exit 0 means a delivery was published, including `report` mode with
`needs_review`; it does not mean engineering readiness. Exit 1 means failure.

Python callers use the same orchestration, not a command wrapper:

```python
from oabm.exports.model_delivery import deliver_model

path = deliver_model("model.json", "options.json", output_directory="fresh-attempt")
```

The versioned options JSON has exactly these fields:

```json
{
  "version": "model-delivery-v1",
  "plans": [{
    "id": "plan:ground",
    "level_id": "level:ground",
    "cut_height_m": 1.2,
    "view_depth_below_m": 0.25,
    "view_depth_above_m": 0.25,
    "scale": 50
  }],
  "preview_glb": true,
  "bend_policy": "require",
  "radii_m": {"route:feed": 0.3}
}
```

Supply real canonical level/route IDs. All distances/radii use metres and plan
scale is the denominator, e.g. `50` for 1:50. Radii are **caller design intent**,
not inferred manufacturer facts. Up to 16 plans are supported. An empty plans
list requests no plans; `preview_glb: false` requests no GLB. At least one
output is required. Plan names map to safe ordinal filenames in stable ID order;
the manifest records each view ID and filename. Bounds and visibility use the
existing PlanSpec defaults; this initial portable interface deliberately does
not expose every exporter option.

Plan generation uses the existing dimension/label collision suppression and
matching paper text height. `drawing-set.json` retains source references and
metadata including suppressed labels/dimensions. SVGs are inspection views,
not composed physical sheets, checked note layouts or permit documents. They
are not stamped with the package bend-check status; keep the notice, report and
manifest with the artifacts. The GLB uses existing default geometry, including
sharp route vertices, without a new rendering engine or invented bend geometry.

## Honest bend-check policy

- `require`: run the [bend preflight](route-bend-preflight.md). Any unresolved
  diagnostic stops publication before exports. The retained report explains why.
- `report`: run the same checks and export review outputs even when unresolved.
  The manifest and `READ-ME.txt` say `needs_review`; no diagnostic is discarded.
- `skip`: explicitly do not evaluate bends. `radii_m` must be empty so assumptions
  cannot be silently ignored. Status is `not_evaluated`, never a passing check.

The gate supports **interior sharp-vertex turns per route**. A valid consolidated
corner may assign its elbow to a straight trunk endpoint, producing
`elbow_off_turn`. That means the cross-route junction is outside this check's
scope, not that the canonical topology is invalid. `report` allows the review
outputs with that limitation visible; `skip` supports unrelated architectural
work. `require` refuses such an unresolved case. Do not remove a valid fitting
to obtain a passing result. There is no automatic junction-fit certification.
Even `passed_bounded_checks` is only a necessary geometric condition, not
survey, manufacturer, installation, code or engineering approval.

## Publication and rebuild evidence

Inputs are read once and retained byte-for-byte. The same reconstructed model
object feeds preflight, plans and GLB. The manifest records:

- each input snapshot and generated file's relative path, byte count and SHA-256;
- the compact canonical serialization's SHA-256 and canonical model ID;
- requested policies, effective plan settings, presentation settings and limitations;
- Python/platform and package versions, plus hashes of the actual installed engine
  Python source files and an aggregate source fingerprint.

`inputs/options.json` retains the exact request, including radius assumptions.
Legacy GLB schemas do not all contain an embedded model digest; the package
manifest binds their output bytes to the retained model without changing those
legacy exporter bytes. Rebuild with `inputs/model.json` and `inputs/options.json`
using the recorded engine sources/runtime. Source hashes identify the required
engine version; this package does not bundle the Python interpreter or engine
source distribution. This is a deterministic review delivery, not a standalone
hermetic runtime or a release of all original evidence files.

The command exclusively reserves a fresh attempt directory. It writes into
`.pending/`, verifies generated files still match their recorded hashes and the
model has not changed, then renames the whole directory to `delivery/`. The
manifest lists every input/output file except itself. There are no timestamps
or destination paths in the manifest. With identical inputs, engine sources and
runtime, complete rebuilds are byte-identical.

A failure retains `.pending/` and `failure.json` for diagnosis, with no published
`delivery/`. Existing attempts, successful or failed, cannot be reused or
replaced; retry at a fresh path. No stale plan/GLB from an old attempt can be
mixed into a later delivery. Invalid model/options or unknown plan levels fail
before reserving a directory. Publication uses a local directory rename; it does
not claim power-loss durability, protection against concurrent external edits
after publication, or distribution/upload to any external system. Verify recorded
hashes before consuming a delivery that may have been edited.

## Executable synthetic demonstration

```sh
PYTHONPATH=src python examples/model_delivery.py /tmp/synthetic-delivery-demo
```

This builds twice at fresh paths, verifies every recorded file hash and compares
all package bytes including the manifest. It produces an SVG and GLB for cloud
visual inspection without running a local renderer. Tests cover required/report/
skip policies, real consolidation endpoint elbows, partial-export failures,
existing destinations, changed input source files and changed pending artifacts.
