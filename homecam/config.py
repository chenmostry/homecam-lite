"""Environment-backed configuration for HomeCam Lite."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when required configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    host: str
    port: int
    origin: str
    dev: bool
    turn_urls: tuple[str, ...]
    turn_secret: bytes
    max_viewers: int = 1
    session_ttl: int = 12 * 60 * 60
    static_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.max_viewers != 1:
            raise ConfigError("HOMECAM_MAX_VIEWERS must be 1 in protocol v0.1")

    @classmethod
    def from_env(cls) -> "Settings":
        dev_value = os.environ.get("HOMECAM_DEV", "0")
        if dev_value not in {"0", "1"}:
            raise ConfigError("HOMECAM_DEV must be 0 or 1")
        dev = dev_value == "1"

        origin = os.environ.get("HOMECAM_ORIGIN")
        if not origin and dev:
            origin = "http://127.0.0.1:8088"
        if not origin:
            raise ConfigError("HOMECAM_ORIGIN is required")
        _validate_origin(origin, dev=dev)

        secret_value = os.environ.get("HOMECAM_TURN_SECRET")
        if secret_value is None:
            if not dev:
                raise ConfigError("HOMECAM_TURN_SECRET is required in production")
            turn_secret = secrets.token_bytes(32)
        else:
            turn_secret = secret_value.encode("utf-8")
            if len(turn_secret) < 32:
                raise ConfigError("HOMECAM_TURN_SECRET must be at least 32 bytes")

        raw_urls = os.environ.get("HOMECAM_TURN_URLS", "")
        turn_urls = tuple(part.strip() for part in raw_urls.split(",") if part.strip())
        if not dev and not turn_urls:
            raise ConfigError("HOMECAM_TURN_URLS is required in production")
        for url in turn_urls:
            if not (url.startswith("turn:") or url.startswith("turns:")):
                raise ConfigError("HOMECAM_TURN_URLS may contain only TURN URLs")

        host = os.environ.get("HOMECAM_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("HOMECAM_PORT", "8088"))
        except ValueError as exc:
            raise ConfigError("HOMECAM_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("HOMECAM_PORT must be between 1 and 65535")

        try:
            max_viewers = int(os.environ.get("HOMECAM_MAX_VIEWERS", "1"))
        except ValueError as exc:
            raise ConfigError("HOMECAM_MAX_VIEWERS must be an integer") from exc
        if max_viewers != 1:
            raise ConfigError("HOMECAM_MAX_VIEWERS must be 1 in protocol v0.1")

        return cls(
            db_path=Path(os.environ.get("HOMECAM_DB", "/var/lib/homecam-lite/homecam.db")),
            host=host,
            port=port,
            origin=origin,
            dev=dev,
            turn_urls=turn_urls,
            turn_secret=turn_secret,
            max_viewers=max_viewers,
        )


def _validate_origin(origin: str, *, dev: bool) -> None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("HOMECAM_ORIGIN is invalid") from exc
    if (
        parsed.scheme not in ({"http", "https"} if dev else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin.endswith("/")
    ):
        raise ConfigError("HOMECAM_ORIGIN must be an exact origin without a path")
    if dev and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError("Development origin must use localhost or a loopback address")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("HOMECAM_ORIGIN port is invalid")
