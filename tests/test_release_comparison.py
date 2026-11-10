import unittest

from repo_mirror import repository


def release(tag, name=None, **fields):
    return {"tag_name": tag, "name": name or tag, **fields}


class ReleaseComparisonTests(unittest.TestCase):
    def test_bound_older_release_is_retried(self):
        source = [release("new"), release("old")]
        destination = [
            release("new"),
            release("old", body='notes\n[repo-mirror:pending:v3]{"phase":"BOUND"}'),
        ]
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)

    def test_unnamed_or_published_release_does_not_match_untagged_draft(self):
        source = [release("new"), {"tag_name": "old", "name": None}]
        destination = [release("new"), {"tag_name": "untagged-1", "name": None, "draft": True}]
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)
        source[1]["name"] = destination[1]["name"] = "same"
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)

    def test_comparison_indexes_destination_once(self):
        class CountingList(list):
            def __init__(self, values):
                super().__init__(values)
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        source = [release(str(i)) for i in range(50)]
        destination = CountingList(source)
        repository.select_existing_releases(source, destination, set())
        self.assertLessEqual(destination.iterations, 2)

    def test_missing_releases_selected_but_existing_older_skipped(self):
        source = [release(str(i)) for i in range(6)]
        destination = [release("0"), release("2"), release("4")]
        selected = repository.select_existing_releases(source, destination, set())
        self.assertEqual([r["tag_name"] for r in selected], ["0", "1", "3", "5"])

    def test_pending_and_failed_old_releases_are_selected(self):
        source = [release(str(i)) for i in range(5)]
        destination = [release(str(i)) for i in range(5)]
        destination[2]["body"] = 'notes\n[repo-mirror:pending:v3]{"phase":"WAITING_SOURCE"}'
        destination[3]["name"] = "[repo-mirror:pending:v2]marker"
        selected = repository.select_existing_releases(source, destination, {"4"})
        self.assertEqual([r["tag_name"] for r in selected], ["0", "2", "3", "4"])

    def test_unsigned_untagged_draft_requires_reconciliation(self):
        source = [release("new"), release("draft", "Draft", draft=True)]
        destination = [release("new"), release("untagged-123", "Draft", draft=True)]
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)

    def test_legacy_untagged_marker_requires_reconciliation(self):
        source = [release("new"), release("draft", "Draft", draft=True)]
        destination = [release("new"), release("untagged-1", "[repo-mirror:pending:v2]marker", draft=True)]
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)

    def test_ambiguous_untagged_drafts_are_rejected(self):
        source = [release("a", "same", draft=True), release("b", "same", draft=True)]
        destination = [release("untagged-1", "same", draft=True)]
        self.assertEqual(repository.select_existing_releases(source, destination, set()), source)
        self.assertEqual(repository.select_existing_releases(source[:1], destination * 2, set()), source[:1])


if __name__ == "__main__":
    unittest.main()
