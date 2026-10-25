"""Independent public synthetic scope assertions. Fake transport is not AI accuracy proof."""
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

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


_NO_SCOPE_OVERRIDE = object()


def build_case(tmp_path, duplicate_scope=_NO_SCOPE_OVERRIDE, primary_scope=_NO_SCOPE_OVERRIDE):
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
    if primary_scope is not _NO_SCOPE_OVERRIDE:
        scopes[0] = primary_scope
    marks = tuple(SymbolMark(image_id="field", category="luminaire", type_proposal="luminaire",
        type_tag="E" if i==7 else None, instance_label=f"S-{i}",
        bounds=((20+i*40)/400,250/300,(30+i*40)/400,260/300),
        anchor=((25+i*40)/400,255/300), polyline=(), confidence=.9,
        source_mark="Independent synthetic square mark.", evidence_image_ids=("field","notes"),
        unknowns=(), scope=item) for i,item in enumerate(scopes))
    if duplicate_scope is not _NO_SCOPE_OVERRIDE:
        marks = (marks[0], replace(marks[0], scope=duplicate_scope,
            source_mark="Second source interpretation of the same synthetic mark."))
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
    if duplicate_scope is not _NO_SCOPE_OVERRIDE:
        survivor, = [r for r in extraction.review.proposals if r["instance_label"] == "S-0"
                     and r["source_mark"] == "Independent synthetic square mark."]
    for row in extraction.review.proposals:
        if duplicate_scope is not _NO_SCOPE_OVERRIDE and row["instance_label"] == "S-0" and row != survivor:
            decisions.append(SymbolDecision(manifest_fingerprint=extraction.review.fingerprint,
                proposal_id=row["proposal_id"], proposal_fingerprint=digest(row),
                reviewer_id="synthetic-scope-review", reviewer_derivation="user",
                rationale="Duplicate physical identity only; source scope is not resolved.",
                action="duplicate", duplicate_of=survivor["proposal_id"]))
            continue
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


@pytest.fixture
def case(tmp_path):
    return build_case(tmp_path)


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
    from oabm.model import ElectricalDevice, Point3, Pose
    entity = ElectricalDevice(id='device:ordinary', device_type='luminaire',
        pose=Pose(position=Point3(x=1, y=1, z=1)),
        provenance=(Provenance(source_kind='synthetic', source_id='fixture:ordinary', derivation='user'),))
    ordinary = BuildingModel(model_id='model:ordinary', electrical_devices=(entity,))
    report = extract_quantities(ordinary)
    item, = report.items
    assert item.group is None and report.group_summary is None
    assert "work_status" not in dict(item.variant)
    assert "groups" not in report.to_dict()
    assert not any(w.code=="ai_symbol_scope_inventory" for w in report.warnings)


@pytest.mark.parametrize('remove_all', [False, True])
def test_missing_review_metadata_cannot_erase_ai_origin(case, remove_all):
    outcome, _, _ = case
    if remove_all:
        from test_symbol_stage import apply
        # Historical AI rows have no scope-qualified owner paths. Removing all
        # metadata therefore passes canonical validation but must not become base.
        data = apply().model.to_dict()
    else:
        data = outcome.model.to_dict()
    entity = data['electrical_devices'][0]
    if remove_all:
        entity['attributes'] = {}
    else:
        del entity['attributes']['symbol_review']
    with pytest.raises(QuantityError, match='source proposal'):
        extract_quantities(BuildingModel.from_dict(data))


def test_coordinated_scope_copies_cannot_rewrite_original_review(case):
    outcome, _, _ = case
    data = outcome.model.to_dict()
    entity = next(e for e in data['electrical_devices']
        if e['attributes']['symbol_review']['proposal']['instance_label'] == 'S-5')
    changed = scope('new', 'base', quote=PHRASES[0])
    entity['attributes']['symbol_review']['proposal']['scope'] = changed.to_dict()
    entity['attributes']['pdf_electrical'] = changed.canonical_metadata()
    with pytest.raises(QuantityError, match='review|source'):
        extract_quantities(BuildingModel.from_dict(data))


def test_duplicate_scope_copies_cannot_rewrite_original_evidence(tmp_path):
    from oabm.proposals.symbol_scope import scope_from_proposals
    outcome, _, _ = build_case(tmp_path, duplicate_scope=scope('new', 'alternate', quote=PHRASES[5], alternate_id='ALT-1'))
    data = outcome.model.to_dict()
    entity = data['electrical_devices'][0]
    review = entity['attributes']['symbol_review']
    review['scope_equivalent_proposals'][0]['scope'] = scope('new', 'base', quote=PHRASES[0]).to_dict()
    entity['attributes']['pdf_electrical'] = scope_from_proposals(
        (review['proposal'], *review['scope_equivalent_proposals'])).canonical_metadata()
    with pytest.raises(QuantityError, match='review|source'):
        extract_quantities(BuildingModel.from_dict(data))


@pytest.mark.parametrize('retained_origin', ['scope_evidence', 'scope_marker'])
def test_partial_provenance_loss_cannot_erase_ai_scope(case, retained_origin):
    outcome, _, _ = case
    data = outcome.model.to_dict()
    entity = next(e for e in data['electrical_devices']
        if e['attributes']['symbol_review']['proposal']['instance_label'] == 'S-5')
    del entity['attributes']['symbol_review']
    entity['provenance'] = ([p for p in entity['provenance']
        if p['method'] == 'ai-symbol-source-scope-v1'] if retained_origin == 'scope_evidence' else [])
    if retained_origin == 'scope_evidence':
        del entity['attributes']['pdf_electrical']['scope_method']
    changed = BuildingModel.from_dict(data)
    with pytest.raises(QuantityError, match='source proposal'):
        extract_quantities(changed)


def test_duplicate_citation_crop_cannot_change_without_retained_evidence(tmp_path):
    outcome, _, _ = build_case(tmp_path, duplicate_scope=scope('relocated', 'base', quote=PHRASES[1]))
    data = outcome.model.to_dict()
    peer = data['electrical_devices'][0]['attributes']['symbol_review']['scope_equivalent_proposals'][0]
    image = peer['scope_sources'][0]
    image['crop_px'] = [100, 100, 300, 200]
    image['render_manifest']['pixel_to_display_pt'] = [1, 0, 0, -1, 100, 200]
    with pytest.raises(QuantityError, match='source|evidence'):
        extract_quantities(BuildingModel.from_dict(data))


def test_scope_image_evidence_is_complete_and_detached(case):
    import copy
    from oabm.proposals.symbol_scope import source_scope_provenance
    outcome, _, _ = case
    row = copy.deepcopy(next(p for p in outcome.review.proposals if p.get('scope', {}).get('claims')))
    records = source_scope_provenance((row,))
    original = copy.deepcopy(row['scope_sources'][0])
    assert records[0].attributes['source_image'] == original
    row['scope_sources'][0]['render_manifest']['scale'] = 99
    assert records[0].attributes['source_image'] == original


@pytest.mark.parametrize('change', ['peer_text', 'extra_peer'])
def test_duplicate_peer_snapshots_require_independent_review_binding(tmp_path, change):
    import copy
    outcome, _, _ = build_case(tmp_path, duplicate_scope=SymbolScope(claims=()))
    data = outcome.model.to_dict()
    review = data['electrical_devices'][0]['attributes']['symbol_review']
    peer = review['scope_equivalent_proposals'][0]
    if change == 'peer_text':
        peer['source_mark'] = 'A different unreviewed source description.'
    else:
        added = copy.deepcopy(peer)
        added['proposal_id'] = 'symbol-proposal:unreviewed-extra'
        review['scope_equivalent_proposals'].append(added)
    with pytest.raises(QuantityError, match='review|source'):
        extract_quantities(BuildingModel.from_dict(data))


def test_extra_nic_reason_agrees_with_ledger_and_cannot_enter_new_base(case):
    from oabm.reports import ScopePolicy, generate_scope_ledger
    outcome, _, _ = case
    data = outcome.model.to_dict()
    source = next(e for e in data["electrical_devices"]
        if e["attributes"]["symbol_review"]["proposal"]["instance_label"] == "S-0")
    source["attributes"]["pdf_electrical"]["scope_reason"] = "not_in_contract"
    changed = BuildingModel.from_dict(data)
    ledger = generate_scope_ledger(changed, policy=ScopePolicy(version="1.0.0",
        mode="preserve_unresolved", actor="synthetic-rule",
        basis=RuleReference(authority="synthetic", revision="1", rule_id="scope-conflict"),
        provenance=(Provenance(source_kind="user", source_id="synthetic:rule", derivation="user"),)))
    row, = [r for r in ledger.rows if r.source.entity["id"] == source["id"]]
    assert row.effective_status == "unresolved"
    assert row.diagnostic_reason == "contradictory_scope_evidence"
    with pytest.raises(QuantityError, match="contradicts"):
        extract_quantities(changed)


def test_complete_ai_scope_metadata_cannot_accept_extra_marker_or_legend_facts(case):
    outcome, _, _ = case
    for extras in ({"status":"E"}, {"scope_marker_source_element_id":"synthetic-marker"},
                   {"scope_legend_source_element_ids":["synthetic-legend"]},
                   {"scope_legend_text":" "}, {"scope_reason":None},
                   {"scope_marker_unrecognized":"D"}, {"status_meaning":"existing"}):
        data = outcome.model.to_dict()
        source = next(e for e in data["electrical_devices"]
            if e["attributes"]["symbol_review"]["proposal"]["instance_label"] == "S-0")
        source["attributes"]["pdf_electrical"].update(extras)
        with pytest.raises(QuantityError, match="contradicts"):
            extract_quantities(BuildingModel.from_dict(data))


@pytest.mark.parametrize("duplicate_scope, expected_work, expected_bid", [
    (scope("relocated", "base", quote=PHRASES[1]), "conflict", "base"),
    (scope("new", "alternate", quote=PHRASES[5], alternate_id="ALT-1"), "new", "conflict"),
    (scope("not_in_contract", "base", quote=PHRASES[4]), "conflict", "base"),
])
def test_trusted_duplicate_identity_cannot_discard_conflicting_scope(
        tmp_path, duplicate_scope, expected_work, expected_bid):
    outcome, args, _ = build_case(tmp_path, duplicate_scope=duplicate_scope)
    originals = [r for r in outcome.review.proposals if r["instance_label"] == "S-0"]
    assert len(originals) == 2
    assert len({r["source_mark_id"] for r in originals}) == 1
    entity = entities_by_label(outcome)["S-0"]
    assert len(outcome.model.electrical_devices) + len(outcome.model.electrical_equipment) == 1
    report = extract_quantities(outcome.model)
    item, = [i for i in report.items if entity.id in i.source_entity_ids]
    assert item.group is not None
    assert dict(item.variant)["work_status"] == expected_work
    assert dict(item.variant)["bid_scope"] == expected_bid
    assert item.quantity == 1
    source_review = entity.attributes["symbol_review"]
    assert source_review["proposal"]["scope"] == scope("new", "base", quote=PHRASES[0]).to_dict()
    equivalent, = source_review["scope_equivalent_proposals"]
    assert equivalent["scope"] == duplicate_scope.to_dict()
    source_ids = {r["proposal_id"] for r in originals}
    records = [p for p in entity.provenance if p.method == "ai-symbol-source-scope-v1"]
    assert {p.attributes["source_proposal_id"] for p in records} == source_ids
    assert any(p.attributes["claim"] == c.to_dict() for p in records for c in duplicate_scope.claims)
    duplicate, = [d for d in outcome.dispositions if d["status"] == "duplicate"]
    assert duplicate["canonical_id"] == entity.id
    repeated = materialize_reviewed_symbols(outcome.model, outcome.review, **args)
    assert repeated.model == outcome.model
    assert BuildingModel.from_json(outcome.model.to_json()) == outcome.model


@pytest.mark.parametrize("primary, peer, expected", [
    (None, scope("relocated", "base", quote=PHRASES[1]), "relocated"),
    (scope("relocated", "base", quote=PHRASES[1]), None, "relocated"),
    (SymbolScope(claims=()), scope("relocated", "base", quote=PHRASES[1]), "relocated"),
    (None, None, "unresolved"),
])
def test_duplicate_peers_with_missing_scope_preserve_available_source_facts(tmp_path, primary, peer, expected):
    outcome, args, _ = build_case(tmp_path, duplicate_scope=peer, primary_scope=primary)
    entity = entities_by_label(outcome)["S-0"]
    item, = [i for i in extract_quantities(outcome.model).items if entity.id in i.source_entity_ids]
    assert item.quantity == 1 and item.group is not None
    assert dict(item.variant)["work_status"] == expected
    if primary is None and peer is None:
        assert "pdf_electrical" not in entity.attributes
        assert "scope_equivalent_proposals" not in entity.attributes["symbol_review"]
    else:
        original = entity.attributes["symbol_review"]["proposal"]
        assert original.get("scope") == (None if primary is None else primary.to_dict())
        equivalent, = entity.attributes["symbol_review"]["scope_equivalent_proposals"]
        assert equivalent.get("scope") == (None if peer is None else peer.to_dict())
    repeated = materialize_reviewed_symbols(outcome.model, outcome.review, **args)
    assert repeated.model == outcome.model


def test_nested_symbol_artifacts_preserve_explicit_unknown_scope():
    from oabm.proposals.symbols import SymbolBenchmarkCase
    request, _, _, output = legacy_fixture()
    expected = replace(output, marks=(replace(output.marks[0], scope=SymbolScope(claims=())),))
    case = SymbolBenchmarkCase(id="synthetic-scoped-audit", request=request, expected=expected)
    assert case.to_dict()["expected"]["marks"][0]["scope"] == {"claims":[], "version":"1.0.0"}
    assert SymbolBenchmarkCase.from_json(case.to_json()) == case



def test_baseline_generated_no_scope_qualification_keeps_exact_bytes_and_binding():
    from oabm.proposals.symbols import SymbolQualification, QualificationCase
    raw = (Path(__file__).parents[1] / "fixtures/proposals/legacy-no-scope-qualification.json").read_text()
    assert hashlib.sha256(raw.encode()).hexdigest() == "efc1e3784fa6d7d2dceaec202536c335ad149f5c9e0b6e258fcf88281634b947"
    loaded = SymbolQualification.from_json(raw)
    assert loaded.content_fingerprint == "fe9ec9a5145e2ec98bf399f7619f7d6c92c63bfb49f3742d62843eba8f28e212"
    assert loaded.to_json() == raw
    assert all(case.passes for case in loaded.cases)
    assert loaded.cases[0].expected.marks[0].scope is None
    assert "scope" not in loaded.to_dict()["cases"][0]["expected"]["marks"][0]
    assert QualificationCase.from_dict(json.loads(raw)["cases"][0]).to_dict() == json.loads(raw)["cases"][0]
    changed = json.loads(raw)
    changed["content_fingerprint"] = "0" * 64
    with pytest.raises(ContractError, match="content binding mismatch"):
        SymbolQualification.from_dict(changed)
