"""Independent public synthetic scope assertions. Fake transport is not AI accuracy proof."""
import hashlib
import io
import json
from dataclasses import replace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.model import BuildingModel, ContractError, Provenance, RuleReference
from oabm.proposals.symbol_scope import SymbolScope, SymbolScopeClaim, SymbolScopeEvidence
from oabm.proposals.symbols import (
    SymbolMark, SymbolOutput, SymbolProposal, SymbolRun, SymbolAdapterReply,
    SymbolRequest, SymbolContext, AttemptJournal, VERSION, TASK, VALIDATOR,
)
from oabm.proposals.takeoff import render_source_image
from oabm.importers.pdf_electrical.symbol_stage import (
    extract_symbol_review, materialize_reviewed_symbols, SymbolDecision, SymbolPlacement,
)
from oabm.proposals.contracts import digest
from oabm.quantities import extract_quantities, QuantityError
from test_symbol_proposals import policy, fixture as legacy_fixture
from test_symbol_stage import model, registration

PHRASES = (
    "NEW FIXTURE - BASE BID",
    "REMOVE AND SALVAGE FOR RELOCATION - BASE BID",
    "EXISTING TO REMAIN - BASE BID",
    "REMOVE FIXTURE - BASE BID",
    "N.I.C. - BASE BID",
    "NEW FIXTURE - ALTERNATE ALT-1",
    "NEW OR RELOCATE - CONFLICTING TAGS - BASE BID",
    "AMBIGUOUS E R SYMBOL",
)


def claim(dimension, value, quote="NEW FIXTURE - BASE BID", *, image="notes", alternate_id=None):
    return SymbolScopeClaim(dimension=dimension, value=value, alternate_id=alternate_id,
        confidence=.4, evidence=(SymbolScopeEvidence(image_id=image,
            bounds=(0,0,1,1), quote=quote),))


def scope(work=None, bid=None, *, quote="NEW FIXTURE - BASE BID", alternate_id=None):
    return SymbolScope(claims=tuple(
        ([claim("work_status", work, quote)] if work else []) +
        ([claim("bid_scope", bid, quote, alternate_id=alternate_id)] if bid else [])))


def source_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 0 RG 1 w " + b" ".join(
        f"{20+i*40} 40 10 10 re S".encode() for i in range(len(PHRASES))))
    page[NameObject("/Contents")] = writer._add_object(stream)
    notes = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    notes[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = "BT /F1 8 Tf " + " ".join(
        f"1 0 0 1 10 {280-i*25} Tm ({phrase}) Tj" for i, phrase in enumerate(PHRASES)) + " ET"
    stream = DecodedStreamObject()
    stream.set_data(content.encode())
    notes[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.fixture
def case(tmp_path):
    document = source_pdf()
    # Evidence quotes actually exist in the independently authored PDF.
    extracted = PdfReader(io.BytesIO(document)).pages[1].extract_text()
    assert all(phrase in extracted for phrase in PHRASES)
    images, refs = {}, []
    for key, role, page in (("field","target",1), ("notes","notes",2)):
        ref, images[key] = render_source_image(document, source_id="synthetic:scope",
            page=page, scale=1, crop_px=(0,0,400,300), image_id=key, role=role)
        refs.append(ref)
    request = SymbolRequest(version=VERSION, task=TASK, validator_version=VALIDATOR,
        prompt_version="synthetic-scope-v1", question="Locate source symbols and cite explicit source scope text.",
        images=tuple(refs), target_image_ids=("field",),
        contexts=tuple(SymbolContext(role=role, status="supplied" if role=="notes" else "inapplicable",
            image_ids=("notes",) if role=="notes" else (), rationale="Explicit synthetic context.")
            for role in ("legend","schedule","notes")),
        allowed_types=("luminaire",), confidence_threshold=.8)
    scopes = [
        scope("new","base",quote=PHRASES[0]),
        scope("relocated","base",quote=PHRASES[1]),
        scope("existing_to_remain","base",quote=PHRASES[2]),
        scope("removed","base",quote=PHRASES[3]),
        scope("not_in_contract","base",quote=PHRASES[4]),
        scope("new","alternate",quote=PHRASES[5],alternate_id="ALT-1"),
        SymbolScope(claims=(claim("work_status","new",PHRASES[6]),
            claim("work_status","relocated",PHRASES[6]),claim("bid_scope","base",PHRASES[6]))),
        SymbolScope(claims=()),
    ]
    marks = tuple(SymbolMark(image_id="field", category="luminaire", type_proposal="luminaire",
        type_tag="E" if i==7 else None, instance_label=f"S-{i}",
        bounds=((20+i*40)/400,250/300,(30+i*40)/400,260/300),
        anchor=((25+i*40)/400,255/300), polyline=(), confidence=.9,
        source_mark="Independent synthetic square mark.", evidence_image_ids=("field","notes"),
        unknowns=(), scope=item) for i,item in enumerate(scopes))
    output = SymbolOutput(version=VERSION, marks=marks, scanned_image_ids=("field",),
        coverage_notes=("Synthetic bounded source only.",))
    documents = {hashlib.sha256(document).hexdigest(): document}
    journal = AttemptJournal.create(tmp_path/"scope-attempts.jsonl", run_id="synthetic-scope")
    def adapter(*args, **kwargs):
        return SymbolAdapterReply(output_json=output.to_json(), resolved_model_id="strong-actual")
    with journal:
        extraction = extract_symbol_review([request], policy(), adapters={"vision_sol":adapter},
            documents=documents, images=images, journals={request.fingerprint:journal})
    reg = registration(source_id="synthetic:scope", document_sha256=next(iter(documents)),
        source_bbox_pt=(0,0,400,300))
    decisions = []
    for row in extraction.review.proposals:
        i = int(row["instance_label"].split("-")[1])
        decisions.append(SymbolDecision(manifest_fingerprint=extraction.review.fingerprint,
            proposal_id=row["proposal_id"], proposal_fingerprint=digest(row),
            reviewer_id="synthetic-scope-review", reviewer_derivation="user", rationale="Reviewed source assertions.",
            action="accept", resolved_unknowns=tuple(row["unknowns"]),
            placement=SymbolPlacement(registration_fingerprint=reg.fingerprint,
                entity_kind="equipment" if i==4 else "device", entity_type="luminaire",
                anchor_pt=(25+i*40,45), height_above_level_m=1, yaw_radians=0, remaining_unknowns=())))
    args = dict(runs=extraction.runs, documents=documents, images=images,
        journal_paths={request.fingerprint:journal.path}, decisions=decisions, registrations=[reg])
    outcome = materialize_reviewed_symbols(model(), extraction.review, **args)
    return outcome, args, scopes


def entities_by_label(outcome):
    return {e.attributes["symbol_review"]["proposal"]["instance_label"]:e
            for e in (*outcome.model.electrical_devices,*outcome.model.electrical_equipment)}


def test_source_assertions_survive_raw_run_review_canonical_and_repeated_intake(case):
    outcome, args, scopes = case
    assert BuildingModel.from_json(outcome.model.to_json()) == outcome.model
    assert SymbolRun.from_json(args["runs"][0].to_json()) == args["runs"][0]
    entities = entities_by_label(outcome)
    for i, expected in enumerate(scopes):
        entity = entities[f"S-{i}"]
        row = entity.attributes["symbol_review"]["proposal"]
        assert SymbolProposal.from_dict(row).scope == expected
        assert entity.attributes["pdf_electrical"] == expected.canonical_metadata()
        records = [p for p in entity.provenance if p.method=="ai-symbol-source-scope-v1"]
        assert len(records) == sum(len(c.evidence) for c in expected.claims)
        assert all(p.page==2 and p.source_id=="synthetic:scope" and p.derivation=="inferred"
            and p.confidence==.4 and p.scope_paths==("attributes.pdf_electrical",) for p in records)
        assert all(p.attributes["document_sha256"] in args["documents"] for p in records)
        assert all(p.source_element_id != row["source_mark_id"] and
            p.attributes["target_source_mark_id"] == row["source_mark_id"] for p in records)
    repeated = materialize_reviewed_symbols(outcome.model, outcome.review, **args)
    assert repeated.model.to_json() == outcome.model.to_json()


def test_quantities_separate_new_reuse_remove_nic_alternates_and_conflicts(case):
    outcome, _, _ = case
    report = extract_quantities(outcome.model)
    entities = entities_by_label(outcome)
    assert sum(i.quantity for i in report.items) == 8
    base, = [i for i in report.items if i.group is None]
    assert base.source_entity_ids == (entities["S-0"].id,)
    assert base.confidence == .4
    assert dict(base.variant)["work_status"] == "new"
    assert dict(base.variant)["bid_scope"] == "base"
    for i in range(1,8):
        item, = [item for item in report.items if entities[f"S-{i}"].id in item.source_entity_ids]
        labels = json.loads(item.group.removeprefix("ai-symbol-scope:"))
        assert labels["work_status"] == ("relocated","existing_to_remain","removed",
            "not_in_contract","new","conflict","unresolved")[i-1]
    alternate, = [item for item in report.items if entities["S-5"].id in item.source_entity_ids]
    assert dict(alternate.variant)["alternate_id"] == "ALT-1"
    assert report.group_summary
    # A caller group cannot wash an alternate or reuse into an ungrouped base.
    grouped = extract_quantities(outcome.model, groups={"caller-base":list(entities[e].id for e in entities)})
    assert all(item.group is not None for item in grouped.items)
    assert sum(item.quantity for item in grouped.items) == 8


def test_existing_ledger_reuses_statuses_without_new_when_unmarked_fallback(case):
    from oabm.reports import ScopePolicy, generate_scope_ledger
    outcome, _, _ = case
    report = generate_scope_ledger(outcome.model, policy=ScopePolicy(version="1.0.0",
        mode="new_when_confirmed_unmarked", actor="synthetic-rule",
        basis=RuleReference(authority="synthetic", revision="1", rule_id="unmarked"),
        provenance=(Provenance(source_kind="user",source_id="synthetic:rule",derivation="user"),)))
    by_id = {row.source.entity["id"]:row for row in report.rows}
    entities = entities_by_label(outcome)
    for i in (4,6,7):
        assert by_id[entities[f"S-{i}"].id].effective_status == "unresolved"
    assert by_id[entities["S-1"].id].effective_status == "relocated"
    assert by_id[entities["S-3"].id].effective_status == "removed"


def test_canonical_scope_edits_fail_closed_instead_of_counting_alternative_as_base(case):
    outcome, _, _ = case
    data = outcome.model.to_dict()
    entity = next(e for e in data["electrical_devices"]
        if e["attributes"]["symbol_review"]["proposal"]["instance_label"]=="S-5")
    entity["attributes"]["pdf_electrical"]["bid_scope"] = "base"
    with pytest.raises(QuantityError, match="contradicts"):
        extract_quantities(BuildingModel.from_dict(data))


def test_old_outputs_and_proposal_fingerprints_keep_absent_optional_scope():
    _, _, _, output = legacy_fixture()
    old = output.to_dict()
    assert "scope" not in old["marks"][0]
    assert SymbolOutput.from_dict(old).to_dict() == old
    assert SymbolMark.from_dict(old["marks"][0]).scope is None


def test_bare_type_tags_never_classify_work_or_bid_scope(case):
    outcome, _, _ = case
    entity = entities_by_label(outcome)["S-7"]
    assert entity.attributes["symbol_review"]["proposal"]["type_tag"] == "E"
    assert entity.attributes["pdf_electrical"]["scope_status"] == "unresolved"
    assert entity.attributes["pdf_electrical"]["bid_scope"] == "unresolved"


def test_bid_conflicts_include_conflicting_alternate_ids_and_never_choose_one():
    a = claim("bid_scope","alternate",alternate_id="ALT-A")
    b = claim("bid_scope","alternate",alternate_id="ALT-B")
    for claims in ((a,b),(b,a),(a,claim("bid_scope","base"))):
        result = SymbolScope(claims=claims)
        assert result.bid_scope=="conflict" and result.alternate_id is None
        assert SymbolScope.from_json(result.to_json()) == result


@pytest.mark.parametrize("changes", [
    {"value":"unknown"}, {"value":"salvage"}, {"confidence":True},
    {"confidence":float("nan")}, {"evidence":()}, {"evidence":[]}, {"alternate_id":"ALT-A"},
])
def test_invalid_assertions_rejected_consistently(changes):
    with pytest.raises(ContractError):
        replace(claim("work_status","new"), **changes)


def test_invalid_image_binding_and_non_normalized_evidence_rejected():
    request, _, _, output = legacy_fixture()
    bad = SymbolScope(
        claims=(claim("work_status","new",image="not-present"),))
    with pytest.raises(ContractError, match="supplied source"):
        replace(output.marks[0],scope=bad).validate_for(request)
    with pytest.raises(ContractError):
        SymbolScopeEvidence(image_id="field",bounds=(0,0,400,300),quote="NEW")
    data = scope("new").to_dict()
    data["selected"] = True
    with pytest.raises(ContractError):
        SymbolScope.from_dict(data)


def test_portable_scope_schema_matches_supported_and_rejected_claims():
    import jsonschema
    from oabm.proposals.symbol_scope import symbol_scope_schema
    request, _, _, _ = legacy_fixture()
    good = scope("new", "alternate", alternate_id="ALT-A")
    # This legacy fixture has legend/schedule but no notes image.
    good = replace(good, claims=tuple(replace(c, evidence=tuple(
        replace(e,image_id="legend") for e in c.evidence)) for c in good.claims))
    schema = symbol_scope_schema(request)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert "uniqueItems" not in json.dumps(schema)
    duplicate = good.to_dict()
    duplicate["claims"].append(duplicate["claims"][0])
    jsonschema.validate(duplicate,schema)  # Provider schema is structural only.
    with pytest.raises(ContractError, match="duplicate"):
        SymbolScope.from_dict(duplicate)
    jsonschema.validate(good.to_dict(),schema)
    jsonschema.validate(SymbolScope(claims=()).to_dict(),schema)
    for update in ({"value":"base","alternate_id":"ALT-A"},
                   {"value":"alternate","alternate_id":None},
                   {"dimension":"work_status","value":"base"},
                   {"confidence":True}):
        bad = good.to_dict()
        bad["claims"][1].update(update)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad,schema)
        with pytest.raises(ContractError):
            SymbolScope.from_dict(bad)


def test_saved_run_scope_edit_cannot_match_original_trusted_journal(case):
    outcome, args, _ = case
    data = args["runs"][0].to_dict()
    raw = json.loads(data["attempts"][-1]["raw_output"])
    raw["marks"][5]["scope"]["claims"][1]["alternate_id"] = "ALT-FORGED"
    data["attempts"][-1]["raw_output"] = json.dumps(raw)
    forged = SymbolRun.from_dict(data)
    with pytest.raises(ContractError, match="journal"):
        materialize_reviewed_symbols(model(), outcome.review,
            **{**args,"runs":[forged]})


def test_legacy_reviewed_ai_inventory_without_scope_stays_outside_base():
    from test_symbol_stage import apply
    legacy = apply().model
    report = extract_quantities(legacy)
    item, = report.items
    assert dict(item.variant)["work_status"] == "unresolved"
    assert dict(item.variant)["bid_scope"] == "unresolved"
    assert item.group.startswith("ai-symbol-scope:")
    assert "pdf_electrical" not in legacy.electrical_devices[0].attributes


def test_non_ai_inventory_keeps_plain_base_and_original_variant():
    from test_symbol_stage import apply
    legacy = apply().model
    entity = replace(legacy.electrical_devices[0], attributes={})
    ordinary = replace(legacy, electrical_devices=(entity,))
    report = extract_quantities(ordinary)
    item, = report.items
    assert item.group is None and report.group_summary is None
    assert "work_status" not in dict(item.variant)
    assert "groups" not in report.to_dict()
    assert not any(w.code=="ai_symbol_scope_inventory" for w in report.warnings)
