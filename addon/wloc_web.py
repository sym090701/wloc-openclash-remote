"""Small authenticated LAN WebUI embedded in the mitmproxy addon process."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import base64
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit

from wloc_config import (
    AGGREGATE_PROVIDER_NAME,
    MAX_DEVICE_SLOTS,
    ConfigConflict,
    ConfigError,
    WlocConfigStore,
    _atomic_write,
    enabled_device_cidrs,
    provider_expected_counts,
    slot_provider_name,
    utc_now,
)


WEBUI_VERSION = "2.0.0"
SESSION_COOKIE = "wloc_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_JSON_BYTES = 64 * 1024
PASSWORD_ITERATIONS = 310_000


class WebError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ProviderError(ConfigError):
    """Raised when a provider update cannot be verified and was rolled back."""


class ProviderIndeterminate(ProviderError):
    """Raised when provider rollback could not be confirmed; slots stay blocked."""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class EventBuffer:
    def __init__(self, maximum: int = 100) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maximum)
        self._lock = threading.Lock()

    def record(self, level: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        event = {
            "time": utc_now(),
            "level": level,
            "message": message,
            "detail": dict(detail or {}),
        }
        with self._lock:
            self._events.appendleft(event)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]


class AuthStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        if not self.path.is_file():
            raise ConfigError(f"WebUI authentication file is missing: {self.path}")
        self._load()

    @classmethod
    def bootstrap(cls, path: str, password: str, *, replace: bool = False) -> "AuthStore":
        target = Path(path)
        if target.exists() and not replace:
            raise ConfigError(f"authentication file already exists: {target}")
        cls.validate_password(password)
        salt = secrets.token_bytes(24)
        record = {
            "schema": 1,
            "iterations": PASSWORD_ITERATIONS,
            "salt": _b64encode(salt),
            "password_hash": _b64encode(
                hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
            ),
            "session_secret": _b64encode(secrets.token_bytes(32)),
            "updated_at": utc_now(),
        }
        _atomic_write(
            target,
            (json.dumps(record, indent=2) + "\n").encode("utf-8"),
            0o600,
        )
        return cls(path)

    @staticmethod
    def validate_password(password: Any) -> None:
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ConfigError("password must be 10-128 characters")

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("schema") != 1:
            raise ConfigError("unsupported authentication schema")
        iterations = record.get("iterations")
        if not isinstance(iterations, int) or not 100_000 <= iterations <= 2_000_000:
            raise ConfigError("invalid password iteration count")
        try:
            salt = _b64decode(record["salt"])
            password_hash = _b64decode(record["password_hash"])
            session_secret = _b64decode(record["session_secret"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ConfigError("invalid authentication file") from exc
        if len(salt) < 16 or len(password_hash) != 32 or len(session_secret) < 32:
            raise ConfigError("invalid authentication material")
        self._record = record
        self._salt = salt
        self._password_hash = password_hash
        self._session_secret = session_secret

    def verify_password(self, password: Any) -> bool:
        if not isinstance(password, str):
            return False
        with self._lock:
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                self._salt,
                self._record["iterations"],
            )
            return hmac.compare_digest(candidate, self._password_hash)

    def change_password(self, current_password: Any, new_password: Any) -> None:
        self.validate_password(new_password)
        with self._lock:
            if not self.verify_password(current_password):
                raise ConfigError("current password is incorrect")
            salt = secrets.token_bytes(24)
            record = {
                "schema": 1,
                "iterations": PASSWORD_ITERATIONS,
                "salt": _b64encode(salt),
                "password_hash": _b64encode(
                    hashlib.pbkdf2_hmac(
                        "sha256", new_password.encode("utf-8"), salt, PASSWORD_ITERATIONS
                    )
                ),
                "session_secret": _b64encode(secrets.token_bytes(32)),
                "updated_at": utc_now(),
            }
            _atomic_write(
                self.path,
                (json.dumps(record, indent=2) + "\n").encode("utf-8"),
                0o600,
            )
            self._load()

    def create_session(self) -> str:
        with self._lock:
            expires = int(time.time()) + SESSION_TTL_SECONDS
            payload = f"{expires}.{secrets.token_urlsafe(18)}"
            signature = hmac.new(
                self._session_secret, payload.encode("ascii"), hashlib.sha256
            ).digest()
            return f"{_b64encode(payload.encode('ascii'))}.{_b64encode(signature)}"

    def verify_session(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            payload_part, signature_part = token.split(".", 1)
            payload = _b64decode(payload_part).decode("ascii")
            signature = _b64decode(signature_part)
            expires_text, _nonce = payload.split(".", 1)
            expires = int(expires_text)
        except (ValueError, UnicodeError):
            return False
        with self._lock:
            expected = hmac.new(
                self._session_secret, payload.encode("ascii"), hashlib.sha256
            ).digest()
        return expires >= int(time.time()) and hmac.compare_digest(signature, expected)


class MihomoProviderClient:
    def __init__(
        self,
        base_url: str,
        secret_path: str,
        ready_path: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret_path = Path(secret_path)
        self.provider_names = (
            AGGREGATE_PROVIDER_NAME,
            *(slot_provider_name(slot) for slot in range(1, MAX_DEVICE_SLOTS + 1)),
        )
        self.ready_path = (
            Path(ready_path)
            if ready_path is not None
            else self.secret_path.with_name("provider-ready-v2.json")
        )
        self.static_rules = os.environ.get("WLOC_STATIC_OPENCLASH_RULES", "").strip() == "1"

    def has_been_ready(self) -> bool:
        """Return true once this installation has observed the live provider.

        Any error other than a genuinely missing marker fails closed. Otherwise a
        permissions or I/O problem could make an established installation look
        like a first-run bootstrap and allow an unsynchronised device change.
        """

        try:
            self.ready_path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _mark_ready(self) -> None:
        if self.has_been_ready():
            return
        marker = {
            "schema": 2,
            "providers": list(self.provider_names),
            "observed_at": utc_now(),
        }
        _atomic_write(
            self.ready_path,
            (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            0o600,
        )

    def _secret(self) -> str:
        try:
            return self.secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _request(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        headers = {"User-Agent": f"wloc-control/{WEBUI_VERSION}"}
        secret = self._secret()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        request = urlrequest.Request(f"{self.base_url}{path}", method=method, headers=headers)
        try:
            with urlrequest.urlopen(request, timeout=3) as response:
                return response.status, response.read(MAX_JSON_BYTES)
        except urlerror.HTTPError as exc:
            return exc.code, exc.read(MAX_JSON_BYTES)
        except (OSError, TimeoutError) as exc:
            raise ProviderError(f"cannot reach OpenClash API: {exc}") from exc

    def status(self, expected_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
        if self.static_rules:
            expected = dict(expected_counts or {})
            providers = {
                name: {
                    "rule_count": count,
                    "expected_count": count,
                    "matches_expected": True,
                    "updated_at": None,
                }
                for name, count in expected.items()
            }
            return {
                "available": True,
                "controller_ok": True,
                "provider_missing": False,
                "bootstrap_allowed": False,
                "ever_ready": True,
                "name": AGGREGATE_PROVIDER_NAME,
                "rule_count": expected.get(AGGREGATE_PROVIDER_NAME, 0),
                "updated_at": None,
                "providers": providers,
                "message": "软路由静态全局规则已启用",
                "matches_expected": True,
            }
        ever_ready = self.has_been_ready()
        try:
            status, body = self._request("/providers/rules")
            if status != HTTPStatus.OK:
                return {
                    "available": False,
                    "controller_ok": False,
                    "provider_missing": False,
                    "bootstrap_allowed": False,
                    "ever_ready": ever_ready,
                    "name": AGGREGATE_PROVIDER_NAME,
                    "rule_count": None,
                    "updated_at": None,
                    "message": f"OpenClash API returned HTTP {status}",
                }
            payload = json.loads(body)
            if not isinstance(payload, Mapping):
                raise ProviderError("OpenClash provider response is not an object")
            providers = payload.get("providers")
            if not isinstance(providers, Mapping):
                raise ProviderError("OpenClash provider response has no providers map")
            missing = [
                name for name in self.provider_names if not isinstance(providers.get(name), Mapping)
            ]
            if missing:
                return {
                    "available": False,
                    "controller_ok": True,
                    "provider_missing": True,
                    "bootstrap_allowed": not ever_ready,
                    "ever_ready": ever_ready,
                    "name": AGGREGATE_PROVIDER_NAME,
                    "rule_count": None,
                    "updated_at": None,
                    "missing": missing,
                    "message": (
                        "等待首次重启 OpenClash 载入逐设备路由规则"
                        if not ever_ready
                        else "OpenClash 中已载入过的逐设备路由规则当前缺失"
                    ),
                }
            details: dict[str, dict[str, Any]] = {}
            matches = True
            for name in self.provider_names:
                provider = providers[name]
                rule_count = provider.get("ruleCount")
                expected = expected_counts.get(name) if expected_counts is not None else None
                provider_matches = expected is None or rule_count == expected
                matches = matches and provider_matches
                details[name] = {
                    "rule_count": rule_count,
                    "expected_count": expected,
                    "matches_expected": provider_matches,
                    "updated_at": provider.get("updatedAt"),
                }
            if matches:
                self._mark_ready()
            aggregate = details[AGGREGATE_PROVIDER_NAME]
            return {
                "available": True,
                "controller_ok": True,
                "provider_missing": False,
                "bootstrap_allowed": False,
                "ever_ready": True,
                "name": AGGREGATE_PROVIDER_NAME,
                "rule_count": aggregate["rule_count"],
                "updated_at": aggregate["updated_at"],
                "providers": details,
                "message": "规则已同步" if matches else "规则数量尚未同步",
                "matches_expected": matches,
            }
        except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "available": False,
                "controller_ok": False,
                "provider_missing": False,
                "bootstrap_allowed": False,
                "ever_ready": self.has_been_ready(),
                "name": AGGREGATE_PROVIDER_NAME,
                "rule_count": None,
                "updated_at": None,
                "message": str(exc),
            }

    def refresh(self, expected_counts: Mapping[str, int]) -> dict[str, Any]:
        if self.static_rules:
            return self.status(expected_counts)
        for name in self.provider_names:
            provider_name = quote(name, safe="")
            status_code, _body = self._request(f"/providers/rules/{provider_name}", "PUT")
            if status_code == HTTPStatus.NOT_FOUND:
                raise ProviderError(f"OpenClash provider disappeared during refresh: {name}")
            if status_code != HTTPStatus.NO_CONTENT:
                raise ProviderError(
                    f"OpenClash provider refresh returned HTTP {status_code}: {name}"
                )
        deadline = time.monotonic() + 3
        latest = self.status(expected_counts)
        while time.monotonic() < deadline:
            if latest.get("available") and latest.get("matches_expected"):
                return latest
            time.sleep(0.1)
            latest = self.status(expected_counts)
        message = latest.get("message") if isinstance(latest, Mapping) else None
        raise ProviderError(
            str(message or "OpenClash provider did not load the expected device count")
        )


class _RateLimiter:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[address]
            while attempts and attempts[0] < now - 300:
                attempts.popleft()
            return len(attempts) < 8

    def fail(self, address: str) -> None:
        with self._lock:
            self._attempts[address].append(time.monotonic())

    def clear(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


class WlocWebApplication:
    def __init__(
        self,
        *,
        store: WlocConfigStore,
        auth: AuthStore,
        provider: MihomoProviderClient,
        events: EventBuffer,
        static_dir: str,
        bind_host: str,
        port: int,
        allowed_networks: tuple[ipaddress._BaseNetwork, ...],
        allowed_hosts: frozenset[str],
    ) -> None:
        self.store = store
        self.auth = auth
        self.provider = provider
        self.events = events
        self.static_dir = Path(static_dir)
        self.bind_host = bind_host
        self.port = port
        self.allowed_networks = allowed_networks
        self.allowed_hosts = allowed_hosts
        self.ca_cert_path = Path(
            os.environ.get(
                "WLOC_CA_CERT_PATH",
                "/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem",
            )
        )
        self.started_at = utc_now()
        self.rate_limiter = _RateLimiter()
        self._mutation_lock = threading.RLock()

    def state(self) -> dict[str, Any]:
        config = self.store.load()
        provider = self.provider.status(provider_expected_counts(config))
        identity = self._identity_status(config)
        blocked_slots = self.store.blocked_slots()
        status = "online"
        if not provider.get("available") or not provider.get("matches_expected", True):
            status = "attention"
        if any(item["status"] == "mismatch" for item in identity.values()):
            status = "attention"
        if blocked_slots:
            status = "attention"
        return {
            "config": config,
            "service": {
                "status": status,
                "version": WEBUI_VERSION,
                "started_at": self.started_at,
                "provider": provider,
                "identity": identity,
                "blocked_slots": list(blocked_slots),
            },
            "events": self.events.snapshot(),
            "can_rollback": bool(self.store.list_backups()),
        }

    @staticmethod
    def _identity_status(config: Mapping[str, Any]) -> dict[str, dict[str, str | None]]:
        observed_by_ip: dict[str, str] = {}
        try:
            with open("/proc/net/arp", "r", encoding="ascii") as handle:
                for line in handle.read().splitlines()[1:]:
                    columns = line.split()
                    if len(columns) >= 4 and columns[2] != "0x0":
                        observed_by_ip[columns[0]] = columns[3].upper()
        except OSError:
            pass
        result: dict[str, dict[str, str | None]] = {}
        for device in config.get("devices", []):
            expected = str(device.get("mac") or "").upper()
            observed = observed_by_ip.get(str(device.get("ip")))
            if not expected or not observed:
                identity_status = "unknown"
            elif hmac.compare_digest(expected, observed):
                identity_status = "match"
            else:
                identity_status = "mismatch"
            result[str(device.get("id"))] = {
                "status": identity_status,
                "expected_mac": expected or None,
                "observed_mac": observed,
            }
        return result

    def _sync_device_provider(
        self,
        *,
        config: Mapping[str, Any],
        backup_id: str,
        before_status: Mapping[str, Any],
    ) -> str | None:
        """Refresh Mihomo or restore the just-written snapshot on any unsafe state."""

        expected_counts = provider_expected_counts(config)
        candidate_revision = config["revision"]
        if before_status.get("available"):
            try:
                refreshed = self.provider.refresh(expected_counts)
                if not (
                    refreshed.get("available")
                    and refreshed.get("matches_expected")
                ):
                    raise ProviderError("OpenClash provider did not confirm the device rules")
                return None
            except ProviderError as refresh_error:
                restored = self.store.restore_backup(
                    backup_id, expected_revision=candidate_revision
                )
                self.store.mark_backup_failed(backup_id, str(refresh_error))
                try:
                    self.provider.refresh(provider_expected_counts(restored))
                except ProviderError as rollback_error:
                    self.events.record(
                        "error",
                        "设备规则刷新失败，且 OpenClash 回退确认失败",
                        {"error": str(rollback_error)},
                    )
                    raise ProviderIndeterminate(
                        "设备规则刷新失败，且旧规则回退无法确认；受影响设备已保持阻断"
                    ) from rollback_error
                raise refresh_error

        self.store.restore_backup(backup_id, expected_revision=candidate_revision)
        failure_message = str(before_status.get("message") or "OpenClash provider is unavailable")
        self.store.mark_backup_failed(backup_id, failure_message)
        if before_status.get("bootstrap_allowed") is True:
            raise ProviderError(
                "设备路由尚未应用并已回退：请先手动重启一次 OpenClash，载入逐设备规则后再修改设备列表"
            )
        raise ProviderError(f"设备列表未应用并已回退：{failure_message}")

    def save_config(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        with self._mutation_lock:
            return self._save_config_locked(payload)

    def _save_config_locked(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        expected_revision = payload.get("revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ConfigError("revision is required")
        current = self.store.load()
        before_status = self.provider.status(provider_expected_counts(current))
        candidate = {
            "schema": 2,
            "devices": payload.get("devices", []),
            "presets": payload.get("presets", []),
        }
        config, backup_id, changed_slots = self.store.save(
            candidate, expected_revision=expected_revision
        )
        warning: str | None = None
        safe_to_unblock = not changed_slots
        try:
            if changed_slots:
                warning = self._sync_device_provider(
                    config=config,
                    backup_id=backup_id,
                    before_status=before_status,
                )
            safe_to_unblock = True
        except ProviderIndeterminate:
            raise
        except ProviderError:
            safe_to_unblock = True
            raise
        finally:
            if safe_to_unblock:
                self.store.unblock_slots(changed_slots)
        removed_ids = {device["id"] for device in current["devices"]} - {
            device["id"] for device in config["devices"]
        }
        self.store.retire_slots(
            tuple(
                int(device["slot"])
                for device in current["devices"]
                if device["id"] in removed_ids
            )
        )
        self.events.record(
            "success",
            "配置已更新",
            {
                "revision": config["revision"],
                "enabled_devices": len(enabled_device_cidrs(config)),
                "locations": {
                    device["name"]: device["location"]["name"]
                    for device in config["devices"]
                },
            },
        )
        return self.state(), warning

    def rollback_latest(self) -> tuple[dict[str, Any], str | None]:
        with self._mutation_lock:
            return self._rollback_latest_locked()

    def _rollback_latest_locked(self) -> tuple[dict[str, Any], str | None]:
        current = self.store.load()
        before_status = self.provider.status(provider_expected_counts(current))
        config, backup_id, changed_slots = self.store.rollback_latest()
        warning: str | None = None
        safe_to_unblock = not changed_slots
        try:
            if changed_slots:
                warning = self._sync_device_provider(
                    config=config,
                    backup_id=backup_id,
                    before_status=before_status,
                )
            safe_to_unblock = True
        except ProviderIndeterminate:
            raise
        except ProviderError:
            safe_to_unblock = True
            raise
        finally:
            if safe_to_unblock:
                self.store.unblock_slots(changed_slots)
        self.store.unretire_active_slots(config)
        self.events.record("warning", "已恢复上一版配置", {"revision": config["revision"]})
        return self.state(), warning


class _WlocHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: WlocWebApplication) -> None:
        self.app = app
        super().__init__(address, WlocRequestHandler)


class WlocRequestHandler(BaseHTTPRequestHandler):
    server_version = "WLOC-Control"
    sys_version = ""

    @property
    def app(self) -> WlocWebApplication:
        return self.server.app  # type: ignore[attr-defined, no-any-return]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _security_headers(self, *, cache: str = "no-store") -> None:
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https://tile.openstreetmap.org; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )

    def _send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
        *,
        cookie: str | None = None,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self._security_headers(cache=cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(
        self, status: int, payload: Mapping[str, Any], *, cookie: str | None = None
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8", cookie=cookie)

    def _fail(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _peer_allowed(self) -> bool:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(peer in network for network in self.app.allowed_networks)

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "").strip().lower()
        return host in self.app.allowed_hosts

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _authenticated(self) -> bool:
        return self.app.auth.verify_session(self._session_token())

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "").strip().lower()
        return origin == f"http://{host}"

    def _read_json(self) -> Mapping[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise WebError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WebError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if not 0 < length <= MAX_JSON_BYTES:
            raise WebError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WebError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc
        if not isinstance(payload, Mapping):
            raise WebError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return payload

    def _preflight(self) -> bool:
        if not self._peer_allowed():
            self._fail(HTTPStatus.FORBIDDEN, "client network is not allowed")
            return False
        if not self._host_allowed():
            self._fail(HTTPStatus.BAD_REQUEST, "invalid Host header")
            return False
        return True

    def _serve_static(self, path: str) -> bool:
        routes = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css; charset=utf-8"),
            "/vendor/leaflet.js": ("vendor/leaflet.js", "text/javascript; charset=utf-8"),
        }
        item = routes.get(path)
        if item is None:
            return False
        file_path = self.app.static_dir / item[0]
        try:
            content = file_path.read_bytes()
        except OSError:
            self._fail(HTTPStatus.NOT_FOUND, "asset not found")
            return True
        self._send_bytes(HTTPStatus.OK, content, item[1], cache="no-cache")
        return True

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._preflight():
            return
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True, "version": WEBUI_VERSION})
            return
        if self._serve_static(path):
            return
        if path == "/api/state":
            if not self._authenticated():
                self._fail(HTTPStatus.UNAUTHORIZED, "authentication required")
                return
            self._json(HTTPStatus.OK, {"ok": True, **self.app.state()})
            return
        if path == "/api/ca-cert":
            if not self._authenticated():
                self._fail(HTTPStatus.UNAUTHORIZED, "authentication required")
                return
            try:
                content = self.app.ca_cert_path.read_bytes()
            except OSError:
                self._fail(HTTPStatus.NOT_FOUND, "CA certificate is not available")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Length", str(len(content)))
            self.send_header(
                "Content-Disposition",
                'attachment; filename="wloc-mitmproxy-ca-cert.pem"',
            )
            self._security_headers(cache="no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
            return
        self._fail(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if not self._preflight():
            return
        path = urlsplit(self.path).path
        try:
            if not self._same_origin():
                raise WebError(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            if path == "/api/login":
                self._login()
                return
            if not self._authenticated():
                raise WebError(HTTPStatus.UNAUTHORIZED, "authentication required")
            if path == "/api/logout":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True},
                    cookie=f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
                )
                return
            if path == "/api/config":
                state, warning = self.app.save_config(self._read_json())
                self._json(HTTPStatus.OK, {"ok": True, **state, "warning": warning})
                return
            if path == "/api/rollback-latest":
                state, warning = self.app.rollback_latest()
                self._json(HTTPStatus.OK, {"ok": True, **state, "warning": warning})
                return
            if path == "/api/password":
                payload = self._read_json()
                self.app.auth.change_password(
                    payload.get("currentPassword"), payload.get("newPassword")
                )
                self.app.events.record("warning", "WebUI 管理密码已修改")
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "message": "密码已修改，请重新登录"},
                    cookie=f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
                )
                return
            raise WebError(HTTPStatus.NOT_FOUND, "not found")
        except ConfigConflict as exc:
            self._fail(HTTPStatus.CONFLICT, str(exc))
        except ProviderError as exc:
            self._fail(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except ConfigError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        except WebError as exc:
            self._fail(exc.status, str(exc))
        except Exception:
            self.app.events.record("error", "WebUI 请求处理失败")
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def _login(self) -> None:
        address = self.client_address[0]
        if not self.app.rate_limiter.allow(address):
            raise WebError(HTTPStatus.TOO_MANY_REQUESTS, "too many login attempts")
        payload = self._read_json()
        if not self.app.auth.verify_password(payload.get("password")):
            self.app.rate_limiter.fail(address)
            raise WebError(HTTPStatus.UNAUTHORIZED, "invalid password")
        self.app.rate_limiter.clear(address)
        token = self.app.auth.create_session()
        self.app.events.record("info", "WebUI 登录成功", {"client": address})
        self._json(
            HTTPStatus.OK,
            {"ok": True},
            cookie=(
                f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
                "HttpOnly; SameSite=Strict"
            ),
        )


class WlocWebServer:
    def __init__(self, app: WlocWebApplication) -> None:
        self.app = app
        self._server: _WlocHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        server = _WlocHTTPServer((self.app.bind_host, self.app.port), self.app)
        thread = threading.Thread(
            target=server.serve_forever,
            name="wloc-webui",
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        self.app.events.record(
            "success", "WebUI 已启动", {"address": f"{self.app.bind_host}:{self.app.port}"}
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


def parse_networks(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    if not networks:
        raise ConfigError("at least one WebUI client network is required")
    return tuple(networks)
