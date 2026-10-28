#pragma once

// Deliberately has no defaults. A board-specific build must supply and review
// every item after identifying the exact carrier, radio, antenna and legal use.
#if !defined(NX_BOARD_REVISION_VERIFIED) || NX_BOARD_REVISION_VERIFIED != 1
#error "Physical radio TX blocked: verify and document the exact board revision"
#endif
#if !defined(NX_PIN_MAP_VERIFIED) || NX_PIN_MAP_VERIFIED != 1
#error "Physical radio TX blocked: verify SPI, BUSY, DIO1, RESET and RF-switch pins"
#endif
#if !defined(NX_ANTENNA_VERIFIED) || NX_ANTENNA_VERIFIED != 1
#error "Physical radio TX blocked: verify the matched LoRa antenna and connector"
#endif
#if !defined(NX_RF_AUTHORIZATION_VERIFIED) || NX_RF_AUTHORIZATION_VERIFIED != 1
#error "Physical radio TX blocked: document applicable WPC/DoT authorization/settings"
#endif

// Explicit numeric settings are required only in the reviewed private build
// configuration; this repository intentionally provides no frequency/power/etc.
#if !defined(NX_TX_FREQUENCY_HZ) || !defined(NX_TX_POWER_DBM) || \
    !defined(NX_LORA_BANDWIDTH_HZ) || !defined(NX_LORA_SPREADING_FACTOR) || \
    !defined(NX_LORA_CODING_RATE) || !defined(NX_DUTY_CYCLE_LIMIT_PERCENT)
#error "Physical radio TX blocked: explicit authorized radio parameters are missing"
#endif

