#!/usr/bin/env python3
"""Preflight, render, and install the HomeCam Lite Ubuntu deployment."""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_NAME = "homecam-lite"
APP_USER = "homecam"
TURN_USER = "homecam-turn"
APP_DIR = Path("/opt/homecam-lite")
CONFIG_DIR = Path("/etc/homecam-lite")
STATE_DIR = Path("/var/lib/homecam-lite")
SYSTEMD_DIR = Path("/etc/systemd/system")
CADDY_SNIPPET = Path("/etc/caddy/homecam-lite.caddy")
APP_UNIT = SYSTEMD_DIR / "homecam-lite.service"
TURN_UNIT = SYSTEMD_DIR / "homecam-turn.service"
TURN_SECRET_RE = re.compile(r"\A[0-9a-f]{64}\Z")
HOST_LABEL_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MARKER = "# Managed by homecam-deploy.py. Local edits will be replaced on apply."


class DeployError(Exception):
    pass


def fail(message: str) -> None:
    raise DeployError(message)


def validate_hostname(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        fail("hostname must be a fully qualified DNS hostname, such as cam.example.com")
    if value != value.lower() or value.endswith(".") or not value.isascii():
        fail("hostname must be lowercase ASCII without a trailing dot (use punycode for IDNs)")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        fail("production HTTPS requires a DNS hostname; an IP address is not accepted here")
    labels = value.split(".")
    if len(labels) < 2 or any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        fail("hostname must contain valid DNS labels, for example cam.example.com")
    return value


def validate_ips(public_value: Any, private_value: Any) -> tuple[str, str]:
    if not isinstance(public_value, str) or not isinstance(private_value, str):
        fail("public_ip and private_ip must be IPv4 string literals")
    try:
        public = ipaddress.IPv4Address(public_value)
        private = ipaddress.IPv4Address(private_value)
    except (ipaddress.AddressValueError, TypeError):
        fail("public_ip and private_ip must be IPv4 literals (not hostnames or IPv6)")
    if not public.is_global:
        fail("public_ip must be a globally routable IPv4 address")
    rfc1918 = any(private in network for network in (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ))
    if not rfc1918:
        fail("private_ip must be the server's RFC1918 private IPv4 address")
    if public == private:
        fail("public_ip and private_ip must be different")
    return str(public), str(private)


def validate_config(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        fail("deployment config must be a JSON object")
    required = {"schema_version", "hostname", "public_ip", "private_ip", "turn_secret", "allow_private_relay"}
    if set(obj) != required:
        fail("deployment config must contain exactly: " + ", ".join(sorted(required)))
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        fail("unsupported deployment config schema_version")
    hostname = validate_hostname(obj["hostname"])
    public_ip, private_ip = validate_ips(obj["public_ip"], obj["private_ip"])
    secret = obj["turn_secret"]
    if not isinstance(secret, str) or not TURN_SECRET_RE.fullmatch(secret):
        fail("turn_secret must be a 64-character lowercase hexadecimal secret generated locally")
    if not isinstance(obj["allow_private_relay"], bool):
        fail("allow_private_relay must be true or false")
    return {
        "schema_version": 1,
        "hostname": hostname,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "turn_secret": secret,
        "allow_private_relay": obj["allow_private_relay"],
    }


def json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key in config: {key}")
        result[key] = value
    return result


def read_config(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        fail(f"refusing symlink config path: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        fail(f"cannot read config {path}: {exc}")
    if not stat.S_ISREG(info.st_mode):
        fail(f"config must be a regular file: {path}")
    if info.st_mode & 0o077:
        fail(f"config contains the TURN secret; set mode 600 before use: chmod 600 {path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=json_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid config JSON {path}: {exc}")
    return validate_config(obj)


def write_private_config(path: Path, cfg: dict[str, Any], *, replace: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        fail(f"refusing symlink config path: {path}")
    if path.exists() and not replace:
        fail(f"config already exists; refusing to replace it: {path}")
    payload = json.dumps(
        {
            "schema_version": 1,
            "hostname": cfg["hostname"],
            "public_ip": cfg["public_ip"],
            "private_ip": cfg["private_ip"],
            "turn_secret": cfg["turn_secret"],
            "allow_private_relay": cfg["allow_private_relay"],
        },
        indent=2,
    ) + "\n"
    atomic_write(path, payload, 0o600)


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail(f"refusing to write through symlink: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


def config_files(cfg: dict[str, Any]) -> dict[str, tuple[str, int, str | None]]:
    host = cfg["hostname"]
    public_ip = cfg["public_ip"]
    private_ip = cfg["private_ip"]
    turn_secret = cfg["turn_secret"]
    env = f"""{MARKER}
HOMECAM_DB=/var/lib/homecam-lite/homecam.db
HOMECAM_HOST=127.0.0.1
HOMECAM_PORT=8088
HOMECAM_ORIGIN=https://{host}
HOMECAM_DEV=0
HOMECAM_TURN_URLS=turn:{public_ip}:3478?transport=udp,turn:{public_ip}:3478?transport=tcp
HOMECAM_TURN_SECRET={turn_secret}
HOMECAM_MAX_VIEWERS=1
"""
    # A private-IP allow rule is intentionally opt-in: coturn applies it to all
    # peer ports on that IP, not just the relay range. Operators must install
    # the documented service-UID OUTPUT guard before enabling this exception.
    denied_ranges = [
        "0.0.0.0-0.255.255.255",
        "10.0.0.0-10.255.255.255",
        "100.64.0.0-100.127.255.255",
        "127.0.0.0-127.255.255.255",
        "169.254.0.0-169.254.255.255",
        "172.16.0.0-172.31.255.255",
        "192.0.0.0-192.0.0.255",
        "192.0.2.0-192.0.2.255",
        "192.88.99.0-192.88.99.255",
        "192.168.0.0-192.168.255.255",
        "198.18.0.0-198.19.255.255",
        "198.51.100.0-198.51.100.255",
        "203.0.113.0-203.0.113.255",
        "224.0.0.0-255.255.255.255",
    ]
    turn_lines = [
        MARKER,
        "# IPv4 only; UDP and TCP client-to-TURN listeners are both enabled.",
        "# UDP relay endpoints are enabled; TCP peer relay endpoints are disabled.",
        f"listening-ip={private_ip}",
        f"relay-ip={private_ip}",
        "listening-port=3478",
        "min-port=49160",
        "max-port=49179",
        f"external-ip={public_ip}/{private_ip}",
        f"realm={host}",
        "fingerprint",
        "no-stun",
        "no-tls",
        "no-dtls",
        "no-tcp-relay",
        "no-cli",
        "no-multicast-peers",
        "no-software-attribute",
        "use-auth-secret",
        f"static-auth-secret={turn_secret}",
        "stale-nonce=600",
        "max-allocate-lifetime=3600",
        "user-quota=4",
        "total-quota=8",
        "max-bps=300000",
        "bps-capacity=600000",
        "log-file=stdout",
    ]
    if cfg["allow_private_relay"]:
        turn_lines.extend(
            [
                "# OPT-IN: allowed-peer-ip matches an address, not a port range; it reaches every peer port on this IP.",
                "# Install the documented nftables OUTPUT guard for the homecam-turn UID before starting coturn.",
                f"allowed-peer-ip={private_ip}",
            ]
        )
    else:
        turn_lines.append("# Private peer ranges remain denied; enable a private self-relay exception only after a real relay test proves it is needed.")
    turn_lines.extend(f"denied-peer-ip={network}" for network in denied_ranges)
    turn_conf = "\n".join(turn_lines) + "\n"
    caddy = f"""{MARKER}
{host} {{
    reverse_proxy 127.0.0.1:8088
}}
"""
    app_unit = f"""{MARKER}
[Unit]
Description=HomeCam Lite application
After=network.target

[Service]
Type=simple
User={APP_USER}
Group={APP_USER}
WorkingDirectory=/opt/homecam-lite/current
EnvironmentFile=/etc/homecam-lite/homecam.env
ExecStart=/opt/homecam-lite/current/.venv/bin/python -m homecam
Restart=on-failure
RestartSec=3s
UMask=0077
StateDirectory=homecam-lite
StateDirectoryMode=0700
MemoryMax=192M
TasksMax=64
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET

[Install]
WantedBy=multi-user.target
"""
    turn_unit = f"""{MARKER}
[Unit]
Description=HomeCam Lite TURN relay (coturn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={TURN_USER}
Group={TURN_USER}
RuntimeDirectory=homecam-turn
RuntimeDirectoryMode=0750
ExecStart=/usr/bin/turnserver -c /etc/homecam-lite/turnserver.conf
Restart=on-failure
RestartSec=3s
UMask=0077
MemoryMax=256M
TasksMax=64
LimitNOFILE=4096
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectHostname=yes
ProtectClock=yes
LockPersonality=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET
ReadWritePaths=/run/homecam-turn

[Install]
WantedBy=multi-user.target
"""
    return {
        "homecam.env": (env, 0o640, APP_USER),
        "turnserver.conf": (turn_conf, 0o640, TURN_USER),
        "homecam-lite.caddy": (caddy, 0o644, None),
        "homecam-lite.service": (app_unit, 0o644, None),
        "homecam-turn.service": (turn_unit, 0o644, None),
    }


def render_to(cfg: dict[str, Any], output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.is_symlink():
        fail(f"refusing symlink render directory: {output}")
    files = config_files(cfg)
    mapping = {
        "homecam.env": (output / "homecam.env", 0o600),
        "turnserver.conf": (output / "turnserver.conf", 0o600),
        "homecam-lite.caddy": (output / "homecam-lite.caddy", 0o644),
        "homecam-lite.service": (output / "homecam-lite.service", 0o644),
        "homecam-turn.service": (output / "homecam-turn.service", 0o644),
    }
    for name, (content, _mode, _group) in files.items():
        path, mode = mapping[name]
        atomic_write(path, content, mode)
    os.chmod(output, 0o700)


def read_os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = value.strip().strip('"')
    except OSError:
        pass
    return result


def ss_bound_ports() -> list[tuple[str, int, str]]:
    ss = shutil.which("ss")
    if not ss:
        fail("`ss` is required for safe port-conflict checks; install iproute2 and rerun")
    proc = subprocess.run([ss, "-H", "-lntu"], text=True, capture_output=True, check=False)
    if proc.returncode:
        fail(f"could not inspect listening sockets with ss: {proc.stderr.strip()}")
    result: list[tuple[str, int, str]] = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        proto = fields[0].lower()
        local = fields[4]
        try:
            port_text = local.rsplit(":", 1)[1]
            port = int(port_text)
        except (IndexError, ValueError):
            continue
        result.append((proto, port, line))
    return result


def check_local_ipv4(address: str) -> bool:
    ip_cmd = shutil.which("ip")
    if not ip_cmd:
        return False
    proc = subprocess.run([ip_cmd, "-o", "-4", "addr", "show"], text=True, capture_output=True, check=False)
    return proc.returncode == 0 and any(address in line.split() for line in proc.stdout.splitlines())


def check_preflight(cfg: dict[str, Any], *, for_apply: bool = False) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    os_release = read_os_release()
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "24.04":
        problems.append("target OS must be Ubuntu 24.04; detected " + (os_release.get("PRETTY_NAME") or "unknown"))
    try:
        version = subprocess.run(["python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True, capture_output=True, check=True).stdout.strip()
        if tuple(int(x) for x in version.split(".")) < (3, 12):
            problems.append(f"Python 3.12 or newer is required; detected Python {version}")
    except (OSError, subprocess.CalledProcessError, ValueError):
        problems.append("python3 is missing or could not be executed")
    missing_packages = []
    for command, package in (("/usr/bin/turnserver", "coturn"), ("/usr/bin/caddy", "caddy")):
        if not Path(command).exists():
            missing_packages.append(package)
    try:
        subprocess.run(["python3", "-m", "venv", "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.CalledProcessError):
        missing_packages.append("python3-venv")
    if missing_packages:
        note = "missing apt package(s): " + ", ".join(sorted(set(missing_packages)))
        notes.append(note)
        if for_apply:
            problems.append(note + "; install them using the safe package steps in docs/DEPLOY.md")
    if not (PROJECT_DIR / "requirements.txt").is_file() or not (PROJECT_DIR / "homecam" / "__main__.py").is_file():
        problems.append(f"project source is incomplete under {PROJECT_DIR}; expected requirements.txt and homecam/__main__.py")
    if (PROJECT_DIR / "requirements.txt").is_file():
        requirements = (PROJECT_DIR / "requirements.txt").read_text(encoding="utf-8")
        if not re.search(r"(?m)^\s*aiohttp\s*==\s*3\.14\.3\s*(?:#.*)?$", requirements):
            problems.append("requirements.txt must pin aiohttp==3.14.3")
    if for_apply and not check_local_ipv4(cfg["private_ip"]):
        problems.append(f"private_ip {cfg['private_ip']} is not assigned to this host; confirm the provider private address")
    if for_apply:
        try:
            os_release_text = Path("/etc/os-release").read_text(encoding="utf-8")
            if "VERSION_ID=\"24.04\"" not in os_release_text and "VERSION_ID=24.04" not in os_release_text:
                problems.append("apply is restricted to Ubuntu 24.04")
        except OSError:
            problems.append("cannot verify /etc/os-release")
        if os.geteuid() != 0:
            problems.append("--apply must run as root, for example through sudo")
        if not Path("/etc/caddy").is_dir():
            problems.append("/etc/caddy does not exist; install the caddy package before applying")
        for username in (APP_USER, TURN_USER):
            try:
                pwd.getpwnam(username)
            except KeyError:
                continue
            problems.append(f"system account {username} already exists; refusing to reuse it")
        for path in (APP_UNIT, TURN_UNIT):
            if path.exists() and not path.read_text(encoding="utf-8", errors="replace").startswith(MARKER):
                problems.append(f"refusing to overwrite unmanaged systemd unit: {path}")
        for path in (CONFIG_DIR / "homecam.env", CONFIG_DIR / "turnserver.conf", CADDY_SNIPPET):
            if path.exists() and not path.read_text(encoding="utf-8", errors="replace").startswith(MARKER):
                problems.append(f"refusing to overwrite unmanaged config: {path}")
        if APP_DIR.exists() and not APP_DIR.is_dir():
            problems.append(f"refusing non-directory application path: {APP_DIR}")
        current = APP_DIR / "current"
        if current.exists() and not current.is_symlink():
            problems.append(f"refusing to replace non-symlink release pointer: {current}")
        if current.is_symlink():
            target = (current.parent / os.readlink(current)).resolve()
            if APP_DIR.resolve() not in target.parents:
                problems.append(f"current release symlink points outside {APP_DIR}")
        try:
            sockets = ss_bound_ports()
            conflicts = []
            for proto, port, line in sockets:
                conflict = (proto in ("tcp", "tcp6") and port == 8088) or (
                    proto in ("tcp", "tcp6", "udp", "udp6") and port == 3478
                ) or (proto in ("udp", "udp6") and 49160 <= port <= 49179)
                if conflict:
                    conflicts.append(line.strip())
            if conflicts:
                problems.append("required HomeCam port(s) are already bound; do not stop the owner automatically: " + " | ".join(conflicts))
        except DeployError as exc:
            problems.append(str(exc))
        if not problems:
            notes.append("local checks passed; provider firewall/NAT and Caddy import still require operator setup")
    else:
        notes.append("read-only preview only; no server packages, sockets, or services were changed")
    return problems, notes


def print_preflight(cfg: dict[str, Any], *, for_apply: bool = False) -> bool:
    problems, notes = check_preflight(cfg, for_apply=for_apply)
    print(f"HomeCam Lite preflight for https://{cfg['hostname']}")
    print(f"  TURN advertised address: {cfg['public_ip']}; relay bind address: {cfg['private_ip']}")
    print("  Planned units: homecam-lite.service, homecam-turn.service")
    print("  Planned files: /etc/homecam-lite/{homecam.env,turnserver.conf}, /etc/caddy/homecam-lite.caddy")
    print("  Caddyfile, SSH, firewall, and existing services will not be edited by this installer.")
    for note in notes:
        print(f"  NOTE: {note}")
    for problem in problems:
        print(f"  BLOCKER: {problem}")
    if problems:
        print("Preflight failed. Resolve every blocker, then rerun the preview.")
        return False
    if for_apply:
        print("Preflight passed. This invocation may now install only the HomeCam files and units shown above.")
    else:
        print("Preview complete. Review these checks, then rerun with --apply to install.")
    return True


def ensure_safe_dir(path: Path, mode: int) -> None:
    if path.is_symlink():
        fail(f"refusing symlink directory: {path}")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if not path.is_dir():
        fail(f"expected directory: {path}")
    os.chmod(path, mode)


def ensure_managed_file(path: Path, content: str, mode: int, group_name: str | None = None) -> None:
    if path.exists():
        if path.is_symlink():
            fail(f"refusing symlink config: {path}")
        previous = path.read_text(encoding="utf-8", errors="replace")
        if not previous.startswith(MARKER):
            fail(f"refusing to overwrite unmanaged config: {path}")
        backup = path.with_name(path.name + ".bak." + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        atomic_write(backup, previous, 0o600)
    atomic_write(path, content, mode)
    gid = 0 if group_name is None else grp.getgrnam(group_name).gr_gid
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def make_account(username: str, home: Path) -> None:
    if shutil.which("useradd") is None:
        fail("useradd command is required")
    subprocess.run(
        ["useradd", "--system", "--user-group", "--home-dir", str(home), "--shell", "/usr/sbin/nologin", username],
        check=True,
    )


def install(cfg: dict[str, Any]) -> None:
    if not print_preflight(cfg, for_apply=True):
        fail("installation blocked by preflight")
    assert (PROJECT_DIR / "requirements.txt").is_file()
    ensure_safe_dir(CONFIG_DIR, 0o755)
    ensure_safe_dir(APP_DIR, 0o755)
    ensure_safe_dir(APP_DIR / "releases", 0o755)
    ensure_safe_dir(STATE_DIR, 0o700)
    make_account(APP_USER, STATE_DIR)
    make_account(TURN_USER, Path("/var/lib/homecam-turn"))
    app_gid = grp.getgrnam(APP_USER).gr_gid
    os.chown(STATE_DIR, pwd.getpwnam(APP_USER).pw_uid, app_gid)
    os.chmod(STATE_DIR, 0o700)
    files = config_files(cfg)
    ensure_managed_file(CONFIG_DIR / "homecam.env", files["homecam.env"][0], 0o640, APP_USER)
    ensure_managed_file(CONFIG_DIR / "turnserver.conf", files["turnserver.conf"][0], 0o640, TURN_USER)
    ensure_managed_file(CADDY_SNIPPET, files["homecam-lite.caddy"][0], 0o644)
    ensure_managed_file(APP_UNIT, files["homecam-lite.service"][0], 0o644)
    ensure_managed_file(TURN_UNIT, files["homecam-turn.service"][0], 0o644)

    release_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = APP_DIR / "releases" / release_id
    if release.exists():
        fail(f"release directory already exists: {release}")
    shutil.copytree(
        PROJECT_DIR,
        release,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", "dist", "build"),
    )
    uid = pwd.getpwnam(APP_USER).pw_uid
    for root, dirs, files_in_dir in os.walk(release):
        os.chown(root, 0, app_gid)
        os.chmod(root, 0o750)
        for name in dirs:
            child = Path(root) / name
            os.chown(child, 0, app_gid)
            os.chmod(child, 0o750)
        for name in files_in_dir:
            child = Path(root) / name
            os.chown(child, 0, app_gid)
            current_mode = stat.S_IMODE(child.stat().st_mode)
            os.chmod(child, 0o750 if current_mode & 0o111 else 0o640)
    venv_python = release / ".venv" / "bin" / "python"
    subprocess.run(["python3", "-m", "venv", str(release / ".venv")], check=True)
    subprocess.run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(release / "requirements.txt")], check=True)
    subprocess.run([str(venv_python), "-m", "pip", "check"], check=True)
    subprocess.run([str(venv_python), "-m", "compileall", "-q", str(release / "homecam")], check=True)
    for root, dirs, files_in_dir in os.walk(release):
        os.chown(root, 0, app_gid)
        for name in dirs:
            os.chown(Path(root) / name, 0, app_gid)
        for name in files_in_dir:
            os.chown(Path(root) / name, 0, app_gid)

    current = APP_DIR / "current"
    new_link = APP_DIR / f".current.{release_id}"
    os.symlink(Path("releases") / release_id, new_link)
    os.replace(new_link, current)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(f"Installed release {release_id} at {release}")
    print("Units are installed but not enabled or started. Bootstrap the admin, integrate the Caddy import, then enable and start explicitly.")
    print("Next: sudo -u homecam env HOMECAM_DB=/var/lib/homecam-lite/homecam.db /opt/homecam-lite/current/.venv/bin/python -m homecam init-admin")
    print("Then: sudo systemctl enable --now homecam-turn.service homecam-lite.service")


def create_config(args: argparse.Namespace) -> None:
    if not all((args.hostname, args.public_ip, args.private_ip)):
        fail("--init-config requires --hostname, --public-ip, and --private-ip")
    hostname = validate_hostname(args.hostname)
    public_ip, private_ip = validate_ips(args.public_ip, args.private_ip)
    cfg = {
        "schema_version": 1,
        "hostname": hostname,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "turn_secret": secrets.token_hex(32),
        "allow_private_relay": bool(args.allow_private_relay),
    }
    write_private_config(args.config, cfg, replace=False)
    print(f"Created private deployment config: {args.config} (mode 600)")
    print("The generated TURN secret is stored only in that file; it was not printed.")


def rotate_secret(path: Path) -> None:
    cfg = read_config(path)
    cfg["turn_secret"] = secrets.token_hex(32)
    write_private_config(path, cfg, replace=True)
    print(f"Generated a new local TURN secret in {path} (mode 600).")
    print("This does not update the server. Review preflight, apply the config, then restart both HomeCam units; existing TURN credentials stop working.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/root/homecam-lite-deploy.json"), help="mode-600 JSON deployment config (default: /root/homecam-lite-deploy.json)")
    parser.add_argument("--init-config", action="store_true", help="create a local config and generate its TURN secret; makes no server changes")
    parser.add_argument("--hostname", help="production DNS hostname (required with --init-config)")
    parser.add_argument("--public-ip", help="public IPv4 address used in TURN candidates")
    parser.add_argument("--private-ip", help="server's RFC1918 IPv4 relay/listener address")
    parser.add_argument(
        "--allow-private-relay",
        action="store_true",
        help="with --init-config only: opt in to coturn's all-ports allowed-peer-ip exception; use only with the documented homecam-turn UID OUTPUT guard",
    )
    parser.add_argument("--rotate-turn-secret", action="store_true", help="rotate only the secret in the local config; server is unchanged")
    parser.add_argument("--render-to", type=Path, help="render reviewable config files to a local directory; no server changes")
    parser.add_argument("--apply", action="store_true", help="install HomeCam release/configs/units after preflight")
    args = parser.parse_args()
    try:
        if args.init_config:
            create_config(args)
            return 0
        if args.allow_private_relay:
            fail("--allow-private-relay is only valid with --init-config")
        if args.rotate_turn_secret:
            rotate_secret(args.config)
            return 0
        cfg = read_config(args.config)
        if args.render_to:
            render_to(cfg, args.render_to)
            print(f"Rendered config files under {args.render_to}; secret-bearing files are mode 600.")
        if args.apply:
            install(cfg)
            return 0
        if not args.render_to:
            print_preflight(cfg)
        return 0
    except (DeployError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
