import unittest
from unittest.mock import patch, Mock

from repo_mirror.repository import mirror_repository


class MigrationAttestationTests(unittest.TestCase):
    def test_short_secrets_rejected_for_signing_and_verification(self):
        from repo_mirror.migration_attestation import FIELDS, sign, verify
        claim = {key: (1 if key.endswith('_id') else 'x') for key in FIELDS}
        signature = sign(claim, 'a' * 32)
        for secret in ('', 'x', 'a' * 31, 'é' * 15):
            with self.subTest(secret=secret):
                with self.assertRaises(ValueError):
                    sign(claim, secret)
                with self.assertRaises(ValueError):
                    verify(claim, signature, secret)
        verify(claim, sign(claim, 'é' * 16), 'é' * 16)

    def test_signed_mapping_is_bound_to_all_fields(self):
        from repo_mirror.migration_attestation import github_claim, sign, verify
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'name': 'repo',
                  'mirror': {'name': 'new-target', 'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'old-target'}}}
        claim = github_claim(source, 'repo', 'dest')
        signature = sign(claim, 'fixture-signing-secret-0123456789abcdef')
        verify(claim, signature, 'fixture-signing-secret-0123456789abcdef')
        for key in claim:
            changed = dict(claim)
            changed[key] = 3 if type(claim[key]) is int else claim[key] + 'x'
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify(changed, signature, 'fixture-signing-secret-0123456789abcdef')
        for invalid in ('', signature.upper(), signature + 'x', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                verify(claim, invalid, 'fixture-signing-secret-0123456789abcdef')

    def test_independent_provider_keys_cannot_verify_each_others_claims(self):
        from repo_mirror.migration_attestation import github_claim, forgejo_claim, sign, verify
        github = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new',
                  'mirror': {'name': 'target', 'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'old'}}}
        forgejo = {'url': 'https://git.example/new/repo', 'legacy_url': 'https://git.example/old/repo',
                   'source_id': 1, 'destination_id': 2, 'destination_name': 'target', 'legacy_destination_name': 'old'}
        keys = ['github-independent-key-0123456789abcdef', 'forgejo-independent-key-0123456789abcd']
        for claim in (github_claim(github, 'repo', 'dest'), forgejo_claim(forgejo, 3300, 'dest')):
            for key, other in (keys, keys[::-1]):
                signature = sign(claim, key)
                verify(claim, signature, key)
                with self.assertRaises(ValueError):
                    verify(claim, signature, other)
                changed = dict(claim, destination_owner='attacker')
                with self.assertRaises(ValueError):
                    verify(changed, signature, key)

    def test_weak_key_forgery_rejected_before_repository_access(self):
        import hashlib, hmac, json
        from repo_mirror.migration_attestation import DOMAIN, github_claim
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'name': 'repo',
                  'mirror': {'name': 'target', 'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'old'}}}
        claim = github_claim(source, 'repo', 'dest')
        payload = json.dumps(claim, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        source['mirror']['legacy']['signature'] = hmac.new(b'weak', DOMAIN + payload, hashlib.sha256).hexdigest()
        src, dst = Mock(), Mock()
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'weak'}), self.assertRaises(ValueError):
            mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        src.request.assert_not_called()
        dst.request.assert_not_called()

    def test_unsigned_forgejo_mapping_fails_before_initialization(self):
        from repo_mirror.forgejo_sync import _sync_requested
        args = Mock(port=3300, owner='dest')
        entry = {'url': 'https://github.com/manoma080/mirror-new', 'identity': 'manoma080/mirror-new',
                 'legacy_identity': 'manoma080/mirror-old', 'legacy_url': 'https://github.com/manoma080/mirror-old',
                 'destination_name': 'new', 'legacy_destination_name': 'old', 'source_id': 1, 'destination_id': 2}
        dest = Mock()
        result = _sync_requested(args, [entry['identity']], dest, 'token', 'local', 'fixture-signing-secret-0123456789abcdef', '.', mappings={entry['identity']: entry})
        self.assertEqual(result, 1)
        dest.begin_initialization.assert_not_called()

    def test_forgejo_signature_replay_rejected_for_each_binding(self):
        from repo_mirror.migration_attestation import forgejo_claim, sign, verify
        entry = {'url': 'https://git.example/new/repo', 'legacy_url': 'https://git.example/old/repo',
                 'source_id': 1, 'destination_id': 2, 'destination_name': 'new', 'legacy_destination_name': 'old'}
        claim = forgejo_claim(entry, 3300, 'dest')
        signature = sign(claim, 'fixture-signing-secret-0123456789abcdef')
        verify(claim, signature, 'fixture-signing-secret-0123456789abcdef')
        for key in claim:
            changed = dict(claim)
            changed[key] = 999 if type(claim[key]) is int else claim[key] + 'x'
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify(changed, signature, 'fixture-signing-secret-0123456789abcdef')
        with self.assertRaises(ValueError):
            verify(claim, signature, 'other-signing-secret-0123456789abcdef')

    def test_cli_secret_file_requires_long_secret_and_secure_permissions(self):
        import json, os, subprocess, sys, tempfile
        from pathlib import Path
        from repo_mirror.migration_attestation import FIELDS, sign
        claim = {key: (1 if key.endswith('_id') else 'x') for key in FIELDS}
        with tempfile.TemporaryDirectory() as directory:
            claim_file, secret_file = Path(directory) / 'claim', Path(directory) / 'secret'
            claim_file.write_text(json.dumps(claim))
            secret_file.write_text('short')
            secret_file.chmod(0o600)
            command = [sys.executable, '-m', 'repo_mirror.migration_attestation', '--claim', str(claim_file),
                       '--secret-file', str(secret_file), '--i-verified-history']
            denied = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stdout, '')
            secret = 'fixture-signing-secret-0123456789abcdef'
            secret_file.write_text(secret + '\n')
            approved = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(approved.stdout.strip(), sign(claim, secret))
            secret_file.chmod(0o644)
            denied = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stdout, '')

    def test_operator_command_requires_confirmation_and_emits_signature_only(self):
        import json, os, subprocess, sys, tempfile
        from repo_mirror.migration_attestation import forgejo_claim, sign
        entry = {'url': 'https://git.example/new/repo', 'legacy_url': 'https://git.example/old/repo',
                 'source_id': 1, 'destination_id': 2, 'destination_name': 'new', 'legacy_destination_name': 'old'}
        claim = forgejo_claim(entry, 3300, 'dest')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'claim.json')
            with open(path, 'w') as stream:
                json.dump(claim, stream)
            command = [sys.executable, '-m', 'repo_mirror.migration_attestation', '--claim', path]
            env = dict(os.environ, MIRROR_TIMESTAMP_SECRET='fixture-signing-secret-0123456789abcdef')
            denied = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)
            approved = subprocess.run(command + ['--i-verified-history'], env=env, capture_output=True, text=True)
            self.assertEqual(approved.returncode, 0)
            self.assertEqual(approved.stdout.strip(), sign(claim, 'fixture-signing-secret-0123456789abcdef'))
            self.assertEqual(approved.stderr, '')

    def test_invalid_migration_attestation_is_preserved_by_error_detail(self):
        from repo_mirror.logging_utils import SAFE_ERROR_MESSAGES, error_detail
        message = 'invalid migration attestation'
        self.assertIn(message, SAFE_ERROR_MESSAGES)
        self.assertEqual(error_detail(ValueError(message)), message)
