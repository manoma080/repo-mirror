"""Direct runner must not reuse credentials as recovery signing material."""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import tempfile
import unittest
from pathlib import Path

from repo_mirror import forgejo_sync

URL = 'https://github.com/mirror-owner/mirror-fixture-org-d-fixture-repo'
SECRET = 'sensitive-synthetic-credential-value'


def check_alias(tmp_path, credential, alias):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps([URL]))
    secret = tmp_path / 'recovery'
    secret.write_text(SECRET)
    secret.chmod(0o600)
    other = tmp_path / 'other-credential'
    other.write_text('different-synthetic-credential')
    other.chmod(0o600)
    credential_path = secret
    if alias == 'hardlink':
        credential_path = tmp_path / 'hardlink'
        os.link(secret, credential_path)
    elif alias == 'symlink':
        credential_path = tmp_path / 'symlink'
        credential_path.symlink_to(secret)
    token = credential_path if credential == 'token' else other
    key = credential_path if credential == 'app_key' else other
    args = ['--config', str(config), '--source', URL, '--token-file', str(token),
            '--timestamp-secret-file', str(secret), '--journal-dir', str(tmp_path),
            '--owner', 'local', '--app-id', '123', '--private-key-file', str(key),
            '--source-app-owner', 'mirror-owner']
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(forgejo_sync, 'AppTokenProvider'), \
         patch.object(forgejo_sync, 'LocalForgejoAPI') as api, \
         patch.object(forgejo_sync, 'sync_refs', return_value=0) as sync, \
         patch.object(forgejo_sync, 'snapshot_refs', return_value=(None, {}, {})), \
         patch.object(forgejo_sync, 'ForgejoDestinationAdapter'), \
         patch.object(forgejo_sync, 'SourceAPI'), \
         patch.object(forgejo_sync, 'mirror_releases'), \
         redirect_stdout(stdout), redirect_stderr(stderr):
        result = forgejo_sync.main(args)
    output = stdout.getvalue() + stderr.getvalue()
    assert SECRET not in output
    assert 'different-synthetic-credential' not in output
    assert result == 2, output
    api.assert_not_called()
    sync.assert_not_called()


class SecretSeparationTests(unittest.TestCase):
    def test_direct_cli_rejects_credential_aliases(self):
        for credential in ('token', 'app_key'):
            for alias in ('same', 'hardlink', 'symlink'):
                with self.subTest(credential=credential, alias=alias), tempfile.TemporaryDirectory() as directory:
                    check_alias(Path(directory), credential, alias)
