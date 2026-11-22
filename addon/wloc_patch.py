"""Fail-open Apple WLOC protobuf response patcher.

The codec deliberately has no protobuf runtime dependency. Unknown fields are
preserved byte-for-byte. Only nested location fields are replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import re
from typing import Union


APPLE_WLOC_MARKER = b"\x00\x00\x00\x01\x00\x00"
COORD_SCALE = 100_000_000
BSSID_RE = re.compile(rb"^[0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5}$")


class PatchError(ValueError):
    """Raised when a body cannot be identified and safely patched."""


@dataclass(frozen=True)
class TargetLocation:
    latitude: float
    longitude: float
    accuracy: int

    def validate(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise PatchError("latitude must be between -90 and 90")
        if not -180 <= self.longitude <= 180:
            raise PatchError("longitude must be between -180 and 180")
        if not 1 <= self.accuracy <= 100_000:
            raise PatchError("accuracy must be between 1 and 100000")


@dataclass(frozen=True)
class ProtoField:
    field_no: int
    wire_type: int
    value: Union[int, bytes]
    raw: bytes


@dataclass
class PatchStats:
    wifi: int = 0
    cell: int = 0
    locations: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class PatchResult:
    data: bytes
    kind: str
    stats: PatchStats


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    start = offset
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift >= 70:
            raise PatchError(f"varint too long at {start}")
    raise PatchError(f"truncated varint at {start}")


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_field(field_no: int, wire_type: int, value: Union[int, bytes]) -> bytes:
    tag = encode_varint(field_no * 8 + wire_type)
    if wire_type == 0:
        if not isinstance(value, int):
            raise PatchError("varint field expects int")
        return tag + encode_varint(value)
    if wire_type == 1:
        if not isinstance(value, bytes) or len(value) != 8:
            raise PatchError("fixed64 field expects 8 bytes")
        return tag + value
    if wire_type == 2:
        if not isinstance(value, bytes):
            raise PatchError("length-delimited field expects bytes")
        return tag + encode_varint(len(value)) + value
    if wire_type == 5:
        if not isinstance(value, bytes) or len(value) != 4:
            raise PatchError("fixed32 field expects 4 bytes")
        return tag + value
    raise PatchError(f"unsupported wire type {wire_type}")


def parse_message(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    offset = 0
    while offset < len(data):
        start = offset
        key, offset = read_varint(data, offset)
        field_no = key >> 3
        wire_type = key & 7
        if field_no == 0:
            raise PatchError(f"invalid protobuf field 0 at {start}")

        value: Union[int, bytes]
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise PatchError(f"truncated fixed64 at {start}")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            if length < 0 or offset + length > len(data):
                raise PatchError(f"truncated length-delimited field at {start}")
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise PatchError(f"truncated fixed32 at {start}")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise PatchError(f"unsupported wire type {wire_type} at {start}")

        fields.append(ProtoField(field_no, wire_type, value, data[start:offset]))
    return fields


def _looks_like_location(data: bytes) -> bool:
    try:
        fields = parse_message(data)
    except PatchError:
        return False
    return any(f.field_no == 1 and f.wire_type == 0 for f in fields) and any(
        f.field_no == 2 and f.wire_type == 0 for f in fields
    )


def _patch_location(data: bytes, target: TargetLocation, stats: PatchStats) -> bytes:
    fields = parse_message(data) if data else []
    preserved = [f.raw for f in fields if f.field_no not in (1, 2, 3)]
    preserved.extend(
        (
            encode_field(1, 0, int(target.latitude * COORD_SCALE)),
            encode_field(2, 0, int(target.longitude * COORD_SCALE)),
            encode_field(3, 0, target.accuracy),
        )
    )
    stats.locations += 1
    return b"".join(preserved)


def _patch_wifi_entry(data: bytes, target: TargetLocation, stats: PatchStats) -> bytes:
    try:
        fields = parse_message(data)
    except PatchError:
        stats.skipped += 1
        return data

    valid_bssid = any(
        f.field_no == 1
        and f.wire_type == 2
        and isinstance(f.value, bytes)
        and BSSID_RE.fullmatch(f.value)
        for f in fields
    )
    location_fields = [
        f
        for f in fields
        if f.field_no == 2
        and f.wire_type == 2
        and isinstance(f.value, bytes)
        and _looks_like_location(f.value)
    ]
    if not valid_bssid and not location_fields:
        return data

    changed = False
    out: list[bytes] = []
    for field in fields:
        if field in location_fields and isinstance(field.value, bytes):
            out.append(encode_field(2, 2, _patch_location(field.value, target, stats)))
            changed = True
        else:
            out.append(field.raw)
    if valid_bssid and not location_fields:
        out.append(encode_field(2, 2, _patch_location(b"", target, stats)))
        changed = True
    if changed:
        stats.wifi += 1
        return b"".join(out)
    return data


def _patch_cell_entry(data: bytes, target: TargetLocation, stats: PatchStats) -> bytes:
    try:
        fields = parse_message(data)
    except PatchError:
        stats.skipped += 1
        return data
    changed = False
    out: list[bytes] = []
    for field in fields:
        if (
            field.field_no == 5
            and field.wire_type == 2
            and isinstance(field.value, bytes)
            and _looks_like_location(field.value)
        ):
            out.append(encode_field(5, 2, _patch_location(field.value, target, stats)))
            changed = True
        else:
            out.append(field.raw)
    if changed:
        stats.cell += 1
        return b"".join(out)
    return data


def patch_root_message(data: bytes, target: TargetLocation, stats: PatchStats) -> bytes:
    fields = parse_message(data)
    out: list[bytes] = []
    for field in fields:
        if field.wire_type == 2 and isinstance(field.value, bytes):
            if field.field_no == 2:
                patched = _patch_wifi_entry(field.value, target, stats)
                out.append(encode_field(2, 2, patched) if patched != field.value else field.raw)
                continue
            if field.field_no in (22, 24):
                patched = _patch_cell_entry(field.value, target, stats)
                out.append(
                    encode_field(field.field_no, 2, patched)
                    if patched != field.value
                    else field.raw
                )
                continue
        out.append(field.raw)
    return b"".join(out)


def _patch_payload(payload: bytes, target: TargetLocation) -> tuple[bytes, PatchStats]:
    stats = PatchStats()
    patched = patch_root_message(payload, target, stats)
    if stats.locations <= 0 or patched == payload:
        raise PatchError("protobuf parsed but contained no patchable locations")
    return patched, stats


def _try_u16_frame(body: bytes, header_offset: int, target: TargetLocation) -> PatchResult:
    length_offset = header_offset + 8
    if length_offset + 2 > len(body):
        raise PatchError("frame header truncated")
    length = int.from_bytes(body[length_offset : length_offset + 2], "big")
    payload_offset = length_offset + 2
    if length <= 0 or payload_offset + length > len(body):
        raise PatchError("invalid frame length")
    patched, stats = _patch_payload(body[payload_offset : payload_offset + length], target)
    if len(patched) > 0xFFFF:
        raise PatchError("patched payload exceeds uint16 frame")
    data = (
        body[:length_offset]
        + len(patched).to_bytes(2, "big")
        + patched
        + body[payload_offset + length :]
    )
    return PatchResult(data, f"frame@{header_offset}", stats)


def _try_arpc(body: bytes, target: TargetLocation) -> PatchResult:
    if len(body) < 16:
        raise PatchError("ARPC body too short")
    offset = 2
    for _ in range(3):
        if offset + 2 > len(body):
            raise PatchError("ARPC Pascal string truncated")
        length = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2 + length
        if offset > len(body):
            raise PatchError("ARPC Pascal string exceeds body")
    if offset + 8 > len(body):
        raise PatchError("ARPC trailer truncated")
    length_offset = offset + 4
    length = int.from_bytes(body[length_offset : length_offset + 4], "big")
    payload_offset = length_offset + 4
    if length <= 0 or payload_offset + length > len(body):
        raise PatchError("invalid ARPC payload length")
    patched, stats = _patch_payload(body[payload_offset : payload_offset + length], target)
    data = (
        body[:length_offset]
        + len(patched).to_bytes(4, "big")
        + patched
        + body[payload_offset + length :]
    )
    return PatchResult(data, "arpc", stats)


def _try_marker(body: bytes, target: TargetLocation) -> PatchResult:
    start = 0
    while True:
        marker_offset = body.find(APPLE_WLOC_MARKER, start)
        if marker_offset < 0:
            raise PatchError("WLOC marker not found")
        length_offset = marker_offset + len(APPLE_WLOC_MARKER)
        if length_offset + 2 <= len(body):
            length = int.from_bytes(body[length_offset : length_offset + 2], "big")
            payload_offset = length_offset + 2
            if 0 < length <= len(body) - payload_offset:
                try:
                    patched, stats = _patch_payload(
                        body[payload_offset : payload_offset + length], target
                    )
                    if len(patched) > 0xFFFF:
                        raise PatchError("patched marker payload exceeds uint16")
                    data = (
                        body[:length_offset]
                        + len(patched).to_bytes(2, "big")
                        + patched
                        + body[payload_offset + length :]
                    )
                    return PatchResult(data, "marker", stats)
                except PatchError:
                    pass
        start = marker_offset + 1


def _patch_uncompressed(body: bytes, target: TargetLocation) -> PatchResult:
    target.validate()
    if len(body) < 2:
        raise PatchError("body too short")

    attempts = []
    if len(body) >= 10 and body[:2] == b"\x00\x01":
        attempts.append(lambda: _try_u16_frame(body, 0, target))
    attempts.extend((lambda: _try_arpc(body, target), lambda: _try_marker(body, target)))
    for attempt in attempts:
        try:
            return attempt()
        except PatchError:
            pass

    max_offset = min(96, max(0, len(body) - 10))
    preferred = [0, 2, 4, 6, 8, 10, 12, 14, 16]
    offsets = preferred + [i for i in range(max_offset + 1) if i not in preferred]
    for offset in offsets:
        try:
            return _try_u16_frame(body, offset, target)
        except PatchError:
            pass

    try:
        patched, stats = _patch_payload(body, target)
        return PatchResult(patched, "bare", stats)
    except PatchError:
        pass

    for offset in range(min(256, len(body)) + 1):
        try:
            patched, stats = _patch_payload(body[offset:], target)
            return PatchResult(body[:offset] + patched, f"raw@{offset}", stats)
        except PatchError:
            pass
    raise PatchError("no safely patchable WLOC payload found")


def patch_wloc_body(body: bytes, target: TargetLocation) -> PatchResult:
    """Patch a WLOC response, preserving body-level gzip if present."""
    if body[:2] == b"\x1f\x8b":
        try:
            inflated = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise PatchError(f"invalid gzip body: {exc}") from exc
        result = _patch_uncompressed(inflated, target)
        return PatchResult(gzip.compress(result.data, mtime=0), f"gzip/{result.kind}", result.stats)
    return _patch_uncompressed(body, target)
