"""Operator-authorized relocation claims; signatures do not prove history."""
import argparse
import hashlib
import hmac
import json
import os
import re

from repo_mirror.sources import destination_name

DOMAIN = b'repo-mirror:legacy-migration:v1\0'
FIELDS = {'source_provider', 'source_origin', 'old_source_owner', 'old_source_name',
          'current_source_owner', 'current_source_name', 'source_id', 'destination_origin',
          'destination_owner', 'old_destination_name', 'current_destination_name', 'destination_id'}


def github_claim(source, name, owner):
    settings = source['mirror']
    legacy = settings['legacy']
    return dict(source_provider=source['type'], source_origin=source['base_url'].rstrip('/'),
                old_source_owner=legacy['source_owner'], old_source_name=legacy['source_name'],
                current_source_owner=source['owner'], current_source_name=name, source_id=settings['source_id'],
                destination_origin='https://github.com', destination_owner=owner,
                old_destination_name=legacy['destination_name'], current_destination_name=destination_name(source, name),
                destination_id=settings['destination_id'])


def forgejo_claim(entry, port, owner):
    origin, current_owner, current_name = entry['url'].rsplit('/', 2)
    _, old_owner, old_name = entry['legacy_url'].rsplit('/', 2)
    return dict(source_provider='github' if origin.lower() == 'https://github.com' else 'forgejo',
                source_origin=origin, old_source_owner=old_owner, old_source_name=old_name,
                current_source_owner=current_owner, current_source_name=current_name, source_id=entry['source_id'],
                destination_origin=f'http://127.0.0.1:{port}', destination_owner=owner,
                old_destination_name=entry['legacy_destination_name'], current_destination_name=entry['destination_name'],
                destination_id=entry['destination_id'])


def sign(claim, secret):
    if not isinstance(claim, dict) or set(claim) != FIELDS or not isinstance(secret, str) or len(secret.encode('utf-8')) < 32:
        raise ValueError('invalid migration attestation')
    for key, value in claim.items():
        if key in ('source_id', 'destination_id'):
            if type(value) is not int or value <= 0:
                raise ValueError('invalid migration attestation')
        elif not isinstance(value, str) or not value:
            raise ValueError('invalid migration attestation')
    payload = json.dumps(claim, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hmac.new(secret.encode('utf-8'), DOMAIN + payload, hashlib.sha256).hexdigest()


def verify(claim, signature, secret):
    if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature):
        raise ValueError('invalid migration attestation')
    if not hmac.compare_digest(sign(claim, secret), signature):
        raise ValueError('invalid migration attestation')


def main():
    parser = argparse.ArgumentParser(description='Sign an independently verified migration claim; this does not verify history.')
    parser.add_argument('--claim', required=True, help='JSON file containing exactly the migration claim fields')
    parser.add_argument('--i-verified-history', action='store_true', required=True)
    parser.add_argument('--secret-file', help='owner-only signing secret file (Forgejo)')
    args = parser.parse_args()
    if args.secret_file:
        from repo_mirror.forgejo_sync import read_secure_file
        secret = read_secure_file(args.secret_file)[0].strip()
    else:
        secret = os.environ.get('MIRROR_TIMESTAMP_SECRET', '')
    with open(args.claim, encoding='utf-8') as stream:
        claim = json.load(stream)
    print(sign(claim, secret))


if __name__ == '__main__':
    main()
