"""Offline unit tests for deployment validation and file rendering.

These tests only use temporary directories; they never need root, a network,
systemd, apt, firewall access, or a running HomeCam service.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("homecam-deploy.py")
SPEC = importlib.util.spec_from_file_location("homecam_deploy", SCRIPT)
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


SECRET = "a" * 64
VALID = {
    "schema_version": 1,
    "hostname": "cam.example.com",
    "public_ip": "8.8.8.8",
    "private_ip": "10.20.30.40",
    "turn_secret": SECRET,
    "allow_private_relay": False,
}


class DeployValidationTests(unittest.TestCase):
    def test_valid_config_normalizes_values(self) -> None:
        self.assertEqual(deploy.validate_config(VALID), VALID)

    def test_rejects_unsafe_or_malformed_inputs(self) -> None:
        cases = [
            ({**VALID, "hostname": "cam.example.com;touch /tmp/pwned"}, "hostname"),
            ({**VALID, "hostname": "127.0.0.1"}, "DNS hostname"),
            ({**VALID, "public_ip": "10.0.0.1"}, "globally routable"),
            ({**VALID, "private_ip": "169.254.1.1"}, "RFC1918"),
            ({**VALID, "private_ip": "192.0.2.1"}, "RFC1918"),
            ({**VALID, "private_ip": 167772161}, "IPv4 string"),
            ({**VALID, "schema_version": True}, "schema_version"),
            ({**VALID, "turn_secret": "not-a-secret"}, "64-character"),
            ({**VALID, "allow_private_relay": 1}, "true or false"),
            ({**VALID, "extra": "ignored?"}, "exactly"),
        ]
        for config, message in cases:
            with self.subTest(config=config):
                with self.assertRaisesRegex(deploy.DeployError, message):
                    deploy.validate_config(config)

    def test_rejects_duplicate_json_keys_and_group_readable_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(deploy.DeployError, "duplicate JSON key"):
                deploy.read_config(path)

            path.write_text(json.dumps(VALID), encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaisesRegex(deploy.DeployError, "mode 600"):
                deploy.read_config(path)


class DeployRenderingTests(unittest.TestCase):
    def test_default_render_has_private_peer_block_and_no_secret_in_http_config(self) -> None:
        cfg = deploy.validate_config(VALID)
        files = deploy.config_files(cfg)
        turn = files["turnserver.conf"][0]
        caddy = files["homecam-lite.caddy"][0]
        env = files["homecam.env"][0]

        self.assertIn("denied-peer-ip=10.0.0.0-10.255.255.255", turn)
        self.assertIn("denied-peer-ip=172.16.0.0-172.31.255.255", turn)
        self.assertIn("denied-peer-ip=192.168.0.0-192.168.255.255", turn)
        self.assertNotIn("allowed-peer-ip=", turn)
        self.assertIn("min-port=49160", turn)
        self.assertIn("max-port=49179", turn)
        self.assertIn("no-tcp-relay", turn)
        self.assertIn("UDP and TCP client-to-TURN listeners are both enabled", turn)
        self.assertNotIn("no-tcp-listening-port", turn)
        self.assertIn("HOMECAM_MAX_VIEWERS=1", env)
        self.assertEqual(env.count(SECRET), 1)  # local service environment only
        self.assertNotIn(SECRET, caddy)  # Caddy/HTTP-facing site config
        self.assertNotIn("static-auth-secret", caddy)

    def test_private_relay_exception_is_explicit_exact_ip_and_documented(self) -> None:
        cfg = deploy.validate_config({**VALID, "allow_private_relay": True})
        turn = deploy.config_files(cfg)["turnserver.conf"][0]
        self.assertIn("allowed-peer-ip=10.20.30.40", turn)
        self.assertIn("matches an address, not a port range", turn)
        self.assertIn("every peer port on this IP", turn)
        self.assertIn("documented nftables OUTPUT guard", turn)

    def test_render_to_writes_restricted_secret_files(self) -> None:
        cfg = deploy.validate_config(VALID)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rendered"
            deploy.render_to(cfg, out)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            for name in ("homecam.env", "turnserver.conf"):
                self.assertEqual((out / name).stat().st_mode & 0o777, 0o600)
            for name in ("homecam-lite.caddy", "homecam-lite.service", "homecam-turn.service"):
                self.assertEqual((out / name).stat().st_mode & 0o777, 0o644)
            self.assertNotIn(SECRET, (out / "homecam-lite.caddy").read_text(encoding="utf-8"))

    def test_render_refuses_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "rendered"
            target = root / "target"
            target.mkdir()
            out.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(deploy.DeployError, "symlink"):
                deploy.render_to(deploy.validate_config(VALID), out)


if __name__ == "__main__":
    unittest.main()
