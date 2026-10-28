# Phase 2E — Hardware preflight and decision

**Status: BLOCKED for the first two-node RF test.** This is an identification and read-only diagnostic phase. The script was exercised only in the current Linux/WSL development environment, not on the physical Pi. No radio was initialized, settings changed, components purchased, or RF transmitted.

## Evidence-based inventory

| Requirement | Status | Evidence / remaining work |
|---|---|---|
| Raspberry Pi 5, 4 GB | NOT VERIFIED (physical preflight) | Board/model is user-reported. Run the command below on that Pi and retain its output. |
| Existing SX1262/GNSS HAT identity | NOT VERIFIED | Photo description suggests Waveshare SX1262 XXXM LoRaWAN/GNSS HAT family and L76K GNSS. Exact SKU, PCB revision, band variant, antenna connectors and vendor-specific configuration are not confirmed from repository evidence. Inspect physical labels and packaging. |
| ESP32-S3-N16R8 MCU | NOT VERIFIED (physical preflight) | Module family is user-reported; carrier manufacturer/revision, pinout and radio attachment are unknown. No integrated SX1262 is confirmed. |
| STM32 NUCLEO-F446RE | NOT VERIFIED (physical preflight) | PCB identity is user-reported. It has no confirmed LoRa radio and is not the second radio endpoint. |
| Solar panel, battery, charge controller | NOT VERIFIED | Their presence is reported, but exact models, electrical ratings, chemistry, protection and wiring are undocumented. Not needed for the initial bench packet test. |
| Second radio endpoint | BLOCKED | None confirmed. Seeed XIAO ESP32-S3 + Wio-SX1262 kit is only a candidate. Do not call it compatible until exact HAT band/variant, Wio variant, interfaces, antennas and driver path are matched. |
| Application transport boundary | VERIFIED (software only) | Flask selects `SimulatedTransport` by default. `RadioSosTransport` is injected, hardware-independent and transmit-disabled unless an explicit permission callback is supplied. It is not bound to a physical driver. |
| Firmware readiness | BLOCKED | ESP32 directory is an abstract interface and configuration guard, not a buildable firmware target or tested board configuration. |
| Regulatory/transmit basis | BLOCKED | The broad HAT marking/range does not establish an allowed configuration. Exact equipment and intended use must be assessed against current DoT/WPC rules before transmission. |

## Run the read-only preflight on the actual Pi

From a terminal on Raspberry Pi OS/Linux, in the project checkout (or after copying only this script):

```sh
bash scripts/pi_radio_preflight.sh
```

It needs no root and installs nothing. It reports board/OS/kernel/architecture, likely boot configuration paths and visible SPI settings, SPI/GPIO nodes and permissions, device-tree aliases, loaded module names, and diagnostic utility/Python module availability. It never opens a device node. Save the complete output with the physical board photos and date.

If run in WSL, it explicitly says the physical Pi has not been checked. No `/dev/spidev*` node on WSL or another host is evidence about Pi readiness. On a real Pi, missing SPI nodes are an observation only: SPI may not be enabled/exposed, but this script does not determine the cause or change it. Do not run `raspi-config`, edit overlays, alter groups/permissions, or load modules as part of preflight. Any later setting change needs a separate reviewed task.

## Physical identity checklist

Before selecting a driver or candidate second endpoint, capture and record:

- Pi board top marking, 4 GB identification, OS release and the preflight output from the actual Pi.
- HAT clear front and back photos; exact product label/packaging SKU; PCB silkscreen and revision; radio-module marking; frequency variant; LoRa and GNSS connector labels and connector type.
- Both supplied/available antenna labels, model, rated band, connector, gain/cable details and condition. Confirm which antenna is explicitly the LoRa antenna. Do not substitute GNSS antenna.
- ESP32-S3 carrier manufacturer/product/revision and exposed pinout. For the proposed Seeed kit, record the exact Wio-SX1262 SKU/band, board revision, connector and antenna included; verify it against the exact HAT band and an official wiring/driver document. No pin map is approved yet.
- NUCLEO PCB label/revision only if it is being assigned a role; it cannot supply the missing radio by itself.
- Solar/energy component labels and measured specifications only when entering the later power phase.

The inventory at [hardware-inventory.md](hardware-inventory.md) now separates user-reported identities from on-device verification. The schematic/example pin references there are manufacturer-family references only; do not configure hardware from them until exact revision matching is complete.

## India regulatory and equipment gate

Official DoT/WPC sources consulted on 2026-10-28:

- [865–868 MHz low-power SRD rules, 2021 (official DoT-hosted Gazette text)](https://www.dot.gov.in/static/uploads/2025/07/84f33f09e137fa81930f44bcd5f2d238.pdf). This is a specific band/rule with technical conditions; it does not authorize arbitrary operation anywhere in a module's wider tuning range.
- [DoT list of applicable frequency-band rules and amendments](https://www.dot.gov.in/static/uploads/2026/02/b110cdc386d3a4e41c8483d7ffd7c410.pdf), including subsequent amendments as applicable.
- [DoT Equipment Type Approval (ETA) service](https://www.eservices.dot.gov.in/equipment-type-approval-eta): describes ETA for products operating in permitted licence-exempt bands and supporting documentation. Confirm how ETA applies to the exact imported/assembled modules and intended use.
- [Radio Equipment Possession Authorisation service](https://www.eservices.dot.gov.in/radio-equipment-possession-authorisation-services) and [DoT authorisation portal](https://www.eservices.dot.gov.in/authorisation-portal): current 2026 framework includes possession for testing/demonstration; applicability and any exemption for this equipment, owner and experiment must be confirmed with DoT/WPC before test.

Before a separately authorized transmit test, retain written evidence that resolves: exact radio variant and equipment approval/ETA status; legal basis for possession and experimental use; permitted channel/frequency, output power/ERP, antenna gain/configuration, bandwidth/duty-cycle/other technical restrictions for the intended application and location; and the correctly matched antenna and connector. These are distinct legal and manufacturer/engineering checks. Do not choose values from a vendor example or infer permission from “850–930 MHz” or “862–930 MHz”. This report is not legal advice or a radio authorization.

## Go/no-go decision

**NO-GO for RF transmission now.** The first experiment is blocked by (1) exact HAT SKU/revision/band, (2) actual Pi read-only preflight, (3) identified second endpoint compatible with that HAT and its antenna, and (4) documented regulatory/equipment authorization and permitted configuration. Firmware/driver matching and a safe configuration review follow those items.

**Operator next action:** photograph the HAT's front, back, labels and packaging plus both antenna labels; record the Seeed kit's exact SKU if acquired/available; then run `bash scripts/pi_radio_preflight.sh` directly on the Pi and attach/save the output. Do not enable SPI or transmit as part of that action.

After identity and regulatory review, plan a separate authorization for the first bench test. It should begin with a source-audited receive-only initialization only if the exact driver supports it; then a compliant two-node packet/ACK test, lost-ACK retry and duplicate-suppression check, and finally an SOS through the existing transport with destination persistence before receipt. No RF settings are prescribed here.

## Diagnostic run in this development environment

Command: `bash scripts/pi_radio_preflight.sh` (exit 0), run on 2026-10-28. Observed Ubuntu 24.04.5 LTS under WSL2, Linux kernel `6.18.33.2-microsoft-standard-WSL2`, `x86_64`, non-root user. Device model was inconclusive; no `/dev/spidev*` or `/dev/gpiochip*` nodes or standard Pi boot configuration locations were visible. `gpioinfo`, `gpiodetect`, `pinctrl`, `raspi-gpio`, `meshtasticd`, and `meshtastic` were not found; Python modules `spidev`, `lgpio`, `gpiod`, `RPi.GPIO`, `LoRaRF`, and `meshtastic` were unavailable. This is only a report about the WSL development environment. It provides **no evidence** for or against the Pi 5's SPI/GPIO readiness or whether the HAT is fitted.
