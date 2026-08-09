"""Golden rule #10 — ToS honesty.

    Providers forbidding automation set ``supports_auto_apply=False`` and route to manual
    review, documented in the module docstring.

Two platforms are discovery-only by deliberate policy rather than by technical limitation:

* **LinkedIn** — its terms prohibit automated scraping and submission. Discovery reads only a
  user-supplied export or a public feed.
* **Workday** — an account-gated, per-tenant, multi-step wizard, routed to a human by design.

``docs/SAFETY.md`` states this as a boundary that only *looks* like a limitation worth
fixing. That claim is worth a test, because the pressure to "just add login" is real and the
change would be one line.

The strongest assertion in the file is the last group: **no credentialed request is
constructed anywhere in ``app/jobs/linkedin.py``.** Declaring ``supports_auto_apply=False``
while quietly holding a session cookie would satisfy every other check here, so the module's
source is parsed and searched for authentication machinery directly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.jobs.base import ApplyContext, UnsupportedFlowError
from app.models.enums import ATSProviderName, PluginKind, ReviewReason

LINKEDIN_SOURCE = Path(__file__).resolve().parent.parent / "app" / "jobs" / "linkedin.py"

#: Providers that may really submit, and providers that must never.
AUTO_APPLY_ALLOWED = ("greenhouse", "lever", "ashby")
AUTO_APPLY_FORBIDDEN = ("linkedin", "workday")


@pytest.fixture
def providers(settings):
    """The plugin registry with every provider loaded."""
    from app.plugins.loader import load_all
    from app.plugins.registry import registry

    registry.configure(settings)
    load_all()
    return registry


def _provider_class(registry, name: str):
    """The provider *class*, since the posture flags are ``ClassVar``."""
    return registry.get_class(PluginKind.PROVIDER, name)


# ======================================================================================
# The posture flags
# ======================================================================================


@pytest.mark.parametrize("name", AUTO_APPLY_FORBIDDEN)
def test_forbidden_providers_declare_no_auto_apply(providers, name: str) -> None:
    """LinkedIn and Workday must both report ``supports_auto_apply is False``."""
    assert _provider_class(providers, name).supports_auto_apply is False


@pytest.mark.parametrize("name", AUTO_APPLY_ALLOWED)
def test_permitted_providers_declare_auto_apply(providers, name: str) -> None:
    """The three public-form ATSs do support real submission.

    Without this the rule above would be satisfiable by disabling everything, which is safe
    and useless.
    """
    assert _provider_class(providers, name).supports_auto_apply is True


@pytest.mark.parametrize("name", AUTO_APPLY_FORBIDDEN)
def test_forbidden_providers_state_their_posture_in_the_module_docstring(
    providers, name: str
) -> None:
    """Golden rule #10 requires the posture to be *documented*, not merely coded."""
    import importlib

    module = importlib.import_module(_provider_class(providers, name).__module__)
    docstring = (module.__doc__ or "").lower()
    assert docstring, f"{name} has no module docstring"
    assert "supports_auto_apply" in docstring or "unsupportedflowerror" in docstring, (
        f"{name}'s module docstring does not state its auto-apply posture"
    )


# ======================================================================================
# `apply()` raises
# ======================================================================================


@pytest.fixture
def apply_context(posting, user):
    """A minimally valid :class:`ApplyContext`."""
    from app.jobs.base import JobPostingDTO, UserProfileDTO

    return ApplyContext(
        application_id=posting.id,
        posting=JobPostingDTO.from_model(posting),
        user=UserProfileDTO(user_id=user.id, full_name=user.full_name, email=user.email),
        resume_path=None,
        cover_letter_path=None,
        answers={},
        dry_run=True,
    )


@pytest.mark.parametrize("name", AUTO_APPLY_FORBIDDEN)
async def test_forbidden_provider_apply_raises(providers, apply_context, name: str) -> None:
    """``apply()`` raises :class:`UnsupportedFlowError` — it does not return a failure."""
    provider = providers.get(PluginKind.PROVIDER, name)
    with pytest.raises(UnsupportedFlowError):
        await provider.apply(apply_context)


@pytest.mark.parametrize("name", AUTO_APPLY_FORBIDDEN)
async def test_forbidden_provider_apply_raises_even_with_both_switches_open(
    providers, apply_context, submission_allowed, name: str
) -> None:
    """The kill switch is not what stops these providers; the provider itself is."""
    provider = providers.get(PluginKind.PROVIDER, name)
    with pytest.raises(UnsupportedFlowError):
        await provider.apply(apply_context)


async def test_unsupported_flow_maps_to_a_review_reason() -> None:
    """The error carries the routing decision, so the pipeline escalates rather than fails."""
    error = UnsupportedFlowError("linkedin does not permit automated submission")
    assert error.review_reason is ReviewReason.UNSUPPORTED_FLOW


# ======================================================================================
# The pipeline honours the posture
# ======================================================================================


async def test_pipeline_routes_a_forbidden_provider_to_review(
    session, submission_allowed, make_posting, make_application, make_score
) -> None:
    """With both switches open and a passing score, a LinkedIn posting still goes to review.

    The posting is scored deliberately high: an unscored posting is refused by an earlier
    guard, which would make this pass without ever reaching the provider-posture check.
    """
    from app.models.enums import ApplicationStatus
    from app.services.pipeline import Pipeline

    posting = await make_posting(
        provider=ATSProviderName.LINKEDIN,
        external_id="li-1",
        url="https://www.linkedin.com/jobs/view/1",
    )
    await make_score(posting, normalized=95)
    application = await make_application(posting, status=ApplicationStatus.READY)

    result = await Pipeline(session, submission_allowed).submit(application.id)

    assert result.submitted is False
    assert result.review_reason is ReviewReason.UNSUPPORTED_FLOW
    assert application.status is ApplicationStatus.NEEDS_REVIEW


# ======================================================================================
# No credentialed request is constructed in `app/jobs/linkedin.py`
# ======================================================================================


def test_linkedin_module_exists_and_was_read() -> None:
    """Guard against the source checks below passing on an empty string."""
    assert LINKEDIN_SOURCE.is_file()
    assert len(LINKEDIN_SOURCE.read_text(encoding="utf-8")) > 1000


#: Tokens that would indicate an authenticated LinkedIn session. ``li_at`` is LinkedIn's own
#: session cookie; ``csrf-token`` and ``x-li-`` head its internal API's required headers.
CREDENTIAL_MARKERS = (
    "li_at",
    "jsessionid",
    "csrf-token",
    "x-li-",
    "voyager",
    "linkedin.com/uas/login",
    "linkedin.com/checkpoint",
    "/login-submit",
)


@pytest.mark.parametrize("marker", CREDENTIAL_MARKERS)
def test_no_linkedin_session_credential_appears_in_the_source(marker: str) -> None:
    """The module must contain no LinkedIn session token, cookie name or auth endpoint."""
    source = LINKEDIN_SOURCE.read_text(encoding="utf-8").lower()
    assert marker not in source, (
        f"app/jobs/linkedin.py mentions {marker!r} — that is authenticated-session "
        "machinery, and LinkedIn's terms prohibit it"
    )


def test_no_authorization_or_cookie_header_is_constructed() -> None:
    """No ``Authorization`` or ``Cookie`` header is built anywhere in the module.

    A dictionary literal is the shape such a header takes, so the check is on the *string*
    keys appearing at all — comments and docstrings are stripped first so prose explaining
    the prohibition does not trip it.
    """
    tree = ast.parse(LINKEDIN_SOURCE.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Skip docstrings: they are the *body* of a module/class/function, not an
            # expression used as a value.
            lowered = node.value.strip().lower()
            if lowered in ("authorization", "cookie", "set-cookie", "x-li-identity"):
                offenders.append(f"line {node.lineno}: {node.value!r}")

    assert not offenders, (
        "app/jobs/linkedin.py constructs an auth header:\n  " + "\n  ".join(offenders)
    )


def test_linkedin_provider_does_not_send_credentials_from_settings() -> None:
    """The module never reads a LinkedIn username, password or session token from settings."""
    source = LINKEDIN_SOURCE.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"settings\.(linkedin_(?:password|username|email|session|cookie|token))", re.IGNORECASE
    )
    match = forbidden.search(source)
    assert match is None, f"linkedin.py reads credential setting {match.group(1)!r}"


def test_linkedin_discovery_is_export_or_public_feed_only() -> None:
    """The documented boundary: a user-supplied export, or a public feed. Nothing else."""
    docstring = LINKEDIN_SOURCE.read_text(encoding="utf-8")[:4000].lower()
    assert "export" in docstring or "rss" in docstring or "public" in docstring, (
        "linkedin.py does not document its discovery boundary"
    )
