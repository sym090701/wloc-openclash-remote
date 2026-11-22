"""Validated, atomic runtime configuration for the WLOC service."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Mapping
import uuid


SCHEMA_VERSION = 2
MAX_DEVICE_SLOTS = 8
MAX_DEVICES = MAX_DEVICE_SLOTS
MAX_PRESETS = 64
AGGREGATE_PROVIDER_NAME = "wloc-clients"
SLOT_PROVIDER_PREFIX = "wloc-slot-"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z-r\d+$")
ALLOWED_DEVICE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)


class ConfigError(ValueError):
    """Raised when a runtime configuration is invalid or stale."""


class ConfigConflict(ConfigError):
    """Raised when a browser attempts to save an outdated revision."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def slot_provider_name(slot: int) -> str:
    return f"{SLOT_PROVIDER_PREFIX}{slot:02d}"


def slot_listener_port(slot: int) -> int:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= MAX_DEVICE_SLOTS:
        raise ConfigError(f"slot must be between 1 and {MAX_DEVICE_SLOTS}")
    return 18081 if slot == 1 else 18099 + slot


def listener_port_slot(port: int) -> int | None:
    for slot in range(1, MAX_DEVICE_SLOTS + 1):
        if slot_listener_port(slot) == port:
            return slot
    return None


def _text(value: Any, field: str, *, minimum: int = 1, maximum: int = 64) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be text")
    normalized = " ".join(value.strip().split())
    if not minimum <= len(normalized) <= maximum:
        raise ConfigError(f"{field} must be {minimum}-{maximum} characters")
    if any(ord(char) < 32 for char in normalized):
        raise ConfigError(f"{field} contains control characters")
    return normalized


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ConfigError(f"{field} must be between {minimum:g} and {maximum:g}")
    return number


def _location(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field} must be an object")
    accuracy_number = _number(value.get("accuracy"), f"{field}.accuracy", 1, 100_000)
    if not accuracy_number.is_integer():
        raise ConfigError(f"{field}.accuracy must be a whole number")
    return {
        "name": _text(value.get("name"), f"{field}.name"),
        "latitude": round(_number(value.get("latitude"), f"{field}.latitude", -90, 90), 8),
        "longitude": round(_number(value.get("longitude"), f"{field}.longitude", -180, 180), 8),
        "accuracy": int(accuracy_number),
    }


def _identifier(value: Any) -> str:
    if value in (None, ""):
        return uuid.uuid4().hex[:12]
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ConfigError("id must contain only letters, digits, underscore, or dash")
    return value


def _device_ip(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("device.ip must be an IPv4 address")
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError as exc:
        raise ConfigError("device.ip must be a valid IPv4 address") from exc
    if not any(address in network for network in ALLOWED_DEVICE_NETWORKS):
        raise ConfigError("device.ip must be an RFC1918 or CGNAT private address")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ConfigError("device.ip is not a usable client address")
    return str(address)


def _device_mac(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ConfigError("device.mac must be a MAC address or empty")
    compact = re.sub(r"[:-]", "", value.strip())
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        raise ConfigError("device.mac must contain 12 hexadecimal digits")
    octets = [int(compact[index : index + 2], 16) for index in range(0, 12, 2)]
    if octets == [0] * 6 or octets == [255] * 6 or octets[0] & 1:
        raise ConfigError("device.mac must be a usable unicast MAC address")
    return ":".join(f"{octet:02X}" for octet in octets)


def _slot(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be a whole number")
    if not 1 <= value <= MAX_DEVICE_SLOTS:
        raise ConfigError(f"{field} must be between 1 and {MAX_DEVICE_SLOTS}")
    return value


def normalize_config(
    raw: Any,
    *,
    revision: int | None = None,
    unavailable_slots: set[int] | frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Normalize schema 2 and transparently migrate legacy schema 1 state."""

    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be an object")
    schema = raw.get("schema", 1)
    if schema not in (1, SCHEMA_VERSION):
        raise ConfigError(f"unsupported schema version: {schema}")

    raw_revision = raw.get("revision", 1) if revision is None else revision
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 1:
        raise ConfigError("revision must be a positive integer")

    legacy_target = _location(raw.get("target"), "target") if schema == 1 else None
    devices_raw = raw.get("devices", [])
    if not isinstance(devices_raw, list) or len(devices_raw) > MAX_DEVICES:
        raise ConfigError(f"devices must be a list with at most {MAX_DEVICES} entries")

    devices: list[dict[str, Any]] = []
    seen_ips: set[str] = set()
    seen_macs: set[str] = set()
    seen_ids: set[str] = set()
    reserved_slots: set[int] = set()
    if schema == 2:
        for index, item in enumerate(devices_raw):
            if not isinstance(item, Mapping):
                raise ConfigError(f"devices[{index}] must be an object")
            explicit_slot = _slot(item.get("slot"), f"devices[{index}].slot")
            if explicit_slot is not None:
                if explicit_slot in reserved_slots:
                    raise ConfigError(f"duplicate device slot: {explicit_slot}")
                reserved_slots.add(explicit_slot)
    used_slots: set[int] = set()
    for index, item in enumerate(devices_raw):
        if not isinstance(item, Mapping):
            raise ConfigError(f"devices[{index}] must be an object")
        item_id = _identifier(item.get("id"))
        ip = _device_ip(item.get("ip"))
        mac = _device_mac(item.get("mac"))
        requested_slot = _slot(item.get("slot"), f"devices[{index}].slot") if schema == 2 else None
        if requested_slot is None:
            requested_slot = next(
                (
                    candidate
                    for candidate in range(1, MAX_DEVICE_SLOTS + 1)
                    if candidate not in used_slots
                    and candidate not in reserved_slots
                    and candidate not in unavailable_slots
                ),
                None,
            )
        if requested_slot is None:
            raise ConfigError("no free device routing slot is available")
        if requested_slot in used_slots:
            raise ConfigError(f"duplicate device slot: {requested_slot}")
        if item_id in seen_ids:
            raise ConfigError(f"duplicate device id: {item_id}")
        if ip in seen_ips:
            raise ConfigError(f"duplicate device IP: {ip}")
        if mac and mac in seen_macs:
            raise ConfigError(f"duplicate device MAC: {mac}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"devices[{index}].enabled must be true or false")
        location_value = legacy_target if schema == 1 else item.get("location")
        devices.append(
            {
                "id": item_id,
                "name": _text(item.get("name"), f"devices[{index}].name"),
                "ip": ip,
                "mac": mac,
                "enabled": enabled,
                "slot": requested_slot,
                "location": _location(location_value, f"devices[{index}].location"),
            }
        )
        seen_ids.add(item_id)
        seen_ips.add(ip)
        if mac:
            seen_macs.add(mac)
        used_slots.add(requested_slot)

    presets_raw = raw.get("presets", [])
    if not isinstance(presets_raw, list) or len(presets_raw) > MAX_PRESETS:
        raise ConfigError(f"presets must be a list with at most {MAX_PRESETS} entries")
    presets: list[dict[str, Any]] = []
    preset_ids: set[str] = set()
    for index, item in enumerate(presets_raw):
        preset = _location(item, f"presets[{index}]")
        preset_id = _identifier(item.get("id") if isinstance(item, Mapping) else None)
        if preset_id in preset_ids:
            raise ConfigError(f"duplicate preset id: {preset_id}")
        presets.append({"id": preset_id, **preset})
        preset_ids.add(preset_id)

    updated_at = raw.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        updated_at = utc_now()
    return {
        "schema": SCHEMA_VERSION,
        "revision": raw_revision,
        "updated_at": updated_at,
        "devices": devices,
        "presets": presets,
    }


def enabled_device_cidrs(config: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        f"{device['ip']}/32"
        for device in config.get("devices", [])
        if device.get("enabled") is True
    )


def device_for_slot(config: Mapping[str, Any], slot: int) -> Mapping[str, Any] | None:
    for device in config.get("devices", []):
        if device.get("slot") == slot:
            return device
    return None


def routing_signature(config: Mapping[str, Any]) -> tuple[tuple[int, str], ...]:
    return tuple(
        sorted(
            (int(device["slot"]), str(device["ip"]))
            for device in config.get("devices", [])
            if device.get("enabled") is True
        )
    )


def routing_slot_state(config: Mapping[str, Any]) -> dict[int, tuple[str, str] | None]:
    state: dict[int, tuple[str, str] | None] = {}
    for device in config.get("devices", []):
        slot = int(device["slot"])
        state[slot] = (
            (str(device["id"]), str(device["ip"]))
            if device.get("enabled") is True
            else None
        )
    return state


def changed_routing_slots(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[int, ...]:
    before_state = routing_slot_state(before)
    after_state = routing_slot_state(after)
    return tuple(
        slot
        for slot in range(1, MAX_DEVICE_SLOTS + 1)
        if before_state.get(slot) != after_state.get(slot)
    )


def provider_expected_counts(config: Mapping[str, Any]) -> dict[str, int]:
    expected = {AGGREGATE_PROVIDER_NAME: len(enabled_device_cidrs(config))}
    for slot in range(1, MAX_DEVICE_SLOTS + 1):
        device = device_for_slot(config, slot)
        expected[slot_provider_name(slot)] = int(bool(device and device.get("enabled") is True))
    return expected


def render_provider(config: Mapping[str, Any], slot: int | None = None) -> str:
    if slot is None:
        cidrs = enabled_device_cidrs(config)
    else:
        device = device_for_slot(config, slot)
        cidrs = (
            (f"{device['ip']}/32",)
            if device is not None and device.get("enabled") is True
            else ()
        )
    header = "# Managed atomically by WLOC Control.\n"
    if not cidrs:
        return f"{header}payload: []\n"
    return header + "payload:\n" + "".join(f"  - {cidr}\n" for cidr in cidrs)


def render_providers(config: Mapping[str, Any]) -> dict[str, str]:
    rendered = {f"{AGGREGATE_PROVIDER_NAME}.yaml": render_provider(config)}
    for slot in range(1, MAX_DEVICE_SLOTS + 1):
        rendered[f"{slot_provider_name(slot)}.yaml"] = render_provider(config, slot)
    return rendered


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class WlocConfigStore:
    """Thread-safe JSON state plus generated aggregate and per-slot providers."""

    def __init__(self, config_path: str, provider_path: str, backup_dir: str) -> None:
        self.config_path = Path(config_path)
        self.provider_path = Path(provider_path)
        self.provider_dir = self.provider_path.parent
        self.backup_dir = Path(backup_dir)
        self._lock = threading.RLock()
        self._cached_signature: tuple[int, int, int] | None = None
        self._cached_config: dict[str, Any] | None = None
        self.blocked_path = self.config_path.with_name("routing-blocked.json")
        self._blocked_slots: set[int] = set()
        self._recovery_slots: set[int] = set()
        self._retired_slots: set[int] = set()
        try:
            with self.blocked_path.open("r", encoding="utf-8") as handle:
                blocked_record = json.load(handle)
            if (
                isinstance(blocked_record, Mapping)
                and blocked_record.get("schema") == 1
                and isinstance(blocked_record.get("slots"), list)
            ):
                self._blocked_slots = {
                    int(slot)
                    for slot in blocked_record["slots"]
                    if isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= MAX_DEVICE_SLOTS
                }
                self._recovery_slots = set(self._blocked_slots)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _provider_paths(self) -> dict[str, Path]:
        return {name: self.provider_dir / name for name in render_providers({"devices": []})}

    def initialize(self, initial: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.config_path.exists():
                config = normalize_config(initial, revision=1)
                config["updated_at"] = utc_now()
                self._write_config_and_providers(config)
                return deepcopy(config)

            with self.config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            legacy = isinstance(raw, Mapping) and raw.get("schema", 1) == 1
            config = normalize_config(raw)
            if legacy:
                initial_config = normalize_config(initial)
                initial_by_id = {item["id"]: item for item in initial_config["devices"]}
                for device in config["devices"]:
                    seed = initial_by_id.get(device["id"])
                    if seed and seed["ip"] == device["ip"] and not device["mac"]:
                        device["mac"] = seed["mac"]
                self._create_backup(config)
                config["revision"] += 1
                config["updated_at"] = utc_now()
                self._write_config_and_providers(config)
                return deepcopy(config)

            expected = render_providers(config)
            for filename, content in expected.items():
                path = self.provider_dir / filename
                current = path.read_bytes() if path.exists() else b""
                encoded = content.encode("utf-8")
                if current != encoded:
                    _atomic_write(path, encoded, 0o644)
            self._cached_config = config
            self._cached_signature = self._signature(self.config_path)
            return deepcopy(config)

    def _load_from_disk(self) -> dict[str, Any]:
        with self.config_path.open("r", encoding="utf-8") as handle:
            config = normalize_config(json.load(handle))
        self._cached_config = config
        self._cached_signature = self._signature(self.config_path)
        return config

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                signature = self._signature(self.config_path)
                if self._cached_config is None or signature != self._cached_signature:
                    self._load_from_disk()
            except (OSError, ValueError, json.JSONDecodeError):
                if self._cached_config is None:
                    raise
            return deepcopy(self._cached_config)

    def _write_config_and_providers(self, config: Mapping[str, Any]) -> None:
        for filename, content in render_providers(config).items():
            _atomic_write(self.provider_dir / filename, content.encode("utf-8"), 0o644)
        serialized = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write(self.config_path, serialized, 0o600)
        self._cached_config = dict(config)
        self._cached_signature = self._signature(self.config_path)

    def is_slot_blocked(self, slot: int) -> bool:
        with self._lock:
            return slot in self._blocked_slots

    def recovery_slots(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._recovery_slots))

    def blocked_slots(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._blocked_slots))

    def _persist_blocked_slots(self) -> None:
        if not self._blocked_slots:
            try:
                self.blocked_path.unlink()
            except FileNotFoundError:
                pass
            return
        record = {
            "schema": 1,
            "slots": sorted(self._blocked_slots),
            "updated_at": utc_now(),
        }
        _atomic_write(
            self.blocked_path,
            (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            0o600,
        )

    def unblock_slots(self, slots: tuple[int, ...]) -> None:
        with self._lock:
            self._blocked_slots.difference_update(slots)
            self._recovery_slots.difference_update(slots)
            self._persist_blocked_slots()

    def retire_slots(self, slots: tuple[int, ...]) -> None:
        with self._lock:
            self._retired_slots.update(slots)

    def unretire_active_slots(self, config: Mapping[str, Any]) -> None:
        with self._lock:
            self._retired_slots.difference_update(
                int(device["slot"]) for device in config.get("devices", [])
            )

    def _create_backup(self, config: Mapping[str, Any]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_id = f"{stamp}-r{config['revision']}"
        destination = self.backup_dir / backup_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copy2(self.config_path, destination / "wloc.json")
        providers_destination = destination / "providers"
        providers_destination.mkdir()
        for filename, path in self._provider_paths().items():
            if path.exists():
                shutil.copy2(path, providers_destination / filename)
        metadata = {
            "id": backup_id,
            "created_at": utc_now(),
            "revision": config["revision"],
            "schema": config["schema"],
            "enabled_devices": len(enabled_device_cidrs(config)),
        }
        _atomic_write(
            destination / "metadata.json",
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            0o600,
        )
        return backup_id

    def save(
        self,
        raw: Mapping[str, Any],
        *,
        expected_revision: int,
        allow_slot_replacement: bool = False,
    ) -> tuple[dict[str, Any], str, tuple[int, ...]]:
        with self._lock:
            current = self.load()
            if expected_revision != current["revision"]:
                raise ConfigConflict(
                    f"configuration changed: expected revision {expected_revision}, "
                    f"current revision is {current['revision']}"
                )
            candidate = normalize_config(
                raw,
                revision=current["revision"] + 1,
                unavailable_slots=self._retired_slots,
            )
            candidate["updated_at"] = utc_now()
            current_by_id = {device["id"]: device for device in current["devices"]}
            candidate_by_id = {device["id"]: device for device in candidate["devices"]}
            if not allow_slot_replacement:
                retired_reuse = {
                    device["slot"]
                    for item_id, device in candidate_by_id.items()
                    if item_id not in current_by_id and device["slot"] in self._retired_slots
                }
                if retired_reuse:
                    raise ConfigError(
                        "a retired device slot cannot be reused until the WLOC container restarts"
                    )
            for item_id in current_by_id.keys() & candidate_by_id.keys():
                if current_by_id[item_id]["slot"] != candidate_by_id[item_id]["slot"]:
                    raise ConfigError("device slot is managed by the server and cannot be changed")
            if not allow_slot_replacement:
                removed_slots = {
                    device["slot"]
                    for item_id, device in current_by_id.items()
                    if item_id not in candidate_by_id
                }
                replaced_slots = {
                    device["slot"]
                    for item_id, device in candidate_by_id.items()
                    if item_id not in current_by_id and device["slot"] in removed_slots
                }
                if replaced_slots:
                    raise ConfigError(
                        "a removed device slot cannot be reused in the same save; "
                        "apply the removal before adding the replacement"
                    )
            changed_slots = changed_routing_slots(current, candidate)
            backup_id = self._create_backup(current)
            self._blocked_slots.update(changed_slots)
            try:
                self._persist_blocked_slots()
                self._write_config_and_providers(candidate)
            except Exception:
                self._blocked_slots.difference_update(changed_slots)
                try:
                    self._persist_blocked_slots()
                except OSError:
                    pass
                raise
            return deepcopy(candidate), backup_id, changed_slots

    def restore_backup(self, backup_id: str, *, expected_revision: int) -> dict[str, Any]:
        if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
            raise ConfigError("invalid backup id")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ConfigError("expected_revision must be a positive integer")
        with self._lock:
            current = self.load()
            if current["revision"] != expected_revision:
                raise ConfigConflict(
                    f"configuration changed: expected revision {expected_revision}, "
                    f"current revision is {current['revision']}"
                )
            source = self.backup_dir / backup_id / "wloc.json"
            if not source.is_file():
                raise ConfigError("backup does not exist")
            with source.open("r", encoding="utf-8") as handle:
                restored = normalize_config(json.load(handle))
            self._write_config_and_providers(restored)
            return deepcopy(restored)

    def rollback_latest(self) -> tuple[dict[str, Any], str, tuple[int, ...]]:
        with self._lock:
            backups = self.list_backups()
            if not backups:
                raise ConfigError("no backup is available")
            source = self.backup_dir / backups[0]["id"] / "wloc.json"
            with source.open("r", encoding="utf-8") as handle:
                previous = normalize_config(json.load(handle))
            current = self.load()
            previous["revision"] = current["revision"]
            return self.save(
                previous,
                expected_revision=current["revision"],
                allow_slot_replacement=True,
            )

    def mark_backup_failed(self, backup_id: str, error: str) -> None:
        if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
            return
        with self._lock:
            metadata_path = self.backup_dir / backup_id / "metadata.json"
            try:
                with metadata_path.open("r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                if metadata.get("id") != backup_id:
                    return
                metadata["failed_attempt"] = True
                metadata["failure"] = str(error)[:512]
                _atomic_write(
                    metadata_path,
                    (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                    0o600,
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return

    def list_backups(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.backup_dir.exists():
                return []
            backups: list[dict[str, Any]] = []
            for directory in sorted(self.backup_dir.iterdir(), reverse=True):
                metadata_path = directory / "metadata.json"
                if not directory.is_dir() or not metadata_path.is_file():
                    continue
                try:
                    with metadata_path.open("r", encoding="utf-8") as handle:
                        metadata = json.load(handle)
                    if metadata.get("id") == directory.name and not metadata.get("failed_attempt"):
                        backups.append(metadata)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            return backups
