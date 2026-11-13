"""Local Forgejo destination support."""
import http.client
import json
import re
from pathlib import Path


def local_api(path, token_file):
    if not re.fullmatch(r'api/v1/[A-Za-z0-9_/-]+', path) or '..' in path.split('/'):
        raise ValueError('invalid API path')
    token = Path(token_file).read_text().strip()
    connection = http.client.HTTPConnection('127.0.0.1', 3300, timeout=30)
    try:
        connection.request('GET', '/' + path, headers={'Authorization': 'token ' + token})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'Forgejo API HTTP {response.status}')
        return json.loads(response.read())
    finally:
        connection.close()


def source_identity(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*', value) or value.endswith('.git'):
        raise ValueError('invalid source identity')
    return tuple(value.split('/'))


def source_assets(assets):
    seen = set()
    for asset in assets:
        name = asset.get('name')
        if not isinstance(name, str) or not name or name in seen or asset.get('state') != 'uploaded':
            raise ValueError('incomplete or ambiguous source assets')
        seen.add(name)
    return assets
