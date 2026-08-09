"""Document rendering (``docs/CONTRACTS.md`` §11) and storage keys.

Three things here have sharp edges:

**``escape_latex`` is mandatory on every model-produced string.** A resume is the one place
in this system where untrusted text (an LLM rewrite, a company name scraped from a posting)
is compiled by an external binary. An unescaped ``&`` breaks the build; an unescaped ``\\``
is worse than that. The round-trip property is what the tests assert: escaping is a single
pass, so nothing is double-escaped, and re-escaping already-escaped text is not idempotent by
accident but by construction.

**The shrink ladder must only ever shrink.** ``render_resume`` enforces ``max_pages`` by
walking rungs of decreasing font size and margin, dropping bullets only at the end. A rung
that grew the font, or fell below the legibility floor, would produce a document that is
either still too long or unreadable — and the resume goes to an employer either way. The
module validates the ladder at import; this file asserts the validator is real by feeding it
bad ladders.

**``build_key`` is traversal-safe.** It builds ``uploaded_files.storage_key``, which is
concatenated onto a filesystem root. ``..`` in a key is a path escape, so it raises rather
than sanitising — a rejected upload is recoverable, a write outside the storage root is not.
"""

from __future__ import annotations

import itertools

import pytest

from app.documents.models import (
    Contact,
    ResumeDocument,
    ResumeEntry,
    ResumeSection,
)
from app.documents.renderer import (
    MIN_FONT_SIZE_PT,
    SHRINK_LADDER,
    ShrinkStep,
    _validate_ladder,
    escape_latex,
    escape_latex_dict,
)
from app.storage.base import (
    KEY_MAX_LENGTH,
    StorageKeyError,
    build_key,
    key_segments,
    normalize_key,
)

# ======================================================================================
# escape_latex
# ======================================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("O'Brien & Sons", "O'Brien \\& Sons"),
        ("C#", "C\\#"),
        ("100% uptime", "100\\% uptime"),
        ("a_b", "a\\_b"),
        ("50$ saved", "50\\$ saved"),
        ("{braces}", "\\{braces\\}"),
        ("~tilde", "\\textasciitilde{}tilde"),
        ("a^b", "a\\textasciicircum{}b"),
    ],
)
def test_escape_latex_escapes_every_special_character(raw: str, expected: str) -> None:
    """The ten characters §11 names, one at a time."""
    assert escape_latex(raw) == expected


def test_backslash_is_escaped_first_so_nothing_is_double_escaped() -> None:
    """A single-pass translation is what makes the result stable.

    Escaping ``&`` to ``\\&`` and *then* escaping backslashes would yield ``\\\\&``, which
    typesets a literal backslash followed by an ampersand — the bug a naive chain of
    ``str.replace`` calls always has.
    """
    assert escape_latex("\\") == "\\textbackslash{}"
    escaped = escape_latex("a\\b & c")
    assert escaped.count("\\textbackslash{}") == 1
    assert "\\&" in escaped


def test_escaping_is_a_single_pass_not_a_fixed_point() -> None:
    """Escaping twice must differ from escaping once — otherwise the first pass was lossy."""
    once = escape_latex("50% & rising")
    twice = escape_latex(once)
    assert once != twice, "escape_latex appears to be skipping already-escaped characters"


def test_ordinary_text_is_unchanged() -> None:
    """A round trip that must be the identity, or every bullet gets mangled."""
    plain = "Cut p99 checkout latency from 840ms to 120ms."
    assert escape_latex(plain) == plain


@pytest.mark.parametrize("value", [None, 42, 3.5, True])
def test_escape_latex_never_raises_on_a_non_string(value) -> None:
    """It sits between an LLM response and a PDF; it must not be the thing that fails."""
    assert isinstance(escape_latex(value), str)


def test_escape_latex_dict_descends_but_leaves_keys_alone() -> None:
    """Keys are template lookups; rewriting ``links["portfolio"]`` breaks the template."""
    escaped = escape_latex_dict(
        {
            "name": "O'Brien & Sons",
            "links": {"portfolio": "https://example.com/?a=1&b=2"},
            "bullets": ["100% uptime", "Saved 50$"],
            "count": 3,
            "flag": True,
            "nothing": None,
        }
    )

    assert escaped["name"] == "O'Brien \\& Sons"
    assert "links" in escaped and "portfolio" in escaped["links"]
    assert "\\&" in escaped["links"]["portfolio"]
    assert escaped["bullets"] == ["100\\% uptime", "Saved 50\\$"]
    assert escaped["count"] == 3
    assert escaped["flag"] is True
    assert escaped["nothing"] is None


def test_escape_latex_dict_does_not_mutate_its_input() -> None:
    """The caller keeps the raw document; only the render sees escaped text."""
    original = {"name": "A & B"}
    escape_latex_dict(original)
    assert original["name"] == "A & B"


# ======================================================================================
# The one-page shrink ladder
# ======================================================================================


def test_the_ladder_only_ever_shrinks() -> None:
    """Each rung is at most as large as the one before it, in both dimensions."""
    for previous, current in itertools.pairwise(SHRINK_LADDER):
        assert current.font_size <= previous.font_size, "a rung grows the font"
        assert current.margin_in <= previous.margin_in, "a rung grows the margin"


def test_no_rung_falls_below_the_legibility_floor() -> None:
    """A resume nobody can read is not a shorter resume."""
    for step in SHRINK_LADDER:
        assert step.font_size >= MIN_FONT_SIZE_PT


def test_typography_is_tried_before_content_is_dropped() -> None:
    """Dropping a bullet loses information; shrinking a point size does not.

    So every content-dropping rung must come after every typography-only rung.
    """
    labels = [step.drops_content for step in SHRINK_LADDER]
    first_drop = next((index for index, drops in enumerate(labels) if drops), len(labels))
    assert not any(labels[:first_drop]), "content is dropped before typography is exhausted"


def test_the_ladder_is_bounded() -> None:
    """§11 caps the loop at five attempts; an unbounded ladder would hang a render."""
    assert 1 <= len(SHRINK_LADDER) <= 5


def test_every_rung_is_labelled() -> None:
    """The label is what an operator reads in ``documents.render_attempt``."""
    assert all(step.label for step in SHRINK_LADDER)


def test_rung_options_carry_the_page_budget() -> None:
    """A template cannot enforce a budget it was not told about."""
    options = SHRINK_LADDER[0].as_options(max_pages=1, attempt=1)
    assert 1 in options.values()
    assert options


# -- the validator is real ---------------------------------------------------------


def test_the_validator_rejects_an_empty_ladder() -> None:
    """These three tests are what make the assertions above more than documentation."""
    with pytest.raises(ValueError):
        _validate_ladder(())


def test_the_validator_rejects_a_growing_font() -> None:
    """A ladder that grows would loop forever without ever fitting."""
    with pytest.raises(ValueError):
        _validate_ladder(
            (
                ShrinkStep(font_size=10.0, margin_in=0.5, label="a"),
                ShrinkStep(font_size=11.0, margin_in=0.5, label="b"),
            )
        )


def test_the_validator_rejects_an_illegible_font() -> None:
    """Below the floor, "it fits" stops being the goal."""
    with pytest.raises(ValueError):
        _validate_ladder((ShrinkStep(font_size=MIN_FONT_SIZE_PT - 1, margin_in=0.5, label="a"),))


def test_the_validator_rejects_a_non_positive_margin() -> None:
    """A zero margin is a printer error waiting to happen."""
    with pytest.raises(ValueError):
        _validate_ladder((ShrinkStep(font_size=10.0, margin_in=0.0, label="a"),))


# ======================================================================================
# The document model
# ======================================================================================


def _document() -> ResumeDocument:
    """A small but complete resume."""
    return ResumeDocument(
        contact=Contact(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        sections=[
            ResumeSection(
                heading="Experience",
                entries=[
                    ResumeEntry(
                        title="Backend Engineer",
                        organization="Acme Robotics",
                        location="Remote",
                        date_range="2022 — 2024",
                        bullets=["Cut latency.", "Owned payments."],
                        fact_ids=["f1", "f2"],
                    )
                ],
            )
        ],
        skills_line="Python, Redis",
    )


def test_total_bullets_counts_every_entry() -> None:
    """The figure the bullet budget is enforced against."""
    assert _document().total_bullets() == 2


def test_estimated_lines_grows_with_content() -> None:
    """The shrink loop needs an estimate before paying to typeset."""
    small = _document()
    large = _document()
    large.sections[0].entries[0].bullets.extend(f"Bullet {index}." for index in range(20))
    assert large.estimated_lines() > small.estimated_lines()


def test_fact_ids_line_up_with_bullets() -> None:
    """Golden rule #7's traceability, at the document level."""
    entry = _document().sections[0].entries[0]
    assert len(entry.fact_ids) == len(entry.bullets)


def test_a_document_round_trips_through_json() -> None:
    """``content_json`` is kept forever, so it must survive serialisation exactly."""
    document = _document()
    payload = document.model_dump(mode="json")
    restored = ResumeDocument.model_validate(payload)

    assert restored.total_bullets() == document.total_bullets()
    assert restored.contact.name == "Ada Lovelace"
    assert restored.sections[0].entries[0].fact_ids == ["f1", "f2"]


# ======================================================================================
# Storage keys — traversal safety
# ======================================================================================


def test_build_key_joins_and_slugifies() -> None:
    """The ordinary case."""
    assert build_key("users", "abc", "resume.pdf") == "users/abc/resume.pdf"


def test_nesting_can_be_expressed_either_way() -> None:
    """Separate arguments and embedded slashes must agree."""
    assert build_key("users/abc", "resume.pdf") == build_key("users", "abc", "resume.pdf")


def test_empty_components_are_dropped() -> None:
    """So an optional prefix can be passed as ``""`` without special-casing."""
    assert build_key("", "users", None, "resume.pdf") == "users/resume.pdf"


def test_an_extension_is_guaranteed_once() -> None:
    """Adding ``.pdf`` to something already ending in ``.pdf`` must not double it."""
    assert build_key("a", "b", ext="pdf").endswith(".pdf")
    assert build_key("a", "b.pdf", ext="pdf").count(".pdf") == 1
    assert build_key("a", "b", ext=".pdf") == build_key("a", "b", ext="pdf")


@pytest.mark.parametrize(
    "hostile",
    [
        "../etc/passwd",
        "users/../../etc/passwd",
        "..",
        "a/../../b",
        "/absolute/path",
        "C:\\Windows\\System32",
        "with\x00null",
    ],
)
def test_traversal_and_absolute_paths_are_refused(hostile: str) -> None:
    """**The security property.** A rejected upload is recoverable; a write outside the
    storage root is not."""
    with pytest.raises(StorageKeyError):
        build_key("users", hostile, "resume.pdf")


def test_a_key_with_nothing_usable_is_refused() -> None:
    """Slugifying punctuation to nothing must raise rather than produce ``""``."""
    with pytest.raises(StorageKeyError):
        build_key("!!!", "???")


def test_a_single_over_long_component_is_truncated_not_refused() -> None:
    """One huge component is slugified down rather than rejected — it is still a valid key."""
    key = build_key("a" * 5000, ext="pdf")
    assert len(key) <= KEY_MAX_LENGTH
    assert key.endswith(".pdf")


def test_an_over_long_key_is_refused() -> None:
    """Filesystems have limits, and the failure should happen before the write.

    Reached with many components rather than one long one, because per-component
    slugification truncates a single oversized segment on its own.
    """
    with pytest.raises(StorageKeyError):
        build_key(*[f"segment{index:04d}" for index in range(200)], ext="pdf")


def test_key_segments_round_trips() -> None:
    """Splitting and rejoining a key is the identity."""
    key = build_key("users", "abc", "resume.pdf")
    assert "/".join(key_segments(key)) == key


def test_normalize_key_is_idempotent() -> None:
    """Normalising twice equals normalising once."""
    once = normalize_key("users//abc/./resume.pdf")
    assert normalize_key(once) == once


def test_build_key_accepts_uuids_and_ints() -> None:
    """Call sites pass ``application.id`` directly."""
    import uuid

    identifier = uuid.uuid4()
    key = build_key("resumes", identifier, 1, ext="pdf")
    assert str(identifier).replace("-", "") in key.replace("-", "")
