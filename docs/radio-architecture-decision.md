# NexAlert Phase 2C radio architecture decision

Updated 2026-10-28 after source-level review. This is an architecture choice for the next proof, **not physical validation**. No hardware was flashed, configured or used for RF transmission.

## Decision

**Select a custom, direct two-node NexAlert packet link for the first SOS proof, using maintained SX126x drivers and NexAlert's existing frame codec. Keep the implementation conditional on exact board identification and WPC/DoT authorization. Do not select Meshtastic or MeshCore as the first gateway stack for the currently owned Pi HAT.**

This is a scoped decision: prove one incident from radio endpoint to the existing Flask receiver, including correlated retry, duplicate suppression and persistent command-centre receipt. It is not a production mesh decision. The HAT is only *likely* a Waveshare SX1262 XXXM Pi HAT. Its exact SKU/revision, radio band, antenna and Pi 5 compatibility remain unverified; therefore firmware or driver settings must not be selected yet.

The smallest plausible path is the Pi HAT using its vendor host driver plus a **supported, complete ESP32-S3/SX1262 endpoint** using RadioLib. The likely lower-risk second endpoint is the Seeed XIAO ESP32-S3 & Wio-SX1262 kit (SKU 102010611), but only after its exact SKU/band and antenna are checked against the HAT. The owner's unidentified ESP32-S3-N16R8 carrier is not yet a known radio endpoint. No purchase is authorized or recommended before HAT identification.

### Responsibility split for the proof

| Responsibility | Component |
|---|---|
| SX1262 on Pi | Candidate Waveshare `LoRaRF` host driver after the exact HAT revision and Pi OS are confirmed; Pi 5 operation is not yet proven. |
| SX1262 on ESP32-S3 | RadioLib behind the firmware `RadioPort`; exact target pin/RF-switch/TCXO settings must come from the selected board documentation. |
| Packet framing and integrity | NexAlert v1 codec in `backend/radio_protocol.py`; C++ firmware must pass matching golden vectors. CRC detects corruption only. |
| Node addressing | NexAlert's fixed 8-byte source/destination IDs in the frame; the adapter currently filters configured peer IDs. |
| Retries / application ACK | `RadioSosTransport` with a bounded attempt count/backoff and identical message bytes/IDs. A driver `TX-done` or optional hop ACK is not completion. |
| Persistent command-centre receipt | Existing command-centre service/database callback. It must commit incident and receipt before the adapter emits ACK/RECEIPT; sender outbox is marked delivered only on a correlated receipt frame. |
| Multi-hop | None in the selected proof. Future MeshCore/Meshtastic or a NexAlert relay layer, after point-to-point is repeatable. |

## Repository contracts inspected

- [`backend/app.py`](../backend/app.py) defines `SosTransport` and the tested `SimulatedTransport`. No physical transport adapter exists.
- [`backend/radio_protocol.py`](../backend/radio_protocol.py) defines NexAlert frame v1: type, source/destination, stable 16-byte message ID, sequence, optional incident ID, correlated ACK, strict lengths and CRC32. It is a host codec only. CRC is not sender authentication or confidentiality.
- [`backend/failover.py`](../backend/failover.py) provides host-side failover state and a separate persistent SQLite fallback outbox. It is not radio firmware.
- [`backend/radio_transport.py`](../backend/radio_transport.py) now implements a hardware-independent `SosTransport` adapter. It injects a radio link and durable store callbacks, defaults transmission authorization to false, and never opens SPI/serial itself.
- Existing tests cover the simulated transport, persistent command-centre receipt, lost-ACK idempotency, frame parsing and failover. They remain the regression baseline.

The adapter boundary should keep four events separate: radio driver TX-done; radio/stack hop ACK; NexAlert application ACK; and command-centre receipt returned only after the existing receiver commits the same incident ID. On reconnect/retry the outbox must retain the incident ID and message ID, and the destination must be idempotent.

## Source-level candidate findings

### Meshtastic

**Pi HAT:** `meshtasticd` is a Linux-native daemon, not a Pi firmware target. Its official hardware guide recognizes SPI/USB radio configurations but specifically warns that the Waveshare SX1262 LoRaWAN HAT is *not recommended for deployment* because known limitations can affect longer messages, and suggests `CLIENT_MUTE` if it is used. This is a direct qualification risk for a NexAlert frame plus metadata/receipt. The exact HAT SKU/revision and Pi 5 combination are not documented as verified. The family HAT uses SPI plus board-specific BUSY, RESET, DIO1/IRQ and RF-switch wiring; the published Waveshare pin map is not an official Meshtastic confirmation for the photographed board.

**ESP32 endpoint:** Meshtastic's supported-device list includes Seeed XIAO ESP32-S3 & Wio-SX1262. The exact owner ESP32-S3-N16R8 carrier is not a target because its carrier/pins/radio are unknown. For a Meshtastic-only comparison, use two identical supported kits; the Pi HAT plus one kit is an experimental mixed-hardware test, not an equivalent benchmark.

**Host API and semantics:** the official Python client connects to a node/daemon over supported serial/TCP interfaces. Its `MeshInterface.sendData()` accepts byte arrays, offers `wantAck`, hop-limit, encryption and priority parameters; the firmware/protobuf data payload limit is 233 bytes. This is sufficient for many compact NexAlert frames, but real end-to-end sizing/fragmentation still needs testing. A Flask adapter must map records, reconnect, maintain the durable retry queue, retain incident/message IDs and implement a receipt after SQLite commit. Meshtastic packet ACK is not that durable receipt. Security/channel encryption is a network feature; it does not replace NexAlert destination idempotency or receipt semantics.

**Verdict:** do not make it the first route through the likely HAT. It remains a useful later network baseline on two supported identical nodes if multi-hop is being compared. Firmware repository is GPL-3.0; distributing a modified firmware build invokes GPL source/license obligations. The Python client has its own repository license; check the exact pinned release before bundling it.

### MeshCore

**ESP32 endpoint:** upstream has a dedicated [`variants/xiao_s3_wio`](https://github.com/meshcore-dev/MeshCore/tree/main/variants/xiao_s3_wio) target for the Seeed XIAO ESP32-S3/Wio-SX1262 hardware family, including target-specific radio setup. That is a stronger match than an unidentified ESP32 carrier, but it does not identify the owner's radio board or prove the matching band/antenna. The upstream [`platformio.ini`](https://github.com/meshcore-dev/MeshCore/blob/main/platformio.ini) pins RadioLib and lists embedded build targets; the owned ESP32 carrier itself is not an upstream target.

**Pi HAT:** no documented MeshCore Linux host-radio target for this Waveshare Pi HAT was found. The practical MeshCore path is a supported ESP32 companion/repeater connected to the Pi over USB serial, which means the Pi HAT is not the MeshCore radio endpoint. Both endpoints could be compatible if the Pi-side radio is also a supported ESP32/SX1262 board; the current Pi HAT cannot be assumed to run MeshCore firmware.

**KISS vs mesh:** [`examples/kiss_modem`](https://github.com/meshcore-dev/MeshCore/tree/main/examples/kiss_modem) and [`docs/kiss_modem_protocol.md`](https://github.com/meshcore-dev/MeshCore/blob/main/docs/kiss_modem_protocol.md) expose a 115200 8N1 serial modem boundary, packet input/output, TX-done events, radio configuration and telemetry. The current documented data frame ceiling is 255 bytes, and the modem permits one outstanding TX at a time. The KISS modem is a raw packet modem; it does not itself perform MeshCore routing, store-and-forward application delivery, NexAlert identity, durable receipt, retry policy or SOS priority. The [`simple_repeater` example](https://github.com/meshcore-dev/MeshCore/tree/main/examples/simple_repeater) is a separate role that runs MeshCore routing and persists its identity/state on device storage. A raw NexAlert frame through KISS can be explored only after confirming the modem's packet contract; using a routing repeater requires MeshCore packet/application integration rather than pretending KISS provides mesh routing.

The Companion protocol offers a host API for MeshCore companion firmware; it is a separate binary protocol from KISS. Upstream `meshcore_py` supports serial connections, direct messages, ACK events, retries and a binary request API, but does not provide NexAlert's durable outbox/receipt transaction. The KISS path exposes raw packets and TX-done, while any NexAlert application ACK and durable receipt remain ours. MeshCore offers authenticated/encrypted network messaging primitives; NexAlert must independently persist incidents and issue its receipt after SQLite commit. MeshCore upstream states MIT licensing; retain notices. Client repositories may have separate licenses.

**Verdict:** compelling later candidate for embedded multi-hop once supported ESP32 nodes are available. Not the fastest path through the present Pi HAT; KISS mode would only simplify a raw-modem link, not deliver the MeshCore mesh capabilities.

### Custom NexAlert protocol with RadioLib / vendor driver

RadioLib upstream supports SX126x, ESP32 and a HAL model for ports including Raspberry Pi, but this does **not** prove that RadioLib has a ready-to-use Raspberry Pi driver profile for the Waveshare HAT. Its Pi guidance requires a platform HAL implementing GPIO and SPI. In contrast, Waveshare's HAT archive includes a Pi-oriented `LoRaRF` example/driver; the inspected continuous-receive example calls `begin()`, sets DIO2 RF-switch behavior and enters RX, but hard-codes 868 MHz and has ineffective cleanup after the infinite loop. It is a source reference, not a ready-to-run initializer. The Waveshare driver is the more direct Pi candidate after board revision matching; RadioLib is the maintained ESP32 candidate.

The existing codec supplies message/incident identity, sequence, ACK correlation and corruption detection. NexAlert still implements the radio adapter, bounded retry/backoff, authenticated application frames, destination deduplication, and receipt-after-commit. The current frame CRC is not cryptographic authentication; add a keyed MAC/signature and encryption design before sensitive deployment. Point-to-point has no automatic mesh route or relay/store-and-forward. These should be added only after the radio proof and with explicit queue/TTL/duplicate rules.

RadioLib is MIT licensed. Waveshare demo code carries its own terms and dependencies; review their license before redistribution. Driver and protocol do not select lawful RF settings.

**Verdict:** best first path for the existing Pi-centered SOS service, because it keeps NexAlert's ID/ACK/receipt model intact and avoids a gateway protocol translation. It has the highest NexAlert firmware responsibility and remains conditional on the Pi HAT being identified and operable.

The Phase 2D fake-link acceptance path now injects the adapter into the real Flask SOS route in tests, reads/writes the application's temporary sender/outbox database, and uses the shared command-centre persistence and receipt helpers. Normal application startup still selects `SimulatedTransport`. There is no concrete serial/SPI backend; fake-link acceptance does not prove an SX1262 driver, target firmware, Pi HAT or RF path works.

### Hybrid

The useful hybrid later is **a maintained radio/network stack for radio and routing, NexAlert for durable incident semantics**. However, there is no established MeshCore or Meshtastic runtime for the current Pi HAT that makes this an immediate drop-in. MeshCore Companion/KISS or Meshtastic Python can be a host boundary only after selecting supported radio endpoints and validating payload/ACK mapping. Do not add a hybrid layer to the first proof: it adds protocol translation while the actual HAT remains unknown.

## Quantitative matrix

Scores are 0–5: 5 means strong documented fit for this project and the current endpoint arrangement; 0 means absent; **U** means evidence is insufficient to score. A higher integration-effort score means less effort. Scores are engineering estimates from the source findings above, not measured performance. Cost is an estimate for the missing radio endpoint only and excludes antenna/accessories unless stated.

| Criterion | Meshtastic: Pi HAT + supported ESP32-S3 kit | MeshCore: current Pi HAT + ESP32-S3 kit | Custom direct link: Pi HAT + ESP32-S3/SX1262 | Hybrid stack + NexAlert semantics |
|---|---:|---:|---:|---:|
| Exact hardware compatibility | 1 | 1 | 2 | 1 |
| Time to first real packet | 2 | 2 | 3 | 1 |
| Bidirectional communication | 4 | 4 | 4 | 4 |
| ACK and retry control for NexAlert | 2 | 2 | 5 | 3 |
| Multi-hop forwarding | 5 | 5 | 0 | 4 |
| Store-and-forward support | 2 | 3 | 1 | 3 |
| Application receipt integration | 2 | 2 | 5 | 3 |
| Message priority control | 2 | 2 | 5 | 3 |
| Security/authentication extension | 3 | 4 | 2 | 4 |
| Flask integration effort | 3 | 2 | 4 | 2 |
| Maintenance burden (higher is easier) | 3 | 3 | 1 | 2 |
| Licensing compatibility (higher is easier) | 2 | 5 | 5 | 4 |
| Estimated extra component cost | 3 | 2 | 4 | 1 |

**Interpretation and evidence limits:** Meshtastic's multi-hop score reflects its network function, not verified use of this HAT; its explicit Waveshare warning drives the low hardware/time scores. MeshCore has upstream XIAO S3 Wio targets and routing source, but not a documented Linux Waveshare endpoint. Its KISS radio role is not a repeater. Custom gets the strongest incident semantics because NexAlert already owns the frame and receiver contract, but lowest maintenance/multi-hop because NexAlert must implement them. Hybrid inherits dependency and endpoint mismatch costs. Hardware compatibility scores remain low until board labels and band are verified. Supplier price is not scored as a fixed cost: previously checked listings for the Seeed Wio kit were ₹1,675–₹1,736 on 2026-10-28, but exact stock/band/antenna contents must be reconfirmed; no current binding quote is established.

### Required missing endpoint

Preferred endpoint **class**: an ESP32-S3 board with a documented SX1262 integration, exact upstream firmware target, accessible USB serial, known SPI/IRQ/BUSY/RESET/RF-switch wiring and a matching LoRa-band antenna/connector. The Seeed XIAO ESP32-S3 & Wio-SX1262 kit is a candidate for both Meshtastic and MeshCore evaluations; upstream MeshCore has `xiao_s3_wio`, while Meshtastic lists the kit. Do not assume the likely Waveshare band is compatible until its label is read and the kit SKU/antenna are verified. A second identical Waveshare HAT is another custom-link option but adds an entire Pi host or a separate MCU/SPI host and is not the minimum-cost endpoint if the current ESP32 can host the other radio.

The assessed retail listings were ₹1,675 and ₹1,736 for the Seeed kit on 2026-10-28. They are evidence of an order-of-magnitude fit inside ₹5,000–₹15,000, not a purchase quote. The minimum missing information before deciding is: HAT SKU/revision and band; kit SKU/band and antenna inclusions; matching connector/antenna; and an applicable WPC/DoT operating path.

## Two-node proof and promotion gates

1. Identify and photograph HAT front/back labels, exact SKU/revision, ESP32 carrier/radio model, both antenna part numbers/connectors, and kit band. **Blocked until inspected.**
2. Obtain authoritative WPC/DoT confirmation for the exact devices, antenna, intended configuration and test conditions. No settings are specified here. **Blocked until confirmed.**
3. On the actual Pi, run read-only OS/SPI/GPIO/package diagnostics. Match the HAT revision to its schematic and review the exact receive-only vendor source; ensure cleanup executes in `finally`. **Receive-only check can proceed after step 1; no configuration changes in this plan.**
4. Build host-only codec/adapter tests and temporary-DB integration tests first. Keep `SimulatedTransport` as a separate explicit backend selection.
5. Once steps 1–2 are complete, select only a documented, compliant common PHY configuration and matched antennas. **No frequency, power, bandwidth, spreading factor or duty cycle is selected by this document.**
6. With authorization and both antennas correctly installed, send a short test packet A→B; validate frame/version/IDs/CRC; send a correlated B→A ACK.
7. Intentionally discard the first application ACK. Retry the exact same message ID/incident ID; confirm receiver deduplication and one persisted incident.
8. Submit a test SOS through the physical adapter implementing `SosTransport`; verify durable sender outbox, radio TX-done, hop ACK if applicable, application ACK, destination SQLite commit, then command-centre receipt as distinct logged events.
9. Measure packet delivery ratio, one-way/round-trip latency, retry count, duplicate count, queue survival across restart, RSSI/SNR only if genuine adapter measurements exist, and power. Repeat at a documented test distance/environment without exceeding the approved conditions.
10. Run the existing simulated transport and full regression suite unchanged. Compare Meshtastic on two identical supported nodes and MeshCore on two compatible supported nodes only as separate later trials; do not conflate their results with the custom mixed-endpoint proof.

**Advance from two-node proof to multi-hop:** require repeatable acknowledged SOS delivery and persistent destination receipt across retry/lost ACK/restart; measured performance and power within project targets; an authenticated frame/peer model; bounded retry/airtime; no duplicate incidents; and a routing/forwarding design tested with at least one relay. Multi-hop must pass partition, duplicate, stale-route, queue exhaustion and recovery tests before implementation is considered for the field.

**Advance from multi-hop to physical master failover:** additionally require real heartbeat transport on independent primary/backup power paths, a tested partition/split-brain safety policy, backup-local incident persistence, reconnect reconciliation without duplication, and repeated cold-start/power-off bench trials. Host failover tests do not satisfy this gate.

## Licence and source references

- Meshtastic: [firmware](https://github.com/meshtastic/firmware) (GPL-3.0), [Python client](https://github.com/meshtastic/python), [meshtasticd overview](https://meshtastic.org/docs/meshtasticd/), [Linux hardware guidance](https://meshtastic.org/docs/meshtasticd/hardware/), [supported devices](https://meshtastic.org/docs/hardware/devices/).
- Meshtastic source details: [`MeshInterface.sendData()`](https://github.com/meshtastic/python/blob/master/meshtastic/mesh_interface.py) for byte payload, `wantAck`, priority and hop limit; [`DATA_PAYLOAD_LEN`](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto) for the 233-byte application payload constant.
- MeshCore: [upstream](https://github.com/meshcore-dev/MeshCore) (MIT per upstream README), [`xiao_s3_wio` target](https://github.com/meshcore-dev/MeshCore/tree/main/variants/xiao_s3_wio), [KISS modem source](https://github.com/meshcore-dev/MeshCore/tree/main/examples/kiss_modem), [KISS protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/kiss_modem_protocol.md), [simple repeater](https://github.com/meshcore-dev/MeshCore/tree/main/examples/simple_repeater), [packet format](https://github.com/meshcore-dev/MeshCore/blob/main/docs/packet_format.md), [Python companion client](https://github.com/meshcore-dev/meshcore_py) (`send_raw_packet`, binary request, ACK/retry APIs).
- Custom driver: [RadioLib](https://github.com/jgromes/RadioLib) (MIT; SX126x and ESP32 support), [RadioLib non-Arduino/HAL guidance](https://github.com/jgromes/RadioLib/wiki/Porting-to-non-Arduino-Platforms), [Waveshare HAT documentation](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT), [Waveshare schematic](https://files.waveshare.com/wiki/SX1262-XXXM-LoRaWAN-GNSS-HAT/SX1262_XXXM_LoRaWAN_HAT_sch.pdf).
- Endpoint reference: [Seeed kit specification](https://files.seeedstudio.com/Bazaar/product_pdf/102010611.pdf), [Wio-SX1262 datasheet](https://files.seeedstudio.com/products/SenseCAP/Wio_SX1262/Wio-SX1262_Module_Datasheet.pdf). Retail observations were previously captured in Phase 2A; recheck before purchase.
- India regulatory stop gate: [DoT/WPC rules](https://www.dot.gov.in/acts-rules), [ETA information](https://www.eservices.dot.gov.in/equipment-type-approval-eta), [Radio Equipment Possession Authorisation](https://preprodeservices.dot.gov.in/radio-equipment-possession-authorisation-services). Applicability must be confirmed for the exact hardware and test.
