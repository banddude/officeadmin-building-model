"""Explicit saved physical sheets; generated artifacts only, never source originals."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import io
import json
import zipfile

from oabm.model import ContractError
from oabm.proposals.contracts import Portable, digest, text
from oabm.drawings.model import DrawingPrimitive, Point2
from oabm.drawings.sheets import SheetSpec, ViewPlacement
from .review_drawings import ReviewDrawingConfig, BANNER

VERSION = "review-sheet-delivery-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewSheet(Portable):
    paper: SheetSpec
    placements: tuple[ViewPlacement, ...]
    formats: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.paper, SheetSpec):
            raise ContractError("typed physical paper required")
        if not isinstance(self.placements, tuple) or not 1 <= len(self.placements) <= 64 or any(not isinstance(p, ViewPlacement) for p in self.placements):
            raise ContractError("one to 64 explicit placements required")
        if len({p.id for p in self.placements}) != len(self.placements):
            raise ContractError("duplicate sheet placement")
        if not isinstance(self.formats, tuple) or not self.formats or len(set(self.formats)) != len(self.formats) or not set(self.formats) <= {"svg", "dxf"}:
            raise ContractError("explicit unique svg/dxf formats required")
        # This status must remain readable, not merely hidden in metadata.
        if self.paper.title != BANNER or self.paper.titleblock_height_mm < 25:
            raise ContractError("physical review sheets require the incomplete-review title and a titleblock of at least 25 mm")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewGlyph(Portable):
    token: str
    segments_mm: tuple[tuple[Point2, Point2], ...]

    def __post_init__(self):
        text(self.token, "explicit glyph token")
        from oabm.drawings.sheet_svg import SheetSymbol
        if not isinstance(self.segments_mm, tuple) or len(self.segments_mm) > 128:
            raise ContractError("glyph segment budget exceeded")
        SheetSymbol(self.segments_mm)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewSheetDeliveryConfig(Portable):
    version: str
    drawings: ReviewDrawingConfig
    sheets: tuple[ReviewSheet, ...]
    glyphs: tuple[ReviewGlyph, ...]

    def __post_init__(self):
        if self.version != VERSION or not isinstance(self.drawings, ReviewDrawingConfig):
            raise ContractError("explicit supported sheet delivery version and drawing config required")
        if not isinstance(self.sheets, tuple) or not 1 <= len(self.sheets) <= 32 or any(not isinstance(s, ReviewSheet) for s in self.sheets):
            raise ContractError("one to 32 physical sheets required")
        if len({s.paper.id for s in self.sheets}) != len(self.sheets):
            raise ContractError("duplicate physical sheet ID")
        if not isinstance(self.glyphs, tuple) or len(self.glyphs) > 128 or any(not isinstance(g, ReviewGlyph) for g in self.glyphs):
            raise ContractError("explicit bounded glyph tuple required")
        if len({g.token for g in self.glyphs}) != len(self.glyphs):
            raise ContractError("duplicate glyph token")
        known = {p.id for p in self.drawings.plans}
        if any(p.view_id not in known for s in self.sheets for p in s.placements):
            raise ContractError("physical sheet references unknown configured view")

    @property
    def plans(self):
        return self.drawings.plans

    @property
    def artifact_kinds(self):
        return (*self.drawings.artifact_kinds, *self.sheet_filenames)

    @property
    def sheet_filenames(self):
        result = {"sheet_package": "generated-sheet-sources.zip", "sheet_delivery": "sheet-delivery.json"}
        for i, sheet in enumerate(sorted(self.sheets, key=lambda s: s.paper.id), 1):
            for fmt in sorted(sheet.formats):
                key = "physical_sheet:" + sheet.paper.id + ":" + fmt
                result[key] = f"physical-sheet-{i:03d}.{fmt}"
                result[key + ":map"] = f"physical-sheet-{i:03d}.{fmt}.json"
        return result


def _lower_glyphs(drawing, config):
    """Explicit paper glyphs become derived lines, never canonical geometry."""
    registry = {g.token: g for g in config.glyphs}
    placed = {p.view_id for s in config.sheets for p in s.placements}
    views, joins = [], []
    for view in drawing.views:
        primitives = []
        for primitive in view.primitives:
            if primitive.kind != "symbol" or view.id not in placed:
                primitives.append(primitive)
                continue
            if primitive.text is not None or primitive.closed or primitive.symbol not in registry:
                raise ContractError("physical sheet symbol requires an explicit glyph and no unplaced symbol text: " + str(primitive.symbol))
            anchor = primitive.points[0]
            generated = []
            for index, segment in enumerate(registry[primitive.symbol].segments_mm):
                # Sheet Y is down, drawing Y is up; glyph size is fixed on paper.
                points = tuple(Point2(anchor.x + p.x * view.scale / 1000,
                                      anchor.y - p.y * view.scale / 1000) for p in segment)
                if points[0] == points[1]:
                    raise ContractError("glyph segment collapses at drawing precision")
                identity = "sheet-glyph:" + digest([view.id, primitive.id, index])
                generated.append(identity)
                primitives.append(DrawingPrimitive(id=identity, kind="line", layer=primitive.layer,
                    source_ids=primitive.source_ids, points=points, style=replace(primitive.style, stroke="electrical"),
                    metadata=(*primitive.metadata, ("source_symbol_id", primitive.id), ("source_symbol_token", primitive.symbol))))
            joins.append({"view_id": view.id, "symbol_id": primitive.id, "token": primitive.symbol,
                "source_ids": list(primitive.source_ids), "line_ids": generated})
        views.append(replace(view, primitives=tuple(primitives)))
    return replace(drawing, views=tuple(views)), joins


def generate_sheet_delivery(drawing, config, payloads, status, *, _dxf_replay_versions=None):
    """Return a deterministic, self-describing generated-source archive and files."""
    from oabm.drawings.sheet_svg import render_sheet_svg
    from .sheet_coherence import validate_sheet_inputs
    validate_sheet_inputs(drawing, config, payloads, status)
    lowered, joins = _lower_glyphs(drawing, config)
    output = {}
    filenames = config.sheet_filenames
    for sheet in sorted(config.sheets, key=lambda s: s.paper.id):
        for fmt in sorted(sheet.formats):
            key = "physical_sheet:" + sheet.paper.id + ":" + fmt
            if fmt == "svg":
                rendered = render_sheet_svg(lowered, sheet.paper, sheet.placements)
                data = rendered.svg.encode()
            else:
                from oabm.drawings.sheet_dxf import sheet_to_dxf, _sheet_to_dxf
                if _dxf_replay_versions is None:
                    rendered = sheet_to_dxf(lowered, sheet.paper, sheet.placements)
                else:
                    rendered = _sheet_to_dxf(lowered, sheet.paper, sheet.placements,
                        version=_dxf_replay_versions[sheet.paper.id])
                data = rendered.dxf
            output[key] = data
            output[key + ":map"] = rendered.manifest_json.encode()
    # Keep complete owner-relative provenance from the original drawing document,
    # not the narrower generic renderer SourceProvenance representation.
    members = {"drawing-set.json": payloads["drawing_set"], "panel-facts.json": payloads["panel_facts"],
        "sheet-config.json": _json(config.to_dict()), "review-status.json": _json(status),
        "rendered-drawing-set.json": lowered.to_json(indent=None).encode()}
    members.update({filenames[k]: data for k, data in output.items()})
    receipt = {"version": VERSION, "status": "incomplete_review", "permit_ready": False,
        "canonical_model_sha256": status["canonical_model_sha256"],
        "config_fingerprint": digest(config.to_dict()),
        "original_drawing_document_sha256": _sha(payloads["drawing_set"]),
        "rendered_drawing_set_sha256": _sha(lowered.to_json(indent=None).encode()),
        "symbol_lowering": joins, "glyph_basis": "caller_supplied_paper_mm_top_left_y_down_no_rotation",
        "source_originals_included": False,
        "preview": "not_generated", "review": status,
        "files": [{"path": name, "sha256": _sha(data), "bytes": len(data)} for name, data in sorted(members.items())]}
    output["sheet_delivery"] = _json(receipt)
    members["sheet-delivery.json"] = output["sheet_delivery"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    output["sheet_package"] = stream.getvalue()
    return output
