"""Source-bound lighting-mark proposals, never accepted canonical objects.

This opt-in experiment reuses the bounded-proposal transport and canonical
provenance. It deliberately does not replace dispatch_ranking's routing policy.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Callable, Mapping

from PIL import Image

from oabm.model import ContractError, Provenance, stable_id
from oabm.importers.pdf_display import PdfPageDisplayTransform, page_display_transform
from .contracts import (
    Endpoint, Portable, _array, _finite, _portable, budget, digest,
    fingerprint, integer, text,
)

VERSION = "1.0.0"
TASK = "locate_lighting_marks_in_source_region"
VALIDATOR = "source-lighting-marks-v1"
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
    """Exact PDF revision and displayed page, before building registration."""
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
class SourceImage(Portable):
    """A rendered/cropped input; crop bounds use full-page top-left pixels."""
    id: str
    source: SourcePage
    role: str
    crop_px: tuple[int, int, int, int]
    sha256: str

    def __post_init__(self):
        text(self.id, "image identity")
        if not isinstance(self.source, SourcePage):
            raise ContractError("image requires a source page")
        if self.role not in ("target", "floor_context", "legend", "schedule", "notes"):
            raise ContractError("unsupported image context role")
        fingerprint(self.sha256, "rendered image digest")
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

    def validate_bytes(self, payload: bytes):
        if not isinstance(payload, bytes) or len(payload) > 32_000_000:
            raise ContractError("image bytes exceed budget")
        if hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ContractError("image content differs from request")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                x0, y0, x1, y1 = self.crop_px
                if image.size != (x1 - x0, y1 - y0) or image.format not in ("PNG", "JPEG"):
                    raise ContractError("image dimensions/format differ from crop")
                image.verify()
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

    def validate_against(self, request, endpoint):
        current = TakeoffResult.from_dict(self.to_dict())
        if current.request != request or current.endpoint != endpoint:
            raise ContractError("stale takeoff request or configured endpoint")
        return current

    def unresolved_observations(self):
        """Return detached importer-compatible rows, without touching a model."""
        result = TakeoffResult.from_dict(self.to_dict())
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
            pt = (normalized[0]*page.width_pt, (1-normalized[3])*page.height_pt,
                  normalized[2]*page.width_pt, (1-normalized[1])*page.height_pt)
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
                                "source_document_sha256": page.document_sha256},
                ))],
            })
        return sorted(rows, key=lambda row: row["source_element_id"])


def run_takeoff_experiment(
    request: TakeoffRequest, endpoint: Endpoint, *,
    adapters: Mapping[str, Callable], images: Mapping[str, bytes], experiment_reason: str,
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
    if set(images) != {im.id for im in request.images}:
        raise ContractError("image bytes must exactly cover supplied context")
    for im in request.images:
        im.validate_bytes(images[im.id])
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
