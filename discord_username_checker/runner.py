"""Orchestration : threads de vérification, statistiques, arrêt propre."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional, Set

from .checker import (
    FATAL_STATUSES,
    PROBLEM_STATUSES,
    STATUS_AVAILABLE,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_TAKEN,
    STATUS_UNKNOWN,
    CheckResult,
    UsernameChecker,
)
from .storage import ResultStore
from .webhook import AvailableNotifier


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass
class Stats:
    checked: int = 0
    available: int = 0
    taken: int = 0
    invalid: int = 0
    errors: int = 0
    unknown: int = 0
    skipped: int = 0
    cancelled: int = 0
    started_at: float = field(default_factory=time.monotonic)
    fatal: Optional[CheckResult] = None
    aborted_reason: str = ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def rate(self) -> float:
        elapsed = self.elapsed
        return self.checked / elapsed if elapsed > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.checked} vérifiés · {self.available} disponibles · {self.taken} pris · "
            f"{self.invalid} invalides · {self.errors} erreurs · {self.unknown} inconnus · "
            f"{self.skipped} ignorés (déjà faits) · durée {format_duration(self.elapsed)}"
        )


class Runner:
    """Fait tourner ``workers`` threads qui piochent dans l'itérateur de pseudos."""

    def __init__(
        self,
        checker: UsernameChecker,
        candidates: Iterator[str],
        total: Optional[int],
        *,
        workers: int = 2,
        store: Optional[ResultStore] = None,
        notifier: Optional[AvailableNotifier] = None,
        already_checked: Optional[Set[str]] = None,
        abort_after_problems: int = 20,
        progress_interval: float = 15.0,
        log_every_result: bool = False,
        logger: Optional[logging.Logger] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.checker = checker
        self.candidates = candidates
        self.total = total
        self.workers = max(1, workers)
        self.store = store
        self.notifier = notifier
        self.already_checked = already_checked or set()
        self.abort_after_problems = max(0, abort_after_problems)
        self.progress_interval = max(1.0, progress_interval)
        self.log_every_result = log_every_result
        self.logger = logger or logging.getLogger(__name__)
        self.stop_event = stop_event or threading.Event()
        self.stats = Stats()
        self._lock = threading.Lock()
        self._consecutive_problems = 0
        self._exhausted = False

    def _next_candidate(self) -> Optional[str]:
        with self._lock:
            while True:
                try:
                    name = next(self.candidates)
                except StopIteration:
                    self._exhausted = True
                    return None
                if name in self.already_checked:
                    self.stats.skipped += 1
                    continue
                return name

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            name = self._next_candidate()
            if name is None:
                return
            result = self.checker.check(name, self.stop_event)
            self._handle(result)

    def _handle(self, result: CheckResult) -> None:
        with self._lock:
            stats = self.stats
            if result.status == STATUS_CANCELLED:
                stats.cancelled += 1
                return
            stats.checked += 1
            if result.status == STATUS_AVAILABLE:
                stats.available += 1
            elif result.status == STATUS_TAKEN:
                stats.taken += 1
            elif result.status == STATUS_INVALID:
                stats.invalid += 1
            elif result.status == STATUS_ERROR:
                stats.errors += 1
            elif result.status == STATUS_UNKNOWN:
                stats.unknown += 1

            if result.status in PROBLEM_STATUSES:
                self._consecutive_problems += 1
            else:
                self._consecutive_problems = 0
            problems = self._consecutive_problems

        if self.store is not None and result.status not in FATAL_STATUSES:
            self.store.record(result)

        if result.available:
            self.logger.info("✅ DISPONIBLE : %s", result.username)
            if self.notifier is not None:
                self.notifier.add(result.username)
        elif self.log_every_result:
            self.logger.info("%s → %s %s", result.username, result.status, result.detail)
        else:
            self.logger.debug("%s → %s %s", result.username, result.status, result.detail)

        if result.fatal:
            with self._lock:
                first = self.stats.fatal is None
                if first:
                    self.stats.fatal = result
            if first:
                self.logger.critical("Arrêt : %s (%s)", result.detail, result.username)
            self.stop_event.set()
        elif self.abort_after_problems and problems >= self.abort_after_problems:
            reason = (
                f"{problems} erreurs consécutives : vérifiez la connexion, "
                "ou l'API a peut-être changé (voir le fichier de résultats)"
            )
            with self._lock:
                first = not self.stats.aborted_reason
                if first:
                    self.stats.aborted_reason = reason
            if first:
                self.logger.critical("Arrêt : %s", reason)
            self.stop_event.set()

    def _progress(self) -> str:
        stats = self.stats
        remaining_total = None
        if self.total is not None:
            remaining_total = max(0, self.total - stats.skipped)
        rate = stats.rate
        parts = []
        if remaining_total:
            percent = 100.0 * stats.checked / remaining_total
            parts.append(f"[{percent:5.1f}%] {stats.checked}/{remaining_total}")
        else:
            parts.append(f"{stats.checked} vérifiés")
        parts.append(f"dispo {stats.available}")
        parts.append(f"pris {stats.taken}")
        if stats.invalid:
            parts.append(f"invalides {stats.invalid}")
        if stats.errors or stats.unknown:
            parts.append(f"erreurs {stats.errors + stats.unknown}")
        paused = self.checker.rate_limiter.paused_for()
        if paused > 1:
            reprise = time.strftime("%H:%M:%S", time.localtime(time.time() + paused))
            parts.append(f"⏸ en pause (429) jusqu'à {reprise}, reste {format_duration(paused)}")
            return " · ".join(parts)
        parts.append(f"{rate:.2f} req/s")
        if remaining_total and rate > 0:
            eta = (remaining_total - stats.checked) / rate
            parts.append(f"ETA {format_duration(eta)}")
        return " · ".join(parts)

    def run(self) -> Stats:
        threads = [
            threading.Thread(target=self._worker, name=f"checker-{i + 1}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in threads:
            thread.start()
        last_report = time.monotonic()
        try:
            while any(t.is_alive() for t in threads):
                for thread in threads:
                    thread.join(timeout=0.5)
                if time.monotonic() - last_report >= self.progress_interval:
                    self.logger.info(self._progress())
                    last_report = time.monotonic()
        except KeyboardInterrupt:
            self.logger.warning("Interruption demandée : arrêt propre en cours…")
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=10.0)
        return self.stats
