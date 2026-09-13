"""Vérification d'un pseudo via l'endpoint public utilisé par la page d'inscription Discord.

Aucun token de compte n'est nécessaire : l'endpoint est celui que le site web appelle
lorsqu'un visiteur tape un pseudo dans le formulaire d'inscription.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from .http import (
    HttpResponse,
    NetworkError,
    build_opener,
    extract_error_message,
    parse_retry_after,
    post_json,
)
from .ratelimit import RateLimiter, sleep_interruptible

DEFAULT_ENDPOINT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

STATUS_AVAILABLE = "available"
STATUS_TAKEN = "taken"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
STATUS_CAPTCHA = "captcha"
STATUS_BLOCKED = "blocked"
STATUS_CANCELLED = "cancelled"

# Statuts définitifs : inutile de re-tester le pseudo lors d'une reprise.
FINAL_STATUSES = frozenset({STATUS_AVAILABLE, STATUS_TAKEN, STATUS_INVALID})
# Statuts qui doivent arrêter toute la vérification (continuer serait inutile).
FATAL_STATUSES = frozenset({STATUS_CAPTCHA, STATUS_BLOCKED})
# Statuts comptés comme « problème » pour l'arrêt automatique.
PROBLEM_STATUSES = frozenset({STATUS_ERROR, STATUS_UNKNOWN})

MAX_PAUSE_SECONDS = 900.0


@dataclass
class CheckResult:
    username: str
    status: str
    detail: str = ""
    http_status: Optional[int] = None

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    @property
    def fatal(self) -> bool:
        return self.status in FATAL_STATUSES


class UsernameChecker:
    """Interroge Discord pour savoir si un pseudo est pris, en respectant les limites."""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        rate_limiter: Optional[RateLimiter] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        max_rate_limit_retries: int = 10,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        extra_headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.endpoint = endpoint
        self.rate_limiter = rate_limiter or RateLimiter(2.0)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self.base_backoff = max(0.0, base_backoff)
        self.max_backoff = max(0.0, max_backoff)
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/register",
        }
        if extra_headers:
            self.headers.update(extra_headers)
        self.logger = logger or logging.getLogger(__name__)
        self._opener = build_opener(proxy)
        self.rate_limit_hits = 0
        self._stats_lock = threading.Lock()

    # Isolé pour pouvoir être remplacé dans les tests.
    def _post(self, username: str) -> HttpResponse:
        return post_json(
            self._opener,
            self.endpoint,
            {"username": username},
            self.headers,
            self.timeout,
        )

    def _backoff(self, attempt: int, stop_event: Optional[threading.Event]) -> bool:
        delay = min(self.max_backoff, self.base_backoff * (2 ** max(0, attempt - 1)))
        delay += random.uniform(0, delay * 0.25) if delay else 0.0
        return sleep_interruptible(delay, stop_event)

    def check(self, username: str, stop_event: Optional[threading.Event] = None) -> CheckResult:
        """Vérifie un pseudo. Ne lève jamais : les problèmes sont décrits dans ``CheckResult``."""
        network_attempts = 0
        rate_limit_attempts = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return CheckResult(username, STATUS_CANCELLED)
            if not self.rate_limiter.acquire(stop_event):
                return CheckResult(username, STATUS_CANCELLED)

            try:
                response = self._post(username)
            except NetworkError as error:
                network_attempts += 1
                if network_attempts > self.max_retries:
                    return CheckResult(username, STATUS_ERROR, f"réseau : {error}")
                self.logger.debug("%s : erreur réseau (%s), nouvel essai %d", username, error, network_attempts)
                if not self._backoff(network_attempts, stop_event):
                    return CheckResult(username, STATUS_CANCELLED)
                continue

            status = response.status
            data = response.json()

            if status == 200:
                return self._interpret_success(username, response, data)

            if status == 429:
                rate_limit_attempts += 1
                with self._stats_lock:
                    self.rate_limit_hits += 1
                delay = min(MAX_PAUSE_SECONDS, parse_retry_after(response)) + 0.5
                level = logging.DEBUG if self.rate_limiter.paused_for() > 0 else logging.WARNING
                self.logger.log(
                    level, "Limite de débit atteinte (429) : pause de %.1f s pour tous les threads", delay
                )
                if rate_limit_attempts > self.max_rate_limit_retries:
                    return CheckResult(username, STATUS_ERROR, "trop de 429 consécutifs", status)
                self.rate_limiter.pause(delay)
                continue

            if status == 400:
                if isinstance(data, dict) and "captcha_key" in data:
                    return CheckResult(
                        username,
                        STATUS_CAPTCHA,
                        "Discord exige un captcha : ralentissez (--rps) ou changez d'adresse IP",
                        status,
                    )
                message = extract_error_message(data) or response.body[:200]
                return CheckResult(username, STATUS_INVALID, message, status)

            if status in (401, 403):
                return CheckResult(
                    username,
                    STATUS_BLOCKED,
                    f"accès refusé (HTTP {status}) : {response.body[:200]}",
                    status,
                )

            if status >= 500:
                network_attempts += 1
                if network_attempts > self.max_retries:
                    return CheckResult(username, STATUS_ERROR, f"HTTP {status}", status)
                if not self._backoff(network_attempts, stop_event):
                    return CheckResult(username, STATUS_CANCELLED)
                continue

            return CheckResult(username, STATUS_ERROR, f"HTTP {status} : {response.body[:200]}", status)

    @staticmethod
    def _interpret_success(username: str, response: HttpResponse, data: object) -> CheckResult:
        if isinstance(data, dict) and isinstance(data.get("taken"), bool):
            if data["taken"]:
                return CheckResult(username, STATUS_TAKEN, http_status=200)
            return CheckResult(username, STATUS_AVAILABLE, http_status=200)
        return CheckResult(
            username,
            STATUS_UNKNOWN,
            f"réponse inattendue (l'API a peut-être changé) : {response.body[:200]}",
            200,
        )
