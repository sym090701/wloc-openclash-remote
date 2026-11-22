"""mitmproxy addon and local control plane for selected WLOC clients."""

from __future__ import annotations

import logging
import os

from mitmproxy import http

from wloc_config import (
    ConfigError,
    SCHEMA_VERSION,
    WlocConfigStore,
    device_for_slot,
    listener_port_slot,
    provider_expected_counts,
)
from wloc_patch import PatchError, TargetLocation, patch_wloc_body
from wloc_web import (
    AuthStore,
    EventBuffer,
    MihomoProviderClient,
    ProviderError,
    WlocWebApplication,
    WlocWebServer,
    parse_networks,
)


LOGGER = logging.getLogger("wloc")
ALLOWED_HOSTS = {"gs-loc.apple.com", "gs-loc-cn.apple.com"}
WLOC_PATH = "/clls/wloc"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


class WlocAddon:
    def __init__(self) -> None:
        raw_allowed_clients = os.environ.get("WLOC_ALLOWED_CLIENTS", "").strip()
        if not raw_allowed_clients:
            raw_allowed_clients = _required_env("WLOC_ALLOWED_CLIENT")
        self.allowed_clients = frozenset(
            client.strip() for client in raw_allowed_clients.split(",") if client.strip()
        )
        if not self.allowed_clients:
            raise RuntimeError("WLOC_ALLOWED_CLIENTS must contain at least one client IP")

        config_path = os.environ.get("WLOC_CONFIG_PATH", "/opt/wloc-state/wloc.json")
        provider_path = os.environ.get(
            "WLOC_PROVIDER_PATH", "/opt/wloc-rules/wloc-clients.yaml"
        )
        backup_dir = os.environ.get("WLOC_BACKUP_DIR", "/opt/wloc-state/backups")
        self.store = WlocConfigStore(config_path, provider_path, backup_dir)
        initial_device_ip = _required_env("WLOC_INITIAL_DEVICE_IP")
        initial_device_name = (
            os.environ.get("WLOC_INITIAL_DEVICE_NAME", "Apple device").strip()
            or "Apple device"
        )
        initial_target = {
            "name": os.environ.get("WLOC_INITIAL_LOCATION_NAME", "Initial location").strip()
            or "Initial location",
            "latitude": float(_required_env("WLOC_LATITUDE")),
            "longitude": float(_required_env("WLOC_LONGITUDE")),
            "accuracy": int(_required_env("WLOC_ACCURACY")),
        }
        initial_config = {
            "schema": SCHEMA_VERSION,
            "revision": 1,
            "devices": [
                {
                    "id": "device-primary",
                    "name": initial_device_name,
                    "ip": initial_device_ip,
                    "mac": os.environ.get("WLOC_INITIAL_DEVICE_MAC", "").strip(),
                    "enabled": True,
                    "slot": 1,
                    "location": initial_target,
                }
            ],
            "presets": [{"id": "initial", **initial_target}],
        }
        config = self.store.initialize(initial_config)
        self.events = EventBuffer()
        self.web: WlocWebServer | None = None
        try:
            # Missing public deployment settings must fail closed on loopback.
            # The installer supplies an explicit LAN address from config.env.
            bind_host = os.environ.get("WLOC_WEBUI_BIND", "127.0.0.1").strip()
            webui_port = int(os.environ.get("WLOC_WEBUI_PORT", "18082"))
            allowed_hosts = frozenset(
                host.strip().lower()
                for host in os.environ.get(
                    "WLOC_WEBUI_ALLOWED_HOSTS", f"{bind_host}:{webui_port}"
                ).split(",")
                if host.strip()
            )
            auth = AuthStore(
                os.environ.get("WLOC_AUTH_PATH", "/opt/wloc-state/auth.json")
            )
            provider = MihomoProviderClient(
                os.environ.get("WLOC_MIHOMO_API", "http://127.0.0.1:9090"),
                os.environ.get(
                    "WLOC_MIHOMO_SECRET_PATH", "/opt/wloc-state/mihomo.secret"
                ),
                ready_path=os.environ.get(
                    "WLOC_PROVIDER_READY_PATH",
                    "/opt/wloc-state/provider-ready-v2.json",
                ),
            )
            recovery_slots = self.store.recovery_slots()
            if recovery_slots:
                try:
                    recovery_status = provider.status(provider_expected_counts(config))
                    if not recovery_status.get("available"):
                        raise ProviderError(
                            str(recovery_status.get("message") or "OpenClash provider unavailable")
                        )
                    provider.refresh(provider_expected_counts(config))
                    self.store.unblock_slots(recovery_slots)
                    self.events.record(
                        "success",
                        "已恢复并确认上次未完成的设备路由切换",
                        {"slots": list(recovery_slots)},
                    )
                except ProviderError as recovery_error:
                    self.events.record(
                        "error",
                        "设备路由仍处于保护性阻断，等待规则恢复确认",
                        {"slots": list(recovery_slots), "error": str(recovery_error)},
                    )
            application = WlocWebApplication(
                store=self.store,
                auth=auth,
                provider=provider,
                events=self.events,
                static_dir=os.environ.get("WLOC_WEBUI_STATIC", "/opt/wloc/webui"),
                bind_host=bind_host,
                port=webui_port,
                allowed_networks=parse_networks(
                    os.environ.get(
                        "WLOC_WEBUI_ALLOWED_NETWORKS", "127.0.0.0/8"
                    )
                ),
                allowed_hosts=allowed_hosts,
            )
            self.web = WlocWebServer(application)
        except (ConfigError, OSError, ValueError) as exc:
            LOGGER.error("[wloc] WebUI disabled: %s", exc)
            self.events.record("error", "WebUI 启动准备失败", {"error": str(exc)})

        enabled_devices = [device for device in config["devices"] if device["enabled"]]
        LOGGER.info(
            "[wloc] addon ready clients=%s schema=%d devices=%d",
            ",".join(sorted(self.allowed_clients)),
            config["schema"],
            len(enabled_devices),
        )

    def running(self) -> None:
        if self.web is None:
            return
        try:
            self.web.start()
            LOGGER.info(
                "[wloc] WebUI listening on http://%s:%d",
                self.web.app.bind_host,
                self.web.app.port,
            )
        except OSError as exc:
            LOGGER.error("[wloc] WebUI failed to bind: %s", exc)
            self.events.record("error", "WebUI 监听失败", {"error": str(exc)})

    def done(self) -> None:
        if self.web is not None:
            self.web.stop()

    @staticmethod
    def _client_ip(flow: http.HTTPFlow) -> str:
        peer = flow.client_conn.peername
        return str(peer[0]) if peer else ""

    @staticmethod
    def _is_wloc(flow: http.HTTPFlow) -> bool:
        return (
            flow.request.host.lower() in ALLOWED_HOSTS
            and flow.request.path.split("?", 1)[0] == WLOC_PATH
        )

    @staticmethod
    def _listener_slot(flow: http.HTTPFlow) -> int | None:
        sockname = flow.client_conn.sockname
        if not sockname or len(sockname) < 2:
            return None
        try:
            port = int(sockname[1])
        except (TypeError, ValueError):
            return None
        slot = listener_port_slot(port)
        proxy_mode = getattr(flow.client_conn, "proxy_mode", None)
        configured_port = getattr(proxy_mode, "custom_listen_port", None)
        if configured_port not in (None, port):
            LOGGER.error(
                "[wloc] listener mismatch sockname_port=%s configured_port=%s",
                port,
                configured_port,
            )
            return None
        return slot

    @staticmethod
    def _observed_mac(ip: str) -> str:
        try:
            with open("/proc/net/arp", "r", encoding="ascii") as handle:
                for line in handle.read().splitlines()[1:]:
                    columns = line.split()
                    if len(columns) >= 4 and columns[0] == ip and columns[2] != "0x0":
                        return columns[3].upper()
        except OSError:
            pass
        return ""

    def _deny(self, flow: http.HTTPFlow, message: str, detail: dict[str, object]) -> None:
        LOGGER.warning("[wloc] denied %s", detail)
        self.events.record("warning", message, detail)
        flow.response = http.Response.make(
            403,
            b"WLOC proxy rejected an unassigned device route.\n",
            {"Content-Type": "text/plain"},
        )

    def request(self, flow: http.HTTPFlow) -> None:
        if not self._is_wloc(flow):
            return
        client_ip = self._client_ip(flow)
        if client_ip not in self.allowed_clients:
            self._deny(
                flow,
                "拒绝了非预期代理来源",
                {"client": client_ip or "unknown"},
            )
            return
        slot = self._listener_slot(flow)
        if slot is None:
            self._deny(
                flow,
                "拒绝了未知的本地路由入口",
                {"client": client_ip or "unknown", "sockname": str(flow.client_conn.sockname)},
            )
            return
        if self.store.is_slot_blocked(slot):
            self._deny(
                flow,
                "设备路由正在原子切换，已暂时拒绝本次定位请求",
                {"slot": slot, "client": client_ip},
            )
            return
        try:
            config = self.store.load()
            device = device_for_slot(config, slot)
            if device is None or device.get("enabled") is not True:
                self._deny(
                    flow,
                    "拒绝了未分配或已停用的设备槽位",
                    {"slot": slot, "client": client_ip},
                )
                return
            expected_mac = str(device.get("mac") or "").upper()
            observed_mac = self._observed_mac(str(device["ip"]))
            if expected_mac and observed_mac and expected_mac != observed_mac:
                self._deny(
                    flow,
                    "拒绝了 IP 与固定 WLAN 地址不符的设备",
                    {
                        "slot": slot,
                        "ip": device["ip"],
                        "expected_mac": expected_mac,
                        "observed_mac": observed_mac,
                    },
                )
                return
            location = device["location"]
            target = TargetLocation(
                latitude=location["latitude"],
                longitude=location["longitude"],
                accuracy=location["accuracy"],
            )
            target.validate()
            flow.metadata["wloc_target"] = target
            flow.metadata["wloc_target_name"] = location["name"]
            flow.metadata["wloc_revision"] = config["revision"]
            flow.metadata["wloc_authorized"] = True
            flow.metadata["wloc_slot"] = slot
            flow.metadata["wloc_device_id"] = device["id"]
            flow.metadata["wloc_device_name"] = device["name"]
        except (ConfigError, PatchError, OSError, ValueError) as exc:
            LOGGER.error("[wloc] invalid runtime config; fail-open error=%s", exc)
            self.events.record("error", "运行配置无效，已放行原始响应", {"error": str(exc)})
            flow.metadata["wloc_config_error"] = True
            return
        LOGGER.info(
            "[wloc] request client=%s slot=%s device=%s host=%s path=%s revision=%s target=%s",
            client_ip,
            slot,
            flow.metadata["wloc_device_name"],
            flow.request.host,
            flow.request.path,
            flow.metadata["wloc_revision"],
            flow.metadata["wloc_target_name"],
        )

    def response(self, flow: http.HTTPFlow) -> None:
        if (
            not self._is_wloc(flow)
            or flow.metadata.get("wloc_authorized") is not True
            or flow.metadata.get("wloc_config_error")
        ):
            return
        if flow.response is None or flow.response.status_code != 200:
            LOGGER.warning(
                "[wloc] upstream status=%s; passthrough",
                flow.response.status_code if flow.response else "none",
            )
            return
        try:
            original = flow.response.content or b""
            target = flow.metadata.get("wloc_target")
            if not isinstance(target, TargetLocation):
                raise PatchError("request has no validated target snapshot")
            result = patch_wloc_body(original, target)
            flow.response.content = result.data
            flow.response.headers.pop("content-md5", None)
            flow.response.headers.pop("etag", None)
            LOGGER.info(
                "[wloc] patched=1 kind=%s wifi=%d cell=%d locations=%d skipped=%d bytes=%d->%d",
                result.kind,
                result.stats.wifi,
                result.stats.cell,
                result.stats.locations,
                result.stats.skipped,
                len(original),
                len(result.data),
            )
            self.events.record(
                "success",
                "定位响应已改写",
                {
                    "target": flow.metadata.get("wloc_target_name", ""),
                    "device": flow.metadata.get("wloc_device_name", ""),
                    "device_id": flow.metadata.get("wloc_device_id", ""),
                    "slot": flow.metadata.get("wloc_slot"),
                    "revision": flow.metadata.get("wloc_revision"),
                    "host": flow.request.host,
                    "locations": result.stats.locations,
                    "wifi": result.stats.wifi,
                    "cell": result.stats.cell,
                },
            )
        except (PatchError, ValueError, OSError) as exc:
            LOGGER.error("[wloc] patched=0 fail-open error=%s", exc)
            self.events.record("error", "定位响应改写失败，已原样放行", {"error": str(exc)})


addons = [WlocAddon()]
