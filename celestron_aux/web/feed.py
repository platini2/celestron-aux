"""Turns bus events into JSON-ready dicts and fans them out to browsers."""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from ..bus import BusEvent, Mark
from ..decoding import describe_packet
from ..discovery import is_unknown
from ..protocol import AuxPacket, CorruptFrame, StarSenseFrame
from ..starsense import parse_star_list
from ..wifi_bridge import ORIGIN_PREFIX

Item = Dict[str, Any]

DEFAULT_BACKLOG = 2000  # enough for a browser opened late to see the whole scan
_SUBSCRIBER_QUEUE = 2000
CLOSED = None  # sentinel put on subscriber queues when the feed shuts down


def event_to_item(event: BusEvent, seq: int) -> Item:
    item: Item = {
        "seq": seq,
        "t": round(event.timestamp, 6),
        "dir": event.direction.value,
        "raw": event.raw.hex(),
    }
    if event.origin and event.origin.startswith(ORIGIN_PREFIX):
        item["wifi"] = event.origin[len(ORIGIN_PREFIX):]  # sent by an app on the Wi-Fi dongle
    elif event.origin == "emulated":
        item["wifi"] = "reply"  # an emulator's reply that went only to the Wi-Fi apps
    frame = event.frame
    if isinstance(frame, AuxPacket):
        d = describe_packet(frame)
        item.update(
            kind="packet",
            sender=d.sender, sender_id=d.sender_id,
            receiver=d.receiver, receiver_id=d.receiver_id,
            command=d.command, command_id=d.command_id,
            value=d.text,  # decoded, or the payload hex when nothing decodes it
            payload=d.payload,
            malformed=d.malformed,
            unknown=is_unknown(frame),
        )
    elif isinstance(frame, StarSenseFrame):
        stars = parse_star_list(frame.payload)
        item.update(kind="stars", stars=[[round(s.x, 2), round(s.y, 2)] for s in stars])
    elif isinstance(frame, CorruptFrame):
        item.update(kind="corrupt", reason=frame.reason)
    elif isinstance(frame, Mark):
        item.update(kind="mark", text=frame.text)
    return item


class TrafficFeed:
    """Bus listener that keeps a backlog and pushes every event to subscribers.

    A subscriber that falls too far behind loses events rather than slowing
    the bus down.
    """

    def __init__(self, backlog: int = DEFAULT_BACKLOG) -> None:
        self._lock = threading.Lock()
        self._backlog: Deque[Item] = deque(maxlen=backlog)
        self._subscribers: Set["queue.Queue[Optional[Item]]"] = set()
        self._seq = 0
        self._latest_stars: Optional[Item] = None

    def __call__(self, event: BusEvent) -> None:
        with self._lock:
            self._seq += 1
            item = event_to_item(event, self._seq)
            self._backlog.append(item)
            if item.get("kind") == "stars":
                self._latest_stars = item
            subscribers = list(self._subscribers)
        self._publish(subscribers, item)

    @staticmethod
    def _publish(subscribers: List["queue.Queue[Optional[Item]]"], item: Item) -> None:
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                pass

    def note(self, text: str) -> None:
        """Add a status line (connected, disconnected, ...) to the traffic stream."""
        with self._lock:
            self._seq += 1
            item: Item = {"seq": self._seq, "t": None, "dir": "", "raw": "", "kind": "note", "text": text}
            self._backlog.append(item)
            subscribers = list(self._subscribers)
        self._publish(subscribers, item)

    def subscribe(self) -> Tuple["queue.Queue[Optional[Item]]", List[Item]]:
        """Return a new subscriber queue and the backlog to send first."""
        subscriber: "queue.Queue[Optional[Item]]" = queue.Queue(maxsize=_SUBSCRIBER_QUEUE)
        with self._lock:
            self._subscribers.add(subscriber)
            return subscriber, list(self._backlog)

    def unsubscribe(self, subscriber: "queue.Queue[Optional[Item]]") -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def latest_stars(self) -> Optional[Item]:
        with self._lock:
            return self._latest_stars

    def close(self) -> None:
        """Wake every subscriber so its stream can end."""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(CLOSED)
            except queue.Full:
                pass
