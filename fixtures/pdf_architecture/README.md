# Architectural PDF fixtures

All fixtures in this directory are synthetic or sanitized geometry-only reductions and are public-safe. They contain no customer plans or project data.

`v1/simple-floor-plan.pdf` is a tiny vector-only known-answer sheet generated for tests. It contains a single labelled rectangular garage, a printed architectural scale, level/elevation text, ceiling height, floor slab thickness, and one marked door annotation. `simple-floor-plan.expected.json` is the canonical model expected from that PDF when imported with source ID `fixture:simple-floor-plan`.

`v1/ordinary-vector-room.json` is a tiny synthetic/public-safe extracted-observation fixture whose room is bounded by ordinary untagged line primitives rather than PDF rectangle objects. It contains two closed rectangular wall-face loops, one room label, scale/level/height annotations, and one unrelated vector detail for stable-identity coverage.

`v1/cad-export-geometry-only.pdf` is a deterministic geometry-only reduction of the path/operator families observed on a real CAD-exported plan page during the #42 audit. Coordinates were normalized and recomposed onto a tiny page; all customer text, names, addresses, project metadata, layer names, and original coordinates were omitted. It contains only straight segments, cubic Bézier operators, and fill-only path operators needed by #43. The PDF is regression source input, never an expected-output artifact.

`v1/cad-derived-wall-faces.json` is a deterministic geometry-only derivative for #44, normalized with the same public-safe reduction approach as the #43 CAD fixture. It contains four pairs of untagged parallel wall-face runs plus two curve-derived distractor segments copied from the normalized #43 geometry family. It contains no customer text, project identifiers, native IDs, or expected canonical output. Tests add generic sheet/scale/level observations independently so the fixture remains source geometry rather than an expected-output artifact.

