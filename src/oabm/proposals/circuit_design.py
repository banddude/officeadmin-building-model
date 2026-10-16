"""Bounded AI circuit choices; explicit trusted review owns design authorization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
import weakref
from types import MappingProxyType

from oabm.model import BuildingModel, ContractError, CoordinateSystem, Provenance, RuleReference, stable_id
from oabm.importers.pdf_electrical.placement_assertions import PlacementEvidence, materialize_asserted_symbols
from oabm.pipeline.reviewed_circuit_design import ReviewedCircuitRequest, ReviewedLoadPort, _profile
from oabm.routing import CircuitDesignRules, RoutingOptions
from oabm.reports.panel_schedules import SourceSnapshot, ENTITY_TYPES
from .contracts import Portable, RoutingConfig, digest, fingerprint, text, budget, _portable
from .dispatch import AdapterAvailabilityError, _availability_reason
from .takeoff import SourceImage, _validate_rendered_images
from .symbols import AttemptJournal, AttemptReceipt, SymbolAdapterReply, SymbolResourceLimits

VERSION = '1.0.0'
TASK = 'propose_preliminary_circuit_groups'
VALIDATOR = 'bounded-circuit-choices-v1'
CONTEXT_ROLES = ('plan','legend','fixture_schedule','panel_schedule','notes','one_line','riser','detail','project_rules')
PLACEMENT_KEYS = {'runs','documents','images','journal_paths','decisions','registrations','assertion_review','evidence_documents'}
CircuitAdapterReply = SymbolAdapterReply  # Transport metadata/budget only; never SymbolOutput or its prompt.
_JOURNAL_GUARD = threading.Lock()
_JOURNAL_CLAIMS = weakref.WeakSet()


def _fresh_journal(journal, *, claim=False):
    # One immutable execution owns a journal. The reservation also closes the
    # same-object concurrent-dispatch window before its durable start is written.
    with _JOURNAL_GUARD:
        if journal in _JOURNAL_CLAIMS or journal.receipts or AttemptJournal.read(journal.path):
            raise ContractError('circuit dispatch requires a new empty attempt journal')
        if claim:_JOURNAL_CLAIMS.add(journal)


def _rows(values, cls, label, maximum=512):
    if not isinstance(values,tuple) or len(values)>maximum or any(not isinstance(v,cls) for v in values):
        raise ContractError('bounded typed '+label+' required')


def _strings(values, label, maximum=512):
    _rows(values,str,label,maximum)
    for value in values:text(value,label)
    if len(values)!=len(set(values)):raise ContractError('duplicate '+label)


def _confidence(value):
    from .contracts import _finite
    value=_finite(value,'proposal confidence')
    if not 0<=value<=1:raise ContractError('proposal confidence outside [0,1]')
    return value


def _document(evidence, documents):
    data=documents.get(evidence.document_sha256)
    if (evidence.kind!='project_decision' or evidence.provenance.derivation!='user'
            or not isinstance(data,bytes) or not 1<=len(data)<=128_000_000
            or hashlib.sha256(data).hexdigest()!=evidence.document_sha256):
        raise ContractError('exact caller-authorized decision evidence required')


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitImage(SourceImage):
    def __post_init__(self):
        if self.role not in CONTEXT_ROLES:raise ContractError('unsupported circuit image role')
        SourceImage(id=self.id,source=self.source,role='floor_context',crop_px=self.crop_px,
                    sha256=self.sha256,render_manifest=self.render_manifest)

    @classmethod
    def capture(cls, image, *, role):
        return cls.from_dict({**image.to_dict(),'role':role})


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitContext(Portable):
    role: str
    status: str
    image_ids: tuple[str,...]
    profile_ids: tuple[str,...]
    rationale: str

    def __post_init__(self):
        if self.role not in CONTEXT_ROLES or self.status not in ('supplied','missing','inapplicable'):
            raise ContractError('invalid circuit context role/status')
        _strings(self.image_ids,'context image IDs',24);_strings(self.profile_ids,'context profile IDs',32)
        text(self.rationale,'context rationale',1024)
        if self.profile_ids and self.role!='project_rules':raise ContractError('profiles only bind project-rule context')
        if (self.status=='supplied')!=bool(self.image_ids or self.profile_ids):
            raise ContractError('supplied context needs exact bindings; unavailable context cannot cite inputs')


@dataclass(frozen=True,slots=True,kw_only=True)
class MissingInput(Portable):
    field: str
    proposal_id: str | None
    reason: str
    needed: str

    def __post_init__(self):
        if self.field not in (*CONTEXT_ROLES,'placement','source_port','approved_profile','connector','source_fact','eligibility'):
            raise ContractError('unsupported missing-input field')
        if self.proposal_id is not None:text(self.proposal_id,'missing-input proposal')
        text(self.reason,'missing-input reason',1024);text(self.needed,'required evidence',1024)


@dataclass(frozen=True,slots=True,kw_only=True)
class ApprovedCircuitProfile(Portable):
    id: str
    rules: dict
    routing_controls: dict
    rule_references: tuple[RuleReference,...]
    approval: PlacementEvidence

    def __post_init__(self):
        text(self.id,'approved profile ID')
        for values,cls,label in ((self.rules,CircuitDesignRules,'circuit_rules'),(self.routing_controls,RoutingOptions,'routing_controls')):
            _,missing=_profile(values,cls,label)
            if missing:raise ContractError('approved profile must be complete')
        _rows(self.rule_references,RuleReference,'rule references',64)
        if not self.rule_references or not isinstance(self.approval,PlacementEvidence):
            raise ContractError('approved profile needs rule references and caller decision evidence')


@dataclass(frozen=True,slots=True,kw_only=True)
class ApprovedConnector(Portable):
    id: str
    proposal_id: str
    offset_canonical_m: tuple[float,float,float]
    direction: tuple[float,float,float]
    reference: RuleReference
    approval: PlacementEvidence

    def __post_init__(self):
        port=ReviewedLoadPort(id=self.id,proposal_id=self.proposal_id,
            offset_canonical_m=self.offset_canonical_m,direction=self.direction)
        object.__setattr__(self,'offset_canonical_m',port.offset_canonical_m)
        object.__setattr__(self,'direction',port.direction)
        if not isinstance(self.reference,RuleReference) or not isinstance(self.approval,PlacementEvidence):
            raise ContractError('connector needs explicit caller basis/evidence')


@dataclass(frozen=True,slots=True,kw_only=True)
class ReviewAuthority(Portable):
    id: str
    kind: str
    authorization_document_sha256: str
    configuration_fingerprint: str | None = None

    def __post_init__(self):
        text(self.id,'authorized reviewer ID');fingerprint(self.authorization_document_sha256,'review authorization')
        if self.kind not in ('human','configured_agent'):raise ContractError('unsupported review authority kind')
        if self.kind=='configured_agent':
            if not self.id.startswith('agent:'):raise ContractError('configured agent identity must use agent namespace')
            fingerprint(self.configuration_fingerprint,'authorized agent configuration')
        elif self.configuration_fingerprint is not None:raise ContractError('human authority cannot claim agent configuration')


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitReviewPolicy(Portable):
    authorities: tuple[ReviewAuthority,...]
    approved_choices_fingerprint: str

    def __post_init__(self):
        fingerprint(self.approved_choices_fingerprint,'authorized circuit choices')
        _rows(self.authorities,ReviewAuthority,'review authorities',32)
        if not self.authorities or len({a.id for a in self.authorities})!=len(self.authorities):
            raise ContractError('distinct explicit review authorities required')

    @classmethod
    def capture(cls, *, authorities, source_port_ids, connectors, profiles):
        return cls(authorities=authorities,approved_choices_fingerprint=_choices_fingerprint(source_port_ids,connectors,profiles))

    @property
    def fingerprint(self):return digest(self.to_dict())


def _choices_fingerprint(source_port_ids,connectors,profiles):
    return digest(_portable({'source_port_ids':source_port_ids,'connectors':connectors,'profiles':profiles}))


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitRoutingPolicy(Portable):
    config: RoutingConfig
    mode: str = 'strong_baseline'
    expected_model_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.config,RoutingConfig) or self.mode not in ('strong_baseline','diagnostic'):
            raise ContractError('circuit task requires explicit strong/diagnostic configuration; no inherited qualification')
        if self.expected_model_id is not None:text(self.expected_model_id,'expected transport identity')

    @property
    def role(self):return 'vision_sol' if self.mode=='strong_baseline' else 'diagnostic_astra'
    @property
    def endpoint(self):return getattr(self.config,self.role)
    @property
    def fingerprint(self):return digest(self.to_dict())


def _state(model, review, placement):
    if not isinstance(placement,dict) or set(placement)!=PLACEMENT_KEYS:
        raise ContractError('exact existing placement input set required')
    outcome=materialize_asserted_symbols(model,review,**placement)
    eligible=tuple(sorted((d['proposal_id'],d['canonical_id']) for d in outcome.dispositions
        if d['status']=='accept' and d['canonical_id'] in {e.id for e in outcome.model.electrical_devices}))
    return outcome,eligible


def _sources(model,eligible,source_ports):
    devices={e.id:e for e in model.electrical_devices};ports={e.id:e for e in model.ports}
    equipment={e.id:e for e in model.electrical_equipment}
    levels={devices[identity].level_id for _,identity in eligible}
    for identity in source_ports:
        port=ports.get(identity);owner=equipment.get(port.owner_id) if port else None
        if port is None or port.role!='source' or port.domain!='electrical' or owner is None or owner.equipment_type!='panelboard' or owner.level_id is None:
            raise ContractError('source choices must name existing electrical panel source ports with known levels')
        levels.add(owner.level_id)
    document=model.to_dict();sources=[]
    for collection in ENTITY_TYPES:
        for entity in document.get(collection,[]):
            included=entity['id'] in levels if collection=='levels' else entity.get('level_id') is None or entity['level_id'] in levels
            if included:sources.append(SourceSnapshot.from_source_data(collection,entity))
    if len(sources)>512:raise ContractError('circuit context exceeds entity limit; explicit scope partition required')
    return tuple(sorted(sources,key=lambda s:(s.collection,s.id)))



@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitSourceBinding(Portable):
    proposal_id: str
    source_id: str
    document_sha256: str
    page: int
    bounds_pt: tuple[float,float,float,float]

    def __post_init__(self):
        from .contracts import _finite, integer
        text(self.proposal_id,'source proposal');text(self.source_id,'source document')
        fingerprint(self.document_sha256,'source PDF');integer(self.page,'original source page',maximum=100000)
        if not isinstance(self.bounds_pt,tuple) or len(self.bounds_pt)!=4:raise ContractError('source bounds need four points')
        values=tuple(_finite(v,'source bound') for v in self.bounds_pt)
        if not (values[0]<values[2] and values[1]<values[3]):raise ContractError('source bounds must have positive area')
        object.__setattr__(self,'bounds_pt',values)


def _proposal_sources(review,eligible):
    rows={r['proposal_id']:r for r in review.proposals}
    return tuple(CircuitSourceBinding(proposal_id=p,source_id=rows[p]['source_id'],
        document_sha256=rows[p]['document_sha256'],page=rows[p]['page'],bounds_pt=tuple(rows[p]['bounds_pt'])) for p,_ in eligible)


def _image_box_pt(image,bounds=(0.,0.,1.,1.)):
    a,b,c,d,e,f=image.render_manifest.pixel_to_display_pt
    width=image.crop_px[2]-image.crop_px[0];height=image.crop_px[3]-image.crop_px[1]
    x0,y0,x1,y1=bounds
    points=[(a*x+b*y+e,c*x+d*y+f) for x,y in ((x0*width,y0*height),(x1*width,y0*height),
        (x0*width,y1*height),(x1*width,y1*height))]
    return min(x for x,y in points),min(y for x,y in points),max(x for x,y in points),max(y for x,y in points)


def _same_page(image,binding):
    return (image.source.source_id,image.source.document_sha256,image.source.page)==(binding.source_id,binding.document_sha256,binding.page)


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitProposalRequest(Portable):
    version: str
    task: str
    validator_version: str
    prompt_version: str
    base_model_fingerprint: str
    admitted_model_fingerprint: str
    symbol_review_fingerprint: str
    assertion_review_fingerprint: str
    routing_policy_fingerprint: str
    review_policy_fingerprint: str
    model_id: str
    coordinate_system: CoordinateSystem
    eligible: tuple[tuple[str,str],...]
    placement_dispositions: tuple[dict,...]
    proposal_sources: tuple[CircuitSourceBinding,...]
    sources: tuple[SourceSnapshot,...]
    source_port_ids: tuple[str,...]
    connectors: tuple[ApprovedConnector,...]
    profiles: tuple[ApprovedCircuitProfile,...]
    images: tuple[CircuitImage,...]
    contexts: tuple[CircuitContext,...]
    resource_limits: SymbolResourceLimits = SymbolResourceLimits()

    def __post_init__(self):
        if (self.version,self.task,self.validator_version)!=(VERSION,TASK,VALIDATOR):raise ContractError('unsupported circuit proposal task')
        text(self.prompt_version,'circuit prompt version');text(self.model_id,'canonical model ID')
        for key in ('base_model_fingerprint','admitted_model_fingerprint','symbol_review_fingerprint',
                    'assertion_review_fingerprint','routing_policy_fingerprint','review_policy_fingerprint'):
            fingerprint(getattr(self,key),key)
        if not isinstance(self.coordinate_system,CoordinateSystem):raise ContractError('canonical coordinate frame required')
        if (not isinstance(self.eligible,tuple) or len(self.eligible)>128
                or any(not isinstance(row,tuple) or len(row)!=2 for row in self.eligible)):
            raise ContractError('bounded eligible proposal/canonical pairs required')
        for row in self.eligible:
            for value in row:text(value,'eligible identity')
        if len({r[0] for r in self.eligible})!=len(self.eligible) or len({r[1] for r in self.eligible})!=len(self.eligible):
            raise ContractError('duplicate eligible proposal/device')
        _rows(self.proposal_sources,CircuitSourceBinding,'eligible source references',128)
        if tuple(r.proposal_id for r in self.proposal_sources)!=tuple(p for p,_ in self.eligible):raise ContractError('source bindings must cover eligible devices exactly')
        _rows(self.placement_dispositions,dict,'placement dispositions');_rows(self.sources,SourceSnapshot,'canonical context')
        _strings(self.source_port_ids,'source port choices',32)
        _rows(self.connectors,ApprovedConnector,'connector choices',256);_rows(self.profiles,ApprovedCircuitProfile,'profile choices',32)
        _rows(self.images,CircuitImage,'circuit source images',24);_rows(self.contexts,CircuitContext,'circuit contexts',len(CONTEXT_ROLES))
        if sorted(c.role for c in self.contexts)!=sorted(CONTEXT_ROLES):raise ContractError('every circuit context role needs explicit coverage')
        for rows in (self.profiles,self.connectors,self.images):
            if len({r.id for r in rows})!=len(rows):raise ContractError('duplicate circuit choice/image ID')
        if any(c.proposal_id not in {r[0] for r in self.eligible} for c in self.connectors):raise ContractError('connector for ineligible proposal')
        images={im.id:im for im in self.images};profiles={p.id for p in self.profiles}
        for context in self.contexts:
            if set(context.image_ids)!={im.id for im in self.images if im.role==context.role} or set(context.profile_ids)-profiles:
                raise ContractError('context must exactly bind all images and only supplied profiles')
        if self.eligible and next(c for c in self.contexts if c.role=='plan').status=='inapplicable':raise ContractError('eligible source devices require supplied or explicitly missing plan context')
        rules=next(c for c in self.contexts if c.role=='project_rules')
        if set(rules.profile_ids)!=profiles:raise ContractError('project-rule context must enumerate every approved profile')
        if not isinstance(self.resource_limits,SymbolResourceLimits):raise ContractError('explicit circuit resource limits required')
        self.resource_limits.validate_manifest(self.images);budget(_portable(self))

    @property
    def fingerprint(self):return digest(self.to_dict())

    @property
    def required_missing_inputs(self):
        rows=[MissingInput(field=c.role,proposal_id=None,reason=c.rationale,needed='Explicit relevant source/context.')
              for c in self.contexts if c.status=='missing']
        if not self.eligible:rows.append(MissingInput(field='eligibility',proposal_id=None,reason='No admitted devices available.',needed='Reviewed physical placements.'))
        if not self.source_port_ids:rows.append(MissingInput(field='source_port',proposal_id=None,reason='No supplied panel source port.',needed='Existing reviewed panel/source port.'))
        if not self.profiles:rows.append(MissingInput(field='approved_profile',proposal_id=None,reason='No complete approved profile.',needed='Explicit caller-approved rules and routing controls.'))
        for proposal,_ in self.eligible:
            if not any(c.proposal_id==proposal for c in self.connectors):
                rows.append(MissingInput(field='connector',proposal_id=proposal,reason='No supplied connector template.',needed='Approved connector offset and direction.'))
        for row in self.placement_dispositions:
            if row['status'] in ('defer','pending'):
                rows.append(MissingInput(field='placement',proposal_id=row['proposal_id'],reason=row['reason'],needed='Resolve upstream placement evidence.'))
        return tuple(rows)

    @classmethod
    def capture(cls, model, review, *, placement, policy, review_policy, source_port_ids, connectors,
                profiles, images, contexts, prompt_version='bounded-circuit-choices-v1',resource_limits=None):
        if not isinstance(policy,CircuitRoutingPolicy) or not isinstance(review_policy,CircuitReviewPolicy):raise ContractError('typed routing/review policies required')
        outcome,eligible=_state(model,review,placement)
        if review_policy.approved_choices_fingerprint!=_choices_fingerprint(source_port_ids,connectors,profiles):
            raise ContractError('choices differ from trusted caller approval policy')
        return cls.from_dict(_portable(dict(version=VERSION,task=TASK,validator_version=VALIDATOR,prompt_version=prompt_version,
            base_model_fingerprint=digest(model.to_dict()),admitted_model_fingerprint=digest(outcome.model.to_dict()),
            symbol_review_fingerprint=review.fingerprint,assertion_review_fingerprint=placement['assertion_review'].fingerprint,
            routing_policy_fingerprint=policy.fingerprint,review_policy_fingerprint=review_policy.fingerprint,
            model_id=model.model_id,coordinate_system=model.coordinate_system,eligible=eligible,
            placement_dispositions=outcome.dispositions,proposal_sources=_proposal_sources(review,eligible),sources=_sources(outcome.model,eligible,source_port_ids),
            source_port_ids=source_port_ids,connectors=connectors,profiles=profiles,images=images,contexts=contexts,
            resource_limits=resource_limits or SymbolResourceLimits())))

    def validate_inputs(self, *, model, review, placement, policy, review_policy, images, documents, policy_documents):
        if not isinstance(policy,CircuitRoutingPolicy) or not isinstance(review_policy,CircuitReviewPolicy):raise ContractError('typed routing/review policies required')
        outcome,eligible=_state(model,review,placement)
        if review_policy.approved_choices_fingerprint!=_choices_fingerprint(self.source_port_ids,self.connectors,self.profiles):
            raise ContractError('choices differ from trusted caller approval policy')
        if (self.base_model_fingerprint!=digest(model.to_dict()) or self.admitted_model_fingerprint!=digest(outcome.model.to_dict())
                or self.symbol_review_fingerprint!=review.fingerprint or self.assertion_review_fingerprint!=placement['assertion_review'].fingerprint
                or self.routing_policy_fingerprint!=policy.fingerprint or self.review_policy_fingerprint!=review_policy.fingerprint
                or self.model_id!=model.model_id or self.coordinate_system!=model.coordinate_system or self.eligible!=eligible
                or list(self.placement_dispositions)!=list(outcome.dispositions) or self.proposal_sources!=_proposal_sources(review,eligible) or self.sources!=_sources(outcome.model,eligible,self.source_port_ids)):
            raise ContractError('stale circuit source/model/profile/review binding')
        self.resource_limits.validate_bytes(images);_validate_rendered_images(self.images,images,documents)
        if next(c for c in self.contexts if c.role=='plan').status=='supplied':
            for binding in self.proposal_sources:
                x0,y0,x1,y1=binding.bounds_pt
                boxes=[_image_box_pt(im) for im in self.images if im.role=='plan' and _same_page(im,binding)]
                if not any(a-1e-8<=x0<x1<=c+1e-8 and b-1e-8<=y0<y1<=d+1e-8 for a,b,c,d in boxes):
                    raise ContractError('plan context does not cover the eligible source page/mark')
        for choice in (*self.connectors,*self.profiles):_document(choice.approval,policy_documents)
        for authority in review_policy.authorities:
            data=policy_documents.get(authority.authorization_document_sha256)
            if not isinstance(data,bytes) or not 1<=len(data)<=128_000_000 or hashlib.sha256(data).hexdigest()!=authority.authorization_document_sha256:
                raise ContractError('missing configured review authorization bytes')
        return outcome


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitCitation(Portable):
    image_id: str
    bounds: tuple[float,float,float,float]
    quote: str

    def __post_init__(self):
        from .takeoff import _unit_box
        text(self.image_id,'circuit evidence image');text(self.quote,'circuit source interpretation',1024)
        object.__setattr__(self,'bounds',_unit_box(self.bounds,'circuit evidence bounds'))


@dataclass(frozen=True,slots=True,kw_only=True)
class DeviceCircuitChoice(Portable):
    proposal_id: str
    status: str
    connector_id: str | None
    rationale: str
    confidence: float
    evidence: tuple[CircuitCitation,...]

    def __post_init__(self):
        text(self.proposal_id,'device proposal');text(self.rationale,'device circuit rationale',1024)
        if self.status not in ('grouped','deferred','excluded'):raise ContractError('invalid device circuit disposition')
        if (self.status=='grouped')!=(self.connector_id is not None):raise ContractError('only grouped devices select connectors')
        if self.connector_id is not None:text(self.connector_id,'connector choice')
        _rows(self.evidence,CircuitCitation,'circuit citations',24)
        if self.status=='grouped' and not self.evidence:raise ContractError('grouped choice needs source citations')
        object.__setattr__(self,'confidence',_confidence(self.confidence))


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitProposalOutput(Portable):
    version: str
    source_port_id: str | None
    profile_id: str | None
    groups: tuple[tuple[str,...],...]
    devices: tuple[DeviceCircuitChoice,...]
    missing_inputs: tuple[MissingInput,...]

    def __post_init__(self):
        if self.version!=VERSION:raise ContractError('unsupported circuit output')
        for value in (self.source_port_id,self.profile_id):
            if value is not None:text(value,'circuit selected ID')
        _rows(self.devices,DeviceCircuitChoice,'device choices',128);_rows(self.missing_inputs,MissingInput,'missing facts',512)
        if not isinstance(self.groups,tuple) or len(self.groups)>128:raise ContractError('bounded circuit groups required')
        for group in self.groups:
            _strings(group,'circuit group',128)
            if not group:raise ContractError('empty circuit group')

    @property
    def fingerprint(self):return digest(self.to_dict())

    def validate_for(self, request):
        eligible={p for p,_ in request.eligible};devices={d.proposal_id:d for d in self.devices}
        if len(devices)!=len(self.devices) or set(devices)!=eligible:raise ContractError('every eligible device needs exactly one disposition')
        flat=[p for g in self.groups for p in g]
        if len(flat)!=len(set(flat)) or set(flat)!={d.proposal_id for d in self.devices if d.status=='grouped'}:
            raise ContractError('circuit groups must exactly cover grouped devices without duplicates')
        if self.source_port_id is not None and self.source_port_id not in request.source_port_ids:raise ContractError('invented source port')
        profiles={p.id:p for p in request.profiles};connectors={c.id:c for c in request.connectors}
        if self.profile_id is not None and self.profile_id not in profiles:raise ContractError('invented rule profile')
        if flat and (self.source_port_id is None or self.profile_id is None):raise ContractError('grouping requires selected source and approved profile')
        if flat and any(len(g)>profiles[self.profile_id].rules['max_devices_per_circuit'] for g in self.groups):
            raise ContractError('group exceeds caller profile device limit')
        images={im.id:im for im in request.images}
        bindings={r.proposal_id:r for r in request.proposal_sources}
        plan_supplied=next(c for c in request.contexts if c.role=='plan').status=='supplied'
        for device in self.devices:
            if device.status=='grouped' and (device.connector_id not in connectors or connectors[device.connector_id].proposal_id!=device.proposal_id):
                raise ContractError('invented or wrong-device connector')
            if any(c.image_id not in images for c in device.evidence):raise ContractError('citation outside supplied source context')
            plan_evidence=False
            for citation in device.evidence:
                image=images[citation.image_id]
                if image.role=='plan':
                    binding=bindings[device.proposal_id];a,b,c,d=_image_box_pt(image,citation.bounds)
                    x=(binding.bounds_pt[0]+binding.bounds_pt[2])/2;y=(binding.bounds_pt[1]+binding.bounds_pt[3])/2
                    if not _same_page(image,binding) or not (a<=x<=c and b<=y<=d):raise ContractError('plan citation does not cover its device source mark/page')
                    plan_evidence=True
            if device.status=='grouped' and plan_supplied and not plan_evidence:raise ContractError('grouped device needs its source-plan citation')
        known={d['proposal_id'] for d in request.placement_dispositions}
        if any(m.proposal_id is not None and m.proposal_id not in known for m in self.missing_inputs):raise ContractError('unknown missing-input proposal')
        signatures=[digest(m.to_dict()) for m in self.missing_inputs]
        if len(signatures)!=len(set(signatures)) or not {digest(m.to_dict()) for m in request.required_missing_inputs}<=set(signatures):
            raise ContractError('missing-input output omits required unknowns or repeats rows')


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitProposalRun(Portable):
    request: CircuitProposalRequest
    policy: CircuitRoutingPolicy
    attempts: tuple[AttemptReceipt,...]
    selected_attempt_id: str | None
    accepted: bool = False
    review_state: str = 'pending'

    def __post_init__(self):
        if self.accepted is not False or self.review_state!='pending':raise ContractError('AI circuit proposals cannot approve themselves')
        if not isinstance(self.request,CircuitProposalRequest) or not isinstance(self.policy,CircuitRoutingPolicy):raise ContractError('typed circuit execution required')
        _rows(self.attempts,AttemptReceipt,'circuit receipts',2)
        if len(self.attempts)!=2:raise ContractError('one complete circuit attempt required')
        start,finish=self.attempts
        if (start.phase,finish.phase)!=('start','finish') or start.attempt_id!=finish.attempt_id or start.reason!=finish.reason:
            raise ContractError('circuit receipt pair mismatch')
        for row in self.attempts:
            if (row.request_fingerprint!=digest(_invocation(self.request)) or row.policy_fingerprint!=self.policy.fingerprint
                    or row.role!=self.policy.role or row.endpoint!=self.policy.endpoint):raise ContractError('circuit trusted dispatch binding mismatch')
        if self.request.routing_policy_fingerprint!=self.policy.fingerprint or datetime.fromisoformat(finish.timestamp_utc)<datetime.fromisoformat(start.timestamp_utc):
            raise ContractError('stale circuit policy or receipt time')
        success=finish.status=='completed'
        if self.selected_attempt_id!=(finish.attempt_id if success else None):raise ContractError('selected circuit response contradicts receipt')
        if success:
            if self.policy.expected_model_id is not None and finish.resolved_model_id!=self.policy.expected_model_id:raise ContractError('unexpected returned model identity')
            CircuitProposalOutput.from_json(finish.raw_output).validate_for(self.request)

    @property
    def fingerprint(self):return digest(self.to_dict())
    @property
    def output(self):
        if self.selected_attempt_id is None:raise ContractError('failed circuit run has no selectable proposal')
        return CircuitProposalOutput.from_json(self.attempts[-1].raw_output)
    @property
    def provenance(self):
        return Provenance(source_kind='ai-circuit-proposal',source_id=self.request.fingerprint,
            source_element_id=self.selected_attempt_id,derivation='inferred',confidence=min((d.confidence for d in self.output.devices),default=0.),
            attributes={'output_fingerprint':self.output.fingerprint,'configured_endpoint':_portable(self.policy.endpoint),
                'transport_reported_model_id':self.attempts[-1].resolved_model_id,'accepted':False,'review_state':'pending'})



def _invocation(request):
    from .circuit_transport import circuit_output_schema, circuit_transport_prompt, TRANSPORT_VERSION
    prompt=circuit_transport_prompt(request);schema=circuit_output_schema(request)
    return {'request':request.to_dict(),'transport':{'version':TRANSPORT_VERSION,'prompt':prompt,
        'output_schema':schema,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'schema_fingerprint':digest(schema)}}

def dispatch_circuit_proposal(request, policy, *, review_policy, model, review, placement, images, documents, policy_documents, adapters, journal):
    if not isinstance(request,CircuitProposalRequest) or not isinstance(policy,CircuitRoutingPolicy) or not isinstance(review_policy,CircuitReviewPolicy):raise ContractError('typed circuit task, routing and review policies required')
    if not isinstance(journal,AttemptJournal):raise ContractError('durable circuit attempt journal required')
    _fresh_journal(journal)
    request=CircuitProposalRequest.from_dict(request.to_dict());policy=CircuitRoutingPolicy.from_dict(policy.to_dict())
    review_policy=CircuitReviewPolicy.from_dict(review_policy.to_dict())
    request.validate_inputs(model=model,review=review,placement=placement,policy=policy,review_policy=review_policy,
                            images=images,documents=documents,policy_documents=policy_documents)
    common=dict(attempt_id=stable_id('circuit-attempt',f'{journal.run_id}:{len(journal.receipts)}'),role=policy.role,
        endpoint=policy.endpoint,request_fingerprint=digest(_invocation(request)),policy_fingerprint=policy.fingerprint,reason=policy.mode+':circuit_primary')
    _fresh_journal(journal,claim=True)
    start=AttemptReceipt(**common,phase='start',status='started',timestamp_utc=datetime.now(timezone.utc).isoformat())
    journal.append(start)
    reply=None;status='adapter_error';error=None
    try:
        adapter=adapters.get(policy.endpoint.adapter_id)
        if not callable(adapter):raise ContractError('configured circuit adapter absent')
        response=adapter(_invocation(request),images=dict(images),provider_id=policy.endpoint.provider_id,
            model_alias=policy.endpoint.model_alias,prompt_version=request.prompt_version)
        if not isinstance(response,CircuitAdapterReply):raise ContractError('circuit adapter requires transport metadata reply')
        reply=CircuitAdapterReply.from_dict(response.to_dict())
        if policy.expected_model_id is not None and reply.resolved_model_id!=policy.expected_model_id:
            status='identity_mismatch';error='expected_resolved_model_identity_missing_or_mismatched'
        else:
            output=CircuitProposalOutput.from_json(reply.output_json);output.validate_for(request);status='completed'
    except AdapterAvailabilityError as exc:
        try:status=_availability_reason(exc)
        except ContractError:status='adapter_error';error='invalid_typed_availability_reason'
    except ContractError:status='invalid_output' if reply else 'adapter_error';error='circuit_domain_or_transport_rejected'
    except Exception as exc:error=type(exc).__name__
    finish=AttemptReceipt(**common,phase='finish',status=status,timestamp_utc=datetime.now(timezone.utc).isoformat(),
        raw_output=None if reply is None else reply.output_json,resolved_model_id=None if reply is None else reply.resolved_model_id,
        provider_request_id=None if reply is None else reply.provider_request_id,error=error)
    journal.append(finish)
    return CircuitProposalRun(request=request,policy=policy,attempts=(start,finish),selected_attempt_id=finish.attempt_id if status=='completed' else None)


@dataclass(frozen=True,slots=True,kw_only=True)
class CircuitReviewDecision(Portable):
    decision_id: str
    run_fingerprint: str
    request_fingerprint: str
    output_fingerprint: str
    review_policy_fingerprint: str
    actor_id: str
    actor_kind: str
    action: str
    selection: CircuitProposalOutput
    rationale: str
    confidence: float
    inventory_group: str

    def __post_init__(self):
        for key in ('decision_id','actor_id','inventory_group'):text(getattr(self,key),key)
        text(self.rationale,'trusted circuit review rationale',1024)
        for key in ('run_fingerprint','request_fingerprint','output_fingerprint','review_policy_fingerprint'):fingerprint(getattr(self,key),key)
        if self.actor_kind not in ('human','configured_agent') or self.action not in ('approve','defer','reject') or not isinstance(self.selection,CircuitProposalOutput):
            raise ContractError('invalid trusted circuit review')
        object.__setattr__(self,'confidence',_confidence(self.confidence))

    @classmethod
    def capture(cls,run,review_policy,**values):
        return cls(run_fingerprint=run.fingerprint,request_fingerprint=run.request.fingerprint,
            output_fingerprint=run.output.fingerprint,review_policy_fingerprint=review_policy.fingerprint,**values)


@dataclass(frozen=True,slots=True)
class CompiledCircuitProposal:
    request: ReviewedCircuitRequest
    decision_bytes: bytes
    evidence_documents: object
    missing_inputs: tuple[MissingInput,...]


def compile_circuit_review(run, decision, *, review_policy, model, review, placement, images, documents, policy_documents, journal_path):
    if not isinstance(run,CircuitProposalRun) or not isinstance(decision,CircuitReviewDecision) or not isinstance(review_policy,CircuitReviewPolicy):raise ContractError('typed pending run and independent review required')
    run=CircuitProposalRun.from_dict(run.to_dict());decision=CircuitReviewDecision.from_dict(decision.to_dict())
    review_policy=CircuitReviewPolicy.from_dict(review_policy.to_dict())
    if run.policy.mode!='strong_baseline' or run.selected_attempt_id is None:raise ContractError('only a completed strong baseline can enter reviewed design')
    if AttemptJournal.read(journal_path)!=run.attempts:raise ContractError('circuit execution journal differs from reviewed run')
    run.request.validate_inputs(model=model,review=review,placement=placement,policy=run.policy,review_policy=review_policy,
        images=images,documents=documents,policy_documents=policy_documents)
    if (decision.action!='approve' or decision.run_fingerprint!=run.fingerprint or decision.request_fingerprint!=run.request.fingerprint
            or decision.output_fingerprint!=run.output.fingerprint or decision.review_policy_fingerprint!=review_policy.fingerprint):
        raise ContractError('unapproved or stale circuit review binding')
    authority=next((a for a in review_policy.authorities if a.id==decision.actor_id and a.kind==decision.actor_kind),None)
    if authority is None:raise ContractError('review actor is not authorized by trusted caller policy')
    decision.selection.validate_for(run.request)
    if not {digest(m.to_dict()) for m in run.output.missing_inputs}<={digest(m.to_dict()) for m in decision.selection.missing_inputs}:
        raise ContractError('review cannot erase missing facts; recapture with new evidence')
    selection=decision.selection
    profile=next((p for p in run.request.profiles if p.id==selection.profile_id),None)
    connectors={c.id:c for c in run.request.connectors}
    chosen=[connectors[d.connector_id] for d in selection.devices if d.status=='grouped']
    request_id=stable_id('reviewed-circuit-proposal',f'{model.model_id}:{decision.decision_id}')
    document={'version':VERSION,'kind':'authorized-preliminary-circuit-design','source_observed':False,
        'actor':authority.to_dict(),'human_confirmation':authority.kind=='human',
        'ai_run':run.to_dict(),'ai_provenance':_portable(run.provenance),'review':decision.to_dict(),
        'review_policy':review_policy.to_dict(),'request_id':request_id,
        'selected_profile_fingerprint':None if profile is None else digest(profile.to_dict()),
        'selected_connector_fingerprints':{c.id:digest(c.to_dict()) for c in chosen}}
    payload=json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    sha=hashlib.sha256(payload).hexdigest()
    evidence=PlacementEvidence(source_id=decision.decision_id,document_sha256=sha,kind='project_decision',
        locator='Exact authorized review of bounded circuit choices and explicit edits.',
        provenance=Provenance(source_kind='trusted-circuit-review',source_id=decision.decision_id,source_element_id=authority.id,
            derivation='user',confidence=min(decision.confidence,run.provenance.confidence),
            attributes={'actor_kind':authority.kind,'human_confirmation':authority.kind=='human',
                'authorization_document_sha256':authority.authorization_document_sha256,
                'agent_configuration_fingerprint':authority.configuration_fingerprint,
                'ai_request_fingerprint':run.request.fingerprint,'ai_output_fingerprint':run.output.fingerprint,
                'execution_fingerprint':digest(_invocation(run.request)),
                'selected_profile_fingerprint':None if profile is None else digest(profile.to_dict()),
                'missing_inputs':[m.to_dict() for m in selection.missing_inputs],
                'meaning':'Explicit caller-authorized design instruction; not source observation or reviewer authentication.'}))
    authorization=PlacementEvidence(source_id=review_policy.fingerprint,
        document_sha256=authority.authorization_document_sha256,kind='project_decision',
        locator='Caller-supplied review authorization for this exact actor configuration and choice policy.',
        provenance=Provenance(source_kind='circuit-review-authorization',source_id=review_policy.fingerprint,
            source_element_id=authority.id,derivation='user',confidence=decision.confidence,
            attributes={'actor_kind':authority.kind,'human_confirmation':authority.kind=='human',
                'agent_configuration_fingerprint':authority.configuration_fingerprint}))
    evidence_rows=(evidence,authorization,*((profile.approval,) if profile else ()),*(c.approval for c in chosen))
    evidence_rows=tuple({digest(e.to_dict()):e for e in evidence_rows}.values())
    refs=(*((profile.rule_references) if profile else ()),*(c.reference for c in chosen))
    refs=tuple({(r.authority,r.revision,r.rule_id):r for r in refs}.values())
    source=next((s.entity for s in run.request.sources if s.collection=='ports' and s.id==selection.source_port_id),None)
    request=ReviewedCircuitRequest.capture(model,review,placement['assertion_review'],request_id=request_id,
        equipment_id=None if source is None else source['owner_id'],source_port_id=selection.source_port_id,
        groups=selection.groups or None,load_ports=tuple(ReviewedLoadPort(
            id=stable_id('port',f'{model.model_id}:{dict(run.request.eligible)[c.proposal_id]}:reviewed-circuit-load'),proposal_id=c.proposal_id,
            offset_canonical_m=c.offset_canonical_m,direction=c.direction) for c in chosen),
        rules=None if profile is None else profile.rules,routing_controls=None if profile is None else profile.routing_controls,
        rule_references=refs,evidence=evidence_rows,reviewer_id=authority.id,
        rationale='Explicit trusted review of pending AI circuit choices; engineering remains unverified.',inventory_group=decision.inventory_group)
    supplied={**placement['evidence_documents'],**policy_documents,sha:payload}
    return CompiledCircuitProposal(request,payload,MappingProxyType(supplied),selection.missing_inputs)
