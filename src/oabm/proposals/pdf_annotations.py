"""Source annotation inventory for explicit printable-appearance rendering.

Inventory is source evidence, not accepted construction semantics. Document
hashes plus object numbers bind records to the immutable PDF supplied by callers.
"""
from __future__ import annotations

import hashlib
import io
from numbers import Integral

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, StreamObject

from oabm.model import ContractError
from .contracts import _finite, integer


def page_annotation_inventory(page):
    """List source annotation facts without executing actions or appearances."""
    try:
        annotations = page.get('/Annots', [])
        annotations = annotations.get_object() if hasattr(annotations, 'get_object') else annotations
        if not isinstance(annotations, (list, ArrayObject)) or len(annotations) > 10000:
            raise ContractError('unsupported source annotation array')
        result = []
        for index, ref in enumerate(annotations):
            annot = ref.get_object()
            if not isinstance(annot, DictionaryObject):
                raise ContractError('source annotation is not a dictionary')
            flags = annot.get('/F', 0)
            if isinstance(flags, bool) or not isinstance(flags, Integral) or not 0 <= flags < 2**32:
                raise ContractError('invalid source annotation flags')
            rect = annot.get('/Rect')
            if not isinstance(rect, (list, ArrayObject)) or len(rect) != 4:
                raise ContractError('source annotation requires a source rectangle')
            rect = tuple(_finite(v, 'annotation rectangle') for v in rect)
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                raise ContractError('source annotation rectangle must be positive')
            appearance = annot.get('/AP')
            appearance = appearance.get_object() if appearance is not None else None
            normal = appearance.get('/N') if isinstance(appearance, DictionaryObject) else None
            normal = normal.get_object() if normal is not None else None
            appearance_bytes = normal.get_data() if isinstance(normal, StreamObject) else None
            result.append({'index': index, 'object_number': getattr(ref, 'idnum', None),
                           'subtype': str(annot.get('/Subtype', '')), 'flags': int(flags),
                           'print_flag': bool(flags & 4), 'hidden': bool(flags & 2),
                           'raw_rect_pt': list(rect),
                           'normal_appearance_sha256': hashlib.sha256(appearance_bytes).hexdigest() if appearance_bytes is not None else None,
                           'normal_appearance_nonempty': bool(appearance_bytes),
                           'optional_content': '/OC' in annot})
        return tuple(result)
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError('source annotations cannot be inventoried') from exc


def require_printable_appearances(page):
    """Refuse omitted form/state-dependent/missing printable appearances."""
    inventory = page_annotation_inventory(page)
    for row in inventory:
        if not row['print_flag'] or row['hidden']:
            continue
        if row['subtype'] == '/Widget':
            raise ContractError('printable form widgets require explicit source reconciliation')
        if row['subtype'] not in ('/Stamp', '/Square', '/Circle', '/FreeText',
                                  '/Line', '/Polygon', '/PolyLine', '/Ink'):
            raise ContractError('unsupported printable annotation subtype')
        if row['optional_content'] or not row['normal_appearance_nonempty']:
            raise ContractError('printable annotation lacks a supported normal appearance')
    return inventory


def source_annotation_inventory(document: bytes, *, page: int):
    """Source-bound sidecar facts; no display-text or review-status inference."""
    if not isinstance(document, bytes) or not 1 <= len(document) <= 128_000_000:
        raise ContractError('source PDF must be immutable bytes within 128 MB')
    integer(page, 'source page', minimum=1, maximum=10000)
    try:
        reader = PdfReader(io.BytesIO(document), strict=True)
        if reader.is_encrypted or page > len(reader.pages):
            raise ContractError('encrypted PDF or source page absent')
        return {'document_sha256': hashlib.sha256(document).hexdigest(), 'page': page,
                'annotations': list(page_annotation_inventory(reader.pages[page-1]))}
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError('source annotations cannot be inventoried') from exc
