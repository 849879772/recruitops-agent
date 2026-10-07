"""Locate presentation-equivalent evidence without changing its meaning.

Whitespace and fullwidth ASCII are presentation differences. Punctuation,
case, negation, numbers, job identifiers and every other character remain
significant. Returned offsets always address the original persisted text.
"""
from __future__ import annotations


def _indexed_text(text: str) -> tuple[str, list[int]]:
    characters, offsets = [], []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        code = ord(character)
        characters.append(chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else character)
        offsets.append(index)
    return "".join(characters), offsets


def evidence_text_key(text: str) -> str:
    return _indexed_text(text)[0]


def evidence_spans(source: str, fragment: str) -> list[tuple[int, int]]:
    """Return every matching half-open original span, never approximate text."""
    text, offsets = _indexed_text(source)
    needle = evidence_text_key(fragment)
    if not needle:
        return []
    spans, cursor = [], 0
    while (start := text.find(needle, cursor)) >= 0:
        spans.append((offsets[start], offsets[start + len(needle) - 1] + 1))
        cursor = start + 1
    return spans


def localize_evidence(source: str, fragment: str) -> str | None:
    """Recover literal evidence; ambiguous differing renderings need a scope."""
    found = {source[start:end] for start, end in evidence_spans(source, fragment)}
    return next(iter(found)) if len(found) == 1 else None
