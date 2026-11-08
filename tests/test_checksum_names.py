"""Checksum file names accepted by the fallback step."""

import unittest

from repo_mirror.repository import CHECKSUM_FILE_NAME


class ChecksumNameTests(unittest.TestCase):
    def test_accepted_names(self):
        for name in ("SHA256SUM", "SHA256SUMS", "SHA256SUM.txt", "SHA256SUMS.txt", "sha256sums.TXT"):
            with self.subTest(name=name):
                self.assertIsNotNone(CHECKSUM_FILE_NAME.fullmatch(name))

    def test_other_names_rejected(self):
        for name in ("checksums.txt", "SHA256SUMS.sig", "xSHA256SUMS", "SHA256SUMS.txt.bak"):
            with self.subTest(name=name):
                self.assertIsNone(CHECKSUM_FILE_NAME.fullmatch(name))


if __name__ == "__main__":
    unittest.main()
