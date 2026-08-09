"""Golden rule #9 — cache aggressively, invalidate precisely.

    Content-addressed keys, never cache a mutation.

This was the one golden rule with no assertion behind it. The others each have a test that
goes red when the rule is broken; this one was documented and trusted.

The failure it guards against is the nastiest in the whole system, because it is silent and
it crosses a privacy boundary. Every expensive artefact here is cached — embeddings, LLM
completions, tailored resumes — and the tailoring cache holds *the user's work history*. If
two users can ever produce the same cache key for different inputs, one person's employers,
projects and metrics are served onto another person's resume. Nothing raises. Nothing is
logged. The resume simply comes out belonging to someone else.

So the tests below are about **key separation**, not about hit rates:

* changing any single input component changes the key (user, posting, preferences, fact set,
  template, variant, budget, model);
* two different users never collide, even when every other input is byte-identical, which is
  exactly the shape of two people applying to the same job;
* keys are stable across processes, because a key derived from a salted ``hash()`` would
  silently miss on every restart and, worse, could alias differently per run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.cache.keys import NAMESPACES, hash_payload, make_key

REPO_ROOT = Path(__file__).resolve().parents[1]


def _key(**over: Any) -> str:
    """Return the key **the production engine actually computes** for one tailoring.

    This calls :meth:`ResumeEngine._cache_key` rather than reimplementing it. That
    distinction is the whole value of the test: a mirror of the key can only ever prove the
    mirror is self-consistent, and would stay green while the real key silently dropped the
    user id. The first version of this file made exactly that mistake — dropping ``user_id``
    from the production key left every assertion passing.

    Args:
        **over: Field overrides applied to the default request. Recognised keys are
            ``user_id``, ``title``, ``company``, ``description``, ``min_score``, ``fact_ids``,
            ``template``, ``variant_label``, ``max_bullets`` and ``model``.

    Returns:
        The content-addressed cache key.
    """
    from app.ai.resume_engine import ResumeEngine, TailorRequest
    from app.jobs.base import JobPostingDTO, UserProfileDTO
    from app.models.enums import ATSProviderName
    from app.models.user import UserPreferences

    user = UserProfileDTO(
        full_name="Ada Lovelace",
        email="ada@example.com",
        user_id=over.get("user_id", "11111111-1111-1111-1111-111111111111"),
    )
    posting = JobPostingDTO(
        provider=ATSProviderName.GREENHOUSE,
        external_id="job-1",
        url="https://boards.greenhouse.io/acme/jobs/1",
        title=over.get("title", "Embedded Engineer"),
        company_name=over.get("company", "Acme"),
        description=over.get("description", "Firmware, RTOS, C++."),
    )
    request = TailorRequest(
        user=user,
        posting=posting,
        prefs=UserPreferences(min_score=over.get("min_score", 70)),
        template=over.get("template", "modern"),
        max_bullets=over.get("max_bullets", 18),
        variant_label=over.get("variant_label"),
    )
    facts = [_FakeFact(fid) for fid in over.get("fact_ids", ["fact-a", "fact-b"])]

    engine = ResumeEngine.__new__(ResumeEngine)  # no collaborators needed to build a key
    engine.llm = _FakeModel(over.get("model", "claude-sonnet-4-5"))  # type: ignore[attr-defined]
    return engine._cache_key(request, facts)  # accessing the real key fn on purpose


class _FakeFact:
    """Minimal stand-in carrying only the ``id`` the key derives from."""

    def __init__(self, fact_id: str) -> None:
        self.id = fact_id


class _FakeModel:
    """Minimal stand-in carrying only the ``model`` name the key derives from."""

    def __init__(self, model: str) -> None:
        self.model = model


# ---------------------------------------------------------------------------------------
# Key separation
# ---------------------------------------------------------------------------------------


def test_two_users_never_share_a_key_for_the_same_posting() -> None:
    """The privacy-critical case: same job, same prefs, same everything but the person.

    Two people applying to the same posting with default preferences differ in exactly one
    component. If the user id were ever dropped from the key, this is the scenario that
    serves one of them the other's work history.
    """
    alice = _key(user_id=str(uuid.uuid4()))
    bob = _key(user_id=str(uuid.uuid4()))
    assert alice != bob


def test_missing_user_id_does_not_alias_two_anonymous_callers() -> None:
    """An absent user id must not collapse distinct fact sets onto one key.

    ``ResumeEngine._cache_key`` tolerates a profile without ``user_id`` by substituting an
    empty string. That is fine only while the rest of the key still separates the callers —
    assert the fact set alone is enough.
    """
    one = _key(user_id="", fact_ids=["fact-a"])
    two = _key(user_id="", fact_ids=["fact-z"])
    assert one != two


@pytest.mark.parametrize(
    "component,replacement",
    [
        ("user_id", "22222222-2222-2222-2222-222222222222"),
        ("title", "GPU Engineer"),
        ("company", "Nvidia"),
        ("description", "CUDA kernels and TensorRT."),
        ("min_score", 85),
        ("fact_ids", ["fact-a", "fact-c"]),
        ("template", "classic"),
        ("variant_label", "robotics"),
        ("max_bullets", 24),
        ("model", "gpt-4.1-mini"),
    ],
)
def test_every_component_participates_in_the_key(component: str, replacement: Any) -> None:
    """Changing any one input must change the key.

    A component that does not participate is a silent staleness bug: switch template and get
    the old layout, tighten preferences and get the old selection.
    """
    assert _key() != _key(**{component: replacement})


def test_identical_inputs_produce_an_identical_key() -> None:
    """The other half of the contract — caching has to actually hit."""
    assert _key() == _key()


def test_namespaces_are_distinct() -> None:
    """Namespaces must not collide: an embedding must never satisfy an LLM read."""
    payload = ("same", "parts", 1)
    keys = {
        make_key(NAMESPACES.LLM, *payload),
        make_key(NAMESPACES.EMBEDDING, *payload),
        make_key(NAMESPACES.RENDER, *payload),
    }
    assert len(keys) == 3


# ---------------------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------------------


def test_keys_are_stable_across_processes() -> None:
    """A key built from ``hash()`` would differ per interpreter (PYTHONHASHSEED is random).

    That failure mode is quiet and expensive: every cache read misses after a restart, so the
    cache looks like it works while paying full cost forever. Two child processes with
    different hash seeds must agree.
    """
    program = (
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "from app.cache.keys import NAMESPACES, make_key;"
        "print(make_key(NAMESPACES.LLM, 'alpha', 'beta', 7))"
    )
    # Inherit the parent environment and override only the seed. Blanking it breaks the
    # child on Windows, where the interpreter needs PATH to resolve its own DLLs.
    seen = set()
    for seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed, SQLITE_MODE="true")
        completed = subprocess.run(  # fixed argv, no shell
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env=env,
            cwd=REPO_ROOT,
        )
        seen.add(completed.stdout.strip())
    assert len(seen) == 1, f"cache key is not stable across processes: {seen}"


def test_hash_payload_is_order_insensitive_for_mappings() -> None:
    """Dict ordering must not change the key, or logically identical inputs miss."""
    assert hash_payload({"a": 1, "b": 2}) == hash_payload({"b": 2, "a": 1})


def test_hash_payload_distinguishes_list_order() -> None:
    """Sequence order *is* meaningful — a reordered resume is a different resume."""
    assert hash_payload(["a", "b"]) != hash_payload(["b", "a"])


def test_no_salted_hash_in_cache_key_construction() -> None:
    """Static guard: ``app/cache/keys.py`` must never use the builtin ``hash()``.

    Cheap to assert, and it fails at the moment someone reaches for the obvious function
    rather than months later when a cache mysteriously stops hitting.
    """
    source = (REPO_ROOT / "app" / "cache" / "keys.py").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "hash(" in line
        and "hashlib" not in line
        and "hash_payload" not in line
        and "__hash__" not in line
        and not line.strip().startswith(("#", "*", '"', "'"))
    ]
    assert not offenders, f"builtin hash() in cache key construction: {offenders}"
