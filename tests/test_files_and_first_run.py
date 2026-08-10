"""Uploads, and the first run that has no account yet.

Two features that only ever run at the edges, and whose failure modes are both silent.

**First run** is the one state nobody develops in. Every developer machine has a seeded user,
so `CurrentUser` always resolves and the wizard always renders — right up until someone
clones the repo. Then `GET /onboarding/steps` 404s because no account exists, the client
renders zero fields, and the form whose submission creates the account can never be filled
in. The app lands on a dashboard where every screen 404s and offers no route out. These tests
run the endpoints with an empty database on purpose, because that is the only way the bug is
visible.

**Uploads** are where a filename becomes a path, a size becomes an allocation, and a digest
becomes a cache key. Each of those is a place to be quietly wrong: a traversing filename, a
lying `Content-Length`, a per-process `hash()`. So the tests here assert the properties
rather than the happy path — that the key is the real SHA-256, that the ceiling is enforced
against bytes actually read, and that a file belonging to someone else is indistinguishable
from one that does not exist.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings

pytestmark = pytest.mark.asyncio

#: A small, recognisable resume. Text rather than a PDF so the assertions are about the
#: upload path and not about whichever parser happens to be installed.
RESUME_BYTES = b"Ada Lovelace\nEngineer\n\nEXPERIENCE\n- Wrote the first program.\n"


# ======================================================================================
# First run — no account exists yet
# ======================================================================================


@pytest.fixture
async def blank_client(session: AsyncSession, settings: Settings):
    """An API client over a database with **no user at all**.

    Deliberately does not depend on the ``user`` fixture and does not override
    ``get_current_user``: the whole point is to exercise the identity dependencies for real,
    against nothing.
    """
    import httpx

    from app.database.session import get_session
    from app.main import create_app
    from app.plugins.loader import load_all
    from app.plugins.registry import registry

    registry.configure(settings)
    load_all()
    application = create_app(settings)

    async def _session_override():
        yield session

    application.dependency_overrides[get_session] = _session_override
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    application.dependency_overrides.clear()


async def test_wizard_renders_before_any_account_exists(blank_client: httpx.AsyncClient) -> None:
    """`GET /onboarding/steps` answers on a fresh install.

    The regression this locks down is a deadlock, not a missing feature: under `CurrentUser`
    this 404s, the client renders no fields, and the only flow that can create an account
    becomes unreachable.
    """
    response = await blank_client.get("/api/v1/onboarding/steps")

    assert response.status_code == 200
    steps = response.json()
    assert [step["key"] for step in steps][:2] == ["identity", "contact"]
    assert all(step["complete"] is False for step in steps), "nothing is answered yet"


async def test_status_reports_not_started_rather_than_missing(
    blank_client: httpx.AsyncClient,
) -> None:
    """`GET /onboarding/status` describes the first-run state instead of 404ing on it."""
    response = await blank_client.get("/api/v1/onboarding/status")

    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is False
    assert body["current_step"] == "identity"
    assert body["progress"] == 0.0


async def test_identity_step_creates_the_account(
    blank_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Submitting `identity` is what brings the user row into existence."""
    from sqlalchemy import func, select

    from app.models.user import User

    assert await session.scalar(select(func.count()).select_from(User)) == 0

    response = await blank_client.post(
        "/api/v1/onboarding/steps/identity",
        json={"full_name": "Ada Lovelace", "email": "ada@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["current_step"] == "contact", "identity is now behind us"

    created = (await session.execute(select(User))).scalars().all()
    assert [(row.email, row.full_name) for row in created] == [
        ("ada@example.com", "Ada Lovelace")
    ]
    assert created[0].profile is not None, "the profile is populated, not lazily loaded later"


async def test_identity_without_an_email_is_refused(blank_client: httpx.AsyncClient) -> None:
    """Email is optional *once* an account exists — never on the call that creates one.

    The field's own placeholder promises it "defaults to your account address", and on a
    first run that account is what this request is making.
    """
    response = await blank_client.post(
        "/api/v1/onboarding/steps/identity", json={"full_name": "Ada Lovelace"}
    )

    assert response.status_code == 422
    assert "email" in response.json()["detail"].lower()


async def test_other_steps_refuse_to_run_before_identity(blank_client: httpx.AsyncClient) -> None:
    """A step that is not `identity` has nothing to write onto, and says so."""
    response = await blank_client.post(
        "/api/v1/onboarding/steps/contact", json={"phone": "+1 555 0100"}
    )

    assert response.status_code == 422
    assert "identity" in response.json()["detail"]


async def test_a_bad_user_header_is_still_an_error(blank_client: httpx.AsyncClient) -> None:
    """`X-User-Id` is an explicit claim, so naming nobody stays a mistake worth reporting.

    Only the *absence* of any account is a first-run state; a header pointing at a user that
    does not exist is not, and quietly treating it as one would let a typo silently onboard a
    second profile.
    """
    response = await blank_client.get(
        "/api/v1/onboarding/steps",
        headers={"X-User-Id": "1a1b3f5f-0000-4000-8000-000000000000"},
    )

    assert response.status_code == 404


# ======================================================================================
# Uploads
# ======================================================================================


async def _upload(client: httpx.AsyncClient, name: str, data: bytes, kind: str) -> Any:
    """Post one multipart file and return the parsed response."""
    return await client.post(
        "/api/v1/files",
        files={"file": (name, data, "text/plain")},
        data={"kind": kind},
    )


async def test_upload_stores_bytes_and_catalogues_them(
    api_client: httpx.AsyncClient, settings: Settings
) -> None:
    """An upload lands on disk and produces a row that points at it."""
    response = await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "resume.txt"
    assert body["kind"] == "master_resume"
    assert body["size_bytes"] == len(RESUME_BYTES)
    assert (settings.storage_root / body["storage_key"]).read_bytes() == RESUME_BYTES


async def test_storage_key_is_the_real_digest(api_client: httpx.AsyncClient) -> None:
    """The key is content-addressed with `hashlib`, not with the salted builtin `hash`.

    Golden rule #9. A per-process salt would make the same file land on a different key every
    time the API restarted, so nothing would ever deduplicate and no cached derivation of a
    file could be reused across runs.
    """
    response = await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")

    digest = hashlib.sha256(RESUME_BYTES).hexdigest()
    assert response.json()["sha256"] == digest
    assert digest in response.json()["storage_key"]


async def test_same_bytes_share_one_object_and_keep_two_records(
    api_client: httpx.AsyncClient, settings: Settings
) -> None:
    """Two uploads of one file are two events over one blob.

    Collapsing the records would make deleting one delete the other's metadata; sharing the
    blob is what keeps a re-upload from doubling disk use.
    """
    first = await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")
    second = await _upload(api_client, "resume-final.txt", RESUME_BYTES, "master_resume")

    assert first.json()["id"] != second.json()["id"]
    assert first.json()["storage_key"] == second.json()["storage_key"]
    stored = list((settings.storage_root / "uploads" / "master_resume").iterdir())
    assert len(stored) == 1


async def test_upload_over_the_ceiling_is_refused(
    api_client: httpx.AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is enforced against bytes actually read.

    Not against `Content-Length`, which a multipart client is not obliged to send and an
    attacker is not obliged to mean.
    """
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    oversized = b"x" * (2 * 1024 * 1024)

    response = await _upload(api_client, "big.txt", oversized, "other")

    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]


async def test_empty_upload_is_refused(api_client: httpx.AsyncClient) -> None:
    """Zero bytes is not a file. Storing it would produce a source with nothing to read."""
    response = await _upload(api_client, "nothing.txt", b"", "other")

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../.ssh/authorized_keys", "authorized_keys"),
        ("C:\\Windows\\System32\\evil.dll", "evil.dll"),
        ("...", "upload"),
    ],
)
async def test_hostile_filenames_are_reduced_to_a_display_name(
    api_client: httpx.AsyncClient, sent: str, stored: str
) -> None:
    """A multipart filename is attacker-controlled and is not a path.

    It never reaches the filesystem — the key comes from the digest — but it is echoed in
    `Content-Disposition` and shown in the UI, so it is stripped to its last component.
    """
    response = await _upload(api_client, sent, RESUME_BYTES, "other")

    assert response.status_code == 201
    assert response.json()["filename"] == stored


def test_a_nameless_part_still_gets_a_display_name() -> None:
    """Multipart permits a part with no filename, so there is a fallback rather than a crash.

    Asserted at the function rather than over HTTP: an empty ``filename`` on the wire is
    parsed as an ordinary form field, not as a file, so the request never reaches the code
    this is about.
    """
    from app.api.routes.files import _safe_filename

    assert _safe_filename(None) == "upload"
    assert _safe_filename("") == "upload"
    assert _safe_filename("   ") == "upload"


async def test_download_returns_the_exact_bytes(api_client: httpx.AsyncClient) -> None:
    """A round trip is byte-identical, and the response says what the file is called."""
    uploaded = (await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")).json()

    response = await api_client.get(f"/api/v1/files/{uploaded['id']}/content")

    assert response.status_code == 200
    assert response.content == RESUME_BYTES
    assert 'filename="resume.txt"' in response.headers["content-disposition"]


async def test_another_users_file_is_indistinguishable_from_a_missing_one(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Ownership is part of the lookup, so a 404 never confirms that an id exists."""
    from app.models.enums import DocumentKind
    from app.models.file import UploadedFile
    from app.models.user import User

    stranger = User(email="stranger@example.com", full_name="Someone Else")
    session.add(stranger)
    await session.flush()
    theirs = UploadedFile(
        user_id=stranger.id,
        kind=DocumentKind.MASTER_RESUME,
        filename="private.pdf",
        storage_key="uploads/master_resume/deadbeef.pdf",
        size_bytes=1,
        backend="local",
    )
    session.add(theirs)
    await session.commit()

    assert (await api_client.get(f"/api/v1/files/{theirs.id}")).status_code == 404
    assert (await api_client.get(f"/api/v1/files/{theirs.id}/content")).status_code == 404


async def test_deleting_a_file_hides_it_but_keeps_the_bytes(
    api_client: httpx.AsyncClient, settings: Settings
) -> None:
    """Delete is a soft delete: another record may be content-addressed to the same object."""
    uploaded = (await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")).json()

    assert (await api_client.delete(f"/api/v1/files/{uploaded['id']}")).status_code == 200
    assert (await api_client.get(f"/api/v1/files/{uploaded['id']}")).status_code == 404
    assert (settings.storage_root / uploaded["storage_key"]).exists()


# ======================================================================================
# A file as a knowledge source
# ======================================================================================


async def test_a_source_takes_a_file_id_instead_of_a_path(
    api_client: httpx.AsyncClient, settings: Settings
) -> None:
    """Uploading a resume and registering it are two calls, not a path the user must know.

    The server resolves the id to the stored path, because the file the user just chose in a
    picker is one whose absolute path they have no way to type.
    """
    uploaded = (await _upload(api_client, "resume.txt", RESUME_BYTES, "master_resume")).json()

    response = await api_client.post(
        "/api/v1/knowledge/sources", json={"kind": "resume", "file_id": uploaded["id"]}
    )

    assert response.status_code in {200, 201}
    body = response.json()
    assert body["kind"] == "resume"
    assert body["uri"].endswith(uploaded["storage_key"].rsplit("/", 1)[-1])
    assert body["label"] == "resume.txt", "the filename names the source when nothing else does"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "resume"}, id="neither"),
        pytest.param(
            {
                "kind": "resume",
                "uri": "C:/tmp/cv.pdf",
                "file_id": "1a1b3f5f-0000-4000-8000-000000000000",
            },
            id="both",
        ),
    ],
)
async def test_a_source_needs_exactly_one_location(
    api_client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    """Both is ambiguous about what identity is keyed on; neither leaves nothing to read."""
    response = await api_client.post("/api/v1/knowledge/sources", json=payload)

    assert response.status_code == 422


async def test_a_source_rejects_a_file_belonging_to_someone_else(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """`file_id` is scoped to the caller, so it cannot be used to index another profile's upload."""
    from app.models.enums import DocumentKind
    from app.models.file import UploadedFile
    from app.models.user import User

    stranger = User(email="stranger2@example.com", full_name="Someone Else")
    session.add(stranger)
    await session.flush()
    theirs = UploadedFile(
        user_id=stranger.id,
        kind=DocumentKind.MASTER_RESUME,
        filename="private.pdf",
        storage_key="uploads/master_resume/cafebabe.pdf",
        size_bytes=1,
        backend="local",
    )
    session.add(theirs)
    await session.commit()

    response = await api_client.post(
        "/api/v1/knowledge/sources", json={"kind": "resume", "file_id": str(theirs.id)}
    )

    assert response.status_code == 404
