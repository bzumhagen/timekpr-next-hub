"""Unit tests for `_prompt_or_die`, the interactive-input helper `enroll`
uses for --hub-url and --code (main.py) -- isolated from the rest of
`_cmd_enroll`, which needs a real/mocked timekpr install to exercise
end-to-end."""

from __future__ import annotations

import pytest
from timekpr_hub_agent.main import _prompt_or_die


def test_prompt_or_die_returns_the_value_unprompted_when_given():
    assert _prompt_or_die("http://hub.local", label="Hub URL", flag="--hub-url") == "http://hub.local"


def test_prompt_or_die_prompts_when_missing_and_stdin_is_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  typed-value  ")
    assert _prompt_or_die(None, label="Enrollment code", flag="--code") == "typed-value"


def test_prompt_or_die_raises_a_clean_error_when_missing_and_not_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="--code is required when not running interactively"):
        _prompt_or_die(None, label="Enrollment code", flag="--code")


def test_prompt_or_die_raises_when_the_prompt_is_answered_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "   ")
    with pytest.raises(SystemExit, match="no enrollment code given"):
        _prompt_or_die(None, label="Enrollment code", flag="--code")
