# Fixtures

Only synthetic or explicitly public/cleared fixtures belong here.

`model/v1/` contains the checked-in canonical contract examples:

1. `minimal-room.json`: tiny known-answer building model
2. `garage-route.json`: broad v1 contract coverage for building + electrical + route semantics

The garage-route fixture is a contract example, not the later convergence acceptance fixture.

`pdf_electrical/` contains source-observation fixtures for the electrical PDF
recognition lane:

1. `synthetic-sheet-e1.json`: panel, EVSE symbol, mounting note, and inferable circuit
2. `ambiguous-sheet-e1.json`: ambiguous symbol, conflicting mounting heights, and an incomplete circuit reference
3. `geometry-only-power-sheet-with-legend.pdf`: Slice 4 source-only acceptance PDF with six geometry-only field glyphs, three drawn legend prototypes and labels, and two unmatched glyphs that must remain unresolved. There is no paired expected-output artifact.
4. `two-page-sheet-local-legend.pdf`: Slice 4 regression PDF. Page 1 has the drawn legend and field glyphs; page 2 repeats only field glyph geometry and must not inherit page 1 legend types. There is no paired expected-output artifact.
5. `geometry-only-power-sheet-edge-legend.pdf`: Slice 7 source-only acceptance PDF. A `SYMBOLS` legend block sits at the sheet edge inside a page frame and title block, with a leader from the heading; six field symbols carry geometry only and must be typed from the detected block. There is no paired expected-output artifact.
6. `geometry-only-power-sheet-dense-legend.pdf`: Slice 7 source-only regression PDF with no legend heading. The importer must find the aligned three-row glyph/label table by density and repeated symbol geometry. There is no paired expected-output artifact.
7. `separate-sheet-explicit-legend-reference.pdf`: Slice 7 source-only three-page regression PDF. Page 1 carries `GENERAL NOTES AND LEGEND` on sheet `E-001`; pages 2 and 3 contain field glyphs and explicit references by sheet name and legend title. Cross-sheet typing is allowed only through those source references. There is no paired expected-output artifact.
8. `geometry-only-power-sheet-notes-column-legend.pdf`: Slice 9/12/13 source-only acceptance PDF with a right-hand notes column, separate bottom title block, KEY NOTES and GENERAL NOTES sections, an eight-row ruled `SYMBOL | FUNCTION` legend covering duplex/quad receptacles, data/combination outlets, power/data junction boxes, access control and CATV, plus two field instances per type at 1.0x and 0.8x (including axis mirroring), E/N status tags, bare +44\" height annotations and circuit-count numbers. Slice 13 extends the tele/data J-box label with a trailing explanatory sentence and adds `/Square` annotations carrying `CR` and `TV` codes beside field glyphs. There is no paired expected-output artifact.
9. Slice 10 rotated electrical fixtures: `geometry-only-power-sheet-notes-column-legend-rotate-{90,270}.pdf`, `geometry-only-power-sheet-edge-legend-rotate-{90,270}.pdf`, and `geometry-only-power-sheet-dense-legend-rotate-{90,270}.pdf`. Each stores the same synthetic content sideways with the matching page `/Rotate` flag so its displayed geometry is identical to the unrotated source. They are source input only and have no paired expected-output artifacts.
10. `geometry-only-power-sheet-inner-view-border-legend.pdf` and its `-rotate-270.pdf` copy: Slice 11 source-only regression sheets with a full sheet border drawn as four independent long rules plus a large closed inner drawing-view border. The notes-column `SYMBOL | FUNCTION` legend must be detected from the outer sheet frame in both displayed orientations. There is no paired expected-output artifact.
11. `geometry-only-power-sheet-real-cad-glyphs.pdf`: Slice 14 source-only acceptance PDF using the eight-row notes-column legend with 24 field glyphs drawn from alternate CAD-style blocks at 0.7x through 1.3x. The field includes oversized neighbor clusters, leader-attached symbols, an undersized fragmented glyph, and vectorized `E1` text that must be stripped from geometry and retained as a tag. There is no paired expected-output artifact.

These fixtures are synthetic. They contain no customer plan content.

`pdf_convergence/v1/` contains Gate D's public-safe convergence manifest. It
reuses the synthetic architectural garage and electrical-sheet fixtures and adds
only the explicit shared-frame registration plus known hosting answers. See
`docs/pdf-convergence.md`.

`golden/v1/` is the issue #10 cross-workstream regression suite. It contains the rectangular room, door obstruction, panel-to-EVSE, elevation-change, multiple-valid-route, impossible-route, two-level-building, and synthetic-garage known-answer cases plus intentionally invalid boundary fixtures. See `golden/v1/README.md` and `docs/qa-golden-fixtures.md` for usage.

## RoomPlan

`roomplan/captured-room-3d.json` is a synthetic CapturedRoom-shaped fixture for the RoomPlan / LiDAR importer. It includes nonzero story elevation, 4x4 transforms, polygon and curved wall surfaces, openings, source confidence, an object, and section metadata. It contains no customer or private scan data.

`roomplan/bundle-v3-envelope-room.json` carries the *envelope shape* a real
RoomPlan Bundle v3 export actually has: `coreModel` and
`referenceOriginTransform` are present, and there is **no top-level
identifier** -- the capture's identity lives in the bundle around the room.
The importer used to refuse that outright, so the scan lane had only ever run
against a fixture that happened to state an identifier while the real
production format failed to load. There is no paired expected-output artifact.

Only the key layout is taken from the real format. Every value in the file is
synthetic:

1. The six element collections (`walls`, `floors`, `doors`, `windows`,
   `openings`, `objects`) and `sections`, `story` and `version` are copied
   byte-for-byte from `roomplan/captured-room-3d.json`. The file is built by
   textual surgery on that fixture -- removing its `identifier` and inserting
   the two envelope keys -- rather than by re-serializing a parsed copy, which
   would silently reorder keys and break the byte-identity this claims.
2. `coreModel` is the literal placeholder `BUNDLE-V3-CORE-MODEL-BLOB-PLACEHOLDER`.
   A real export stores an opaque binary blob here.
3. `referenceOriginTransform` is a hand-written quarter-turn about +Y with a
   half-metre/quarter-metre offset -- `[0,0,1,0, 0,1,0,0, -1,0,0,0, 0.5,0,0.25,1]`
   in the column-major order the real field uses.
4. `sections[0].center` is the pre-existing synthetic `[0, 3.2, 0]` from
   `captured-room-3d.json`.

**The importer does read the two envelope fields.** It does not interpret them,
but it copies every unrecognised top-level key of a capture verbatim into
`model.attributes.roomplan.extra_fields`, so both reach the canonical model and
everything derived from it, including IFC exports, which embed the canonical
model as JSON. For this synthetic fixture that is harmless. For a REAL capture it
means the real transform and the real `coreModel` blob travel into every derived
artifact, which is why derived artifacts of a real import must be handled as
private.

No captured coordinates, transforms, room dimensions or other measurements
from any real scan appear in this file. `coreModel` and the `0.25` translation
component are the only two scalars in it that do not also appear in
`captured-room-3d.json`.

`test_the_bundle_envelope_fixture_carries_no_captured_measurements` requires the
file's **bytes on disk** to equal its documented construction,
`_build_envelope_fixture` in `tests/test_roomplan_importer.py`. It also refuses,
in both this file and the synthetic one, every byte a reviewer could not see in a
diff: carriage returns, a byte-order mark, any non-ASCII byte (a zero-width
character renders as nothing), tabs, and trailing whitespace.
That function is the construction above made executable, and it is also how to
regenerate the fixture after a deliberate change to the synthetic one.

Bytes, not text: `Path.read_text` translates CRLF and a lone CR into LF before
any comparison sees them, so a text-level check can be passed by a file whose
line endings carry an encoded payload. A lone CR is also left alone by git's own
text normalisation, so a `.gitattributes` rule would only close part of that.

Weaker versions of this guard were each defeated. Every defeat is kept as a
regression, run through the same file-reading path as the guard itself:

- a global whitelist of permitted scalars let an existing synthetic
  high-precision number be moved into `referenceOriginTransform`;
- a first-occurrence scan let a duplicate key hide a poisoned value, since the
  JSON parser keeps the LAST occurrence;
- comparing the envelope fields as parsed values let arbitrary digits sit in the
  text, because the float parser rounds away digits past double precision --
  `0.2500000000000000000012345678` parses to exactly `0.25`;
- comparing TEXT rather than bytes let a payload ride in the line endings, one
  bit per line, with every text comparison seeing a clean file.

The byte-channel checks matter most for the SYNTHETIC file: the envelope fixture
reproduces it faithfully, so a channel planted there would pass the byte
comparison on its own. Each check has a regression that plants its channel
upstream and fails if that check is removed.

**Trust boundary.** The guard proves this file is exactly a deterministic
function of two things it cannot itself vouch for:

- `roomplan/captured-room-3d.json`. No check on a file can establish where its
  numbers came from. An edit introducing captured data there would flow into
  this fixture, so changes to it deserve the same scrutiny. What the guard does
  close in that file is every channel a reviewer could not see; what remains is
  what a reviewer CAN see -- its JSON values and visible formatting.
- the envelope constants in `tests/test_roomplan_importer.py` --
  `_ENVELOPE_CORE_MODEL` and `_ENVELOPE_TRANSFORM`. The likeliest well-meaning
  mistake is editing those to "more realistic" values. That passes, because it
  edits the guard's own definition of correct; it is a code change a reviewer
  must catch, not something a test can.
