# Graph Studio calculation-boundary spike (#93)

This is an offline feasibility comparison, outside the OABM runtime package.
It reads the existing **synthetic** garage contract fixture, validates it through
`BuildingModel`, resolves the circuit's one route, measures the 3D centerline,
and runs two upstream pure functions through a bounded subprocess. It does not
write the model, determine conductor sizes, or apply a jurisdiction pack.

Run from the repository root with the Python package installed and **Node 24 LTS**
(upstream declares Node >=23.6):

```sh
python spikes/graph_studio/spike.py --power-factor 1 --resistance-ohm-per-m 0.001
pytest -q tests/test_reuse_calculation_spike.py
```

`OABM_SPIKE_NODE=/absolute/path/to/node` selects a different Node executable.
No network access, package installation, external service or upstream checkout
is needed to run the spike. CI installs Node 24 alongside the existing Python
test environment and executes the same tests.

The canonical load is **9600 VA**, not watts. Explicit power factor `pf` converts
it to `P = S * pf` for the upstream watts API; the API's `P / (V * pf)` then
recovers `40 A` at `240 V`. The route is `7.3 m` one way. The deliberately
hypothetical resistance `0.001 ohm/m` yields `2 * 40 * 7.3 * .001 = 0.584 V`,
or `0.24333333333333335%`. Tests repeat with `pf=0.8` to catch an erroneous
VA-as-watts conversion. The report retains canonical IDs, inputs, explicit
assumptions, formulas, upstream revision and independently calculated results.

Only one single-phase circuit, one source-to-load route, and one load are
supported. Missing phase/load/voltage, branched or unresolved routes and invalid
assumptions fail closed. There is no neutral/shared-raceway model, reactance,
temperature correction, derating, ampacity check or jurisdiction compliance.
The resistance is **not** derived from the fixture's conductor size.

## Upstream source and license

The only third-party source copied is the following two unmodified files from
[Graph Studio / BuildGraph](https://github.com/muhamadbyaba/Graph-Studio/tree/9cb234266ae06927dcdb1b2de3b0a8a65d8d9f9d),
revision `9cb234266ae06927dcdb1b2de3b0a8a65d8d9f9d`:

| Original path under `packages/engineering-core/src/` | Local path under `upstream/` | SHA-256 |
|---|---|---|
| `electrical/electrics.ts` | `electrical/electrics.ts` | `5e30c37606e5352a745a99b83bcd8e2797c68ef8317853972b716d3c37b2245a` |
| `units/quantity.ts` | `units/quantity.ts` | `fbb0b5ae3f0b244280cc46b2e4d32d164f4712a70b551463ddb5421d8aa92c01` |

Copyright 2026 The BuildGraph Authors. These files are Apache-2.0 licensed;
the upstream [LICENSE](upstream/LICENSE) and [NOTICE](upstream/NOTICE) are
retained verbatim. Hash tests pin the actual audited source. OABM's own
`bridge.mjs` and `spike.py` are new adapter code, not modifications of these files.
This third-party license does not change the license status of the rest of OABM.

The upstream [US pack](https://github.com/muhamadbyaba/Graph-Studio/blob/9cb234266ae06927dcdb1b2de3b0a8a65d8d9f9d/packages/engineering-core/src/jurisdiction/us.ts)
explicitly describes draft representative constants requiring verification.
It is not imported. The proof supports a narrow calculation boundary; it
does not establish that the upstream registry, sizing rules or jurisdiction
data should be adopted wholesale.
