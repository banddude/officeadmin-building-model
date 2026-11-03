# Physical route measurement

`oabm-physical-route-measure --model model.json --circuit circuit:id` writes a bounded JSON measurement for one stored physical route connecting a circuit's single source and load. The geometry is described by integer nanometre ticks and quarter-arc radii; the one-way feet value is conservatively rounded upward. Leads, slack and installation allowances are not included.

This is **geometric measurement only**. Source context and obstacle clearance are unverified, a physical outside diameter is not a nominal trade size, and the output neither selects wire nor certifies an installation. No model is modified. It does not send data to a provider or over the network; its output can include sensitive project provenance, so handle it as private project data.

`model_sha256` hashes the UTF-8 canonical JSON snapshot after bounded capture and normalization. It is not a hash of the original input file and does not authenticate the model or prove it was verified.
