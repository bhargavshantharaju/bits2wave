"""Shared SOS transport contract used by simulated and future physical adapters."""

from abc import ABC, abstractmethod


class SosTransport(ABC):
    """Transport boundary; delivery means a persistent command-centre receipt."""

    @abstractmethod
    def process_queue(self, limit=None):
        """Try durable pending messages without changing their incident IDs."""

    def status(self):
        """Return truthful transport state; adapters must not infer radio health."""
        return {"kind": "unknown", "connected": False, "radio_verified": False}
