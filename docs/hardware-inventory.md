# NexAlert hardware inventory

Updated 2026-10-28 from the user's board-photo identification and the cited manufacturer documentation. “Photo confirmed” means the user identified the visible PCB/module markings; the photographs are not stored in this repository, so a label close-up should be retained with the final BOM.

## Identified equipment

| Hardware | Evidence and confirmed facts | Intended role | Still unresolved |
|---|---|---|---|
| Raspberry Pi 5, 4 GB | User-confirmed board/model and RAM. | Primary command centre, Flask/SQLite host, candidate radio gateway. | OS image/release and architecture on the actual Pi; power supply; HAT fit, cooling and measured behavior. |
| Blue integrated SX1262/GNSS HAT | User reports photo markings consistent with **Waveshare SX1262 XXXM LoRaWAN/GNSS HAT**, likely 868/915-class SKU. The official family uses SX1262, SPI, Raspberry Pi 40-pin header and optional GNSS. | One radio endpoint on the Pi, subject to label/revision verification. | Exact SKU/revision, 433/470 versus 868/915 variant, antenna connector and included antenna, board pin revisions, radio front-end population, frequency/power settings. |
| GNSS on likely HAT | Waveshare's GNSS variant documentation and schematic identify a Quectel L76K receiver. The user's photo description identifies the integrated GNSS variant. | Optional GNSS location/time source; not required for SOS submission. | Exact board SKU/revision, GNSS antenna presence/type, battery/backup cell presence, serial/GPIO routing, reception test. |
| ST NUCLEO-F446RE | User-confirmed PCB label `NUCLEO-F446RE`; this is the STM32F446RE Nucleo-64 development board. It is **not** the DPR's STM32G031K8 and does not contain a LoRa radio. | General-purpose MCU/dev board for non-radio experiments if needed; not the proposed second radio endpoint. | Exact PCB revision and firmware role. |
| ESP32-S3-N16R8 development board | User-confirmed module marking `ESP32-S3-N16R8`. This identifies the ESP32-S3 module family/variant, not the carrier manufacturer. No SX1262 is visible in the supplied photo description. | Candidate local emergency-page/controller MCU. | Carrier-board manufacturer/revision, exposed GPIO map, power input, USB interface, battery circuitry and any attached radio (none confirmed). |

## Radio count and RF chain

- **Confirmed radio endpoints: one SX1262 endpoint**, integrated on the likely Raspberry Pi HAT.
- **Second endpoint: missing.** The ESP32-S3 board alone is not a second radio.
- A complete ESP32-S3/SX1262 board with an upstream documented target is the preferred class for the missing endpoint. Seeed XIAO ESP32-S3 & Wio-SX1262 is one candidate with Meshtastic device-list support and a MeshCore `xiao_s3_wio` target. This is a candidate only: exact Wio SKU/band, antenna inclusion and compatibility with the photographed HAT are not confirmed.
- The photo description indicates separate LoRa and GNSS antenna ports, but connector standards, included antennas, model, band, gain, condition and cable loss are unresolved. Verify the LoRa antenna is attached to the LoRa port before any RF transmit test; the GNSS antenna is not a LoRa antenna.
- The user's HAT description says `850–930 MHz`, which is consistent with Waveshare's 868/915M family range. It does **not** establish a lawful channel, usable Indian channel plan, ERP, transmit power, duty cycle, ETA or current possession/operation authorisation.
- Do not treat 433/470M and 868/915M HATs as interchangeable. Waveshare documents different ranges: 433/470M family nominally 410–490 MHz, versus 868/915M family nominally 850–930 MHz. Exact board label/packaging is the authority for the owned unit.

## Manufacturer reference pin map — reference only

References: [Waveshare HAT documentation](https://www.waveshare.com/wiki/SX1262_XXXM_LoRaWAN/GNSS_HAT) and [published schematic](https://files.waveshare.com/wiki/SX1262-XXXM-LoRaWAN-GNSS-HAT/SX1262_XXXM_LoRaWAN_HAT_sch.pdf). These describe a documented product-family/revision and do not establish the identity of the photographed unit.

Waveshare's published HAT schematic shows a Raspberry Pi 40-pin connection. For the documented schematic revision, the visible signal map corresponds to:

| SX1262 signal | Pi header / BCM reference |
|---|---|
| MOSI | physical 19 / BCM 10 |
| MISO | physical 21 / BCM 9 |
| SCLK | physical 23 / BCM 11 |
| CS / CE0 | physical 24 / BCM 8 |
| DIO1 / IRQ | physical 36 / BCM 16 |
| BUSY | physical 38 / BCM 20 |
| RESET | physical 12 / BCM 18 |
| TXEN / board RF switch control | physical 31 / BCM 6 |

This is **not yet the owned board's confirmed wiring**. Do not paste these values into a driver or Meshtastic config until the board marking/revision and schematic are matched. Waveshare documents DIO2-to-RXEN/RF-switch behavior; control details differ from generic SX1262 carriers. Raspberry Pi `SPI0` signals and CE0 are standard header functions, but host pin mapping does not prove the HAT revision or RF switch path.

## Power details and limits

Waveshare describes 3.3 V radio operation and publishes module figures around 45 mA transmit at 14 dBm and 5.3 mA receive at 125 kHz for the product family. Treat those as vendor test figures, not measured whole-HAT or Pi-system consumption. The HAT schematic shows a 5 V Pi input and onboard 3.3 V supply; verify the exact revision and budget the Pi 5 separately. Antenna mismatch, peak current, thermal behavior and supply transients have not been measured.

Battery chemistry/capacity, solar panel rating, charge controller, protection, GPS backup cell and runtime remain unverified. Environmental sensing remains deferred.

## Repository and current host evidence

- No photographs, HAT-specific source, firmware, BOM or wiring drawings are stored in this repository.
- `backend/app.py` contains the existing `SosTransport` interface and `SimulatedTransport`; it is not connected to radio hardware.
- `backend/transport.py` is the shared interface; `backend/radio_transport.py` implements a hardware-independent, authorization-gated adapter contract with injected driver/storage callbacks. It has no actual GPIO/SPI/serial implementation and is not selected by Flask.
- `backend/radio_protocol.py` is host-side byte serialization scaffolding only. It contains no radio driver or RF configuration.
- `firmware/esp32_sx1262/` contains only an abstract driver interface and a deliberately incomplete compile-time RF configuration guard. No board target or radio settings are supplied; it is not a buildable firmware target.
- `backend/failover.py` and its SQLite outbox simulate failover on the host; they do not run on the ESP32-S3.
- `scripts/hardware_readiness.py` performs read-only OS/device-node/package checks. It cannot detect a HAT physically without board inspection or an opt-in hardware initialization.

## Required evidence before wiring or transmission

1. Save clear front/back photographs of the HAT, product label/packaging, antenna ports and any SKU/revision code.
2. Confirm the 433/470M or 868/915M product code from marking/packaging and verify the manufacturer schematic matching that revision.
3. Inventory both antennas separately: model, frequency range, connector, gain, cable and physical condition.
4. Confirm the Pi OS, SPI and GPIO behavior on the actual Pi 5 using read-only diagnostics before enabling or changing interfaces.
5. Obtain current WPC/regulatory guidance for exact hardware and use before any RF transmission. No frequency, transmit power or duty-cycle values are selected in this repository.

## Phase 2E verification status (2026-10-28)

| Item | Status | Evidence and next verification |
|---|---|---|
| Raspberry Pi 5, 4 GB | NOT VERIFIED on physical hardware | User-reported board identity; no physical Pi preflight output is present. Run `bash scripts/pi_radio_preflight.sh` on the Pi and save output. |
| Blue SX1262/GNSS HAT | PHYSICAL VERIFICATION REQUIRED | Likely Waveshare product family only. Exact SKU, PCB revision, frequency variant, connector models and antenna identity have not been established by repository evidence. Photograph front/back silkscreen, labels and packaging. |
| ESP32-S3-N16R8 carrier | PHYSICAL VERIFICATION REQUIRED | Module identity is user-reported; carrier manufacturer/revision, pinout, power details and radio interface remain unknown. |
| STM32 NUCLEO-F446RE | PHYSICAL VERIFICATION REQUIRED | PCB label is user-reported; this board is not a LoRa radio endpoint. |
| Solar panel, battery, charge controller | NOT VERIFIED | Existence is user-reported only. Exact models, panel rating, battery chemistry/capacity, controller, protection and wiring are unknown. Deferred from first radio packet proof. |
| Second radio endpoint | BLOCKED | No second endpoint is confirmed. Seeed XIAO ESP32-S3 + Wio-SX1262 remains a candidate only; do not declare compatible until both bands, radio/host interfaces, firmware/driver path and LoRa antennas are confirmed. |
| Physical Pi SPI/GPIO state | NOT VERIFIED | Run the new read-only `scripts/pi_radio_preflight.sh` locally on the physical Pi. A WSL/development-host result cannot establish Pi readiness. |
| Actual transmission authorization | BLOCKED | Confirm current applicable DoT/WPC possession/testing requirements, equipment approval, exact permitted RF parameters and matched antenna for this device and experiment. See [Phase 2E preflight](phase2e-hardware-preflight.md). |

The preflight script does not open SPI/GPIO device nodes and cannot detect whether the HAT is physically fitted. Manufacturer family pin maps in this document remain reference-only until matched to the exact board revision.
