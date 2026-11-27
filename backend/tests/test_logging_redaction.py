import io
import json
import logging

from backend.logging.setup import REDACTED, JsonFormatter, RedactingFilter, redact_text, register_secret


def _logger():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(RedactingFilter())
    h.setFormatter(JsonFormatter())
    lg = logging.getLogger("redaction-test")
    lg.handlers[:] = [h]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg, buf


def test_patterns_in_message():
    lg, buf = _logger()
    lg.info("login api_key=abc123def access_token: 'tok_987654' Authorization: token key:secretpart")
    out = buf.getvalue()
    for s in ("abc123def", "tok_987654", "secretpart"):
        assert s not in out
    assert REDACTED in out


def test_extra_fields_and_nested():
    lg, buf = _logger()
    lg.info("profile", extra={"access_token": "zzz111", "payload": {"api_secret": "qqq222", "ok": 1}})
    rec = json.loads(buf.getvalue())
    assert rec["access_token"] == REDACTED
    assert rec["payload"]["api_secret"] == REDACTED
    assert rec["payload"]["ok"] == 1


def test_registered_secret_masked_anywhere():
    register_secret("RANDOMTOKENVALUE42")
    assert "RANDOMTOKENVALUE42" not in redact_text("url?x=RANDOMTOKENVALUE42&y=1")


def test_exception_text_redacted():
    lg, buf = _logger()
    try:
        raise RuntimeError("failed with password=hunter2xyz")
    except RuntimeError:
        lg.exception("boom")
    assert "hunter2xyz" not in buf.getvalue()


def test_percent_args_redacted():
    lg, buf = _logger()
    lg.info("token=%s", "abcdef987")
    assert "abcdef987" not in buf.getvalue()
