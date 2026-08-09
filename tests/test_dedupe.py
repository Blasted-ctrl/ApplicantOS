"""Deduplication (``docs/CONTRACTS.md`` §9).

Dedupe sits upstream of golden rule #1 and has an asymmetric failure mode that shapes every
test here. Merging two postings that are **not** the same job makes one of them silently
disappear — the user is never shown it and can never discover that they were not shown it.
Failing to merge two postings that *are* the same job shows one extra row, which is mildly
annoying and immediately visible.

So the module is deliberately conservative, and the tests assert that conservatism rather
than treating it as a bug:

* :func:`~app.jobs.dedupe.dedupe_key` is provider-scoped and **cannot** merge across boards.
  Cross-provider collapse is a *judgement* and belongs to :func:`~app.jobs.dedupe.is_duplicate`
  and the dedupe service, where a wrong answer is visible and reversible.
* The similarity threshold is 0.92, high enough that one differing token in a short
  normalised title fails the match.

Tracking-parameter stripping gets the most cases, because it is the mechanism that stops the
*same* posting arriving twice from one board under two URLs.
"""

from __future__ import annotations

import pytest

from app.jobs.dedupe import (
    DEFAULT_SIMILARITY_THRESHOLD,
    canonical_url,
    content_hash,
    dedupe_key,
    is_duplicate,
    normalize_company,
    normalize_location,
    normalize_title,
    similarity,
)
from app.models.enums import ATSProviderName, WorkArrangement


def _posting(**overrides):
    """A :class:`~app.jobs.base.RawPosting` with sensible defaults."""
    from app.jobs.base import RawPosting

    values = {
        "provider": ATSProviderName.GREENHOUSE,
        "external_id": "4012",
        "url": "https://boards.greenhouse.io/acme/jobs/4012",
        "title": "Senior Backend Engineer",
        "company_name": "Acme Robotics, Inc.",
        "description": "Python and PostgreSQL.",
        "location": "San Francisco, CA",
        "work_arrangement": WorkArrangement.ONSITE,
    }
    values.update(overrides)
    return RawPosting(**values)


# ======================================================================================
# Tracking parameters
# ======================================================================================


@pytest.mark.parametrize(
    "dirty",
    [
        "https://boards.greenhouse.io/acme/jobs/4012?utm_source=linkedin",
        "https://boards.greenhouse.io/acme/jobs/4012?utm_medium=email&utm_campaign=q3",
        "https://boards.greenhouse.io/acme/jobs/4012?gh_src=abc123",
        "https://boards.greenhouse.io/acme/jobs/4012?utm_source=x&gh_src=y",
        "https://boards.greenhouse.io/acme/jobs/4012#application",
        "https://boards.greenhouse.io/acme/jobs/4012/",
        "HTTPS://Boards.Greenhouse.IO/acme/jobs/4012",
        "https://boards.greenhouse.io:443/acme/jobs/4012",
    ],
)
def test_tracking_parameters_and_noise_are_stripped(dirty: str) -> None:
    """Every decorated spelling of one URL reduces to the same canonical form."""
    assert canonical_url(dirty) == "https://boards.greenhouse.io/acme/jobs/4012"


def test_a_meaningful_query_parameter_survives() -> None:
    """For many boards a parameter *is* the posting id; stripping it breaks the link."""
    url = "https://jobs.lever.co/acme?posting=abc-123"
    assert "posting=abc-123" in canonical_url(url)


def test_parameter_order_is_preserved() -> None:
    """Reordering can produce a URL that no longer resolves, so it is left alone."""
    url = "https://example.com/jobs?b=2&a=1"
    assert canonical_url(url).endswith("?b=2&a=1")


def test_the_root_path_keeps_its_slash() -> None:
    """ "/" is the path, not a trailing slash to strip."""
    assert canonical_url("https://example.com/") == "https://example.com/"


@pytest.mark.parametrize("value", [None, "", "   ", 12345])
def test_unparseable_input_never_raises(value) -> None:
    """A feed full of junk must not take down a discovery run."""
    assert isinstance(canonical_url(value), str)


def test_canonical_url_is_idempotent() -> None:
    """Canonicalising twice equals canonicalising once."""
    once = canonical_url("https://boards.greenhouse.io/acme/jobs/4012?utm_source=x")
    assert canonical_url(once) == once


# ======================================================================================
# The identity key
# ======================================================================================


def test_the_same_posting_twice_collapses_to_one_key() -> None:
    """Two polls of one posting produce one key, whatever the URL decoration."""
    first = _posting(url="https://boards.greenhouse.io/acme/jobs/4012?utm_source=a")
    second = _posting(url="https://boards.greenhouse.io/acme/jobs/4012?gh_src=b")
    assert dedupe_key(first) == dedupe_key(second)


def test_two_different_postings_have_different_keys() -> None:
    """The failure that matters: distinct openings must never share a key."""
    assert dedupe_key(_posting(external_id="4012")) != dedupe_key(_posting(external_id="4013"))


def test_the_key_is_stable_across_reparses() -> None:
    """A parser change that alters the description must not change identity."""
    first = _posting(description="Original text.")
    second = _posting(description="Rewritten by a better parser.")
    assert dedupe_key(first) == dedupe_key(second)


def test_the_key_is_a_sha256_digest() -> None:
    """Never ``hash()`` — it is salted per process and this value is persisted."""
    key = dedupe_key(_posting())
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)


def test_the_key_does_not_merge_across_providers() -> None:
    """**Deliberate.** The same role on two boards keeps two keys.

    Cross-provider merging is a judgement, not an identity claim, and encoding it in a unique
    constraint would trade a visible duplicate for an invisible disappearance.
    """
    greenhouse = _posting(provider=ATSProviderName.GREENHOUSE, external_id="4012")
    lever = _posting(provider=ATSProviderName.LEVER, external_id="4012")
    assert dedupe_key(greenhouse) != dedupe_key(lever)


def test_without_an_external_id_the_key_falls_back_to_content() -> None:
    """A manually entered posting still gets a stable identity."""
    first = _posting(external_id="", company_name="Acme Robotics, Inc.")
    second = _posting(external_id="", company_name="ACME ROBOTICS INCORPORATED")
    assert dedupe_key(first) == dedupe_key(second)


def test_content_hash_tracks_the_text_not_the_identity() -> None:
    """``content_hash`` answers "has the text changed since I scored it?" — a different job."""
    first = _posting(description="Original.")
    second = _posting(description="Changed.")
    assert content_hash(first) != content_hash(second)
    assert dedupe_key(first) == dedupe_key(second)


# ======================================================================================
# Normalisation
# ======================================================================================


@pytest.mark.parametrize(
    ("raw", "expected_equal"),
    [
        ("Acme Robotics, Inc.", "ACME Robotics Incorporated"),
        ("Acme Robotics LLC", "Acme Robotics, llc"),
        ("Foo & Bar Ltd", "Foo and Bar Limited"),
        ("Acme Robotics Corp", "Acme Robotics"),
    ],
)
def test_company_normalisation_collapses_legal_suffixes(raw, expected_equal) -> None:
    """``normalize_company`` must agree with ``Company.normalize`` or the unique index splits."""
    assert normalize_company(raw) == normalize_company(expected_equal)


def test_a_dotted_legal_suffix_does_not_collapse() -> None:
    """**Documented limitation**, asserted so it cannot change silently.

    ``COMPANY_LEGAL_SUFFIXES`` is frozen at thirteen bare tokens (``docs/OPEN_QUESTIONS.md``
    item 13) and punctuation is replaced by spaces *before* suffix stripping, so ``L.L.C.``
    becomes the three tokens ``l l c`` and never matches ``llc``. The consequence is real but
    small: an employer spelled both ways yields two ``companies`` rows, fragmenting the block
    list and the per-company analytics.

    Changing the suffix set is a data migration rather than a code change, so this records
    the behaviour instead of asserting the ideal. If the set is ever extended, this test
    fails and the migration question gets asked deliberately.
    """
    assert normalize_company("Acme Robotics LLC") != normalize_company("Acme Robotics, L.L.C.")


def test_a_company_named_only_by_a_suffix_survives() -> None:
    """Stripping must never produce the empty string."""
    assert normalize_company("Limited") != ""


def test_two_different_companies_stay_different() -> None:
    """The suffix stripper must not over-merge."""
    assert normalize_company("Acme Robotics") != normalize_company("Acme Aerospace")


@pytest.mark.parametrize(
    ("raw", "expected_equal"),
    [
        ("Senior Backend Engineer (Remote)", "Senior Backend Engineer"),
        ("Senior Backend Engineer - San Francisco", "Senior Backend Engineer"),
        ("Senior Backend Engineer [REQ-4012]", "Senior Backend Engineer"),
    ],
)
def test_title_normalisation_strips_decorations(raw, expected_equal) -> None:
    """Requisition ids, bracketed locations and arrangement hints are not part of the role."""
    assert normalize_title(raw) == normalize_title(expected_equal)


def test_title_normalisation_keeps_seniority() -> None:
    """ "Senior" and "Junior" are the job, not decoration."""
    assert normalize_title("Senior Backend Engineer") != normalize_title("Junior Backend Engineer")


def test_location_normalisation_is_stable() -> None:
    """Spelling variants of one place collapse."""
    assert normalize_location("San Francisco, CA") == normalize_location("san francisco, ca")


# ======================================================================================
# Near-duplicates and the similarity judgement
# ======================================================================================


def test_identical_postings_are_duplicates() -> None:
    """The trivial case, as a floor on the similarity function."""
    assert similarity(_posting(), _posting()) == pytest.approx(1.0)
    assert is_duplicate(_posting(), _posting()) is True


def test_the_same_role_syndicated_to_two_boards_is_judged_a_duplicate() -> None:
    """What ``dedupe_key`` deliberately will not do, ``is_duplicate`` will — reversibly."""
    greenhouse = _posting(provider=ATSProviderName.GREENHOUSE, external_id="4012")
    lever = _posting(
        provider=ATSProviderName.LEVER,
        external_id="xyz-9",
        url="https://jobs.lever.co/acme/xyz-9",
    )

    assert dedupe_key(greenhouse) != dedupe_key(lever)
    assert is_duplicate(greenhouse, lever) is True


def test_two_different_roles_at_one_company_are_not_duplicates() -> None:
    """The expensive mistake. A backend and a frontend opening must both survive."""
    backend = _posting(title="Senior Backend Engineer", external_id="1")
    frontend = _posting(title="Senior Frontend Engineer", external_id="2")

    assert is_duplicate(backend, frontend) is False


def test_two_seniorities_of_one_role_are_not_duplicates() -> None:
    """ "Senior" and "Staff" are different jobs with different pay."""
    senior = _posting(title="Senior Backend Engineer", external_id="1")
    staff = _posting(title="Staff Backend Engineer", external_id="2")
    assert is_duplicate(senior, staff) is False


def test_the_same_title_at_two_companies_is_not_a_duplicate() -> None:
    """Title alone is not identity."""
    acme = _posting(company_name="Acme Robotics", external_id="1")
    initech = _posting(company_name="Initech", external_id="2")
    assert is_duplicate(acme, initech) is False


def test_a_decorated_title_still_matches_its_plain_form() -> None:
    """The near-duplicate case dedupe exists for."""
    plain = _posting(title="Senior Backend Engineer", external_id="1")
    decorated = _posting(title="Senior Backend Engineer (Remote) [REQ-4012]", external_id="2")
    assert is_duplicate(plain, decorated) is True


def test_similarity_is_symmetric() -> None:
    """``similarity(a, b) == similarity(b, a)``, or the merge depends on iteration order."""
    a = _posting(title="Senior Backend Engineer", external_id="1")
    b = _posting(title="Backend Engineer, Senior", external_id="2")
    assert similarity(a, b) == pytest.approx(similarity(b, a))


def test_similarity_is_bounded() -> None:
    """A score outside 0-1 would make the threshold meaningless."""
    a = _posting(title="Senior Backend Engineer", external_id="1")
    b = _posting(title="Warehouse Associate", company_name="Other Co", external_id="2")
    assert 0.0 <= similarity(a, b) <= 1.0


def test_the_threshold_is_conservative() -> None:
    """0.92 is documented as high on purpose; a lower default would merge real openings."""
    assert DEFAULT_SIMILARITY_THRESHOLD >= 0.9


def test_the_threshold_is_honoured() -> None:
    """An explicit threshold overrides the default in the direction the caller asked for."""
    backend = _posting(title="Senior Backend Engineer", external_id="1")
    frontend = _posting(title="Senior Frontend Engineer", external_id="2")

    assert is_duplicate(backend, frontend, threshold=0.99) is False
    assert is_duplicate(backend, backend, threshold=0.99) is True
