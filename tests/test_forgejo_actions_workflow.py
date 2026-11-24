"""Check workflow parity and exercise the actual workflow step shell."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / 'scheduler/forgejo-actions.yml'


class ForgejoActionsWorkflowTests(unittest.TestCase):
    def test_manual_input_names_and_defaults_match(self):
        def inputs(path):
            text = path.read_text().split('    inputs:\n', 1)[1]
            text = re.split(r'\n[^ \n]', text, maxsplit=1)[0]
            return dict(re.findall(r'^      ([a-z_]+):\n(?:^        .*\n)*?^        default: (.*)$', text, re.M))
        expected = inputs(ROOT / '.github/workflows/mirror.yml')
        self.assertEqual(len(expected), 6)
        self.assertEqual(inputs(WORKFLOW), expected)

    def test_shell_only_forwards_manual_options_as_data(self):
        body = WORKFLOW.read_text().split('        run: |\n', 1)[1]
        shell = '\n'.join(line[10:] for line in body.splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / 'run-mirror'
            launcher.write_text('#!/bin/sh\nprintf "%s\\n%s\\n" "$1" "${FORGEJO_MANUAL_INPUTS:-}"\n')
            launcher.chmod(0o700)
            env = {**os.environ, 'PATH': tmp + ':' + os.environ['PATH'], 'EVENT': 'workflow_dispatch',
                   'SOURCE_URLS': '$(exit 11); https://github.com/owner/name', 'ADD': 'true', 'REMOVE': 'false',
                   'DESTINATION': '', 'VISIBILITY': 'unchanged', 'IGNORE_RELEASES': 'false'}
            result = subprocess.check_output(['sh', '-c', shell], env=env, text=True).splitlines()
            self.assertEqual(result[0], 'mirror')
            self.assertEqual(json.loads(result[1]), dict(source_urls=env['SOURCE_URLS'], add_to_config=True,
                             remove_from_config=False, destination_name='', visibility='unchanged', ignore_releases='false'))
            result = subprocess.check_output(['sh', '-c', shell], env={**env, 'EVENT': 'schedule'}, text=True)
            self.assertEqual(result, 'mirror\n\n')
