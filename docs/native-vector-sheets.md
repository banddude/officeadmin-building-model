# Native vector views on shared company sheets

`oabm.drawings.native_sheet_set` joins a completed native delivery to the existing
shared company renderer. There is no second hand-authored geometry model, image
paste or post-export drawing composer. The same company SVG/PDF assembly call
owns all ordered drawing sheets. Private models, customer details, logos, inputs
and attempts belong outside the public checkout.

```sh
PYTHONPATH=src python -m oabm.drawings.native_sheet_set --check /private/native-sheets.json
PYTHONPATH=src python -m oabm.drawings.native_sheet_set /private/native-sheets.json /private/fresh-attempt
```

The optional installed entry point is `oabm-native-review-sheets`. The existing
PDF extra, native Cairo, Fontconfig and readable fonts are required by this
adapter even for SVG, because it verifies the original converter's actual font
fallback. It never installs dependencies, fonts or runtimes.

A request has exact keys:

```json
{"version":"native-review-sheet-set-v1", "profile":"company.json", "job":"job.json",
 "native_delivery":"../native-attempt",
 "sheets":[{"sheet_id":"A1", "views":[{"view_id":"plan","origin_mm":[15,25]}],
             "body":"notes.svg"}]}
```

Paths resolve beside the request. The ordered sheet owners must exactly match
the shared job. Every sheet needs native views or a populated supplied body.
`body` may be null, or an existing shared primitive `g` subtree, for source-backed
notes/schedules or another supported producer's body. Its original safe grammar
is unchanged. The source-view scale must agree with the title rail, or the rail
must explicitly say `AS NOTED`. Views are never automatically fitted or shrunk.

`native_view_asset(delivery, view_id, origin_mm=...)` verifies the completed
native receipt, detached SVG hash and source-preservation proof when an existing
IFC was consumed. The coordinator verifies every retained native file and refuses
symlinks/escaping paths, then retains exact snapshots. Receipts are hashes, not
signatures or authenticated history; preserve exclusive ownership during builds.

`create_company_sheet_set(..., vectors={sheet_id: (asset, ...)})` uses the same
shared frame as every other consumer. The typed component channel accepts a
bounded, self-contained native SVG vocabulary: paths/curves, polygons/holes,
markers/patterns, simple static filters, text and transforms. Scripts, images,
links, external resources, CSS imports, animation, unknown elements/declarations,
root viewport transforms and relative/CSS-wide length/font forms refuse. This
does not relax ordinary shared body validation or enable arbitrary web content.
Presentation values cannot contain CSS comment boundaries or consume following
declarations through malformed strings/functions. Ordinary parsed stylesheet
rule/declaration comments remain supported.

Static source CSS is resolved against the original tree before IDs/classes are
changed. Per-node styles preserve the cascade and inheritance; IDs and local
resource references are namespaced per component. Native path/curve/transform
strings and physical viewport remain unchanged. The original native viewport's
clipping behavior is retained; this is not a promise that the native producer
had no clipped or colliding annotations. Component viewports must fit in the
shared drawing area and cannot overlap one another.

Fontconfig and Cairo can resolve a missing source family differently. A tiny
local SVG probe identifies the original converter's embedded PostScript face;
the adapter requires the same readable measurement face and converter identity.
The actual family/weight, glyph coverage and final embedded PDF fonts are
verified, without downloading or silently replacing fonts. Existing shared
text-body family limits remain unchanged. Native source styling beyond the
supported computed-value boundary requires an explicit adapter, not omission.
Rendered native text requires an explicit inherited source font family; the
converter's implicit default is not replaced by a company font. Validation and
font/PDF accounting include nested-span tails in their parent element's face,
including parents that have no leading text.

The delivery retains native IFC/SVG/resources and immutable inputs under
`native/`, the shared packet under `company/delivery/`, body inputs, a portable
request and `NATIVE-SHEET-MANIFEST.json`. The shared manifest additionally retains
native SVG bytes, component placement/source maps and font resolution. Rebuild
the shared delivery with its existing verified rebuild command; rerun the full
native-sheet assembly from `inputs/portable.json` into a different fresh attempt.
No new native execution occurs during that sheet-only rerun.

The synthetic executable proof is
`examples/native_source_review_sheets.py`. Tests compare original/placed vector
pixels including compound holes, curves, dashed lines and text, exercise vector
PDF and portable rebuilds, and reject receipt, scale, input-coherence, resource
and presentation failures. Actual pinned native runtime tests remain separate
from skipped optional tests. None of these checks establishes professional
drafting acceptance, engineering completeness or agency approval.
