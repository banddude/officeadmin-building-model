"""Observed ruled-table cells, supplementary to stable native PDF text IDs.

No semantic fields or fixture identities are inferred here. Cell boundaries come
from PDF rulings; text must bind back to an original native row observation.
Coordinates use the importers' displayed, bottom-origin point convention.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence


def _clean(value: str) -> str:
    return " ".join(value.split())


def extract_schedule_tables(
    path: str | Path, *, texts: Sequence[Any], headings: Sequence[Any]
) -> dict[int, list[dict[str, Any]]]:
    if not headings:
        return {}
    import pdfplumber
    from oabm.importers.pdf_architecture.extract import _LayerPage

    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with pdfplumber.open(path) as pdf:
        for number in sorted({heading.page for heading in headings}):
            original = pdf.pages[number - 1]
            page = _LayerPage(pdf, original.page_obj, number, original.initial_doctop)
            visible = page.filter(lambda obj: not obj.get("_oabm_hidden", False))
            tables = visible.find_tables()
            for heading in sorted(
                (item for item in headings if item.page == number),
                key=lambda item: item.element_id,
            ):
                candidates = [
                    table for table in tables
                    if table.bbox[0] <= heading.x_pt <= table.bbox[2]
                    and table.bbox[1] <= page.height - heading.y_pt <= table.bbox[3]
                ]
                if not candidates:
                    continue  # Unruled schedules retain the native text-column path.
                if len(candidates) != 1:
                    result[number].append({
                        "heading_element_id": heading.element_id,
                        "reason_code": "lighting_schedule_table_boundary_ambiguous",
                    })
                    continue
                table = candidates[0]
                bbox = [table.bbox[0], page.height - table.bbox[3],
                        table.bbox[2], page.height - table.bbox[1]]
                table_id = "derived-table:" + hashlib.sha256(
                    repr((number, heading.element_id, tuple(bbox))).encode()
                ).hexdigest()[:20]
                rows = []
                for row_index, (row, values) in enumerate(zip(table.rows, table.extract())):
                    cells = []
                    for column, (bounds, value) in enumerate(zip(row.cells, values)):
                        if bounds is None:
                            cells.append(None)
                            continue
                        x0, top, x1, bottom = bounds
                        cell_bbox = [x0, page.height - bottom, x1, page.height - top]
                        text = _clean(value or "")
                        # A visitor may emit an entire row in one text callback.
                        # Its baseline identifies the row, while the observed grid
                        # (not the parent's initial x) identifies individual cells.
                        parents = sorted(
                            item.element_id for item in texts
                            if item.page == number
                            and bbox[0] - 1 <= item.x_pt <= bbox[2] + 1
                            and cell_bbox[1] - 1 <= item.y_pt <= cell_bbox[3] + 1
                            and text and text in _clean(item.text)
                        )
                        if len(parents) > 1:
                            parents = [source_id for source_id in parents
                                       if any(item.element_id == source_id
                                              and x0 <= item.x_pt <= x1
                                              for item in texts)]
                        cells.append({
                            "element_id": f"{table_id}:r{row_index}:c{column}",
                            "text": text,
                            "evidence_kind": "derived-cell",
                            "method": "pdf-ruled-cell-source-binding",
                            "bbox_pt": cell_bbox,
                            "source_element_ids": parents,
                        })
                    rows.append(cells)
                result[number].append({
                    "element_id": table_id,
                    "heading_element_id": heading.element_id,
                    "bbox_pt": bbox,
                    "method": "pdf-ruled-table-cells",
                    "coordinate_space": "displayed-bottom-origin-points",
                    "rows": rows,
                })
    return dict(result)
