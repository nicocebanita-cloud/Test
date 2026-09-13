"""Limiteur de débit partagé entre les threads, avec pause globale sur HTTP 429."""

from __future__ import annotations

import threading
import time
from typing import Optional


def sleep_interruptible(seconds: float, stop_event: Optional[threading.Event] = None) -> bool:
    """Dort ``seconds`` secondes par tranches, s'arrête tôt si ``stop_event`` est levé.

    Retourne ``True`` si l'attente s'est terminée normalement, ``False`` si elle a été interrompue.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(min(remaining, 0.25))


class RateLimiter:
    """Espace les requêtes à ``rps`` requêtes/seconde, tous threads confondus."""

    def __init__(self, rps: float) -> None:
        self.set_rate(rps)
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._pause_until = 0.0

    def set_rate(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError("rps doit être > 0")
        self._interval = 1.0 / rps

    @property
    def interval(self) -> float:
        return self._interval

    def pause(self, seconds: float) -> None:
        """Bloque toutes les requêtes pendant ``seconds`` (utilisé sur un 429)."""
        with self._lock:
            until = time.monotonic() + max(0.0, seconds)
            self._pause_until = max(self._pause_until, until)
            self._next_slot = max(self._next_slot, self._pause_until)

    def paused_for(self) -> float:
        """Durée de pause restante, en secondes (0 si aucune pause active)."""
        with self._lock:
            return max(0.0, self._pause_until - time.monotonic())

    def acquire(self, stop_event: Optional[threading.Event] = None) -> bool:
        """Attend son tour. Retourne ``False`` si ``stop_event`` a été levé pendant l'attente."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot, self._pause_until)
            self._next_slot = slot + self._interval
        while True:
            with self._lock:
                target = max(slot, self._pause_until)
            now = time.monotonic()
            if now >= target:
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            time.sleep(min(target - now, 0.25))
