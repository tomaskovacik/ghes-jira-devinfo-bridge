from __future__ import annotations

import re

from bridge.config import DEFAULT_ISSUE_KEY_REGEX
from bridge.issuekeys import compile_pattern, extract, extract_many


def test_extract_basic() -> None:
    pat = compile_pattern(None)
    assert extract("fix ABC-1 and ABC-2", pat) == {"ABC-1", "ABC-2"}


def test_extract_upper_cases_result() -> None:
    # Default regex is case-sensitive; feed an already-upper key with lower digits noise.
    pat = compile_pattern(r"[A-Za-z][A-Za-z0-9]+-\d+")
    assert extract("see abc-9, Def-10", pat) == {"ABC-9", "DEF-10"}


def test_extract_no_match_is_empty_set() -> None:
    pat = compile_pattern(None)
    assert extract("nothing to see here", pat) == set()
    assert extract("", pat) == set()
    assert extract(None, pat) == set()


def test_extract_dedupes() -> None:
    pat = compile_pattern(None)
    assert extract("ABC-1 ABC-1 ABC-1", pat) == {"ABC-1"}


def test_extract_many_union_skips_falsy() -> None:
    pat = compile_pattern(None)
    assert extract_many(pat, "ABC-1", "", None, "ABC-2 XY-9") == {
        "ABC-1",
        "ABC-2",
        "XY-9",
    }
    assert extract_many(pat) == set()


def test_compile_pattern_default_when_none() -> None:
    assert compile_pattern(None).pattern == DEFAULT_ISSUE_KEY_REGEX


def test_compile_pattern_bad_regex_falls_back(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        pat = compile_pattern(r"([A-Z")  # unbalanced -> re.error
    assert pat.pattern == DEFAULT_ISSUE_KEY_REGEX
    assert any("invalid issue-key regex" in r.message for r in caplog.records)


def test_compile_pattern_custom_regex_used() -> None:
    pat = compile_pattern(r"ISSUE_\d+")
    assert isinstance(pat, re.Pattern)
    assert extract("ISSUE_42 here", pat) == {"ISSUE_42"}


def test_default_regex_is_case_sensitive() -> None:
    pat = compile_pattern(None)
    assert extract("abc-1", pat) == set()
