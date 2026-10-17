"""Session-relative timestamps."""

from __future__ import annotations

import threading
import time
from typing import Callable


class SessionClock:
    """Seconds elapsed since the session started (or since the last reset)."""

    def __init__(self, time_source: Callable[[], float] = time.monotonic) -> None:
        self._time_source = time_source
        self._lock = threading.Lock()
        self._origin = time_source()

    def now(self) -> float:
        with self._lock:
            return self._time_source() - self._origin

    def reset(self) -> None:
        with self._lock:
            self._origin = self._time_source()
