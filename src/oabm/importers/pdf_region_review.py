"""Opt-in PDF review foundation; deliberately not wired to either importer.

Only internal, independently verified producers may issue runtime capabilities.
This first slice has no production producers. Test producers live in tests only.
Python code able to modify this module's private state is outside this trust
boundary. Serializable receipt IDs describe content, not runtime authority or
proof that a person reviewed it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable
import weakref

EPSILON_PT = 1e-6
MAX_REGIONS = 1000
MAX_JSON_BYTES = 4_000_000
_ADAPTERS = {"pdf_architecture", "pdf_electrical"}
_DISPOSITIONS = {"plan", "non_plan", "unresolved"}
_KEY = object()
_SNAPSHOTS: weakref.WeakSet = weakref.WeakSet()
_PREVIEWS: weakref.WeakSet = weakref.WeakSet()
_PLANS: weakref.WeakSet = weakref.WeakSet()


class RegionReviewError(ValueError):
    """Stable error code; never includes extracted customer text."""

    def __init__(self, code: str, detail: str = "", *, page: int | None = None,
                 review_ids: tuple[str, ...] = ()):
        self.code, self.page, self.review_ids = code, page, review_ids
        super().__init__(f"{code}: {detail}" if detail else code)


def _fail(code: str = "review_schema_invalid", detail: str = "") -> None:
    raise RegionReviewError(code, detail)


def _freeze(value: Any, _depth: int = 0) -> Any:
    """Copy all JSON data deeply; reject coercions and non-JSON objects."""
    if _depth > 32:
        _fail(detail="JSON nesting exceeds supported depth")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(detail="nonfinite value")
        return value
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            _fail(detail="mapping keys must be strings")
        return MappingProxyType({k: _freeze(v, _depth + 1) for k, v in value.items()})
    if type(value) in (tuple, list):
        return tuple(_freeze(v, _depth + 1) for v in value)
    _fail(detail="non-JSON value")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _keys(data: Any, required: set[str], optional: set[str] = frozenset()) -> Mapping:
    if not isinstance(data, Mapping) or set(data) - required - optional or required - set(data):
        _fail(detail="missing or unknown fields")
    return data


def _string(value: Any, maximum: int = 512) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        _fail(detail="invalid string")
    return value


def _sha(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail(detail="invalid SHA-256")
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or value < 1:
        _fail(detail="expected positive integer")
    return value


def _numbers(value: Any, count: int) -> tuple[float, ...]:
    if type(value) not in (list, tuple) or len(value) != count:
        _fail(detail="invalid numeric tuple")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        _fail(detail="coordinates must be finite numbers")
    return tuple(float(v) for v in value)


def _bbox(value: Any, *, allow_degenerate: bool = False) -> tuple[float, ...]:
    b = _numbers(value, 4)
    if b[0] > b[2] or b[1] > b[3] or (not allow_degenerate and (b[0] == b[2] or b[1] == b[3])):
        _fail("review_geometry_invalid", "unordered or degenerate rectangle")
    return b


def _affine(value: Any) -> tuple[float, ...]:
    a = _numbers(value, 6)
    if not math.isfinite(a[0] * a[3] - a[1] * a[2]) or a[0] * a[3] - a[1] * a[2] == 0:
        _fail("review_geometry_invalid", "singular affine transform")
    return a


def _inside(inner: Sequence[float], outer: Sequence[float], margin: float = 0) -> bool:
    return (inner[0] >= outer[0] + margin and inner[1] >= outer[1] + margin
            and inner[2] <= outer[2] - margin and inner[3] <= outer[3] - margin)


def _intersects(a: Sequence[float], b: Sequence[float], epsilon: float = EPSILON_PT) -> bool:
    return not (a[2] < b[0] - epsilon or b[2] < a[0] - epsilon
                or a[3] < b[1] - epsilon or b[3] < a[1] - epsilon)


def _frame(data: Any) -> Mapping:
    keys = {"page_number", "coordinate_space", "media_box_pdf_units", "crop_box_pdf_units",
            "user_unit", "rotation_degrees", "displayed_width_pt", "displayed_height_pt",
            "pdf_to_display_affine", "visible_page_bbox_pt", "normalization_version"}
    d = dict(_keys(data, keys))
    _integer(d["page_number"])
    if d["coordinate_space"] != "displayed-bottom-origin-pt/v1":
        _fail("review_frame_mismatch", "unsupported coordinate space")
    if type(d["rotation_degrees"]) is not int or d["rotation_degrees"] not in (0, 90, 180, 270):
        _fail("review_frame_mismatch", "unsupported rotation")
    for key in ("media_box_pdf_units", "crop_box_pdf_units", "visible_page_bbox_pt"):
        d[key] = _bbox(d[key])
    for key in ("user_unit", "displayed_width_pt", "displayed_height_pt"):
        d[key] = _numbers([d[key]], 1)[0]
        if d[key] <= 0:
            _fail("review_frame_mismatch", "invalid dimensions")
    d["pdf_to_display_affine"] = _affine(d["pdf_to_display_affine"])
    _string(d["normalization_version"])
    if not _inside(d["visible_page_bbox_pt"], (0, 0, d["displayed_width_pt"], d["displayed_height_pt"])):
        _fail("review_frame_mismatch", "visible page outside display")
    return _freeze(d)


def _binding(data: Any) -> Mapping:
    d = _keys(data, {"adapter", "extractor_version", "normalization_version",
                     "observation_fingerprint_sha256"})
    if type(d["adapter"]) is not str or d["adapter"] not in _ADAPTERS:
        _fail("review_extraction_mismatch", "unsupported adapter")
    _string(d["extractor_version"])
    _string(d["normalization_version"])
    _sha(d["observation_fingerprint_sha256"])
    return _freeze(d)


def _inventory(observations: Any, pages: Mapping[int, Mapping]) -> tuple:
    """Producer-neutral complete JSON inventory. Production adapters are deferred."""
    if type(observations) not in (list, tuple):
        _fail("review_extraction_mismatch", "inventory must be a sequence")
    records, seen = [], set()
    for record in observations:
        d = _keys(record, {"page_number", "kind", "source_id", "payload"})
        key = (_integer(d["page_number"]), _string(d["kind"]), _string(d["source_id"]))
        if key[0] not in pages or key in seen:
            _fail("review_extraction_mismatch", "duplicate or unknown source identity")
        seen.add(key)
        records.append((key, _freeze(d)))
    return tuple(record for _, record in sorted(records))


@dataclass(frozen=True, slots=True)
class PdfByteSnapshot:
    """One captured bytes value. This alone is NOT verified extraction authority."""

    source_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.source_bytes) is not bytes or not self.source_bytes:
            _fail("review_source_mismatch", "expected nonempty immutable bytes")

    @classmethod
    def read(cls, path: str | Path) -> PdfByteSnapshot:
        return cls(Path(path).read_bytes())

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.source_bytes).hexdigest()


class _Capability:
    __slots__ = ("_data", "__weakref__")

    def __init__(self, key: object, data: Any):
        if key is not _KEY:
            _fail("review_snapshot_not_issued")
        object.__setattr__(self, "_data", data)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("issued capabilities are immutable")

    def __copy__(self):
        _fail("review_snapshot_not_issued", "capabilities cannot be copied")

    def __deepcopy__(self, memo):
        _fail("review_snapshot_not_issued", "capabilities cannot be copied")

    def __reduce__(self):
        _fail("review_snapshot_not_issued", "capabilities cannot be serialized")


class ExtractionSnapshot(_Capability):
    __slots__ = ()

    @property
    def source_bytes(self) -> bytes:
        return self._data[0].source_bytes

    @property
    def context(self) -> Mapping:
        return self._data[1]

    @property
    def observations(self) -> tuple:
        return self._data[2]


class PreviewReceipt(_Capability):
    __slots__ = ()

    @property
    def render_context(self) -> Mapping:
        return self._data[1]


class ValidatedRegionPlan(_Capability):
    __slots__ = ()

    @property
    def review_set(self) -> Mapping:
        return self._data[1]


def extract_review_snapshot(path: str | Path, adapter: str) -> ExtractionSnapshot:
    """No production adapter is admitted in this foundation-only release."""
    if type(adapter) is not str or adapter not in _ADAPTERS:
        _fail("review_extraction_mismatch", "unsupported adapter")
    _fail("review_adapter_support_incomplete", "production snapshot adapter not yet verified")


def issue_default_review_preview(snapshot: ExtractionSnapshot, page_number: int) -> PreviewReceipt:
    """No production renderer is admitted; caller-supplied receipts cannot substitute."""
    _require_snapshot(snapshot)
    _integer(page_number)
    _fail("review_adapter_support_incomplete", "production preview producer not yet verified")


def _extract_snapshot(captured: PdfByteSnapshot, source_id: str, adapter: str,
                      extractor_version: str, normalization_version: str,
                      extractor: Callable[[bytes], tuple[Any, Any]]) -> ExtractionSnapshot:
    """Internal issuance machinery, NOT a producer-registration/caller API.

    Future verified adapters must be hard-wired to this boundary. Tests use
    producers in their own test module; there is no fixture selector in src.
    The private callback is trusted code, not an untrusted review input.
    """
    if type(captured) is not PdfByteSnapshot or adapter not in _ADAPTERS:
        _fail("review_snapshot_not_issued")
    _string(source_id)
    _string(extractor_version)
    _string(normalization_version)
    frames, observations = extractor(captured.source_bytes)
    pages = {}
    for raw in frames:
        frame = _frame(raw)
        page = frame["page_number"]
        if page in pages or frame["normalization_version"] != normalization_version:
            _fail("review_frame_mismatch", "duplicate page or normalization mismatch")
        pages[page] = frame
    if not pages:
        _fail("review_context_missing", "no frames")
    pages = MappingProxyType(dict(sorted(pages.items())))
    inventory = _inventory(observations, pages)
    payload = {"version": "inventory/v1", "frames": list(pages.values()), "observations": inventory}
    binding = _binding({"adapter": adapter, "extractor_version": extractor_version,
                        "normalization_version": normalization_version,
                        "observation_fingerprint_sha256": _digest(payload)})
    context = MappingProxyType({"content_sha256": captured.content_sha256, "source_id": source_id,
                                "pages": pages, "extraction": binding,
                                "context_producer_version": "region-foundation/v1"})
    result = ExtractionSnapshot(_KEY, (captured, context, inventory))
    _SNAPSHOTS.add(result)
    return result


def _require_snapshot(snapshot: Any) -> None:
    if type(snapshot) is not ExtractionSnapshot or snapshot not in _SNAPSHOTS:
        _fail("review_snapshot_not_issued")
    context = snapshot.context
    if hashlib.sha256(snapshot.source_bytes).hexdigest() != context["content_sha256"]:
        _fail("review_source_mismatch")
    payload = {"version": "inventory/v1", "frames": list(context["pages"].values()),
               "observations": snapshot.observations}
    if _digest(payload) != context["extraction"]["observation_fingerprint_sha256"]:
        _fail("review_extraction_mismatch")


_RENDER_KEYS = {"profile", "content_sha256", "page_number", "frame_digest_sha256",
                "renderer_id", "renderer_version", "view_event", "optional_content_policy",
                "annotation_policy", "interactive_overrides", "external_content_policy",
                "output_raster_sha256", "output_width_px", "output_height_px",
                "display_to_raster_affine", "producer_receipt_id"}


def _render(data: Any) -> Mapping:
    d = dict(_keys(data, _RENDER_KEYS))
    required = {"profile": "pdf-default-review-view/v1", "view_event": "View",
                "optional_content_policy": "document-default-configuration",
                "annotation_policy": "default-view-visible-annotations",
                "external_content_policy": "disabled"}
    if any(d[k] != v for k, v in required.items()) or d["interactive_overrides"] is not False:
        _fail("review_render_profile_unsupported")
    for key in ("content_sha256", "frame_digest_sha256", "output_raster_sha256", "producer_receipt_id"):
        _sha(d[key])
    for key in ("page_number", "output_width_px", "output_height_px"):
        _integer(d[key])
    _string(d["renderer_id"])
    _string(d["renderer_version"])
    d["display_to_raster_affine"] = _affine(d["display_to_raster_affine"])
    payload = {k: v for k, v in d.items() if k != "producer_receipt_id"}
    if d["producer_receipt_id"] != _digest(payload):
        _fail("review_render_context_mismatch", "receipt content digest")
    return _freeze(d)


def _issue_preview(snapshot: ExtractionSnapshot, page_number: int, renderer_id: str,
                   renderer_version: str, renderer: Callable[[bytes, Mapping], tuple]) -> PreviewReceipt:
    """Internal verified-producer boundary; no production caller can select it."""
    _require_snapshot(snapshot)
    _integer(page_number)
    if page_number not in snapshot.context["pages"]:
        _fail("review_context_missing", "unknown page")
    frame = snapshot.context["pages"][page_number]
    raster, width, height, affine = renderer(snapshot.source_bytes, frame)
    if type(raster) is not bytes or not raster:
        _fail("review_render_context_mismatch", "missing raster bytes")
    data = dict(profile="pdf-default-review-view/v1", content_sha256=snapshot.context["content_sha256"],
                page_number=page_number, frame_digest_sha256=_digest(frame), renderer_id=renderer_id,
                renderer_version=renderer_version, view_event="View",
                optional_content_policy="document-default-configuration",
                annotation_policy="default-view-visible-annotations", interactive_overrides=False,
                external_content_policy="disabled", output_raster_sha256=hashlib.sha256(raster).hexdigest(),
                output_width_px=width, output_height_px=height, display_to_raster_affine=affine)
    data["display_to_raster_affine"] = _affine(data["display_to_raster_affine"])
    data["producer_receipt_id"] = _digest(data)
    result = PreviewReceipt(_KEY, (snapshot, _render(data)))
    _PREVIEWS.add(result)
    return result


def _parse_review_set(data: Any) -> Mapping:
    _keys(data, {"version", "content_sha256", "source_id", "page_frames", "extraction_bindings", "regions"})
    frozen = _freeze(data)
    if len(_canonical(frozen)) > MAX_JSON_BYTES or frozen["version"] != "1.0":
        _fail(detail="unsupported version or oversized review")
    _sha(frozen["content_sha256"])
    _string(frozen["source_id"])
    for name in ("page_frames", "extraction_bindings", "regions"):
        if type(frozen[name]) is not tuple:
            _fail(detail="expected sequence")
    if len(frozen["regions"]) > MAX_REGIONS:
        _fail(detail="too many regions")
    frames = tuple(_frame(f) for f in frozen["page_frames"])
    bindings = tuple(_binding(b) for b in frozen["extraction_bindings"])
    if not bindings or len(bindings) > 2 or len({b["adapter"] for b in bindings}) != len(bindings):
        _fail("review_extraction_mismatch", "missing or duplicated adapter binding")
    if len({f["page_number"] for f in frames}) != len(frames):
        _fail("review_frame_mismatch", "duplicate page frame")
    regions, by_id = [], {}
    for raw in frozen["regions"]:
        d = dict(_keys(raw, {"review_id", "region_id", "page_number", "bbox_pt", "disposition",
                             "scope", "reviewer_id", "reviewed_at", "review_basis", "render_context",
                             "basis_reference"}, {"note"}))
        for key in ("review_id", "region_id", "reviewer_id", "basis_reference"):
            _string(d[key])
        if "note" in d and (type(d["note"]) is not str or len(d["note"]) > 4096):
            _fail(detail="invalid note")
        _integer(d["page_number"])
        d["bbox_pt"] = _bbox(d["bbox_pt"])
        if type(d["disposition"]) is not str or d["disposition"] not in _DISPOSITIONS or d["scope"] != "automatic_plan_object_admission":
            _fail(detail="unsupported disposition or scope")
        if d["review_basis"] != "issued_default_view_preview":
            _fail("review_render_profile_unsupported")
        try:
            dt = datetime.fromisoformat(_string(d["reviewed_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            _fail(detail="invalid review timestamp")
        if dt.tzinfo is None or dt.utcoffset() is None:
            _fail(detail="review timestamp needs timezone")
        d["render_context"] = _render(d["render_context"])
        d = _freeze(d)
        key = d["region_id"]
        if key in by_id:
            if _canonical(by_id[key]) != _canonical(d):
                _fail("review_id_conflict")
            continue
        by_id[key] = d
        regions.append(d)
    return _freeze({**_plain(frozen), "page_frames": sorted(frames, key=lambda f: f["page_number"]),
                    "extraction_bindings": sorted(bindings, key=lambda b: b["adapter"]),
                    "regions": sorted(regions, key=lambda r: (r["page_number"], r["region_id"]))})


def review_set_from_json(text: str) -> Mapping:
    """Parse portable review records, never runtime snapshot/receipt authority."""
    if type(text) is not str or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        _fail(detail="oversized or invalid JSON input")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail(detail="duplicate JSON object key")
            result[key] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=lambda value: _fail(detail="nonfinite JSON"))
    except RegionReviewError:
        raise
    except (ValueError, TypeError, RecursionError):
        _fail(detail="invalid JSON")
    return _parse_review_set(data)


def validate_region_reviews(snapshot: ExtractionSnapshot, runtime_preview_receipts: Sequence[PreviewReceipt],
                            reviewed_regions: Mapping, *, selected_pages: Sequence[int] | None = None
                            ) -> ValidatedRegionPlan:
    """Validate the complete input atomically. No canonical objects are produced."""
    data = _parse_review_set(reviewed_regions)
    if type(runtime_preview_receipts) not in (list, tuple):
        _fail("review_render_context_mismatch", "expected receipt sequence")
    if selected_pages is not None and type(selected_pages) not in (list, tuple):
        _fail("review_page_not_selected", "expected page sequence")
    _require_snapshot(snapshot)
    context = snapshot.context
    if data["source_id"] != context["source_id"] or data["content_sha256"] != context["content_sha256"]:
        _fail("review_source_mismatch")
    binding = next((b for b in data["extraction_bindings"] if b["adapter"] == context["extraction"]["adapter"]), None)
    if binding != context["extraction"]:
        _fail("review_extraction_mismatch")
    if selected_pages is None:
        selected = set(context["pages"])
    else:
        selected = {_integer(p) for p in selected_pages}
        if len(selected) != len(selected_pages) or not selected <= set(context["pages"]):
            _fail("review_page_not_selected")
    frames = {f["page_number"]: f for f in data["page_frames"]}
    referenced = {r["page_number"] for r in data["regions"]}
    if set(frames) != referenced:
        _fail("review_frame_mismatch", "frames must exactly cover referenced pages")
    receipts = {}
    for receipt in runtime_preview_receipts:
        if type(receipt) is not PreviewReceipt or receipt not in _PREVIEWS or receipt._data[0] is not snapshot:
            _fail("review_render_context_mismatch", "preview not issued for this snapshot")
        receipts[receipt.render_context["producer_receipt_id"]] = receipt.render_context
    for page, frame in frames.items():
        if page not in context["pages"] or frame != context["pages"][page]:
            _fail("review_frame_mismatch")
        if page not in selected:
            _fail("review_page_not_selected")
    for region in data["regions"]:
        page = region["page_number"]
        render = region["render_context"]
        if (render["page_number"] != page or render["content_sha256"] != context["content_sha256"]
                or render["frame_digest_sha256"] != _digest(frames[page])
                or receipts.get(render["producer_receipt_id"]) != render):
            _fail("review_render_context_mismatch")
        if not _inside(region["bbox_pt"], frames[page]["visible_page_bbox_pt"]):
            _fail("review_geometry_invalid", "review region outside visible page")
    regions = data["regions"]
    for i, a in enumerate(regions):
        for b in regions[i + 1:]:
            if a["page_number"] != b["page_number"] or a["disposition"] == b["disposition"]:
                continue
            x, y = a["bbox_pt"], b["bbox_pt"]
            if min(x[2], y[2]) > max(x[0], y[0]) and min(x[3], y[3]) > max(x[1], y[1]):
                _fail("review_disposition_conflict")
    result = ValidatedRegionPlan(_KEY, (snapshot, data, frozenset(selected)))
    _PLANS.add(result)
    return result


@dataclass(frozen=True, slots=True)
class CandidateSupport:
    """Complete adapter-certified identity support, never an anchor-only bbox.

    Constructing this record is an adapter assertion, not proof of completeness.
    No production adapter supplies this type in the first release.
    """

    candidate_key: str
    page_number: int
    entity_kind: str
    identity_support_refs: tuple[tuple[int, str, str], ...]
    contextual_support_refs: tuple[tuple[int, str, str], ...]
    support_bbox_pt: tuple[float, float, float, float] | None
    complete: bool
    explicit_hint: bool = False

    def __post_init__(self) -> None:
        _string(self.candidate_key)
        _string(self.entity_kind)
        _integer(self.page_number)
        if type(self.complete) is not bool or type(self.explicit_hint) is not bool:
            _fail(detail="support flags must be booleans")
        for name in ("identity_support_refs", "contextual_support_refs"):
            values = getattr(self, name)
            if type(values) not in (list, tuple):
                _fail(detail="invalid support refs")
            refs = []
            for ref in values:
                if type(ref) not in (tuple, list) or len(ref) != 3:
                    _fail(detail="typed source reference required")
                refs.append((_integer(ref[0]), _string(ref[1]), _string(ref[2])))
            if len(set(refs)) != len(refs):
                _fail(detail="duplicate support reference")
            object.__setattr__(self, name, tuple(sorted(refs)))
        if not self.identity_support_refs:
            _fail("review_adapter_support_incomplete", "missing identity support")
        if self.support_bbox_pt is not None:
            object.__setattr__(self, "support_bbox_pt", _bbox(self.support_bbox_pt, allow_degenerate=True))
        if self.complete and self.support_bbox_pt is None:
            _fail("review_adapter_support_incomplete", "complete support requires bounds")


@dataclass(frozen=True, slots=True)
class RegionDecision:
    candidate: CandidateSupport
    action: str
    reason: str
    review_ids: tuple[str, ...]
    region_ids: tuple[str, ...]
    containing_region_ids: tuple[str, ...]


def evaluate_region_candidate(plan: ValidatedRegionPlan, candidate: CandidateSupport) -> RegionDecision:
    """Pure source-frame evaluation. Does not delete, create or alter any model."""
    if type(plan) is not ValidatedRegionPlan or plan not in _PLANS:
        _fail("review_snapshot_not_issued", "plan was not validated")
    if type(candidate) is not CandidateSupport:
        _fail("review_adapter_support_incomplete")
    snapshot, data, selected = plan._data
    if candidate.page_number not in selected:
        _fail("review_page_not_selected")
    observed = {(o["page_number"], o["kind"], o["source_id"]) for o in snapshot.observations}
    if (any(r[0] != candidate.page_number for r in candidate.identity_support_refs)
            or not set(candidate.identity_support_refs) <= observed
            or not set(candidate.contextual_support_refs) <= observed):
        _fail("review_adapter_support_incomplete", "unbound source reference")
    regions = [r for r in data["regions"] if r["page_number"] == candidate.page_number]
    if not regions:
        return RegionDecision(candidate, "unchanged", "no_review_on_page", (), (), ())
    if candidate.support_bbox_pt is None:
        _fail("review_adapter_support_incomplete", "safe localization unavailable")
    hits = [r for r in regions if _intersects(candidate.support_bbox_pt, r["bbox_pt"])]
    refs = tuple(sorted({r["review_id"] for r in hits}))
    ids = tuple(r["region_id"] for r in hits)
    containing = tuple(r["region_id"] for r in hits if all((
        candidate.support_bbox_pt[0] > r["bbox_pt"][0] + EPSILON_PT,
        candidate.support_bbox_pt[1] > r["bbox_pt"][1] + EPSILON_PT,
        candidate.support_bbox_pt[2] < r["bbox_pt"][2] - EPSILON_PT,
        candidate.support_bbox_pt[3] < r["bbox_pt"][3] - EPSILON_PT)))
    if candidate.explicit_hint and any(r["disposition"] != "plan" for r in hits):
        raise RegionReviewError("review_hint_conflict", page=candidate.page_number, review_ids=refs)
    if not hits:
        return RegionDecision(candidate, "unchanged", "outside_review", (), (), ())
    dispositions = {r["disposition"] for r in hits}
    if not candidate.complete:
        return RegionDecision(candidate, "unresolved", "support_not_complete", refs, ids, ())
    if len(dispositions) != 1:
        return RegionDecision(candidate, "unresolved", "mixed_review_contact", refs, ids, ())
    if not containing:
        return RegionDecision(candidate, "unresolved", "review_boundary_contact", refs, ids, ())
    disposition = next(iter(dispositions))
    action = {"plan": "unchanged", "non_plan": "reviewed_exclusion", "unresolved": "unresolved"}[disposition]
    return RegionDecision(candidate, action, "contained_" + disposition, refs, ids, containing)
