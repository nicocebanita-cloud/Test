"""Envoi des pseudos disponibles vers un webhook Discord, par lots."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Iterable, List, Optional

from . import __version__
from .http import HttpResponse, NetworkError, build_opener, parse_retry_after, post_json
from .ratelimit import sleep_interruptible

WEBHOOK_URL_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+/?$"
)
MAX_CONTENT_LENGTH = 2000
# Discord refuse les noms de webhook contenant « discord » ou « clyde ».
DEFAULT_BOT_NAME = "Vérificateur de pseudos"
FORBIDDEN_NAME_WORDS = ("discord", "clyde")
MAX_BOT_NAME_LENGTH = 80
# Sans User-Agent explicite, urllib se présente comme « Python-urllib », que Cloudflare
# refuse devant Discord (erreur 1010). Format recommandé par la documentation Discord.
DEFAULT_WEBHOOK_USER_AGENT = f"DiscordBot (https://github.com/nicocebanita-cloud/Test, {__version__})"


def validate_webhook_url(url: str) -> bool:
    return bool(url) and WEBHOOK_URL_RE.match(url.strip()) is not None


def sanitize_bot_name(name: str) -> str:
    """Nom affiché acceptable par Discord : sans mot interdit, 80 caractères maximum."""
    cleaned = (name or "").strip()[:MAX_BOT_NAME_LENGTH]
    if not cleaned or any(word in cleaned.lower() for word in FORBIDDEN_NAME_WORDS):
        return DEFAULT_BOT_NAME
    return cleaned


def mask_webhook_url(url: str) -> str:
    """Masque le token du webhook pour les journaux."""
    match = re.match(r"^(https://[^/]+/api/(?:v\d+/)?webhooks/\d+)/", url or "")
    return f"{match.group(1)}/…" if match else "<url invalide>"


def render_available_message(chunk_names: List[str], header: Optional[str] = None) -> str:
    title = header or f"✅ **{len(chunk_names)} pseudo(s) disponible(s)**"
    return f"{title}\n```\n" + "\n".join(chunk_names) + "\n```"


def chunk_available_names(names: Iterable[str], header: Optional[str] = None) -> List[List[str]]:
    """Découpe les pseudos en lots dont le message rendu tient dans 2000 caractères."""
    names = [n for n in names if n]
    chunks: List[List[str]] = []
    chunk: List[str] = []
    for name in names:
        if len(render_available_message(chunk + [name], header)) > MAX_CONTENT_LENGTH and chunk:
            chunks.append(chunk)
            chunk = [name]
        else:
            chunk.append(name)
    if chunk:
        chunks.append(chunk)
    return chunks


def format_available_messages(names: Iterable[str], header: Optional[str] = None) -> List[str]:
    """Met en forme les pseudos disponibles en messages de moins de 2000 caractères."""
    return [render_available_message(chunk, header) for chunk in chunk_available_names(names, header)]


class DiscordWebhook:
    """Client minimal pour un webhook Discord (gestion des 429 et des erreurs serveur)."""

    def __init__(
        self,
        url: str,
        *,
        bot_name: str = DEFAULT_BOT_NAME,
        timeout: float = 15.0,
        max_retries: int = 5,
        proxy: Optional[str] = None,
        user_agent: str = DEFAULT_WEBHOOK_USER_AGENT,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not validate_webhook_url(url):
            raise ValueError(
                "URL de webhook invalide : attendu https://discord.com/api/webhooks/<id>/<token>"
            )
        self.url = url.strip()
        self.bot_name = sanitize_bot_name(bot_name)
        if self.bot_name != (bot_name or "").strip():
            (logger or logging.getLogger(__name__)).warning(
                "Nom de webhook « %s » refusé par Discord (mot interdit ou trop long) : « %s » sera utilisé",
                bot_name,
                self.bot_name,
            )
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.headers = {"User-Agent": user_agent}
        self.logger = logger or logging.getLogger(__name__)
        self._opener = build_opener(proxy)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    # Isolé pour les tests.
    def _post(self, payload: dict) -> HttpResponse:
        return post_json(self._opener, self.url, payload, self.headers, self.timeout)

    def send(self, content: str, stop_event: Optional[threading.Event] = None) -> bool:
        """Envoie un message texte. Retourne ``True`` si Discord l'a accepté."""
        if len(content) > MAX_CONTENT_LENGTH:
            content = content[: MAX_CONTENT_LENGTH - 1] + "…"
        payload = {
            "content": content,
            "username": self.bot_name,
            # Aucune mention ne doit être déclenchée par le contenu envoyé.
            "allowed_mentions": {"parse": []},
        }
        with self._lock:
            attempt = 0
            while True:
                wait = self._next_allowed - time.monotonic()
                if wait > 0:
                    sleep_interruptible(wait, stop_event)
                try:
                    response = self._post(payload)
                except NetworkError as error:
                    attempt += 1
                    if attempt > self.max_retries:
                        self.logger.error("Webhook : erreur réseau définitive (%s)", error)
                        return False
                    sleep_interruptible(min(30.0, 2.0 ** attempt), stop_event)
                    continue

                self._update_bucket(response)

                if response.status in (200, 204):
                    return True
                if response.status == 429:
                    attempt += 1
                    delay = parse_retry_after(response, default=2.0) + 0.25
                    self.logger.warning("Webhook : limite de débit, attente %.1f s", delay)
                    if attempt > self.max_retries:
                        self.logger.error("Webhook : trop de 429, message abandonné")
                        return False
                    sleep_interruptible(delay, stop_event)
                    continue
                if response.status >= 500:
                    attempt += 1
                    if attempt > self.max_retries:
                        self.logger.error("Webhook : erreur serveur HTTP %d", response.status)
                        return False
                    sleep_interruptible(min(30.0, 2.0 ** attempt), stop_event)
                    continue
                if response.status == 403 and "cloudflare" in response.body.lower():
                    self.logger.error(
                        "Webhook : requête bloquée par Cloudflare (HTTP 403). Vérifiez VPN, proxy ou "
                        "pare-feu, et que le programme est à jour. Détail : %s",
                        response.body[:200],
                    )
                    return False
                self.logger.error(
                    "Webhook : refusé (HTTP %d) : %s", response.status, response.body[:200]
                )
                return False

    def _update_bucket(self, response: HttpResponse) -> None:
        remaining = response.header("x-ratelimit-remaining")
        reset_after = response.header("x-ratelimit-reset-after")
        if remaining == "0" and reset_after:
            try:
                self._next_allowed = time.monotonic() + float(reset_after)
            except ValueError:
                pass


class AvailableNotifier:
    """Accumule les pseudos disponibles et les envoie par lots au webhook."""

    def __init__(
        self,
        webhook: DiscordWebhook,
        *,
        batch_size: int = 25,
        flush_interval: float = 60.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.webhook = webhook
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(1.0, flush_interval)
        self.logger = logger or logging.getLogger(__name__)
        self._pending: List[str] = []
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sent = 0
        self.failed = 0

    def add(self, username: str) -> None:
        flush_now = False
        with self._lock:
            self._pending.append(username)
            if len(self._pending) >= self.batch_size:
                flush_now = True
        if flush_now:
            self.flush()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self) -> None:
        with self._send_lock:
            with self._lock:
                batch = self._pending
                self._pending = []
                self._last_flush = time.monotonic()
            if not batch:
                return
            for chunk in chunk_available_names(batch):
                if self.webhook.send(render_available_message(chunk)):
                    self.sent += len(chunk)
                else:
                    self.failed += len(chunk)
                    self.logger.error(
                        "Webhook : envoi impossible, pseudos non transmis (conservés dans le fichier) : %s",
                        ", ".join(chunk),
                    )

    def send_text(self, text: str) -> bool:
        with self._send_lock:
            return self.webhook.send(text)

    def start(self) -> None:
        """Démarre un thread qui vide le lot toutes les ``flush_interval`` secondes."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="webhook-flush", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                due = (
                    self._pending
                    and time.monotonic() - self._last_flush >= self.flush_interval
                )
            if due:
                self.flush()
