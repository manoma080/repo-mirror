"""Exercise the published GitLab configuration examples with real validators."""
import json
import re
import unittest
from pathlib import Path

from repo_mirror.config_named import normalize_config
from repo_mirror.forgejo_sync import parse_manifest, parse_asset_max_bytes
from scheduler.auto_update import valid_manifest


class GitLabReadmeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
        matches = re.findall(r'<!-- gitlab-example: ([\w-]+) -->\s*```json\s*(.*?)\s*```', text, re.S)
        cls.examples = {name: json.loads(body) for name, body in matches}
        assert len(matches) == len(cls.examples) == 4

    def test_github_project(self):
        source = normalize_config(self.examples['github-project'])['repositories'][0]
        self.assertEqual(source['type'], 'gitlab')
        self.assertEqual(source['mirror']['source_id'], 42)
        self.assertEqual(source['mirror']['destination_id'], 99)

    def test_github_group(self):
        config = normalize_config(self.examples['github-group'])
        rule = config['auto_discoveries'][0]
        self.assertEqual(rule['type'], 'gitlab')
        self.assertEqual(rule['owner'], 'team/sub')
        self.assertEqual(rule['id'], 7)

    def test_forgejo_project(self):
        config = self.examples['forgejo-project']
        self.assertTrue(valid_manifest(config))
        entry = parse_manifest(config)[0]
        self.assertEqual((entry['source_type'], entry['source_id'], entry['destination_id']), ('gitlab', 42, 99))

    def test_forgejo_group(self):
        config = self.examples['forgejo-group']
        self.assertTrue(valid_manifest(config))
        self.assertEqual(config['auto_discoveries'][0]['type'], 'gitlab')
        self.assertEqual(parse_asset_max_bytes('2147483647'), 2147483647)
        with self.assertRaises(ValueError):
            parse_asset_max_bytes('2147483648')
