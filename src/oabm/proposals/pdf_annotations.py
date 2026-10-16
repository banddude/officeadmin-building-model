"""Source annotation inventory for explicit printable-appearance rendering.

Inventory is source evidence, not accepted construction semantics. Document
hashes plus object numbers bind records to the immutable PDF supplied by callers.
"""
from __future__ import annotations

import hashlib
import io
from numbers import Integral

from pypdf import PdfReader
from pypdf.generic import (ArrayObject, DictionaryObject, StreamObject,
                           ContentStream, IndirectObject, NameObject)

from oabm.model import ContractError
from .contracts import _finite, integer


def _appearance_optional_content(appearance, page_resources=None):
    """Bounded appearance/resource inspection without changing layer visibility."""
    seen, active = set(), set()
    nodes = 0
    decoded_bytes = 0

    def resolved(value):
        references = set()
        while isinstance(value, IndirectObject):
            key = (id(value.pdf), value.idnum, value.generation)
            if key in references or len(references) >= 32:
                raise ContractError('cyclic or excessive annotation resource indirection')
            references.add(key)
            value = value.get_object()
        return value

    page_resources = resolved(page_resources)
    if page_resources is not None and not isinstance(page_resources, DictionaryObject):
        raise ContractError('effective page resources must be a dictionary')

    def resource(resources, category, name):
        # PDFium falls back by resource CATEGORY, not by missing entry name.
        holder = resolved(resources.get(category)) if isinstance(resources, DictionaryObject) else None
        if not isinstance(holder, DictionaryObject):
            holder = resolved(page_resources.get(category)) if page_resources is not None else None
        if not isinstance(holder, DictionaryObject) or name not in holder:
            raise ContractError('unresolved annotation appearance resource reference')
        target = resolved(holder[name])
        if (category == '/XObject' and not isinstance(target,StreamObject)) or (
                category in ('/Font','/ExtGState','/Pattern','/Shading') and not isinstance(target,DictionaryObject)):
            raise ContractError('unsupported annotation appearance resource object')
        return target

    def walk(value, *, depth=0, content=False, glyphs=False, parent_resources=None):
        nonlocal nodes, decoded_bytes
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ContractError('annotation appearance resource inspection budget exceeded')
        value = resolved(value)
        if isinstance(value, NameObject):
            return value in ('/OC', '/OCG', '/OCMD')
        if not isinstance(value, (DictionaryObject, list, tuple, ArrayObject)):
            return False
        identity = id(value)
        key = (identity, id(parent_resources), content, glyphs)
        if identity in active:
            raise ContractError('cyclic annotation appearance resource graph')
        if key in seen:
            return False
        active.add(identity)
        try:
            if isinstance(value, DictionaryObject):
                if '/OC' in value or value.get('/Type') in ('/OCG', '/OCMD'):
                    return True
                # Parse actual appearance/form/pattern/Type3 glyph content only;
                # binary image/font resources are traversed but not tokenized.
                is_content = isinstance(value, StreamObject) and (
                    content or value.get('/Subtype') == '/Form' or value.get('/PatternType') == 1)
                resources = parent_resources
                if is_content:
                    own_resources = resolved(value.get('/Resources'))
                    if own_resources is not None and not isinstance(own_resources, DictionaryObject):
                        raise ContractError('annotation appearance resources must be a dictionary')
                    resources = own_resources if own_resources is not None else parent_resources
                    if resources is None:
                        resources = page_resources
                    data = value.get_data()
                    decoded_bytes += len(data)
                    if decoded_bytes > 8_000_000:
                        raise ContractError('annotation appearance decoded content budget exceeded')
                    reference = getattr(value, 'indirect_reference', None)
                    operations = ContentStream(value, getattr(reference, 'pdf', None)).operations
                    if len(operations) > 100000:
                        raise ContractError('annotation appearance operator budget exceeded')
                    for operands, operator in operations:
                        if walk(operands, depth=depth+1, parent_resources=resources):
                            return True
                        category = None
                        name = None
                        if operator in (b'Do', b'Tf', b'gs', b'sh', b'CS', b'cs'):
                            category = {b'Do':'/XObject', b'Tf':'/Font', b'gs':'/ExtGState',
                                        b'sh':'/Shading', b'CS':'/ColorSpace', b'cs':'/ColorSpace'}[operator]
                            name = operands[0] if operands else None
                            if category == '/ColorSpace' and name in ('/DeviceGray','/DeviceRGB','/DeviceCMYK','/Pattern'):
                                category = None
                        elif operator in (b'SCN', b'scn') and operands and isinstance(operands[-1],NameObject):
                            category, name = '/Pattern', operands[-1]
                        if category is not None:
                            if not isinstance(name,NameObject):
                                raise ContractError('invalid annotation appearance resource operand')
                            target = resource(resources,category,name)
                            if walk(target,depth=depth+1,parent_resources=resources):
                                return True
                for name, child in value.items():
                    if walk(child, depth=depth+1, content=glyphs,
                            glyphs=name == '/CharProcs', parent_resources=resources):
                        return True
            else:
                for child in value:
                    if walk(child, depth=depth+1, content=content, glyphs=glyphs, parent_resources=parent_resources):
                        return True
            seen.add(key)
            return False
        finally:
            active.remove(identity)

    try:
        return walk(appearance, content=True)
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError('annotation appearance resources cannot be inspected') from exc


def _effective_page_resources(page):
    current = page
    seen = set()
    for _ in range(32):
        if isinstance(current,IndirectObject):
            current=current.get_object()
        if not isinstance(current,DictionaryObject) or id(current) in seen:
            raise ContractError('invalid page resource inheritance')
        seen.add(id(current))
        if '/Resources' in current:
            resources=current['/Resources']
            if not isinstance(resources,DictionaryObject):
                raise ContractError('effective page resources must be a dictionary')
            return resources
        if '/Parent' not in current:
            return None
        current=current['/Parent']
    raise ContractError('page resource inheritance budget exceeded')


def page_annotation_inventory(page):
    """List source annotation facts without executing actions or appearances."""
    try:
        annotations = page.get('/Annots', [])
        annotations = annotations.get_object() if hasattr(annotations, 'get_object') else annotations
        if not isinstance(annotations, (list, ArrayObject)) or len(annotations) > 10000:
            raise ContractError('unsupported source annotation array')
        result = []
        page_resources = _effective_page_resources(page)
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
                           'optional_content': '/OC' in annot or (
                               isinstance(normal, StreamObject) and _appearance_optional_content(normal, page_resources))})
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
