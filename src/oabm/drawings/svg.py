from __future__ import annotations

import html
from collections import defaultdict

from .model import DrawingDimension, DrawingPrimitive, DrawingView, Point2


def view_to_svg(view: DrawingView, *, pixels_per_model_unit: float = 100.0) -> str:
    """Serialize a view to deterministic SVG for inspection/export.

    SVG carries canonical source IDs in ``data-source-ids``; it is a presentation
    artifact and is intentionally not parseable back into the canonical model.
    """
    width = view.bounds.width * pixels_per_model_unit
    height = view.bounds.height * pixels_per_model_unit
    layers: dict[str, list[DrawingPrimitive]] = defaultdict(list)
    for primitive in view.primitives:
        layers[primitive.layer].append(primitive)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_fmt(width)} {_fmt(height)}" data-view-id="{html.escape(view.id)}" data-view-type="{view.view_type}">',
        f'  <title>{html.escape(view.title)}</title>',
    ]
    for layer in sorted(layers):
        lines.append(f'  <g id="{html.escape(layer)}">')
        for primitive in sorted(layers[layer], key=lambda item: item.id):
            lines.append("    " + _primitive_svg(primitive, view, pixels_per_model_unit))
        lines.append("  </g>")
    if view.dimensions:
        lines.append('  <g id="dimensions">')
        for dimension in sorted(view.dimensions, key=lambda item: item.id):
            lines.extend("    " + item for item in _dimension_svg(dimension, view, pixels_per_model_unit))
        lines.append("  </g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _primitive_svg(primitive: DrawingPrimitive, view: DrawingView, ppu: float) -> str:
    attrs = _attrs(primitive)
    if primitive.kind in {"line", "polyline", "polygon"}:
        points = " ".join(_svg_point(point, view, ppu) for point in primitive.points)
        tag = "polygon" if primitive.closed or primitive.kind == "polygon" else "polyline"
        fill = "none"
        return f'<{tag} id="{primitive.id}" points="{points}" fill="{fill}" stroke="currentColor" {attrs}/>'
    anchor = _svg_point(primitive.points[0], view, ppu)
    x, y = anchor.split(",")
    if primitive.kind == "text":
        return f'<text id="{primitive.id}" x="{x}" y="{y}" {attrs}>{html.escape(primitive.text or "")}</text>'
    if primitive.kind == "symbol":
        token = html.escape(primitive.symbol or "")
        return f'<g id="{primitive.id}" transform="translate({x} {y})" data-symbol="{token}" {attrs}><circle r="4" fill="none" stroke="currentColor"/><path d="M-4 0H4M0-4V4" stroke="currentColor"/></g>'
    raise ValueError(f"unsupported primitive kind {primitive.kind!r}")


def _dimension_svg(dimension: DrawingDimension, view: DrawingView, ppu: float) -> list[str]:
    a = _offset_point(dimension.start, dimension.end, dimension.offset_m)
    b = _offset_point(dimension.end, dimension.start, -dimension.offset_m)
    ax, ay = _svg_point(a, view, ppu).split(",")
    bx, by = _svg_point(b, view, ppu).split(",")
    mx = (float(ax) + float(bx)) / 2.0
    my = (float(ay) + float(by)) / 2.0
    source_ids = html.escape(" ".join(dimension.source_ids))
    return [
        f'<line id="{dimension.id}" x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" stroke="currentColor" data-source-ids="{source_ids}"/>',
        f'<text x="{_fmt(mx)}" y="{_fmt(my)}" text-anchor="middle" data-dimension-id="{dimension.id}">{html.escape(dimension.text)}</text>',
    ]


def _offset_point(a: Point2, b: Point2, amount: float) -> Point2:
    dx = b.x - a.x
    dy = b.y - a.y
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return a
    return Point2(x=a.x - dy / length * amount, y=a.y + dx / length * amount)


def _attrs(primitive: DrawingPrimitive) -> str:
    source_ids = html.escape(" ".join(primitive.source_ids))
    return (
        f'data-source-ids="{source_ids}" '
        f'data-stroke="{html.escape(primitive.style.stroke)}" '
        f'data-weight="{html.escape(primitive.style.weight)}" '
        f'data-pattern="{html.escape(primitive.style.pattern)}"'
    )


def _svg_point(point: Point2, view: DrawingView, ppu: float) -> str:
    x = (point.x - view.bounds.min_x) * ppu
    y = (view.bounds.max_y - point.y) * ppu
    return f"{_fmt(x)},{_fmt(y)}"


def _fmt(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text
