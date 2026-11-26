import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml'


def configure_step():
    text = WORKFLOW.read_text()
    section = text.split('      - name: Update requested repositories in config.json\n', 1)[1]
    return section.split('\n      - name:', 1)[0]


def run_configure(remove):
    step = configure_step()
    script = step.split('        run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines())
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'config.json').write_text('{}')
        (root / 'python').write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['RECORD'], 'w') as f:
    json.dump({'args': sys.argv[1:], 'env': {k: os.environ.get(k) for k in ('MIRROR_APP_ID', 'MIRROR_APP_PRIVATE_KEY', 'DESTINATION_OWNER', 'MIRROR_TIMESTAMP_SECRET')}}, f)
''')
        (root / 'git').write_text('''#!/bin/bash
args=("$@")
if [[ "${args[0]}" == "-C" ]]; then args=("${args[@]:2}"); fi
if [[ "${args[0]}" == rev-parse ]]; then printf '%s\\n' abc123; fi
exit 0
''')
        for name in ('python', 'git'):
            (root / name).chmod(0o755)
        env = dict(os.environ, PATH=f'{root}:' + os.environ['PATH'], RECORD=str(root / 'record.json'),
                   MIRROR_CONFIG_DIR='.',
                   GITHUB_OUTPUT=str(root / 'output'), REQUESTED_SOURCE_URLS='https://github.com/a/x',
                   REQUESTED_DESTINATION='custom', REQUESTED_VISIBILITY='public', REMOVE=str(remove).lower(),
                   REQUESTED_IGNORE_RELEASES='keep',
                   DEFAULT_BRANCH='main', MIRROR_APP_ID='app-id', MIRROR_APP_PRIVATE_KEY='private-key',
                   DESTINATION_OWNER='target-owner', MIRROR_TIMESTAMP_SECRET='timestamp-secret')
        subprocess.run(['bash', '-c', script], cwd=root, env=env, check=True, capture_output=True, text=True)
        revision = WORKFLOW.read_text().split('      - name: Record configuration revision\n', 1)[1]
        revision = revision.split('        run: |\n', 1)[1].split('\n  mirror:', 1)[0]
        subprocess.run(['bash', '-c', textwrap.dedent(revision)], cwd=root, env=env, check=True,
                       capture_output=True, text=True)
        return json.loads((root / 'record.json').read_text()), (root / 'output').read_text()


class WorkflowPinWiringTests(unittest.TestCase):
    def test_add_invokes_pin_with_requested_settings_and_credentials(self):
        record, output = run_configure(False)
        self.assertEqual(record['args'], ['-m', 'repo_mirror.adhoc', '--config', './config.json', '--urls',
                                         'https://github.com/a/x', '--destination', 'custom', '--visibility', 'public',
                                         '--ignore-releases', 'keep', '--auto-pin'])
        self.assertEqual(record['env'], {'MIRROR_APP_ID': 'app-id', 'MIRROR_APP_PRIVATE_KEY': 'private-key',
                                         'DESTINATION_OWNER': 'target-owner', 'MIRROR_TIMESTAMP_SECRET': 'timestamp-secret'})
        self.assertEqual(output, 'sha=abc123\n')
        step = configure_step()
        for binding in ('MIRROR_APP_ID: ${{ secrets.MIRROR_APP_ID }}',
                        'MIRROR_APP_PRIVATE_KEY: ${{ secrets.MIRROR_APP_PRIVATE_KEY }}',
                        'MIRROR_TIMESTAMP_SECRET: ${{ secrets.MIRROR_TIMESTAMP_SECRET }}',
                        'DESTINATION_OWNER: ${{ vars.MIRROR_OWNER }}'):
            self.assertIn(binding, step)

    def test_remove_never_pins(self):
        record, _ = run_configure(True)
        self.assertIn('--remove', record['args'])
        self.assertNotIn('--auto-pin', record['args'])

    def test_actual_workflow_script_runs_real_adhoc_entrypoint(self):
        repo_root = WORKFLOW.parents[2]
        script = configure_step().split('        run: |\n', 1)[1]
        script = '\n'.join(line[10:] for line in script.splitlines())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {'destinations': {'d': {'type': 'github', 'url': 'https://github.com', 'owner': 'bot', 'auth': 'mirrorbot'}}, 'repositories': []}
            (root / 'config.json').write_text(json.dumps(config))
            (root / 'git').write_text('#!/bin/bash\nif [[ "$1" == rev-parse ]]; then echo abc123; fi\nexit 0\n')
            (root / 'git').chmod(0o755)
            (root / 'sitecustomize.py').write_text("""
import json, os
from urllib.error import HTTPError
from repo_mirror import github_api
class Provider:
    def __init__(self, app_id, key, owner, account_type):
        assert (app_id, key, owner, account_type) == ('app-id', 'private-key', 'bot', 'personal')
    def __call__(self):
        return 'installation-token'
class Source:
    def __init__(self, token):
        assert isinstance(token, Provider)
    def request(self, method, url, *args):
        return {'id': 11, 'clone_url': 'https://github.com/a/x.git'}
class API:
    def __init__(self, token=None):
        self.token = token
        self.repo = None
    def create_personal_repository(self, owner, data):
        assert owner == 'bot'
        return self.request('POST', 'https://api.github.com/user/repos', data)
    def request(self, method, url, data=None):
        assert isinstance(self.token, Provider)
        if url.endswith('/actions/permissions'): return {'enabled': False} if method == 'GET' else None
        if url.endswith('/user'): return {'login': 'bot'}
        if method == 'GET':
            if self.repo is None: raise HTTPError(url, 404, 'missing', {}, None)
            return self.repo.copy()
        self.repo = {'id': 22, 'full_name': 'bot/' + data['name'], 'description': data['description']}
        return self.repo.copy()
github_api.AppTokenProvider = Provider
github_api.SourceAPI = Source
github_api.API = API
""")
            env = dict(os.environ, PATH=f'{root}:' + os.environ['PATH'], PYTHONPATH=f'{root}:{repo_root}', MIRROR_CONFIG_DIR='.',
                       GITHUB_OUTPUT=str(root / 'output'), REQUESTED_SOURCE_URLS='https://github.com/a/x',
                       REQUESTED_DESTINATION='custom', REQUESTED_VISIBILITY='public', REMOVE='false',
                       REQUESTED_IGNORE_RELEASES='true',
                       DEFAULT_BRANCH='main', MIRROR_APP_ID='app-id', MIRROR_APP_PRIVATE_KEY='private-key',
                       DESTINATION_OWNER='bot', GH_TOKEN='incorrect-workflow-token')
            result = subprocess.run(['bash', '-c', script], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = json.loads((root / 'config.json').read_text())['repositories'][0]
            self.assertEqual(entry['source']['id'], 11)
            self.assertEqual(entry['mirrors']['d'],
                             {'name': 'custom', 'visibility': 'public', 'id': 22, 'ignore_releases': True})
            self.assertNotIn('private-key', result.stderr + result.stdout)
            self.assertNotIn('installation-token', result.stderr + result.stdout)

    def test_mirror_selection_and_mirrored_revision_remain_wired(self):
        text = WORKFLOW.read_text()
        mirror = text.split('  mirror:\n', 1)[1]
        self.assertIn('needs: configure', mirror.split('    if:', 1)[0])
        self.assertIn('if: ${{ !inputs.remove_from_config }}', mirror.split('    steps:', 1)[0])
        for binding in ('repository: ${{ env.MIRROR_CONFIG_REPOSITORY }}',
                        'ref: ${{ needs.configure.outputs.config-sha }}',
                        'path: ${{ env.MIRROR_CONFIG_DIR }}',
                        'MIRROR_SOURCE_URLS: ${{ inputs.source_urls }}',
                        'MIRROR_DESTINATION_NAME: ${{ inputs.destination_name }}',
                        "MIRROR_VISIBILITY: ${{ inputs.visibility || 'unchanged' }}",
                        'MIRROR_MANAGEMENT_REPOSITORY: ${{ env.MIRROR_CONFIG_REPOSITORY }}',
                        "MIRROR_IGNORE_RELEASES: ${{ (inputs.add_to_config && 'keep') || inputs.ignore_releases || 'keep' }}"):
            self.assertIn(binding, mirror)
        self.assertIn('--config "$MIRROR_CONFIG_DIR/config.json"', mirror)
        self.assertIn('--failures "$MIRROR_CONFIG_DIR/failures.json"', mirror)

    def test_configuration_files_live_outside_this_repository(self):
        text = WORKFLOW.read_text()
        root = WORKFLOW.parents[2]
        for name in ('config.json', 'config-forgejo.json', 'failures.json', 'migrations.json'):
            self.assertFalse((root / name).exists(), name)
        self.assertIn('MIRROR_CONFIG_REPOSITORY: ${{ github.repository_owner }}/repo-mirror-config', text)
        self.assertEqual(text.count('Check out the configuration repository'), 2)

    def test_one_installation_token_serves_the_whole_workflow(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('${{ github.token }}', text)
        self.assertEqual(text.count('uses: ./.github/actions/mirror-token'), 2)
        bindings = [line.strip() for line in text.splitlines()
                    if 'TOKEN' in line and 'steps.auth.outputs.token' in line]
        self.assertEqual(bindings.count('GITHUB_TOKEN_VALUE: ${{ steps.auth.outputs.token }}'), 1)
        self.assertEqual(bindings.count('MANAGEMENT_TOKEN: ${{ steps.auth.outputs.token }}'), 2)
        self.assertEqual(bindings.count('MIRROR_MANAGEMENT_TOKEN: ${{ steps.auth.outputs.token }}'), 1)
        configure = text.split('  configure:\n', 1)[1].split('    steps:', 1)[0]
        self.assertIn('contents: read', configure)
        mirror = text.split('  mirror:\n', 1)[1].split('    steps:', 1)[0]
        self.assertIn('needs: configure', mirror)
        action = (WORKFLOW.parents[1] / 'actions/mirror-token/action.yml').read_text()
        self.assertIn('AppTokenProvider', action)
        self.assertIn('::add-mask::', action)

    def test_tests_run_outside_the_mirror_workflow(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('\n  test:\n', text)
        self.assertNotIn('unittest discover', text)
        tests = (WORKFLOW.parent / 'test.yml').read_text()
        self.assertIn('pull_request:', tests)
        self.assertIn('branches: [main]', tests)
        self.assertIn('  push:', tests)
        self.assertIn('python -m unittest discover -s tests -v', tests)
        for data_file in ('failures.json', 'migrations.json', 'config.json',
                          'config-forgejo.json', '.failure-journal-in-progress'):
            self.assertIn(data_file, tests)


if __name__ == '__main__':
    unittest.main()
