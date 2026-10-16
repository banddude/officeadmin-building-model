"""Deterministic provider-facing schema and prompt for symbol observations.

These helpers do not dispatch, verify source pixels, or accept observations.
SymbolRequest/Output and the source-bound dispatcher remain authoritative.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .symbols import CATEGORIES, SymbolRequest

TRANSPORT_VERSION = "symbol-normalized-crop-v1"


def normalized_point_schema() -> dict[str, Any]:
    """A top-left crop-relative normalized (x, y) pair, including edges."""
    return {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
            "minItems": 2, "maxItems": 2,
            "description": "[x,y] in this image_id's own crop; top-left (0,0), bottom-right (1,1)."}


def normalized_bounds_schema() -> dict[str, Any]:
    """A crop-relative normalized box; ordering is also checked by SymbolMark."""
    return {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
            "minItems": 4, "maxItems": 4,
            "description": "[left,top,right,bottom] normalized to this image_id's own crop; left<right, top<bottom."}


def symbol_output_schema(request: SymbolRequest, *,
                         scope_schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return fresh JSON Schema without weakening geometry for extensions.

    The optional scope hook adds an optional property only. Its caller owns the
    scope shape and corresponding typed parser. Historical no-scope output stays
    readable. Providers requiring every property to be required should omit the
    hook until their nullable scope contract is explicitly supported.
    """
    request = SymbolRequest.from_dict(request.to_dict())
    nullable_text = {"type": ["string", "null"]}
    properties = {
        "image_id": {"type": "string", "enum": list(request.target_image_ids)},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "type_proposal": {"type": ["string", "null"], "enum": [*request.allowed_types, None]},
        "type_tag": deepcopy(nullable_text), "instance_label": deepcopy(nullable_text),
        "bounds": normalized_bounds_schema(),
        "anchor": {"anyOf": [normalized_point_schema(), {"type": "null"}]},
        "polyline": {"type": "array", "items": normalized_point_schema(), "maxItems": 64},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "source_mark": {"type": "string", "minLength": 1, "maxLength": 1024},
        "evidence_image_ids": {"type": "array", "items": {"type": "string", "enum": [im.id for im in request.images]}, "maxItems": 24},
        "unknowns": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
    }
    required = list(properties)
    if scope_schema is not None:
        properties["scope"] = deepcopy(dict(scope_schema))
    mark = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return {"type": "object", "properties": {
        "version": {"type": "string", "enum": [request.version]},
        "marks": {"type": "array", "items": mark, "maxItems": 256},
        "scanned_image_ids": {"type": "array", "items": {"type": "string", "enum": list(request.target_image_ids)}},
        "coverage_notes": {"type": "array", "items": {"type": "string"}},
    }, "required": ["version", "marks", "scanned_image_ids", "coverage_notes"], "additionalProperties": False}


def symbol_transport_prompt(request: SymbolRequest) -> str:
    """Describe exact attachment order and crop units; never repair output."""
    request = SymbolRequest.from_dict(request.to_dict())
    lines = [
        f"Transport contract: {TRANSPORT_VERSION}.",
        "Interpret only the attached source images and typed request. Output JSON only.",
        "Do not call tools, read files, search, execute commands, contact anyone, delegate, or access other context.",
        "COORDINATES: every mark belongs to its exact target image_id. Use that attachment's OWN CROP frame.",
        "All bounds, anchor and polyline coordinates MUST be normalized numbers between 0 and 1 inclusive.",
        "Origin is crop top-left; x increases right, y increases down; crop bottom-right is (1,1).",
        "For a location measured in crop pixels, output x/crop_width_px and y/crop_height_px.",
        "Do not output pixels, PDF points, page-relative coordinates, metres, or a different attachment's frame.",
        "bounds=[left,top,right,bottom] with left<right and top<bottom. Point marks: anchor=[x,y], polyline=[].",
        "Line marks: anchor=null, polyline contains at least two distinct points in the same normalized crop frame.",
        "Emit marks only for target_image_ids; scanned_image_ids must cover every target exactly once.",
        "Context attachments support interpretation but are not target regions unless explicitly targeted.",
        "Use supplied legend/notes evidence and preserve uncertainty. E/N/R and repeated type tags are not unique instance IDs.",
        "Missing context remains unknown. Do not infer circuits, routes, canonical geometry, review acceptance, or compliance.",
        "Attachment order and identity:",
    ]
    for number, im in enumerate(request.images, 1):
        x0, y0, x1, y1 = im.crop_px
        lines.append(
            f"Attachment {number}: image_id={im.id}; role={im.role}; target={im.id in request.target_image_ids}; "
            f"image_sha256={im.sha256}; source_id={im.source.source_id}; PDF_sha256={im.source.document_sha256}; "
            f"original_page={im.source.page}; crop_px={im.crop_px}; crop_width_px={x1-x0}; crop_height_px={y1-y0}; "
            f"displayed_page_px={im.source.width_px}x{im.source.height_px}; rotation={im.source.rotation}. "
            "Output frame is this crop alone, not displayed_page_px."
        )
    lines.extend(("FULL TYPED REQUEST:", request.to_json()))
    return "\n".join(lines)
