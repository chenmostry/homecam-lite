from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from homecam.__main__ import _init_admin
from homecam.app import COOKIE_NAME, create_app
from homecam.config import Settings
from homecam.database import Database
from homecam.security import new_secret_token, token_digest


ORIGIN = "http://127.0.0.1:8088"
ADMIN_PASSWORD = "correct-horse-battery-staple"


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "homecam.db"
        db = Database(self.db_path)
        db.open()
        db.set_admin_password(ADMIN_PASSWORD)
        db.close()
        self.settings = Settings(
            db_path=self.db_path,
            host="127.0.0.1",
            port=8088,
            origin=ORIGIN,
            dev=True,
            turn_urls=("turn:turn.example.test:3478?transport=udp",),
            turn_secret=b"test-turn-secret-0123456789abcdef",
        )
        self.server = TestServer(create_app(self.settings))
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    def origin_headers(self, **extra: str) -> dict[str, str]:
        return {"Origin": ORIGIN, **extra}

    async def login(self, client: TestClient | None = None) -> str:
        client = client or self.client
        response = await client.post(
            "/api/login",
            json={"password": ADMIN_PASSWORD},
            headers=self.origin_headers(),
        )
        self.assertEqual(response.status, 200)
        return response.cookies[COOKIE_NAME].value

    async def create_device(self, client: TestClient | None = None) -> tuple[str, str]:
        client = client or self.client
        await self.login(client)
        response = await client.post(
            "/api/pairings",
            json={"name": "Front room"},
            headers=self.origin_headers(),
        )
        self.assertEqual(response.status, 200)
        code = (await response.json())["code"]
        redeemed = await client.post(
            "/api/pairings/redeem",
            json={"code": code},
            headers=self.origin_headers(),
        )
        self.assertEqual(redeemed.status, 200)
        info = await redeemed.json()
        return info["device_id"], info["device_token"]

    async def ws_json(self, ws, timeout: float = 1.0) -> dict:
        message = await ws.receive(timeout=timeout)
        self.assertEqual(message.type, WSMsgType.TEXT, message)
        return json.loads(message.data)

    async def test_health_auth_bypass_origin_and_body_limit(self) -> None:
        health = await self.client.get("/healthz")
        self.assertEqual(await health.json(), {"ok": True})
        self.assertNotIn("version", await health.json())

        no_origin = await self.client.post("/api/login", json={"password": ADMIN_PASSWORD})
        self.assertEqual(no_origin.status, 403)
        self.assertEqual(set(await no_origin.json()), {"error"})

        bypass = await self.client.get("/api/devices")
        self.assertEqual(bypass.status, 401)
        self.assertEqual(set(await bypass.json()), {"error"})

        logged_in = await self.client.post(
            "/api/login", json={"password": ADMIN_PASSWORD}, headers=self.origin_headers()
        )
        self.assertEqual(logged_in.status, 200)
        cookie = logged_in.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertFalse(cookie["secure"])
        allowed = await self.client.get("/api/devices")
        self.assertEqual(allowed.status, 200)

        large = await self.client.post(
            "/api/login",
            data=b"{" + b" " * (64 * 1024) + b"}",
            headers=self.origin_headers(**{"Content-Type": "application/json"}),
        )
        self.assertEqual(large.status, 413)
        self.assertEqual(set(await large.json()), {"error"})

    async def test_static_route_excludes_nonproduction_files(self) -> None:
        exposed = await self.client.get("/tests/frontend-smoke.mjs")
        self.assertEqual(exposed.status, 404)

    async def test_pairing_is_one_use(self) -> None:
        await self.login()
        created = await self.client.post(
            "/api/pairings", json={"name": "Kitchen"}, headers=self.origin_headers()
        )
        self.assertEqual(created.status, 200)
        pairing = await created.json()
        self.assertRegex(pairing["code"], r"^[A-Z0-9]{8}$")
        self.assertTrue(pairing["expires_at"].endswith("Z"))

        first = await self.client.post(
            "/api/pairings/redeem", json={"code": pairing["code"]}, headers=self.origin_headers()
        )
        self.assertEqual(first.status, 200)
        first_payload = await first.json()
        self.assertEqual(first_payload["name"], "Kitchen")
        self.assertEqual(len(first_payload["device_token"]), 43)
        second = await self.client.post(
            "/api/pairings/redeem", json={"code": pairing["code"]}, headers=self.origin_headers()
        )
        self.assertEqual(second.status, 400)

        db = Database(self.db_path)
        db.open()
        device = db.device_by_id(first_payload["device_id"])
        self.assertEqual(device["token_hash"], token_digest(first_payload["device_token"]))
        db.close()

    async def test_ice_rest_credentials_use_hmac_and_device_bearer(self) -> None:
        await self.login()
        response = await self.client.get("/api/ice")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["ice_transport_policy"], "all")
        self.assertTrue(data["expires_at"].endswith("Z"))
        server = data["ice_servers"][0]
        username = server["username"]
        expiry_text, identity = username.split(":", 1)
        self.assertEqual(identity, "admin")
        expected = base64.b64encode(
            hmac.new(self.settings.turn_secret, username.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(server["credential"], expected)
        self.assertLessEqual(int(expiry_text), int(time.time()) + 3600)
        self.assertGreater(int(expiry_text), int(time.time()))

        device_id, device_token = await self.create_device()
        device_response = await self.client.get(
            "/api/ice", headers={"Authorization": f"Bearer {device_token}"}
        )
        self.assertEqual(device_response.status, 200)
        device_server = (await device_response.json())["ice_servers"][0]
        self.assertEqual(device_server["username"].split(":", 1)[1], device_id)
        rejected = await self.client.get("/api/ice", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(rejected.status, 401)

    async def test_cross_session_signaling_is_rejected(self) -> None:
        device_id, device_token = await self.create_device()
        viewer1_cookie = await self.login()
        viewer2_cookie = await self.login()

        common_ws_headers = {"Origin": ORIGIN}
        camera = await self.client.ws_connect("/ws", headers=common_ws_headers)
        await camera.send_json({"type": "auth", "role": "camera", "device_id": device_id, "device_token": device_token})
        camera_ready = await self.ws_json(camera)
        self.assertEqual(camera_ready["role"], "camera")

        viewer1 = await self.client.ws_connect(
            "/ws", headers={**common_ws_headers, "Cookie": f"{COOKIE_NAME}={viewer1_cookie}"}
        )
        await viewer1.send_json({"type": "auth", "role": "viewer"})
        viewer1_ready = await self.ws_json(viewer1)
        self.assertEqual(viewer1_ready["role"], "viewer")
        self.assertEqual((await self.ws_json(viewer1))["type"], "devices")

        viewer2 = await self.client.ws_connect(
            "/ws", headers={**common_ws_headers, "Cookie": f"{COOKIE_NAME}={viewer2_cookie}"}
        )
        await viewer2.send_json({"type": "auth", "role": "viewer"})
        viewer2_ready = await self.ws_json(viewer2)
        self.assertEqual(viewer2_ready["role"], "viewer")
        self.assertEqual((await self.ws_json(viewer2))["type"], "devices")

        await viewer1.send_json({"type": "watch", "device_id": device_id})
        watching = await self.ws_json(viewer1)
        self.assertEqual(watching["type"], "watching")
        joined = await self.ws_json(camera)
        self.assertEqual(joined["type"], "viewer-joined")
        # Presence broadcasts follow the watch events.
        self.assertEqual((await self.ws_json(viewer1))["type"], "devices")
        self.assertEqual((await self.ws_json(viewer2))["type"], "devices")

        await viewer1.send_json({
            "type": "signal",
            "target": camera_ready["peer_id"],
            "session_id": watching["session_id"],
            "data": [],
        })
        malformed = await self.ws_json(viewer1)
        self.assertEqual(malformed["type"], "error")
        with self.assertRaises(TimeoutError):
            await camera.receive(timeout=0.1)

        await viewer2.send_json({
            "type": "signal",
            "target": camera_ready["peer_id"],
            "session_id": watching["session_id"],
            "data": {"type": "answer", "sdp": {"type": "answer", "sdp": "v=0"}},
        })
        rejected = await self.ws_json(viewer2)
        self.assertEqual(rejected["type"], "error")
        with self.assertRaises(TimeoutError):
            await camera.receive(timeout=0.1)

        await viewer1.close()
        await viewer2.close()
        await camera.close()

    async def test_cli_password_reset_invalidates_viewer(self) -> None:
        old_cookie = await self.login()
        viewer = await self.client.ws_connect(
            "/ws", headers={"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={old_cookie}"}
        )
        await viewer.send_json({"type": "auth", "role": "viewer"})
        self.assertEqual((await self.ws_json(viewer))["role"], "viewer")
        self.assertEqual((await self.ws_json(viewer))["type"], "devices")

        new_password = "new-password-for-reset-check"
        with patch(
            "homecam.__main__.getpass.getpass",
            side_effect=[new_password, new_password],
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(_init_admin(self.db_path, reset=True), 0)

        rejected = await self.client.get(
            "/api/devices", headers={"Cookie": f"{COOKIE_NAME}={old_cookie}"}
        )
        self.assertEqual(rejected.status, 401)

        await viewer.send_json({"type": "watch", "device_id": "anything"})
        self.assertEqual(
            await self.ws_json(viewer),
            {"type": "error", "error": "authentication expired"},
        )
        closed = await viewer.receive(timeout=1)
        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(viewer.close_code, 1008)

        old_login = await self.client.post(
            "/api/login",
            json={"password": ADMIN_PASSWORD},
            headers=self.origin_headers(),
        )
        self.assertEqual(old_login.status, 401)
        new_login = await self.client.post(
            "/api/login",
            json={"password": new_password},
            headers=self.origin_headers(),
        )
        self.assertEqual(new_login.status, 200)

    async def test_revoking_device_closes_camera_and_viewer(self) -> None:
        admin_cookie = await self.login()
        pair = await self.client.post(
            "/api/pairings", json={"name": "Entry"}, headers=self.origin_headers()
        )
        code = (await pair.json())["code"]
        redeemed = await self.client.post(
            "/api/pairings/redeem", json={"code": code}, headers=self.origin_headers()
        )
        info = await redeemed.json()

        camera = await self.client.ws_connect("/ws", headers={"Origin": ORIGIN})
        await camera.send_json({"type": "auth", "role": "camera", "device_id": info["device_id"], "device_token": info["device_token"]})
        await self.ws_json(camera)
        viewer = await self.client.ws_connect(
            "/ws", headers={"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={admin_cookie}"}
        )
        await viewer.send_json({"type": "auth", "role": "viewer"})
        await self.ws_json(viewer)
        await self.ws_json(viewer)
        await viewer.send_json({"type": "watch", "device_id": info["device_id"]})
        await self.ws_json(viewer)
        await self.ws_json(camera)
        await self.ws_json(viewer)  # devices presence broadcast

        delete_task = asyncio.create_task(self.client.delete(
            f"/api/devices/{info['device_id']}", headers=self.origin_headers()
        ))
        self.assertEqual((await self.ws_json(camera))["error"], "device revoked")
        camera_close = await camera.receive(timeout=2)
        self.assertEqual(camera_close.type, WSMsgType.CLOSE)
        self.assertEqual((await self.ws_json(viewer))["type"], "peer-left")
        deleted = await delete_task
        self.assertEqual(deleted.status, 200)
        self.assertEqual((await deleted.json()), {"ok": True})
        self.assertEqual(camera.close_code, 1008)
        devices = await self.client.get("/api/devices")
        self.assertEqual((await devices.json())["devices"], [])

    async def test_expired_session_and_logout_close_viewer_socket(self) -> None:
        expired_token = new_secret_token()
        db = Database(self.db_path)
        db.open()
        db.create_session(expired_token, time.time() - 120, 1)
        db.close()
        expired = await self.client.get("/api/devices", headers={"Cookie": f"{COOKIE_NAME}={expired_token}"})
        self.assertEqual(expired.status, 401)
        ws_expired = await self.client.ws_connect(
            "/ws", headers={"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={expired_token}"}
        )
        await ws_expired.send_json({"type": "auth", "role": "viewer"})
        self.assertEqual((await self.ws_json(ws_expired))["type"], "error")
        closed = await ws_expired.receive(timeout=1)
        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(ws_expired.close_code, 1008)

        session_cookie = await self.login()
        viewer = await self.client.ws_connect(
            "/ws", headers={"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={session_cookie}"}
        )
        await viewer.send_json({"type": "auth", "role": "viewer"})
        await self.ws_json(viewer)
        await self.ws_json(viewer)
        logout_task = asyncio.create_task(self.client.post(
            "/api/logout", json={}, headers=self.origin_headers()
        ))
        self.assertEqual((await self.ws_json(viewer))["error"], "session logged out")
        logout_close = await viewer.receive(timeout=2)
        self.assertEqual(logout_close.type, WSMsgType.CLOSE)
        logged_out = await logout_task
        self.assertEqual(logged_out.status, 200)
        self.assertEqual((await logged_out.json()), {"ok": True})
        self.assertEqual(viewer.close_code, 1008)

    async def test_websocket_requires_origin_and_auth_first(self) -> None:
        # An invalid origin is rejected before websocket upgrade, as an HTTP JSON error.
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect("/ws", headers={"Origin": "http://attacker.test"})
        self.assertEqual(raised.exception.status, 403)

        socket = await self.client.ws_connect("/ws", headers={"Origin": ORIGIN})
        message = await self.ws_json(socket, timeout=6)
        self.assertEqual(message, {"type": "error", "error": "auth required"})
        closing = await socket.receive(timeout=1)
        self.assertEqual(closing.type, WSMsgType.CLOSE)
        self.assertEqual(socket.close_code, 1008)


if __name__ == "__main__":
    unittest.main()
