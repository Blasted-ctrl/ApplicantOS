"""Runtime configuration, the plugin inventory, and the scoring rule pack (§14).

``GET /settings`` is the single most dangerous read in this API: the settings object holds
five API keys, a signing secret and two connection URLs with embedded credentials. It is
answered by :meth:`~app.schemas.settings.SettingsRead.from_settings` and by nothing else.
That method is an **allow-list** — every field is named explicitly, so a new secret added to
:class:`~app.config.settings.Settings` later cannot leak through it by default. Where a
client legitimately needs to know whether a credential is configured, the answer is a
boolean (``anthropic_configured``), never a masked string: a mask still leaks length, prefix
and enough tail to confirm a guess, and every masking scheme eventually grows an "unmask"
endpoint. **A leaked key is a security bug, not a cosmetic one.**

``PUT /settings`` is the mirror image. Credentials *are* writable — they have to be settable
somehow — and :class:`~app.schemas.settings.SettingsUpdate` marks them ``repr=False`` so they
cannot surface in a traceback or a debug dump. :mod:`app.api.errors` drops the ``input`` key
from every validation error for the same reason. A field accepted for write is never echoed
back on read.

**The write is process-scoped and says so.** :class:`~app.config.settings.Settings` is read
from the environment and the ``.env`` file; there is no settings table and this module does
not invent one. Writing credentials into a JSON file beside the database would be a worse
security posture than leaving them where the user already put them, so the change takes
effect immediately in this process and the response says plainly that ``.env`` is what makes
it survive a restart.

``GET|PUT /settings/scoring-rules`` edits ``app/config/scoring_rules.yaml`` — the pack
:func:`app.ai.scoring.load_rules` reads and every :class:`~app.ai.scoring.Scorer` is built
from. Before the first write the packaged pack is snapshotted into the data directory, which
is what makes ``reset_to_defaults`` able to restore something.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, SettingsDep
from app.config.logging import configure_logging
from app.config.settings import DEFAULT_SCORING_RULES_PATH, Settings
from app.models.enums import PluginKind
from app.schemas.common import Page
from app.schemas.scoring import ScoreRuleSchema, ScoringRulesUpdate
from app.schemas.settings import PluginInfo, SettingsRead, SettingsUpdate

__all__ = ["PREFIX", "TAGS", "router"]

logger = structlog.get_logger(__name__)

#: Path prefix for this group.
PREFIX: Final[str] = "/settings"

#: OpenAPI tag for this group.
TAGS: Final[list[str]] = ["settings"]

#: Filename of the snapshot taken of the packaged rule pack before it is first overwritten.
#: Kept in the data directory rather than beside the package, because an installed package
#: directory is frequently read-only and a backup that cannot be written is not a backup.
PACKAGED_RULES_SNAPSHOT: Final[str] = "scoring_rules.packaged.yaml"

#: Settings whose change has to be applied to the running process, not merely stored.
_LOGGING_FIELDS: Final[frozenset[str]] = frozenset({"log_level", "log_json"})

#: Note appended to the ``PUT /settings`` response. Stated rather than implied: a user who
#: pastes an API key deserves to know what makes it survive a restart.
_PERSISTENCE_NOTE: Final[str] = (
    "Applied to the running process. Add the same values to your .env file to keep them "
    "across a restart."
)

#: The document key a scoring pack lives under. Mirrors ``app.ai.scoring.RULES_DOCUMENT_KEY``.
_RULES_DOCUMENT_KEY: Final[str] = "rules"

#: Fields of :class:`app.ai.scoring.ScoreRule` carried in ``ScoreRuleSchema.criteria``. The
#: two models are not the same shape: the engine's rule is a matcher (which haystack, which
#: terms), while the schema is the editable form the settings screen binds to.
_CRITERIA_FIELDS: Final[tuple[str, ...]] = ("any_of", "all_of", "none_of", "regex")

router = APIRouter()


# ======================================================================================
# Runtime configuration
# ======================================================================================


@router.get(
    "",
    response_model=SettingsRead,
    summary="Runtime configuration, with no credential of any kind",
)
async def read_settings(settings: SettingsDep) -> SettingsRead:
    """Return the safe projection of runtime configuration.

    Contains no API key, no signing secret and no connection URL. Credentials are reported
    as ``*_configured`` booleans and the database as ``database_backend``, because a boolean
    cannot leak anything and a backend name cannot carry a password.

    ``is_submission_allowed`` is included as the *effective* permission — the AND of the kill
    switch and ``NOT dry_run`` — so the client renders one truth rather than re-deriving it
    and getting the polarity wrong.

    Args:
        settings: Runtime configuration.

    Returns:
        The safe projection.
    """
    return SettingsRead.from_settings(settings)


@router.put(
    "",
    response_model=SettingsRead,
    summary="Update runtime configuration",
    description="Credentials are accepted for write and are never returned on read.",
)
async def update_settings(
    payload: SettingsUpdate,
    settings: SettingsDep,
    user: CurrentUser,
) -> SettingsRead:
    """Apply a partial update to the process-wide settings.

    Only the fields a user can meaningfully change at run time are writable.
    :class:`~app.schemas.settings.SettingsUpdate` deliberately omits ``database_url``,
    ``redis_url``, ``secret_key``, ``data_dir``, ``environment`` and the API bind address —
    those either need a restart or would let an HTTP request repoint the process at a
    different database.

    The log line names which fields changed and never their values, so switching on an API
    key does not write it into the log the redaction chain is meant to keep it out of.

    Args:
        payload: The fields to change, applied with ``exclude_unset`` so an omitted field is
            left alone and an explicit ``null`` clears a credential.
        settings: The process-wide settings singleton, mutated in place.
        user: The acting user, recorded on the audit line.

    Returns:
        The safe projection after the write — never the values that were written.
    """
    changes = payload.model_dump(exclude_unset=True)
    for name, value in changes.items():
        setattr(settings, name, value)

    if _LOGGING_FIELDS & set(changes):
        configure_logging(settings)

    _reconfigure_plugins(settings)

    logger.info(
        "api.settings_updated",
        user_id=str(user.id),
        fields=sorted(changes),
        auto_apply_enabled=settings.auto_apply_enabled,
        dry_run=settings.dry_run,
    )
    return SettingsRead.from_settings(settings)


def _reconfigure_plugins(settings: Settings) -> None:
    """Hand the updated settings to the plugin registry and drop cached instances.

    A model client built before an API key was pasted in holds the old (absent) credential
    for the life of the process, so a settings change that does not reach the registry looks
    to the user like the key did not work.

    Args:
        settings: The updated settings.
    """
    from app.plugins.registry import registry

    registry.configure(settings)


# ======================================================================================
# Plugins
# ======================================================================================


@router.get(
    "/plugins",
    response_model=Page[PluginInfo],
    summary="Every registered plugin",
)
async def list_plugins(
    kind: Annotated[
        PluginKind | None,
        Query(description="Restrict to one plugin kind."),
    ] = None,
    probe: Annotated[
        bool,
        Query(description="Run each plugin's healthcheck. Slower; touches the network."),
    ] = False,
) -> Page[PluginInfo]:
    """Return the plugin inventory: providers, models, templates, parsers and analyzers.

    Read from the registry's *metadata* rather than from instances, so nothing is
    constructed — a provider whose constructor would fail for want of an API key is still
    describable, which is exactly the state the settings screen exists to show.

    ``healthy`` is ``None`` unless ``probe`` is set. Probing is opt-in because a healthcheck
    reaches out over the network, and a settings screen that hangs for ten seconds on every
    open is worse than one that shows a "check" button.

    Args:
        kind: Restrict to one :class:`~app.models.enums.PluginKind`.
        probe: Run each enabled plugin's healthcheck.

    Returns:
        The inventory, ordered by kind then name.
    """
    from app.plugins.registry import registry

    metas = [meta for meta in registry.describe() if kind is None or meta.kind is kind]
    health: dict[str, bool] = await registry.healthcheck(kind) if probe else {}

    items = [
        PluginInfo(
            kind=meta.kind,
            name=meta.name,
            version=meta.version,
            display_name=meta.display_name or meta.name,
            description=meta.description,
            author=meta.author,
            capabilities=sorted(meta.capabilities),
            enabled=registry.is_enabled(meta.kind, meta.name),
            enabled_by_default=meta.enabled_by_default,
            healthy=health.get(f"{meta.kind.value}/{meta.name}"),
        )
        for meta in metas
    ]
    return Page.of(items, total=len(items), limit=len(items) or 1, offset=0)


# ======================================================================================
# Scoring rules
# ======================================================================================


def _snapshot_path(settings: Settings) -> Path:
    """Return where the packaged rule pack is snapshotted before the first user edit.

    Args:
        settings: Runtime configuration, supplying the data directory.

    Returns:
        The snapshot's path.
    """
    return settings.data_path / PACKAGED_RULES_SNAPSHOT


def _to_schema(rule: Any) -> ScoreRuleSchema:
    """Project one :class:`app.ai.scoring.ScoreRule` into its editable form.

    The engine's rule is a matcher — a haystack (``field``) and three term lists — while
    :class:`~app.schemas.scoring.ScoreRuleSchema` is the general editable shape the settings
    screen binds to. ``field`` becomes ``category``, because that is genuinely what groups
    rules in the score panel, and the term lists become ``criteria``, which is documented as
    "rule-specific matching configuration interpreted by the rule's own evaluator".

    ``weight`` is 1.0 and ``hard_negative`` is false because the packed rule format has
    neither: a penalty is expressed as negative ``points``, and a policy veto is enforced by
    :class:`~app.ai.scoring.Scorer` from the user's preferences rather than by a flag in the
    pack. Reporting a flag the engine does not read would be a lie the UI could act on.

    Args:
        rule: The engine's rule.

    Returns:
        The editable form.
    """
    return ScoreRuleSchema(
        key=rule.key,
        label=rule.label,
        points=float(rule.points),
        weight=1.0,
        category=rule.field,
        enabled=True,
        hard_negative=False,
        criteria={name: getattr(rule, name) for name in _CRITERIA_FIELDS},
    )


def _to_pack_entry(rule: ScoreRuleSchema) -> dict[str, Any]:
    """Project one editable rule back into the pack's YAML shape.

    ``points`` is rounded to an integer because :class:`~app.ai.scoring.ScoreRule` requires
    one — the pack is summed as integers so a score is reproducible without float drift, and
    accepting 2.5 here would silently become 2 at load time.

    Args:
        rule: The submitted rule.

    Returns:
        A mapping with only the keys :func:`app.ai.scoring.load_rules` accepts.
    """
    criteria = rule.criteria or {}
    entry: dict[str, Any] = {
        "key": rule.key,
        "label": rule.label or rule.key,
        "points": round(rule.points * (rule.weight if rule.weight else 1.0)),
        "field": str(criteria.get("field") or rule.category or "description"),
    }
    for name in _CRITERIA_FIELDS:
        value = criteria.get(name)
        if name == "regex":
            entry[name] = str(value) if value else None
        else:
            entry[name] = [str(item) for item in (value or [])]
    return entry


@router.get(
    "/scoring-rules",
    response_model=Page[ScoreRuleSchema],
    summary="The scoring rule pack",
)
async def read_scoring_rules() -> Page[ScoreRuleSchema]:
    """Return the rule pack the scorer is currently built from, in pack order.

    Order matters and is preserved: it is the order the score breakdown renders in, so a
    reordered pack is a reordered explanation.

    Returns:
        The pack.

    Raises:
        ValueError: If the pack on disk is missing or malformed — mapped to 400, with the
            offending rule named, because this file is user-editable and the answer to a bad
            edit is the line number.
    """
    from app.ai.scoring import load_rules

    rules = [_to_schema(rule) for rule in load_rules()]
    return Page.of(rules, total=len(rules), limit=len(rules) or 1, offset=0)


@router.put(
    "/scoring-rules",
    response_model=Page[ScoreRuleSchema],
    status_code=status.HTTP_200_OK,
    summary="Replace the scoring rule pack",
    description="The whole pack is submitted at once; rules are tuned relative to each other.",
)
async def update_scoring_rules(
    payload: ScoringRulesUpdate,
    settings: SettingsDep,
    user: CurrentUser,
) -> Page[ScoreRuleSchema]:
    """Write a new rule pack, or restore the packaged one.

    The whole pack is replaced rather than patched rule by rule, because rules are tuned
    *relative to each other*: a partial update would let a user raise one weight without
    seeing what it did to the balance of the rest. Duplicate keys are rejected by the schema,
    since a duplicate would make ``ScoreComponentRead.key`` ambiguous and silently shadow one
    of the two rules.

    Every submitted rule is constructed as a real :class:`~app.ai.scoring.ScoreRule` **before
    anything is written**, so a pack that would not load is rejected with the offending key
    named instead of leaving the file in a state that breaks every future score.

    The packaged pack is snapshotted into the data directory on the first write, which is
    what gives ``reset_to_defaults`` something to restore; without it, the first save would
    destroy the defaults permanently.

    Args:
        payload: The complete pack, or ``reset_to_defaults``.
        settings: Runtime configuration, supplying the data directory.
        user: The acting user, recorded on the audit line.

    Returns:
        The pack now in force.

    Raises:
        ValueError: If a rule fails validation — mapped to 400.
        OSError: If the pack cannot be written, which is a genuine server fault.
    """
    from app.ai.scoring import ScoreRule, load_rules, reset_rule_cache

    if payload.reset_to_defaults:
        snapshot = _snapshot_path(settings)
        if snapshot.is_file():
            shutil.copyfile(snapshot, DEFAULT_SCORING_RULES_PATH)
            logger.info("api.scoring_rules_reset", user_id=str(user.id))
        else:
            logger.info("api.scoring_rules_reset_noop", user_id=str(user.id))
        reset_rule_cache()
        rules = [_to_schema(rule) for rule in load_rules()]
        return Page.of(rules, total=len(rules), limit=len(rules) or 1, offset=0)

    entries = [_to_pack_entry(rule) for rule in payload.rules]
    if not entries:
        raise ValueError(
            "a scoring pack must contain at least one rule; use reset_to_defaults to "
            "restore the packaged pack"
        )

    # Construct every rule first. ScoreRule validates in __post_init__ and names the
    # offending key, so a bad pack fails here rather than on the next discovery run.
    validated = [
        ScoreRule(
            key=entry["key"],
            label=entry["label"],
            points=entry["points"],
            field=entry["field"],
            any_of=entry["any_of"],
            all_of=entry["all_of"],
            none_of=entry["none_of"],
            regex=entry["regex"],
        )
        for entry in entries
    ]

    _write_pack(settings, [rule.to_dict() for rule in validated])
    reset_rule_cache()

    logger.info(
        "api.scoring_rules_updated",
        user_id=str(user.id),
        rules=len(validated),
    )
    rules = [_to_schema(rule) for rule in load_rules()]
    return Page.of(rules, total=len(rules), limit=len(rules) or 1, offset=0)


def _write_pack(settings: Settings, entries: list[dict[str, Any]]) -> None:
    """Snapshot the packaged pack if needed, then write the new one.

    Args:
        settings: Runtime configuration, supplying the data directory.
        entries: The validated rules, already in pack shape.

    Raises:
        OSError: If either file cannot be written.
    """
    import yaml  # Lazy: the app must import without optional dependencies present.

    snapshot = _snapshot_path(settings)
    if not snapshot.exists() and DEFAULT_SCORING_RULES_PATH.is_file():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_SCORING_RULES_PATH, snapshot)
        logger.info("api.scoring_rules_snapshotted", path=str(snapshot))

    document = yaml.safe_dump(
        {_RULES_DOCUMENT_KEY: entries},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    DEFAULT_SCORING_RULES_PATH.write_text(document, encoding="utf-8")
