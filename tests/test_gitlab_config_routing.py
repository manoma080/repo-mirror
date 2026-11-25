import unittest

from repo_mirror.adhoc import parse_repo_url


class GitLabConfigRoutingTests(unittest.TestCase):
    def test_named_manual_selection_preserves_self_hosted_binding(self):
        from repo_mirror.adhoc import add_to_config
        config = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://code.example/group/sub/project', 'id': 12},
             'mirrors': {'d': {'id': 34}}}]}
        self.assertFalse(add_to_config(config, 'https://code.example/group/sub/project'))
        self.assertEqual(len(config['repositories']), 1)

    def test_gitlab_nested_url_and_explicit_self_hosted_type(self):
        assert parse_repo_url('https://gitlab.com/group/sub/project') == ('gitlab', 'https://gitlab.com', 'group/sub', 'project')
        assert parse_repo_url('https://code.example/group/sub/project', source_type='gitlab') == ('gitlab', 'https://code.example', 'group/sub', 'project')
        assert parse_repo_url('https://code.example/group/project')[0] == 'forgejo'
        with self.assertRaises(ValueError):
            parse_repo_url('https://github.com/group/sub/project', source_type='gitlab')


    def test_named_config_preserves_self_hosted_type_and_pin(self):
        from repo_mirror.config_named import normalize_config
        config = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://code.example/group/sub/project', 'id': 12}, 'mirrors': {'d': {'name': 'project', 'id': 34}}}]}
        source = normalize_config(config)['repositories'][0]
        assert source['type'] == 'gitlab'
        assert source['owner'] == 'group/sub'
        assert source['mirror'] == {'name': 'project', 'source_id': 12, 'destination_id': 34}


    def test_gitlab_destination_name_slug(self):
        from repo_mirror.sources import destination_name
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group/sub', 'name': 'project'}
        assert destination_name(source, 'project') == 'mirror-group-sub-project'


    def test_forgejo_manifest_retains_explicit_gitlab_source_type(self):
        from repo_mirror.forgejo_sync import parse_manifest
        config = {'destinations': {'d': {}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://code.example/group/sub/project', 'id': 12}, 'mirrors': {'d': {'name': 'project', 'id': 34}}}]}
        entry = parse_manifest(config)[0]
        assert entry['identity'] == 'https://code.example/group/sub/project'
        assert entry['source_type'] == 'gitlab'
        assert entry['source_id'] == 12 and entry['destination_id'] == 34


    def test_gitlab_pin_checks_id_namespace_and_origin(self):
        from repo_mirror.sources import verified_source_identity
        source = {'type': 'gitlab', 'base_url': 'https://code.example', 'owner': 'group/sub'}
        metadata = {'id': 12, 'web_url': 'https://code.example/group/sub/project', 'path_with_namespace': 'group/sub/project'}
        assert verified_source_identity(source, 'project', 12, metadata) == metadata['web_url']
        with self.assertRaises(ValueError):
            verified_source_identity(source, 'project', 13, metadata)


    def test_explicit_github_cannot_reclassify_other_origins(self):
        with self.assertRaises(ValueError):
            parse_repo_url('https://code.example/group/project', source_type='github')
