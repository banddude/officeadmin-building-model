# Architectural PDF fixtures

All fixtures in this directory are synthetic and public-safe. They contain no customer plans or project data.

`v1/simple-floor-plan.pdf` is a tiny vector-only known-answer sheet generated for tests. It contains a single labelled rectangular garage, a printed architectural scale, level/elevation text, ceiling height, floor slab thickness, and one marked door annotation. `simple-floor-plan.expected.json` is the canonical model expected from that PDF when imported with source ID `fixture:simple-floor-plan`.
