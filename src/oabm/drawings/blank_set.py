"""Job metadata plus an ordered sheet list to blank populated-titleblock sheets."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re

import oabm
from .sheets import SheetSpec, compose_blank_sheet
from .sheet_svg import render_blank_sheet_svg

VERSION = "blank-sheet-set-v1"
_PAPER = {"width_mm", "height_mm", "margin_mm", "titleblock_height_mm"}
_RESERVED = {"sheet", "sheet_id", "sheet_number", "title", "sheet_title", "revision", "date"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PDF_LIMITATION = "Combined PDF is unsupported: this repository has no supported SVG/DXF-to-PDF converter; pypdf does not convert SVG."


def _json(value):
    return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+"\n").encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _text(value, label, *, empty=False):
    if not isinstance(value,str) or (not empty and not value.strip()) or any(ord(c)<32 for c in value):
        raise ValueError(f"{label}: supply a {'single-line' if empty else 'nonempty single-line'} string")
    return value


def _identifier(value,label):
    _text(value,label)
    if not _ID.fullmatch(value):
        raise ValueError(f"{label}: use 1-64 letters/digits/periods/underscores/hyphens, starting with a letter or digit")
    return value


def _date(value,label):
    _text(value,label)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value):
        raise ValueError(f"{label}: supply an explicit ISO YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}: invalid calendar date") from exc
    return value


def _fields(value,label):
    if not isinstance(value,dict):
        raise ValueError(f"{label}: expected a key/value object")
    folded=set()
    for key,item in value.items():
        _text(key,label+" field name")
        _text(item,f"{label}.{key}",empty=True)
        if key.casefold() in _RESERVED:
            raise ValueError(f"{label}.{key}: reserved; use sheet id/title and explicit revision/date properties")
        if key.casefold() in folded:
            raise ValueError(f"{label}: field names must be unique ignoring case")
        folded.add(key.casefold())
    return dict(value)


def _decode(raw):
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:
                raise ValueError(f"duplicate config key {key!r}")
            out[key]=value
        return out
    try:
        return json.loads(raw,object_pairs_hook=unique)
    except (UnicodeError,json.JSONDecodeError) as exc:
        raise ValueError("blank-sheet config must be UTF-8 JSON") from exc


def resolve_blank_sheet_set(config):
    """Validate an explicit request; preserve sheet-list order and field origins."""
    keys={"version","set_id","job","paper","revision","date","formats","sheets"}
    if not isinstance(config,dict) or set(config)!=keys or config["version"]!=VERSION:
        raise ValueError("supply exact blank-sheet-set-v1 keys: "+", ".join(sorted(keys)))
    _identifier(config["set_id"],"set_id")
    job=_fields(config["job"],"job")
    revision=_text(config["revision"],"revision")
    supplied_date=_date(config["date"],"date")
    paper=config["paper"]
    if not isinstance(paper,dict) or set(paper)!=_PAPER:
        raise ValueError("paper: explicitly supply width_mm, height_mm, margin_mm and titleblock_height_mm")
    formats=config["formats"]
    if not isinstance(formats,list) or not formats or any(not isinstance(f,str) for f in formats):
        raise ValueError("formats: expected a unique list including svg, optionally dxf")
    if "pdf" in formats:
        raise ValueError(PDF_LIMITATION)
    if len(set(formats))!=len(formats) or "svg" not in formats or not set(formats)<={"svg","dxf"}:
        raise ValueError("formats: include svg and optionally dxf, without duplicates")
    sheets=config["sheets"]
    if not isinstance(sheets,list) or not sheets:
        raise ValueError("sheets: supply a nonempty ordered list")
    resolved=[]
    seen=set()
    for ordinal,row in enumerate(sheets,1):
        where=f"sheets[{ordinal-1}]"
        if not isinstance(row,dict) or not {"id","title"}<=set(row) or set(row)-{"id","title","fields","paper","revision","date"}:
            raise ValueError(where+": id/title required; optional fields, paper, revision, date only")
        identity=_identifier(row["id"],where+".id")
        where+=f" ({identity})"
        if identity.casefold() in seen:
            raise ValueError(where+": duplicate sheet id ignoring case")
        seen.add(identity.casefold())
        title=_text(row["title"],where+".title")
        fields=_fields(row.get("fields",{}),where+".fields")
        merged={**job,**fields}
        if len({key.casefold() for key in merged})!=len(merged):
            raise ValueError(where+": use the exact shared field spelling to override it; case-only duplicates are ambiguous")
        date_value=_date(row.get("date",supplied_date),where+".date")
        revision_value=_text(row.get("revision",revision),where+".revision")
        paper_override=row.get("paper",{})
        if not isinstance(paper_override,dict) or set(paper_override)-_PAPER:
            raise ValueError(where+".paper: only explicit paper dimension/margin/titleblock overrides are supported")
        effective_paper={**paper,**paper_override}
        for key,value in effective_paper.items():
            try:
                good=type(value) in (int,float) and math.isfinite(value) and value>=0
            except OverflowError:
                good=False
            if not good:
                raise ValueError(where+f".paper.{key}: supply a finite nonnegative number in millimetres")
        if effective_paper["titleblock_height_mm"]<=0:
            raise ValueError(where+": titleblock_height_mm must be positive for populated blank sheets")
        try:
            spec=SheetSpec(id=identity,title=title,revision=revision_value,
                           titleblock_fields=tuple({**merged,"Date":date_value}.items()),**effective_paper)
        except ValueError as exc:
            raise ValueError(where+": "+str(exc)) from exc
        resolved.append({"ordinal":ordinal,"spec":spec,"date":date_value,
                         "field_sources":{**{key:"sheet.fields" if key in fields else "job" for key in merged},
                                          "Date":"sheet.date" if "date" in row else "date"},
                         "revision_source":"sheet.revision" if "revision" in row else "revision",
                         "stem":f"{ordinal:03d}-{identity}"})
    return tuple(resolved)


def _runtime():
    root=Path(oabm.__file__).parent
    sources=[{"path":p.relative_to(root).as_posix(),"sha256":_sha(p.read_bytes())} for p in sorted(root.rglob("*.py"))]
    return {"python":platform.python_version(),"platform":platform.system(),"oabm_module_version":oabm.__version__,
            "engine_sources":sources,"engine_sources_sha256":_sha(_json(sources))}


class BlankSheetSetError(ValueError):
    def __init__(self,attempt,stage,cause):
        self.attempt_directory=Path(attempt)
        self.stage=stage
        super().__init__(f"blank sheet set failed at {stage}: {cause}; attempt retained at {attempt}")


def create_blank_sheet_set(config_path, *, output_directory):
    """Publish a fresh ordered blank set, with exact config and hash receipts.

    No model/review input, external resource, clock date, logo or stamp is used.
    Keep exclusive ownership of attempt contents until publication. Ordinary
    failures retain a pending attempt and never publish a completed delivery.
    """
    raw=Path(config_path).read_bytes()
    config=_decode(raw)
    sheets=resolve_blank_sheet_set(config)
    attempt=Path(output_directory).absolute()
    attempt.parent.mkdir(parents=True,exist_ok=True)
    attempt.mkdir()
    pending=attempt/".pending"
    stage="snapshot"
    try:
        pending.mkdir()
        receipts=[]
        def write(name,data,kind):
            path=pending/name
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("xb") as stream:stream.write(data)
            receipts.append({"path":name,"kind":kind,"bytes":len(data),"sha256":_sha(data)})
        write("config.json",raw,"editable_input")
        resolved=[]
        for item in sheets:
            spec=item["spec"]
            prefix="sheets/"+item["stem"]
            resolved.append({**{k:v for k,v in item.items() if k!="spec"},"spec":asdict(spec)})
            stage=f"sheet {spec.id} composition"
            write(prefix+".sheet.json",(compose_blank_sheet(spec).to_json()+"\n").encode(),"blank_composition")
            for fmt in config["formats"]:
                stage=f"sheet {spec.id} {fmt} titleblock"
                try:
                    if fmt=="svg":
                        artifact=render_blank_sheet_svg(spec)
                        data=artifact.svg.encode()
                    else:
                        from .sheet_dxf import blank_sheet_to_dxf
                        artifact=blank_sheet_to_dxf(spec)
                        data=artifact.dxf
                except (ValueError,ImportError) as exc:
                    raise ValueError(str(exc)+"; check titleblock_height_mm, row count, field length and requested format support") from exc
                write(prefix+"."+fmt,data,"blank_"+fmt)
                write(prefix+"."+fmt+".map.json",(artifact.manifest_json+"\n").encode(),"source_map")
        write("resolved-sheets.json",_json(resolved),"resolved_input")
        write("READ-ME.txt",("BLANK SHEET SET - NO DESIGN CONTENT\n"+PDF_LIMITATION+"\n"
             "Generic bottom titleblock only. Supplied job fields are not approval evidence.\n"
             "Keep source config private when it contains private job information.\n").encode(),"notice")
        stage="verify"
        if {p.relative_to(pending).as_posix() for p in pending.rglob("*") if p.is_file()}!={r["path"] for r in receipts}:
            raise ValueError("unexpected pending output")
        for row in receipts:
            data=(pending/row["path"]).read_bytes()
            if len(data)!=row["bytes"] or _sha(data)!=row["sha256"]:
                raise ValueError("output changed before publication")
        manifest={"version":VERSION,"set_id":config["set_id"],"status":"blank_no_design_content",
                  "model_id":None,"engineering_approval":False,"template":"generic-bottom-titleblock-v1",
                  "sheet_order":[s["spec"].id for s in sheets],"formats":config["formats"],
                  "pdf":{"status":"unsupported","reason":PDF_LIMITATION},"sheets":resolved,
                  "runtime":_runtime(),"files":sorted(receipts,key=lambda r:r["path"])}
        (pending/"MANIFEST.json").write_bytes(_json(manifest))
        stage="publish"
        os.rename(pending,attempt/"delivery")
        return attempt/"delivery"
    except Exception as exc:
        try:
            (attempt/"failure.json").write_bytes(_json({"version":VERSION,"stage":stage,"error_type":type(exc).__name__}))
        except OSError:
            pass
        raise BlankSheetSetError(attempt,stage,exc) from exc


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config",type=Path,help="explicit job metadata and ordered blank-sheet list JSON")
    parser.add_argument("output_directory",type=Path,help="fresh attempt directory; never overwritten")
    args=parser.parse_args(argv)
    try:
        path=create_blank_sheet_set(args.config,output_directory=args.output_directory)
    except (ValueError,OSError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(path)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
