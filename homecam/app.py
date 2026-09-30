"""aiohttp application for HomeCam Lite."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import ipaddress
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path
from typing import Any
import uuid

from aiohttp import WSMsgType, web

from .config import Settings
from .database import Database, utc_iso
from .security import make_turn_credentials, new_secret_token, token_digest, verify_password


COOKIE_NAME = "homecam_session"
MAX_BODY = 64 * 1024
MAX_WS_CONNECTIONS = 32
MAX_PENDING_WS_AUTH = 8
PAIRING_TTL = 10 * 60
TURN_TTL = 60 * 60
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_STATIC_FILES = frozenset({
    "index.html",
    "app.js",
    "styles.css",
    "ice-queue.mjs",
    "sw.js",
    "manifest.webmanifest",
    "icon.svg",
})
_DUMMY_SALT = bytes.fromhex("1c214f708d337d7762cd1d28fc50703b")
_DUMMY_EXPECTED = bytes(32)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


@dataclass(eq=False, slots=True)
class Peer:
    peer_id: str
    role: str
    ws: web.WebSocketResponse
    session_hash: str | None = None
    device_id: str | None = None
    watch_session_id: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, packet: dict[str, Any]) -> bool:
        if self.ws.closed:
            return False
        try:
            async with self.send_lock:
                if self.ws.closed:
                    return False
                await asyncio.wait_for(self.ws.send_json(packet), timeout=3)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.ws.closed:
                try:
                    await asyncio.wait_for(self.ws.close(code=1011, message=b"send failed"), timeout=1)
                except Exception:
                    pass
            return False


@dataclass(slots=True)
class WatchSession:
    session_id: str
    device_id: str
    viewer: Peer
    camera: Peer


class RateLimiter:
    """Small bounded sliding-window limiter for public credential endpoints."""

    def __init__(self, limit: int = 5, window_seconds: int = 60, max_keys: int = 4096):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self.events: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        if now is None:
            now = time.monotonic()
        entries = self.events.get(key)
        if entries is None:
            if len(self.events) >= self.max_keys:
                # Dropping one old bucket bounds memory if an attacker rotates IPs.
                oldest = min(self.events, key=lambda item: self.events[item][-1] if self.events[item] else 0)
                self.events.pop(oldest, None)
            entries = self.events.setdefault(key, deque())
        cutoff = now - self.window
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= self.limit:
            return False
        entries.append(now)
        return True


class ConnectionBudget:
    """Bound open and unauthenticated WebSockets before allocating peer state."""

    def __init__(self) -> None:
        self.total = 0
        self.pending = 0

    def reserve(self) -> bool:
        if self.total >= MAX_WS_CONNECTIONS or self.pending >= MAX_PENDING_WS_AUTH:
            return False
        self.total += 1
        self.pending += 1
        return True

    def authenticated(self) -> None:
        self.pending = max(0, self.pending - 1)

    def release(self, *, still_pending: bool) -> None:
        self.total = max(0, self.total - 1)
        if still_pending:
            self.pending = max(0, self.pending - 1)


class Hub:
    def __init__(self, app: web.Application):
        self.app = app
        self.peers: dict[str, Peer] = {}
        self.cameras: dict[str, Peer] = {}
        self.watch_sessions: dict[str, WatchSession] = {}

    def device_view(self) -> list[dict[str, Any]]:
        devices = []
        for row in self.app["db"].list_devices():
            device_id = str(row["id"])
            devices.append({
                "id": device_id,
                "name": str(row["name"]),
                "online": device_id in self.cameras and not self.cameras[device_id].ws.closed,
                "created_at": str(row["created_at"]),
                "viewer_count": sum(1 for item in self.watch_sessions.values() if item.device_id == device_id),
            })
        return devices

    async def broadcast_devices(self) -> None:
        packet = {"type": "devices", "devices": self.device_view()}
        for peer in tuple(self.peers.values()):
            if peer.role == "viewer":
                await peer.send(packet)

    async def remove_peer(self, peer: Peer) -> None:
        self.peers.pop(peer.peer_id, None)
        if peer.role == "camera" and peer.device_id:
            if self.cameras.get(peer.device_id) is peer:
                self.cameras.pop(peer.device_id, None)
            for session in tuple(self.watch_sessions.values()):
                if session.camera is peer:
                    await self._end_watch(session, camera_left=True)
            await self.broadcast_devices()
        elif peer.role == "viewer" and peer.watch_session_id:
            session = self.watch_sessions.get(peer.watch_session_id)
            if session is not None:
                await self._end_watch(session, viewer_left=True)

    async def _end_watch(
        self,
        session: WatchSession,
        *,
        viewer_left: bool = False,
        camera_left: bool = False,
    ) -> None:
        current = self.watch_sessions.pop(session.session_id, None)
        if current is None:
            return
        if current.viewer.watch_session_id == current.session_id:
            current.viewer.watch_session_id = None
        if current.camera.watch_session_id == current.session_id:
            current.camera.watch_session_id = None
        if viewer_left and not current.camera.ws.closed:
            await current.camera.send({
                "type": "viewer-left",
                "peer_id": current.viewer.peer_id,
                "session_id": current.session_id,
            })
        if camera_left and not current.viewer.ws.closed:
            await current.viewer.send({"type": "peer-left", "session_id": current.session_id})
        if current.viewer in self.peers.values():
            await self.broadcast_devices()

    async def close_session(self, session_hash: str) -> None:
        for peer in tuple(self.peers.values()):
            if peer.role == "viewer" and peer.session_hash == session_hash:
                if peer.watch_session_id:
                    await peer.send({"type": "peer-left", "session_id": peer.watch_session_id})
                await peer.send({"type": "error", "error": "session logged out"})
                await peer.ws.close(code=1008, message=b"session logged out")
                await self.remove_peer(peer)

    async def close_device(self, device_id: str) -> None:
        peer = self.cameras.get(device_id)
        if peer is not None:
            await peer.send({"type": "error", "error": "device revoked"})
            await peer.ws.close(code=1008, message=b"device revoked")
            await self.remove_peer(peer)
        else:
            for session in tuple(self.watch_sessions.values()):
                if session.device_id == device_id:
                    await self._end_watch(session, camera_left=True)
            await self.broadcast_devices()

    async def expire_sessions(self, expired_hashes: list[str]) -> None:
        for session_hash in expired_hashes:
            await self.close_session(session_hash)

    async def close_all(self) -> None:
        for peer in tuple(self.peers.values()):
            await peer.ws.close(code=1001, message=b"server shutting down")
        self.peers.clear()
        self.cameras.clear()
        self.watch_sessions.clear()


@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin != request.app["settings"].origin:
            return json_error(403, "origin rejected")
    try:
        response = await handler(request)
    except ApiError as exc:
        response = json_error(exc.status, exc.message)
    except web.HTTPRequestEntityTooLarge:
        response = json_error(413, "request body too large") if request.path.startswith("/api/") else web.Response(status=413, text="Request body too large")
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            response = json_error(exc.status, "request rejected")
        else:
            response = exc
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = _content_security_policy(request.app["settings"])
    return response


def _content_security_policy(settings: Settings) -> str:
    scheme = "wss" if settings.origin.startswith("https://") else "ws"
    host_origin = settings.origin.split("://", 1)[1]
    ws_origin = f"{scheme}://{host_origin}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; "
        f"connect-src 'self' {ws_origin}; worker-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def read_json(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise ApiError(415, "content type must be application/json")
    try:
        value = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError, web.HTTPBadRequest):
        raise ApiError(400, "invalid JSON")
    if not isinstance(value, dict):
        raise ApiError(400, "JSON object required")
    return value


def require_admin(request: web.Request) -> str:
    raw_token = request.cookies.get(COOKIE_NAME)
    if not raw_token:
        raise ApiError(401, "authentication required")
    session_hash = request.app["db"].session_expiry(raw_token, time.time())
    if session_hash is None:
        raise ApiError(401, "authentication required")
    return token_digest(raw_token)


def get_rate_key(request: web.Request, endpoint: str) -> str:
    remote = request.remote or "unknown"
    # Trust a forwarded address only when the direct peer is loopback (the default
    # deployment binds behind a local Caddy reverse proxy).
    try:
        direct_ip = ipaddress.ip_address(remote)
    except ValueError:
        direct_ip = None
    if direct_ip is not None and direct_ip.is_loopback:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        try:
            remote = str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return f"{endpoint}:{remote}"


def check_rate_limit(request: web.Request, endpoint: str) -> None:
    limiter: RateLimiter = request.app["rate_limiter"]
    if not limiter.allow(get_rate_key(request, endpoint)):
        raise ApiError(429, "too many requests")


def validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "name must be a string")
    name = value.strip()
    if not name or len(name) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ApiError(400, "name must contain 1 to 64 visible characters")
    return name


async def healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def login(request: web.Request) -> web.Response:
    check_rate_limit(request, "login")
    payload = await read_json(request)
    password = payload.get("password")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ApiError(401, "invalid credentials")
    row = request.app["db"].admin_password_row()
    if row is None:
        async with request.app["scrypt_sem"]:
            await asyncio.to_thread(verify_password, password, _DUMMY_SALT, _DUMMY_EXPECTED)
        raise ApiError(401, "invalid credentials")
    async with request.app["scrypt_sem"]:
        valid_password = await asyncio.to_thread(verify_password, password, bytes(row["salt"]), bytes(row["password_hash"]))
    if not valid_password:
        raise ApiError(401, "invalid credentials")
    settings: Settings = request.app["settings"]
    raw_token = new_secret_token()
    request.app["db"].create_session(raw_token, time.time(), settings.session_ttl)
    response = web.json_response({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        path="/",
        max_age=settings.session_ttl,
        httponly=True,
        secure=not settings.dev,
        samesite="Strict",
    )
    return response


async def session_status(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE_NAME)
    authenticated = bool(token and request.app["db"].session_expiry(token, time.time()) is not None)
    return web.json_response({"authenticated": authenticated})


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session_hash = request.app["db"].delete_session(token)
        await request.app["hub"].close_session(session_hash)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE_NAME, path="/", secure=not request.app["settings"].dev, httponly=True, samesite="Strict")
    return response


async def list_devices(request: web.Request) -> web.Response:
    require_admin(request)
    return web.json_response({"devices": request.app["hub"].device_view()})


async def create_pairing(request: web.Request) -> web.Response:
    require_admin(request)
    payload = await read_json(request)
    name = validate_name(payload.get("name"))
    now = time.time()
    expires = now + PAIRING_TTL
    db: Database = request.app["db"]
    for _ in range(8):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        try:
            db.add_pairing(code, name, expires)
            return web.json_response({"code": code, "expires_at": utc_iso(expires)})
        except Exception as exc:
            # Retry only on the vanishingly rare primary-key collision.
            if "UNIQUE constraint failed" not in str(exc):
                raise
    raise ApiError(503, "could not create pairing code")


async def redeem_pairing(request: web.Request) -> web.Response:
    check_rate_limit(request, "pairing-redeem")
    payload = await read_json(request)
    code = payload.get("code")
    if not isinstance(code, str) or re.fullmatch(r"[A-Z0-9]{8}", code) is None:
        raise ApiError(400, "invalid pairing code")
    device_id = str(uuid.uuid4())
    device_token = new_secret_token()
    name = request.app["db"].redeem_pairing(code, device_id, device_token, time.time())
    if name is None:
        raise ApiError(400, "pairing code is invalid or expired")
    return web.json_response({"device_id": device_id, "device_token": device_token, "name": name})


async def delete_device(request: web.Request) -> web.Response:
    require_admin(request)
    device_id = request.match_info["device_id"]
    try:
        uuid.UUID(device_id)
    except (ValueError, AttributeError):
        raise ApiError(404, "device not found")
    if not request.app["db"].delete_device(device_id):
        raise ApiError(404, "device not found")
    await request.app["hub"].close_device(device_id)
    return web.json_response({"ok": True})


async def get_ice(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    identity = "admin"
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]
        if not raw_token or " " in raw_token:
            raise ApiError(401, "authentication required")
        device = request.app["db"].device_for_token(raw_token)
        if device is None:
            raise ApiError(401, "authentication required")
        identity = str(device["id"])
    else:
        require_admin(request)
    expiry = int(time.time()) + TURN_TTL
    username, credential, expiry = make_turn_credentials(settings.turn_secret, identity, ttl=TURN_TTL, now=int(time.time()))
    ice_servers = []
    if settings.turn_urls:
        ice_servers.append({"urls": list(settings.turn_urls), "username": username, "credential": credential})
    return web.json_response({
        "ice_servers": ice_servers,
        "ice_transport_policy": "all" if settings.dev else "relay",
        "expires_at": utc_iso(expiry),
    })


def _static_allowlist(root: Path | None) -> tuple[Path | None, set[str]]:
    if root is None:
        return None, set()
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return None, set()
    if not resolved_root.is_dir():
        return None, set()
    allowed: set[str] = set()
    for relative in _STATIC_FILES:
        path = resolved_root / relative
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if path.is_file() and resolved.is_relative_to(resolved_root):
            allowed.add(relative)
    return resolved_root, allowed


async def static_file(request: web.Request) -> web.StreamResponse:
    root: Path | None = request.app["static_root"]
    if root is None:
        raise web.HTTPNotFound()
    requested = "index.html" if request.path == "/" else request.match_info.get("asset", "")
    if not requested or requested not in request.app["static_allowlist"]:
        raise web.HTTPNotFound()
    candidate = (root / requested).resolve(strict=False)
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise web.HTTPNotFound()
    content_type, encoding = mimetypes.guess_type(candidate.name)
    response = web.FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    if content_type:
        response.content_type = content_type
    if encoding:
        response.headers["Content-Encoding"] = encoding
    return response


def _auth_error(ws: web.WebSocketResponse, message: str) -> None:
    # Helper exists for symmetry with packet errors; actual send must be awaited.
    return None


async def websocket(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    if request.headers.get("Origin") != settings.origin:
        return json_error(403, "origin rejected")
    if request.query_string:
        return json_error(400, "credentials and query parameters are not accepted on WebSocket")
    budget: ConnectionBudget = request.app["ws_budget"]
    if not budget.reserve():
        return json_error(503, "WebSocket connection limit reached")
    auth_pending = True
    ws = web.WebSocketResponse(max_msg_size=MAX_BODY, heartbeat=30, autoping=True)
    try:
        await ws.prepare(request)
        try:
            first = await ws.receive(timeout=5)
        except asyncio.TimeoutError:
            await ws.send_json({"type": "error", "error": "auth required"})
            await ws.close(code=1008, message=b"auth timeout")
            return ws
        if first.type != WSMsgType.TEXT:
            await ws.send_json({"type": "error", "error": "auth required"})
            await ws.close(code=1008, message=b"auth required")
            return ws
        try:
            auth = json.loads(first.data)
        except (json.JSONDecodeError, TypeError):
            auth = None
        if not isinstance(auth, dict) or auth.get("type") != "auth":
            await ws.send_json({"type": "error", "error": "auth required"})
            await ws.close(code=1008, message=b"auth required")
            return ws

        peer_id = secrets.token_urlsafe(12)
        peer: Peer
        if auth.get("role") == "viewer":
            raw_session = request.cookies.get(COOKIE_NAME)
            if not raw_session or request.app["db"].session_expiry(raw_session, time.time()) is None:
                await ws.send_json({"type": "error", "error": "authentication required"})
                await ws.close(code=1008, message=b"authentication required")
                return ws
            peer = Peer(peer_id, "viewer", ws, session_hash=token_digest(raw_session))
        elif auth.get("role") == "camera":
            device_id = auth.get("device_id")
            token = auth.get("device_token")
            if not isinstance(device_id, str) or not isinstance(token, str):
                await ws.send_json({"type": "error", "error": "authentication required"})
                await ws.close(code=1008, message=b"authentication required")
                return ws
            try:
                uuid.UUID(device_id)
            except ValueError:
                await ws.send_json({"type": "error", "error": "authentication required"})
                await ws.close(code=1008, message=b"authentication required")
                return ws
            row = request.app["db"].device_by_id(device_id)
            supplied_hash = token_digest(token)
            if row is None or not secrets.compare_digest(str(row["token_hash"]), supplied_hash):
                await ws.send_json({"type": "error", "error": "authentication required"})
                await ws.close(code=1008, message=b"authentication required")
                return ws
            peer = Peer(peer_id, "camera", ws, device_id=device_id)
        else:
            await ws.send_json({"type": "error", "error": "authentication required"})
            await ws.close(code=1008, message=b"authentication required")
            return ws

        budget.authenticated()
        auth_pending = False
        hub: Hub = request.app["hub"]
        if peer.role == "camera":
            previous = hub.cameras.get(peer.device_id or "")
            if previous is not None and previous is not peer:
                await previous.send({"type": "error", "error": "camera connection replaced"})
                await previous.ws.close(code=1008, message=b"camera connection replaced")
                await hub.remove_peer(previous)
            hub.cameras[peer.device_id or ""] = peer
        hub.peers[peer.peer_id] = peer
        await peer.send({"type": "ready", "peer_id": peer.peer_id, "role": peer.role})
        if peer.role == "viewer":
            await peer.send({"type": "devices", "devices": hub.device_view()})
        else:
            await hub.broadcast_devices()

        async for message in ws:
            if message.type == WSMsgType.TEXT:
                if peer.role == "viewer" and (
                    peer.session_hash is None
                    or not request.app["db"].session_hash_is_valid(peer.session_hash, time.time())
                ):
                    await peer.send({"type": "error", "error": "authentication expired"})
                    await ws.close(code=1008, message=b"authentication expired")
                    break
                try:
                    packet = json.loads(message.data)
                except (json.JSONDecodeError, TypeError):
                    await peer.send({"type": "error", "error": "invalid JSON message"})
                    continue
                if not isinstance(packet, dict):
                    await peer.send({"type": "error", "error": "message must be a JSON object"})
                    continue
                await handle_ws_message(request, peer, packet)
            elif message.type == WSMsgType.BINARY:
                await peer.send({"type": "error", "error": "binary messages are not supported"})
                await ws.close(code=1003, message=b"binary messages are not supported")
                break
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR}:
                break
    except (asyncio.TimeoutError, ConnectionError, RuntimeError):
        pass
    finally:
        peer_value = locals().get("peer")
        if isinstance(peer_value, Peer):
            await request.app["hub"].remove_peer(peer_value)
        budget.release(still_pending=auth_pending)
    return ws


async def handle_ws_message(request: web.Request, sender: Peer, packet: dict[str, Any]) -> None:
    hub: Hub = request.app["hub"]
    message_type = packet.get("type")
    if sender.role == "viewer" and message_type == "watch":
        device_id = packet.get("device_id")
        if not isinstance(device_id, str) or not request.app["db"].has_device(device_id):
            await sender.send({"type": "error", "error": "device not found"})
            return
        camera = hub.cameras.get(device_id)
        if camera is None or camera.ws.closed:
            await sender.send({"type": "error", "error": "camera is offline"})
            return
        existing = hub.watch_sessions.get(sender.watch_session_id or "")
        if existing is not None:
            await hub._end_watch(existing, viewer_left=True)
        count = sum(1 for item in hub.watch_sessions.values() if item.device_id == device_id)
        if count >= request.app["settings"].max_viewers:
            await sender.send({"type": "error", "error": "viewer limit reached"})
            return
        session_id = str(uuid.uuid4())
        session = WatchSession(session_id, device_id, sender, camera)
        hub.watch_sessions[session_id] = session
        sender.watch_session_id = session_id
        await sender.send({"type": "watching", "device_id": device_id, "peer_id": camera.peer_id, "session_id": session_id})
        await camera.send({"type": "viewer-joined", "peer_id": sender.peer_id, "session_id": session_id})
        await hub.broadcast_devices()
        return
    if sender.role == "viewer" and message_type == "unwatch":
        session = hub.watch_sessions.get(sender.watch_session_id or "")
        if session is not None:
            await hub._end_watch(session, viewer_left=True)
        return
    if message_type == "signal":
        await _relay_signal(hub, sender, packet)
        return
    await sender.send({"type": "error", "error": "message type or role is not allowed"})


async def _relay_signal(hub: Hub, sender: Peer, packet: dict[str, Any]) -> None:
    session_id = packet.get("session_id")
    target_id = packet.get("target")
    data = packet.get("data")
    session = hub.watch_sessions.get(session_id) if isinstance(session_id, str) else None
    if session is None or not isinstance(target_id, str) or not isinstance(data, dict):
        await sender.send({"type": "error", "error": "signal session not found"})
        return
    if sender is session.viewer:
        target = session.camera
    elif sender is session.camera:
        target = session.viewer
    else:
        await sender.send({"type": "error", "error": "signal membership rejected"})
        return
    if target_id != target.peer_id or target.ws.closed:
        await sender.send({"type": "error", "error": "signal target rejected"})
        return
    signal_type = data.get("type")
    valid = False
    if isinstance(signal_type, str) and signal_type in {"offer", "answer"}:
        sdp = data.get("sdp")
        allowed_sender = (signal_type == "offer" and sender.role == "camera") or (signal_type == "answer" and sender.role == "viewer")
        valid = allowed_sender and isinstance(sdp, dict) and sdp.get("type") == signal_type and isinstance(sdp.get("sdp"), str) and len(sdp["sdp"]) <= MAX_BODY - 1024
    elif signal_type == "ice":
        candidate = data.get("candidate")
        valid = (
            isinstance(candidate, dict)
            and set(candidate).issubset({"candidate", "sdpMid", "sdpMLineIndex", "usernameFragment"})
            and isinstance(candidate.get("candidate"), str)
            and (candidate.get("sdpMid") is None or isinstance(candidate.get("sdpMid"), str))
            and (candidate.get("sdpMLineIndex") is None or isinstance(candidate.get("sdpMLineIndex"), int))
            and (candidate.get("usernameFragment") is None or isinstance(candidate.get("usernameFragment"), str))
            and len(json.dumps(candidate, separators=(",", ":"))) <= MAX_BODY - 1024
        )
    if not valid:
        await sender.send({"type": "error", "error": "invalid signal data"})
        return
    await target.send({"type": "signal", "source": sender.peer_id, "session_id": session.session_id, "data": data})


async def _maintenance(app: web.Application) -> None:
    while True:
        await asyncio.sleep(15)
        expired = app["db"].cleanup(time.time())
        await app["hub"].expire_sessions(expired)
        invalid = [
            peer.session_hash
            for peer in tuple(app["hub"].peers.values())
            if peer.role == "viewer"
            and peer.session_hash is not None
            and not app["db"].session_hash_is_valid(peer.session_hash, time.time())
        ]
        await app["hub"].expire_sessions([item for item in invalid if item is not None])


async def app_lifecycle(app: web.Application):
    db = Database(app["settings"].db_path)
    db.open()
    if not db.has_admin():
        db.close()
        raise RuntimeError("administrator is not initialized; run python -m homecam init-admin")
    app["db"] = db
    app["hub"] = Hub(app)
    task = asyncio.create_task(_maintenance(app), name="homecam-maintenance")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app["hub"].close_all()
        db.close()


async def on_prepare(_request: web.Request, response: web.StreamResponse) -> None:
    response.headers.pop("Server", None)


def create_app(settings: Settings, static_dir: Path | None = None) -> web.Application:
    if static_dir is None:
        static_dir = settings.static_dir or Path(__file__).resolve().parent.parent / "web"
    root, allowlist = _static_allowlist(Path(static_dir) if static_dir else None)
    app = web.Application(middlewares=[security_middleware], client_max_size=MAX_BODY)
    app["settings"] = settings
    app["static_root"] = root
    app["static_allowlist"] = allowlist
    app["rate_limiter"] = RateLimiter()
    app["scrypt_sem"] = asyncio.Semaphore(2)
    app["ws_budget"] = ConnectionBudget()
    app.cleanup_ctx.append(app_lifecycle)
    app.on_response_prepare.append(on_prepare)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/session", session_status)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/devices", list_devices)
    app.router.add_post("/api/pairings", create_pairing)
    app.router.add_post("/api/pairings/redeem", redeem_pairing)
    app.router.add_delete("/api/devices/{device_id}", delete_device)
    app.router.add_get("/api/ice", get_ice)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/", static_file)
    app.router.add_get("/{asset:.*}", static_file)
    return app
