"""Golden rule #4 — no secrets in logs.

Three layers, and all three are regression tests for real leaks rather than hypotheticals:

**The key pass.** Any key whose name contains a sensitive token loses its value, recursively
through nested dicts *and* through lists, tuples and sets. A dict one level down inside a
list is the shape a real payload takes (``providers=[{"name": ..., "api_key": ...}]``), so
that exact structure is asserted.

**The value pass.** ``scrub_text`` catches what a key name cannot predict. The motivating
case is ordinary: ``logger.warning("mailbox.failed", error=str(exc))``. ``error`` is not a
sensitive name, so the key pass waves it through — yet the string can carry the user's email
address, a bearer token, or an ``access_token=`` query parameter.

**The traceback.** ``docs/SAFETY.md`` records that frame-locals capture "shipped once here
and was fixed; the fix has been verified manually … but is not yet covered by a committed
regression test." :func:`test_secret_in_a_traceback_frame_local_never_reaches_the_log` is
that test. It goes through the *configured* pipeline and reads the bytes that actually reach
the stream, because a unit test of ``redact_secrets`` alone cannot see the renderer — and
the renderer is where the bug was.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from app.config.logging import (
    EMAIL_MASK,
    REDACTED,
    configure_logging,
    redact_secrets,
    scrub_text,
)

#: A credential distinctive enough that finding it anywhere in the output is unambiguous.
SECRET = "sk-live-511f3ab9c0de4d7e8f21"


def _redact(payload: dict) -> dict:
    """Run the processor exactly as structlog does."""
    return dict(redact_secrets(None, "info", dict(payload)))


# ======================================================================================
# The key pass
# ======================================================================================


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "x-api-key",
        "secret",
        "client_secret",
        "authorization",
        "x-auth",
        "cookie",
        "session_cookie",
        "ssn",
        "dob",
        "credit_card",
    ],
)
def test_sensitive_keys_lose_their_value(key: str) -> None:
    """Every contract-mandated pattern scrubs, including compound spellings."""
    assert _redact({key: SECRET})[key] == REDACTED


@pytest.mark.parametrize("key", ["event", "user_id", "provider", "correlation_id", "posting_id"])
def test_contract_bound_context_keys_survive(key: str) -> None:
    """The seven bound context keys of §16 must remain readable.

    ``session_id`` in particular: scrubbing it makes a run untraceable through its own logs
    and feeds ``***redacted***`` into a GUID column.
    """
    assert _redact({key: "abc-123"})[key] == "abc-123"


def test_session_id_is_not_redacted() -> None:
    """Named separately because it was wrongly scrubbed once (``OPEN_QUESTIONS`` item 1)."""
    value = "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert _redact({"session_id": value})["session_id"] == value


def test_nested_dictionaries_are_walked() -> None:
    """A secret three levels down is scrubbed."""
    event = {
        "event": "provider.configured",
        "config": {
            "endpoint": "https://api.example.com",
            "credentials": {"api_key": SECRET, "region": "us-east-1"},
        },
    }
    result = _redact(event)

    assert result["config"]["credentials"]["api_key"] == REDACTED
    assert result["config"]["credentials"]["region"] == "us-east-1"
    assert SECRET not in json.dumps(result)


def test_dicts_inside_lists_are_walked() -> None:
    """The shape a real payload takes: a list of provider descriptors."""
    event = {
        "event": "plugins.loaded",
        "providers": [
            {"name": "greenhouse", "token": SECRET},
            {"name": "lever", "api_key": SECRET},
        ],
    }
    result = _redact(event)

    assert result["providers"][0]["token"] == REDACTED
    assert result["providers"][1]["api_key"] == REDACTED
    assert result["providers"][0]["name"] == "greenhouse"
    assert SECRET not in json.dumps(result)


def test_tuples_and_sets_are_walked_and_keep_their_type() -> None:
    """Sequences are rebuilt, not skipped, and the container type survives."""
    result = _redact(
        {
            "as_tuple": ({"token": SECRET},),
            "as_list": [{"password": SECRET}],
            "as_set": frozenset({"Bearer abcdefghijklmnop"}),
        }
    )

    assert isinstance(result["as_tuple"], tuple)
    assert result["as_tuple"][0]["token"] == REDACTED
    assert result["as_list"][0]["password"] == REDACTED
    assert isinstance(result["as_set"], frozenset)
    assert all(REDACTED in item for item in result["as_set"])


def test_the_callers_structures_are_not_mutated() -> None:
    """Redaction substitutes copies; the object the caller still holds is untouched."""
    payload = {"credentials": {"api_key": SECRET}}
    _redact({"event": "x", "payload": payload})
    assert payload["credentials"]["api_key"] == SECRET


def test_a_self_referential_structure_terminates() -> None:
    """A cycle must not hang the logger."""
    cycle: dict = {"name": "loop"}
    cycle["self"] = cycle
    assert _redact({"event": "x", "cycle": cycle})  # returns rather than recursing forever


# ======================================================================================
# The value pass (added in Phase 7)
# ======================================================================================


def test_email_address_is_masked_under_a_harmless_key() -> None:
    """``error`` is not a sensitive name, but the string is still cleaned."""
    result = _redact({"error": "SMTP auth failed for ada.lovelace@personal-domain.com"})
    assert "ada.lovelace@personal-domain.com" not in result["error"]
    assert EMAIL_MASK in result["error"]


def test_ats_relay_domain_survives_but_the_local_part_does_not() -> None:
    """The domain says *which system* mailed the user and identifies nobody."""
    scrubbed = scrub_text("received from no-reply@greenhouse.io")
    assert "no-reply@greenhouse.io" not in scrubbed
    assert "greenhouse.io" in scrubbed


def test_bearer_token_is_removed() -> None:
    """An ``Authorization`` header echoed into an exception message."""
    scrubbed = scrub_text("401 from provider: Authorization: Bearer abcdef1234567890XYZ")
    assert "abcdef1234567890XYZ" not in scrubbed
    assert REDACTED in scrubbed


def test_access_token_in_a_url_keeps_the_name_and_loses_the_value() -> None:
    """Knowing *which* parameter was present is diagnostic; its value is not."""
    scrubbed = scrub_text("redirect to https://oauth.example.com/cb?access_token=xyzzy123&state=7")
    assert "xyzzy123" not in scrubbed
    assert "access_token=" in scrubbed
    assert "state=7" in scrubbed


@pytest.mark.parametrize(
    "credential",
    [
        "sk-proj-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz",
        "github_pat_abcdefghijklmnop",
        "xoxb-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstu",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ],
)
def test_self_identifying_credential_shapes_are_removed(credential: str) -> None:
    """Each branch of the credential pattern fires on a realistic value."""
    assert credential not in scrub_text(f"provider rejected {credential} at 09:15")


def test_ordinary_prose_is_left_alone() -> None:
    """A scrubber that mangles normal log lines gets disabled and protects nothing."""
    line = "discovered 41 postings from greenhouse in 2.4 seconds"
    assert scrub_text(line) == line


def test_value_scrubbing_reaches_inside_nested_containers() -> None:
    """The value pass runs at every depth, not only at the top level."""
    result = _redact({"errors": [{"detail": "failed for ada@personal-domain.com"}]})
    assert "ada@personal-domain.com" not in json.dumps(result)


# ======================================================================================
# The traceback — the regression `docs/SAFETY.md` asked for
# ======================================================================================


@pytest.fixture
def configured_json_logging(settings, monkeypatch):
    """Configure the real logging pipeline in JSON mode, piped into a buffer.

    The buffer is swapped onto the handler *after* ``configure_logging`` has built it, so the
    formatter under test is the real one — only its destination changes. Reading a
    ``StringIO`` rather than pytest's capture makes the assertion independent of fixture
    ordering and of whether capturing is fd- or sys-level.
    """
    monkeypatch.setattr(settings, "log_json", True)
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    monkeypatch.setattr(settings, "debug", False)
    configure_logging(settings)

    buffer = io.StringIO()
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)

    yield buffer

    structlog.reset_defaults()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_secret_in_a_traceback_frame_local_never_reaches_the_log(
    configured_json_logging,
) -> None:
    """A credential held in a *frame local* of the failing function must not be rendered.

    This is the bug ``docs/SAFETY.md`` records as having shipped once: ``redact_secrets``
    walks the event dict, but at that point the traceback is still an ``exc_info`` tuple, so
    nothing scrubs it. With structlog's default ``show_locals=True`` every frame's variables
    are expanded into ``exception[].frames[].locals`` — and the key that was correctly
    redacted two fields over is dumped verbatim.

    The assertion is against the bytes that actually reach the stream, which is the only
    place the renderer's behaviour is observable.
    """

    def _call_provider() -> None:
        # A local, not an argument and not a logged field. Exactly the shape that leaked.
        anthropic_api_key = SECRET
        authorization_header = f"Bearer {SECRET}"
        assert anthropic_api_key and authorization_header  # keep them live in the frame
        raise RuntimeError("provider rejected the request")

    logger = structlog.get_logger("tests.redaction")
    try:
        _call_provider()
    except RuntimeError:
        logger.error("provider.failed", api_key=SECRET, provider="anthropic", exc_info=True)

    combined = configured_json_logging.getvalue()

    assert combined.strip(), "nothing was logged; the pipeline is not wired to the stream"
    assert SECRET not in combined, "a secret in a traceback frame local reached the log"
    assert "provider.failed" in combined
    assert REDACTED in combined, "the key pass did not run"
    # The traceback itself must still be useful.
    assert "provider rejected the request" in combined


def test_the_redaction_processor_is_in_the_configured_chain(configured_json_logging) -> None:
    """A structural check: removing ``redact_secrets`` must fail a test, not go unnoticed."""
    processors = structlog.get_config()["processors"]
    assert redact_secrets in processors, "redact_secrets is not in the structlog chain"


def test_exception_renderer_has_locals_capture_disabled(configured_json_logging) -> None:
    """The specific setting behind the regression above, asserted directly."""
    root = logging.getLogger()
    formatters = [h.formatter for h in root.handlers if h.formatter is not None]
    assert formatters, "no formatter installed on the root handler"

    found = False
    for formatter in formatters:
        for processor in getattr(formatter, "processors", ()):
            transformer = getattr(processor, "format_exception", None)
            if isinstance(transformer, structlog.tracebacks.ExceptionDictTransformer):
                assert transformer.show_locals is False, (
                    "frame-locals capture is on; every local in the failing frame — including "
                    "an API key — would be rendered after redaction has already run"
                )
                found = True
    assert found, "no ExceptionRenderer found in the configured chain"
