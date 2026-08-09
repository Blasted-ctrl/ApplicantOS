"""Board health — the machinery that makes an empty discovery run say so.

Three related failures, all of which are silent by construction, all of which really
happened, and none of which any existing test could have caught:

1. **A Lever board that answers with nothing.** Lever returns ``200 []`` for a real company
   with no openings, so an empty board is indistinguishable from a working one. Discovery
   yields zero either way and logs nothing that says which.
2. **A provider that produces nothing.** No exception, no error entry, a ``by_provider``
   count of ``0`` — which reads exactly like a provider that was never enabled.
3. **A Workday tenant that cannot be resolved.** The tenant root began answering ``406`` to
   every request, so the shard/site probe failed for every tenant and Workday returned
   nothing forever. Resolution now reads ``robots.txt``, which is both the authoritative
   source for a career-site name and the polite one.

The live half of this — that the boards are actually alive today — is
``tests/integration/test_providers_live.py`` and ``scripts/validate_boards.py``. What is here
is the hermetic half: the parsing and the reporting, which must be right whether or not there
is a network.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import structlog

from app.jobs.base import RawPosting, SearchQuery
from app.jobs.registry import get_provider
from app.models.enums import ATSProviderName
from app.services.discovery_service import DiscoveryReport, DiscoveryService

# ======================================================================================
# Recorded payloads
# ======================================================================================

#: NVIDIA's real ``robots.txt``, byte for byte as fetched on 2026-08-09. The career-site
#: name it publishes — ``NVIDIAExternalCareerSite`` — is not derivable from the tenant token
#: by any rule, which is the whole reason this file is read rather than guessed at.
NVIDIA_ROBOTS = (
    "Sitemap: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/siteMap.xml\n"
    "\n"
    "User-agent: *\n"
    "Allow: /NVIDIAExternalCareerSite/\n"
    "Disallow: /talentcommunity/\n"
    "Disallow: /refreshFacet/\n"
)

#: Nike's real ``robots.txt``. It allows nothing and disallows three real board names. The
#: CXS API would happily serve them; this provider does not ask (golden rule #10).
NIKE_ROBOTS = (
    "\nUser-agent: *\n"
    "Disallow: /nke/\nDisallow: /nke2/\nDisallow: /nke4/\nDisallow: /refreshFacet/\n"
)

#: Salesforce's, which lists several boards. Order matters: the first is the general external
#: site and the rest are narrower programmes.
SALESFORCE_ROBOTS = (
    "Sitemap: https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/siteMap.xml\n"
    "Sitemap: https://salesforce.wd12.myworkdayjobs.com/Futureforce_NewGradRoles/siteMap.xml\n"
    "\n"
    "User-agent: *\n"
    "Allow: /External_Career_Site/\n"
    "Disallow: /refreshFacet/\n"
)


def lever_job(job_id: str, title: str, *, created_at: int = 1_754_000_000_000) -> dict[str, Any]:
    """Build one Lever feed entry.

    Args:
        job_id: The posting's identifier.
        title: The role title.
        created_at: ``createdAt`` in milliseconds, as Lever sends it.

    Returns:
        The payload, carrying the fields ``LeverProvider`` reads.
    """
    return {
        "id": job_id,
        "text": title,
        "createdAt": created_at,
        "hostedUrl": f"https://jobs.lever.co/acme/{job_id}",
        "applyUrl": f"https://jobs.lever.co/acme/{job_id}/apply",
        "descriptionPlain": "We build things and you would build them with us.",
        "categories": {"location": "Remote", "commitment": "Full-time"},
    }


# ======================================================================================
# Workday: robots.txt is the source of a career-site name
# ======================================================================================


def test_robots_names_the_career_site() -> None:
    """The one thing that cannot be guessed is read straight out of the published file."""
    from app.jobs.workday import career_sites_from_robots

    assert career_sites_from_robots(NVIDIA_ROBOTS) == ["NVIDIAExternalCareerSite"]


def test_robots_lists_every_published_board_sitemaps_first() -> None:
    """A tenant with several boards yields all of them, most-general first."""
    from app.jobs.workday import career_sites_from_robots

    assert career_sites_from_robots(SALESFORCE_ROBOTS) == [
        "External_Career_Site",
        "Futureforce_NewGradRoles",
    ]


def test_a_disallowed_board_is_never_offered() -> None:
    """``Disallow`` means do not poll it, even though the CXS API would serve it."""
    from app.jobs.workday import career_sites_from_robots

    assert career_sites_from_robots(NIKE_ROBOTS) == []


def test_robots_that_names_nothing_yields_nothing() -> None:
    """An absent or unrelated file must not produce a phantom candidate."""
    from app.jobs.workday import career_sites_from_robots

    assert career_sites_from_robots("") == []
    assert career_sites_from_robots("User-agent: *\nDisallow: /\n") == []


def test_reserved_segments_are_not_career_sites() -> None:
    """Workday owns ``/wday/`` and ``/job/``; neither is a board however it is spelled."""
    from app.jobs.workday import career_sites_from_robots

    body = "User-agent: *\nAllow: /wday/\nAllow: /job/\nAllow: /en-US/\nAllow: /Real_Site/\n"
    assert career_sites_from_robots(body) == ["Real_Site"]


def test_conventional_site_names_are_derived_from_the_tenant() -> None:
    """The fallback list is tenant-shaped, because most real names are.

    ``NVIDIAExternalCareerSite``, ``targetcareers`` and ``Cisco_Careers`` are all the tenant
    token with a suffix. A fixed list of six generic names reaches none of them.
    """
    from app.jobs.workday import site_name_candidates

    candidates = site_name_candidates("cisco")

    assert candidates[0] == "External", "the most common name must be tried first"
    assert "Cisco_Careers" in candidates
    assert "CISCOExternalCareerSite" in candidates
    assert "ciscocareers" in candidates
    assert len(candidates) == len(set(candidates)), "a candidate must never be probed twice"


def test_a_known_site_name_is_tried_first() -> None:
    """A name the caller already has beats every convention."""
    from app.jobs.workday import site_name_candidates

    candidates = site_name_candidates("nvidia", preferred="NVIDIAExternalCareerSite")
    assert candidates[0] == "NVIDIAExternalCareerSite"
    assert candidates.count("NVIDIAExternalCareerSite") == 1


# ======================================================================================
# Lever: an empty board is reported as an empty board
# ======================================================================================


async def test_an_empty_lever_board_is_logged_distinguishably(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feed that carried no postings emits its own event, not a generic zero."""
    from app.jobs.lever import EVENT_BOARD_EMPTY

    provider = get_provider(ATSProviderName.LEVER)

    async def empty_page(company: str, skip: int, limit: int) -> list[dict[str, Any]]:
        """Stand in for a board that answers ``200 []``."""
        return []

    monkeypatch.setattr(provider, "_page", empty_page)

    query = SearchQuery(limit=10, extra={"lever": ["ghostcorp"]})
    with structlog.testing.capture_logs() as logs:
        found = [raw async for raw in provider.search(query)]

    assert found == []
    empties = [entry for entry in logs if entry.get("event") == EVENT_BOARD_EMPTY]
    assert len(empties) == 1, f"expected one {EVENT_BOARD_EMPTY}, got {[e['event'] for e in logs]}"
    assert empties[0]["company"] == "ghostcorp"
    assert empties[0]["log_level"] == "warning"


async def test_a_board_filtered_to_nothing_is_not_reported_as_empty(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction that matters: the feed had postings, the *query* rejected them.

    Reporting this as an empty board would send someone to delete a perfectly good token
    from ``app/jobs/seeds.py``.
    """
    from app.jobs.lever import EVENT_BOARD_EMPTY

    provider = get_provider(ATSProviderName.LEVER)

    async def one_page(company: str, skip: int, limit: int) -> list[dict[str, Any]]:
        """A board with a posting on it, returned once and then exhausted."""
        if skip:
            return []
        return [lever_job("11111111-2222-3333-4444-555555555555", "Plumber")]

    monkeypatch.setattr(provider, "_page", one_page)

    query = SearchQuery(keywords=["quantum cryptographer"], limit=10, extra={"lever": ["acme"]})
    with structlog.testing.capture_logs() as logs:
        found = [raw async for raw in provider.search(query)]

    assert found == []
    assert not [entry for entry in logs if entry.get("event") == EVENT_BOARD_EMPTY]
    scanned = [entry for entry in logs if entry.get("event") == "lever.company_scanned"]
    assert scanned and scanned[0]["scanned"] == 1 and scanned[0]["yielded"] == 0


async def test_a_live_board_yields_and_is_not_reported_as_empty(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case, so the two tests above cannot both pass vacuously."""
    from app.jobs.lever import EVENT_BOARD_EMPTY

    provider = get_provider(ATSProviderName.LEVER)

    async def one_page(company: str, skip: int, limit: int) -> list[dict[str, Any]]:
        """A board with a matching posting on it."""
        if skip:
            return []
        return [lever_job("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "Firmware Engineer")]

    monkeypatch.setattr(provider, "_page", one_page)

    query = SearchQuery(limit=10, extra={"lever": ["acme"]})
    with structlog.testing.capture_logs() as logs:
        found = [raw async for raw in provider.search(query)]

    assert len(found) == 1
    assert found[0].title == "Firmware Engineer"
    assert not [entry for entry in logs if entry.get("event") == EVENT_BOARD_EMPTY]


# ======================================================================================
# DiscoveryReport: "lever: 30 boards, 0 postings"
# ======================================================================================


def test_an_empty_provider_is_described_in_terms_a_user_can_act_on() -> None:
    """The sentence this whole mechanism exists to be able to print."""
    report = DiscoveryReport(
        by_provider={"lever": 0, "greenhouse": 12},
        boards_by_provider={"lever": 30, "greenhouse": 49},
        empty_providers=["lever"],
    )
    assert report.describe_empty_providers() == ["lever: 30 boards, 0 postings"]


def test_a_provider_with_no_boards_is_described_differently() -> None:
    """"Nowhere to look" and "looked everywhere and found nothing" need different fixes."""
    report = DiscoveryReport(boards_by_provider={"linkedin": 0}, empty_providers=["linkedin"])
    assert report.describe_empty_providers() == ["linkedin: no boards configured, 0 postings"]


def test_the_report_carries_the_board_counts_over_the_wire() -> None:
    """The desktop app reads these off ``as_dict``; a missing key is an invisible feature."""
    report = DiscoveryReport(boards_by_provider={"ashby": 40}, empty_providers=["ashby"])
    payload = report.as_dict()
    assert payload["boards_by_provider"] == {"ashby": 40}
    assert payload["empty_providers"] == ["ashby"]


async def test_discovery_records_a_provider_that_found_nothing(
    session: Any, settings: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful run that discovered nothing is recorded, not merely absent.

    This is the outcome with no other trace: no exception, no ``errors`` entry, and a
    ``by_provider`` count of zero that is identical to the one a disabled provider produces.
    """
    provider = get_provider(ATSProviderName.LEVER)

    async def empty_page(company: str, skip: int, limit: int) -> list[dict[str, Any]]:
        """Every board answers with nothing."""
        return []

    monkeypatch.setattr(provider, "_page", empty_page)

    service = DiscoveryService(session, settings)
    report = await service.discover(user.id, providers=["lever"])

    assert report.found == 0
    assert report.ok, "an empty run is not a failed run and must record no error"
    assert report.empty_providers == ["lever"]
    assert report.boards_by_provider["lever"] > 0
    assert report.describe_empty_providers() == [
        f"lever: {report.boards_by_provider['lever']} boards, 0 postings"
    ]


async def test_discovery_does_not_call_a_working_provider_empty(
    session: Any, settings: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case for the report, so an always-empty list cannot pass."""
    provider = get_provider(ATSProviderName.LEVER)

    async def search(q: SearchQuery):
        """Yield exactly one posting, whatever was asked for."""
        yield RawPosting(
            provider=ATSProviderName.LEVER,
            external_id=str(uuid.uuid4()),
            url="https://jobs.lever.co/acme/1",
            title="Firmware Engineer",
            company_name="Acme",
            description="Embedded C on ARM.",
        )

    monkeypatch.setattr(provider, "search", search)

    service = DiscoveryService(session, settings)
    report = await service.discover(user.id, providers=["lever"])

    assert report.found == 1
    assert report.empty_providers == []
    assert report.describe_empty_providers() == []
