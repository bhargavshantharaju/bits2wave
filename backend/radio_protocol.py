"""Hardware-independent NexAlert radio frame codec (no radio I/O).

This is host-side protocol scaffolding only. It deliberately has no frequency,
power, GPIO, antenna, or hardware configuration and cannot transmit packets.
"""

from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib


MAGIC = b"NX"
VERSION = 1
MAX_PAYLOAD = 128
MAX_INCIDENT_ID = 40
MAX_FRAME = 240
_HEADER = struct.Struct(">2sBBB8s8s16sI16sBB")
_CRC = struct.Struct(">I")
_NO_ACK = b"\x00" * 16


class FrameError(ValueError):
    """A frame is malformed, unsupported, or exceeds protocol limits."""


class MessageType(IntEnum):
    SOS = 1
    DATA = 2
    ACK = 3
    RECEIPT = 4


@dataclass(frozen=True)
class Frame:
    message_type: MessageType
    source: bytes
    destination: bytes
    message_id: bytes
    sequence: int
    payload: bytes
    incident_id: str = ""
    ack_for: bytes | None = None


def _fixed_bytes(value: bytes, size: int, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != size:
        raise FrameError(f"{name} must be exactly {size} bytes")
    return value


def encode(frame: Frame) -> bytes:
    """Serialize one v1 frame; CRC32 detects corruption, not forgery."""
    try:
        message_type = MessageType(frame.message_type)
    except (TypeError, ValueError) as exc:
        raise FrameError("unknown message type") from exc
    source = _fixed_bytes(frame.source, 8, "source")
    destination = _fixed_bytes(frame.destination, 8, "destination")
    message_id = _fixed_bytes(frame.message_id, 16, "message_id")
    if isinstance(frame.sequence, bool) or not isinstance(frame.sequence, int) or not 0 <= frame.sequence <= 0xFFFFFFFF:
        raise FrameError("sequence must be an unsigned 32-bit integer")
    if not isinstance(frame.payload, bytes) or len(frame.payload) > MAX_PAYLOAD:
        raise FrameError(f"payload must be bytes of at most {MAX_PAYLOAD} bytes")
    if not isinstance(frame.incident_id, str):
        raise FrameError("incident_id must be text")
    incident = frame.incident_id.encode("utf-8")
    if len(incident) > MAX_INCIDENT_ID:
        raise FrameError(f"incident_id must encode to at most {MAX_INCIDENT_ID} bytes")
    ack_for = _NO_ACK if frame.ack_for is None else _fixed_bytes(frame.ack_for, 16, "ack_for")
    if message_type == MessageType.ACK and ack_for == _NO_ACK:
        raise FrameError("ACK must correlate to a message_id")
    header = _HEADER.pack(
        MAGIC, VERSION, int(message_type), int(ack_for != _NO_ACK), source,
        destination, message_id, frame.sequence, ack_for, len(incident), len(frame.payload),
    )
    body = header + incident + frame.payload
    encoded = body + _CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)
    if len(encoded) > MAX_FRAME:
        raise FrameError(f"frame exceeds {MAX_FRAME} bytes")
    return encoded


def decode(packet: bytes) -> Frame:
    """Validate and parse one complete v1 frame; rejects truncation/trailing bytes."""
    if not isinstance(packet, bytes) or len(packet) < _HEADER.size + _CRC.size:
        raise FrameError("packet is shorter than the minimum frame")
    if len(packet) > MAX_FRAME:
        raise FrameError(f"frame exceeds {MAX_FRAME} bytes")
    fields = _HEADER.unpack(packet[:_HEADER.size])
    magic, version, raw_type, flags, source, destination, message_id, sequence, ack_for, inc_len, pay_len = fields
    if magic != MAGIC:
        raise FrameError("invalid frame magic")
    if version != VERSION:
        raise FrameError(f"unsupported protocol version: {version}")
    if flags & ~1:
        raise FrameError("reserved flags must be zero")
    if inc_len > MAX_INCIDENT_ID or pay_len > MAX_PAYLOAD:
        raise FrameError("declared field length exceeds protocol limit")
    expected = _HEADER.size + inc_len + pay_len + _CRC.size
    if len(packet) != expected:
        raise FrameError("packet length does not match declared fields")
    body, crc_bytes = packet[:-_CRC.size], packet[-_CRC.size:]
    if zlib.crc32(body) & 0xFFFFFFFF != _CRC.unpack(crc_bytes)[0]:
        raise FrameError("CRC mismatch")
    try:
        message_type = MessageType(raw_type)
        incident_id = packet[_HEADER.size:_HEADER.size + inc_len].decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError("invalid message type or incident ID encoding") from exc
    has_ack = bool(flags & 1)
    if has_ack != (ack_for != _NO_ACK):
        raise FrameError("acknowledgement flag and correlation field disagree")
    if message_type == MessageType.ACK and not has_ack:
        raise FrameError("ACK must correlate to a message_id")
    payload_start = _HEADER.size + inc_len
    payload = packet[payload_start:payload_start + pay_len]
    return Frame(message_type, source, destination, message_id, sequence, payload,
                 incident_id, ack_for if has_ack else None)
