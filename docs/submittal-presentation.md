# Utility-submittal presentation (opt-in)

Plan sets sent to a utility or agency need plain sheets. The default review
presentation keeps every diagnostic, source-basis row and disclaimer on the
printed sheet. The submittal presentation prints the same admitted geometry and
topology without that internal wording. Receipts, audits, source maps and the
retained JSON reports are unchanged and still carry every qualification.

## Site plan (`site-drafting-v1` graphics, optional `submittal` key)

```json
"submittal": {
  "legend": [{"label": "P1  (N) meter-main", "sample": "box"},
             {"label": "E1  (E) meter", "sample": "symbol", "symbol": "site-panel"},
             {"label": "(N) 3 in. PVC lateral", "sample": "heavy"}],
  "annotations": [{"text": "MORENO DR", "at": [x, y], "source_ids": ["street"],
                   "size": 5, "weight": 700, "rotate": 90},
                  {"text": "P1", "at": [x, y], "leader_to": [x2, y2], "source_ids": ["p1"]}],
  "dimension_units": "ft",
  "scale_bar_ft": [0, 4, 8, 16]
}
```

- Replaces PLAN GRAPHICS with the caller's plain LEGEND (samples: line, dash,
  dot, heavy, box, symbol). Generic-mark rows are not printed.
- Omits the SOURCE / PRESENTATION BASIS block and the two review disclaimers.
- `annotations` place text (optionally rotated a quarter turn) and leader
  arrows in request coordinates, bound to real feature IDs.
- Dimensions may name explicit `start_xy`/`end_xy` with `source_ids` and an
  optional fixed `text`; otherwise `dimension_units: "ft"` prints feet-inches.
- `scale_bar_ft` draws the scale bar in feet with the sheet scale label.

The shared company body admits text `transform="rotate(+/-90|180|270 cx cy)"`
only, checking the exact rotated text box against the drawing bounds.

## Single-line

```python
from oabm.drawings.single_line.drafting_facts import submittal_presentation
with submittal_presentation():
    layout = plan_pages(project_topology(model), bounds, first_sheet_id="E-1")
    bodies, receipt = build_single_line_bodies(model, sheets, layout=layout)
```

Unknown status prints no `(?)`; unknown bus ratings print no `BUS A ?`;
feeders use the circuit name, protection and the circuit's supplied drafting
notes (conductor and conduit callouts) instead of C-tags, port IDs and
`Routes: UNKNOWN`. Register links and generic symbol notes are not printed on
a single-sheet set. The feeder schedule uses plain columns.

## Load calculation

`utility_delivery._load_bodies(report, template, reserved, submittal=True)`
prints a plain connected-load table and the demand steps (first block,
remainder, general demand, HVAC, floor heat, total, current) without record
IDs, source IDs, the model hash, approval wording or the sensitivity scenario.
