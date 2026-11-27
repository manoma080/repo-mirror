import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mirror_test_support import patch_everywhere

import mirror


class FilterIntegrationTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_selected_only(self):
        config = {
            "graphql": False,
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/one"}, "mirrors": {"main": {}}},
                {"source": {"url": "https://github.com/alice/two"}, "mirrors": {"main": {}}},
                {"source": {"url": "https://github.com/bob/three"}, "mirrors": {"main": {}}},
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f)
            f.flush()
            env = {
                "GH_TOKEN": "token",
                "DESTINATION_OWNER": "dest",
                "MIRROR_SOURCE_URLS": "https://github.com/alice/two",
            }
            with (
                patch.dict(os.environ, env),
                patch_everywhere("get_existing", return_value=None),
                patch_everywhere("mirror_repository") as run,
            ):
                mirror.main(["--config", f.name])
                self.assertEqual([call.args[1] for call in run.call_args_list], ["two"])
