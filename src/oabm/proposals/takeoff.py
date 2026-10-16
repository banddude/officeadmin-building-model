"""Source-bound lighting-mark proposals, never accepted canonical objects.

This opt-in experiment reuses the bounded-proposal transport and canonical
provenance. It deliberately does not replace dispatch_ranking's routing policy.
"""
from __future__ import annotations

import hashlib
import io
import math
import threading
from dataclasses import dataclass, replace
from contextlib import closing
from typing import Callable, Mapping

from PIL import Image
from pypdf import PdfReader

from oabm.model import ContractError, Provenance, stable_id
from oabm.importers.pdf_display import PdfPageDisplayTransform, page_display_transform
from .pdf_annotations import require_printable_appearances
from .contracts import (
    Endpoint, Portable, _array, _finite, _portable, budget, digest,
    fingerprint, integer, text,
)

VERSION = "2.0.0"
TASK = "locate_lighting_marks_in_source_region"
VALIDATOR = "source-lighting-marks-v2"
LEGACY_RENDER_POLICY = "pdfium-mediabox-rgb-no-annots-v1"
PRINT_RENDER_POLICY = "pdfium-mediabox-rgb-print-annots-v2"
_RENDER_LOCK = threading.Lock()
MAX_ITEMS = 128
MAX_IMAGES = 24
MAX_IMAGE_PIXELS = 16_000_000


def _unit_box(value, label):
    if not isinstance(value, tuple) or len(value) != 4:
        raise ContractError(f"{label} requires four normalized coordinates")
    values = tuple(_finite(v, label) for v in value)
    if not (0 <= values[0] < values[2] <= 1 and 0 <= values[1] < values[3] <= 1):
        raise ContractError(f"{label} must be positive and inside [0,1]")
    return values


def _strings(values, label, *, maximum=32, empty=True):
    _array(values, str, label)
    if len(values) > maximum or (not empty and not values):
        raise ContractError(f"{label} exceeds count bounds")
    for value in values:
        text(value, label, maximum=1024)
    if len(set(values)) != len(values):
        raise ContractError(f"{label} must be distinct")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePage(Portable):
    """Claimed PDF page facts; source bytes must be checked before use."""
    source_id: str
    document_sha256: str
    page: int
    width_pt: float
    height_pt: float
    width_px: int
    height_px: int
    media_box_pt: tuple[float, float, float, float]
    rotation: int

    def __post_init__(self):
        text(self.source_id, "source document identity")
        fingerprint(self.document_sha256, "source PDF digest")
        integer(self.page, "1-based source page", maximum=100000)
        for key in ("width_pt", "height_pt"):
            value = _finite(getattr(self, key), key)
            if value <= 0:
                raise ContractError("displayed page dimensions must be positive")
            object.__setattr__(self, key, value)
        for key in ("width_px", "height_px"):
            integer(getattr(self, key), key, maximum=100000)

        if not isinstance(self.media_box_pt, tuple) or len(self.media_box_pt) != 4:
            raise ContractError("source MediaBox requires four absolute point edges")
        box = tuple(_finite(v, "source MediaBox") for v in self.media_box_pt)
        object.__setattr__(self, "media_box_pt", box)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ContractError("source MediaBox must be ordered and positive")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int) or self.rotation not in (0, 90, 180, 270):
            raise ContractError("rendering rotation must be a PDF quarter turn")
        width, height = box[2]-box[0], box[3]-box[1]
        expected = (height, width) if self.rotation in (90,270) else (width,height)
        if (self.width_pt, self.height_pt) != expected:
            raise ContractError("displayed page size disagrees with MediaBox/rotation")

    @property
    def display_transform(self):
        box = self.media_box_pt
        return PdfPageDisplayTransform(
            page_rotation=self.rotation, source_width_pt=box[2]-box[0],
            source_height_pt=box[3]-box[1], displayed_width_pt=self.width_pt,
            displayed_height_pt=self.height_pt, origin_x_pt=box[0], origin_y_pt=box[1],
        )

    @classmethod
    def capture(cls, pdf_page, *, source_id, document_sha256, page, width_px, height_px):
        transform = page_display_transform(pdf_page)
        return cls(source_id=source_id, document_sha256=document_sha256, page=page,
                   width_pt=transform.displayed_width_pt, height_pt=transform.displayed_height_pt,
                   width_px=width_px, height_px=height_px, rotation=transform.page_rotation,
                   media_box_pt=tuple(float(v) for v in pdf_page.mediabox))

    @property
    def id(self):
        return stable_id("source-page", digest({"source_id": self.source_id,
                         "document_sha256": self.document_sha256, "page": self.page}))



@dataclass(frozen=True, slots=True, kw_only=True)
class RenderManifest(Portable):
    """Reproducible render policy, never a trusted attestation by itself."""
    policy: str
    renderer_version: str
    pdfium_version: str
    scale: float
    pixel_to_display_pt: tuple[float, float, float, float, float, float]

    def __post_init__(self):
        if self.policy not in (LEGACY_RENDER_POLICY, PRINT_RENDER_POLICY):
            raise ContractError("unsupported takeoff render policy")
        text(self.renderer_version, "pypdfium2 version")
        text(self.pdfium_version, "PDFium version")
        scale = _finite(self.scale, "render scale")
        if not .1 <= scale <= 8:
            raise ContractError("render scale must be between .1 and 8")
        object.__setattr__(self, "scale", scale)
        if not isinstance(self.pixel_to_display_pt, tuple) or len(self.pixel_to_display_pt) != 6:
            raise ContractError("render requires a six-value crop-pixel affine")
        object.__setattr__(self, "pixel_to_display_pt",
                           tuple(_finite(v, "render affine") for v in self.pixel_to_display_pt))


def _crop_affine(source, crop_px):
    if not isinstance(crop_px, tuple) or len(crop_px) != 4:
        raise ContractError("crop requires four pixel edges")
    for edge in crop_px:
        integer(edge, "crop pixel edge", minimum=0, maximum=100000)
    sx, sy = source.width_pt/source.width_px, source.height_pt/source.height_px
    return (sx, 0.0, 0.0, -sy, crop_px[0]*sx, source.height_pt-crop_px[1]*sy)


def _renderer():
    try:
        import pypdfium2 as pdfium
        from pypdfium2.version import PYPDFIUM_INFO, PDFIUM_INFO
    except ImportError as exc:
        raise ContractError("source rendering requires the optional tables dependency pypdfium2") from exc
    return pdfium, str(PYPDFIUM_INFO), str(PDFIUM_INFO)


def _render_page(document, *, source_id, page, scale, expected_source=None,
                 policy=PRINT_RENDER_POLICY):
    """Read source facts and render from the same immutable original PDF bytes."""
    if not isinstance(document, bytes) or not 1 <= len(document) <= 128_000_000:
        raise ContractError("source PDF must be immutable bytes within 128 MB")
    integer(page, "source page", maximum=10000)
    scale = _finite(scale, "render scale")
    if not .1 <= scale <= 8:
        raise ContractError("render scale must be between .1 and 8")
    if policy not in (LEGACY_RENDER_POLICY, PRINT_RENDER_POLICY):
        raise ContractError("unsupported takeoff render policy")
    pdfium, wrapper_version, engine_version = _renderer()
    try:
        reader = PdfReader(io.BytesIO(document), strict=True)
        if reader.is_encrypted or page > len(reader.pages):
            raise ContractError("encrypted PDF or source page absent")
        actual_page = reader.pages[page-1]
        if _finite(actual_page.get("/UserUnit", 1), "PDF UserUnit") != 1:
            raise ContractError("non-unit PDF UserUnit is unsupported")
        if policy == PRINT_RENDER_POLICY:
            require_printable_appearances(actual_page)
        display = page_display_transform(actual_page)
        width = math.ceil(display.displayed_width_pt*scale)
        height = math.ceil(display.displayed_height_pt*scale)
        if width*height > 64_000_000:
            raise ContractError("full-page render pixel budget exceeded")
        source = SourcePage.capture(actual_page, source_id=source_id,
            document_sha256=hashlib.sha256(document).hexdigest(), page=page,
            width_px=width, height_px=height)
        if expected_source is not None and source != expected_source:
            raise ContractError("claimed PDF page metadata/rotation/raster differs from source")
        # PDFium is not thread-safe. This serializes this component's calls;
        # callers must isolate other PDFium use in a separate process.
        with _RENDER_LOCK, pdfium.PdfDocument(document) as pdf:
            with closing(pdf[page-1]) as rendered_page:
                rendered_page.set_cropbox(*source.media_box_pt)
                if any(abs(a-b) > .0001 for a,b in zip(
                        rendered_page.get_size(), (source.width_pt, source.height_pt))):
                    raise ContractError("renderer page size disagrees with source MediaBox/rotation")
                bitmap = rendered_page.render(
                    scale=scale, rotation=0, crop=(0,0,0,0), may_draw_forms=False,
                    draw_annots=policy == PRINT_RENDER_POLICY, fill_color=(255,255,255,255),
                    grayscale=False, optimize_mode="print" if policy == PRINT_RENDER_POLICY else None,
                    rev_byteorder=True)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
        if image.size != (width, height):
            image.close()
            raise ContractError("renderer raster size disagrees with source transform")
        return source, image, wrapper_version, engine_version
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("source PDF cannot be rendered under the fixed policy") from exc


def render_source_image(document: bytes, *, source_id: str, page: int, scale: float,
                        crop_px: tuple[int, int, int, int], image_id: str, role: str):
    """Produce a source reference and PNG from actual PDF bytes, with no resize."""
    source, full, wrapper, engine = _render_page(
        document, source_id=source_id, page=page, scale=scale)
    try:
        manifest = RenderManifest(policy=PRINT_RENDER_POLICY,
            renderer_version=wrapper, pdfium_version=engine, scale=scale,
            pixel_to_display_pt=_crop_affine(source, crop_px))
        # Validate crop before handing it to Pillow, which otherwise pads outside bounds.
        reference = SourceImage(id=image_id, source=source, role=role, crop_px=crop_px,
                                sha256="0"*64, render_manifest=manifest)
        with full.crop(crop_px) as cropped:
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG")
        payload = buffer.getvalue()
        return replace(reference, sha256=hashlib.sha256(payload).hexdigest()), payload
    finally:
        full.close()


def _validate_rendered_images(references, images, documents):
    """Recompute actual renders; no manifest, object flag or saved hash grants trust."""
    images, documents = dict(images), dict(documents)
    if set(images) != {im.id for im in references}:
        raise ContractError("image bytes must exactly cover supplied context")
    expected_documents = {im.source.document_sha256 for im in references}
    if set(documents) != expected_documents:
        raise ContractError("exact source PDF bytes are required for every claimed document")
    if any(not isinstance(data, bytes) for data in documents.values()):
        raise ContractError("source PDFs must be immutable bytes")
    if sum(map(len, documents.values())) > 256_000_000:
        raise ContractError("source PDF input budget exceeded")
    for sha, data in documents.items():
        if hashlib.sha256(data).hexdigest() != sha:
            raise ContractError("source PDF digest differs from supplied bytes")
    keys = {(im.source, im.render_manifest.scale, im.render_manifest.policy) for im in references}
    if sum(src.width_px*src.height_px for src, _, _ in keys) > 64_000_000:
        raise ContractError("total full-page render budget exceeded")
    cache = {}
    try:
        for reference in references:
            pixels = reference._decode_pixels(images[reference.id])
            key = (reference.source, reference.render_manifest.scale, reference.render_manifest.policy)
            if key not in cache:
                cache[key] = _render_page(documents[reference.source.document_sha256],
                    source_id=reference.source.source_id, page=reference.source.page,
                    scale=reference.render_manifest.scale, expected_source=reference.source,
                    policy=reference.render_manifest.policy)
            actual, full, wrapper, engine = cache[key]
            if actual != reference.source:
                raise ContractError("claimed PDF page metadata/rotation/raster differs from source")
            manifest = reference.render_manifest
            if (manifest.renderer_version, manifest.pdfium_version) != (wrapper, engine):
                raise ContractError("render manifest engine version differs from active renderer")
            with full.crop(reference.crop_px) as cropped:
                if cropped.tobytes() != pixels:
                    raise ContractError("image pixels differ from the claimed PDF page/crop render")
    finally:
        for _, image, _, _ in cache.values():
            image.close()


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceImage(Portable):
    """Declared render/crop; rerender comparison establishes source binding."""
    id: str
    source: SourcePage
    role: str
    crop_px: tuple[int, int, int, int]
    sha256: str
    render_manifest: RenderManifest

    def __post_init__(self):
        text(self.id, "image identity")
        if not isinstance(self.source, SourcePage):
            raise ContractError("image requires a source page")
        if self.role not in ("target", "floor_context", "legend", "schedule", "notes"):
            raise ContractError("unsupported image context role")
        fingerprint(self.sha256, "rendered image digest")
        if not isinstance(self.render_manifest, RenderManifest):
            raise ContractError("image requires a typed render manifest")
        if not isinstance(self.crop_px, tuple) or len(self.crop_px) != 4:
            raise ContractError("crop requires four full-page pixel edges")
        for value in self.crop_px:
            integer(value, "crop pixel edge", minimum=0, maximum=100000)
        x0, y0, x1, y1 = self.crop_px
        if not (0 <= x0 < x1 <= self.source.width_px and
                0 <= y0 < y1 <= self.source.height_px):
            raise ContractError("crop lies outside its displayed source page")
        if (x1 - x0) * (y1 - y0) > MAX_IMAGE_PIXELS:
            raise ContractError("rendered crop pixel budget exceeded")

        if self.render_manifest.pixel_to_display_pt != _crop_affine(self.source, self.crop_px):
            raise ContractError("render manifest affine disagrees with source/crop")

    def validate_bytes(self, payload: bytes, *, document: bytes):
        _validate_rendered_images((self,), {self.id: payload},
                                  {self.source.document_sha256: document})

    def _decode_pixels(self, payload: bytes):
        if not isinstance(payload, bytes) or len(payload) > 32_000_000:
            raise ContractError("image bytes exceed budget")
        if hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ContractError("image content differs from request")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                x0, y0, x1, y1 = self.crop_px
                if image.size != (x1 - x0, y1 - y0) or (image.format != "PNG" or image.mode != "RGB"):
                    raise ContractError("image dimensions/format differ from crop")
                return image.tobytes()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ContractError("invalid bounded image") from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeoffRequest(Portable):
    version: str
    task: str
    validator_version: str
    prompt_version: str
    question: str
    images: tuple[SourceImage, ...]
    target_image_id: str
    allowed_types: tuple[str, ...]
    allowed_tags: tuple[str, ...]
    confidence_threshold: float

    def __post_init__(self):
        if (self.version, self.task, self.validator_version) != (VERSION, TASK, VALIDATOR):
            raise ContractError("unsupported source takeoff task/version")
        text(self.prompt_version, "prompt version")
        text(self.question, "bounded visual question", maximum=8192)
        _array(self.images, SourceImage, "supplied images")
        if not 1 <= len(self.images) <= MAX_IMAGES:
            raise ContractError("image count exceeds budget")
        if len({im.id for im in self.images}) != len(self.images):
            raise ContractError("image identities must be distinct")
        if sum((im.crop_px[2]-im.crop_px[0]) * (im.crop_px[3]-im.crop_px[1])
               for im in self.images) > 64_000_000:
            raise ContractError("total image pixel budget exceeded")
        targets = [im for im in self.images if im.role == "target"]
        if len(targets) != 1 or targets[0].id != self.target_image_id:
            raise ContractError("request requires exactly one named target")
        if not any(im.role == "legend" for im in self.images):
            raise ContractError("lighting request requires explicit legend context")
        _strings(self.allowed_types, "allowed types", maximum=32, empty=False)
        _strings(self.allowed_tags, "allowed fixture tags", maximum=128)
        threshold = _finite(self.confidence_threshold, "review confidence threshold")
        if not 0 <= threshold <= 1:
            raise ContractError("review confidence threshold outside [0,1]")
        object.__setattr__(self, "confidence_threshold", threshold)
        budget(_portable(self))

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    @property
    def target(self):
        return next(im for im in self.images if im.id == self.target_image_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedMark(Portable):
    bounds: tuple[float, float, float, float]
    type_proposal: str | None
    fixture_tag: str | None
    confidence: float
    source_mark: str
    evidence_image_ids: tuple[str, ...]
    unknowns: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "bounds", _unit_box(self.bounds, "local crop bounds"))
        if self.type_proposal is not None:
            text(self.type_proposal, "proposed lighting type")
        if self.fixture_tag is not None:
            text(self.fixture_tag, "proposed fixture tag")
        value = _finite(self.confidence, "mark confidence")
        if not 0 <= value <= 1:
            raise ContractError("mark confidence outside [0,1]")
        object.__setattr__(self, "confidence", value)
        text(self.source_mark, "visible source mark evidence", maximum=1024)
        _strings(self.evidence_image_ids, "evidence image references", maximum=MAX_IMAGES, empty=False)
        _strings(self.unknowns, "mark unknowns")
        if self.type_proposal is None and not self.unknowns:
            raise ContractError("unknown type needs an explicit reason")

    def validate_for(self, request):
        references = {im.id: im for im in request.images}
        if set(self.evidence_image_ids) - set(references):
            raise ContractError("proposal references unsupplied image evidence")
        if request.target_image_id not in self.evidence_image_ids:
            raise ContractError("proposal must cite its target mark")
        if self.type_proposal is not None:
            if self.type_proposal not in request.allowed_types:
                raise ContractError("proposal type is outside allowed choices")
            if not any(references[key].role == "legend" for key in self.evidence_image_ids):
                raise ContractError("typed mark requires supplied legend evidence")
        if self.fixture_tag is not None:
            if self.fixture_tag not in request.allowed_tags:
                raise ContractError("proposal tag is outside supplied choices")
            if not any(references[key].role == "schedule" for key in self.evidence_image_ids):
                raise ContractError("tag proposal requires supplied schedule evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeoffOutput(Portable):
    version: str
    items: tuple[ProposedMark, ...]
    coverage_notes: tuple[str, ...]

    def __post_init__(self):
        if self.version != VERSION:
            raise ContractError("unsupported source takeoff output version")
        _array(self.items, ProposedMark, "proposed marks")
        if len(self.items) > MAX_ITEMS:
            raise ContractError("mark budget exceeded; split the source region")
        _strings(self.coverage_notes, "coverage notes", empty=False)

    def validate_for(self, request):
        for item in self.items:
            item.validate_for(request)
        boxes = [item.bounds for item in self.items]
        if len(set(boxes)) != len(boxes):
            raise ContractError("duplicate exact mark bounds require review before export")


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeoffResult(Portable):
    request: TakeoffRequest
    endpoint: Endpoint
    experiment_reason: str
    raw_output: TakeoffOutput
    request_fingerprint: str
    output_fingerprint: str
    accepted: bool = False
    review_state: str = "pending"

    def __post_init__(self):
        if not isinstance(self.request, TakeoffRequest) or not isinstance(self.endpoint, Endpoint):
            raise ContractError("takeoff result requires typed request and endpoint")
        if not isinstance(self.raw_output, TakeoffOutput):
            raise ContractError("takeoff result requires typed raw proposals")
        text(self.experiment_reason, "explicit experiment authorization", maximum=1024)
        if self.accepted is not False or self.review_state != "pending":
            raise ContractError("AI takeoff proposals are always unaccepted/pending")
        if (self.request_fingerprint != self.request.fingerprint or
                self.output_fingerprint != digest(self.raw_output.to_dict())):
            raise ContractError("takeoff result binding mismatch")
        self.raw_output.validate_for(self.request)

    def validate_against(self, request, endpoint, *, documents, images):
        current = TakeoffResult.from_dict(self.to_dict())
        if current.request != request or current.endpoint != endpoint:
            raise ContractError("stale takeoff request or configured endpoint")
        _validate_rendered_images(current.request.images, images, documents)
        return current

    def unresolved_observations(self, *, documents, images):
        """Return detached importer-compatible rows, without touching a model."""
        result = TakeoffResult.from_dict(self.to_dict())
        _validate_rendered_images(result.request.images, images, documents)
        target = result.request.target
        page = target.source
        x0, y0, x1, y1 = target.crop_px
        rows = []
        for item in result.raw_output.items:
            a, b, c, d = item.bounds
            px = (x0+a*(x1-x0), y0+b*(y1-y0), x0+c*(x1-x0), y0+d*(y1-y0))
            normalized = (px[0]/page.width_px, px[1]/page.height_px,
                          px[2]/page.width_px, px[3]/page.height_px)
            # Existing PDF observations use displayed bottom-origin points.
            sx, _, _, sy, tx, ty = target.render_manifest.pixel_to_display_pt
            pt = (sx*a*(x1-x0)+tx, sy*d*(y1-y0)+ty,
                  sx*c*(x1-x0)+tx, sy*b*(y1-y0)+ty)
            identity = stable_id("source-mark", digest({
                "source_id": page.source_id, "document_sha256": page.document_sha256,
                "page": page.page, "bounds_pt": [round(v, 9) for v in pt],
            }))
            reasons = list(item.unknowns)
            if item.confidence < result.request.confidence_threshold:
                reasons.append("below_configured_confidence_threshold")
            rows.append({
                "kind": "ai_takeoff_proposal", "page": page.page,
                "source_id": page.source_id, "source_page_id": page.id,
                "document_sha256": page.document_sha256, "source_element_id": identity,
                "proposal_id": stable_id("takeoff-proposal", digest({
                    "source_mark_id": identity, "request": result.request_fingerprint,
                    "output": result.output_fingerprint})),
                "status": "unresolved", "reason": "ai_proposal_requires_independent_review",
                "review_state": "pending", "accepted": False,
                "coordinate_convention": "displayed-page; pixels/normalized top-left; pt bottom-left",
                "bounds_px": list(px), "bounds_normalized": list(normalized), "bounds_pt": list(pt),
                "crop_bounds_px": list(target.crop_px),
                "source_to_display_transform": _portable(page.display_transform),
                "render_manifest": target.render_manifest.to_dict(),
                "position_pt": {"x": (pt[0]+pt[2])/2, "y": (pt[1]+pt[3])/2},
                "type_proposal": item.type_proposal, "fixture_tag": item.fixture_tag,
                "confidence": item.confidence, "unknowns": reasons, "source_mark": item.source_mark,
                "evidence_image_ids": list(item.evidence_image_ids),
                "provenance": [_portable(Provenance(
                    source_kind="ai-proposed", source_id=page.source_id,
                    source_element_id=identity, page=page.page, method=result.request.prompt_version,
                    confidence=item.confidence, derivation="inferred",
                    attributes={"request_fingerprint": result.request_fingerprint,
                                "output_fingerprint": result.output_fingerprint,
                                "endpoint": _portable(result.endpoint),
                                "source_document_sha256": page.document_sha256,
                                "render_manifest_fingerprint": digest(target.render_manifest.to_dict())},
                ))],
            })
        return sorted(rows, key=lambda row: row["source_element_id"])


def run_takeoff_experiment(
    request: TakeoffRequest, endpoint: Endpoint, *,
    adapters: Mapping[str, Callable], images: Mapping[str, bytes],
    documents: Mapping[str, bytes], experiment_reason: str,
) -> TakeoffResult:
    """Explicit caller-authorized vision experiment with no implicit fallback.

    This is not the production routing ladder. Callers supply the actual adapter
    and model alias; ordinary CI uses fakes. Pixels and a disposable JSON request
    are passed to the registered adapter. Output cannot choose its own endpoint,
    accepted state, source coordinates, circuits or canonical geometry.
    """
    request = TakeoffRequest.from_dict(request.to_dict())
    endpoint = Endpoint(**_portable(endpoint))
    text(experiment_reason, "explicit experiment authorization", maximum=1024)
    images = dict(images)
    _validate_rendered_images(request.images, images, documents)
    adapter = adapters.get(endpoint.adapter_id)
    if not callable(adapter):
        raise ContractError("configured takeoff adapter is not registered")
    raw = adapter(
        request.to_dict(), images=dict(images),
        provider_id=endpoint.provider_id, model_alias=endpoint.model_alias,
        prompt_version=request.prompt_version,
    )
    output = TakeoffOutput.from_dict(raw)
    output.validate_for(request)
    return TakeoffResult(
        request=request, endpoint=endpoint, experiment_reason=experiment_reason,
        raw_output=output, request_fingerprint=request.fingerprint,
        output_fingerprint=digest(output.to_dict()),
    )
