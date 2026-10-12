"""Strict, bounded semantic rankings over existing deterministic placement facts."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from oabm.model import BuildingModel, ContractError, Provenance, stable_id
from oabm.model.common import _finite as _canonical_finite, _validate_id, _validate_json_value, provenance_applies_to
from oabm.model.model import _decode_dataclass as _canonical_decode_dataclass
from oabm.reports.panel_schedules import (
    ModelSourceContext, SourceSnapshot, ENTITY_TYPES, _source_document,
    _portable, _json, _array, _ids,
)
from oabm.routing.placement import PlacementCandidate, propose_equipment_placement

VERSION = '1.0.0'
TASK = 'rank_equipment_placement_candidates'
TASK_VERSION = '1.0.0'
VALIDATOR_VERSION = 'placement-ranking-v1'
MAX_CANDIDATES = 128
MAX_SOURCE_ENTITIES = 512
MAX_DOCUMENT_BYTES = 1_000_000
MAX_TEXT = 4096
MAX_PIXELS = 262144
MAX_PRODUCER_WORK = 100000
MAX_GEOMETRY_VERTICES = 4096


def _finite(value,label):
    try:return _canonical_finite(value,label)
    except OverflowError as exc:
        raise ContractError(f'{label} exceeds supported finite numeric range') from exc


def _decode_dataclass(cls,data):
    try:return _canonical_decode_dataclass(cls,data)
    except OverflowError as exc:
        raise ContractError('proposal numeric value exceeds supported finite range') from exc


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def fingerprint(value, label):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ContractError(f'{label} must be lowercase SHA-256 hex')


def text(value, label, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError(f'{label} must be bounded nonempty text')


def integer(value, label, minimum=1, maximum=MAX_CANDIDATES):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError(f'{label} must be a bounded integer')


def budget(value, maximum=MAX_DOCUMENT_BYTES):
    _validate_json_value(value)
    if len(_json(value).encode()) > maximum:
        raise ContractError('proposal document exceeds bounded byte budget')


class Portable:
    @classmethod
    def from_dict(cls, data):
        budget(data)
        return _decode_dataclass(cls, data)

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, str) or len(value.encode()) > MAX_DOCUMENT_BYTES:
            raise ContractError('proposal JSON exceeds bounded byte budget')
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ContractError('duplicate proposal JSON key')
                result[key] = item
            return result
        def constant(value):
            raise ContractError('nonfinite proposal JSON number')
        try:
            data = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
        except json.JSONDecodeError as exc:
            raise ContractError('invalid proposal JSON') from exc
        return cls.from_dict(data)

    def to_dict(self):
        data = _portable(self)
        self.from_dict(data)  # nested mutation may not retain stale bindings
        return data

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, allow_nan=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementOptions:
    identity_key: str
    equipment_type: str
    name: str | None
    level_id: str
    served_device_ids: tuple[str, ...]
    mounting_height_m: float
    working_depth_m: float
    working_width_m: float

    def __post_init__(self):
        text(self.identity_key, 'placement identity')
        text(self.equipment_type, 'equipment type')
        if self.name is not None:text(self.name, 'equipment name')
        _validate_id(self.level_id, 'placement level')
        _ids(self.served_device_ids, 'served devices')
        if len(self.served_device_ids) > MAX_CANDIDATES:
            raise ContractError('served device budget exceeded')
        object.__setattr__(self, 'served_device_ids', tuple(sorted(self.served_device_ids)))
        for key in ('mounting_height_m', 'working_depth_m', 'working_width_m'):
            value=_finite(getattr(self,key),key)
            object.__setattr__(self,key,value)
            if value <= 0:
                raise ContractError('placement dimensions must be positive')

    def kwargs(self):
        return {key:getattr(self,key) for key in self.__dataclass_fields__}


def _normalize_candidate(candidate):
    from dataclasses import replace
    from oabm.model import Point3,Vector3
    if not isinstance(candidate,PlacementCandidate) or not isinstance(candidate.position,Point3) or not isinstance(candidate.direction,Vector3):
        raise ContractError('candidate geometry must use canonical primitives')
    return replace(candidate,score=_finite(candidate.score,'candidate score'),
        confidence=_finite(candidate.confidence,'candidate confidence'),
        position=_decode_dataclass(Point3,_portable(candidate.position)),
        direction=_decode_dataclass(Vector3,_portable(candidate.direction)))


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateReference:
    id: str
    content_fingerprint: str
    candidate: PlacementCandidate

    def __post_init__(self):
        if not isinstance(self.candidate, PlacementCandidate):
            raise ContractError('candidate requires actual PlacementCandidate data')
        object.__setattr__(self,'candidate',_normalize_candidate(self.candidate))
        data = self.candidate.to_dict()
        budget(data)
        for key in ('score', 'confidence'):_finite(data[key], key)
        if not 0 <= self.candidate.confidence <= 1 or not isinstance(self.candidate.valid, bool):
            raise ContractError('invalid candidate confidence or validity')
        _validate_id(self.candidate.wall_id, 'candidate wall')
        _validate_id(self.candidate.space_id, 'candidate space')
        _array(self.candidate.reasons, str, 'candidate reasons')
        if len(self.candidate.reasons)>512:raise ContractError('candidate reasons exceed budget')
        for reason in self.candidate.reasons:text(reason,'candidate reason')
        # PlacementCandidate has no __post_init__; recheck canonical primitives.
        from oabm.model import Point3, Vector3
        if not isinstance(self.candidate.position,Point3) or not isinstance(self.candidate.direction,Vector3):
            raise ContractError('candidate geometry must use canonical primitives')
        _decode_dataclass(Point3, _portable(self.candidate.position))
        _decode_dataclass(Vector3, _portable(self.candidate.direction))
        actual = digest(data)
        if self.content_fingerprint != actual or self.id != stable_id('placement-candidate',actual):
            raise ContractError('candidate content identity mismatch')

    @classmethod
    def capture(cls, candidate):
        candidate=_normalize_candidate(candidate)
        value = digest(candidate.to_dict())
        return cls(id=stable_id('placement-candidate',value),content_fingerprint=value,candidate=candidate)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementContext:
    model_source_context: ModelSourceContext
    sources: tuple[SourceSnapshot, ...]
    source_model_fingerprint: str
    options: PlacementOptions
    candidates: tuple[CandidateReference, ...]

    def __post_init__(self):
        if not isinstance(self.model_source_context,ModelSourceContext) or not isinstance(self.options,PlacementOptions):
            raise ContractError('placement context needs typed source and producer options')
        _array(self.sources,SourceSnapshot,'source snapshots')
        _array(self.candidates,CandidateReference,'placement candidates')
        if len(self.sources)>MAX_SOURCE_ENTITIES or not 1 <= len(self.candidates) <= MAX_CANDIDATES:
            raise ContractError('placement context exceeds bounded entity/candidate budget')
        identities=tuple(source.id for source in self.sources)
        if identities != tuple(sorted(set(identities))):
            raise ContractError('source identities must be sorted and distinct')
        refs=tuple(c.id for c in self.candidates)
        if refs != tuple(sorted(set(refs))):
            raise ContractError('candidate identities must be sorted and distinct')
        fingerprint(self.source_model_fingerprint,'source model fingerprint')
        model=self.source_model()
        if digest(_source_document(model)) != self.source_model_fingerprint:
            raise ContractError('source model snapshot binding mismatch')
        if self.candidates != _candidates(model,self.options):
            raise ContractError('candidates contradict recomputed deterministic producer')
        budget(_portable(self))

    def source_model(self):
        # Reconstruct only an original canonical source document, not a new model.
        header=_portable(self.model_source_context)
        header.pop('content_fingerprint')
        document=BuildingModel(model_id=header['model_id'],schema_version=header['schema_version']).to_dict()
        document.update(header)
        for source in self.sources:
            if source.collection not in document:
                raise ContractError('source collection unavailable in original model version')
            document[source.collection].append(_portable(source.entity))
        return BuildingModel.from_dict(document)

    @classmethod
    def capture(cls,model,options):
        _preflight(model,options)
        document=_source_document(model)
        budget(document)
        model=BuildingModel.from_dict(document)
        sources=tuple(sorted((SourceSnapshot.from_source_data(collection,e)
            for collection in ENTITY_TYPES for e in document.get(collection,[])),key=lambda s:s.id))
        return cls(model_source_context=ModelSourceContext.capture(document),sources=sources,
            source_model_fingerprint=digest(document),options=options,candidates=_candidates(model,options))


def _preflight(model,options):
    if not isinstance(model,BuildingModel) or not isinstance(options,PlacementOptions):
        raise ContractError('capture requires canonical source and typed options')
    if sum(len(getattr(model,c)) for c in ENTITY_TYPES)>MAX_SOURCE_ENTITIES:
        raise ContractError('source entity budget exceeded before placement work')
    walls=[w for w in model.walls if w.level_id==options.level_id and w.construction!='glazed']
    spaces=[s for s in model.spaces if s.level_id==options.level_id]
    segments=sum(len(w.centerline.points)-1 for w in walls)
    def vertices(geometry):
        return len(geometry.points) if hasattr(geometry,'points') else 8
    space_vertices=sum(len(space.footprint.points) for space in spaces)
    geometry_vertices=sum(vertices(o.geometry) for o in (*model.obstacles,*model.route_constraints))
    total_vertices=space_vertices+geometry_vertices+sum(len(w.centerline.points) for w in walls)
    if total_vertices>MAX_GEOMETRY_VERTICES:
        raise ContractError('deterministic producer preflight vertex budget exceeded')
    checks=1+space_vertices+len(model.openings)+geometry_vertices+len(options.served_device_ids)
    # Conservative upper bound on generated positions and conflict comparisons.
    if segments*len(spaces)*6>MAX_CANDIDATES or segments*len(spaces)*6*checks>MAX_PRODUCER_WORK:
        raise ContractError('deterministic producer preflight work budget exceeded')


def _candidates(model,options):
    _preflight(model,options)
    proposal=propose_equipment_placement(model,**options.kwargs())
    candidates={reference.id:reference for reference in
        (CandidateReference.capture(c) for c in (proposal.selected,*proposal.alternatives))}
    return tuple(candidates[key] for key in sorted(candidates))


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageCrop:
    source_id: str
    region_id: str
    width: int
    height: int
    gray8_base64: str
    content_fingerprint: str

    def __post_init__(self):
        _validate_id(self.source_id,'crop source')
        _validate_id(self.region_id,'crop region')
        integer(self.width,'crop width',maximum=MAX_PIXELS)
        integer(self.height,'crop height',maximum=MAX_PIXELS)
        if self.width*self.height>MAX_PIXELS or not isinstance(self.gray8_base64,str):
            raise ContractError('crop pixel budget exceeded')
        if len(self.gray8_base64)!=4*((self.width*self.height+2)//3):
            raise ContractError('crop encoded length exceeds exact pixel budget')
        try: pixels=base64.b64decode(self.gray8_base64,validate=True)
        except (ValueError,TypeError) as exc:raise ContractError('invalid gray8 crop encoding') from exc
        if len(pixels)!=self.width*self.height or base64.b64encode(pixels).decode()!=self.gray8_base64:
            raise ContractError('crop dimensions/encoding disagree with pixels')
        data=_portable(self);data.pop('content_fingerprint')
        if self.content_fingerprint!=digest(data):raise ContractError('crop content binding mismatch')

    @classmethod
    def capture(cls,*,source_id,region_id,width,height,pixels):
        if not isinstance(pixels,bytes):raise ContractError('crop pixels must be immutable bytes')
        integer(width,'crop width',maximum=MAX_PIXELS);integer(height,'crop height',maximum=MAX_PIXELS)
        if width*height>MAX_PIXELS or len(pixels)!=width*height:raise ContractError('crop pixel budget/dimensions mismatch')
        data=dict(source_id=source_id,region_id=region_id,width=width,height=height,
            gray8_base64=base64.b64encode(pixels).decode())
        return cls(**data,content_fingerprint=digest(data))


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingRequest(Portable):
    version: str
    task: str
    task_version: str
    validator_version: str
    prompt_version: str
    policy_fingerprint: str
    context: PlacementContext
    question: str
    modality: str
    crop: ImageCrop | None
    input_fingerprint: str

    def __post_init__(self):
        if self.version!=VERSION or self.task!=TASK or self.task_version!=TASK_VERSION or self.validator_version!=VALIDATOR_VERSION:
            raise ContractError('unsupported ranking contract/task/validator version')
        text(self.prompt_version,'prompt version')
        fingerprint(self.policy_fingerprint,'routing policy fingerprint')
        if not isinstance(self.context,PlacementContext):raise ContractError('ranking request needs typed source context')
        text(self.question,'bounded ranking question',MAX_TEXT)
        if self.modality not in ('text','vision') or not isinstance(self.modality,str):
            raise ContractError('unknown proposal modality')
        if (self.modality=='vision') != isinstance(self.crop,ImageCrop) or (self.modality=='text' and self.crop is not None):
            raise ContractError('vision needs a bounded crop; text cannot carry pixels')
        if self.crop and (self.crop.source_id!=self.context.model_source_context.model_id or self.crop.region_id!=self.context.options.level_id):
            raise ContractError('crop source/region does not match candidate source')
        data=_portable(self);data.pop('input_fingerprint')
        if self.input_fingerprint!=digest(data):raise ContractError('ranking input fingerprint mismatch')
        budget(data)

    @classmethod
    def capture(cls,model,*,options,question,prompt_version,policy_fingerprint,crop=None):
        data=dict(version=VERSION,task=TASK,task_version=TASK_VERSION,validator_version=VALIDATOR_VERSION,
            prompt_version=prompt_version,policy_fingerprint=policy_fingerprint,
            context=PlacementContext.capture(model,options),question=question,
            modality='vision' if crop is not None else 'text',crop=crop)
        return cls(**data,input_fingerprint=digest(_portable(data)))

    def validate_against(self,model,options,policy_fingerprint):
        self.to_dict()
        if self.policy_fingerprint!=policy_fingerprint or self.context!=PlacementContext.capture(model,options):
            raise ContractError('ranking request is stale against model/options/policy')


@dataclass(frozen=True, slots=True, kw_only=True)
class RankedCandidate:
    candidate_id: str
    confidence: float
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self):
        _validate_id(self.candidate_id,'ranked candidate')
        object.__setattr__(self,'confidence',_finite(self.confidence,'ranking confidence'))
        if not 0<=self.confidence<=1:raise ContractError('ranking confidence outside range')
        _array(self.reasons,str,'ranking reasons')
        if not 1<=len(self.reasons)<=4:raise ContractError('ranking reasons exceed budget')
        for reason in self.reasons:text(reason,'ranking reason')
        _ids(self.evidence_ids,'ranking evidence')
        if len(self.evidence_ids)>8:raise ContractError('ranking evidence exceeds budget')
        object.__setattr__(self,'evidence_ids',tuple(sorted(self.evidence_ids)))


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingOutput(Portable):
    ranking: tuple[RankedCandidate, ...]

    def __post_init__(self):
        _array(self.ranking,RankedCandidate,'ranking')
        if not 1<=len(self.ranking)<=MAX_CANDIDATES:raise ContractError('ranking size exceeds budget')
        ids=tuple(r.candidate_id for r in self.ranking)
        if len(ids)!=len(set(ids)):raise ContractError('duplicate ranking candidate')

    def validate_for(self,request):
        sources={s.id for s in request.context.sources}
        candidates={c.id:c.candidate for c in request.context.candidates}
        if {r.candidate_id for r in self.ranking}!=set(candidates):
            raise ContractError('ranking must contain each supplied candidate exactly once')
        if any(set(r.evidence_ids)-sources for r in self.ranking):
            raise ContractError('ranking cites unknown source evidence')
        if not candidates[self.ranking[0].candidate_id].valid:
            raise ContractError('chosen candidate has known producer clearance conflicts')


@dataclass(frozen=True, slots=True, kw_only=True)
class Endpoint:
    adapter_id: str
    provider_id: str
    model_alias: str

    def __post_init__(self):
        for key in ('adapter_id','provider_id','model_alias'):text(getattr(self,key),key)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingConfig(Portable):
    version: str
    text_glm: Endpoint
    text_codex: Endpoint
    vision_luna: Endpoint
    vision_sol: Endpoint
    diagnostic_astra: Endpoint

    def __post_init__(self):
        if self.version!=VERSION:raise ContractError('unsupported routing config version')
        endpoints=[getattr(self,key) for key in ('text_glm','text_codex','vision_luna','vision_sol','diagnostic_astra')]
        if any(not isinstance(e,Endpoint) for e in endpoints):raise ContractError('routing roles need typed configured endpoints')
        identities=[(e.provider_id,e.model_alias) for e in endpoints]
        if len(identities)!=len(set(identities)):raise ContractError('routing roles must use distinct provider/model aliases')

    @property
    def fingerprint(self):return digest(self.to_dict())


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkCase:
    id: str
    request: RankingRequest
    expected_candidate_id: str

    def __post_init__(self):
        _validate_id(self.id,'benchmark case')
        if not isinstance(self.request,RankingRequest) or self.request.modality!='vision':
            raise ContractError('vision benchmark requires frozen vision requests')
        candidate=next((c for c in self.request.context.candidates if c.id==self.expected_candidate_id),None)
        if candidate is None or not candidate.candidate.valid:
            raise ContractError('benchmark expected choice must be a known eligible candidate')


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkTrial:
    case_id: str
    response: Any
    availability_reason: str | None

    def __post_init__(self):
        _validate_id(self.case_id,'benchmark trial case')
        budget(self.response, 65536)
        if self.availability_reason is not None:
            if self.availability_reason not in ('rate_limited','unavailable') or self.response is not None:
                raise ContractError('invalid benchmark availability receipt')


def _measured_success(case,trial):
    if trial.availability_reason is not None:return False
    try:
        output=RankingOutput.from_dict(trial.response)
        output.validate_for(case.request)
    except ContractError:return False
    return output.ranking[0].candidate_id==case.expected_candidate_id


@dataclass(frozen=True, slots=True, kw_only=True)
class VisionBenchmark(Portable):
    version: str
    config: RoutingConfig
    endpoint: Endpoint
    cases: tuple[BenchmarkCase,...]
    trials: tuple[BenchmarkTrial,...]
    corpus_fingerprint: str
    task: str
    task_version: str
    validator_version: str
    prompt_version: str
    case_count: int
    success_count: int
    required_success_count: int
    content_fingerprint: str

    def __post_init__(self):
        if self.version!=VERSION or not isinstance(self.config,RoutingConfig) or self.endpoint!=self.config.vision_luna:
            raise ContractError('benchmark must bind configured Luna endpoint')
        _array(self.cases,BenchmarkCase,'benchmark cases')
        _array(self.trials,BenchmarkTrial,'benchmark trials')
        integer(self.case_count,'benchmark case count',maximum=16)
        integer(self.success_count,'benchmark successes',minimum=0,maximum=self.case_count)
        integer(self.required_success_count,'benchmark required successes',maximum=self.case_count)
        ids=tuple(c.id for c in self.cases)
        if len(ids)!=self.case_count or ids!=tuple(sorted(set(ids))):
            raise ContractError('benchmark corpus must be nonempty, complete, sorted and distinct')
        if tuple(t.case_id for t in self.trials)!=ids:
            raise ContractError('benchmark needs exactly one sorted result for every frozen case')
        for case in self.cases:
            request=case.request
            if (request.task,request.task_version,request.validator_version,request.prompt_version)!=(
                self.task,self.task_version,self.validator_version,self.prompt_version) or request.policy_fingerprint!=self.config.fingerprint:
                raise ContractError('benchmark task/prompt/validator/policy binding mismatch')
        if self.corpus_fingerprint!=digest(_portable(self.cases)):
            raise ContractError('frozen benchmark corpus binding mismatch')
        measured=sum(_measured_success(c,t) for c,t in zip(self.cases,self.trials))
        if self.success_count!=measured:raise ContractError('benchmark counts disagree with frozen case/results')
        data=_portable(self);data.pop('content_fingerprint')
        if self.content_fingerprint!=digest(data):raise ContractError('measured benchmark content binding mismatch')
        budget(_portable(self))

    def validate_gate(self,request,config):
        self.to_dict()
        if self.config!=config or self.endpoint!=config.vision_luna or (
            self.task,self.task_version,self.validator_version,self.prompt_version)!=(
            request.task,request.task_version,request.validator_version,request.prompt_version):
            raise ContractError('vision benchmark is stale against request task/config')
        if self.success_count>=self.required_success_count or any(t.availability_reason is not None for t in self.trials):
            raise ContractError('Sol requires measured Luna task failure, not availability or passing results')


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalResult(Portable):
    version: str
    id: str
    request: RankingRequest
    config: RoutingConfig
    endpoint: Endpoint
    role: str
    output: RankingOutput
    benchmark: VisionBenchmark | None
    availability_reason: str | None
    diagnostic_requested: bool
    provenance: tuple[Provenance, ...]
    diagnostic: bool
    accepted: bool

    def __post_init__(self):
        if self.version!=VERSION or self.accepted is not False:raise ContractError('ranking proposal cannot be canonically accepted')
        if not isinstance(self.request,RankingRequest) or not isinstance(self.config,RoutingConfig) or not isinstance(self.endpoint,Endpoint) or not isinstance(self.output,RankingOutput):
            raise ContractError('proposal result requires typed bound inputs/config/output')
        if self.request.policy_fingerprint!=self.config.fingerprint:raise ContractError('result policy binding mismatch')
        if not isinstance(self.role,str) or self.role not in {'text_glm','text_codex','vision_luna','vision_sol','diagnostic_astra'}:
            raise ContractError('unknown trusted dispatcher role')
        if self.endpoint!=getattr(self.config,self.role):raise ContractError('result endpoint differs from configured dispatched role')
        if self.diagnostic is not (self.role=='diagnostic_astra'):raise ContractError('diagnostic role cannot be normal production')
        if self.role.startswith('text_') and self.request.modality!='text' or self.role in {'vision_luna','vision_sol'} and self.request.modality!='vision':
            raise ContractError('result role/modality mismatch')
        if self.diagnostic_requested is not (self.role=='diagnostic_astra'):
            raise ContractError('Astra requires an explicit diagnostic request')
        if self.role=='vision_sol':
            if not isinstance(self.benchmark,VisionBenchmark):raise ContractError('Sol requires measured benchmark evidence')
            self.benchmark.validate_gate(self.request,self.config)
        elif self.benchmark is not None:raise ContractError('benchmark gate belongs only to explicit Sol request')
        if self.role=='text_codex':
            if self.availability_reason not in ('rate_limited','unavailable'):
                raise ContractError('Codex fallback requires typed primary availability failure')
        elif self.availability_reason is not None:raise ContractError('unexpected fallback availability receipt')
        self.output.validate_for(self.request)
        _array(self.provenance,Provenance,'proposal provenance')
        expected=_result_provenance(self.request,self.output,self.endpoint)
        if self.provenance!=(expected,):raise ContractError('proposal evidence must be inferred and scoped without source promotion')
        data=_portable(self);data.pop('id')
        if self.id!=stable_id('ranking-result',digest(data)):raise ContractError('proposal result identity binding mismatch')
        budget(_portable(self))

    def validate_against(self,model,options,config,*,current_request):
        self.to_dict()
        if not isinstance(current_request,RankingRequest):raise ContractError('current bound task input is required')
        current_request.validate_against(model,options,config.fingerprint)
        if self.request.to_dict()!=current_request.to_dict():raise ContractError('saved proposal task input changed')
        self.request.validate_against(model,options,config.fingerprint)
        if self.config!=config:raise ContractError('saved result routing configuration changed')


def _result_provenance(request,output,endpoint):
    candidate=next(c.candidate for c in request.context.candidates if c.id==output.ranking[0].candidate_id)
    consumed={
        'walls':('centerline','thickness_m','height_m','construction','level_id'),
        'spaces':('footprint','usage','name','level_id'),
        'levels':('elevation_m',),
        'openings':('host_id','opening_type','pose','size'),
        'obstacles':('geometry','clearance_m','obstacle_type'),
        'route_constraints':('hard','constraint_type','geometry','clearance_m'),
        'electrical_devices':('pose',),
    }
    records=request.context.model_source_context.provenance
    confidence=min(output.ranking[0].confidence,candidate.confidence,
        *(r.confidence for r in records if provenance_applies_to(r,tuple(consumed))))
    for source in request.context.sources:
        if source.collection in consumed:
            confidence=min(confidence,source.entity['confidence'],
                *(r['confidence'] for r in source.entity['provenance']
                  if provenance_applies_to(_decode_dataclass(Provenance,r),consumed[source.collection])))
    return Provenance(source_kind='ai-proposed',source_id=request.input_fingerprint,
        source_element_id=endpoint.model_alias,method='ai-proposed-semantic-ranking-v1',
        confidence=confidence,derivation='inferred',scope_paths=('output',),
        attributes={'provider_id':endpoint.provider_id,'adapter_id':endpoint.adapter_id,
                    'prompt_version':request.prompt_version,'source_observed':False})


def make_result(request,config,role,output,*,benchmark=None,availability_reason=None,diagnostic_requested=False):
    endpoint=getattr(config,role)
    data=dict(version=VERSION,request=request,config=config,endpoint=endpoint,role=role,
        output=output,benchmark=benchmark,availability_reason=availability_reason,
        diagnostic_requested=diagnostic_requested,provenance=(_result_provenance(request,output,endpoint),),
        diagnostic=role=='diagnostic_astra',accepted=False)
    return ProposalResult(**data,id=stable_id('ranking-result',digest(_portable(data))))
