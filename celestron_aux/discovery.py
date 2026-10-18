"""Collects traffic that the catalog cannot name, to help decode new devices.

Every packet whose device or command is missing from the catalog is grouped by
(device, command) with a count, when it was seen, who it talked to and a few
distinct example payloads in each direction.
"""

from __future__ import annotations

import csv
import io
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from . import catalog
from .bus import BusEvent
from .protocol import AuxPacket

MAX_SAMPLES = 5  # distinct example payloads kept per direction


def is_unknown(packet: AuxPacket) -> bool:
    """True if the catalog cannot name the device or the command of ``packet``."""
    subject = catalog.subject_device(packet.source, packet.destination)
    return not (
        catalog.is_known_device(packet.source)
        and catalog.is_known_device(packet.destination)
        and catalog.is_known_command(subject, packet.command)
    )


@dataclass
class Discovery:
    device: int
    command: int
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    peers: Set[int] = field(default_factory=set)
    requests: List[str] = field(default_factory=list)  # example payloads sent to the device
    replies: List[str] = field(default_factory=list)   # example payloads the device sent

    @property
    def device_known(self) -> bool:
        return catalog.is_known_device(self.device)

    def as_dict(self) -> Dict[str, object]:
        return {
            "device": self.device,
            "device_name": catalog.device_name(self.device),
            "device_known": self.device_known,
            "command": self.command,
            "command_known": catalog.is_known_command(self.device, self.command),
            "count": self.count,
            "first_seen": round(self.first_seen, 3),
            "last_seen": round(self.last_seen, 3),
            "peers": [{"address": p, "name": catalog.device_name(p)} for p in sorted(self.peers)],
            "requests": list(self.requests),
            "replies": list(self.replies),
        }


class DiscoveryTracker:
    """Bus listener that records unknown traffic. Safe to read from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[Tuple[int, int], Discovery] = {}

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or not is_unknown(packet):
            return
        subject = catalog.subject_device(packet.source, packet.destination)
        peer = packet.destination if subject == packet.source else packet.source
        with self._lock:
            item = self._items.get((subject, packet.command))
            if item is None:
                item = Discovery(subject, packet.command, first_seen=event.timestamp)
                self._items[(subject, packet.command)] = item
            item.count += 1
            item.last_seen = event.timestamp
            item.peers.add(peer)
            if packet.data:
                samples = item.replies if packet.source == subject else item.requests
                payload = packet.data.hex()
                if payload not in samples and len(samples) < MAX_SAMPLES:
                    samples.append(payload)

    def snapshot(self) -> List[Discovery]:
        """Copies of the discoveries, most frequent first."""
        with self._lock:
            items = [
                Discovery(d.device, d.command, d.count, d.first_seen, d.last_seen,
                          set(d.peers), list(d.requests), list(d.replies))
                for d in self._items.values()
            ]
        return sorted(items, key=lambda d: (-d.count, d.device, d.command))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def prune(self) -> int:
        """Forget entries the catalog can now name. Returns how many were removed."""
        with self._lock:
            known = [key for key, d in self._items.items()
                     if catalog.is_known_device(d.device) and catalog.is_known_command(d.device, d.command)
                     and all(catalog.is_known_device(peer) for peer in d.peers)]
            for key in known:
                del self._items[key]
        return len(known)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def to_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["device_id", "device", "command_id", "count", "first_seen", "last_seen",
                         "peers", "request_payloads", "reply_payloads"])
        for d in self.snapshot():
            writer.writerow([
                f"0x{d.device:02x}", catalog.device_name(d.device), f"0x{d.command:02x}", d.count,
                f"{d.first_seen:.3f}", f"{d.last_seen:.3f}",
                " ".join(f"0x{p:02x}" for p in sorted(d.peers)),
                " ".join(d.requests), " ".join(d.replies),
            ])
        return out.getvalue()
