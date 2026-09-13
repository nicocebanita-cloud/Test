"""Enregistrement des résultats sur disque et reprise après interruption."""

from __future__ import annotations

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional, Set

from .checker import FINAL_STATUSES, CheckResult

CSV_FIELDS = ("username", "status", "http_status", "detail", "checked_at")


class ResultStore:
    """Écrit chaque résultat dans un CSV et les pseudos disponibles dans un fichier texte."""

    def __init__(
        self,
        results_path: str,
        available_path: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.results_path = results_path
        self.available_path = available_path
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._results_file = None
        self._available_file = None
        self._writer = None

    def load_checked(self) -> Set[str]:
        """Pseudos déjà vérifiés avec un statut définitif (à ignorer lors d'une reprise)."""
        checked: Set[str] = set()
        if not os.path.exists(self.results_path):
            return checked
        with open(self.results_path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                username = (row.get("username") or "").strip()
                status = (row.get("status") or "").strip()
                if username and status in FINAL_STATUSES:
                    checked.add(username)
        return checked

    def open(self) -> None:
        for path in (self.results_path, self.available_path):
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        write_header = not os.path.exists(self.results_path) or os.path.getsize(self.results_path) == 0
        self._results_file = open(self.results_path, "a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._results_file)
        if write_header:
            self._writer.writerow(CSV_FIELDS)
            self._results_file.flush()
        self._available_file = open(self.available_path, "a", encoding="utf-8")

    def record(self, result: CheckResult) -> None:
        if self._writer is None:
            self.open()
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._writer.writerow(
                [result.username, result.status, result.http_status or "", result.detail, stamp]
            )
            self._results_file.flush()
            if result.available:
                self._available_file.write(result.username + "\n")
                self._available_file.flush()

    def close(self) -> None:
        with self._lock:
            for handle in (self._results_file, self._available_file):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:  # pragma: no cover
                        pass
            self._results_file = None
            self._available_file = None
            self._writer = None

    def __enter__(self) -> "ResultStore":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
