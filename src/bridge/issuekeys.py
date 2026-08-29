"""Jira issue-key extraction. Implemented by agent B.

Pure functions, no I/O.
"""

from __future__ import annotations

import logging
import re

from bridge.config import DEFAULT_ISSUE_KEY_REGEX

logger = logging.getLogger(__name__)


def compile_pattern(raw: str | None) -> re.Pattern[str]:
    """Compile ``raw`` (or the default) case-sensitively.

    Invalid patterns fall back to :data:`bridge.config.DEFAULT_ISSUE_KEY_REGEX`.
    """
    try:
        return re.compile(raw or DEFAULT_ISSUE_KEY_REGEX)
    except re.error as exc:
        logger.warning("invalid issue-key regex %r (%s); using default", raw, exc)
        return re.compile(DEFAULT_ISSUE_KEY_REGEX)


def extract(text: str, pattern: re.Pattern[str]) -> set[str]:
    """Return the set of upper-cased issue keys found in ``text`` (may be empty)."""
    return {m.group(0).upper() for m in pattern.finditer(text or "")}


def extract_many(pattern: re.Pattern[str], *texts: str) -> set[str]:
    """Union of :func:`extract` over every non-empty text."""
    keys: set[str] = set()
    for text in texts:
        if text:
            keys |= extract(text, pattern)
    return keys


__all__ = ["DEFAULT_ISSUE_KEY_REGEX", "compile_pattern", "extract", "extract_many"]
