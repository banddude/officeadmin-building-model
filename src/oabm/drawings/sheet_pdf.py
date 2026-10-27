"""Optional local vector PDF adapter for renderer-produced blank sheets only."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import xml.etree.ElementTree as ET

from .sheets import SheetSpec
from .sheet_svg import render_blank_sheet_svg

VERSION = "1.0.0"
DEPENDENCY_MESSAGE = "PDF output requires officeadmin-building-model[pdf] (CairoSVG) and an installed native Cairo library; install the optional extra and check Cairo availability."
_MM_TO_PT = 72.0 / 25.4


@dataclass(frozen=True, slots=True)
class BlankSheetPdf:
    pdf: bytes
    manifest_json: str

    def manifest(self):
        return json.loads(self.manifest_json)


def _dependencies():
    try:
        import cairosvg
        import cairocffi
        import pypdf
    except (ImportError, OSError) as exc:
        raise ImportError(DEPENDENCY_MESSAGE) from exc
    return cairosvg,cairocffi,pypdf


def _renderer_svg(spec):
    """Only the bounded internal renderer may supply converter input."""
    raw=render_blank_sheet_svg(spec).svg.encode()
    root=ET.fromstring(raw)
    allowed={
        "svg":{"width","height","viewBox"}, "title":set(), "desc":set(),
        "rect":{"x","y","width","height","fill","stroke","stroke-width"},
        "line":{"x1","y1","x2","y2","stroke","stroke-width"},
        "text":{"x","y","font-family","font-size","text-anchor","fill","paint-order",
                "stroke","stroke-width","stroke-linejoin","data-titleblock-row"}}
    for node in root.iter():
        tag=node.tag.removeprefix('{http://www.w3.org/2000/svg}')
        if tag not in allowed or set(node.attrib)-allowed[tag]:
            raise ValueError(f"sheet {spec.id}: unsupported SVG content/attributes; only internally rendered blank titleblocks are supported")
        for key,value in node.attrib.items():
            if (any(token in value.lower() for token in ('url(', '@import', 'file:', 'data:')) or
                    key in {'fill','stroke'} and value not in {'none','#000000','#ffffff'}):
                raise ValueError(f"sheet {spec.id}: external/resource SVG attributes are unsupported")
    if root.tag!='{http://www.w3.org/2000/svg}svg' or root.get('width') is None or root.get('height') is None:
        raise ValueError(f"sheet {spec.id}: expected renderer-produced physical SVG")
    if root.get('width')!=f'{spec.width_mm:.9f}'.rstrip('0').rstrip('.')+'mm' or root.get('height')!=f'{spec.height_mm:.9f}'.rstrip('0').rstrip('.')+'mm':
        raise ValueError(f"sheet {spec.id}: SVG paper dimensions differ from SheetSpec")
    return raw


def _check_page(page,spec):
    expected=(spec.width_mm*_MM_TO_PT,spec.height_mm*_MM_TO_PT)
    actual=(float(page.mediabox.width),float(page.mediabox.height))
    if any(not math.isfinite(a) or abs(a-b)>.01 for a,b in zip(actual,expected)):
        raise ValueError(f"sheet {spec.id}: PDF page dimensions differ from supplied paper size")
    if abs(float(page.mediabox.left))>.01 or abs(float(page.mediabox.bottom))>.01 or page.rotation:
        raise ValueError(f"sheet {spec.id}: PDF has unexpected origin or rotation")
    if list(page.images):
        raise ValueError(f"sheet {spec.id}: unexpected raster images in vector blank-sheet PDF")
    normal=lambda value:' '.join(value.split())
    extracted=normal(page.extract_text() or '')
    expected_rows=[f'{spec.id} | {spec.title or "Drawing sheet"}',
                   f'Revision: {spec.revision or "unspecified"}',
                   *(f'{key}: {value}' for key,value in spec.titleblock_fields)]
    missing=[row for row in expected_rows if normal(row) not in extracted]
    if missing:
        raise ValueError(f"sheet {spec.id}: PDF text verification failed for supplied titleblock rows")
    resources=page.get("/Resources",{})
    fonts=resources.get("/Font",{}) if hasattr(resources,"get") else {}
    names=sorted(str(font.get_object().get("/BaseFont","unknown")) for font in fonts.values())
    return {"width_pt":actual[0],"height_pt":actual[1],"text_check":"supplied_rows_found",
            "raster_images":0,"font_names":names}


def render_blank_sheet_set_pdf(specs, *, title: str) -> BlankSheetPdf:
    """Render supplied ordered blank SheetSpecs locally; no arbitrary SVG/assets.

    Uses CairoSVG for page conversion and pypdf for assembly. No web service,
    unsafe XML mode, rasterizer, guessed paper size or approval is involved.
    Renderer text still requires independent visual/font inspection.
    """
    specs=tuple(specs)
    if not specs or any(not isinstance(s,SheetSpec) for s in specs):
        raise ValueError("PDF requires a nonempty ordered sequence of SheetSpec objects")
    if len({s.id.casefold() for s in specs})!=len(specs):
        raise ValueError("PDF sheet IDs must be unique ignoring case")
    if not isinstance(title,str) or not title.strip():
        raise ValueError("PDF title must be explicitly supplied")
    cairosvg,cairo,pypdf=_dependencies()
    writer=pypdf.PdfWriter()
    writer.add_metadata({'/Title':title,'/Producer':'oabm local sheet PDF (CairoSVG + pypdf)'})
    readers=[]
    pages=[]
    for number,spec in enumerate(specs,1):
        svg=_renderer_svg(spec)
        try:
            converted=cairosvg.svg2pdf(bytestring=svg,dpi=96,unsafe=False)
        except Exception as exc:
            raise ValueError(f"sheet {spec.id}: local CairoSVG PDF conversion failed: {exc}") from exc
        reader=pypdf.PdfReader(io.BytesIO(converted),strict=True)
        readers.append(reader)
        if len(reader.pages)!=1:
            raise ValueError(f"sheet {spec.id}: converter must return exactly one PDF page")
        details=_check_page(reader.pages[0],spec)
        writer.add_page(reader.pages[0])
        writer.add_outline_item(spec.id,number-1)
        pages.append({"ordinal":number,"sheet_id":spec.id,"width_mm":spec.width_mm,"height_mm":spec.height_mm,
                      "svg_sha256":hashlib.sha256(svg).hexdigest(),**details})
    output=io.BytesIO()
    writer.write(output)
    data=output.getvalue()
    readback=pypdf.PdfReader(io.BytesIO(data),strict=True)
    if len(readback.pages)!=len(specs):
        raise ValueError("combined PDF page count mismatch")
    for page,spec in zip(readback.pages,specs):
        _check_page(page,spec)
    manifest={"kind":"oabm.blank-sheet-pdf","version":VERSION,"pdf_sha256":hashlib.sha256(data).hexdigest(),
              "page_order":[s.id for s in specs],"pages":pages,"title":title,
              "converter":{"CairoSVG":cairosvg.__version__,"cairo":cairo.cairo_version_string(),"pypdf":pypdf.__version__},
              "limitations":["Local vector conversion of internally generated blank sheets only.",
                             "Extracted text checks do not certify font shaping or visual layout.",
                             "No design, engineering or permit approval is provided."]}
    return BlankSheetPdf(data,json.dumps(manifest,sort_keys=True,indent=2,allow_nan=False))
