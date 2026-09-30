from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from homecam.config import ConfigError, Settings


class ConfigTests(unittest.TestCase):
    def test_production_requires_turn_secret_and_secure_origin(self) -> None:
        env = {
            "HOMECAM_DEV": "0",
            "HOMECAM_ORIGIN": "https://cam.example.test",
            "HOMECAM_TURN_URLS": "turn:turn.example.test:3478?transport=udp",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "TURN_SECRET"):
                Settings.from_env()
        env["HOMECAM_TURN_SECRET"] = "short"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "32 bytes"):
                Settings.from_env()
        env["HOMECAM_TURN_SECRET"] = "0123456789abcdef0123456789abcdef"
        env["HOMECAM_ORIGIN"] = "http://cam.example.test"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "exact origin"):
                Settings.from_env()

    def test_dev_must_be_explicit_and_localhost_only(self) -> None:
        with patch.dict(os.environ, {"HOMECAM_DEV": "1"}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.dev)
        self.assertEqual(settings.origin, "http://127.0.0.1:8088")
        with patch.dict(os.environ, {"HOMECAM_DEV": "1", "HOMECAM_ORIGIN": "http://example.test"}, clear=True):
            with self.assertRaisesRegex(ConfigError, "localhost"):
                Settings.from_env()

    def test_protocol_v01_allows_exactly_one_viewer(self) -> None:
        with patch.dict(os.environ, {"HOMECAM_DEV": "1", "HOMECAM_MAX_VIEWERS": "1"}, clear=True):
            self.assertEqual(Settings.from_env().max_viewers, 1)
        for invalid in ("0", "2", "many"):
            with self.subTest(value=invalid), patch.dict(
                os.environ, {"HOMECAM_DEV": "1", "HOMECAM_MAX_VIEWERS": invalid}, clear=True
            ):
                with self.assertRaisesRegex(ConfigError, "HOMECAM_MAX_VIEWERS"):
                    Settings.from_env()
        with self.assertRaisesRegex(ConfigError, "HOMECAM_MAX_VIEWERS"):
            Settings(
                db_path=Path(":memory:"),
                host="127.0.0.1",
                port=8088,
                origin="http://127.0.0.1:8088",
                dev=True,
                turn_urls=(),
                turn_secret=b"x" * 32,
                max_viewers=2,
            )


if __name__ == "__main__":
    unittest.main()
