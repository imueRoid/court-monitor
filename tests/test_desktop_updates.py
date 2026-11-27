from io import BytesIO
import json
import unittest
from unittest.mock import patch

from desktop_updates import INSTALLER, MANIFEST, RELEASES, check_update, release_page


class DesktopUpdateTests(unittest.TestCase):
    def test_only_a_newer_windows_installer_triggers_an_update(self):
        manifest = json.dumps({"version": "1.0.10", "installer": INSTALLER}).encode()
        for current, expected in [("1.0.9", "1.0.10"), ("1.0.10", None), ("1.0.11", None)]:
            with self.subTest(current=current), patch("desktop_updates.urlopen", return_value=BytesIO(manifest)) as request:
                self.assertEqual(check_update(current), expected)
                request.assert_called_once_with(f"{RELEASES}/latest/download/{MANIFEST}", timeout=8)

    def test_rejects_non_windows_invalid_and_oversized_metadata(self):
        for manifest in [
            b'{"version":"1.0.50"}',
            json.dumps({"version": "../latest", "installer": INSTALLER}).encode(),
            b" " * 4100,
        ]:
            with patch("desktop_updates.urlopen", return_value=BytesIO(manifest)), self.assertRaises(ValueError):
                check_update("1.0.0")

    def test_release_page_uses_the_known_github_repository(self):
        self.assertEqual(release_page("1.0.50"), f"{RELEASES}/tag/app-v1.0.50")
