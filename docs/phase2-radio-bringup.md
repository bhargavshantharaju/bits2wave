# NexAlert Phase 2 radio bring-up preparation

Updated 2026-10-28. This preparation is host-only. No device was flashed, no Pi configuration was changed, no GPIO/SPI transaction was attempted, and no RF packet was transmitted.

## Confirm exact hardware first

The first endpoint is one user-identified blue Pi HAT, **likely** the Waveshare SX1262 XXXM LoRaWAN/GNSS HAT with L76K GNSS. Before touching the driver, record board labels, exact frequency SKU, PCB revision, antenna connector and both antenna models. The supplied photo identification is strong but the exact SKU/revision has not yet been verified against packaging or a board close-up. The Pi 5 is 4 GB; the second radio is not present. The ESP32-S3-N16R8 carrier is unidentified and has no visible SX1262.

The manufacturer's current family page describes a standard Raspberry Pi 40-pin connection, SPI radio interface, SX1262 BUSY handling, reset, DIO1 IRQ, DIO2/RXEN switch use and TXEN on a Pi BCM pin; its schematic gives the reference pin map in [hardware-inventory.md](hardware-inventory.md). Use it only after matching the physical board revision. Do not substitute pin tables from unrelated Waveshare Core1262 boards or generic SX1262 breakouts.

The product family specifies distinct 433/470M and 868/915M products (nominal family ranges 410–490 MHz and 850–930 MHz, respectively). A printed 850–930 MHz capability label is not a legal channel configuration. Manufacturer material lists 3.3 V radio operation and module-only current figures; obtain a measured full-system power budget before designing battery operation.

## Read-only host diagnostics

Run from the project root with the existing Python environment:

```bash
./.venv-phase1-verify/bin/python -B scripts/hardware_readiness.py
```

It only reads OS/device-tree text, checks `/dev/spidev*` and `/dev/gpiochip*` nodes and access bits, and checks installed module/executable availability. It does not open the devices. It does not enable SPI or GPIO, install packages, probe a radio, or transmit. “Ready” cannot mean the HAT is physically present; visual board/antenna inspection remains necessary.

Optional manual checks on the **actual Pi 5**, also read-only:

```bash
cat /proc/device-tree/model
cat /etc/os-release
uname -a
ls -l /dev/spidev* /dev/gpiochip* 2>/dev/null
id
command -v meshtasticd
python3 -c 'import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ("spidev", "lgpio", "gpiod", "LoRaRF", "meshtastic")})'
```

If SPI nodes are absent, record that fact and inspect the Pi's current configuration separately. Do not edit `/boot/firmware/config.txt`, run `raspi-config`, add `dtoverlay=spi0-0cs`, or change GPIO access groups during this diagnostic. Any later system change needs a reviewed board-specific driver requirement and a backup/recovery path. Do not install packages globally; use a dedicated environment after confirming the correct official sample dependencies and OS compatibility.

### Diagnostics performed for this preparation

The available host is **Ubuntu 24.04.5 on x86_64 WSL2**, Python 3.12.3—not a Raspberry Pi. The script found no device-tree model, `/dev/spidev*`, `/dev/gpiochip*`, `meshtasticd`, `meshtastic`, `spidev`, `lgpio`, `gpiod`, `RPi.GPIO`, `LoRaRF` or Meshtastic Python module. This is expected for this host and says nothing about the actual Pi 5 or physical HAT.

## Driver and safe initialization gate

Waveshare publishes a Pi Python/C demo archive and documents `receiver_continuous.py` as the receive-side example; `transmitter.py` is separate. I inspected the official archive in memory without extracting or installing it. The receive file calls `begin()`, sets DIO2 RF-switch behavior, hard-codes **868 MHz**, sets modulation/packet parameters, and calls `request(RX_CONTINUOUS)`. It contains no transmit call, so its main receive flow is receive-only. It uses `busId=0`, `csId=0`, `resetPin=18`, `busyPin=20`, `irqPin=16`, `txenPin=6`, `rxenPin=-1`—matching the published HAT family example, not independently verifying the owned revision.

This sample is **not ready to run unchanged**. The hard-coded 868 MHz is not a configuration recommendation or permission to transmit, and the code's `try/except` cleanup is positioned after an infinite loop rather than wrapping it; interrupting the loop will not reliably call `LoRa.end()`. Treat it as reference source only. A separate opt-in receive-only test should use a reviewed local copy with cleanup in `finally`, and run only after verifying the physical HAT SKU/revision and pinout. It was not run in this task.

Before that opt-in check:

1. Match HAT revision and band from the board label and use the corresponding manufacturer source.
2. Recheck the exact downloaded receive example and every call path to verify it never invokes transmit, auto-response or a mode that emits RF. Preserve a local source hash and audit notes.
3. Confirm the SKU/antenna and only use a matching receive configuration. The sample's 868 MHz constant must not be copied as a transmit setting; get WPC authorization for all future TX settings separately.
4. Review dependencies and GPIO library compatibility with the Pi OS before installing anything. Prefer a project-local/isolated environment where the vendor package supports it.
5. Record command, source archive hash/version, OS, HAT revision, pin map, antenna and logs. Stop if board identity or antenna remains uncertain.

No RF transmit command belongs in bring-up diagnostics. The manufacturer's own packet transmitter requires a second HAT and is not authorized for this task. We did not run either sample.

## Meshtastic on the Pi HAT

Meshtastic's Linux-native `meshtasticd` runs on Linux/macOS with SPI or supported USB radios, exposes the node API to clients, and provides Raspberry Pi OS/Linux package instructions. Meshtastic's current Linux hardware page, however, explicitly says the Waveshare SX1262 LoRaWAN HAT is **not recommended for deployment** because of known hardware limitations affecting longer messages; if used, it recommends `CLIENT_MUTE` to avoid rebroadcast. This is stronger negative evidence than “SX1262 is supported.” A community issue reports a Waveshare `lora-waveshare-sxxx.yaml` profile and a setup that ran on a Pi 4, but also documents detection/operation problems. That is not a Pi 5 certification.

Meshtastic's Linux distribution matrix includes Raspbian and Ubuntu SPI support, and the 64-bit Pi OS image documentation includes Pi 5 models. This establishes software-platform plausibility, not the exact HAT+Pi5 combination. The user's actual OS/SPI nodes and HAT revision remain unknown. Do not apply a sample YAML pin map until the board revision is matched; do not use an unverified `spi0-0cs` overlay.

The named NUCLEO-F446RE is not a Meshtastic STM32WL radio target and has no LoRa radio. The ESP32-S3-N16R8 MCU family is compatible in principle, but Meshtastic support is board/pin/variant-specific. A complete supported ESP32-S3/SX1262 endpoint is a safer Meshtastic candidate than an unidentified ESP32 carrier plus a loose radio module.

## Before first physical radio test

- Verify both endpoint models and that their radio modules overlap in usable band and have compatible front-end/antenna connections.
- Confirm Indian WPC legal basis for the exact device, frequency, ERP, antenna gain, duty cycle, equipment approval and whether current possession/testing rules apply. The DoT publishes the 865–868 MHz SRD exemption rules and later amendments, and a separate ETA process; 2026 radio-equipment possession authorisation rules are also now listed by DoT. A transmitter's broad tuning range is not permission to use all those frequencies.
- Keep actual TX disabled until the above review is complete and the LoRa antenna is installed. A GNSS antenna is not a substitute.
- Start with receive-only hardware initialization after code audit; then a separately approved conducted/very-low-risk RF test plan with defined legal settings and two endpoints.
- Capture raw logs and measurements. Distinguish radio TX completion, hop ACK, application ACK, and final persisted command-centre receipt.

Regulatory primary references: [DoT/WPC rules and acts](https://www.dot.gov.in/acts-rules), [DoT ETA information](https://www.eservices.dot.gov.in/equipment-type-approval-eta), and [DoT Radio Equipment Possession Authorisation service](https://preprodeservices.dot.gov.in/radio-equipment-possession-authorisation-services). Applicability must be confirmed for this exact equipment and experiment before transmission.

## Phase 2B decision and reproducible two-node proof

The selected proof path is a **direct custom NexAlert link** using maintained SX126x drivers and the existing `backend/radio_protocol.py` frame codec. It is not yet cleared for hardware use: the photographed Pi HAT's exact SKU/revision/band and the second endpoint are unconfirmed, and WPC/DoT conditions are unresolved. Do not purchase, configure or transmit based only on the broad `850–930 MHz` marking. See [radio-architecture-decision.md](radio-architecture-decision.md) for comparison with Meshtastic, MeshCore and the hybrid option.

### Hardware and authorization gates

1. Record readable front/back board labels and exact revisions for the HAT and ESP32-S3 carrier; identify the second radio SKU, radio band, antenna model, connector and included cable. Compare each against the matching manufacturer schematic/datasheet. **Not yet complete.**
2. Obtain applicable WPC/DoT guidance for the exact modules, antenna gain, assembly, test purpose and intended settings. **Not yet complete.** No frequency, TX power, bandwidth, spreading factor or duty cycle is prescribed here.
3. On the physical Pi 5, collect model, OS, SPI/GPIO node and permissions diagnostics. Match the Waveshare driver to the HAT revision. **Not performed; WSL diagnostics are not Pi evidence.**
4. Review the downloaded receive-only sample and call graph. It must use no TX/auto-response path and clean up in `finally`. Run that diagnostic only after board identity is confirmed. **Source archive review was performed; the sample was not run.**
5. Build and run packet codec, idempotency, failure/retry and temporary-database tests before connecting any radio.

### Authorized two-node proof sequence (future bench run)

Proceed to each RF step only after steps 1–2 above are documented and approved for the exact hardware.

1. Confirm both endpoints have compatible, verified radio variants and matching approved configuration; install the correct LoRa antennas on their LoRa ports. Do not substitute GNSS antennas.
2. Initialize each radio receive-only first if the reviewed driver supports it. Record driver/library revision, HAT/endpoint revision and initialization result. Do not claim initialization based on host mocks.
3. Establish the approved two-node radio configuration from the applicable authoritative source. Values are deliberately omitted until legal and board details are confirmed.
4. Send one short test frame A→B. Verify protocol version, source/destination IDs, stable message ID, sequence, payload length and integrity check at B.
5. Return a correlated ACK B→A and log radio TX-done separately from ACK receipt.
6. Drop one application ACK and retry the exact same `message_id` and `incident_id`. Confirm B deduplicates it and stores only one incident.
7. Connect endpoint A's gateway to a physical adapter implementing the existing `SosTransport` contract. Keep the simulated adapter an explicit, separate choice.
8. Submit a test SOS and trace: durable source outbox → radio TX-done → optional hop ACK → application ACK → receiver SQLite commit → command-centre receipt. The last receipt is allowed only after commit. Verify all identifiers remain unchanged.
9. Measure packet delivery ratio, one-way/round-trip latency, retry count, duplicate count, behavior after a lost ACK/restart, and current draw. Record distance/environment and any genuine radio signal diagnostics. Repeat only within the approved conditions.
10. Run the current simulated-transport regression suite and compare its outcomes with the physical run; never report simulated delivery as radio delivery.

Physical master failover is a later gate. First require repeatable two-node SOS receipt, authenticated application frames, idempotent retry/restart recovery and bounded measured airtime. Then add a relay and prove routing, duplicate suppression and store-and-forward. Only after that should heartbeat transport, independent backup power, partition safety and recovery sync be tested on real primary/backup hardware.

### Stack notes for the bench

- **Custom direct link (selected):** likely Pi endpoint is Waveshare's host-side driver, but exact HAT identity and Pi 5 support are unverified; ESP32 endpoint can use RadioLib if its target pin/RF-switch setup is confirmed.
- **Meshtastic:** the official Linux hardware page warns that the Waveshare SX1262 HAT is not recommended for deployment due to longer-message limitations. A fair test requires two supported identical ESP32-S3/SX1262 nodes; the Pi Python API can then be evaluated separately.
- **MeshCore:** upstream has a `xiao_s3_wio` target and a KISS serial radio-modem role. KISS is a raw modem boundary and does not itself implement routing; `simple_repeater` is a separate MeshCore routing role. There is no confirmed MeshCore Linux target for the photographed Pi HAT.
- No approach is physically validated by these notes. No device was flashed and no RF packet was sent.

### Phase 2C host integration status

`backend/transport.py` now holds the common `SosTransport` ABC, and the existing `SimulatedTransport` still implements it. `backend/radio_transport.py` provides a `RadioSosTransport` adapter over an injected `RadioLink`: it supports send/receive, peer filtering, correlated ACK/RECEIPT, bounded attempts/backoff, disconnect state, stable incident-derived message IDs and injected durable-store callbacks. Transmission is denied unless an explicit authorization callback returns true. A radio TX completion or hop ACK never marks the source outbox delivered; only a correlated, non-local receipt can do so.

The destination callback contract is synchronous: it must commit the incident and its persistent receipt before returning a receipt ID. The receiver then emits its ACK and receipt. The existing simulator and the fake-link Flask acceptance test use shared command-centre persistence/receipt helpers. The adapter itself does not open or migrate either database, and no production queue binding has been installed. Flask continues to select the existing simulated adapter by default. Integration tests inject the fake-link adapter into the real `/api/sos` request path with temporary sender and receiver databases.

`firmware/esp32_sx1262/` contains an abstract C++ `RadioPort` and an empty-by-default compile-time configuration guard. There is no firmware target, no pin map, no RadioLib binding and no firmware build claim. The guard requires explicit board, pin, antenna, regulatory and radio-parameter macros; the project supplies no values.

The current host-side Python protocol tests cover serialization, limits, corruption and ACK correlation; the new radio-adapter tests cover retry identity, lost receipts, duplicate suppression, persistence-before-receipt, disconnected links and the default-disabled TX gate. These tests validate software boundaries only, not hardware.

## Phase 2E physical readiness gate

The latest hardware preflight decision is recorded in [phase2e-hardware-preflight.md](phase2e-hardware-preflight.md). The Pi model, HAT SKU/revision/band, antenna match and second endpoint remain unverified from physical evidence. Run `bash scripts/pi_radio_preflight.sh` on the actual Raspberry Pi 5; its output from WSL or another Linux machine does not count as Pi evidence. This diagnostic reads metadata and permission bits only and does not access SPI/GPIO, change configuration, initialize the radio or transmit. RF testing remains **NO-GO** until the exact endpoint pair and antennas are identified and the current Indian regulatory/equipment authorization basis and permitted configuration are documented.
