# Offline revision change register

`oabm-revision-register before.json after.json fresh-output-directory` produces
an offline `index.html`, `changes.csv`, `quantity-deltas.csv` and `register.json`.
The complete canonical snapshots must share model ID, schema version and the
complete coordinate frame. No source-plan matching or identity inference occurs.

The callable API in `oabm.reports.revision_register` is
`generate_revision_register(before, after)`, followed by
`write_revision_register(report, fresh_directory)`. The immutable report exposes
`to_json()`, `to_dict()` and `files()`. `validate_revision_register(payload)`
rebuilds every reported value from the retained normalized canonical snapshots;
changed hashes, fields, quantities or measurement evidence are refused.

The HTML lists every changed canonical object with expandable field details and
the complete added/removed document. The changes CSV has one row per changed
field, repeating its stable change/object ID; counting CSV rows is not counting
changed objects. The quantity CSV has one row per complete canonical quantity
identity. JSON `null` is retained literally on absent sides and absent deltas;
an added or removed product variant is never silently converted into a changed
quantity against zero. Equal-length moved paths remain geometry changes.

Physical route measurements retain exact straight ticks and rational π ticks.
They explain the existing canonical quantity deltas and must not be added to
them again. Legacy route quantities remain separate canonical variants. These
are geometric quantities, not stock orders, prices or engineering verification.
The physical table's canonical status/classes come from the same canonical diff
as the object register. Separately labeled ordered-snapshot status and comparison
categories retain the existing physical comparison's evidence. Reordering equal
provenance facts changes source hashes and ordered snapshot status but does not
become a canonical semantic revision. The underlying comparison is unchanged.

HTML contains escaped source text, inline CSS and relative local download links;
it needs no network or scripts. CSV text uses the existing spreadsheet formula
guard. Each source uses the existing 32 MiB/depth/node admission; the source-bound
JSON also uses that admission. Each HTML/CSV presentation is limited to 16 MiB
and the prepared bundle to 64 MiB. Exceeding a bound refuses the export rather
than truncating rows. All bytes are prepared and source-validated before an
exclusive fresh-directory reservation; existing paths and symlinks are refused.
No canonical geometry, quantities or source objects are modified.

This is the readable list section of #235. Plan identity reconciliation, 3D
revision presentation and priced change orders remain outside this exporter.
# Quantity aggregation dependency

The register consumes the accepted canonical revision engine directly. That
engine sums original measured contributions once, before provenance bucketing,
and refuses incompatible quantity ownership bases. The register does not add a
second aggregator or sum rounded evidence-bucket subtotals. Provenance-only
regrouping must not create quantity deltas; real fractional changes remain
visible across JSON, HTML and CSV without intermediate rounding.
