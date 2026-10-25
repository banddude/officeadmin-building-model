"""Primary AI electrical-symbol proposals over verified source pixels.

No recognition heuristics, canonical mutation, circuit design or provider SDK.
The caller registers transport adapters and owns orchestration and review.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from oabm.model import ContractError, Provenance, stable_id
from .contracts import (
    Endpoint, Portable, RoutingConfig, _array, _finite, _portable as _contract_portable, budget,
    digest, fingerprint, integer, text,
)
from .dispatch import AdapterAvailabilityError, _availability_reason
from .symbol_scope import SymbolScope
from .takeoff import SourceImage, _strings, _unit_box, _validate_rendered_images


def _portable(value):
    """Preserve typed legacy symbol omissions inside nested audit artifacts."""
    def prune(source, encoded):
        if isinstance(source, (SymbolMark, SymbolProposal)) and source.scope is None:
            encoded.pop("scope", None)
            if isinstance(source, SymbolProposal):
                encoded.pop("scope_sources", None)
        if is_dataclass(source) and not isinstance(source, type):
            for field in fields(source):
                if field.name in encoded:
                    prune(getattr(source, field.name), encoded[field.name])
        elif isinstance(source, (tuple, list)):
            for child, data in zip(source, encoded):
                prune(child, data)
        elif isinstance(source, dict):
            for key, child in source.items():
                prune(child, encoded[str(key)])
        return encoded
    return prune(value, _contract_portable(value))


class _SymbolPortable(Portable):
    def to_dict(self):
        data = _portable(self)
        self.from_dict(data)
        return data


VERSION = "1.0.0"
TASK = "extract_electrical_symbols"
VALIDATOR = "electrical-symbols-v1"
CATEGORIES = ("luminaire", "switch", "dimmer", "detector", "fan",
              "receptacle", "equipment", "linear_symbol", "unknown")
MAX_MARKS = 256
MAX_RAW_BYTES = 400000
RESOURCE_SPLIT_REQUIRED = (
    "split target regions or use explicit transport batches while preserving every "
    "applicable context image for the stage; never silently omit context"
)
CHEAPER_DISABLED = (
    "qualified_cheaper is disabled: current evidence is insufficient for task/category/"
    "context/target composition coverage; use strong_baseline"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolResourceLimits(Portable):
    """Explicit batch limits; these never authorize silently discarding context."""
    max_total_pixels: int = 64_000_000
    max_total_image_bytes: int = 64_000_000

    def __post_init__(self):
        # The verifier already limits total full-page work to 64M pixels.
        # Callers can lower this batch ceiling to match their actual transport.
        integer(self.max_total_pixels, "symbol aggregate pixel limit", maximum=64_000_000)
        integer(self.max_total_image_bytes, "symbol aggregate image byte limit", maximum=256_000_000)

    def validate_manifest(self, references):
        total = sum((im.crop_px[2]-im.crop_px[0]) * (im.crop_px[3]-im.crop_px[1])
                    for im in references)
        if total > self.max_total_pixels:
            raise ContractError(
                f"symbol batch pixels {total} exceed configured limit "
                f"{self.max_total_pixels}; {RESOURCE_SPLIT_REQUIRED}")

    def validate_bytes(self, images):
        if any(not isinstance(payload, bytes) for payload in images.values()):
            raise ContractError("symbol image inputs must be immutable bytes")
        total = sum(map(len, images.values()))
        if total > self.max_total_image_bytes:
            raise ContractError(
                f"symbol batch image bytes {total} exceed configured limit "
                f"{self.max_total_image_bytes}; {RESOURCE_SPLIT_REQUIRED}")


def _validate_symbol_inputs(request, images, documents):
    request.resource_limits.validate_manifest(request.images)
    request.resource_limits.validate_bytes(images)
    _validate_rendered_images(request.images, images, documents)


def _point(value, label):
    if not isinstance(value, tuple) or len(value) != 2:
        raise ContractError(f"{label} requires two coordinates")
    result = tuple(_finite(v, label) for v in value)
    if any(v < 0 or v > 1 for v in result):
        raise ContractError(f"{label} must lie inside normalized crop space")
    return result


def _confidence(value):
    value = _finite(value, "symbol confidence")
    if not 0 <= value <= 1:
        raise ContractError("symbol confidence outside [0,1]")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolContext(Portable):
    role: str
    status: str
    image_ids: tuple[str, ...]
    rationale: str

    def __post_init__(self):
        if self.role not in ("legend", "schedule", "notes"):
            raise ContractError("unsupported symbol context role")
        if self.status not in ("supplied", "missing", "inapplicable"):
            raise ContractError("unsupported context availability")
        _strings(self.image_ids, "context images", maximum=24)
        text(self.rationale, "context rationale", maximum=1024)
        if (self.status == "supplied") != bool(self.image_ids):
            raise ContractError("supplied context needs images; missing/inapplicable cannot cite images")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRequest(Portable):
    version: str
    task: str
    validator_version: str
    prompt_version: str
    question: str
    images: tuple[SourceImage, ...]
    target_image_ids: tuple[str, ...]
    contexts: tuple[SymbolContext, ...]
    allowed_types: tuple[str, ...]
    confidence_threshold: float
    resource_limits: SymbolResourceLimits = SymbolResourceLimits()

    def __post_init__(self):
        if (self.version, self.task, self.validator_version) != (VERSION, TASK, VALIDATOR):
            raise ContractError("unsupported electrical symbol request/version")
        text(self.prompt_version, "symbol prompt version")
        text(self.question, "symbol question", maximum=8192)
        _array(self.images, SourceImage, "symbol images")
        if not 1 <= len(self.images) <= 24:
            raise ContractError(f"symbol image count exceeds per-batch contract; {RESOURCE_SPLIT_REQUIRED}")
        if not isinstance(self.resource_limits, SymbolResourceLimits):
            raise ContractError("symbol request needs typed explicit resource limits")
        self.resource_limits.validate_manifest(self.images)
        images = {im.id: im for im in self.images}
        if len(images) != len(self.images):
            raise ContractError("duplicate symbol image identity")
        _strings(self.target_image_ids, "target images", maximum=24, empty=False)
        if len(self.target_image_ids) > 12:
            raise ContractError(f"symbol target count exceeds per-batch contract; {RESOURCE_SPLIT_REQUIRED}")
        if set(self.target_image_ids) != {im.id for im in self.images if im.role == "target"}:
            raise ContractError("target image IDs must exactly cover target crops")
        _array(self.contexts, SymbolContext, "context coverage")
        if sorted(c.role for c in self.contexts) != ["legend", "notes", "schedule"]:
            raise ContractError("legend, schedule and notes need explicit context coverage")
        for context in self.contexts:
            if any(key not in images or images[key].role != context.role for key in context.image_ids):
                raise ContractError("context references absent or wrong-role source images")
            if set(context.image_ids) != {im.id for im in self.images if im.role == context.role}:
                raise ContractError("context must enumerate every supplied image of its role")
        _strings(self.allowed_types, "allowed symbol types", maximum=128)
        object.__setattr__(self, "confidence_threshold", _confidence(self.confidence_threshold))
        budget(_portable(self))

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    @property
    def task_signature(self):
        # Experimental framing digest only; it never authorizes cheaper routing.
        return digest({"version": self.version, "task": self.task,
            "validator": self.validator_version, "prompt_version": self.prompt_version,
            "question": self.question, "allowed_types": sorted(self.allowed_types),
            "context_status": sorted((c.role,c.status) for c in self.contexts),
            "confidence_threshold": self.confidence_threshold,
            "render_policies": sorted({(im.render_manifest.policy, im.render_manifest.renderer_version,
                im.render_manifest.pdfium_version, im.render_manifest.scale) for im in self.images})})


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolMark(Portable):
    image_id: str
    category: str
    type_proposal: str | None
    type_tag: str | None
    instance_label: str | None
    bounds: tuple[float, float, float, float]
    anchor: tuple[float, float] | None
    polyline: tuple[tuple[float, float], ...]
    confidence: float
    source_mark: str
    evidence_image_ids: tuple[str, ...]
    unknowns: tuple[str, ...]
    scope: SymbolScope | None = None

    def __post_init__(self):
        if self.scope is not None and not isinstance(self.scope, SymbolScope):
            raise ContractError("symbol scope requires typed source assertions")
        text(self.image_id, "symbol source image")
        if self.category not in CATEGORIES:
            raise ContractError("symbol category outside vocabulary")
        for key in ("type_proposal", "type_tag", "instance_label"):
            value = getattr(self, key)
            if value is not None:
                text(value, key)
        object.__setattr__(self, "bounds", _unit_box(self.bounds, "symbol bounds"))
        if self.anchor is not None:
            object.__setattr__(self, "anchor", _point(self.anchor, "symbol anchor"))
        if not isinstance(self.polyline, tuple) or len(self.polyline) > 64:
            raise ContractError("symbol polyline exceeds bounds")
        points = tuple(_point(v, "symbol polyline point") for v in self.polyline)
        object.__setattr__(self, "polyline", points)
        if (self.anchor is None) == (not points):
            raise ContractError("symbol needs exactly one point anchor or a polyline")
        if points and (len(points) < 2 or any(a == b for a,b in zip(points,points[1:]))):
            raise ContractError("linear symbol requires distinct consecutive points")
        if self.category == "linear_symbol" and self.anchor is not None:
            raise ContractError("linear category requires source polyline geometry")
        if points and self.category not in ("linear_symbol", "luminaire", "unknown"):
            raise ContractError("point equipment/device categories cannot acquire linear geometry")
        x0,y0,x1,y1 = self.bounds
        for x,y in ((self.anchor,) if self.anchor is not None else points):
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                raise ContractError("symbol anchor/polyline lies outside its glyph bounds")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        text(self.source_mark, "symbol mark evidence", maximum=1024)
        _strings(self.evidence_image_ids, "symbol evidence", maximum=24, empty=False)
        _strings(self.unknowns, "symbol unknowns", maximum=16)
        if (self.category == "unknown" or self.type_proposal is None) and not self.unknowns:
            raise ContractError("unknown symbol/type needs an explicit reason")

    def to_dict(self):
        data = Portable.to_dict(self)
        if self.scope is None:
            data.pop("scope")  # Preserve historical raw outputs and mark IDs.
        return data

    @property
    def geometry_kind(self):
        return "point" if self.anchor is not None else "linear"

    def validate_for(self, request):
        if self.image_id not in request.target_image_ids:
            raise ContractError("symbol must originate in a target crop, not a legend/schedule")
        known = {im.id for im in request.images}
        if self.scope is not None:
            self.scope.validate_for(request)
        if set(self.evidence_image_ids)-known or self.image_id not in self.evidence_image_ids:
            raise ContractError("symbol evidence must cite its target and only supplied images")
        if self.type_proposal is not None and self.type_proposal not in request.allowed_types:
            raise ContractError("symbol type outside caller vocabulary")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolOutput(Portable):
    version: str
    marks: tuple[SymbolMark, ...]
    scanned_image_ids: tuple[str, ...]
    coverage_notes: tuple[str, ...]

    def __post_init__(self):
        if self.version != VERSION:
            raise ContractError("unsupported symbol output version")
        _array(self.marks, SymbolMark, "symbol marks")
        if len(self.marks) > MAX_MARKS:
            raise ContractError("symbol mark budget exceeded; split the request")
        _strings(self.scanned_image_ids, "scanned targets", maximum=12, empty=False)
        _strings(self.coverage_notes, "symbol coverage notes", empty=False)
        keys = [digest(mark.to_dict()) for mark in self.marks]
        if len(keys) != len(set(keys)):
            raise ContractError("exact duplicate raw rows require a corrected bounded response")

    def to_dict(self):
        data = Portable.to_dict(self)
        data["marks"] = [mark.to_dict() for mark in self.marks]
        return data

    def validate_for(self, request):
        if set(self.scanned_image_ids) != set(request.target_image_ids):
            raise ContractError("response does not cover every target crop")
        for mark in self.marks:
            mark.validate_for(request)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolAdapterReply(Portable):
    """Transport-reported model identity is outside the model's JSON output."""
    output_json: str
    resolved_model_id: str | None = None
    provider_request_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.output_json, str) or len(self.output_json.encode()) > MAX_RAW_BYTES:
            raise ContractError("symbol adapter response exceeds raw output budget")
        for key in ("resolved_model_id", "provider_request_id"):
            value = getattr(self,key)
            if value is not None:
                text(value,key)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRoutingPolicy(Portable):
    config: RoutingConfig
    mode: str = "strong_baseline"
    strong_availability_fallback: Endpoint | None = None
    expected_strong_model_id: str | None = None
    expected_cheap_model_id: str | None = None
    expected_diagnostic_model_id: str | None = None
    expected_fallback_model_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.config, RoutingConfig):
            raise ContractError("symbol routing requires existing RoutingConfig")
        if self.mode not in ("strong_baseline", "qualified_cheaper", "diagnostic"):
            raise ContractError("unsupported symbol routing mode")
        if self.mode == "qualified_cheaper":
            raise ContractError(CHEAPER_DISABLED)
        fallback = self.strong_availability_fallback
        if fallback is not None:
            if not isinstance(fallback,Endpoint):
                raise ContractError("strong fallback needs an explicit Endpoint")
            others = (self.config.vision_sol,self.config.vision_luna,self.config.diagnostic_astra)
            if any((fallback.provider_id,fallback.model_alias) == (e.provider_id,e.model_alias) for e in others):
                raise ContractError("strong fallback must be distinct from normal/cheap/diagnostic endpoints")
        for key in ("expected_strong_model_id","expected_cheap_model_id",
                    "expected_diagnostic_model_id","expected_fallback_model_id"):
            value = getattr(self,key)
            if value is not None:
                text(value,key)

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    def endpoint(self, role):
        if role == "vision_sol": return self.config.vision_sol
        if role == "vision_luna": return self.config.vision_luna
        if role == "diagnostic_astra": return self.config.diagnostic_astra
        if role == "strong_fallback" and self.strong_availability_fallback is not None:
            return self.strong_availability_fallback
        raise ContractError("unconfigured symbol routing role")

    def expected_model(self, role):
        return getattr(self, {"vision_sol":"expected_strong_model_id",
            "vision_luna":"expected_cheap_model_id","diagnostic_astra":"expected_diagnostic_model_id",
            "strong_fallback":"expected_fallback_model_id"}[role])


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptReceipt(Portable):
    attempt_id: str
    phase: str
    role: str
    endpoint: Endpoint
    request_fingerprint: str
    policy_fingerprint: str
    timestamp_utc: str
    reason: str
    status: str
    raw_output: str | None = None
    resolved_model_id: str | None = None
    provider_request_id: str | None = None
    error: str | None = None

    def __post_init__(self):
        for key in ("attempt_id","reason","timestamp_utc"):
            text(getattr(self,key),key,maximum=1024)
        if self.phase not in ("start","finish") or self.role not in (
                "vision_sol","vision_luna","strong_fallback","diagnostic_astra"):
            raise ContractError("invalid symbol attempt phase/role")
        if not isinstance(self.endpoint,Endpoint):
            raise ContractError("attempt requires actual configured endpoint")
        for value in (self.request_fingerprint,self.policy_fingerprint):
            fingerprint(value,"attempt content binding")
        try:
            timestamp=datetime.fromisoformat(self.timestamp_utc)
        except ValueError as exc:
            raise ContractError("attempt timestamp must be ISO UTC") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ContractError("attempt timestamp must carry UTC timezone")
        if self.status not in ("started","completed","invalid_output","rate_limited",
                                "unavailable","identity_mismatch","adapter_error","insufficient_confidence"):
            raise ContractError("invalid symbol attempt status")
        if (self.phase == "start") != (self.status == "started"):
            raise ContractError("attempt phase/status disagree")
        if self.phase == "start" and any(v is not None for v in (
                self.raw_output,self.resolved_model_id,self.provider_request_id,self.error)):
            raise ContractError("start receipt cannot claim returned output/identity")
        if self.raw_output is not None:
            SymbolAdapterReply(output_json=self.raw_output,
                resolved_model_id=self.resolved_model_id,provider_request_id=self.provider_request_id)
        for key in ("resolved_model_id","provider_request_id","error"):
            value = getattr(self,key)
            if value is not None: text(value,key,maximum=1024)
        if self.status in ("completed","invalid_output","identity_mismatch","insufficient_confidence") and self.raw_output is None:
            raise ContractError("returned-output attempt needs exact raw output")


class AttemptJournal:
    """Exclusive new, private, append-only and fsync'd JSONL execution journal."""

    def __init__(self, path, run_id):
        self.path = Path(path)
        text(run_id,"symbol run identity")
        self.run_id = run_id
        self._lock = threading.Lock()
        self._receipts = []
        self._previous = "0"*64
        self._fd = os.open(self.path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_APPEND, 0o600)
        try:
            directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except Exception:
            os.close(self._fd);self._fd=None
            raise

    @classmethod
    def create(cls, path, *, run_id=None):
        return cls(path, run_id or str(uuid.uuid4()))

    @property
    def receipts(self):
        return tuple(self._receipts)

    def append(self, receipt):
        if not isinstance(receipt,AttemptReceipt):
            raise ContractError("journal only accepts typed attempt receipts")
        receipt = AttemptReceipt.from_dict(receipt.to_dict())
        with self._lock:
            record = {"run_id":self.run_id,"sequence":len(self._receipts),
                      "previous_sha256":self._previous,"receipt":receipt.to_dict()}
            current = digest(record)
            record["sha256"] = current
            payload = (json.dumps(record,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n").encode()
            view = memoryview(payload)
            while view:
                written = os.write(self._fd,view)
                if written <= 0: raise OSError("attempt journal write failed")
                view = view[written:]
            os.fsync(self._fd)
            self._previous = current
            self._receipts.append(receipt)

    @staticmethod
    def read(path):
        with Path(path).open("rb") as stream:
            data = stream.read(64_000_001)
        if len(data) > 64_000_000 or (data and not data.endswith(b"\n")):
            raise ContractError("attempt journal oversized or has incomplete terminal record")
        try:
            lines=data.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise ContractError("invalid attempt journal UTF-8") from exc
        receipts=[]; previous="0"*64; run_id=None
        for sequence,line in enumerate(lines):
            def pairs(items):
                value={}
                for key,item in items:
                    if key in value: raise ContractError("duplicate journal JSON key")
                    value[key]=item
                return value
            def constant(value): raise ContractError("nonfinite journal JSON")
            try:
                record=json.loads(line,object_pairs_hook=pairs,parse_constant=constant)
            except (ValueError,UnicodeError) as exc:
                raise ContractError("invalid attempt journal JSON") from exc
            if not isinstance(record,dict) or set(record) != {"run_id","sequence","previous_sha256","receipt","sha256"}:
                raise ContractError("unknown attempt journal fields")
            integer(record["sequence"],"journal sequence",minimum=0,maximum=100000)
            text(record["run_id"],"journal run identity")
            fingerprint(record["sha256"],"journal record digest")
            fingerprint(record["previous_sha256"],"journal previous digest")
            actual=record.pop("sha256")
            if (record["sequence"] != sequence or record["previous_sha256"] != previous
                    or digest(record) != actual or (run_id is not None and record["run_id"] != run_id)):
                raise ContractError("attempt journal chain/content mismatch")
            run_id=record["run_id"];previous=actual
            receipts.append(AttemptReceipt.from_dict(record["receipt"]))
        return tuple(receipts)

    def close(self):
        if self._fd is not None:
            os.close(self._fd);self._fd=None

    def __enter__(self): return self
    def __exit__(self,*args): self.close()


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolProposal(Portable):
    proposal_id: str
    source_mark_id: str
    source_id: str
    source_page_id: str
    image_id: str
    document_sha256: str
    page: int
    category: str
    type_proposal: str | None
    type_tag: str | None
    instance_label: str | None
    geometry_kind: str
    bounds_px: tuple[float,float,float,float]
    bounds_normalized: tuple[float,float,float,float]
    bounds_pt: tuple[float,float,float,float]
    anchor_pt: tuple[float,float] | None
    polyline_pt: tuple[tuple[float,float], ...]
    confidence: float
    unknowns: tuple[str, ...]
    source_mark: str
    evidence_image_ids: tuple[str, ...]
    request_fingerprint: str
    result_fingerprint: str
    attempt_id: str
    provenance: tuple[Provenance, ...]
    accepted: bool = False
    review_state: str = "pending"
    scope: SymbolScope | None = None
    scope_sources: tuple[SourceImage, ...] = ()

    def __post_init__(self):
        if self.scope is not None and not isinstance(self.scope, SymbolScope):
            raise ContractError("exported scope requires typed source assertions")
        _array(self.scope_sources, SourceImage, "scope source images")
        expected = () if self.scope is None else self.scope.evidence_image_ids
        if tuple(sorted(im.id for im in self.scope_sources)) != expected:
            raise ContractError("scope source images must exactly cover source assertions")
        if self.accepted is not False or self.review_state != "pending":
            raise ContractError("AI symbol proposals always remain pending/unaccepted")
        if self.category not in CATEGORIES or self.geometry_kind not in ("point","linear"):
            raise ContractError("invalid exported symbol category/geometry")
        if self.geometry_kind != ("point" if self.anchor_pt is not None else "linear"):
            raise ContractError("exported symbol geometry disagree")
        object.__setattr__(self,"confidence",_confidence(self.confidence))
        fingerprint(self.document_sha256,"proposal PDF")
        fingerprint(self.request_fingerprint,"proposal request")
        fingerprint(self.result_fingerprint,"proposal result")
        integer(self.page,"proposal source page",maximum=100000)
        for key in ("proposal_id","source_mark_id","source_id","source_page_id","image_id","attempt_id"):
            text(getattr(self,key),key)
        for key in ("type_proposal","type_tag","instance_label"):
            if getattr(self,key) is not None: text(getattr(self,key),key)
        for key in ("bounds_px","bounds_pt"):
            box=getattr(self,key)
            if not isinstance(box,tuple) or len(box)!=4:
                raise ContractError("exported symbol box requires four coordinates")
            box=tuple(_finite(v,key) for v in box)
            if not (0 <= box[0] < box[2] and 0 <= box[1] < box[3]):
                raise ContractError("exported symbol box is degenerate or outside page space")
            object.__setattr__(self,key,box)
        object.__setattr__(self,"bounds_normalized",_unit_box(self.bounds_normalized,"exported normalized bounds"))
        points=(self.anchor_pt,) if self.anchor_pt is not None else self.polyline_pt
        if self.anchor_pt is not None and self.polyline_pt or self.anchor_pt is None and len(points)<2:
            raise ContractError("exported point/linear geometry disagree")
        for point in points:
            if not isinstance(point,tuple) or len(point)!=2:
                raise ContractError("exported symbol point is invalid")
            x,y=(_finite(v,"source point") for v in point)
            if not (self.bounds_pt[0] <= x <= self.bounds_pt[2] and self.bounds_pt[1] <= y <= self.bounds_pt[3]):
                raise ContractError("exported symbol geometry outside source bounds")
        _strings(self.unknowns,"exported symbol unknowns",maximum=24)
        _strings(self.evidence_image_ids,"exported symbol evidence",maximum=24,empty=False)
        text(self.source_mark,"exported source mark",maximum=1024)
        _array(self.provenance,Provenance,"exported symbol provenance")
        if not self.provenance or any(p.derivation != "inferred" or p.source_kind != "ai-proposed"
                or p.source_id != self.source_id or p.page != self.page for p in self.provenance):
            raise ContractError("symbol proposal provenance must retain its inferred source binding")


    def to_dict(self):
        data = Portable.to_dict(self)
        if self.scope is None:
            data.pop("scope")
            data.pop("scope_sources")
        return data

    @property
    def fingerprint(self):
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateCandidate(Portable):
    id: str
    proposal_ids: tuple[str, str]
    reason: str
    review_state: str = "pending"

    def __post_init__(self):
        if self.review_state != "pending" or self.reason not in (
            "same_page_overlap","same_page_classification_conflict","cross_page_instance_label"):
            raise ContractError("duplicate candidates cannot establish equivalence")
        text(self.id,"duplicate candidate identity")
        _strings(self.proposal_ids,"duplicate proposals",maximum=2,empty=False)
        if len(self.proposal_ids) != 2 or tuple(sorted(set(self.proposal_ids))) != self.proposal_ids:
            raise ContractError("duplicate candidate needs two distinct ordered proposals")


def _semantic_signature(output):
    def point(value):
        return None if value is None else tuple(round(v,3) for v in value)
    records = []
    for mark in output.marks:
        records.append({"image_id":mark.image_id,"category":mark.category,
            "type":mark.type_proposal,"type_tag":mark.type_tag,"instance_label":mark.instance_label,
            "bounds":point(mark.bounds),"anchor":point(mark.anchor),
            "polyline":tuple(point(p) for p in mark.polyline),
            "scope":None if mark.scope is None else mark.scope.to_dict()})
    return digest(sorted(records,key=digest))


@dataclass(frozen=True, slots=True, kw_only=True)
class QualificationCase(_SymbolPortable):
    id: str
    request: SymbolRequest
    expected: SymbolOutput
    strong_receipt: AttemptReceipt
    cheap_receipt: AttemptReceipt

    def __post_init__(self):
        text(self.id,"qualification case identity")
        if not isinstance(self.request,SymbolRequest) or not isinstance(self.expected,SymbolOutput):
            raise ContractError("qualification needs frozen typed request/reference labels")
        self.expected.validate_for(self.request)
        for receipt,role in ((self.strong_receipt,"vision_sol"),(self.cheap_receipt,"vision_luna")):
            if (not isinstance(receipt,AttemptReceipt) or receipt.phase != "finish" or
                    receipt.role != role or receipt.request_fingerprint != self.request.fingerprint):
                raise ContractError("qualification receipt/request mismatch")

    @property
    def passes(self):
        for receipt in (self.strong_receipt,self.cheap_receipt):
            if receipt.status != "completed" or receipt.raw_output is None:
                return False
            try:
                output=SymbolOutput.from_json(receipt.raw_output)
                output.validate_for(self.request)
            except ContractError:
                return False
            if _semantic_signature(output) != _semantic_signature(self.expected):
                return False
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolQualification(_SymbolPortable):
    config: RoutingConfig
    expected_strong_model_id: str
    expected_cheap_model_id: str
    cases: tuple[QualificationCase, ...]
    content_fingerprint: str

    def __post_init__(self):
        if not isinstance(self.config,RoutingConfig):
            raise ContractError("qualification needs configured routing identities")
        text(self.expected_strong_model_id,"qualified strong identity")
        text(self.expected_cheap_model_id,"qualified cheap identity")
        _array(self.cases,QualificationCase,"qualification cases")
        if not 1 <= len(self.cases) <= 8 or len({c.id for c in self.cases}) != len(self.cases):
            raise ContractError("qualification cases must be distinct and bounded")
        signatures={case.request.task_signature for case in self.cases}
        if len(signatures) != 1:
            raise ContractError("qualification mixes task/prompt/context policies")
        calibration=SymbolRoutingPolicy(config=self.config,
            expected_strong_model_id=self.expected_strong_model_id,
            expected_cheap_model_id=self.expected_cheap_model_id)
        for case in self.cases:
            for receipt,endpoint,model in (
                (case.strong_receipt,self.config.vision_sol,self.expected_strong_model_id),
                (case.cheap_receipt,self.config.vision_luna,self.expected_cheap_model_id)):
                if receipt.endpoint != endpoint or receipt.policy_fingerprint != calibration.fingerprint:
                    raise ContractError("qualification endpoint/policy mismatch")
                if receipt.status == "completed" and receipt.resolved_model_id != model:
                    raise ContractError("qualification needs exact transport-reported model identities")
        data=_portable(self);data.pop("content_fingerprint")
        if self.content_fingerprint != digest(data):
            raise ContractError("qualification content binding mismatch")

    @classmethod
    def capture(cls, *, config, expected_strong_model_id, expected_cheap_model_id, cases):
        data=dict(config=config,expected_strong_model_id=expected_strong_model_id,
                  expected_cheap_model_id=expected_cheap_model_id,cases=cases)
        return cls(**data,content_fingerprint=digest(_portable(data)))

    def validate_for(self,request,policy):
        # A small paired record is an auditable experiment, not evidence of
        # task/category/context/target-composition coverage for routing.
        raise ContractError(CHEAPER_DISABLED)



@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRun(_SymbolPortable):
    version: str
    request: SymbolRequest
    policy: SymbolRoutingPolicy
    attempts: tuple[AttemptReceipt, ...]
    selected_attempt_id: str | None
    qualification: SymbolQualification | None = None
    accepted: bool = False
    review_state: str = "pending"

    def __post_init__(self):
        if self.version != VERSION or self.accepted is not False or self.review_state != "pending":
            raise ContractError("symbol run is always a versioned pending proposal")
        if not isinstance(self.request,SymbolRequest) or not isinstance(self.policy,SymbolRoutingPolicy):
            raise ContractError("symbol run requires typed request/policy")
        _array(self.attempts,AttemptReceipt,"symbol execution receipts")
        if not 2 <= len(self.attempts) <= 6 or len(self.attempts)%2:
            raise ContractError("symbol run needs complete bounded start/finish pairs")
        if self.policy.mode == "qualified_cheaper":
            if not isinstance(self.qualification,SymbolQualification):
                raise ContractError("cheap routing requires measured symbol qualification")
            self.qualification.validate_for(self.request,self.policy)
        elif self.qualification is not None:
            raise ContractError("qualification cannot implicitly change baseline/diagnostic routing")
        terminal=[]
        request_fp=self.request.fingerprint;policy_fp=self.policy.fingerprint
        for start,finish in zip(self.attempts[::2],self.attempts[1::2]):
            if (start.phase != "start" or finish.phase != "finish"
                    or (start.attempt_id,start.role,start.endpoint,start.reason) !=
                       (finish.attempt_id,finish.role,finish.endpoint,finish.reason)):
                raise ContractError("symbol attempt receipt pair mismatch")
            if datetime.fromisoformat(finish.timestamp_utc) < datetime.fromisoformat(start.timestamp_utc):
                raise ContractError("attempt finish precedes its durable start")
            for receipt in (start,finish):
                if (receipt.request_fingerprint != request_fp
                        or receipt.policy_fingerprint != policy_fp
                        or receipt.endpoint != self.policy.endpoint(receipt.role)):
                    raise ContractError("symbol receipt trusted dispatch binding mismatch")
            if finish.status == "completed":
                output=SymbolOutput.from_json(finish.raw_output)
                output.validate_for(self.request)
                expected=self.policy.expected_model(finish.role)
                if expected is not None and finish.resolved_model_id != expected:
                    raise ContractError("completed symbol receipt violates configured model identity")
            terminal.append(finish)
        if len({r.attempt_id for r in terminal}) != len(terminal):
            raise ContractError("symbol attempt identities must be distinct")
        roles=[r.role for r in terminal]
        mode=self.policy.mode
        first={"strong_baseline":"vision_sol","qualified_cheaper":"vision_luna","diagnostic":"diagnostic_astra"}[mode]
        if roles[0] != first:
            raise ContractError("symbol run started outside its explicit routing mode")
        for prior,current in zip(terminal,terminal[1:]):
            if current.role == "vision_sol":
                if prior.role != "vision_luna" or prior.status not in (
                    "invalid_output","identity_mismatch","unavailable","rate_limited","insufficient_confidence"):
                    raise ContractError("unjustified symbol strong escalation")
            elif current.role == "strong_fallback":
                if prior.role != "vision_sol" or prior.status not in ("unavailable","rate_limited"):
                    raise ContractError("strong fallback is availability-only")
            else:
                raise ContractError("implicit symbol retry/diagnostic escalation forbidden")
        completed=terminal[-1].status == "completed"
        if self.selected_attempt_id != (terminal[-1].attempt_id if completed else None):
            raise ContractError("selected symbol output contradicts terminal execution receipt")

    @property
    def status(self):
        if self.selected_attempt_id is None: return "failed"
        return "diagnostic" if self.policy.mode == "diagnostic" else "completed"

    @property
    def fingerprint(self):
        return digest(self.to_dict())

    @property
    def output(self):
        if self.selected_attempt_id is None: return None
        return SymbolOutput.from_json(self.attempts[-1].raw_output)

    def _verify_journal(self, journal_path):
        # The path comes from the trusted caller, never the portable run or model.
        if AttemptJournal.read(journal_path) != self.attempts:
            raise ContractError("symbol run receipts differ from the trusted local journal")

    def validate_against(self,request,policy,*,images,documents,journal_path):
        current=SymbolRun.from_dict(self.to_dict())
        if current.request != request or current.policy != policy:
            raise ContractError("saved symbol run is stale")
        current._verify_journal(journal_path)
        _validate_symbol_inputs(current.request,images,documents)
        return current

    def proposals(self,*,images,documents,journal_path):
        result=SymbolRun.from_dict(self.to_dict())
        result._verify_journal(journal_path)
        _validate_symbol_inputs(result.request,images,documents)
        if result.status != "completed":
            return ()
        images_by_id={im.id:im for im in result.request.images}
        receipt=result.attempts[-1]
        proposals=[]
        request_fp=result.request.fingerprint;result_fp=result.fingerprint
        missing=[f"missing_{c.role}: {c.rationale}" for c in result.request.contexts if c.status == "missing"]
        for mark in result.output.marks:
            image=images_by_id[mark.image_id];page=image.source
            x0,y0,x1,y1=image.crop_px
            def pixel(p): return (x0+p[0]*(x1-x0),y0+p[1]*(y1-y0))
            sx,_,_,sy,tx,ty=image.render_manifest.pixel_to_display_pt
            def source(p):
                # Unit-crop validation makes these mathematical page bounds;
                # clip only floating-point residue at exact terminal edges.
                return (min(page.width_pt,max(0,sx*p[0]*(x1-x0)+tx)),
                        min(page.height_pt,max(0,sy*p[1]*(y1-y0)+ty)))
            a,b,c,d=mark.bounds
            left,top=pixel((a,b));right,bottom=pixel((c,d))
            lo=source((a,d));hi=source((c,b))
            bounds_pt=(*lo,*hi)
            anchor=None if mark.anchor is None else source(mark.anchor)
            line=tuple(source(p) for p in mark.polyline)
            source_key={"document_sha256":page.document_sha256,"source_id":page.source_id,"page":page.page,
                        "bounds_pt":tuple(round(v,9) for v in bounds_pt),
                        "anchor_pt":None if anchor is None else tuple(round(v,9) for v in anchor),
                        "polyline_pt":tuple(tuple(round(v,9) for v in p) for p in line)}
            source_mark_id=stable_id("symbol-source-mark",digest(source_key))
            proposal_id=stable_id("symbol-proposal",digest({
                "result":result_fp,"mark":mark.to_dict()}))
            unknowns=list(mark.unknowns)+missing
            if mark.confidence < result.request.confidence_threshold:
                unknowns.append("below_configured_confidence_threshold")
            if receipt.resolved_model_id is None:
                unknowns.append("resolved_model_identity_unavailable")
            provenance=Provenance(source_kind="ai-proposed",source_id=page.source_id,
                source_element_id=source_mark_id,page=page.page,method=result.request.prompt_version,
                confidence=mark.confidence,derivation="inferred",attributes={
                    "request_fingerprint":request_fp,"result_fingerprint":result_fp,
                    "attempt_id":receipt.attempt_id,"requested_endpoint":_portable(receipt.endpoint),
                    "transport_reported_model_id":receipt.resolved_model_id,
                    "provider_request_id":receipt.provider_request_id,"image_id":mark.image_id,
                    "image_sha256":image.sha256,"render_manifest":image.render_manifest.to_dict()})
            proposals.append(SymbolProposal(proposal_id=proposal_id,source_mark_id=source_mark_id,
                source_id=page.source_id,source_page_id=page.id,image_id=mark.image_id,document_sha256=page.document_sha256,
                page=page.page,category=mark.category,type_proposal=mark.type_proposal,
                type_tag=mark.type_tag,instance_label=mark.instance_label,geometry_kind=mark.geometry_kind,
                bounds_px=(left,top,right,bottom),
                bounds_normalized=(left/page.width_px,top/page.height_px,right/page.width_px,bottom/page.height_px),
                bounds_pt=bounds_pt,anchor_pt=anchor,polyline_pt=line,confidence=mark.confidence,
                unknowns=tuple(dict.fromkeys(unknowns)),source_mark=mark.source_mark,
                evidence_image_ids=mark.evidence_image_ids,request_fingerprint=request_fp,
                result_fingerprint=result_fp,attempt_id=receipt.attempt_id,provenance=(provenance,),
                scope=mark.scope, scope_sources=tuple(images_by_id[key] for key in
                    (() if mark.scope is None else mark.scope.evidence_image_ids))))
        return tuple(sorted(proposals,key=lambda p:p.proposal_id))

    def duplicate_candidates(self,*,images,documents,journal_path,overlap_threshold=.5):
        threshold=_confidence(overlap_threshold)
        if threshold == 0: raise ContractError("duplicate overlap threshold must be positive")
        proposals=self.proposals(images=images,documents=documents,journal_path=journal_path)
        result=[]
        for index,a in enumerate(proposals):
            for b in proposals[index+1:]:
                if (a.source_id,a.document_sha256) != (b.source_id,b.document_sha256):
                    continue
                reason=None
                if a.page == b.page:
                    x0=max(a.bounds_pt[0],b.bounds_pt[0]);y0=max(a.bounds_pt[1],b.bounds_pt[1])
                    x1=min(a.bounds_pt[2],b.bounds_pt[2]);y1=min(a.bounds_pt[3],b.bounds_pt[3])
                    intersection=max(0,x1-x0)*max(0,y1-y0)
                    area=lambda box:(box[2]-box[0])*(box[3]-box[1])
                    union=area(a.bounds_pt)+area(b.bounds_pt)-intersection
                    if intersection/union >= threshold:
                        conflict=(a.category,a.type_proposal,a.geometry_kind)!=(b.category,b.type_proposal,b.geometry_kind)
                        reason="same_page_classification_conflict" if conflict else "same_page_overlap"
                elif (a.instance_label is not None and b.instance_label is not None
                      and a.instance_label.strip().casefold() == b.instance_label.strip().casefold()):
                    reason="cross_page_instance_label"
                if reason:
                    ids=tuple(sorted((a.proposal_id,b.proposal_id)))
                    result.append(DuplicateCandidate(id=stable_id("symbol-duplicate-candidate",
                        digest({"proposals":ids,"reason":reason})),proposal_ids=ids,reason=reason))
        return tuple(sorted(result,key=lambda item:item.id))


def _attempt(request,policy,role,reason,*,adapters,images,journal):
    endpoint=policy.endpoint(role)
    attempt_id=stable_id("symbol-attempt",f"{journal.run_id}:{len(journal.receipts)}")
    common=dict(attempt_id=attempt_id,role=role,endpoint=endpoint,
        request_fingerprint=request.fingerprint,policy_fingerprint=policy.fingerprint,reason=reason)
    start=AttemptReceipt(**common,phase="start",status="started",
                         timestamp_utc=datetime.now(timezone.utc).isoformat())
    journal.append(start)  # durability barrier before invoking any transport
    status="adapter_error";reply=None;error=None
    try:
        adapter=adapters.get(endpoint.adapter_id)
        if not callable(adapter):
            raise ContractError("configured symbol adapter is not registered")
        response=adapter(request.to_dict(),images=dict(images),
            provider_id=endpoint.provider_id,model_alias=endpoint.model_alias,
            prompt_version=request.prompt_version)
        if not isinstance(response,SymbolAdapterReply):
            raise ContractError("symbol adapter must return SymbolAdapterReply with transport metadata")
        reply=SymbolAdapterReply.from_dict(response.to_dict())
        expected=policy.expected_model(role)
        if expected is not None and reply.resolved_model_id != expected:
            status="identity_mismatch";error="expected_resolved_model_identity_missing_or_mismatched"
        else:
            output=SymbolOutput.from_json(reply.output_json)
            output.validate_for(request)
            status="completed"
            if role == "vision_luna" and any(mark.confidence < request.confidence_threshold for mark in output.marks):
                status="insufficient_confidence";error="below_symbol_confidence_threshold"
    except AdapterAvailabilityError as exc:
        try:
            status=_availability_reason(exc)
        except ContractError:
            status="adapter_error";error="invalid_typed_availability_reason"
    except ContractError as exc:
        status="invalid_output" if reply is not None else "adapter_error"
        error="domain_output_rejected" if reply is not None else "adapter_contract_error"
    except Exception as exc:
        status="adapter_error";error=type(exc).__name__
    finish=AttemptReceipt(**common,phase="finish",status=status,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        raw_output=None if reply is None else reply.output_json,
        resolved_model_id=None if reply is None else reply.resolved_model_id,
        provider_request_id=None if reply is None else reply.provider_request_id,error=error)
    journal.append(finish)  # failure here cannot return a successful proposal run
    return start,finish


def dispatch_symbols(request,policy,*,adapters,images,documents,journal,qualification=None):
    """Invoke the primary symbol stage for all supplied crops and categories.

    No deterministic-recognizer fallback gate precedes this call. Configured Sol
    role is the strong baseline. Provider transport is supplied by the caller.
    """
    if not isinstance(request,SymbolRequest) or not isinstance(policy,SymbolRoutingPolicy):
        raise ContractError("symbol dispatch requires typed request/routing policy")
    if not isinstance(journal,AttemptJournal):
        raise ContractError("symbol dispatch requires a durable AttemptJournal")
    request=SymbolRequest.from_dict(request.to_dict())
    policy=SymbolRoutingPolicy.from_dict(policy.to_dict())
    images=dict(images)
    if policy.mode == "qualified_cheaper":
        if not isinstance(qualification,SymbolQualification):
            raise ContractError("cheaper symbol routing requires measured qualification")
        qualification=SymbolQualification.from_dict(qualification.to_dict())
        qualification.validate_for(request,policy)
    elif qualification is not None:
        raise ContractError("qualification cannot silently change strong/diagnostic routing")
    _validate_symbol_inputs(request,images,documents)
    role={"strong_baseline":"vision_sol","qualified_cheaper":"vision_luna",
          "diagnostic":"diagnostic_astra"}[policy.mode]
    attempts=list(_attempt(request,policy,role,f"{policy.mode}:primary",
        adapters=adapters,images=images,journal=journal))
    if role == "vision_luna" and attempts[-1].status in (
            "invalid_output","identity_mismatch","rate_limited","unavailable","insufficient_confidence"):
        attempts.extend(_attempt(request,policy,"vision_sol","qualified_cheaper_unsatisfied",
            adapters=adapters,images=images,journal=journal))
    if (attempts[-1].role == "vision_sol" and attempts[-1].status in ("unavailable","rate_limited")
            and policy.strong_availability_fallback is not None):
        attempts.extend(_attempt(request,policy,"strong_fallback","strong_endpoint_unavailable",
            adapters=adapters,images=images,journal=journal))
    selected=attempts[-1].attempt_id if attempts[-1].status == "completed" else None
    return SymbolRun(version=VERSION,request=request,policy=policy,attempts=tuple(attempts),
                     selected_attempt_id=selected,qualification=qualification)


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolBenchmarkCase(_SymbolPortable):
    id: str
    request: SymbolRequest
    expected: SymbolOutput

    def __post_init__(self):
        text(self.id,"benchmark case identity")
        if not isinstance(self.request,SymbolRequest) or not isinstance(self.expected,SymbolOutput):
            raise ContractError("symbol benchmark requires typed frozen inputs/reference labels")
        self.expected.validate_for(self.request)


def measure_symbol_qualification(cases,*,config,expected_strong_model_id,expected_cheap_model_id,
                                 adapters,images_by_case,documents_by_case,journal):
    """Capture paired experiments; never authorize cheaper routing or pass labels to adapters.

    This strict first gate requires identical semantic/reference geometry after
    rounding normalized coordinates to .001, with complete one-to-one inventory.
    Current records do not establish task/category/context/target coverage.
    Qualified-cheaper routing remains disabled even when an experiment passes.
    """
    if not isinstance(journal,AttemptJournal):
        raise ContractError("symbol calibration requires a durable AttemptJournal")
    _array(cases,SymbolBenchmarkCase,"symbol benchmark cases")
    if not 1 <= len(cases) <= 8 or len({c.id for c in cases}) != len(cases):
        raise ContractError("symbol benchmark corpus must be bounded and distinct")
    if set(images_by_case) != {c.id for c in cases} or set(documents_by_case) != {c.id for c in cases}:
        raise ContractError("benchmark source bytes must exactly cover the frozen cases")
    text(expected_strong_model_id,"calibration strong model")
    text(expected_cheap_model_id,"calibration cheap model")
    policy=SymbolRoutingPolicy(config=config,expected_strong_model_id=expected_strong_model_id,
                               expected_cheap_model_id=expected_cheap_model_id)
    budget(_portable(cases))
    if len({c.request.task_signature for c in cases}) != 1:
        raise ContractError("symbol benchmark mixes task framing")
    frozen_images={case.id:dict(images_by_case[case.id]) for case in cases}
    frozen_documents={case.id:dict(documents_by_case[case.id]) for case in cases}
    for case in cases:
        _validate_symbol_inputs(case.request,frozen_images[case.id],frozen_documents[case.id])
    strong={}
    for case in cases:
        _,receipt=_attempt(case.request,policy,"vision_sol","qualification:strong_baseline",
            adapters=adapters,images=frozen_images[case.id],journal=journal)
        if (receipt.status != "completed" or
            _semantic_signature(SymbolOutput.from_json(receipt.raw_output)) != _semantic_signature(case.expected)):
            raise ContractError("strong symbol baseline did not satisfy the frozen qualification case")
        strong[case.id]=receipt
    trials=[]
    for case in cases:
        _,receipt=_attempt(case.request,policy,"vision_luna","qualification:cheap_comparison",
            adapters=adapters,images=frozen_images[case.id],journal=journal)
        trials.append(QualificationCase(id=case.id,request=case.request,expected=case.expected,
                                      strong_receipt=strong[case.id],cheap_receipt=receipt))
    return SymbolQualification.capture(config=config,expected_strong_model_id=expected_strong_model_id,
        expected_cheap_model_id=expected_cheap_model_id,cases=tuple(trials))
