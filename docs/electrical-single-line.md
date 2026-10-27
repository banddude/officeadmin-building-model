# Electrical single-line production

An opt-in drawing consumer derives a schematic from canonical equipment, ports,
circuits and conductors. Paper coordinates use millimetres, top-left origin and
+Y down. There is no physical scale or disguised plan `DrawingView`. The tool
performs no engineering, sizing, grounding, protection selection or code approval.

## Shared sheet boundary

`oabm.drawings.company_sheets` owns company defaults, job/per-sheet overrides,
logos, the right title rail, physical paper, measured fonts, XML/space handling,
SVG/PDF assembly and retained profile assets. Single-line supplies SVG `<g>`
bodies within `drawing_bounds_mm`, using the shared text measurement helpers.
The dependency includes final space-preservation commit
`c251382e9c08b875a6f79b6fd069edc46486e345`. No shared renderer fork is introduced.

```python
from oabm.model import BuildingModel
from oabm.drawings.single_line import project_topology, plan_pages
from oabm.drawings.single_line.production import build_single_line_bodies
from oabm.drawings.company_sheets import resolve_company_sheet_set

resolved = resolve_company_sheet_set("profile.json", "job.json")
model = BuildingModel.from_json(open("model.json").read())
plan = plan_pages(project_topology(model), resolved.sheets[0].drawing_bounds_mm)
# Resolve a job whose ordered sheet IDs match plan['pages'].
bodies, source_map = build_single_line_bodies(model, resolved.sheets)
```

```sh
PYTHONPATH=src python -m oabm.drawings.single_line model.json profile.json job.json fresh-attempt
# Installed CLI: oabm-single-line
PYTHONPATH=src python examples/single_line/produce.py fresh-synthetic-output
```

The synthetic producer automatically derives the required job sheet list from one
shared template. Direct CLI job sheets must match the deterministic ordered IDs;
a mismatch reports the required list before reserving output. All sheets need
identical drawing bounds. Fontconfig, installed fonts and the optional CairoSVG/
native Cairo PDF dependencies are required. Printable ASCII is supported; invalid
XML, unsupported glyphs and unplaceable labels fail explicitly. Text wrapping uses
actual shared font widths and preserves every supplied character, including spaces.

## Compact pagination

Small systems share one sheet between the connected one-line, feeder facts and
notes. Larger systems place the supply/transfer/transformer backbone beside the
primary branch groups, then continue the remaining branches at full sheet width.
Parents are repeated with canonical ID and CONTINUED sheet references. The tool
does not invent external buses between distinct equipment ports. Each drawn edge
is one complete canonical circuit/load pair, with its own C-tag and voltage.
Normal/alternate transfer inputs remain separate and switch state is unspecified.

Full canonical circuit/equipment/port IDs, port roles, voltage, poles, phase,
conductor count/role/size/material/insulation and route IDs appear in a compact
measured register. The complete register uses the unused lower band of the final
branch sheet if it fits; otherwise it gets necessary continuation sheets. Notes
and the complete compact sheet index share these sheets. There are no compulsory
per-equipment detail grids, separate notes pages or separate index pages.

The synthetic residential proof has four equipment objects and three explicit
connections on **one** sheet, including on standard A3 landscape drawing bounds.
The shared standard D commercial proof has exactly 27 panels, 32 equipment
objects and 31 explicit connections on **two** sheets:

- E601: utility, generator, main switchboard, transfer and transformer backbone;
  all fifteen 480 V panels and their branching feeds.
- E602: all twelve 120 V panels, their transformer/secondary branches, complete
  feeder facts, supplied equipment notes, unknowns and sheet index.

Critical electrical labels are at least 2.5 mm; secondary status/index text is at
least 2.3 mm. Fonts are never shrunk to fit. Smaller paper or longer supplied IDs/
specifications can require additional sheets or a visible unsupported-layout error.
Symbols and continuation references are diagrammatic, not internal wiring claims.

## Electrical truth and completeness

Supported equipment types: source, generator, meter, panelboard, switchboard,
disconnect, transformer and transfer_switch. Every equipment object is retained.
Each circuit/load-port pair produces exactly one drawn connection, two mapped
terminals and one complete register entry. Equipment
`attributes.single_line_notes` may contain supplied display notes; these are
printed verbatim without interpreting them as engineering decisions.

Supported networks are directed acyclic radial/branching graphs. Ordinary items
have at most one incoming feeder. A transformer needs one primary incoming feeder,
distinct secondary port(s), and at least one secondary feeder. Primary and
secondary voltages remain explicit connection labels; multiple differing secondary
voltages are unsupported. Transfer equipment needs exactly two distinct incoming
ports with canonical roles `normal` and `alternate`; outgoing source ports use
role `load`. The symbol does not assert closed switch state or source paralleling.

Rings, ambiguous multiple feeders, duplicate edges, conflicting supplied voltages,
unsupported equipment/devices and port-only links without directed circuit facts
fail before delivery. Physical route geometry never implies connectivity. Missing
facts remain UNKNOWN. Isolated equipment and absent upstream supply are visible.

## Review package and checks

Fresh attempts expose `delivery/` only after complete rendering and hash checks.
Ordinary failures retain `.pending/` and `failure.json`; existing attempts are
refused. Packages retain exact canonical input, field provenance/source entities,
layout, actual connection paths, equipment-symbol rectangles, measured text
bounds, endpoint mappings, body SVGs, the real shared company delivery and hashes.
The PDF is a vector presentation; SVG is its editable source.

Focused tests cover both complete synthetic systems, deterministic page counts,
standard A3 residential fit, transformer and transfer semantics, measured labels,
space preservation, invalid topology, unknown data, narrow-paper continuation,
node-label containment, routes clear of unrelated equipment, repeatable SVG/PDF
bytes, manifest integrity and failure atomicity. Independent cloud pixel/source
acceptance remains with the parent; local tests do not claim engineering approval.
