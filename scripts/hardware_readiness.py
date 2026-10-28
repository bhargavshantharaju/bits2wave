"""Read-only host readiness checks for a future NexAlert radio bring-up.

This script does not open SPI/GPIO devices, probe a radio, edit configuration,
or transmit. A positive software check cannot prove that a HAT is physically
installed or that its variant/configuration is correct.
"""

import glob
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys


def text_file(path):
    try:
        return Path(path).read_text(errors="replace").strip().replace("\x00", "")
    except (OSError, UnicodeError):
        return None


def module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def main():
    os_release = text_file("/etc/os-release") or "unavailable"
    model = text_file("/proc/device-tree/model") or text_file("/sys/firmware/devicetree/base/model")
    spi_nodes = sorted(glob.glob("/dev/spidev*"))
    gpiochips = sorted(glob.glob("/dev/gpiochip*"))
    gpio_access = {path: os.access(path, os.R_OK | os.W_OK) for path in gpiochips}
    spi_access = {path: os.access(path, os.R_OK | os.W_OK) for path in spi_nodes}
    packages = {name: module_available(name) for name in ("spidev", "lgpio", "gpiod", "RPi.GPIO", "LoRaRF", "meshtastic")}
    executables = {name: shutil.which(name) for name in ("meshtasticd", "meshtastic")}
    on_pi = bool(model and "raspberry pi" in model.lower())

    print("NexAlert radio host readiness (read-only; no SPI/GPIO probe or RF)")
    print(f"Host: {platform.node()} | {platform.machine()} | Python {platform.python_version()}")
    print(f"OS: {os_release.splitlines()[0] if os_release else 'unavailable'}")
    print(f"Device-tree model: {model or 'not available'}")
    print(f"Raspberry Pi detected: {'yes' if on_pi else 'no'}")
    print(f"SPI device nodes: {', '.join(spi_nodes) if spi_nodes else 'none'}")
    print(f"SPI node read/write access: {spi_access or 'not applicable'}")
    print(f"GPIO character devices: {', '.join(gpiochips) if gpiochips else 'none'}")
    print(f"GPIO device read/write access: {gpio_access or 'not applicable'}")
    print(f"Effective UID: {os.geteuid()} (root={os.geteuid() == 0})")
    print("Python modules: " + ", ".join(f"{key}={'yes' if value else 'no'}" for key, value in packages.items()))
    print("Executables: " + ", ".join(f"{key}={value or 'not found'}" for key, value in executables.items()))
    print("Physical HAT/radio presence: not detectable by these checks; inspect board labels and fitment")
    print("Board revision, RF band, antenna, pin wiring, and lawful configuration: not verified")
    print("Result: " + ("Pi host candidate; physical HAT and exact configuration remain unverified" if on_pi
                      else "environment-only report; this host cannot validate Raspberry Pi radio readiness"))


if __name__ == "__main__":
    main()
