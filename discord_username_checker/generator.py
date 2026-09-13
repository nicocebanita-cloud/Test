"""Génération et validation des pseudos à tester.

Règles Discord (nouveau système de pseudos) :
- 2 à 32 caractères ;
- uniquement des minuscules a-z, des chiffres, ``_`` et ``.`` ;
- pas deux points consécutifs ;
- certains mots sont réservés (``discord``, ``clyde``, ``everyone``, ``here``).
"""

from __future__ import annotations

import itertools
import random
from typing import Iterable, Iterator, Optional, Sequence, Tuple

LETTERS = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"
SPECIAL = "_."
ALLOWED_CHARS = LETTERS + DIGITS + SPECIAL

CHARSETS = {
    "letters": LETTERS,
    "alnum": LETTERS + DIGITS,
    "full": ALLOWED_CHARS,
}

MIN_LENGTH = 2
MAX_LENGTH = 32
RESERVED_WORDS: Tuple[str, ...] = ("discord", "clyde", "everyone", "here")

WILDCARD = "?"


def resolve_charset(value: str) -> str:
    """Convertit ``letters``/``alnum``/``full`` ou une liste de caractères en jeu de caractères."""
    key = (value or "").strip().lower()
    if key in CHARSETS:
        return CHARSETS[key]
    chars = "".join(dict.fromkeys(key))  # dédoublonne en gardant l'ordre
    if not chars:
        raise ValueError("le jeu de caractères est vide")
    bad = [c for c in chars if c not in ALLOWED_CHARS]
    if bad:
        raise ValueError(
            "caractères non autorisés par Discord dans le jeu de caractères : "
            + ", ".join(repr(c) for c in bad)
        )
    return chars


def validate_username(name: str) -> Optional[str]:
    """Retourne ``None`` si le pseudo respecte les règles Discord, sinon la raison."""
    if not MIN_LENGTH <= len(name) <= MAX_LENGTH:
        return f"longueur invalide ({MIN_LENGTH} à {MAX_LENGTH} caractères)"
    if any(c not in ALLOWED_CHARS for c in name):
        return "caractères non autorisés (a-z, 0-9, _ et . uniquement)"
    if ".." in name:
        return "deux points consécutifs interdits"
    for word in RESERVED_WORDS:
        if word in name:
            return f"contient le mot réservé « {word} »"
    return None


def is_valid_username(name: str) -> bool:
    return validate_username(name) is None


def generate_all(length: int, charset: str) -> Iterator[str]:
    """Toutes les combinaisons de ``length`` caractères pris dans ``charset``."""
    if length < 1:
        raise ValueError("la longueur doit être >= 1")
    for combo in itertools.product(charset, repeat=length):
        yield "".join(combo)


def generate_from_pattern(pattern: str, charset: str) -> Iterator[str]:
    """``a??z`` génère tous les pseudos commençant par ``a`` et finissant par ``z``."""
    pattern = pattern.lower()
    slots = [i for i, c in enumerate(pattern) if c == WILDCARD]
    fixed = list(pattern)
    if not slots:
        yield pattern
        return
    for combo in itertools.product(charset, repeat=len(slots)):
        for index, char in zip(slots, combo):
            fixed[index] = char
        yield "".join(fixed)


def count_pattern(pattern: str, charset: str) -> int:
    return len(charset) ** pattern.count(WILDCARD)


def read_wordlist(path: str) -> list:
    """Lit une liste de pseudos (un par ligne, ``#`` pour un commentaire), dédoublonnée."""
    names = []
    seen = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            name = line.strip().lower()
            if not name or name.startswith("#"):
                continue
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def build_candidates(
    *,
    length: int = 4,
    charset: str = "letters",
    pattern: Optional[str] = None,
    wordlist: Optional[str] = None,
    explicit: Optional[Sequence[str]] = None,
    shuffle: bool = False,
    seed: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[Iterator[str], Optional[int]]:
    """Construit l'itérateur de pseudos à tester et une estimation du total.

    Priorité : ``explicit`` > ``wordlist`` > ``pattern`` > combinaisons complètes.
    Le total est une estimation (les pseudos invalides sont filtrés à la volée).
    """
    chars = resolve_charset(charset)

    if explicit:
        names = list(dict.fromkeys(n.strip().lower() for n in explicit if n.strip()))
        source: Iterable[str] = names
        total: Optional[int] = len(names)
    elif wordlist:
        names = read_wordlist(wordlist)
        source = names
        total = len(names)
    elif pattern:
        source = generate_from_pattern(pattern, chars)
        total = count_pattern(pattern, chars)
    else:
        source = generate_all(length, chars)
        total = len(chars) ** length

    if shuffle:
        materialised = list(source)
        random.Random(seed).shuffle(materialised)
        source = materialised
        total = len(materialised)

    if limit is not None:
        if limit < 0:
            raise ValueError("la limite doit être >= 0")
        source = itertools.islice(source, limit)
        total = min(total, limit) if total is not None else limit

    return iter(source), total
