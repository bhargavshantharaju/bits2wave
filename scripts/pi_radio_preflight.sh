#!/usr/bin/env bash
# Read-only NexAlert Pi/SX1262 host preflight. Never opens SPI/GPIO or radio devices.
# Run on the physical Pi from a shell; no root or package installation required.

set -u

section() { printf '\n== %s ==\n' "$1"; }
show_file() {
    local label="$1" path="$2"
    if [[ -r "$path" ]]; then
        printf '%s: %s\n' "$label" "$path"
        sed -n '1,30p' "$path" 2>/dev/null || printf '  INCONCLUSIVE: could not read\n'
    else
        printf '%s: NOT AVAILABLE (%s)\n' "$label" "$path"
    fi
}

printf 'NexAlert Pi radio preflight (read-only; no SPI/GPIO access, radio init, or RF)\n'
printf 'Timestamp: '; date -Is 2>/dev/null || date
printf 'Effective user: '; id 2>&1 || true

section 'Host and board identity'
printf 'Kernel: '; uname -srvm 2>&1 || true
printf 'Architecture: '; uname -m 2>&1 || true
show_file 'OS release' /etc/os-release
model=''
for path in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    if [[ -r "$path" ]]; then
        model="$(tr -d '\000' < "$path" 2>/dev/null || true)"
        [[ -n "$model" ]] && break
    fi
done
if [[ -z "$model" ]] && [[ -r /proc/cpuinfo ]]; then
    model="$(sed -n 's/^Model[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -n 1)"
fi
if [[ -n "$model" ]]; then printf 'Device model: %s\n' "$model"; else printf 'Device model: INCONCLUSIVE (no readable device-tree/cpuinfo model)\n'; fi
release="$(uname -r 2>/dev/null || true)"
case "${release,,} ${WSL_DISTRO_NAME-}" in
    *microsoft*|*wsl*) printf 'Environment classification: WSL detected; physical Raspberry Pi has NOT been checked.\n' ;;
    *) if [[ "${model,,}" == *"raspberry pi"* ]]; then
           printf 'Environment classification: Raspberry Pi model string detected; physical identity still requires operator confirmation.\n'
       else
           printf 'Environment classification: Linux host, not identified as Raspberry Pi; physical Pi has NOT been checked.\n'
       fi ;;
esac

section 'Boot configuration locations (read-only)'
found_config=0
for path in /boot/firmware/config.txt /boot/config.txt /boot/firmware/usercfg.txt; do
    if [[ -e "$path" ]]; then
        found_config=1
        printf 'Found: %s\n' "$path"
        if [[ -r "$path" ]]; then
            matches="$(grep -nEi '^[[:space:]]*(dtparam=.*spi|dtoverlay=.*spi|spi=)' "$path" 2>/dev/null || true)"
            if [[ -n "$matches" ]]; then printf '%s\n' "$matches"; else printf '  No matching SPI setting found (not proof of SPI state).\n'; fi
        else printf '  INACCESSIBLE: cannot read file.\n'; fi
    fi
done
[[ "$found_config" -eq 1 ]] || printf 'No standard boot config location found.\n'

section 'SPI device nodes and device-tree references'
shopt -s nullglob
spi_nodes=(/dev/spidev*)
if ((${#spi_nodes[@]})); then
    for path in "${spi_nodes[@]}"; do
        if [[ -e "$path" ]]; then
            stat -c 'Node: %n | mode=%A (%a) | owner=%U:%G' "$path" 2>&1 || true
            [[ -r "$path" ]] && readable=yes || readable=no
            [[ -w "$path" ]] && writable=yes || writable=no
            printf '  Current-user file access: read=%s write=%s (no device opened)\n' "$readable" "$writable"
        fi
    done
else printf 'No /dev/spidev* nodes visible. This is inconclusive for Pi readiness; SPI may be disabled, unavailable, or not exposed in this environment.\n'; fi
for path in /proc/device-tree/aliases /sys/firmware/devicetree/base/aliases; do
    if [[ -d "$path" ]]; then
        printf 'Device-tree SPI aliases under %s:\n' "$path"
        find "$path" -maxdepth 1 -type f -iname '*spi*' -print 2>/dev/null | while IFS= read -r item; do
            printf '  %s = ' "$item"; tr -d '\000' < "$item" 2>/dev/null || printf 'INACCESSIBLE'; printf '\n'
        done
    fi
done

section 'GPIO device nodes and diagnostic tools'
gpio_nodes=(/dev/gpiochip*)
if ((${#gpio_nodes[@]})); then
    for path in "${gpio_nodes[@]}"; do
        [[ -e "$path" ]] || continue
        stat -c 'Node: %n | mode=%A (%a) | owner=%U:%G' "$path" 2>&1 || true
        [[ -r "$path" ]] && readable=yes || readable=no
        [[ -w "$path" ]] && writable=yes || writable=no
        printf '  Current-user file access: read=%s write=%s (no GPIO device opened)\n' "$readable" "$writable"
    done
else printf 'No /dev/gpiochip* nodes visible. This is inconclusive outside the physical Pi.\n'; fi
for utility in gpioinfo gpiodetect pinctrl raspi-gpio; do
    if command -v "$utility" >/dev/null 2>&1; then
        path="$(command -v "$utility")"
        version="$("$utility" --version 2>&1 | head -n 1 || true)"
        printf '%s: %s%s\n' "$utility" "$path" "${version:+ | $version}"
    else printf '%s: not installed/not in PATH\n' "$utility"; fi
done

section 'Kernel modules (read-only inventory)'
if [[ -r /proc/modules ]]; then
    matches="$(grep -Ei '(^|_)(spi|gpio|spidev)(_|[[:space:]])' /proc/modules || true)"
    [[ -n "$matches" ]] && printf '%s\n' "$matches" || printf 'No matching loaded module names found; built-in drivers may not appear here.\n'
else printf '/proc/modules unavailable.\n'; fi
if command -v lsmod >/dev/null 2>&1; then
    lsmod 2>/dev/null | grep -Ei '(^Module|spi|gpio)' || true
else printf 'lsmod: not installed/not in PATH\n'; fi

section 'Radio-related utilities and Python modules'
for utility in meshtasticd meshtastic sx126x lora; do
    if command -v "$utility" >/dev/null 2>&1; then printf '%s: %s\n' "$utility" "$(command -v "$utility")"; else printf '%s: not installed/not in PATH\n' "$utility"; fi
done
if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import importlib.util
for name in ("spidev", "lgpio", "gpiod", "RPi.GPIO", "LoRaRF", "meshtastic"):
    try:
        present = importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        present = False
    print(f"Python module {name}: {'available' if present else 'not available'}")
PY
else printf 'python3: not installed/not in PATH\n'; fi

section 'Interpretation and safety'
printf 'This report only inspects files, metadata, command availability, and device-node permissions. It did not open SPI/GPIO devices, initialize/reset a radio, change system configuration, install software, or transmit.\n'
if [[ "${release,,} ${WSL_DISTRO_NAME-}" == *microsoft* || "${release,,} ${WSL_DISTRO_NAME-}" == *wsl* ]]; then
    printf 'RESULT: WSL environment only. Raspberry Pi 5 and HAT readiness are NOT VERIFIED. Run this script locally on the physical Pi.\n'
elif [[ "${model,,}" == *"raspberry pi"* ]]; then
    printf 'RESULT: Pi model metadata is visible, but SPI/GPIO status and permissions are observations only; HAT identity and physical radio readiness remain NOT VERIFIED.\n'
else
    printf 'RESULT: This host is not identified as a Raspberry Pi. Physical Pi readiness is NOT VERIFIED.\n'
fi
