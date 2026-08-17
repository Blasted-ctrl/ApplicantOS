"""``docs/CONTRACTS.md`` says what the enums are. This checks that it is telling the truth.

CLAUDE.md calls the contracts document **binding**, and ``docs/CONTRACTS.md`` §3 freezes every
enum value: renaming one is a breaking change to the database, the HTTP contract and the
desktop client at once. A binding document nothing verifies is a document that drifts, and
this one had: ``ApplicationStatus`` gained ``accepted`` and ``SignalKind`` gained
``offer_accepted`` in the code while §3 and §17.1 still listed the old sets.

The TypeScript mirror did not drift, and the reason is not diligence — it is
``test_models.py::test_enum_values_match_the_typescript_union``. This file is the same guard
pointed at the markdown, so the spec is enforceable rather than aspirational.

Only the enums the document lists as bare value sets are checked. §17.1's
``PluginKind: + TRACKER = "tracker"`` is prose about a *delta*, not a value set; it is skipped
on its **content** rather than by name, so §3's full ``PluginKind`` listing is still verified.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from app.models import enums as enums_module
from app.models.enums import StrEnum

#: The binding document.
CONTRACTS: Final[Path] = Path(__file__).resolve().parent.parent / "docs" / "CONTRACTS.md"

#: ``Name: value value value``, with wrapped continuation lines indented beneath.
_ENTRY: Final[re.Pattern[str]] = re.compile(r"^(?P<name>[A-Z][A-Za-z]+):\s*(?P<values>.*)$")

#: Tokens that mark a line as prose about a *change* rather than a value set — §17.1's
#: ``PluginKind: + TRACKER = "tracker"``. Detected by content so that the same enum's full
#: listing elsewhere in the document is still checked.
_DELTA_TOKENS: Final[frozenset[str]] = frozenset({"+", "="})


def _spec_enums() -> dict[str, list[str]]:
    """Parse every enum value set the contracts document declares.

    Returns:
        Enum name to the values listed for it, in document order.
    """
    parsed: dict[str, list[str]] = {}
    current: str | None = None

    for raw in CONTRACTS.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip():
            current = None
            continue

        match = _ENTRY.match(line.strip())
        if match:
            name = match.group("name")
            if not hasattr(enums_module, name):
                current = None
                continue
            values = match.group("values").split()
            if any(token in _DELTA_TOKENS for token in values):
                current = None
                continue
            parsed[name] = values
            current = name
            continue

        # A wrapped continuation: indented, and part of the entry above it.
        if current is not None and raw.startswith(" ") and ":" not in line:
            parsed[current].extend(line.split())
            continue

        current = None

    return parsed


def _declared() -> dict[str, type[StrEnum]]:
    """Return every ``StrEnum`` declared in :mod:`app.models.enums`."""
    import inspect

    return {
        name: member
        for name, member in vars(enums_module).items()
        if inspect.isclass(member)
        and issubclass(member, StrEnum)
        and member is not StrEnum
        and member.__module__ == enums_module.__name__
    }


def test_the_document_declares_enums_at_all() -> None:
    """Guards the parser: a regex that matched nothing would make every test below vacuous."""
    parsed = _spec_enums()

    assert len(parsed) >= 10, f"parsed only {sorted(parsed)}"
    assert "ApplicationStatus" in parsed
    assert "SignalKind" in parsed


@pytest.mark.parametrize("name", sorted(_spec_enums()))
def test_every_documented_enum_matches_the_code(name: str) -> None:
    """A value in the spec that is not in the code, or the reverse, is a broken contract.

    Compared as **sets**: the document wraps its lists across lines for readability, so
    ordering there is a typesetting decision rather than a promise.
    """
    documented = set(_spec_enums()[name])
    actual = set(_declared()[name].values())

    assert documented == actual, (
        f"docs/CONTRACTS.md and {name} disagree.\n"
        f"  in the spec, not in the code: {sorted(documented - actual) or 'none'}\n"
        f"  in the code, not in the spec: {sorted(actual - documented) or 'none'}"
    )


def test_the_four_user_facing_statuses_are_documented() -> None:
    """§3's rollup is part of the frozen surface, and the desktop mirrors it."""
    parsed = _spec_enums()

    assert "UserFacingStatus" in parsed, "the spec does not name the product's four statuses"
    assert set(parsed["UserFacingStatus"]) == set(enums_module.UserFacingStatus.values())
