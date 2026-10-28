# ESP32-S3 / SX1262 integration scaffold

This is an interface contract only. The owned ESP32-S3 carrier is unidentified,
the SX1262 is not yet attached to it, and no PlatformIO board target, GPIO map,
radio configuration or RF operation has been selected. This directory is not a
buildable firmware target and contains no driver implementation.

`radio_config_guard.hpp` intentionally fails compilation until the board/revision,
SPI and control-pin map, antenna, lawful RF settings and explicit authorization
are supplied by a reviewed board-specific configuration. It contains no example
values. Never copy settings from another SX1262 carrier or from the Pi HAT.

`radio_port.hpp` defines the future driver boundary: initialize, connection state,
send result and receive. A RadioLib adapter may implement it after identifying the
actual carrier and verifying its RF switch/TCXO and pin wiring. The transport-side
host reference is `backend/radio_transport.py`; the wire codec remains
`backend/radio_protocol.py` v1. Firmware must match the host codec exactly and
pass shared golden-vector tests before connecting to a live NexAlert queue.

Required firmware behavior before a board build can be called ready:

- Fixed node identity and peer allowlist.
- Bounded send/receive queues and bounded retry/backoff.
- Stable message/incident IDs across retries and durable duplicate suppression.
- Separate TX-done, link ACK, application ACK and persistent receipt events.
- No receipt claim from local enqueue or serial write completion.
- Clear initialization, disconnect, malformed-frame and queue-full diagnostics.
- Authentication/encryption extension; CRC alone is neither.

No firmware build or radio initialization is claimed. No RF transmission has
occurred.
