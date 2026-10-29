# Radio protocol status

The current Python codec is **protocol v1 test scaffolding**, not a safe radio protocol for untrusted nodes. It must not be used as a firmware contract or enabled for transmission.

## V1 wire layout (big endian)

Header format is `>2sBBB8s8s16sI16sBB`: magic (2), version (1), type (1), flags (1), source node (8), destination node (8), message ID (16), sequence (4), ACK correlation ID (16), incident-ID byte length (1), and payload byte length (1). Incident ID bytes and payload follow; a four-byte CRC32 terminates the frame. Variable fields are UTF-8 incident ID (maximum 40 bytes) and opaque payload (maximum 128 bytes). Maximum encoded size is `59 + 40 + 128 + 4 = 231` bytes. The declared frame cap is 240 bytes.

| Frame | Header | Incident ID | Payload | CRC | Maximum |
|---|---:|---:|---:|---:|---:|
| SOS / DATA | 59 | 40 | 128 | 4 | 231 bytes |
| ACK / RECEIPT without variable fields | 59 | 0 | 0 | 4 | 63 bytes |
| ACK / RECEIPT with incident and 128-byte payload | 59 | 40 | 128 | 4 | 231 bytes |

CRC detects accidental corruption only. It does not authenticate a sender, prevent replay, encrypt content, or establish ownership. Source and destination node IDs are caller-provided bytes and are not cryptographically verified. The current radio adapter uses these frames only with injected fake links in tests; application default remains simulated transport and transmission is gated off.

The current 250 ms adapter timeout is a software-test default and has no credible physical LoRa timing basis. It must be replaced only after data rate, spreading factor, coding rate, frame airtime, turnaround, duty-cycle/regulatory rules, and the chosen driver are specified and measured.

## Firmware gate

Do not write production firmware against v1. A later version needs a compact binary schema, authenticated node key IDs, a reviewed MAC or AEAD construction, persistent replay protection, durable sequence allocation, per-frame byte budget including authentication tags, key provisioning/rotation/revocation, and cross-language golden vectors. A 128-bit truncation of HMAC-SHA-256 is a candidate requiring review and threat-model approval; it is not implemented or selected here. CRC must not be described as authentication. There is no production key in source and no shared default key suitable for deployment.

The current Python header's theoretical byte count is not a link budget: regional frequency, legal power, antenna, airtime and packet loss are unresolved. No HAT pinout, frequency, transmit power, antenna or RF timing is specified by this document.

## Phase 2H ownership impact (not a v2 wire definition)

Incident recovery uses a 32-byte random browser capability. Only SHA-256(capability) is carried by local synchronization; the raw capability must not appear in a radio frame. Fallback ownership HMAC-SHA-256 covers incident ID, stable citizen UUID, issuer, primary audience, source node and the capability commitment. Compact binary ownership evidence therefore costs 32 bytes for the commitment plus 32 bytes for the MAC (64 bytes total), before node/message identifiers, priority, expiry, framing or any separate packet authentication. Current local JSON sync represents the MAC as 64 ASCII hexadecimal characters. This extra ownership payload cannot be assumed to fit the existing v1 budget. Protocol v2 remains unfrozen until a reviewed authenticated frame design and byte-level vectors include these fields.
