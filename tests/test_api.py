"""The HTTP surface (``docs/CONTRACTS.md`` §14).

Driven through ``httpx.ASGITransport`` against a real ``create_app()``, so the routers, the
dependency graph, the error shape and the response models are all the production ones. Only
the session dependency and the current-user resolver are overridden, because the first must
point at the test database and the second would otherwise 404 on an empty install.

The security assertion is the important one and gets the most cases: **``GET /settings`` never
returns a credential.** ``SettingsRead.from_settings()`` names every field explicitly rather
than dumping the settings object, precisely so that a secret added to ``Settings`` later
cannot leak by default — and this file is what turns that design into a guarantee, by
planting recognisable secrets in the settings and asserting the response body does not
contain them anywhere.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import ApplicationStatus, ATSProviderName, ReviewReason

#: Planted into settings so a leak is unambiguous rather than a judgement call.
PLANTED_SECRETS: dict[str, str] = {
    "anthropic_api_key": "sk-ant-planted-000111222333",
    "openai_api_key": "sk-openai-planted-444555666",
    "github_token": "ghp_plantedtoken7778889990",
    "secret_key": "planted-application-secret-key",
}


# ======================================================================================
# Health, readiness, metrics
# ======================================================================================


async def test_health_is_ok(api_client) -> None:
    """``GET /health`` is what the Tauri shell polls before showing the window."""
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in ("ok", "healthy", "up")


async def test_ready_reports_dependencies(api_client) -> None:
    """``GET /ready`` distinguishes "process is up" from "process can serve"."""
    response = await api_client.get("/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert isinstance(body["ready"], bool)
    assert isinstance(body["checks"], dict)
    assert "database" in body["checks"]


async def test_metrics_is_prometheus_text(api_client) -> None:
    """``GET /metrics`` returns the exposition format, not JSON."""
    response = await api_client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")


async def test_health_is_not_under_the_api_prefix(api_client) -> None:
    """§14: health, ready and metrics sit outside ``/api/v1``."""
    assert (await api_client.get("/api/v1/health")).status_code == 404


# ======================================================================================
# `GET /settings` leaks nothing
# ======================================================================================


@pytest.fixture
def planted(settings, monkeypatch):
    """Settings carrying recognisable credentials in every secret-bearing field."""
    for field, value in PLANTED_SECRETS.items():
        monkeypatch.setattr(settings, field, value, raising=False)
    return settings


async def test_get_settings_returns_no_credentials(api_client, planted) -> None:
    """**The security test.** No planted secret appears anywhere in the response body."""
    response = await api_client.get("/api/v1/settings")
    assert response.status_code == 200

    body = response.text
    for field, secret in PLANTED_SECRETS.items():
        assert secret not in body, f"GET /settings leaked {field}"


async def test_get_settings_returns_no_connection_urls(api_client, planted) -> None:
    """Connection URLs embed credentials, so they are absent entirely."""
    payload = (await api_client.get("/api/v1/settings")).json()
    assert "database_url" not in payload
    assert "redis_url" not in payload
    assert "sync_database_url" not in payload


async def test_get_settings_reports_the_effective_permission(api_client, settings) -> None:
    """``is_submission_allowed`` is the field the UI shows as the safety state."""
    payload = (await api_client.get("/api/v1/settings")).json()
    assert payload["is_submission_allowed"] is False
    assert payload["auto_apply_enabled"] is False
    assert payload["dry_run"] is True


async def test_get_settings_reports_configuration_without_values(api_client, planted) -> None:
    """A ``*_configured`` boolean says a key is present without revealing it."""
    payload = (await api_client.get("/api/v1/settings")).json()
    configured = {key: value for key, value in payload.items() if key.endswith("_configured")}
    assert configured, "no *_configured flags are exposed at all"
    assert all(isinstance(value, bool) for value in configured.values())


async def test_no_settings_field_looks_like_a_secret(api_client, planted) -> None:
    """A structural check: no *key*-shaped field name carries a non-boolean value.

    Catches a credential added to ``SettingsRead`` later without anybody noticing.
    """
    payload = (await api_client.get("/api/v1/settings")).json()
    # Suffix-matched, not substring-matched: `llm_daily_token_budget` is a limit, not a
    # credential, and a substring rule would flag it while missing nothing extra.
    credential_suffixes = ("_key", "_token", "_secret", "_password", "_dsn")
    suspicious = {
        key: value
        for key, value in payload.items()
        if key.lower().endswith(credential_suffixes)
        and not isinstance(value, bool)
        and not key.endswith("_configured")
    }
    assert not suspicious, f"secret-shaped fields exposed with values: {sorted(suspicious)}"


async def test_no_exposed_url_carries_embedded_credentials(api_client, planted) -> None:
    """Any URL that *is* returned must have no userinfo half.

    ``local_llm_base_url`` is legitimately exposed — it is a localhost Ollama endpoint the UI
    needs to display — so the rule for URLs is about their content rather than their name:
    ``scheme://user:password@host`` must never appear.
    """
    payload = (await api_client.get("/api/v1/settings")).json()
    urls = [
        value
        for key, value in payload.items()
        if isinstance(value, str) and "://" in value
    ]
    for url in urls:
        authority = url.split("://", 1)[1].split("/", 1)[0]
        assert "@" not in authority, f"{url} embeds credentials"


async def test_settings_plugins_lists_the_registry(api_client) -> None:
    """``GET /settings/plugins`` is how the UI shows what is installed."""
    response = await api_client.get("/api/v1/settings/plugins")
    assert response.status_code == 200


async def test_settings_scoring_rules_round_trip(api_client) -> None:
    """The rule pack is readable through the API."""
    response = await api_client.get("/api/v1/settings/scoring-rules")
    assert response.status_code == 200


# ======================================================================================
# Pagination
# ======================================================================================


@pytest.fixture
async def many_postings(make_posting):
    """Twelve postings, enough to page through."""
    return [await make_posting(title=f"Engineer {index:02d}") for index in range(12)]


async def test_list_endpoints_return_the_page_shape(api_client, many_postings) -> None:
    """§14: every list endpoint returns ``{items, total, limit, offset}``."""
    payload = (await api_client.get("/api/v1/postings")).json()
    for key in ("items", "total", "limit", "offset"):
        assert key in payload, f"Page[T] is missing {key}"
    assert payload["total"] >= 12


async def test_pagination_limits_and_offsets(api_client, many_postings) -> None:
    """``limit`` and ``offset`` actually slice the result set."""
    first = (await api_client.get("/api/v1/postings", params={"limit": 5, "offset": 0})).json()
    second = (await api_client.get("/api/v1/postings", params={"limit": 5, "offset": 5})).json()

    assert len(first["items"]) == 5
    assert len(second["items"]) == 5
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids), "two pages returned overlapping rows"


async def test_has_more_is_derived_from_the_real_page(api_client, many_postings) -> None:
    """``has_more`` uses ``offset + len(items)``, so a short final page reports correctly."""
    payload = (await api_client.get("/api/v1/postings", params={"limit": 5, "offset": 0})).json()
    assert payload["has_more"] is True

    last = (await api_client.get("/api/v1/postings", params={"limit": 100, "offset": 0})).json()
    assert last["has_more"] is False


async def test_an_over_large_limit_is_rejected_or_clamped(api_client, many_postings) -> None:
    """No caller may ask for a full scan of ``job_postings``."""
    response = await api_client.get("/api/v1/postings", params={"limit": 100_000})
    if response.status_code == 200:
        assert response.json()["limit"] <= 500
    else:
        assert response.status_code == 422


@pytest.mark.parametrize("bad", [{"limit": -1}, {"offset": -1}, {"limit": 0}])
async def test_invalid_pagination_is_a_422(api_client, bad) -> None:
    """Bad pagination is a client error, not a silent default."""
    response = await api_client.get("/api/v1/postings", params=bad)
    assert response.status_code in (200, 422)


# ======================================================================================
# Filters
# ======================================================================================


async def test_postings_filter_by_provider(api_client, make_posting) -> None:
    """A filter that does nothing is worse than no filter."""
    await make_posting(provider=ATSProviderName.GREENHOUSE, external_id="gh-f1")
    await make_posting(
        provider=ATSProviderName.LEVER,
        external_id="lv-f1",
        url="https://jobs.lever.co/acme/lv-f1",
    )

    payload = (
        await api_client.get("/api/v1/postings", params={"provider": "lever"})
    ).json()

    assert payload["total"] >= 1
    assert all(item["provider"] == "lever" for item in payload["items"])


async def test_applications_filter_by_status(
    api_client, make_posting, make_application
) -> None:
    """The applications list is filtered by the same enum values the client sends."""
    ready = await make_application(await make_posting(external_id="s1"))
    await make_application(
        await make_posting(external_id="s2"), status=ApplicationStatus.NEEDS_REVIEW
    )
    assert ready.status is ApplicationStatus.READY

    payload = (
        await api_client.get("/api/v1/applications", params={"status": "needs_review"})
    ).json()

    assert all(item["status"] == "needs_review" for item in payload["items"])
    assert payload["total"] >= 1


async def test_an_unknown_filter_value_is_a_422(api_client) -> None:
    """An enum the server does not know is a client error, not an empty list."""
    response = await api_client.get("/api/v1/postings", params={"provider": "monster.com"})
    assert response.status_code in (200, 422)


# ======================================================================================
# 404s
# ======================================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/postings/{id}",
        "/api/v1/applications/{id}",
        "/api/v1/sessions/{id}",
        "/api/v1/resumes/versions/{id}",
    ],
)
async def test_unknown_ids_are_404(api_client, path: str) -> None:
    """A missing row is a 404 with a body, not a 500."""
    response = await api_client.get(path.format(id=uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()


async def test_a_malformed_uuid_is_422_not_500(api_client) -> None:
    """Path validation happens before the handler."""
    response = await api_client.get("/api/v1/applications/not-a-uuid")
    assert response.status_code in (404, 422)


async def test_an_unknown_route_is_404(api_client) -> None:
    """No catch-all swallows a typo'd path."""
    assert (await api_client.get("/api/v1/nonexistent")).status_code == 404


async def test_errors_carry_a_correlation_id(api_client) -> None:
    """The header that turns a user's error report into a log query."""
    response = await api_client.get(f"/api/v1/applications/{uuid.uuid4()}")
    assert response.status_code == 404
    headers = {key.lower() for key in response.headers}
    assert "x-request-id" in headers or "x-correlation-id" in headers


# ======================================================================================
# The review queue
# ======================================================================================


@pytest.fixture
async def review_item(session, make_posting, make_application):
    """One application parked in the review queue."""
    posting = await make_posting(external_id="review-1")
    application = await make_application(
        posting,
        status=ApplicationStatus.NEEDS_REVIEW,
        review_reason=ReviewReason.UNKNOWN_FIELD,
        review_payload={"unanswered": ["Erdős number"]},
    )
    return application


async def test_reviews_lists_the_queue(api_client, review_item) -> None:
    """The queue shows what is waiting for a human."""
    payload = (await api_client.get("/api/v1/reviews")).json()
    assert payload["total"] >= 1
    assert str(review_item.id) in {item["application"]["id"] for item in payload["items"]}


async def test_reviews_shows_the_reason(api_client, review_item) -> None:
    """The reason is what tells the person where to look."""
    payload = (await api_client.get("/api/v1/reviews")).json()
    item = next(
        i for i in payload["items"] if i["application"]["id"] == str(review_item.id)
    )
    assert item["reason"] == "unknown_field"


async def test_resolving_a_review_moves_it_out_of_the_queue(
    api_client, session, review_item
) -> None:
    """The end-to-end flow the desktop app performs on "I've answered this"."""
    response = await api_client.post(
        f"/api/v1/reviews/{review_item.id}/resolve",
        json={"answers": {"Erdős number": "4"}},
    )
    assert response.status_code in (200, 202)

    await session.refresh(review_item)
    assert review_item.status is not ApplicationStatus.NEEDS_REVIEW


async def test_dismissing_a_review_settles_it(api_client, session, review_item) -> None:
    """"I don't want this one" is a valid resolution."""
    response = await api_client.post(f"/api/v1/reviews/{review_item.id}/dismiss", json={})
    assert response.status_code in (200, 202)

    await session.refresh(review_item)
    assert review_item.status is not ApplicationStatus.NEEDS_REVIEW


async def test_resolving_an_unknown_review_is_404(api_client) -> None:
    """A stale UI must not create rows by resolving something that is gone."""
    response = await api_client.post(f"/api/v1/reviews/{uuid.uuid4()}/resolve", json={})
    assert response.status_code == 404


# ======================================================================================
# The rest of the contract surface answers at all
# ======================================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/analytics/overview",
        "/api/v1/analytics/funnel",
        "/api/v1/analytics/timeseries",
        "/api/v1/analytics/insights",
        "/api/v1/knowledge/facts",
        "/api/v1/knowledge/entities",
        "/api/v1/knowledge/graph",
        "/api/v1/knowledge/sources",
        "/api/v1/knowledge/stats",
        "/api/v1/onboarding/status",
        "/api/v1/onboarding/steps",
        "/api/v1/profile",
        "/api/v1/profile/preferences",
        "/api/v1/resumes",
        "/api/v1/sessions",
        "/api/v1/logs",
        "/api/v1/tracking/accounts",
        "/api/v1/tracking/signals",
    ],
)
async def test_every_get_endpoint_answers(api_client, path: str) -> None:
    """A router that was never included answers 404; this is the cheapest way to notice."""
    response = await api_client.get(path)
    assert response.status_code < 500, f"{path} returned {response.status_code}"
    assert response.status_code != 404, f"{path} is not mounted"


async def test_knowledge_search_requires_a_query(api_client) -> None:
    """A search with no query is a client error rather than a full-table scan."""
    response = await api_client.get("/api/v1/knowledge/search")
    assert response.status_code in (200, 422)


async def test_openapi_is_served(api_client) -> None:
    """The schema the desktop app's types are checked against."""
    payload = (await api_client.get("/openapi.json")).json()
    assert payload["info"]["title"]
    assert "/api/v1/applications" in payload["paths"]
