from __future__ import annotations

import csv
import io
from html import escape

from .model import DrawingView, Schedule


def render_svg(view: DrawingView, *, pixels_per_metre: float = 100.0) -> str:
    """Render a deterministic, dependency-free SVG preview of a derived view."""
    if pixels_per_metre <= 0:
        raise ValueError("pixels_per_metre must be > 0")
    bounds = view.clip_bounds
    width = max(bounds.width * pixels_per_metre, 1.0)
    height = max(bounds.height * pixels_per_metre, 1.0)

    def xy(x: float, y: float) -> tuple[float, float]:
        return (
            (x - bounds.min_x) * pixels_per_metre,
            (bounds.max_y - y) * pixels_per_metre,
        )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}" '
            f'height="{height:.3f}" viewBox="0 0 {width:.3f} {height:.3f}" '
            f'data-view-id="{escape(view.id)}" data-model-id="{escape(view.model_id)}">'
        ),
        '<g fill="none" stroke="black" stroke-width="1">',
    ]
    for primitive in view.primitives:
        source = escape(primitive.source.entity_id)
        layer = escape(primitive.layer)
        if len(primitive.points) == 1:
            x, y = xy(primitive.points[0].x, primitive.points[0].y)
            lines.append(
                f'<circle cx="{x:.3f}" cy="{y:.3f}" r="2" data-source-id="{source}" data-layer="{layer}"/>'
            )
            continue
        coords = " ".join(
            f"{px:.3f},{py:.3f}" for px, py in (xy(point.x, point.y) for point in primitive.points)
        )
        tag = "polygon" if primitive.closed else "polyline"
        lines.append(
            f'<{tag} points="{coords}" data-source-id="{source}" data-layer="{layer}"/>'
        )
    lines.append("</g>")

    lines.append('<g fill="black" stroke="none" font-family="sans-serif" font-size="11">')
    for annotation in view.annotations:
        x, y = xy(annotation.position.x, annotation.position.y)
        source = "" if annotation.source is None else f' data-source-id="{escape(annotation.source.entity_id)}"'
        lines.append(
            f'<text x="{x:.3f}" y="{y:.3f}"{source}>{escape(annotation.text)}</text>'
        )
    lines.append("</g>")

    lines.append('<g fill="none" stroke="black" stroke-width="1" font-family="sans-serif" font-size="10">')
    for dimension in view.dimensions:
        x1, y1 = xy(dimension.start.x, dimension.start.y)
        x2, y2 = xy(dimension.end.x, dimension.end.y)
        lines.append(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}"/>')
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        lines.append(
            f'<text x="{mx:.3f}" y="{my:.3f}" fill="black" stroke="none">{escape(dimension.label)}</text>'
        )
    lines.append("</g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def schedule_to_csv(schedule: Schedule) -> str:
    """Serialize a schedule with stable column and row order."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow([column.title for column in schedule.columns])
    for row in schedule.rows:
        writer.writerow(row.values)
    return stream.getvalue()
