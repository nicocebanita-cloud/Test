"""Petite couche HTTP basée sur ``urllib`` (aucune dépendance externe)."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class NetworkError(Exception):
    """Erreur réseau ou délai dépassé (à retenter)."""


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""

    def json(self) -> Optional[Any]:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)


def build_opener(proxy: Optional[str] = None) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def post_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: Any,
    headers: Dict[str, str],
    timeout: float,
) -> HttpResponse:
    """POST JSON ; les codes 4xx/5xx sont renvoyés comme réponses, pas comme exceptions."""
    data = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return HttpResponse(
                status=response.status,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
            )
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - lecture impossible du corps
            body = ""
        return HttpResponse(
            status=error.code,
            headers={k.lower(): v for k, v in error.headers.items()},
            body=body,
        )
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as error:
        raise NetworkError(str(error)) from error


def parse_retry_after(response: HttpResponse, default: float = 5.0) -> float:
    """Extrait le délai d'attente d'une réponse 429 (en-tête ou corps JSON), en secondes."""
    header = response.header("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    data = response.json()
    if isinstance(data, dict) and "retry_after" in data:
        try:
            value = float(data["retry_after"])
        except (TypeError, ValueError):
            return default
        # Les anciennes versions de l'API renvoyaient des millisecondes.
        if value > 300:
            value = value / 1000.0
        return max(0.0, value)
    return default


def extract_error_message(data: Any) -> str:
    """Rend lisible un corps d'erreur Discord (``{"code":50035,"errors":{...}}``)."""
    if not isinstance(data, dict):
        return ""
    errors = data.get("errors")
    if isinstance(errors, dict):
        message = _first_nested_error(errors)
        if message:
            return message
    message = data.get("message")
    return str(message) if message else ""


def _first_nested_error(node: Any) -> str:
    if isinstance(node, dict):
        entries = node.get("_errors")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    code = entry.get("code", "")
                    message = entry.get("message", "")
                    return f"{code}: {message}".strip(": ")
        for value in node.values():
            found = _first_nested_error(value)
            if found:
                return found
    return ""
