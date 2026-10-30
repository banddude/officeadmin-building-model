"""Synthetic Apple adapter receipts; no network or private locations."""
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image
import pytest
from oabm.drawings.cover_maps import acquire_cover_map, MapAcquisitionError, MapAmbiguityError, candidate_fingerprint

class Client:
    def __init__(self, provider='apple', ambiguous=False):
        self.provider=provider; self.ambiguous=ambiguous; self.calls=[]
    def request(self,path,query,*,binary=False):
        self.calls.append(path)
        receipt={'status':200,'headers':{'x-imagery-provider':self.provider,'Cache-Control':'max-age=3600'}}
        if binary:
            out=io.BytesIO();Image.new('RGB',(896,896),'white').save(out,format='PNG')
            return out.getvalue(),receipt
        if path.endswith('geocode'):
            rows=[{'label':'123 Example Street, Test City 99999','lat':34.,'lng':-118.}]
            return {'provider':self.provider,'results':rows*(2 if self.ambiguous else 1)},receipt
        return {'provider':self.provider},receipt

NOW=datetime(2026,1,1,tzinfo=timezone.utc)
ADDRESS='123 Example Street, Test City 99999'

def test_cache_returns_the_same_bytes_that_were_verified(tmp_path, monkeypatch):
    original=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=Client(),now=NOW)
    read_bytes=Path.read_bytes
    reads=[]
    def changing_read(path):
        if path.suffix == '.png':
            reads.append(path)
            if len(reads)>1:
                return b'changed after validation'
        return read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', changing_read)
    cached=acquire_cover_map(ADDRESS,cache_directory=tmp_path,now=NOW,offline=True)
    assert len(reads)==1
    assert cached.png_bytes==original.png_bytes
    assert hashlib.sha256(cached.png_bytes).hexdigest()==cached.sha256

def test_verified_cache_roundtrip_and_traversal_refusal(tmp_path):
    client=Client(); a=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW)
    assert a.sha256==hashlib.sha256(a.png_bytes).hexdigest()
    assert a.provenance['uncropped']=='true'
    b=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=Client('esri'),now=NOW,offline=True)
    assert a==b
    receipt=next(tmp_path.glob('*.json'));record=json.loads(receipt.read_text());record['sha256']='../outside'
    receipt.write_text(json.dumps(record))
    with pytest.raises(MapAcquisitionError,match='SHA256'):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,now=NOW,offline=True)

@pytest.mark.parametrize('client,error',[(Client('esri'),MapAcquisitionError),(Client(ambiguous=True),MapAmbiguityError)])
def test_provider_and_ambiguity_fail_before_asset(tmp_path,client,error):
    with pytest.raises(error):acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW)
    assert not list(tmp_path.iterdir())


class AddressClient(Client):
    def __init__(self,label):super().__init__();self.label=label
    def request(self,path,query,*,binary=False):
        data,receipt=super().request(path,query,binary=binary)
        if path.endswith('geocode'):data['results'][0]['label']=self.label
        return data,receipt


def test_same_number_postcode_wrong_street_city_requires_confirmation(tmp_path):
    client=AddressClient('123 Different Avenue, Other City 99999')
    with pytest.raises(MapAmbiguityError):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW)
    assert len(client.calls)==1  # No imagery acquisition for unresolved location.
    assert not list(tmp_path.iterdir())
    confirmed=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW,candidate_index=0,candidate_confirmation=candidate_fingerprint(ADDRESS,{'label':client.label,'lat':34.,'lng':-118.}))
    assert confirmed.provenance['resolved_address']==client.label


def test_legitimate_street_abbreviation_is_automatic(tmp_path):
    client=AddressClient('123 Example St., Test City 99999')
    result=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW)
    assert result.provenance['resolved_address']==client.label


def test_cached_mismatched_address_also_requires_confirmation(tmp_path):
    acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=Client(),now=NOW)
    receipt=next(tmp_path.glob('*.json'));record=json.loads(receipt.read_text())
    record['provenance']['resolved_address']='123 Different Avenue, Other City 99999'
    receipt.write_text(json.dumps(record))
    with pytest.raises(MapAmbiguityError):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,now=NOW,offline=True)


def test_confirmation_refuses_changed_or_reordered_candidate(tmp_path):
    old={'label':'123 Different Avenue, Other City 99999','lat':34.,'lng':-118.}
    fingerprint=candidate_fingerprint(ADDRESS,old)
    client=AddressClient('123 Another Road, Other City 99999')
    with pytest.raises(MapAmbiguityError):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW,candidate_index=0,candidate_confirmation=fingerprint)
    assert len(client.calls)==1
    # Even a matching address cannot reuse confirmation for another query or coordinates.
    assert fingerprint!=candidate_fingerprint('123 Other St, Other City 99999',old)
    assert fingerprint!=candidate_fingerprint(ADDRESS,dict(old,lat=34.001))
    with pytest.raises(MapAmbiguityError):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=AddressClient(old['label']),now=NOW,candidate_index=0)


def test_candidate_reordering_requires_new_index_for_same_fingerprint(tmp_path):
    old={'label':'123 Different Avenue, Other City 99999','lat':34.,'lng':-118.}
    other={'label':'123 Another Road, Other City 99999','lat':34.01,'lng':-118.01}
    class Reordered(Client):
        def request(self,path,query,*,binary=False):
            data,receipt=super().request(path,query,binary=binary)
            if path.endswith('geocode'):data['results']=[other,old]
            return data,receipt
    client=Reordered();fingerprint=candidate_fingerprint(ADDRESS,old)
    with pytest.raises(MapAmbiguityError):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW,candidate_index=0,candidate_confirmation=fingerprint)
    assert len(client.calls)==1
    result=acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW,candidate_index=1,candidate_confirmation=fingerprint)
    assert result.provenance['resolved_address']==old['label']


def test_unit_number_cannot_confirm_wrong_house_number(tmp_path):
    label='999 Other Avenue Apt 123, Test City 99999'
    client=AddressClient(label)
    fingerprint=candidate_fingerprint(ADDRESS,{'label':label,'lat':34.,'lng':-118.})
    with pytest.raises(MapAcquisitionError,match='street number'):
        acquire_cover_map(ADDRESS,cache_directory=tmp_path,client=client,now=NOW,candidate_index=0,candidate_confirmation=fingerprint)
    assert len(client.calls)==1
    assert not list(tmp_path.iterdir())
