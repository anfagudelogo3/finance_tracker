import re
import unicodedata


def normalize(text: str) -> str:
    """Lowercase and strip accents for accent-insensitive keyword matching."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def normalize_answer(text: str | None) -> str:
    """For matching short answers: normalized, no punctuation, single spaces."""
    cleaned = re.sub(r"[^\w\s]", " ", normalize(text or ""))
    return " ".join(cleaned.split())


_YES = {"1", "uno", "si", "s", "dale", "claro", "ok", "listo", "yes", "de una", "si claro",
        "confirmo", "si confirmo", "hagale", "hazlo", "si senor"}
_NO = {"2", "dos", "no", "n", "nop", "nel", "no gracias", "cancelar", "cancela", "mejor no"}


def is_yes(text: str | None) -> bool:
    return normalize_answer(text) in _YES


def is_no(text: str | None) -> bool:
    return normalize_answer(text) in _NO
