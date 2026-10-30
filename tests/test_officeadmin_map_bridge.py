"""Exercise the bridge's actual urllib chain without sockets or credentials."""
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize('status', [200, 302])
def test_bridge_receipts_and_redirect_refusal(tmp_path, status):
    script=tmp_path/'synthetic_client.py';receipt=tmp_path/'receipt.json';followed=tmp_path/'followed'
    script.write_text('''import urllib.request, urllib.response, urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
class SyntheticHTTP(urllib.request.BaseHandler):
    handler_order=100
    def http_open(self, request):
        headers=Message()
        headers['Content-Type']='image/png'
        headers['x-imagery-provider']='apple'
        headers['Location']='http://synthetic.invalid/fallback'
        if request.full_url.endswith('/fallback'):
            Path(FOLLOWED).write_text('unexpected redirect followed')
        response=urllib.response.addinfourl(BytesIO(b'fixture'),headers,request.full_url,STATUS)
        response.msg='synthetic'
        return response
urllib.request._opener.add_handler(SyntheticHTTP())
try:
    urllib.request.urlopen('http://synthetic.invalid/api/v1/tools/site-plan/tiles/16/1/2')
except urllib.error.HTTPError as error:
    assert error.code==302
'''.replace('FOLLOWED',repr(str(followed))).replace('STATUS',str(status)))
    result=subprocess.run([sys.executable,'-m','oabm.drawings.officeadmin_map_bridge',str(receipt),str(script)],capture_output=True)
    assert result.returncode==0,result.stderr.decode()
    assert not followed.exists()
    row=json.loads(receipt.read_text())['responses'][0]
    assert row['status']==status
    assert row['headers']['x-imagery-provider']=='apple'
    assert 'Location' not in row['headers']
