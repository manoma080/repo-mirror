"""Bind the manual Forgejo planner to the authoritative GitHub config."""
import json
from urllib.parse import quote

from repo_mirror.config_json import unique_pairs


def dispatch_failure(exc):
    # Never print user-provided URLs or transport exceptions to a job log.
    from repo_mirror.forgejo_dispatch import DispatchError
    if isinstance(exc, DispatchError):
        return str(exc)
    return 'validation, destination preparation or configuration publication failed'


def publication(before, after, cas):
    if after == before:
        return None
    if cas.read() != before:
        raise ValueError('configuration changed')
    cas.replace(before, after)
    if cas.read() != after:
        raise ValueError('configuration publication readback mismatch')
    return after


def prepare_target(destination, source, mirror, parse_source, *, create):
    from repo_mirror.forgejo_git import APIError, _source_marker

    if 'legacy' in mirror:
        # A matching provenance string alone does not verify a relocation attestation.
        raise ValueError('existing migration mapping requires normal scheduled recovery')
    identity = parse_source(source['url'], source.get('type'))
    name, pin = mirror['name'], mirror.get('id')
    visibility = mirror.get('visibility', 'private')
    if create:
        if pin is None:
            return destination.ensure_repository(name, identity, visibility=visibility, require_new=True)['id']
    try:
        repo = destination.api.request('GET', f'/api/v1/repos/{destination.owner}/{name}')
    except APIError as exc:
        if exc.status == 404 and pin is None and not create:
            return None
        raise
    destination._verify(repo, name, _source_marker(identity), actions=True, private=visibility == 'private')
    if pin is not None and repo['id'] != pin:
        raise ValueError('destination repository ID changed')
    return repo['id']


def prepare(args, secret, manifest_bytes, inputs):
    from repo_mirror.forgejo_dispatch import plan_dispatch
    from repo_mirror import forgejo_migration
    from repo_mirror.adhoc import parse_repo_url
    from repo_mirror.forgejo_git import ForgejoDestination
    from repo_mirror.forgejo_sync import parse_source, verify_source_pin
    from repo_mirror.gitlab_onboarding_forgejo import AnonymousGroupAPI
    from repo_mirror.sources import verified_source_identity

    manifest = json.loads(manifest_bytes, object_pairs_hook=unique_pairs)
    transaction = None

    def authority():
        nonlocal transaction
        if transaction is None:
            transaction = forgejo_migration.build_migration(args, secret)
            if transaction is None or transaction.cas.read() != manifest_bytes:
                raise ValueError('configuration changed or authority not configured')
        return transaction

    def source_lookup(url, kind):
        kind, origin, owner, name = parse_repo_url(url, source_type=kind, allow_forgejo_port=True)
        source = {'type': kind, 'base_url': origin, 'owner': owner}
        if kind == 'gitlab':
            metadata = AnonymousGroupAPI({origin}).request('GET', origin + '/api/v4/projects/' + quote(owner + '/' + name, safe=''))
            if (not isinstance(metadata, dict) or metadata.get('visibility') != 'public'
                    or metadata.get('path_with_namespace') != owner + '/' + name):
                raise ValueError('GitLab source identity mismatch')
            metadata = {**metadata, 'clone_url': metadata.get('http_url_to_repo')}
        else:
            metadata = authority().source(origin, owner, name)
        repository_id = metadata.get('id')
        if type(repository_id) is not int or repository_id <= 0:
            raise ValueError('invalid source pin')
        if kind == 'gitlab':
            verified_source_identity(source, name, repository_id, metadata)
        else:
            verify_source_pin(parse_source(url, kind), metadata, repository_id)
        return repository_id

    def prepare_destination(source, mirror, *, create):
        tx = authority()
        destination = ForgejoDestination(tx.destination.api, args.owner, organization=args.organization)
        return prepare_target(destination, source, mirror, parse_source, create=create)

    candidate, runtime, selected = plan_dispatch(manifest, inputs, owner=args.owner, port=args.port,
                                                source_lookup=source_lookup, prepare_destination=prepare_destination)
    if candidate is None or candidate == manifest:
        if transaction is not None and transaction.cas.read() != manifest_bytes:
            raise ValueError('configuration changed')
        return None, runtime, selected
    proposed = (json.dumps(candidate, ensure_ascii=True, indent=2) + '\n').encode('utf-8')
    return publication(manifest_bytes, proposed, authority().cas), runtime, selected
