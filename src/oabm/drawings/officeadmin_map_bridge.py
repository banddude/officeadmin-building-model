"""Read-only response receipt around the installed OA client. Never handles keys."""
from __future__ import annotations
import json
from pathlib import Path
import runpy
import sys
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MapReceipt(urllib.request.BaseHandler):
    handler_order = 400

    def __init__(self, destination):
        self.destination = Path(destination)
        self.rows = []

    def http_response(self, request, response):
        path = urllib.parse.urlsplit(request.full_url).path
        if path.startswith('/api/v1/tools/site-plan/'):
            self.rows.append({'path': path, 'status': response.status,
                'headers': {key: response.headers.get(key, '') for key in
                    ('Content-Type', 'x-imagery-provider', 'Cache-Control', 'Date', 'ETag')}})
            self.destination.write_text(json.dumps({'version':'oa-map-transport-v1','responses':self.rows}, sort_keys=True))
        return response

    https_response = http_response


def main():
    receipt, script, *arguments = sys.argv[1:]
    client = Path(script)
    if not client.is_file():
        raise SystemExit('installed OfficeAdmin client script unavailable')
    urllib.request.install_opener(urllib.request.build_opener(NoRedirect(), MapReceipt(receipt)))
    sys.path.insert(0, str(client.parent))
    sys.argv = [str(client), *arguments]
    runpy.run_path(str(client), run_name='__main__')


if __name__ == '__main__':
    main()
