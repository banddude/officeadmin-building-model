# Layer selections evidence (#275)

`render_evidence.py` renders the actual GLB exports of four saved layer
selections on a synthetic two-level building: all visible, phase 1
(lighting hidden, architecture ghosted), power ghosted, and power isolated.
The renderer honours the file's own `KHR_node_visibility` extension and the
actual material alphas, and the panel reads materials back from the
ghost-power GLB: the ghosted route and all three conductor wires drawn
inside it carry `-dimmed` materials at alpha 0.3.

Regenerate with:

    python spikes/layer_selections_evidence/render_evidence.py evidence.png

The committed `evidence.png` is the inspected acceptance artifact; the test
`tests/test_layer_selections_evidence.py` pins its content and determinism.
