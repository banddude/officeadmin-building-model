# Synthetic companion example retention proposal

Source preparation only; do not interpret this as hosted/runtime acceptance.
The local execution hold and serial publication slot remain in force.

## Existing artifact route

The current CI command runs `examples/reference_production_set.py` against
`$RUNNER_TEMP/oabm-synthetic-review`. The existing checksum command recursively
finds files under `inputs`, `attempt`, `page-images` and `schedules`. The existing
artifact upload retains the complete review root for seven days. Its workflow,
runner, upload destination, permissions and action settings need no change.

## Prepared companion

`examples/utility_service_set.py FRESH_DIRECTORY` is independent of the reference
model and any private project. It prepares a fictional paper-scaled SVG source,
canonical 1.8 site/service model, supplied synthetic load source/profile and
baseline/field-edit requests. It then calls the real utility delivery API twice,
retaining each document PDF/page PNG, source bindings, inputs and receipts.

The model has no fabricated 3D equipment/ports. It explicitly retains unknown
utility/subpanel locations and equipment extents. The edited plan anchor changes
its geometric marker separation from 2 m to 1 m, updates the private plan lateral,
preserves canonical IDs and binds all three documents to the edited model.
Ratings, loads, names, geometry and filenames are fictional. It contains no
customer plan, crop, address, actual load calculation or selected private product.

The small fixture-admission test exercises only input preparation/decoding; it
does not add another full six-PDF generation to the unit suite. Real companion
outputs are produced by the explicit example command, once a runtime slot opens.

## Reference-only hook

After the existing reference production delivery completes in the script's
`__main__` path, call `utility_service_set.create_example` only for
`variant == "reference"`, with
`root / "inputs" / "model-utility-example"` as its fresh root. Keep the existing
printed primary delivery path. Do not put service objects into the reference
production model, change any default library `prepare()` behavior or alter its
existing sheet bodies.

The existing `find inputs ...` checksum command and whole-root artifact upload
cover the companion recursively, including its own file hashes and
all PNG pages. Its six PDFs are two separate three-document deliveries, not
additional pages in the reference production sheet set. Private project proof
stays elsewhere and is never part of this hook.

The hook runs only after primary generation succeeds and prints the primary
success path only after companion success. Companion failures propagate. The
existing upload is success-only: failed companion output is not retained by that
upload. Actual hosted runtime must still measure cost and inspect PDFs/images.
No workflow edit or new artifact destination is introduced.
