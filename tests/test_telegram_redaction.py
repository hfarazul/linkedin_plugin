"""Telegram bot tokens must never reach a log file.

The token is embedded in every Telegram API URL, and httpx logs request URLs
at INFO. The bot daemon enables INFO logging, so before this filter existed
the daemon wrote its live token to data/bot-daemon.err.log every ~25 seconds
— which is how it ended up committed to a public repository.

These tests exercise the redaction filter against the exact record shapes
httpx produces, including the httpx.URL object (not a str) it passes as a
logging argument.
"""

from __future__ import annotations

import logging

import pytest

from linkedin_agent.telegram import (
    RedactBotToken,
    _redact,
    install_log_redaction,
)

# Shaped like a real token (digits, colon, 35-char secret) but not a real one.
FAKE_TOKEN = "8832515932:AAFvChNSFx_F6LaV955OB4RUC0ErdHjmunA"
FAKE_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"


def _record(msg, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


# ----------------------------- _redact ---------------------------------------

@pytest.mark.unit
def test_redact_strips_token_from_string() -> None:
    assert FAKE_TOKEN not in _redact(FAKE_URL)
    assert "/bot<redacted>" in _redact(FAKE_URL)


@pytest.mark.unit
def test_redact_handles_non_string_url_object() -> None:
    """httpx passes an httpx.URL, not a str — the arg must still be redacted."""
    class FakeURL:
        def __str__(self) -> str:
            return FAKE_URL

    out = _redact(FakeURL())
    assert isinstance(out, str)
    assert FAKE_TOKEN not in out


@pytest.mark.unit
def test_redact_leaves_other_args_untouched() -> None:
    """Status codes must stay ints so %d formatting still works."""
    assert _redact(200) == 200
    assert _redact("GET") == "GET"
    assert _redact(None) is None


@pytest.mark.unit
def test_redact_preserves_surrounding_url() -> None:
    out = _redact(FAKE_URL)
    assert out.startswith("https://api.telegram.org")
    assert out.endswith("/getUpdates")


# ----------------------------- the filter ------------------------------------

@pytest.mark.unit
def test_filter_redacts_token_in_message() -> None:
    rec = _record(f"HTTP Request: POST {FAKE_URL}")
    assert RedactBotToken().filter(rec) is True
    assert FAKE_TOKEN not in rec.getMessage()


@pytest.mark.unit
def test_filter_redacts_token_in_args() -> None:
    """The exact shape httpx emits: URL travels as a %s argument."""
    rec = _record('HTTP Request: %s %s "%s %d %s"',
                  ("POST", FAKE_URL, "HTTP/1.1", 200, "OK"))
    assert RedactBotToken().filter(rec) is True
    rendered = rec.getMessage()
    assert FAKE_TOKEN not in rendered
    assert "/bot<redacted>" in rendered
    assert "200" in rendered      # non-string args survive formatting


@pytest.mark.unit
def test_filter_redacts_dict_args() -> None:
    # A mapping arg is passed to LogRecord wrapped in a tuple; LogRecord
    # unwraps it to record.args itself. Mirror that shape exactly.
    rec = _record("request to %(url)s", ({"url": FAKE_URL},))
    assert isinstance(rec.args, dict), "expected LogRecord to unwrap the mapping"
    assert RedactBotToken().filter(rec) is True
    assert FAKE_TOKEN not in rec.getMessage()


# ----------------------------- end to end ------------------------------------

@pytest.mark.unit
def test_no_token_reaches_a_handler(caplog) -> None:
    """The whole point: nothing token-shaped is emitted downstream."""
    install_log_redaction()
    logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logger.info('HTTP Request: %s %s "%s %d %s"',
                    "POST", FAKE_URL, "HTTP/1.1", 200, "OK")
    assert caplog.records, "expected the record to be emitted, not dropped"
    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()
    assert FAKE_TOKEN not in caplog.text


@pytest.mark.unit
def test_install_is_idempotent() -> None:
    """Called from every TelegramClient construction — must not stack filters."""
    install_log_redaction()
    before = len(logging.getLogger("httpx").filters)
    install_log_redaction()
    install_log_redaction()
    assert len(logging.getLogger("httpx").filters) == before
