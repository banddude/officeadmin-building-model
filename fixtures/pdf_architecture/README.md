# Architectural PDF fixtures

All fixtures in this directory are synthetic or sanitized geometry-only reductions and are public-safe. They contain no customer plans or project data.

`v1/simple-floor-plan.pdf` is a tiny vector-only known-answer sheet generated for tests. It contains a single labelled rectangular garage, a printed architectural scale, level/elevation text, ceiling height, floor slab thickness, and one marked door annotation. `simple-floor-plan.expected.json` is the canonical model expected from that PDF when imported with source ID `fixture:simple-floor-plan`.

`v1/ordinary-vector-room.json` is a tiny synthetic/public-safe extracted-observation fixture whose room is bounded by ordinary untagged line primitives rather than PDF rectangle objects. It contains two closed rectangular wall-face loops, one room label, scale/level/height annotations, and one unrelated vector detail for stable-identity coverage.

`v1/cad-export-geometry-only.pdf` is a deterministic geometry-only reduction of the path/operator families observed on a real CAD-exported plan page during the #42 audit. Coordinates were normalized and recomposed onto a tiny page; all customer text, names, addresses, project metadata, layer names, and original coordinates were omitted. It retains the straight, cubic Bézier, and fill-only path families used by #43 and includes normalized untagged parallel wall-face runs used by #44 to prove the extraction-to-wall/space path end to end. The PDF is regression source input, never an expected-output artifact.

`v1/cad-derived-wall-faces.json` is an additional deterministic geometry-only #44 algorithm fixture, normalized with the same public-safe reduction approach as the CAD PDF fixture. It contains four pairs of untagged parallel wall-face runs plus two curve-derived distractor segments. It contains no customer text, project identifiers, native IDs, or expected canonical output. Focused tests use it for stable identity, explicit-height precedence, and fail-closed ambiguity coverage; #44 acceptance is proved separately through the extracted `cad-export-geometry-only.pdf` path.
