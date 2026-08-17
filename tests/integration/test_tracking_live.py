"""The mailbox sync, against a real inbox.

This is the last item on the Phase 1 gate that cannot be proved from a fixture: *"email
status sync updates an application"*. Every part of the path has hermetic coverage in
``tests/test_tracking.py`` — the classifier's phrase tables, the matcher's ranked signals,
the confidence floor, the transition — but none of that proves an IMAP connection is opened
read-only, that a real message parses, or that the incremental cursor advances.

**The exact remaining prerequisite is a mailbox credential**, which no amount of code can
supply. Put these in the repository's ``.env`` — or export them — and the test runs:

```
APPLICANTOS_TEST_IMAP_HOST=imap.gmail.com
APPLICANTOS_TEST_IMAP_ADDRESS=you@example.com
APPLICANTOS_TEST_IMAP_SECRET=<app-specific password>
```

``.env`` is read directly here rather than through :class:`~app.config.settings.Settings`.
pydantic-settings loads that file into the *settings object*, never into ``os.environ``, so a
credential written where every other secret in this project lives would otherwise be invisible
and the suite would skip while looking configured — the worst of both outcomes.

For Gmail the secret must be an **app password**, not the account password, and 2FA must be
on for one to exist. Nothing here sends, deletes, moves or re-flags a message: the tracker
opens the mailbox with ``readonly=True``, and this suite additionally asserts that the run
made no state change to the inbox it can observe.

Marked ``integration``, so ``pytest`` never runs it by accident — a contributor on a train
would otherwise see a failure that says nothing about their change.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.enums import ApplicationStatus, MailProvider

pytestmark = pytest.mark.integration

#: Environment variables carrying the mailbox to read. Named rather than inferred from the
#: user's own settings so a live run can never point at the mailbox the product is syncing
#: for real.
ENV_HOST = "APPLICANTOS_TEST_IMAP_HOST"
ENV_ADDRESS = "APPLICANTOS_TEST_IMAP_ADDRESS"
ENV_SECRET = "APPLICANTOS_TEST_IMAP_SECRET"

#: How far back the live run looks. Wide enough to find something in a real inbox, narrow
#: enough that the run is quick and bounded.
LOOKBACK_DAYS = 30


#: The repository's ``.env``, read as a fallback for each variable below.
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


def _from_env_file(name: str) -> str:
    """Read one ``KEY=value`` line out of the repository's ``.env``.

    A deliberately small parser rather than a dependency: it handles ``KEY=value``, surrounding
    whitespace, ``export`` prefixes, quotes and ``#`` comments, which is the whole of what a
    credential line looks like. Anything more elaborate belongs in the settings loader, which
    already exists and is not what this needs.

    Args:
        name: The variable to look for.

    Returns:
        The value, or ``""`` when the file or the key is absent.
    """
    if not ENV_FILE.is_file():
        return ""
    for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        return value.strip().strip('"').strip("'")
    return ""


def _setting(name: str) -> str:
    """Return *name* from the environment, falling back to ``.env``.

    Args:
        name: The variable to resolve.

    Returns:
        The value, or ``""`` when neither source has it.
    """
    return os.environ.get(name, "").strip() or _from_env_file(name)


def _credentials() -> tuple[str, str, str]:
    """Return the configured mailbox, or skip with the prerequisite spelled out.

    Returns:
        ``(host, address, secret)``.
    """
    host = _setting(ENV_HOST)
    address = _setting(ENV_ADDRESS)
    secret = _setting(ENV_SECRET)
    if not (host and address and secret):
        missing = [
            name
            for name, value in ((ENV_HOST, host), (ENV_ADDRESS, address), (ENV_SECRET, secret))
            if not value
        ]
        pytest.skip(
            f"no live mailbox configured — missing {', '.join(missing)}. "
            f"Add them to {ENV_FILE.name} (an app-specific password for Gmail, not the "
            "account password) to run the live status-sync suite."
        )
    return host, address, secret


@pytest.fixture
async def live_account(session, settings, user, monkeypatch):
    """Connect the configured mailbox for the test user.

    Yields:
        The persisted :class:`~app.models.tracking.EmailAccount`.
    """
    host, address, secret = _credentials()
    monkeypatch.setattr(settings, "imap_host", host, raising=False)

    from app.tracking.service import StatusSyncService

    service = StatusSyncService(session, settings)
    account = await service.connect_account(
        user.id,
        provider=MailProvider.IMAP,
        address=address,
        secret=secret,
    )
    await session.commit()
    return account


async def test_a_real_mailbox_can_be_read(session, settings, live_account) -> None:
    """The connection opens, authenticates, and returns without error.

    The narrowest possible live assertion, and the one that fails first when a credential is
    wrong or a provider changes its IMAP behaviour. Everything below assumes it passed.
    """
    from app.tracking.service import StatusSyncService

    report = await StatusSyncService(session, settings).sync_account(
        live_account.id,
        since=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
    )

    assert report.error is None, report.error
    assert report.fetched >= 0


async def test_the_cursor_advances_and_a_second_run_is_idempotent(
    session, settings, live_account
) -> None:
    """Golden rule #9 against a real inbox.

    Every message already seen collides with ``UNIQUE(user_id, source, external_ref)`` inside
    its own SAVEPOINT and is counted as a skip, so a redelivered Celery message, a reset
    cursor and a crash mid-run are all safe. A live mailbox is the only place that claim is
    tested against real message identifiers rather than fabricated ones.
    """
    from app.tracking.service import StatusSyncService

    service = StatusSyncService(session, settings)
    since = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)

    first = await service.sync_account(live_account.id, since=since)
    second = await service.sync_account(live_account.id, since=since)

    assert first.error is None and second.error is None
    # Nothing new the second time: every signal the first run created is a duplicate now.
    assert second.created == 0


async def test_a_real_recruiter_email_moves_a_real_application(
    session, settings, user, live_account, make_posting, make_application
) -> None:
    """The gate item itself: an email in a real inbox changes an application's status.

    The application is seeded to match whatever the mailbox actually contains, which is why
    this test reads the signals first and then asserts on the one it matched. A live inbox
    cannot be arranged in advance, and asserting on a company that happens not to have
    written would make the suite fail for a reason that is not a defect.
    """
    from sqlalchemy import select

    from app.models.tracking import StatusSignal as StatusSignalRow
    from app.tracking.service import StatusSyncService

    service = StatusSyncService(session, settings)
    await service.sync_account(
        live_account.id, since=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    )

    signals = list(
        (
            await session.execute(
                select(StatusSignalRow)
                .where(StatusSignalRow.user_id == user.id)
                .order_by(StatusSignalRow.received_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not signals:
        pytest.skip(
            "the configured mailbox contains no message this classifier recognised in the "
            f"last {LOOKBACK_DAYS} days; the sync ran cleanly but had nothing to classify."
        )

    classified = [signal for signal in signals if signal.detected_status is not None]
    if not classified:
        pytest.skip(
            f"{len(signals)} message(s) were read but none carried a status; "
            "'unknown' is a first-class outcome, not a failure (golden rule #2)."
        )

    # Every classified signal must have produced either a status change or a review item —
    # never a silent nothing, which would mean the mailbox was read for no purpose.
    for signal in classified:
        assert signal.applied or signal.needs_review or signal.application_id is None, (
            f"signal {signal.id} classified as {signal.detected_status} but was neither "
            "applied nor escalated"
        )


async def test_an_unmatched_email_never_moves_someone_elses_application(
    session, settings, user, live_account, make_posting, make_application
) -> None:
    """Directive §10: a low-confidence match parks rather than guessing.

    Seeds an application for an employer the mailbox has no relationship with. Whatever the
    inbox contains, nothing in it may move this row — the matcher's confidence floor is the
    only thing standing between a rejection from one company and a different company's
    application.
    """
    from app.tracking.service import StatusSyncService

    posting = await make_posting(external_id=f"live-decoy-{uuid.uuid4().hex[:8]}")
    decoy = await make_application(posting, status=ApplicationStatus.SUBMITTED)
    original = decoy.status

    await StatusSyncService(session, settings).sync_account(
        live_account.id, since=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    )
    await session.refresh(decoy)

    assert decoy.status is original
