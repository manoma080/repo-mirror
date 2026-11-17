"""Post-batch migration: plan, execute and recover a detected source relocation.

The mirror worker only *detects* that a source repository moved. It copies into
the existing pinned destination and reports the relocation so the batch can
finish the work afterwards:

    prepared -> synced -> renamed -> configured

Intent is recorded in the durable migration journal (``migrations.json``)
*before* the destination rename, and every later step is derived from live
provider state plus the pinned IDs. Nothing is ever renamed blindly: a name
that no longer resolves to the pinned ID fails closed for operator review.

Helpers here are provider-neutral. They take a duck-typed destination
(``get(name)`` returning ``{'id', 'full_name'}`` or ``None``, and
``rename(old, new)``) and a configuration store (``read()`` returning
``(bytes, blob_sha)`` and ``publish(expected_sha, replacement)``).
``MigrationTransaction`` owns the journal phase protocol; the format-specific
checks (source pin, configuration gate, destination identity, rename and
publication) come from a duck-typed ``policy`` hook, so the Forgejo manifest
runner drives the same body with its own adapters.
"""
import hashlib
import json
import re

from repo_mirror.adhoc import parse_repo_url
from repo_mirror.config_json import unique_pairs
from repo_mirror.config_named import normalize_config
from repo_mirror.migration_attestation import sign, verify
from repo_mirror.migrations_journal import PHASES
from repo_mirror.migrations_store import build_entry
from repo_mirror.sources import destination_name

GITHUB_WEB_ORIGIN = 'https://github.com'
GITHUB_API_ORIGIN = 'https://api.github.com'
CONFIG_PATHS = ('config.json', 'config-forgejo.json')
_SHA40 = re.compile(r'[0-9a-f]{40}')

_DECODER = json.JSONDecoder()


# -- configuration bytes --------------------------------------------------
def blob_sha(raw):
    """Return the git blob SHA of ``raw`` (the identifier the Contents API uses)."""
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def config_document(raw):
    """Parse published configuration bytes, rejecting duplicate keys."""
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('migration configuration invalid') from None


def canonical_origin(origin):
    """Return the web origin of a forge from a relocation's API origin."""
    if not isinstance(origin, str) or not origin:
        raise ValueError('migration source identity mismatch')
    value = origin.rstrip('/')
    if value.endswith('/api/v1'):
        value = value[: -len('/api/v1')]
    if value.lower() == GITHUB_API_ORIGIN:
        return GITHUB_WEB_ORIGIN
    return value


# -- configuration inspection --------------------------------------------
def _destination_mirror(document, destination_id):
    """Return (profile, mirror, repository_entry) pinning one destination ID."""
    if not isinstance(document, dict) or not isinstance(document.get('repositories'), list):
        raise ValueError('migration requires named repository configuration')
    found = []
    for entry in document['repositories']:
        if not isinstance(entry, dict) or not isinstance(entry.get('mirrors'), dict):
            continue
        for profile, mirror in entry['mirrors'].items():
            if isinstance(mirror, dict) and type(mirror.get('id')) is int and mirror['id'] == destination_id:
                found.append((profile, mirror, entry))
    if len(found) != 1:
        raise ValueError('migration destination pin is ambiguous')
    return found[0]


def _source_of(repository):
    source = repository.get('source')
    if not isinstance(source, dict) or not isinstance(source.get('url'), str):
        raise ValueError('migration configuration invalid')
    kind, base, owner, name = parse_repo_url(source['url'])
    return kind, base, owner, name


def config_reflects(document, relocation):
    """True when the configuration already points at the relocated source."""
    destination = relocation['destination']
    _, _, repository = _destination_mirror(document, destination['id'])
    _source_of(repository)
    origin = canonical_origin(relocation['source_origin'])
    target = f"{origin}/{relocation['owner']}/{relocation['name']}"
    return repository['source']['url'].lower() == target.lower()


def destination_plan(relocation, document):
    """Return (profile, mirror, repository_entry, configured_name, target_name).

    The configured name is authoritative. A name that is merely the default
    ``mirror-OWNER-REPO`` derivation for the *old* source identity follows the
    move to the new identity; any other (custom) name is preserved verbatim and
    the destination is not renamed.
    """
    destination = relocation['destination']
    profile, mirror, repository = _destination_mirror(document, destination['id'])
    configured = mirror.get('name')
    if not isinstance(configured, str) or not configured:
        raise ValueError('migration destination name mismatch')
    if configured.lower() != str(destination['name']).lower():
        raise ValueError('migration destination name mismatch')
    kind, base, owner, name = _source_of(repository)
    if canonical_origin(base).lower() != canonical_origin(relocation['source_origin']).lower():
        raise ValueError('migration source identity mismatch')
    if (owner.lower(), name.lower()) != (str(relocation['original_owner']).lower(),
                                         str(relocation['original_name']).lower()):
        raise ValueError('migration source identity mismatch')
    derived = destination_name({'type': kind, 'base_url': base, 'owner': owner, 'repos': [name]}, name)
    if configured.lower() != derived.lower():
        return profile, mirror, repository, configured, configured
    target = destination_name(
        {'type': kind, 'base_url': base, 'owner': relocation['owner'], 'repos': [relocation['name']]},
        relocation['name'])
    return profile, mirror, repository, configured, target


def _binding(config):
    if not isinstance(config, dict) or set(config) != {'document', 'path', 'base_sha'}:
        raise ValueError('migration configuration invalid')
    path, base_sha = config['path'], config['base_sha']
    if path not in CONFIG_PATHS:
        raise ValueError('migration configuration invalid')
    if not isinstance(base_sha, str) or not _SHA40.fullmatch(base_sha):
        raise ValueError('migration configuration invalid')
    document = config['document']
    if isinstance(document, (bytes, str)):
        document = config_document(document)
    if not isinstance(document, dict):
        raise ValueError('migration configuration invalid')
    return document, path, base_sha


def claim(entry, destination_origin=GITHUB_WEB_ORIGIN):
    """Reconstruct the signed operator claim for an entry's original binding."""
    source, destination, recovery = entry['source'], entry['destination'], entry['recovery']
    return dict(
        source_provider='github' if recovery['source_origin'].lower() == GITHUB_WEB_ORIGIN else 'forgejo',
        source_origin=recovery['source_origin'],
        old_source_owner=recovery['source_owner'],
        old_source_name=recovery['source_name'],
        current_source_owner=source['owner'],
        current_source_name=source['name'],
        source_id=source['id'],
        destination_origin=destination_origin,
        destination_owner=destination['owner'],
        old_destination_name=recovery['destination_name'],
        current_destination_name=destination['name'],
        destination_id=destination['id'],
    )


def verify_recovery(entry, secret, destination_origin=GITHUB_WEB_ORIGIN):
    """Fail closed unless the recorded binding carries a valid signature."""
    verify(claim(entry, destination_origin), entry['recovery']['signature'], secret)
    return entry


def plan_migration(relocation, config, secret=None, destination_origin=GITHUB_WEB_ORIGIN):
    """Build the pinned journal entry for one detected relocation.

    ``config`` is a binding mapping with the parsed ``document``, the published
    ``path`` and the ``base_sha`` the intent was planned against. The entry
    carries the *configured* destination name and the original (never
    intermediate) recovery binding.
    """
    document, path, base_sha = _binding(config)
    destination = relocation['destination']
    profile, mirror, repository, current_name, target_name = destination_plan(relocation, document)
    _, _, source_owner, source_name = _source_of(repository)
    origin = canonical_origin(relocation['source_origin'])
    source_id = relocation['source_id']
    if type(source_id) is not int or source_id <= 0:
        raise ValueError('migration source identity mismatch')
    if type(destination['id']) is not int or destination['id'] <= 0:
        raise ValueError('migration destination identity mismatch')
    legacy = mirror.get('legacy')
    if legacy is None:
        binding = {'source_origin': origin, 'source_owner': source_owner,
                   'source_name': source_name, 'destination_name': current_name}
        plan = build_entry({
            'source': {'origin': origin, 'id': source_id,
                       'owner': relocation['owner'], 'name': relocation['name']},
            'destination': {'profile': profile, 'owner': destination['owner'],
                            'name': target_name, 'id': destination['id']},
            'recovery': {**binding, 'signature': '0' * 64},
            'config': {'path': path, 'base_sha': base_sha},
        })
        if not isinstance(secret, str) or len(secret.encode()) < 32:
            raise ValueError('migration signing secret required')
        signature = sign(claim(plan, destination_origin), secret)
    else:
        if (not isinstance(legacy, dict)
                or not {'source_owner', 'source_name', 'destination_name', 'signature'} <= set(legacy)):
            raise ValueError('migration original binding changed')
        binding = {'source_origin': origin, 'source_owner': legacy['source_owner'],
                   'source_name': legacy['source_name'], 'destination_name': legacy['destination_name']}
        if isinstance(secret, str) and len(secret.encode()) >= 32:
            # The surviving binding was signed against the configuration that is
            # being replaced; verify it before carrying it forward unchanged.
            previous = {
                'source': {'origin': origin, 'id': source_id,
                           'owner': source_owner, 'name': source_name},
                'destination': {'profile': profile, 'owner': destination['owner'],
                                'name': current_name, 'id': destination['id']},
                'recovery': {**binding, 'signature': legacy['signature']},
            }
            verify_recovery(previous, secret, destination_origin)
        signature = legacy['signature']
    entry = build_entry({
        'source': {'origin': origin, 'id': source_id,
                   'owner': relocation['owner'], 'name': relocation['name']},
        'destination': {'profile': profile, 'owner': destination['owner'],
                        'name': target_name, 'id': destination['id']},
        'recovery': {**binding, 'signature': signature},
        'config': {'path': path, 'base_sha': base_sha},
    })
    if isinstance(secret, str) and len(secret.encode()) >= 32:
        verify_recovery(entry, secret, destination_origin)
    return entry


# -- byte-exact configuration rewrite ------------------------------------
def _skip_ws(text, index):
    while index < len(text) and text[index] in ' \t\r\n':
        index += 1
    return index


def _object_members(text, index):
    """Yield (key, value_start, value_end) for the object at ``index``."""
    if text[index] != '{':
        raise ValueError('migration configuration invalid')
    index = _skip_ws(text, index + 1)
    if text[index] == '}':
        return
    while True:
        key, index = _DECODER.raw_decode(text, index)
        index = _skip_ws(text, index)
        if text[index] != ':':
            raise ValueError('migration configuration invalid')
        start = _skip_ws(text, index + 1)
        _, end = _DECODER.raw_decode(text, start)
        yield key, start, end
        index = _skip_ws(text, end)
        if text[index] == ',':
            index = _skip_ws(text, index + 1)
            continue
        if text[index] != '}':
            raise ValueError('migration configuration invalid')
        return


def _array_items(text, index):
    """Yield (item_start, item_end) for the array at ``index``."""
    if text[index] != '[':
        raise ValueError('migration configuration invalid')
    index = _skip_ws(text, index + 1)
    if text[index] == ']':
        return
    while True:
        start = _skip_ws(text, index)
        _, end = _DECODER.raw_decode(text, start)
        yield start, end
        index = _skip_ws(text, end)
        if text[index] == ',':
            index = _skip_ws(text, index + 1)
            continue
        if text[index] != ']':
            raise ValueError('migration configuration invalid')
        return


def _repository_span(text, destination_id):
    """Locate the exact byte span of the repository entry pinning an ID."""
    for key, start, _ in _object_members(text, _skip_ws(text, 0)):
        if key != 'repositories':
            continue
        for item_start, item_end in _array_items(text, start):
            candidate = json.loads(text[item_start:item_end])
            if not isinstance(candidate, dict) or not isinstance(candidate.get('mirrors'), dict):
                continue
            for mirror in candidate['mirrors'].values():
                if isinstance(mirror, dict) and type(mirror.get('id')) is int and mirror['id'] == destination_id:
                    return item_start, item_end
    raise ValueError('migration destination pin is ambiguous')


def _render_element(text, start, value):
    line_start = text.rfind('\n', 0, start) + 1
    indent = text[line_start:start]
    if indent.strip():
        raise ValueError('migration configuration invalid')
    return json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False).replace('\n', '\n' + indent)


def _published_target(entry):
    source = entry['source']
    return f"{source['origin']}/{source['owner']}/{source['name']}"


def _same_binding(mirror, entry):
    legacy = mirror.get('legacy')
    if not isinstance(legacy, dict):
        return False
    recovery = entry['recovery']
    return (legacy.get('source_owner') == recovery['source_owner']
            and legacy.get('source_name') == recovery['source_name']
            and legacy.get('destination_name') == recovery['destination_name']
            and legacy.get('signature') == recovery['signature'])


def is_published(document, entry):
    """True when the configuration bytes already describe the finished state."""
    try:
        profile, mirror, repository = _destination_mirror(document, entry['destination']['id'])
    except ValueError:
        return False
    if profile != entry['destination']['profile']:
        return False
    source = repository.get('source')
    return (isinstance(source, dict) and source.get('url') == _published_target(entry)
            and mirror.get('name') == entry['destination']['name']
            and _same_binding(mirror, entry))


def rewrite_config(raw, entry):
    """Return configuration bytes with only the migrating entry replaced.

    Every other repository entry keeps its exact bytes; the edited entry is
    re-serialised in place. The result is re-parsed and validated before it is
    returned, so a malformed edit can never be published.
    """
    text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
    document = config_document(text)
    profile, _, repository = _destination_mirror(document, entry['destination']['id'])
    if profile != entry['destination']['profile']:
        raise ValueError('migration destination identity mismatch')
    updated = json.loads(json.dumps(repository))
    updated['source']['url'] = _published_target(entry)
    updated['source']['id'] = entry['source']['id']
    target = updated['mirrors'][profile]
    target['name'] = entry['destination']['name']
    target['id'] = entry['destination']['id']
    if target.get('legacy') is None:
        target['legacy'] = {'source_owner': entry['recovery']['source_owner'],
                            'source_name': entry['recovery']['source_name'],
                            'destination_name': entry['recovery']['destination_name'],
                            'signature': entry['recovery']['signature']}
    elif not _same_binding(target, entry):
        raise ValueError('migration original binding changed')
    start, end = _repository_span(text, entry['destination']['id'])
    replacement = _render_element(text, start, updated)
    result = (text[:start] + replacement + text[end:]).encode('utf-8')
    published = config_document(result)
    normalize_config(published)
    if not is_published(published, entry):
        raise ValueError('migration configuration rewrite failed')
    return result


# -- transaction ----------------------------------------------------------
def _repo_id(repo):
    if not isinstance(repo, dict) or type(repo.get('id')) is not int or repo['id'] <= 0:
        raise ValueError('migration destination identity mismatch')
    return repo['id']


def _same_repo(repo, owner, name):
    full_name = repo.get('full_name') if isinstance(repo, dict) else None
    if not isinstance(full_name, str):
        raise ValueError('migration destination identity mismatch')
    return full_name.lower() == (owner + '/' + name).lower()


def _config_state(configuration, entry):
    raw, sha = configuration.read()
    if isinstance(sha, str) and sha == entry['config']['base_sha']:
        return 'pending', raw
    if is_published(config_document(raw), entry):
        return 'published', raw
    return 'changed', raw


def _require_unpublished(configuration, entry):
    state, _ = _config_state(configuration, entry)
    if state == 'changed':
        raise ValueError('migration configuration changed by another writer')


def _pinned(repository, name, entry):
    repo = repository.get(name)
    if repo is None:
        raise ValueError('migration destination missing')
    if _repo_id(repo) != entry['destination']['id']:
        raise ValueError('migration destination pin replaced')
    if not _same_repo(repo, entry['destination']['owner'], name):
        raise ValueError('migration destination identity mismatch')


def _verify_destination(repository, entry):
    old_name = entry['recovery']['destination_name']
    new_name = entry['destination']['name']
    if new_name.lower() == old_name.lower():
        _pinned(repository, new_name, entry)
        return
    existing = repository.get(new_name)
    if existing is not None:
        if _repo_id(existing) != entry['destination']['id'] or not _same_repo(
                existing, entry['destination']['owner'], new_name):
            raise ValueError('migration destination name collision')
        return
    old = repository.get(old_name)
    if old is None:
        raise ValueError('migration destination missing')
    if _repo_id(old) != entry['destination']['id']:
        raise ValueError('migration destination pin replaced')
    if not _same_repo(old, entry['destination']['owner'], old_name):
        raise ValueError('migration destination identity mismatch')


def _rename_destination(repository, entry):
    old_name = entry['recovery']['destination_name']
    new_name = entry['destination']['name']
    if new_name.lower() == old_name.lower():
        _pinned(repository, new_name, entry)
        return
    existing = repository.get(new_name)
    if existing is not None:
        # A previous attempt already renamed; the pinned ID proves it.
        if _repo_id(existing) != entry['destination']['id'] or not _same_repo(
                existing, entry['destination']['owner'], new_name):
            raise ValueError('migration destination identity mismatch')
        return
    old = repository.get(old_name)
    if old is None:
        raise ValueError('migration destination missing')
    if _repo_id(old) != entry['destination']['id']:
        raise ValueError('migration destination pin replaced')
    if not _same_repo(old, entry['destination']['owner'], old_name):
        raise ValueError('migration destination identity mismatch')
    repository.rename(old_name, new_name)
    _pinned(repository, new_name, entry)


def _publish_config(configuration, entry):
    state, raw = _config_state(configuration, entry)
    if state == 'published':
        return
    if state != 'pending':
        raise ValueError('migration configuration changed by another writer')
    replacement = rewrite_config(raw, entry)
    configuration.publish(entry['config']['base_sha'], replacement)
    readback, _ = configuration.read()
    if readback != replacement:
        raise ValueError('migration configuration publication failed')


class NamedConfigPolicy:
    """Provider adapter for the named ``config.json`` document.

    ``MigrationTransaction`` owns the journal protocol; every provider-specific
    check lives behind these hooks so a different configuration format (a flat
    manifest of URL entries) can drive the same body without branching on the
    provider. The default implementation is the GitHub-style named document.
    """

    def error(self, message):
        """Return the exception type a refusal should surface as."""
        return ValueError(message)

    def verify_source(self, entry):
        """Re-check the live source pin; the named configuration needs nothing."""
        return None

    def verify_config(self, configuration, entry):
        _require_unpublished(configuration, entry)

    def verify_destination(self, repository, configuration, entry, phase):
        # Only the pre-rename phase has to prove where the pinned ID lives.
        if phase == 'prepared':
            _verify_destination(repository, entry)

    def rename(self, repository, configuration, entry):
        _rename_destination(repository, entry)

    def publish(self, configuration, entry):
        _publish_config(configuration, entry)
        return None


class MigrationTransaction:
    """Drive one recorded entry through every phase, resumable at any point.

    ``policy`` is a duck-typed provider adapter (see ``NamedConfigPolicy``) that
    supplies the source pin check, the configuration gate, the destination
    identity check, the rename and the publication for one configuration format.
    """

    def __init__(self, store, repository, configuration, secret=None,
                 destination_origin=GITHUB_WEB_ORIGIN, config_path='config.json',
                 policy=None):
        if config_path not in CONFIG_PATHS:
            raise ValueError('migration configuration invalid')
        self.store = store
        self.repository = repository
        self.configuration = configuration
        self.secret = secret
        self.destination_origin = destination_origin
        self.config_path = config_path
        self.policy = policy if policy is not None else NamedConfigPolicy()

    # -- entry points -----------------------------------------------------
    def migrate(self, relocation):
        """Plan and execute the migration for one relocation (idempotent)."""
        raw, sha = self.configuration.read()
        document = config_document(raw)
        if config_reflects(document, relocation):
            return None
        entry = plan_migration(
            relocation, {'document': document, 'path': self.config_path, 'base_sha': sha},
            secret=self.secret, destination_origin=self.destination_origin)
        self.drive(entry, record=True)
        return entry

    def recover(self):
        """Resume every pending entry, or fail closed on an unreviewable state."""
        count = 0
        for _, entry in sorted(self.store.pending().items()):
            self.drive(entry, record=False)
            count += 1
        return count

    # -- phases -----------------------------------------------------------
    def drive(self, entry, record=False):
        """Drive one entry from its recorded phase; return (entry, published bytes)."""
        if entry['config']['path'] != self.config_path:
            raise self.policy.error('migration configuration invalid')
        origin, source_id = entry['source']['origin'], entry['source']['id']
        if record and self.store.get(origin, source_id) is None:
            self.store.record(entry)
        current = self._current(origin, source_id)
        published = None
        while True:
            phase = current['phase']
            if phase == 'prepared':
                self.policy.verify_source(current)
                self.policy.verify_config(self.configuration, current)
                self.policy.verify_destination(self.repository, self.configuration, current, 'prepared')
                self._advance(current, 'synced')
            elif phase == 'synced':
                self.policy.verify_source(current)
                self.policy.verify_config(self.configuration, current)
                self.policy.rename(self.repository, self.configuration, current)
                self._advance(current, 'renamed')
            elif phase == 'renamed':
                self.policy.verify_source(current)
                self.policy.verify_destination(self.repository, self.configuration, current, 'renamed')
                published = self.policy.publish(self.configuration, current)
                self._advance(current, 'configured')
            elif phase == 'configured':
                self.store.complete(origin, source_id)
                return current, published
            else:
                raise self.policy.error('migration state inconsistent')
            current = self._current(origin, source_id)

    def _current(self, origin, source_id):
        current = self.store.get(origin, source_id)
        if current is None:
            raise self.policy.error('migration record missing')
        return current

    def _advance(self, entry, phase):
        if phase not in PHASES:
            raise self.policy.error('migration phase invalid')
        return self.store.advance(entry['source']['origin'], entry['source']['id'],
                                  {**entry, 'phase': phase})
