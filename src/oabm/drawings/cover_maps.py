"""Apple-only cover snapshot acquisition through existing OfficeAdmin access.

The installed OA client owns its authentication. Apple signing remains in OA.
No credential, signed provider URL or authorization header enters a receipt.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable

VERSION = 'officeadmin-cover-map-v1'
GEOCODE_PATH = '/tools/site-plan/geocode'
IMAGERY_PATH = '/tools/site-plan/imagery'
ATTRIBUTION = 'Map imagery (c) Apple Maps | Geocoding (c) Apple'


class MapAcquisitionError(ValueError):
    pass


class MapAmbiguityError(MapAcquisitionError):
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        super().__init__('Apple map candidate confirmation required: '+json.dumps(self.candidates,sort_keys=True))


@dataclass(frozen=True)
class CoverMapSnapshot:
    png_bytes: bytes
    sha256: str
    width_px: int
    height_px: int
    attribution: str
    provenance: dict[str,str]


def validate_png(data, expected_sha256=None):
    from PIL import Image
    if not isinstance(data,bytes) or not data or len(data)>8*1024*1024:
        raise MapAcquisitionError('map asset must be immutable PNG bytes within 8 MiB')
    digest=hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest!=expected_sha256:
        raise MapAcquisitionError('retained map PNG hash mismatch')
    try:
        with Image.open(io.BytesIO(data)) as image:
            width,height=image.size
            if image.format!='PNG' or width*height>16_000_000:
                raise ValueError('bounded PNG required')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:image.load()
    except (OSError,ValueError) as exc:
        raise MapAcquisitionError('invalid map PNG') from exc
    return digest,width,height


class OfficeAdminMapClient:
    """Public read endpoints via installed client with fail-closed redirects.

    Direct OA response metadata is mandatory. A relay that hides upstream
    provider headers is refused, rather than being treated as Apple evidence.
    """
    def __init__(self, client_script=None):
        self.client_script=Path(client_script or Path.home()/'.aiva/modules/toolbox-officeadmin/cli/officeadmin-cli.py')

    def request(self,path,query,*,binary=False):
        with tempfile.TemporaryDirectory(prefix='oa-cover-map-') as directory:
            root=Path(directory);receipt=root/'transport.json';output=root/'snapshot.png'
            args=[sys.executable,'-m','oabm.drawings.officeadmin_map_bridge',str(receipt),str(self.client_script),
                  '--json','request','--method','GET','--path',path]
            for key,value in query.items():args.extend(['--query',f'{key}={value}'])
            if binary:args.extend(['--accept','image/png','--output',str(output)])
            try:
                result=subprocess.run(args,capture_output=True,timeout=50,check=False)
            except (OSError,subprocess.TimeoutExpired) as exc:
                raise MapAcquisitionError('existing OfficeAdmin map client is unavailable or timed out') from exc
            if result.returncode:
                raise MapAcquisitionError(f'OfficeAdmin map read failed (exit {result.returncode}); no fallback map accepted')
            if not receipt.exists():
                raise MapAcquisitionError('OfficeAdmin transport hides upstream map headers; strict adapter required')
            rows=json.loads(receipt.read_text()).get('responses',[])
            expected='/api/v1'+path
            matching=[row for row in rows if row.get('path')==expected]
            if not matching or matching[-1].get('status')!=200:
                raise MapAcquisitionError('OfficeAdmin map response lacks direct HTTP 200 source receipt')
            transport=matching[-1]
            if binary:
                if transport['headers'].get('x-imagery-provider')!='apple':
                    raise MapAcquisitionError('snapshot provider is not explicitly Apple')
                if transport['headers'].get('Content-Type','').split(';')[0]!='image/png' or not output.exists():
                    raise MapAcquisitionError('Apple map response is not a retained PNG')
                return output.read_bytes(),transport
            try:
                envelope=json.loads(result.stdout)
                if envelope.get('ok') is not True or envelope.get('status')!=200:
                    raise ValueError('failed envelope')
                return envelope['data'],transport
            except (ValueError,KeyError,TypeError) as exc:
                raise MapAcquisitionError('invalid OfficeAdmin map JSON envelope') from exc


def _candidate(row):
    if not isinstance(row,dict) or set(row)!={'label','lat','lng'} or not isinstance(row['label'],str) or not row['label'].strip():
        raise MapAcquisitionError('invalid Apple geocode candidate')
    for key,limit in [('lat',85.05112878),('lng',180)]:
        if type(row[key]) not in (int,float) or not math.isfinite(row[key]) or abs(row[key])>limit:
            raise MapAcquisitionError('geocode coordinate outside supported map domain')
    return dict(row)


def _numeric_address_check(address,label):
    number=re.match(r'\s*(\d+[A-Za-z]?)\b',address)
    candidate_number=re.match(r'\s*(\d+[A-Za-z]?)\b',label)
    if number and (not candidate_number or number.group(1).casefold()!=candidate_number.group(1).casefold()):
        raise MapAcquisitionError('Apple candidate does not preserve supplied street number')
    postcodes=re.findall(r'\b\d{5}(?:-\d{4})?\b',address)
    candidate_postcodes=re.findall(r'\b\d{5}(?:-\d{4})?\b',label)
    if postcodes and (not candidate_postcodes or postcodes[-1][:5]!=candidate_postcodes[-1][:5]):
        raise MapAcquisitionError('Apple candidate does not preserve supplied postal code')


def _normalized_address(address):
    # Conservative equivalence only: no fuzzy street/city substitutions.
    aliases = {'street':'st', 'avenue':'ave', 'road':'rd', 'drive':'dr',
               'boulevard':'blvd', 'lane':'ln', 'court':'ct', 'place':'pl',
               'terrace':'ter', 'parkway':'pkwy', 'highway':'hwy',
               'north':'n', 'south':'s', 'east':'e', 'west':'w',
               'northeast':'ne', 'northwest':'nw', 'southeast':'se', 'southwest':'sw'}
    value=address.casefold()
    # State names are equivalent to their postal abbreviations where supplied.
    states={'california':'ca','new york':'ny','new jersey':'nj','new mexico':'nm',
            'north carolina':'nc','south carolina':'sc','north dakota':'nd','south dakota':'sd',
            'washington':'wa','oregon':'or','arizona':'az','nevada':'nv','texas':'tx',
            'florida':'fl','colorado':'co','illinois':'il','massachusetts':'ma'}
    for name,code in states.items():
        value=re.sub(r'\b'+name+r'\b',code,value)
    value=re.sub(r',?\s+(?:united states(?: of america)?|usa|us)\s*$', '', value)
    return tuple(aliases.get(token,token) for token in re.findall(r'[a-z0-9]+',value))


def candidate_fingerprint(query, candidate):
    """Bind caller confirmation to query, provider, exact label and coordinates."""
    payload={'query':_normalized_address(query),'provider':'apple','label':candidate['label'],
             'latitude':float(candidate['lat']),'longitude':float(candidate['lng'])}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _choices(query, candidates):
    return [{**row,'fingerprint':candidate_fingerprint(query,row)} for row in candidates]


def _tile(lat,lng,zoom):
    n=2**zoom
    x=min(n-1,max(0,math.floor((lng+180)/360*n)))
    y=min(n-1,max(0,math.floor((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n)))
    return x,y


def _utc(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise MapAcquisitionError('map timestamp requires explicit timezone')
    return parsed.astimezone(timezone.utc)


def validate_candidate_selection(candidate_index, candidate_confirmation):
    """Validate caller's static choice shape without network or cache access."""
    if candidate_index is not None and (type(candidate_index) is not int or candidate_index<0):
        raise MapAcquisitionError('candidate index must be a nonnegative integer')
    if candidate_confirmation is not None and (candidate_index is None or not isinstance(candidate_confirmation,str) or not re.fullmatch(r'[0-9a-f]{64}',candidate_confirmation)):
        raise MapAcquisitionError('candidate confirmation requires an index and SHA256 fingerprint')


def _cache_key(query, candidate_index, candidate_confirmation, zoom=16):
    return hashlib.sha256(json.dumps({'version':VERSION,'address':query,'candidate_index':candidate_index,'candidate_confirmation':candidate_confirmation,'zoom':zoom,'style':'standard','scale':2},sort_keys=True).encode()).hexdigest()


def acquire_cover_map(address,*,cache_directory,client=None,candidate_index=None,candidate_confirmation=None,zoom=16,now=None,offline=False):
    """Return uncropped Apple PNG and source receipt, or fail without a map.

    Cache is private, hash-addressed and bounded by OA response max-age (up to
    24h). Cached bytes are checked against their receipt on every read.
    """
    if not isinstance(address,str) or not address.strip() or len(address)>300 or any(ord(c)<32 for c in address):
        raise MapAcquisitionError('map acquisition needs a nonempty single-line address within OA query limits')
    if type(zoom) is not int or not 3<=zoom<=19:
        raise MapAcquisitionError('map zoom must be an integer from 3 through 19')
    validate_candidate_selection(candidate_index,candidate_confirmation)
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise MapAcquisitionError('explicit timezone required')
    query=address.strip();cache=Path(cache_directory)
    key=_cache_key(query,candidate_index,candidate_confirmation,zoom)
    receipt_path=cache/(key+'.json')
    if receipt_path.exists():
        try:
            record=json.loads(receipt_path.read_text())
            if record['version']!=VERSION or record['query']!=query or record['provider']!='apple' or record['uncropped'] is not True:
                raise MapAcquisitionError('cached map source receipt mismatch')
            if not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256']):
                raise MapAcquisitionError('cached map hash must be SHA256')
            if receipt_path.is_symlink():
                raise MapAcquisitionError('cached receipt symlinks are refused')
            png=cache/(record['sha256']+'.png')
            if png.is_symlink():
                raise MapAcquisitionError('cached map symlinks are refused')
            provenance=record['provenance']
            if not isinstance(provenance, dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in provenance.items()):
                raise MapAcquisitionError('invalid cached map provenance')
            if provenance.get('query')!=query or provenance.get('provider')!='apple' or provenance.get('sha256')!=record['sha256'] or provenance.get('zoom')!=str(zoom):
                raise MapAcquisitionError('cached map provenance binding mismatch')
            _numeric_address_check(query, provenance['resolved_address'])
            cached_candidate={'label':provenance['resolved_address'],'lat':float(provenance['latitude']),'lng':float(provenance['longitude'])}
            if (candidate_index is not None and candidate_confirmation!=candidate_fingerprint(query,cached_candidate)) or (candidate_index is None and _normalized_address(query)!=_normalized_address(cached_candidate['label'])):
                raise MapAmbiguityError(_choices(query,[cached_candidate]))
            if _utc(provenance['fetched_at'])>now or _utc(record['expires_at'])-_utc(provenance['fetched_at'])>timedelta(days=1):
                raise MapAcquisitionError('invalid cached map validity interval')
            data=png.read_bytes()
            digest,w,h=validate_png(data,record['sha256'])
            if _utc(record['expires_at'])>now:
                return CoverMapSnapshot(data,digest,w,h,ATTRIBUTION,dict(record['provenance']))
        except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
            raise MapAcquisitionError('cached map is incomplete or malformed') from exc
    if offline:raise MapAcquisitionError('no fresh verified Apple map in the supplied private cache')
    client=client or OfficeAdminMapClient()
    geocode,_=client.request(GEOCODE_PATH,{'q':query})
    if not isinstance(geocode,dict) or geocode.get('provider')!='apple' or not isinstance(geocode.get('results'),list):
        raise MapAcquisitionError('Apple geocoding unavailable; another provider is not accepted')
    candidates=[_candidate(row) for row in geocode['results']]
    if not candidates:raise MapAcquisitionError('Apple returned no geocode candidate')
    if candidate_index is None and len(candidates)!=1:raise MapAmbiguityError(_choices(query,candidates))
    chosen=0 if candidate_index is None else candidate_index
    if chosen>=len(candidates):raise MapAcquisitionError('selected map candidate is outside returned results')
    selected=candidates[chosen];_numeric_address_check(query,selected['label'])
    if candidate_index is not None and candidate_confirmation!=candidate_fingerprint(query,selected):
        raise MapAmbiguityError(_choices(query,candidates))
    if candidate_index is None and _normalized_address(query)!=_normalized_address(selected['label']):
        raise MapAmbiguityError(_choices(query,candidates))
    imagery,_=client.request(IMAGERY_PATH,{})
    if not isinstance(imagery,dict) or imagery.get('provider')!='apple':
        raise MapAcquisitionError('Apple snapshot capability unavailable; no Esri fallback accepted')
    x,y=_tile(selected['lat'],selected['lng'],zoom)
    path=f'/tools/site-plan/tiles/{zoom}/{x}/{y}'
    data,transport=client.request(path,{'style':'standard','scale':2},binary=True)
    if transport.get('status')!=200 or transport.get('headers',{}).get('x-imagery-provider')!='apple':
        raise MapAcquisitionError('map response does not prove Apple source')
    digest,w,h=validate_png(data)
    if (w,h)!=(896,896):
        raise MapAcquisitionError('snapshot dimensions differ from the full uncropped OA retina snapshot')
    n=2**zoom;center_lng=((x+.5)/n)*360-180
    center_lat=math.degrees(math.atan(math.sinh(math.pi*(1-2*(y+.5)/n))))
    cache_control=transport['headers'].get('Cache-Control','')
    match=re.search(r'(?:^|[, ])max-age=(\d+)',cache_control)
    ttl=min(int(match.group(1)) if match else 0,86400)
    if re.search(r'(?:^|[, ])(?:no-store|no-cache)(?:[, ]|$)', cache_control, re.I):
        ttl=0
    expires=now+timedelta(seconds=ttl)
    provenance={'version':VERSION,'provider':'apple','query':query,'resolved_address':selected['label'],
       'latitude':str(selected['lat']),'longitude':str(selected['lng']),'candidate_index':str(chosen),'candidate_fingerprint':candidate_fingerprint(query,selected),
       'source_endpoint':path,'style':'standard','scale':'2','zoom':str(zoom),'tile_x':str(x),'tile_y':str(y),
       'snapshot_center_latitude':str(center_lat),'snapshot_center_longitude':str(center_lng),
       'snapshot_size_points':'448','snapshot_pad_points':'96','uncropped':'true',
       'sha256':digest,'width_px':str(w),'height_px':str(h),
       'fetched_at':now.isoformat(),'expires_at':expires.isoformat()}
    record={'version':VERSION,'provider':'apple','query':query,'uncropped':True,'sha256':digest,
       'expires_at':expires.isoformat(),'provenance':provenance}
    if ttl == 0:
        return CoverMapSnapshot(data,digest,w,h,ATTRIBUTION,provenance)
    cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    asset_path=cache/(digest+'.png')
    if asset_path.exists():validate_png(asset_path.read_bytes(),digest)
    else:
        with asset_path.open('xb') as stream:stream.write(data)
    # Receipt last: interrupted writes never create a reusable incomplete entry.
    temporary=cache/(key+'.pending')
    with temporary.open('x') as stream:json.dump(record,stream,sort_keys=True,indent=2)
    os.replace(temporary,receipt_path)
    return CoverMapSnapshot(data,digest,w,h,ATTRIBUTION,provenance)
