"""Durable migration journal (`migrations.json`).

One record per source repository that a mirror run detected as renamed or
transferred. The record is written *before* any destination rename so that a
later run can reconstruct the intended configuration update and finish or
refuse the transaction.

The journal is separate from the resource failure journal on purpose: the two
files have different writers and conflicting write rules, and the failure
journal's public schema must not grow transaction state.

Records are authenticated but not encrypted. They contain only values already
published in the mirror configuration (source URL, source ID, destination name
and ID), never credentials, tokens or signed links.
"""
import hashlib
import hmac
import json
import re

from repo_mirror.config_json import unique_pairs

PHASES = ('prepared', 'synced', 'renamed', 'configured')
_DOMAIN = b'repo-mirror:migrations:v1\0'
_ORIGIN = re.compile(r'https://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_SHA40 = re.compile(r'[0-9a-f]{40}')
_NAME = re.compile(r'[A-Za-z0-9_.\-]+')


def journal_key(source_origin, source_id):
    """Identify a source repository across renames by forge origin and ID."""
    if not isinstance(source_origin, str) or not _ORIGIN.fullmatch(source_origin):
        raise ValueError('invalid source origin')
    if type(source_id) is not int or source_id <= 0:
        raise ValueError('invalid source id')
    return hashlib.sha256(f'{source_origin.lower()}|{source_id}'.encode()).hexdigest()


def _name(value):
    return isinstance(value, str) and bool(_NAME.fullmatch(value)) and value not in ('.', '..')


def _origin(value):
    return isinstance(value, str) and bool(_ORIGIN.fullmatch(value))


def _validate_entry(entry):
    if not isinstance(entry, dict) or set(entry) != {
            'source', 'destination', 'recovery', 'config', 'phase'}:
        raise ValueError('invalid migration entry')
    source, destination = entry['source'], entry['destination']
    recovery, config = entry['recovery'], entry['config']
    if (not isinstance(source, dict) or set(source) != {'origin', 'id', 'owner', 'name'}
            or not _origin(source['origin']) or type(source['id']) is not int or source['id'] <= 0
            or not _name(source['owner']) or not _name(source['name'])):
        raise ValueError('invalid migration source')
    if (not isinstance(destination, dict)
            or set(destination) != {'profile', 'owner', 'name', 'id'}
            or not _name(destination['profile']) or not _name(destination['owner'])
            or not _name(destination['name']) or type(destination['id']) is not int
            or destination['id'] <= 0):
        raise ValueError('invalid migration destination')
    # The recovery binding is the *original* source/destination pair. It must
    # stay unchanged across subsequent moves so release recovery and journal
    # lookups keep resolving to work created before the first rename.
    if (not isinstance(recovery, dict)
            or set(recovery) != {'source_origin', 'source_owner', 'source_name',
                                 'destination_name', 'signature'}
            or not _origin(recovery['source_origin'])
            or not _name(recovery['source_owner']) or not _name(recovery['source_name'])
            or not _name(recovery['destination_name'])
            or not isinstance(recovery['signature'], str) or not recovery['signature']):
        raise ValueError('invalid migration recovery binding')
    if (not isinstance(config, dict) or set(config) != {'path', 'base_sha'}
            or config['path'] not in ('config.json', 'config-forgejo.json')
            or not isinstance(config['base_sha'], str) or not _SHA40.fullmatch(config['base_sha'])):
        raise ValueError('invalid migration config binding')
    if entry['phase'] not in PHASES:
        raise ValueError('invalid migration phase')
    return entry


class MigrationsJournal:
    """Authenticated view over one `migrations.json` document."""

    def __init__(self, document, secret):
        if not isinstance(secret, str) or len(secret.encode()) < 32:
            raise ValueError('invalid migrations journal secret')
        self.secret = secret.encode()
        self.document = self._validate(document)

    # -- envelope ---------------------------------------------------------
    def _signature(self, payload):
        return hmac.new(self.secret, _DOMAIN + _encode(payload), hashlib.sha256).hexdigest()

    def _validate(self, document):
        if document is None:
            return {'version': 1, 'migrations': {}, 'signatures': {}}
        try:
            document = json.loads(_encode(document))
        except (TypeError, ValueError):
            raise ValueError('invalid migrations journal') from None
        if (not isinstance(document, dict)
                or set(document) != {'version', 'migrations', 'signatures'}
                or document['version'] != 1
                or not isinstance(document['migrations'], dict)
                or not isinstance(document['signatures'], dict)
                or set(document['migrations']) != set(document['signatures'])):
            raise ValueError('invalid migrations journal')
        for key, entry in document['migrations'].items():
            if not isinstance(key, str) or not _HEX64.fullmatch(key):
                raise ValueError('invalid migrations journal key')
            _validate_entry(entry)
            expected = self._signature(entry)
            signature = document['signatures'][key]
            if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                raise ValueError('migrations journal entry signature mismatch')
        return document

    # -- reads ------------------------------------------------------------
    def entries(self):
        return json.loads(_encode(self.document['migrations']))

    def get(self, source_origin, source_id):
        return self.document['migrations'].get(journal_key(source_origin, source_id))

    def pending(self):
        return {key: entry for key, entry in self.document['migrations'].items()
                if entry['phase'] != 'configured'}

    # -- writes (return a new journal; callers publish it) -----------------
    def put(self, entry):
        _validate_entry(entry)
        key = journal_key(entry['source']['origin'], entry['source']['id'])
        current = self.document['migrations'].get(key)
        if current is not None and current != entry:
            raise ValueError('migration entry already recorded with different content')
        return self._rewrite(key, entry)

    def _rewrite(self, key, entry):
        migrations = self.entries()
        signatures = dict(self.document['signatures'])
        migrations[key] = json.loads(_encode(entry))
        signatures[key] = self._signature(entry)
        return MigrationsJournal({'version': 1, 'migrations': migrations,
                                  'signatures': signatures}, self.secret.decode())

    def advance(self, source_origin, source_id, entry):
        """Forward-only phase transition of an existing record."""
        key = journal_key(source_origin, source_id)
        current = self.document['migrations'].get(key)
        if current is None:
            raise ValueError('migration entry not recorded')
        _validate_entry(entry)
        if (entry['source'], entry['destination'], entry['recovery'], entry['config']) != (
                current['source'], current['destination'], current['recovery'], current['config']):
            raise ValueError('migration entry binding mismatch')
        if PHASES.index(entry['phase']) != PHASES.index(current['phase']) + 1:
            raise ValueError('invalid migration phase transition')
        return self._rewrite(key, entry)

    def remove(self, source_origin, source_id):
        """Drop a completed record; the transaction is finished."""
        key = journal_key(source_origin, source_id)
        if self.document['migrations'].get(key, {}).get('phase') != 'configured':
            raise ValueError('migration is not complete')
        migrations = self.entries()
        signatures = dict(self.document['signatures'])
        del migrations[key]
        del signatures[key]
        return MigrationsJournal({'version': 1, 'migrations': migrations,
                                  'signatures': signatures}, self.secret.decode())

    def encode(self):
        return (_encode_document({'version': 1, 'migrations': self.document['migrations'],
                                  'signatures': self.document['signatures']}) + b'\n')


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _encode_document(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2,
                      allow_nan=False).encode('ascii')


def parse(raw, secret):
    """Parse journal bytes as published, rejecting duplicate keys and tampering."""
    try:
        document = json.loads(raw, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('invalid migrations journal') from None
    return MigrationsJournal(document, secret)
