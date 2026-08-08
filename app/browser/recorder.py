"""Artifact recording — the evidence one application attempt leaves behind.

``docs/CONTRACTS.md`` §12. An :class:`ArtifactRecorder` owns exactly one directory,
``settings.screenshot_path / {application_id}``, and everything an apply attempt wants to
keep goes through it: the screenshots §12 rule 3 requires before and after every submit, the
page HTML that proves what the form actually said, the discovered fields and planned answers,
and a timeline of what happened in what order.

That evidence is not decoration. When an application lands in ``NEEDS_REVIEW``, a human opens
this directory to answer "what did the robot see, and where did it stop?" — and when an
employer says they never received an application, the confirmation screenshot is the only
thing that disagrees. So the recorder is written to *never throw*: a failed write is logged
and recorded as a failed write, because losing one artifact must not turn a successful
submission into a crashed one.

**Nothing a caller passes can escape the directory.** Every name is slugified down to
``[a-z0-9-]`` by :func:`~app.browser.playwright_runner.slugify_artifact_name` — which can
produce neither a separator nor ``..`` — and the composed path is then re-checked against the
directory before anything is written. A label is a label, never a path.

The recorder is synchronous. Its writes are small local files, it is called from the middle
of an async apply flow purely for bookkeeping, and making it awaitable would mean either
lying about blocking or pulling in a thread pool for a few kilobytes. :meth:`finalize` is the
one method that produces a value the pipeline stores: a manifest of every file, its label,
when it was written and how large it is, which is what
``GET /api/v1/applications/{id}/artifacts`` serves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from app.browser.playwright_runner import (
    SCREENSHOT_ORDINAL_WIDTH,
    slugify_artifact_name,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.config.settings import Settings

__all__ = [
    "ARTIFACT_KIND_EVENT",
    "ARTIFACT_KIND_HTML",
    "ARTIFACT_KIND_JSON",
    "ARTIFACT_KIND_SCREENSHOT",
    "EVENTS_FILENAME",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "ArtifactRecord",
    "ArtifactRecorder",
    "RecordedEvent",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Name of the manifest :meth:`ArtifactRecorder.finalize` writes into the directory. Excluded
#: from its own file listing, so re-finalising does not make the manifest grow every time.
MANIFEST_FILENAME: Final[str] = "manifest.json"

#: Name of the newline-delimited event log written beside the manifest.
EVENTS_FILENAME: Final[str] = "events.jsonl"

#: Manifest schema version, so a stored manifest read back by a later build is interpretable.
MANIFEST_VERSION: Final[int] = 1

#: Artifact kind: a page capture.
ARTIFACT_KIND_SCREENSHOT: Final[str] = "screenshot"

#: Artifact kind: serialised page markup.
ARTIFACT_KIND_HTML: Final[str] = "html"

#: Artifact kind: a structured payload — discovered fields, planned answers, a result.
ARTIFACT_KIND_JSON: Final[str] = "json"

#: Artifact kind: a timeline entry rather than a file.
ARTIFACT_KIND_EVENT: Final[str] = "event"

#: Files written per kind get this extension.
_KIND_SUFFIXES: Final[dict[str, str]] = {
    ARTIFACT_KIND_SCREENSHOT: ".png",
    ARTIFACT_KIND_HTML: ".html",
    ARTIFACT_KIND_JSON: ".json",
}

#: Width of the zero-padded ordinal prefixed to every filename, so this recorder's own
#: artifacts sort into the order they were written. Deliberately the same width as
#: :data:`~app.browser.playwright_runner.SCREENSHOT_ORDINAL_WIDTH`: a session writes its
#: screenshots straight into this directory from a counter of its own, and two different
#: paddings would make the listing sort by width rather than by number. The manifest's
#: ``recorded_at`` is the authoritative timeline across both writers.
_ORDINAL_WIDTH: Final[int] = SCREENSHOT_ORDINAL_WIDTH

#: Encoding for every text artifact. Explicit because Windows still defaults to cp1252, and a
#: job description containing an em-dash would otherwise fail to write.
_TEXT_ENCODING: Final[str] = "utf-8"

#: Indentation of the manifest, which is read by humans far more often than by machines.
_JSON_INDENT: Final[int] = 2

#: Longest event message retained. An event log is a timeline, not a place to inline a
#: 200KB stack trace or a whole page of HTML.
_MAX_EVENT_MESSAGE_CHARS: Final[int] = 2_000

#: Stem used when a caller's name slugifies to nothing.
_FALLBACK_STEM: Final[str] = "artifact"


def _now() -> datetime:
    """Return the current UTC time.

    Returns:
        A timezone-aware :class:`~datetime.datetime`. Aware and UTC because artifact
        timestamps are compared against database rows written by other processes.
    """
    return datetime.now(UTC)


def _json_default(value: Any) -> str:
    """Serialise an object :mod:`json` does not understand.

    Args:
        value: Whatever the encoder could not handle — a :class:`~pathlib.Path`, a
            :class:`~uuid.UUID`, an enum, a dataclass instance.

    Returns:
        Its string form. Falling back to ``str`` rather than raising is deliberate: an
        artifact is diagnostic, and a manifest that says ``"PosixPath('/tmp/x')"`` is
        infinitely more useful than a ``TypeError`` thrown while recording evidence about a
        failure.
    """
    return str(value)


# ======================================================================================
# Records
# ======================================================================================


@dataclass(slots=True)
class ArtifactRecord:
    """One file this recorder wrote.

    Attributes:
        name: Filename inside the artifacts directory.
        path: Absolute path to the file.
        kind: One of :data:`ARTIFACT_KIND_SCREENSHOT`, :data:`ARTIFACT_KIND_HTML`,
            :data:`ARTIFACT_KIND_JSON`.
        label: The human-readable label the caller supplied, kept verbatim — the slug in
            *name* has already lost the punctuation a reviewer might want to read.
        recorded_at: When the file was written.
        size_bytes: Size on disk at the moment it was recorded, or ``0`` when the write
            failed.
        error: Why the write failed, when it did. A recorded failure is more useful than a
            silently missing file.
    """

    name: str
    path: Path
    kind: str
    label: str
    recorded_at: datetime
    size_bytes: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of this record, as it appears in the manifest.

        Returns:
            A mapping with ``path`` stringified and ``recorded_at`` in ISO-8601.
        """
        return {
            "name": self.name,
            "path": str(self.path),
            "kind": self.kind,
            "label": self.label,
            "recorded_at": self.recorded_at.isoformat(),
            "size_bytes": self.size_bytes,
            "error": self.error,
        }


@dataclass(slots=True)
class RecordedEvent:
    """One entry in the attempt's timeline.

    Attributes:
        kind: A short machine-readable category — ``"navigate"``, ``"fill"``, ``"blocker"``,
            ``"submit"``.
        message: What happened, in a sentence a human resolving a review item can read.
        payload: Structured detail, JSON-serialised on a best-effort basis.
        at: When it happened.
    """

    kind: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of this event.

        Returns:
            A mapping with ``at`` in ISO-8601.
        """
        return {
            "kind": self.kind,
            "message": self.message,
            "payload": self.payload,
            "at": self.at.isoformat(),
        }


# ======================================================================================
# The recorder
# ======================================================================================


class ArtifactRecorder:
    """Collects one application attempt's evidence into one directory.

    Constructed by the pipeline before the browser opens and passed down as
    :attr:`app.jobs.base.ApplyContext.recorder`, so the provider, the autofiller and the
    verifier all write into the same place without knowing where it is::

        recorder = ArtifactRecorder(settings, application.id)
        async with BrowserSession(settings, artifacts_dir=recorder.directory) as session:
            recorder.record_event("navigate", "opened the application form", {"url": url})
            recorder.record_screenshot(await session.screenshot("form"), "form as loaded")
            recorder.record_html("form", await session.html())
        manifest = recorder.finalize()

    Every method is synchronous and none of them raises. See the module docstring for why.

    Attributes:
        settings: The runtime configuration whose ``screenshot_path`` roots the directory.
        application_id: The application this evidence belongs to.
    """

    def __init__(
        self,
        settings: Settings,
        application_id: uuid.UUID | str,
        *,
        root: Path | None = None,
    ) -> None:
        """Create (or adopt) the artifacts directory for *application_id*.

        The directory is created eagerly: a recorder that cannot write is a problem worth
        discovering before the browser launches, not after a submission has been made.

        Args:
            settings: Runtime configuration. ``screenshot_path`` is read.
            application_id: The :class:`~app.models.application.Application` being recorded.
                Slugified into the directory name, so a caller-supplied string cannot
                traverse out of *root*.
            root: Override for the parent directory, mainly for tests. Defaults to
                ``settings.screenshot_path``.
        """
        self.settings = settings
        self.application_id = str(application_id)
        self._root = (Path(root) if root is not None else Path(settings.screenshot_path)).resolve()
        self._directory = self._make_directory()
        self._records: list[ArtifactRecord] = []
        self._events: list[RecordedEvent] = []
        self._ordinal = 0
        self._started_at = _now()
        self._finalized_at: datetime | None = None

    # ---------------------------------------------------------------------------------
    # Directory management
    # ---------------------------------------------------------------------------------

    def _make_directory(self) -> Path:
        """Create and return the per-application directory.

        Returns:
            The absolute directory. Falls back to the root itself if the subdirectory cannot
            be created, so recording degrades rather than failing the attempt.
        """
        slug = slugify_artifact_name(self.application_id, fallback=_FALLBACK_STEM)
        directory = (self._root / slug).resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "recorder.directory_unavailable",
                directory=str(directory),
                error=str(exc),
                application_id=self.application_id,
            )
            self._root.mkdir(parents=True, exist_ok=True)
            return self._root
        return directory

    @property
    def directory(self) -> Path:
        """The directory every artifact for this application is written into."""
        return self._directory

    @property
    def records(self) -> tuple[ArtifactRecord, ...]:
        """Every file recorded so far, in the order it was recorded."""
        return tuple(self._records)

    @property
    def events(self) -> tuple[RecordedEvent, ...]:
        """Every timeline entry recorded so far, in order."""
        return tuple(self._events)

    def _safe_path(self, name: str, kind: str) -> Path:
        """Compose the path *name* should be written to, guaranteed inside the directory.

        Two independent defences, because this is the only place a caller-supplied string
        becomes a filesystem path. First :func:`slugify_artifact_name` reduces the name to
        ``[a-z0-9-]``, which cannot express a separator, a drive letter or ``..``. Then the
        composed path is resolved and checked to still be under the directory — so even a
        future change to the slug rules cannot silently open a traversal.

        Args:
            name: The caller's label.
            kind: One of the ``ARTIFACT_KIND_*`` constants, deciding the extension.

        Returns:
            An absolute path inside :attr:`directory`.
        """
        self._ordinal += 1
        stem = slugify_artifact_name(name, fallback=_FALLBACK_STEM)
        suffix = _KIND_SUFFIXES.get(kind, "")
        filename = f"{self._ordinal:0{_ORDINAL_WIDTH}d}-{stem}{suffix}"
        candidate = (self._directory / filename).resolve()
        if not candidate.is_relative_to(self._directory):  # pragma: no cover - defence in depth
            logger.error("recorder.path_escape_blocked", name=name, candidate=str(candidate))
            fallback = f"{self._ordinal:0{_ORDINAL_WIDTH}d}-{_FALLBACK_STEM}{suffix}"
            candidate = self._directory / fallback
        return candidate

    def _add(
        self,
        *,
        path: Path,
        kind: str,
        label: str,
        error: str | None = None,
    ) -> ArtifactRecord:
        """Append a record for *path* and return it.

        Args:
            path: The file that was written (or that a write was attempted on).
            kind: The artifact kind.
            label: The caller's label, kept verbatim.
            error: Why the write failed, if it did.

        Returns:
            The stored :class:`ArtifactRecord`.
        """
        size = 0
        if error is None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                error = str(exc)
        record = ArtifactRecord(
            name=path.name,
            path=path,
            kind=kind,
            label=label,
            recorded_at=_now(),
            size_bytes=size,
            error=error,
        )
        self._records.append(record)
        return record

    # ---------------------------------------------------------------------------------
    # Recording
    # ---------------------------------------------------------------------------------

    def record_screenshot(self, path: Path, label: str) -> ArtifactRecord:
        """Record a screenshot, copying it into the directory if it is not already there.

        :meth:`app.browser.playwright_runner.BrowserSession.screenshot` normally writes
        straight into this directory (the pipeline passes :attr:`directory` as the session's
        ``artifacts_dir``), in which case this only registers the file. A capture taken
        somewhere else — a temporary directory, a previous attempt — is copied in, so the
        manifest never points outside the application's own folder.

        Args:
            path: The screenshot on disk.
            label: What it shows, e.g. ``"before submit"``.

        Returns:
            The record. Its ``error`` is set, rather than an exception raised, when the file
            is missing or could not be copied — evidence about a failure must not become a
            second failure.
        """
        source = Path(path)
        if not source.is_file():
            logger.warning("recorder.screenshot_missing", path=str(source), label=label)
            return self._add(
                path=source,
                kind=ARTIFACT_KIND_SCREENSHOT,
                label=label,
                error="file does not exist",
            )

        try:
            resolved = source.resolve()
        except OSError as exc:  # pragma: no cover - unresolvable paths are rare
            return self._add(
                path=source, kind=ARTIFACT_KIND_SCREENSHOT, label=label, error=str(exc)
            )

        if resolved.parent == self._directory:
            return self._add(path=resolved, kind=ARTIFACT_KIND_SCREENSHOT, label=label)

        target = self._safe_path(label or resolved.stem, ARTIFACT_KIND_SCREENSHOT)
        try:
            target.write_bytes(resolved.read_bytes())
        except OSError as exc:
            logger.warning(
                "recorder.screenshot_copy_failed",
                source=str(resolved),
                target=str(target),
                error=str(exc),
            )
            return self._add(
                path=target, kind=ARTIFACT_KIND_SCREENSHOT, label=label, error=str(exc)
            )
        return self._add(path=target, kind=ARTIFACT_KIND_SCREENSHOT, label=label)

    def record_html(self, name: str, html: str) -> ArtifactRecord:
        """Write a page's markup into the directory.

        Kept because a review item that says "the required field 'Work authorization' had no
        confident answer" is only checkable against the page it was read from.

        Args:
            name: Label for the capture, e.g. ``"application-form"``.
            html: The serialised document. ``None``-ish values are written as an empty file
                rather than crashing, since this is usually the return of
                :meth:`~app.browser.playwright_runner.BrowserSession.html`, which yields
                ``""`` for an unreadable page.

        Returns:
            The record, with ``error`` set when the write failed.
        """
        target = self._safe_path(name, ARTIFACT_KIND_HTML)
        try:
            target.write_text(html or "", encoding=_TEXT_ENCODING, errors="replace")
        except OSError as exc:
            logger.warning("recorder.html_write_failed", path=str(target), error=str(exc))
            return self._add(path=target, kind=ARTIFACT_KIND_HTML, label=name, error=str(exc))
        return self._add(path=target, kind=ARTIFACT_KIND_HTML, label=name)

    def record_json(self, name: str, obj: Any) -> ArtifactRecord:
        """Write a structured payload into the directory.

        Used for the discovered fields, the planned answers, the blockers found and the final
        :class:`~app.jobs.base.ApplyResult` — the machine-readable half of the evidence.

        Args:
            name: Label for the payload, e.g. ``"discovered-fields"``.
            obj: Anything serialisable. Objects :mod:`json` rejects are stringified rather
                than raising (see :func:`_json_default`).

        Returns:
            The record, with ``error`` set when the payload could not be serialised or
            written.
        """
        target = self._safe_path(name, ARTIFACT_KIND_JSON)
        try:
            payload = json.dumps(obj, indent=_JSON_INDENT, default=_json_default, sort_keys=False)
        except (TypeError, ValueError) as exc:
            logger.warning("recorder.json_serialise_failed", name=name, error=str(exc))
            return self._add(path=target, kind=ARTIFACT_KIND_JSON, label=name, error=str(exc))

        try:
            target.write_text(payload, encoding=_TEXT_ENCODING)
        except OSError as exc:
            logger.warning("recorder.json_write_failed", path=str(target), error=str(exc))
            return self._add(path=target, kind=ARTIFACT_KIND_JSON, label=name, error=str(exc))
        return self._add(path=target, kind=ARTIFACT_KIND_JSON, label=name)

    def record_event(
        self,
        kind: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> RecordedEvent:
        """Append a timeline entry.

        Events are held in memory and flushed to :data:`EVENTS_FILENAME` by :meth:`finalize`,
        so recording one costs nothing during an apply attempt. They are also what
        :attr:`app.jobs.base.ApplyResult.browser_log` is built from.

        Args:
            kind: Short category — ``"navigate"``, ``"fill"``, ``"blocker"``, ``"submit"``.
            message: One readable sentence, truncated past
                :data:`_MAX_EVENT_MESSAGE_CHARS`.
            payload: Structured detail. A non-mapping is wrapped rather than dropped.

        Returns:
            The stored event.
        """
        text = str(message or "")
        if len(text) > _MAX_EVENT_MESSAGE_CHARS:
            text = f"{text[:_MAX_EVENT_MESSAGE_CHARS]}…"
        detail: dict[str, Any]
        if payload is None:
            detail = {}
        elif isinstance(payload, dict):
            detail = dict(payload)
        else:
            detail = {"value": payload}

        event = RecordedEvent(kind=str(kind or ARTIFACT_KIND_EVENT), message=text, payload=detail)
        self._events.append(event)
        logger.debug(
            "recorder.event", application_id=self.application_id, kind=event.kind, message=text
        )
        return event

    # ---------------------------------------------------------------------------------
    # Finalisation
    # ---------------------------------------------------------------------------------

    def finalize(self) -> dict[str, Any]:
        """Flush the event log, write ``manifest.json``, and return the manifest.

        Idempotent: calling it twice rewrites the same two files and returns an equivalent
        manifest, because the retry path in
        :meth:`app.services.pipeline.Pipeline.submit` may finalise a recorder that a previous
        attempt already finalised. Neither the manifest nor the event log is listed as a
        recorded file, so re-finalising cannot make the manifest grow.

        Returns:
            The manifest: ``version``, ``application_id``, ``directory``, ``started_at``,
            ``finalized_at``, per-kind ``counts``, ``total_bytes``, the ``files`` list (name,
            path, kind, label, timestamp, size) and the ``events`` timeline. JSON-safe
            throughout — the pipeline stores it on the application row.
        """
        self._finalized_at = _now()
        self._write_events()

        files = [record.as_dict() for record in self._records]
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        counts[ARTIFACT_KIND_EVENT] = len(self._events)

        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "application_id": self.application_id,
            "directory": str(self._directory),
            "started_at": self._started_at.isoformat(),
            "finalized_at": self._finalized_at.isoformat(),
            "counts": counts,
            "total_bytes": sum(record.size_bytes for record in self._records),
            "errors": [
                {"name": record.name, "error": record.error}
                for record in self._records
                if record.error
            ],
            "files": files,
            "events": [event.as_dict() for event in self._events],
        }

        manifest_path = self._directory / MANIFEST_FILENAME
        try:
            manifest_path.write_text(
                json.dumps(manifest, indent=_JSON_INDENT, default=_json_default),
                encoding=_TEXT_ENCODING,
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "recorder.manifest_write_failed", path=str(manifest_path), error=str(exc)
            )
        else:
            manifest["manifest_path"] = str(manifest_path)

        logger.info(
            "recorder.finalized",
            application_id=self.application_id,
            directory=str(self._directory),
            files=len(files),
            events=len(self._events),
            total_bytes=manifest["total_bytes"],
        )
        return manifest

    def _write_events(self) -> None:
        """Write the timeline to :data:`EVENTS_FILENAME` as newline-delimited JSON.

        Newline-delimited rather than one array so the file stays readable with ``tail`` and
        survives a truncated write with every complete line still parseable.
        """
        if not self._events:
            return
        target = self._directory / EVENTS_FILENAME
        try:
            lines = [json.dumps(event.as_dict(), default=_json_default) for event in self._events]
            target.write_text("\n".join(lines) + "\n", encoding=_TEXT_ENCODING)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("recorder.events_write_failed", path=str(target), error=str(exc))

    def __repr__(self) -> str:
        """Return ``<ArtifactRecorder application=… files=… events=…>`` for logs."""
        return (
            f"<ArtifactRecorder application={self.application_id} "
            f"files={len(self._records)} events={len(self._events)}>"
        )
