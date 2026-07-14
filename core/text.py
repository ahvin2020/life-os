import re

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens. The single source for note/query tokenizing."""
    return _WORD_RE.findall((text or "").lower())
