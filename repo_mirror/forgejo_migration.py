"""Crash-durable owner-change migration for a Forgejo mirror destination.

A worker that verifies a pinned source relocation mirrors into the *existing*
destination and reports the move. This module finishes the transaction through
the shared, provider-neutral ``migrations.MigrationTransaction`` engine:

1. the intent is recorded in the durable migration journal (``migrations.json``
   in the owner-only journal directory) before any destination rename,
2. both repository pins are rechecked from live metadata,
3. the existing destination is renamed only when its own ID already sits at the
   recorded name or its candidate name is free,
4. the candidate manifest is published to the authoritative GitHub repository
   through a byte-conditional write with readback,
5. only then is the intent removed.

Only the Forgejo-specific parts remain here: how a manifest entry is located,
how the source pin and the destination marker are verified, how the destination
is renamed and how the manifest is published. The journal phase protocol itself
lives in ``migrations.MigrationTransaction``.

Every phase is resumable from the recorded phase. A conditional write is not a
transaction and a rename is not atomic, so any conflict fails closed and leaves
the intent pending for review. Non-cooperating external writers can still race
between a check and a following write.
"""
import json
import os

from repo_mirror.config_json import unique_pairs
from repo_mirror.forgejo_automatic_transition import prepare_source_transition
from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter
from repo_mirror.forgejo_git import _source_marker
from repo_mirror.forgejo_sync import parse_manifest, verify_source_pin
from repo_mirror.migrations import MigrationTransaction, blob_sha
from repo_mirror.migrations_store import build_entry

CONFIG_PATH = 'config-forgejo.json'


class ForgejoRenameAPI:
    """Pinned-by-caller Forgejo repository lookup and rename."""

    def __init__(self, api, owner):
        self.api, self.owner = api, owner

    def get(self, name):
        from repo_mirror.forgejo_git import APIError
        try:
            return self.api.request('GET', f'/api/v1/repos/{self.owner}/{name}')
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def rename(self, old, new):
        self.api.request('PATCH', f'/api/v1/repos/{self.owner}/{old}', {'name': new})


class MigrationRefused(Exception):
    """A migration step refused to proceed; the journal keeps the last phase."""


def encode_document(document):
    """Serialize a manifest the way the authoritative configuration is stored."""
    return (json.dumps(document, indent=2, ensure_ascii=True) + '\n').encode('ascii')


def _provider(origin):
    return 'github' if origin.lower() == 'https://github.com' else 'forgejo'


def _url(part):
    return f"{part['origin']}/{part['owner']}/{part['name']}"


def _entries(data):
    try:
        return parse_manifest(json.loads(data, object_pairs_hook=unique_pairs))
    except (UnicodeDecodeError, ValueError):
        raise MigrationRefused('migration state inconsistent') from None


class _ManifestConfiguration:
    """Adapt the byte-conditional config CAS to the transaction's read/publish."""

    def __init__(self, cas):
        self.cas = cas

    def read(self):
        try:
            data = self.cas.read()
        except MigrationRefused:
            raise
        except Exception:
            raise MigrationRefused('configuration publication failed') from None
        if not isinstance(data, bytes):
            raise MigrationRefused('configuration publication failed')
        return data, blob_sha(data)

    def publish(self, expected_sha, replacement):
        data, sha = self.read()
        if sha != expected_sha:
            raise MigrationRefused('configuration publication failed')
        try:
            self.cas.replace(data, replacement)
        except MigrationRefused:
            raise
        except Exception:
            raise MigrationRefused('configuration publication failed') from None


class _ForgejoPolicy:
    """Provider adapter binding the shared transaction to the Forgejo manifest."""

    def __init__(self, source, port, owner, secret):
        self.source, self.port, self.owner, self.secret = source, port, owner, secret

    def error(self, message):
        return MigrationRefused(message)

    # -- source -----------------------------------------------------------
    def metadata(self, origin, owner, name):
        try:
            metadata = self.source(origin, owner, name)
        except MigrationRefused:
            raise
        except Exception:
            raise MigrationRefused('source pin mismatch') from None
        if not isinstance(metadata, dict):
            raise MigrationRefused('source pin mismatch')
        return metadata

    def verify_source(self, entry):
        part = entry['source']
        metadata = self.metadata(part['origin'], part['owner'], part['name'])
        try:
            verify_source_pin(_url(part), metadata, part['id'])
        except ValueError:
            raise MigrationRefused('source pin mismatch') from None

    # -- configuration ----------------------------------------------------
    def _locate(self, entries, entry):
        for index, item in enumerate(entries):
            if item.get('source_id') != entry['source']['id']:
                continue
            if item['url'].rsplit('/', 2)[0].lower() != entry['source']['origin'].lower():
                continue
            return index
        raise MigrationRefused('migration state inconsistent')

    def _document(self, configuration, entry):
        data = configuration.read()[0]
        entries = _entries(data)
        return data, entries[self._locate(entries, entry)]

    def verify_config(self, configuration, entry):
        data, item = self._document(configuration, entry)
        if item['url'].lower() == _url(entry['source']).lower():
            # Published before the rename: the state is not recoverable here.
            raise MigrationRefused('migration state inconsistent')
        if blob_sha(data) != entry['config']['base_sha']:
            raise MigrationRefused('configuration changed by another writer')

    # -- destination ------------------------------------------------------
    def _get(self, repository, name):
        try:
            return repository.get(name)
        except MigrationRefused:
            raise
        except Exception:
            raise MigrationRefused('destination identity mismatch') from None

    def _verify_repo(self, repository, entry, name):
        repo = self._get(repository, name)
        if repo is None:
            raise MigrationRefused('destination missing')
        if (not isinstance(repo, dict) or type(repo.get('id')) is not int
                or repo['id'] != entry['destination']['id']
                or not isinstance(repo.get('full_name'), str)
                or repo['full_name'].lower() != f'{self.owner}/{name}'.lower()
                or repo.get('private') is not True):
            raise MigrationRefused('destination identity mismatch')
        recovery = entry['recovery']
        # Forgejo destinations carry the Forgejo marker format
        # ('repo-mirror-source: host/owner/repo'), built from the *original*
        # recovery binding so a move never invalidates it.
        if recovery['source_origin'].lower() == 'https://github.com':
            marker_source = f"{recovery['source_owner']}/{recovery['source_name']}"
        else:
            marker_source = (f"{recovery['source_origin'].rstrip('/')}/"
                             f"{recovery['source_owner']}/{recovery['source_name']}")
        marker = _source_marker(marker_source)
        # Forgejo keeps a third, post-initialization marker form that the shared
        # GitHub check does not know about.
        allowed = (marker, marker + ' [repo-mirror:initializing:v1]',
                   marker + ' [repo-mirror:initialized:v1]')
        if repo.get('description') not in allowed:
            raise MigrationRefused('destination identity mismatch')

    def _current_name(self, repository, configuration, entry):
        """Return the name the pinned destination currently has, by ID."""
        _, item = self._document(configuration, entry)
        for name in dict.fromkeys((item['destination_name'], entry['destination']['name'])):
            repo = self._get(repository, name)
            if repo is not None and repo.get('id') == entry['destination']['id']:
                self._verify_repo(repository, entry, name)
                return name
        raise MigrationRefused('destination identity mismatch')

    def verify_destination(self, repository, configuration, entry, phase):
        if phase == 'renamed':
            self._verify_repo(repository, entry, entry['destination']['name'])
        else:
            self._current_name(repository, configuration, entry)

    def rename(self, repository, configuration, entry):
        candidate = entry['destination']['name']
        target = self._get(repository, candidate)
        if target is not None:
            if target.get('id') != entry['destination']['id']:
                # Never clobber another repository, even with the same name.
                raise MigrationRefused('destination collision')
            self._verify_repo(repository, entry, candidate)
            return
        current = self._current_name(repository, configuration, entry)
        if current.lower() == candidate.lower():
            raise MigrationRefused('destination missing')
        try:
            repository.rename(current, candidate)
        except MigrationRefused:
            raise
        except Exception:
            raise MigrationRefused('destination rename failed') from None
        self._verify_repo(repository, entry, candidate)

    def publish(self, configuration, entry):
        data = configuration.read()[0]
        entries = _entries(data)
        try:
            document = json.loads(data, object_pairs_hook=unique_pairs)
        except (UnicodeDecodeError, ValueError):
            raise MigrationRefused('migration state inconsistent') from None
        index = self._locate(entries, entry)
        item = entries[index]
        if item['url'].lower() == _url(entry['source']).lower():
            # A previous run already published; only the bookkeeping is left.
            if item['destination_name'] != entry['destination']['name']:
                raise MigrationRefused('migration state inconsistent')
            return None
        # The pre-transition entry must still carry both pinned identities, so a
        # second move of the same repository is recognised by ID, not by name.
        if (item.get('source_id') != entry['source']['id']
                or item.get('destination_id') != entry['destination']['id']):
            raise MigrationRefused('migration state inconsistent')
        if blob_sha(data) != entry['config']['base_sha']:
            raise MigrationRefused('configuration changed by another writer')
        origin, owner, name = item['url'].rsplit('/', 2)
        metadata = self.metadata(origin, owner, name)
        try:
            candidate = prepare_source_transition(document, index, metadata, self.port, self.owner, self.secret)
            moved = parse_manifest(candidate)[index]
        except ValueError:
            raise MigrationRefused('source pin mismatch') from None
        if (moved['url'].lower() != _url(entry['source']).lower()
                or moved['destination_name'] != entry['destination']['name']):
            raise MigrationRefused('migration state inconsistent')
        replacement = encode_document(candidate)
        configuration.publish(entry['config']['base_sha'], replacement)
        return replacement


class ForgejoMigration:
    """Resumable rename and configuration publication for one relocation."""

    def __init__(self, store, cas, destination, source, secret, port, owner, config_path=CONFIG_PATH):
        if not isinstance(secret, str) or len(secret.encode()) < 32:
            raise ValueError('invalid migration secret')
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError('invalid port')
        if not isinstance(owner, str) or not owner:
            raise ValueError('invalid destination owner')
        if config_path not in ('config.json', 'config-forgejo.json'):
            raise ValueError('invalid configuration path')
        self.store, self.cas, self.destination, self.source = store, cas, destination, source
        self.secret, self.port, self.owner, self.config_path = secret, port, owner, config_path
        self.policy = _ForgejoPolicy(source, port, owner, secret)
        self.transaction = MigrationTransaction(
            store, destination, _ManifestConfiguration(cas), secret=secret,
            config_path=config_path, policy=self.policy)

    # -- entry points -----------------------------------------------------
    def pending(self):
        return self.store.pending()

    def resume(self):
        """Finish every recorded intent; returns (completed entries, published bytes)."""
        pending = self.store.pending()
        completed, published = [], None
        for entry in sorted(pending.values(),
                            key=lambda item: (item['source']['origin'], item['source']['id'])):
            finished, changed = self.transaction.drive(entry, record=False)
            completed.append(finished)
            if changed is not None:
                published = changed
        return completed, published

    def apply(self, document, relocation):
        """Record and finish a move a worker reported against `document`.

        `document` must be the authoritative bytes the batch validated; the
        publication target must still contain exactly those bytes. Returns the
        authoritative bytes afterwards so a later relocation can be applied.
        """
        entries = _entries(document)
        if self._config() != document:
            raise MigrationRefused('configuration changed by another writer')
        entry = self._plan(document, self._locate_relocation(entries, relocation))
        self.store.record(entry)
        self.transaction.drive(entry, record=False)
        return self._config()

    # -- planning ---------------------------------------------------------
    @staticmethod
    def _locate_relocation(entries, relocation):
        if (not isinstance(relocation, dict) or set(relocation) != {'url', 'canonical', 'source_id', 'destination_id'}
                or not isinstance(relocation['url'], str) or not isinstance(relocation['canonical'], str)
                or type(relocation['source_id']) is not int or relocation['source_id'] <= 0
                or type(relocation['destination_id']) is not int or relocation['destination_id'] <= 0):
            raise MigrationRefused('invalid relocation report')
        for index, item in enumerate(entries):
            if (item['url'].lower() == relocation['url'].lower()
                    and item.get('source_id') == relocation['source_id']
                    and item.get('destination_id') == relocation['destination_id']):
                return index
        raise MigrationRefused('migration state inconsistent')

    def _plan(self, document_bytes, index):
        try:
            document = json.loads(document_bytes, object_pairs_hook=unique_pairs)
            current = parse_manifest(document)[index]
        except (UnicodeDecodeError, ValueError):
            raise MigrationRefused('migration state inconsistent') from None
        try:
            metadata = self.policy.metadata(*current['url'].rsplit('/', 2))
            candidate = prepare_source_transition(document, index, metadata, self.port, self.owner, self.secret)
            moved = parse_manifest(candidate)[index]
        except ValueError:
            raise MigrationRefused('source relocation is not verified') from None
        if moved['url'] == current['url'] or 'legacy_identity' not in moved:
            raise MigrationRefused('source relocation is not verified')
        # The recovery binding and both IDs survive every later move; the
        # journal key is derived from the source origin and ID, not its name.
        record = {
            'source': {'origin': moved['url'].rsplit('/', 2)[0], 'id': moved['source_id'],
                       'owner': moved['url'].rsplit('/', 2)[1], 'name': moved['url'].rsplit('/', 2)[2]},
            'destination': {'profile': next(iter(document['destinations'])), 'owner': self.owner,
                            'name': moved['destination_name'], 'id': moved['destination_id']},
            'recovery': {'source_origin': moved['legacy_url'].rsplit('/', 2)[0],
                         'source_owner': moved['legacy_url'].rsplit('/', 2)[1],
                         'source_name': moved['legacy_url'].rsplit('/', 2)[2],
                         'destination_name': moved['legacy_destination_name'],
                         'signature': moved['signature']},
            'config': {'path': self.config_path, 'base_sha': blob_sha(document_bytes)},
        }
        try:
            return build_entry(record)
        except ValueError:
            raise MigrationRefused('migration state inconsistent') from None

    # -- configuration ----------------------------------------------------
    def _config(self):
        return self.transaction.configuration.read()[0]


class DeferredMigration:
    """Build the transaction only when a relocation or a pending intent needs it.

    Ordinary batches must not require configuration-publication credentials, so
    construction failures surface as a refusal at the point of use.
    """

    def __init__(self, build):
        if not callable(build):
            raise ValueError('invalid migration builder')
        self.build, self._migration = build, None

    def _load(self):
        if self._migration is None:
            try:
                self._migration = self.build()
            except MigrationRefused:
                raise
            except Exception:
                raise MigrationRefused('configuration publication failed') from None
            if self._migration is None:
                raise MigrationRefused('configuration publication is not configured')
        return self._migration

    def resume(self):
        return self._load().resume()

    def apply(self, document, relocation):
        return self._load().apply(document, relocation)


def build_migration(args, secret):
    """Construct the transaction from scheduler arguments; None when unconfigured."""
    if not getattr(args, 'config_repo', None):
        return None
    from repo_mirror.forgejo_authoritative_config import GitHubConfigCAS
    from repo_mirror.forgejo_git import LocalForgejoAPI
    from repo_mirror.forgejo_sync import read_secure_file
    from repo_mirror.github_api import API, AppTokenProvider, SourceAPI
    from repo_mirror.migrations_store import FileMigrations, MigrationsStore

    owner, separator, repo = args.config_repo.partition('/')
    if not separator or not owner or not repo:
        raise ValueError('invalid configuration repository')
    if args.app_id and args.private_key_file:
        key = read_secure_file(args.private_key_file)[0]
        token = AppTokenProvider(args.app_id, key, owner, getattr(args, 'config_account_type', 'personal'))
    else:
        token = os.environ.get('GITHUB_TOKEN')
        if not token or '\n' in token or '\r' in token:
            raise ValueError('configuration publication requires GitHub App credentials')
    forgejo_token = read_secure_file(args.token_file)[0].strip()
    if not forgejo_token or '\n' in forgejo_token or '\r' in forgejo_token:
        raise ValueError('invalid Forgejo token')
    api = LocalForgejoAPI(f'http://127.0.0.1:{args.port}', args.token_file)
    # Pin the checked credential; re-opening its path could race with replacement.
    api.token = lambda: forgejo_token

    def probe(origin, owner, name):
        identity = f'{origin}/{owner}/{name}'
        if origin.lower() == 'https://github.com':
            client = SourceAPI(token)
            client.restrict_origin('https://api.github.com')
            metadata = client.request('GET', f'https://api.github.com/repos/{owner}/{name}')
        else:
            metadata = PublicForgejoSourceAdapter(identity).request('GET', f'{origin}/api/v1/repos/{owner}/{name}')
        if not isinstance(metadata, dict):
            raise ValueError('invalid source metadata')
        return metadata

    store = MigrationsStore(FileMigrations(os.path.join(args.journal_dir, 'migrations.json')), secret)
    cas = GitHubConfigCAS(API(token), owner, repo, 'main', CONFIG_PATH)
    return ForgejoMigration(store, cas, ForgejoRenameAPI(api, args.owner), probe,
                            secret, args.port, args.owner)
