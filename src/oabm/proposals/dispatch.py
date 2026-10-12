"""Injected fake/deterministic adapters only; no SDK, network or canonical writes."""
from .contracts import (
    BenchmarkCase, BenchmarkTrial, VisionBenchmark, RankingRequest, RankingOutput,
    RoutingConfig, ContractError, _array, _portable, digest, make_result, integer, budget, _decode_dataclass,
)


class AdapterAvailabilityError(RuntimeError):
    def __init__(self,reason):
        if reason not in ('rate_limited','unavailable'):
            raise ContractError('availability reason must be rate_limited or unavailable')
        self.reason=reason
        super().__init__(reason)


def _availability_reason(exc):
    reason=getattr(exc,'reason',None)
    if not isinstance(reason,str) or reason not in ('rate_limited','unavailable'):
        raise ContractError('altered unsupported adapter availability reason')
    return reason


def _call(request,config,endpoint,adapters):
    # Disposable deep JSON copy on every attempt, including an availability retry.
    # An adapter can mutate its copy; it cannot modify source or trusted inputs.
    request=RankingRequest.from_dict(request.to_dict())
    if request.policy_fingerprint!=config.fingerprint:
        raise ContractError('request routing policy fingerprint differs from dispatcher')
    adapter=adapters.get(endpoint.adapter_id)
    if not callable(adapter):raise ContractError('configured adapter is not registered')
    return adapter(request.to_dict(),provider_id=endpoint.provider_id,
                   model_alias=endpoint.model_alias,prompt_version=request.prompt_version)


def dispatch_ranking(request,config,*,adapters,vision_tier='primary',benchmark=None,diagnostic=False):
    if not isinstance(request,RankingRequest) or not isinstance(config,RoutingConfig):
        raise ContractError('dispatcher requires strict typed request/config')
    request=RankingRequest.from_dict(request.to_dict())
    config=RoutingConfig.from_dict(config.to_dict())
    if not isinstance(diagnostic,bool) or vision_tier not in ('primary','benchmark_secondary'):
        raise ContractError('explicit diagnostic/tier request is malformed')
    if diagnostic:
        if vision_tier!='primary' or benchmark is not None:
            raise ContractError('diagnostic request cannot be an automatic benchmark fallback')
        role='diagnostic_astra'
        response=_call(request,config,config.diagnostic_astra,adapters)
        output=RankingOutput.from_dict(response);output.validate_for(request)
        return make_result(request,config,role,output,diagnostic_requested=True)
    if request.modality=='text':
        if vision_tier!='primary' or benchmark is not None:
            raise ContractError('text request cannot use vision escalation')
        role='text_glm';availability=None
        try:response=_call(request,config,config.text_glm,adapters)
        except AdapterAvailabilityError as exc:
            availability=_availability_reason(exc)
            role='text_codex'
            response=_call(request,config,config.text_codex,adapters)
        output=RankingOutput.from_dict(response);output.validate_for(request)
        return make_result(request,config,role,output,availability_reason=availability)
    role='vision_luna'
    if vision_tier=='benchmark_secondary':
        if not isinstance(benchmark,VisionBenchmark):raise ContractError('missing measured Luna benchmark')
        benchmark=VisionBenchmark.from_dict(benchmark.to_dict())
        benchmark.validate_gate(request,config);role='vision_sol'
    elif benchmark is not None:
        raise ContractError('benchmark does not authorize implicit vision retry')
    response=_call(request,config,getattr(config,role),adapters)
    output=RankingOutput.from_dict(response);output.validate_for(request)
    return make_result(request,config,role,output,benchmark=benchmark)


def measure_luna_benchmark(cases,config,*,adapters,required_success_count):
    _array(cases,BenchmarkCase,'benchmark cases')
    integer(len(cases),'benchmark case count',maximum=16)
    integer(required_success_count,'benchmark required successes',maximum=len(cases))
    if not isinstance(config,RoutingConfig):raise ContractError('benchmark needs typed routing config')
    config=RoutingConfig.from_dict(config.to_dict())
    cases=tuple(sorted((_decode_dataclass(BenchmarkCase,_portable(c)) for c in cases),key=lambda c:c.id))
    if len({c.id for c in cases})!=len(cases):raise ContractError('duplicate benchmark case identities')
    first=cases[0].request
    for case in cases:
        req=case.request
        if req.policy_fingerprint!=config.fingerprint or (req.task,req.task_version,req.validator_version,req.prompt_version)!=(
                first.task,first.task_version,first.validator_version,first.prompt_version):
            raise ContractError('mixed/stale benchmark task input')
    budget(_portable(cases),500000)
    trials=[]
    for case in cases:
        try:
            response=_call(case.request,config,config.vision_luna,adapters)
            budget(response,65536)
            trials.append(BenchmarkTrial(case_id=case.id,response=_portable(response),availability_reason=None))
        except AdapterAvailabilityError as exc:
            trials.append(BenchmarkTrial(case_id=case.id,response=None,availability_reason=_availability_reason(exc)))
    from .contracts import _measured_success, VERSION
    first=cases[0].request
    data=dict(version=VERSION,config=config,endpoint=config.vision_luna,
        cases=cases,trials=tuple(trials),corpus_fingerprint=digest(_portable(cases)),
        task=first.task,task_version=first.task_version,validator_version=first.validator_version,
        prompt_version=first.prompt_version,case_count=len(cases),
        success_count=sum(_measured_success(c,t) for c,t in zip(cases,trials)),
        required_success_count=required_success_count)
    return VisionBenchmark(**data,content_fingerprint=digest(_portable(data)))
