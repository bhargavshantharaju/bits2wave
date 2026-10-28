"""Simulated-packet tests for the host-side radio frame codec; no RF access."""

import unittest

from backend.radio_protocol import (
    Frame, FrameError, MessageType, MAX_FRAME, MAX_PAYLOAD, decode, encode,
)


class RadioProtocolTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(
            MessageType.SOS, b"SOURCE01", b"DEST0001", b"M" * 16, 7,
            b"help", "INC-abc-123",
        )

    def test_round_trip_preserves_identity_and_payload(self):
        self.assertEqual(decode(encode(self.frame)), self.frame)

    def test_ack_round_trip_correlates_to_original_message(self):
        ack = Frame(MessageType.ACK, b"DEST0001", b"SOURCE01", b"A" * 16,
                    8, b"", "INC-abc-123", b"M" * 16)
        self.assertEqual(decode(encode(ack)), ack)

    def test_corruption_and_truncation_are_rejected(self):
        wire = encode(self.frame)
        damaged = bytearray(wire)
        damaged[-5] ^= 0x01
        for packet in (bytes(damaged), wire[:-1]):
            with self.assertRaises(FrameError):
                decode(packet)

    def test_extra_bytes_are_rejected(self):
        with self.assertRaises(FrameError):
            decode(encode(self.frame) + b"x")

    def test_payload_limit_is_strict(self):
        allowed = Frame(**{**self.frame.__dict__, "payload": b"x" * MAX_PAYLOAD})
        self.assertLessEqual(len(encode(allowed)), MAX_FRAME)
        too_large = Frame(**{**self.frame.__dict__, "payload": b"x" * (MAX_PAYLOAD + 1)})
        with self.assertRaises(FrameError):
            encode(too_large)

    def test_ack_requires_correlation_and_ids_have_fixed_width(self):
        with self.assertRaises(FrameError):
            encode(Frame(MessageType.ACK, b"SOURCE01", b"DEST0001", b"A" * 16, 1, b""))
        with self.assertRaises(FrameError):
            encode(Frame(MessageType.DATA, b"short", b"DEST0001", b"M" * 16, 1, b""))

    def test_unsupported_version_and_checksum_failures_are_rejected(self):
        wire = bytearray(encode(self.frame))
        wire[2] = 2
        import zlib
        wire[-4:] = (zlib.crc32(wire[:-4]) & 0xFFFFFFFF).to_bytes(4, "big")
        with self.assertRaises(FrameError):
            decode(bytes(wire))


if __name__ == "__main__":
    unittest.main()
