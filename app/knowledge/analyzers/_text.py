"""HTML and URL primitives shared by every analyzer that reads markup.

Three analyzers need the same two things and would otherwise each invent them badly:
:class:`~app.knowledge.analyzers.website.WebsiteAnalyzer` crawling a portfolio,
:class:`~app.knowledge.analyzers.github.GitHubAnalyzer` when a README ships HTML, and the
local project-folder analyzer reading a ``docs/`` tree off disk. This module is that shared
layer, and it is deliberately the *only* place in ApplicantOS where HTML is turned into
text or a URL is canonicalised.

**HTML → text.** :func:`html_to_text` returns the readable prose of a page plus the
metadata a knowledge document wants (title, meta description, heading outline, canonical
url, outbound links). It strips ``script``/``style``/``nav``/``footer``/``aside`` and their
friends, prefers ``<main>``/``<article>`` when the page has one, preserves the heading
hierarchy as markdown-ish ``#`` prefixes so that
:func:`~app.knowledge.extractors.split_bullets` and
:func:`~app.knowledge.extractors.detect_project_name` see the same structure they see in a
README, and collapses whitespace.

**Two parsers, one renderer.** ``selectolax`` is used when it is importable — it is a
Lexbor binding and repairs real-world tag soup far better than the standard library — and a
complete :mod:`html.parser` implementation is used when it is not. Both produce the same
small :class:`_Element` tree, so the rendering rules, the drop list, the scope selection and
the metadata extraction exist exactly once and behave identically either way. ``selectolax``
is therefore a genuine accelerator and never a dependency: nothing in this module imports it
at module scope, and an installation without it loses no capability.

**URLs.** :func:`normalize_url`, :func:`same_origin` and :func:`is_asset_url` are what keep
a crawl from visiting the same page five times under five spellings, from wandering off the
user's site, and from downloading a 40 MB video in place of prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import structlog

__all__ = [
    "ASSET_EXTENSIONS",
    "FETCHABLE_SCHEMES",
    "MAX_HEADINGS",
    "MAX_LINKS",
    "extract_links",
    "html_to_text",
    "is_asset_url",
    "is_http_url",
    "normalize_url",
    "same_origin",
]

logger = structlog.get_logger(__name__)


# ======================================================================================
# Constants
# ======================================================================================

#: Elements whose subtree is never prose. ``script``/``style``/``noscript``/``template``
#: are machine content; ``nav``/``footer``/``aside`` are site chrome repeated on every page,
#: and keeping them would make forty crawled pages look like forty copies of the same menu
#: to the embedder. ``header`` is deliberately *absent*: on a personal site it usually holds
#: the ``<h1>`` that names the page.
DROP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "audio",
        "video",
        "map",
        "form",
        "input",
        "select",
        "option",
        "optgroup",
        "textarea",
        "button",
        "nav",
        "footer",
        "aside",
        "dialog",
        "datalist",
        "picture",
        "source",
        "track",
    }
)

#: Elements that carry an outbound link in their ``href``.
_LINK_TAGS: Final[frozenset[str]] = frozenset({"a", "area"})

#: Elements that are skipped when harvesting outbound links. Much shorter than
#: :data:`DROP_TAGS` on purpose: a site's navigation is *exactly* where the crawler finds
#: the project pages, so ``nav`` and ``footer`` links must survive even though their text
#: does not.
LINK_SKIP_TAGS: Final[frozenset[str]] = frozenset({"script", "style", "noscript", "template"})

#: Heading elements and the outline level each one denotes.
HEADING_LEVELS: Final[dict[str, int]] = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

#: Elements that start and end a block of text. Anything not listed here and not handled
#: specially is inline, so ``<span>a</span><b>b</b>`` renders as ``ab`` exactly as a browser
#: would show it.
BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "caption",
        "details",
        "div",
        "dd",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "header",
        "hgroup",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "ul",
        *HEADING_LEVELS,
    }
)

#: Table cells, rendered with a ``|`` separator so a specification table stays readable.
_CELL_TAGS: Final[frozenset[str]] = frozenset({"td", "th"})

#: Elements inside which newlines are meaningful and must survive whitespace collapsing.
PREFORMATTED_TAGS: Final[frozenset[str]] = frozenset({"pre"})

#: Elements that never have children or an end tag.
VOID_TAGS: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Start tags that implicitly close a set of still-open elements, which is how real pages
#: get away with ``<li>a<li>b`` and ``<td>1<td>2``.
IMPLICIT_CLOSE: Final[dict[str, frozenset[str]]] = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "thead": frozenset({"tr", "td", "th"}),
    "tbody": frozenset({"tr", "td", "th", "thead"}),
    "tfoot": frozenset({"tr", "td", "th", "tbody"}),
}

#: Nesting ceiling for the parsed tree. Bounds both the builder's stack and the renderer's
#: recursion, so a hand-written page with ten thousand unclosed ``<div>`` elements degrades
#: to flat text instead of exhausting the interpreter stack.
MAX_TREE_DEPTH: Final[int] = 200

#: Most headings kept in the returned metadata. An outline longer than this is a generated
#: index, and the full text is retained regardless.
MAX_HEADINGS: Final[int] = 100

#: Most links returned per page. A link farm must not be able to make one crawled page cost
#: unbounded memory in the frontier.
MAX_LINKS: Final[int] = 500

#: Characters of readable text a candidate container must hold before it is accepted as the
#: page's main content. Without this floor a decorative empty ``<main>`` would blank the
#: page; with it, extraction falls through to ``<body>``.
MIN_SCOPE_CHARS: Final[int] = 200

#: The document containers tried in order when deciding what counts as page content.
SCOPE_PRIORITY: Final[tuple[str, ...]] = ("main", "article", "body")

#: Synthetic tag of the parse tree's root node.
ROOT_TAG: Final[str] = "[document]"

#: Tag ``selectolax`` gives a text node.
_SELECTOLAX_TEXT_TAG: Final[str] = "-text"

#: URL schemes a crawler may actually fetch.
FETCHABLE_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Default port per scheme, stripped during normalisation so ``https://x.com:443/a`` and
#: ``https://x.com/a`` are one URL rather than two.
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

#: Host label treated as equivalent to its absence by :func:`same_origin`. A browser would
#: call ``example.com`` and ``www.example.com`` different origins; a crawler that did the
#: same would abandon most personal sites at the first internal link, because the canonical
#: host and the linked host routinely disagree on this one label. Recorded in
#: ``docs/OPEN_QUESTIONS.md``.
WWW_PREFIX: Final[str] = "www."

#: Path extensions that are assets rather than prose. ``.pdf``/``.docx`` are included
#: because this module's consumers extract *HTML*: a portfolio's resume PDF is real content,
#: but it belongs to ``ResumeParser`` as its own source, not to a crawler that would hand
#: binary to an HTML parser.
ASSET_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        # images
        "apng",
        "avif",
        "bmp",
        "gif",
        "heic",
        "ico",
        "jpeg",
        "jpg",
        "png",
        "svg",
        "tif",
        "tiff",
        "webp",
        # fonts
        "eot",
        "otf",
        "ttf",
        "woff",
        "woff2",
        # media
        "aac",
        "avi",
        "flac",
        "m4a",
        "m4v",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "mpeg",
        "mpg",
        "oga",
        "ogg",
        "ogv",
        "opus",
        "wav",
        "webm",
        "wmv",
        # archives and binaries
        "7z",
        "bin",
        "bz2",
        "deb",
        "dmg",
        "exe",
        "gz",
        "iso",
        "jar",
        "msi",
        "rar",
        "rpm",
        "tar",
        "tgz",
        "whl",
        "xz",
        "zip",
        "zst",
        # documents and data dumps
        "csv",
        "doc",
        "docx",
        "epub",
        "key",
        "numbers",
        "odp",
        "ods",
        "odt",
        "pages",
        "pdf",
        "ppt",
        "pptx",
        "ps",
        "rtf",
        "tsv",
        "xls",
        "xlsx",
        # code and stylesheets served as files
        "css",
        "js",
        "mjs",
        "map",
        "wasm",
        # engineering artefacts a maker's site links to
        "3mf",
        "dxf",
        "f3d",
        "gcode",
        "hex",
        "kicad_pcb",
        "sch",
        "step",
        "stl",
        "stp",
    }
)

#: Characters that may never appear in a host name. A URL like ``https://not a url`` parses
#: happily into a "hostname" of ``not a url``; without this check it would be accepted as
#: fetchable and the crawl would fail later with a confusing DNS error instead of a clear
#: "that is not an address" at configuration time.
_INVALID_HOST_CHARS: Final[re.Pattern[str]] = re.compile(r"""[\s<>"{}|\\^`]""")

#: Collapses every run of whitespace, used on inline text outside ``<pre>``.
_ALL_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Collapses horizontal whitespace only, preserving the newlines inside ``<pre>``.
_HORIZONTAL_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"[^\S\n]+")

#: Trims the spaces that surround a block boundary once the tree has been flattened.
_SPACED_NEWLINE: Final[re.Pattern[str]] = re.compile(r"[^\S\n]*\n[^\S\n]*")

#: Collapses a run of blank lines to a single blank line.
_BLANK_LINE_RUN: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

#: Block separator emitted around every block-level element.
_BLOCK_BREAK: Final[str] = "\n\n"


# ======================================================================================
# The parse tree
# ======================================================================================


@dataclass(slots=True)
class _Element:
    """One element of the simplified document tree.

    The single representation both parser back ends produce, which is what lets every rule
    in this module — the drop list, block handling, scope selection, metadata extraction —
    be written once.

    Attributes:
        tag: Lowercased tag name; :data:`ROOT_TAG` for the synthetic root.
        attrs: Lowercased attribute names mapped to their values. A valueless attribute
            (``<input disabled>``) maps to the empty string rather than ``None``, so
            callers never need a null check.
        children: Child elements and text runs, in document order.
    """

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Element | str] = field(default_factory=list)

    def attr(self, name: str) -> str:
        """Return one attribute's value.

        Args:
            name: Attribute name, lowercase.

        Returns:
            The attribute value, or the empty string when it is absent.
        """
        return self.attrs.get(name, "")


def _iter_elements(root: _Element) -> list[_Element]:
    """Return every element in the tree, in document order.

    Iterative rather than recursive so that a tree at :data:`MAX_TREE_DEPTH` cannot come
    near the interpreter's recursion limit during the cheap metadata passes.

    Args:
        root: The tree root.

    Returns:
        Every element below *root*, excluding *root* itself.
    """
    found: list[_Element] = []
    stack: list[_Element] = [root]
    while stack:
        node = stack.pop()
        for child in reversed(node.children):
            if isinstance(child, _Element):
                found.append(child)
                stack.append(child)
    return found


# ======================================================================================
# Parser back end: the standard library
# ======================================================================================


class _DomBuilder(HTMLParser):
    """Builds an :class:`_Element` tree from markup with :mod:`html.parser`.

    The fallback used when ``selectolax`` is not installed, and complete in its own right:
    it closes implicitly-ended elements (``<li>a<li>b``), tolerates stray end tags, treats
    void elements as childless, and refuses to nest past :data:`MAX_TREE_DEPTH`.

    Attributes:
        root: The synthetic document root every parsed node hangs from.
    """

    def __init__(self) -> None:
        """Start an empty document."""
        super().__init__(convert_charrefs=True)
        self.root = _Element(tag=ROOT_TAG)
        self._stack: list[_Element] = [self.root]

    # -- helpers ------------------------------------------------------------------------

    @property
    def _current(self) -> _Element:
        """The element currently being filled."""
        return self._stack[-1]

    def _open_tags(self) -> list[str]:
        """Return the tags of the currently open elements, outermost first."""
        return [element.tag for element in self._stack]

    def _close_through(self, tag: str) -> None:
        """Pop the stack up to and including the innermost open *tag*.

        Args:
            tag: The tag to close. Ignored when it is not open, which is what makes a
                stray ``</div>`` harmless.
        """
        open_tags = self._open_tags()
        if tag not in open_tags:
            return
        index = len(open_tags) - 1 - open_tags[::-1].index(tag)
        if index == 0:  # never pop the synthetic root
            return
        del self._stack[index:]

    def _close_implied(self, tag: str) -> None:
        """Close the elements a *tag* start implicitly ends.

        Two rules, both of which real pages depend on: a new ``<li>``/``<td>``/``<dd>``
        closes its open sibling (:data:`IMPLICIT_CLOSE`), and any block-level start tag
        closes an open ``<p>``, because a paragraph cannot contain a block.

        The first rule closes the whole *contiguous run* of implied ancestors rather than
        just the innermost one. That distinction is load-bearing: with ``<tr><th>a<th>b<tr>``
        the stack is ``table > tr > th``, and closing only ``th`` would nest the second row
        inside the first.

        Args:
            tag: The tag about to be opened.
        """
        implied = IMPLICIT_CLOSE.get(tag)
        if implied:
            outermost: int | None = None
            for position in range(len(self._stack) - 1, 0, -1):
                if self._stack[position].tag in implied:
                    outermost = position
                elif outermost is not None:
                    break
            if outermost is not None:
                del self._stack[outermost:]
        if tag in BLOCK_TAGS and tag != "p":
            self._close_through("p")

    # -- HTMLParser hooks ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element, or append it directly when it cannot have children."""
        name = tag.lower()
        self._close_implied(name)
        element = _Element(
            tag=name,
            attrs={key.lower(): (value or "") for key, value in attrs},
        )
        self._current.children.append(element)
        if name in VOID_TAGS or len(self._stack) >= MAX_TREE_DEPTH:
            return
        self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Append a self-closing element without ever opening it."""
        name = tag.lower()
        self._close_implied(name)
        self._current.children.append(
            _Element(tag=name, attrs={key.lower(): (value or "") for key, value in attrs})
        )

    def handle_endtag(self, tag: str) -> None:
        """Close the innermost matching element, ignoring an unmatched end tag."""
        self._close_through(tag.lower())

    def handle_data(self, data: str) -> None:
        """Append a text run to the element being filled."""
        if data:
            self._current.children.append(data)


def _build_with_stdlib(html: str) -> _Element:
    """Parse *html* with :mod:`html.parser`.

    Args:
        html: Raw markup.

    Returns:
        The document tree. Malformed markup still yields a tree — the builder never raises
        on bad input, and a parse that dies mid-document keeps everything parsed so far.
    """
    builder = _DomBuilder()
    try:
        builder.feed(html)
        builder.close()
    except (AssertionError, ValueError) as exc:  # pragma: no cover - pathological markup
        logger.debug("html.stdlib_parse_incomplete", error=str(exc))
    return builder.root


# ======================================================================================
# Parser back end: selectolax
# ======================================================================================


def _convert_selectolax(node: Any, depth: int) -> _Element:
    """Convert one ``selectolax`` node, and its subtree, into an :class:`_Element`.

    Args:
        node: A ``selectolax`` node.
        depth: Current nesting depth, bounded by :data:`MAX_TREE_DEPTH`.

    Returns:
        The converted element.
    """
    raw_attributes = node.attributes or {}
    element = _Element(
        tag=(node.tag or "").lower(),
        attrs={
            str(key).lower(): ("" if value is None else str(value))
            for key, value in raw_attributes.items()
        },
    )
    if depth >= MAX_TREE_DEPTH:
        return element
    for child in node.iter(include_text=True):
        tag = child.tag or ""
        if tag == _SELECTOLAX_TEXT_TAG:
            text = child.text(deep=False)
            if text:
                element.children.append(text)
            continue
        if tag.startswith("-") or tag.startswith("!"):
            # Comments and other non-element nodes carry no prose.
            continue
        element.children.append(_convert_selectolax(child, depth + 1))
    return element


def _build_with_selectolax(html: str) -> _Element | None:
    """Parse *html* with ``selectolax`` when it is installed.

    Preferred over the standard library because Lexbor implements the HTML5 tree
    construction algorithm, so it recovers from the tag soup real sites ship far more
    faithfully than a stack of start/end tags can.

    Args:
        html: Raw markup.

    Returns:
        The document tree, or ``None`` when ``selectolax`` is absent or refused the input —
        in which case the caller falls back to :func:`_build_with_stdlib`.
    """
    try:
        from selectolax.parser import HTMLParser as SelectolaxParser
    except ImportError:
        return None
    except Exception as exc:
        logger.debug("html.selectolax_unavailable", error=str(exc))
        return None

    try:
        tree = SelectolaxParser(html)
        root_node = tree.root
        if root_node is None:
            return None
        root = _Element(tag=ROOT_TAG)
        root.children.append(_convert_selectolax(root_node, 1))
    except Exception as exc:
        logger.debug("html.selectolax_parse_failed", error=str(exc))
        return None
    return root


def _parse(html: str) -> tuple[_Element, str]:
    """Parse *html* with the best available back end.

    Args:
        html: Raw markup.

    Returns:
        ``(root, parser_name)`` where *parser_name* is ``"selectolax"`` or ``"stdlib"``,
        reported in the returned metadata so a support question about odd extraction can be
        answered without guessing which parser ran.
    """
    root = _build_with_selectolax(html)
    if root is not None:
        return root, "selectolax"
    return _build_with_stdlib(html), "stdlib"


# ======================================================================================
# Rendering
# ======================================================================================


def _inline_text(text: str, *, preformatted: bool) -> str:
    """Normalise one text run.

    Args:
        text: The raw text run.
        preformatted: Whether it sits inside ``<pre>``, where newlines are meaningful.

    Returns:
        The run with runs of whitespace collapsed — to a single space normally, and to a
        single space *per line* inside ``<pre>``.
    """
    if not preformatted:
        return _ALL_WHITESPACE.sub(" ", text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _HORIZONTAL_WHITESPACE.sub(" ", normalized)


def _plain_length(element: _Element) -> int:
    """Return how many characters of readable text an element holds.

    Used to choose between ``<main>``, ``<article>`` and ``<body>`` without rendering each
    candidate. Dropped subtrees do not count, so a ``<body>`` consisting of one analytics
    script does not beat a real ``<main>``.

    Args:
        element: The container to measure.

    Returns:
        The number of non-whitespace-collapsed characters below it.
    """
    total = 0
    stack: list[_Element] = [element]
    while stack:
        node = stack.pop()
        for child in node.children:
            if isinstance(child, str):
                total += len(_ALL_WHITESPACE.sub(" ", child).strip())
            elif child.tag not in DROP_TAGS:
                stack.append(child)
    return total


def _render(
    element: _Element,
    *,
    preformatted: bool,
    headings: list[dict[str, Any]],
    depth: int = 0,
) -> str:
    """Render an element's children to markdown-ish text.

    Block elements are wrapped in blank lines, headings become ``#`` prefixes and are also
    appended to *headings*, list items gain a ``- `` marker, table cells are separated by
    ``|``, ``<br>`` becomes a newline, and ``<img alt>`` contributes its alternative text —
    which on a portfolio is frequently the only description a screenshot has.

    Args:
        element: The container whose children are rendered.
        preformatted: Whether this subtree is inside ``<pre>``.
        headings: Accumulator for the outline, appended to in document order.
        depth: Current recursion depth, bounded by :data:`MAX_TREE_DEPTH`.

    Returns:
        The rendered text, still containing redundant whitespace; :func:`_finalize`
        performs the single global cleanup pass.
    """
    if depth >= MAX_TREE_DEPTH:
        return ""

    pieces: list[str] = []
    for child in element.children:
        if isinstance(child, str):
            pieces.append(_inline_text(child, preformatted=preformatted))
            continue

        tag = child.tag
        if tag in DROP_TAGS:
            continue
        if tag == "br":
            pieces.append("\n")
            continue
        if tag == "hr":
            pieces.append(_BLOCK_BREAK)
            continue
        if tag == "img":
            alt = _ALL_WHITESPACE.sub(" ", child.attr("alt")).strip()
            if alt:
                pieces.append(f" {alt} ")
            continue

        level = HEADING_LEVELS.get(tag)
        if level is not None:
            text = _ALL_WHITESPACE.sub(
                " ", _render(child, preformatted=False, headings=headings, depth=depth + 1)
            ).strip()
            if text:
                if len(headings) < MAX_HEADINGS:
                    headings.append({"level": level, "text": text})
                pieces.append(f"{_BLOCK_BREAK}{'#' * level} {text}{_BLOCK_BREAK}")
            continue

        inner = _render(
            child,
            preformatted=preformatted or tag in PREFORMATTED_TAGS,
            headings=headings,
            depth=depth + 1,
        )
        if tag == "li":
            body = inner.strip()
            if body:
                # One leading newline, not a block break: consecutive items must stay on
                # consecutive lines so the list survives as a list into split_bullets().
                pieces.append(f"\n- {body}")
            continue
        if tag in _CELL_TAGS:
            body = inner.strip()
            if body:
                pieces.append(f"{body} | ")
            continue
        if tag == "tr":
            body = inner.strip().rstrip("|").strip()
            if body:
                pieces.append(f"\n{body}")
            continue
        if tag in BLOCK_TAGS:
            if inner.strip():
                pieces.append(f"{_BLOCK_BREAK}{inner}{_BLOCK_BREAK}")
            continue
        pieces.append(inner)

    return "".join(pieces)


def _finalize(text: str) -> str:
    """Collapse the rendered text's redundant whitespace, once, globally.

    Args:
        text: Rendered text straight out of :func:`_render`.

    Returns:
        Text with horizontal whitespace collapsed, no spaces hugging a line break, at most
        one blank line between blocks, and no leading or trailing whitespace.
    """
    collapsed = _HORIZONTAL_WHITESPACE.sub(" ", text)
    collapsed = _SPACED_NEWLINE.sub("\n", collapsed)
    return _BLANK_LINE_RUN.sub(_BLOCK_BREAK, collapsed).strip()


# ======================================================================================
# Metadata
# ======================================================================================


def _meta_index(elements: list[_Element]) -> dict[str, str]:
    """Index every ``<meta>`` element by its ``name``/``property``/``http-equiv``.

    Args:
        elements: Every element of the document.

    Returns:
        Lowercased key to content value, first occurrence winning — which is the one a
        browser honours too.
    """
    index: dict[str, str] = {}
    for element in elements:
        if element.tag != "meta":
            continue
        content = element.attr("content").strip()
        if not content:
            continue
        for attribute in ("name", "property", "http-equiv"):
            key = element.attr(attribute).strip().lower()
            if key:
                index.setdefault(key, content)
    return index


def _first_value(index: dict[str, str], keys: tuple[str, ...]) -> str:
    """Return the first non-empty value among *keys*.

    Args:
        index: A metadata index.
        keys: Candidate keys, most authoritative first.

    Returns:
        The value, or the empty string when none of the keys is present.
    """
    for key in keys:
        value = index.get(key, "").strip()
        if value:
            return value
    return ""


def _element_text(element: _Element) -> str:
    """Return an element's collapsed text content, ignoring dropped subtrees.

    Args:
        element: Any element.

    Returns:
        Its readable text on one line.
    """
    return _ALL_WHITESPACE.sub(" ", _render(element, preformatted=False, headings=[])).strip()


def _document_base(elements: list[_Element], base_url: str | None) -> str | None:
    """Resolve the base URL relative links are measured against.

    Args:
        elements: Every element of the document.
        base_url: The URL the document was fetched from, when known.

    Returns:
        The document's ``<base href>`` resolved against *base_url*, else *base_url*, else
        ``None``.
    """
    for element in elements:
        if element.tag != "base":
            continue
        href = element.attr("href").strip()
        if href:
            return urljoin(base_url, href) if base_url else href
    return base_url


def _collect_links(root: _Element, base: str | None) -> list[str]:
    """Harvest the outbound links of a document.

    Walks the *whole* tree rather than the content scope, because navigation and footer
    links are how a crawler discovers the pages worth reading even though their text is
    stripped from the prose.

    Args:
        root: The document root.
        base: Base URL for resolving relative hrefs, or ``None`` to return them as written.

    Returns:
        Up to :data:`MAX_LINKS` normalised URLs, deduplicated, in document order.
    """
    links: list[str] = []
    seen: set[str] = set()
    stack: list[_Element] = [root]
    while stack and len(links) < MAX_LINKS:
        node = stack.pop()
        descend: list[_Element] = []
        for child in node.children:
            if not isinstance(child, _Element) or child.tag in LINK_SKIP_TAGS:
                continue
            descend.append(child)
            if child.tag not in _LINK_TAGS:
                continue
            href = child.attr("href").strip()
            if not href or href.startswith("#"):
                continue
            resolved = normalize_url(href, base)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            links.append(resolved)
            if len(links) >= MAX_LINKS:
                break
        # Reversed, so that popping the stack keeps the walk in document order and the
        # returned links read in the order a person would encounter them on the page.
        stack.extend(reversed(descend))
    return links


def _select_scope(root: _Element, elements: list[_Element]) -> tuple[list[_Element], str]:
    """Choose the containers holding the page's actual content.

    ``<main>`` wins when the page has one, then ``<article>`` (all of them, since an index
    page legitimately lists several), then ``<body>``, then the whole document. A candidate
    is only accepted when it holds at least :data:`MIN_SCOPE_CHARS` of text, so a decorative
    empty ``<main>`` falls through instead of blanking the page; when nothing clears the
    floor the largest candidate wins.

    Args:
        root: The document root.
        elements: Every element of the document.

    Returns:
        ``(containers, scope_name)``.
    """
    by_tag: dict[str, list[_Element]] = {tag: [] for tag in SCOPE_PRIORITY}
    for element in elements:
        bucket = by_tag.get(element.tag)
        if bucket is not None:
            bucket.append(element)

    candidates: list[tuple[str, list[_Element], int]] = []
    for tag in SCOPE_PRIORITY:
        containers = by_tag[tag]
        if not containers:
            continue
        size = sum(_plain_length(container) for container in containers)
        if size >= MIN_SCOPE_CHARS:
            return containers, tag
        candidates.append((tag, containers, size))

    document_size = _plain_length(root)
    candidates.append(("document", [root], document_size))
    best = max(candidates, key=lambda candidate: candidate[2])
    return best[1], best[0]


# ======================================================================================
# Public API — HTML
# ======================================================================================


def html_to_text(html: str, *, base_url: str | None = None) -> tuple[str, dict[str, Any]]:
    """Extract readable text and document metadata from *html*.

    The one HTML-to-text conversion in ApplicantOS. Site chrome (:data:`DROP_TAGS`) is
    discarded, ``<main>``/``<article>`` is preferred over the whole body, and the surviving
    prose is rendered markdown-ish: ``#`` heading prefixes, ``- `` list markers, ``|``
    between table cells, blank lines between blocks, whitespace collapsed. That shape is not
    cosmetic — it is what lets :func:`~app.knowledge.extractors.split_bullets` and
    :func:`~app.knowledge.extractors.detect_project_name` treat a crawled portfolio page
    exactly as they treat a README.

    Args:
        html: Raw markup. Empty or whitespace-only input yields empty results rather than
            an error.
        base_url: The URL the document came from. When given, the returned ``links`` are
            absolute and normalised (honouring any ``<base href>``); when omitted they are
            returned as the document wrote them.

    Returns:
        ``(text, metadata)``. The metadata mapping always carries every one of these keys:

        ``title``
            ``<title>``, else ``og:title``, else the first heading, else ``""``.
        ``description``
            ``meta[name=description]``, else ``og:description``, else
            ``twitter:description``, else ``None``.
        ``headings``
            Outline as ``[{"level": int, "text": str}, …]``, capped at
            :data:`MAX_HEADINGS`.
        ``lang``
            The ``<html lang>`` value, or ``None``.
        ``canonical``
            ``<link rel=canonical href>`` resolved against *base_url*, or ``None``.
        ``links``
            Outbound ``<a href>`` targets, deduplicated, capped at :data:`MAX_LINKS`.
            Collected from the entire document including navigation, because that is where
            a crawler finds the next page.
        ``scope``
            Which container the text came from: ``"main"``, ``"article"``, ``"body"`` or
            ``"document"``.
        ``parser``
            ``"selectolax"`` or ``"stdlib"``.
    """
    metadata: dict[str, Any] = {
        "title": "",
        "description": None,
        "headings": [],
        "lang": None,
        "canonical": None,
        "links": [],
        "scope": "document",
        "parser": "stdlib",
    }
    if not html or not html.strip():
        return "", metadata

    root, parser_name = _parse(html)
    metadata["parser"] = parser_name

    elements = _iter_elements(root)
    meta_index = _meta_index(elements)
    base = _document_base(elements, base_url)

    for element in elements:
        if element.tag == "html":
            lang = element.attr("lang").strip()
            if lang:
                metadata["lang"] = lang
            break

    for element in elements:
        if element.tag == "link" and "canonical" in element.attr("rel").lower().split():
            href = element.attr("href").strip()
            if href:
                metadata["canonical"] = normalize_url(href, base)
            break

    title = ""
    for element in elements:
        if element.tag == "title":
            title = _element_text(element)
            break
    if not title:
        title = _first_value(meta_index, ("og:title", "twitter:title"))

    description = _first_value(meta_index, ("description", "og:description", "twitter:description"))
    metadata["description"] = description or None

    containers, scope = _select_scope(root, elements)
    metadata["scope"] = scope

    headings: list[dict[str, Any]] = []
    text = _finalize(
        _BLOCK_BREAK.join(
            _render(container, preformatted=False, headings=headings) for container in containers
        )
    )
    metadata["headings"] = headings

    if not title and headings:
        title = headings[0]["text"]
    metadata["title"] = title

    metadata["links"] = _collect_links(root, base)
    return text, metadata


def extract_links(html: str, base: str | None = None) -> list[str]:
    """Return the outbound links of *html*.

    A thin convenience over :func:`html_to_text` — callers that already need the page text
    should read ``metadata["links"]`` from that call instead of parsing twice.

    Args:
        html: Raw markup.
        base: Base URL for resolving relative hrefs.

    Returns:
        Up to :data:`MAX_LINKS` normalised URLs, deduplicated, in document order.
    """
    return list(html_to_text(html, base_url=base)[1]["links"])


# ======================================================================================
# Public API — URLs
# ======================================================================================


def _remove_dot_segments(path: str) -> str:
    """Resolve ``.`` and ``..`` segments in a URL path.

    Args:
        path: The path component.

    Returns:
        The path with relative segments resolved, per RFC 3986 §5.2.4.
    """
    if "." not in path:
        return path
    resolved: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(resolved) > 1:
                resolved.pop()
            continue
        resolved.append(segment)
    return "/".join(resolved)


def _authority(parts: Any) -> str:
    """Rebuild a URL authority with a lowercase host and no default port.

    Args:
        parts: The :func:`urllib.parse.urlsplit` result.

    Returns:
        The canonical authority, or the empty string when the URL has none.
    """
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        # An out-of-range port. Keep the authority verbatim rather than losing the URL.
        return parts.netloc.lower()
    if not host:
        return ""
    authority = host
    if port is not None and DEFAULT_PORTS.get(parts.scheme.lower()) != port:
        authority = f"{host}:{port}"
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials = f"{credentials}:{parts.password}"
        authority = f"{credentials}@{authority}"
    return authority


def normalize_url(url: str, base: str | None = None) -> str:
    """Return the canonical form of *url*.

    Canonicalisation is what makes a visited-set work: ``https://Example.com:443/work/``,
    ``https://example.com/work`` and ``/work/#top`` linked from a page on that host are one
    URL, visited once. Specifically the result is absolute (when *base* allows), has a
    lowercase scheme and host, no default port, no ``.``/``..`` segments, no fragment, and
    no trailing slash. The path's case and the query string are preserved untouched — both
    are case- and order-sensitive on real servers.

    Args:
        url: An absolute or relative URL, as written in the document.
        base: The URL to resolve a relative reference against.

    Returns:
        The canonical URL, or the empty string when *url* is blank. Non-HTTP references
        (``mailto:``, ``tel:``, ``javascript:``) are returned canonicalised but otherwise
        intact, so a caller can recognise and skip them; see :func:`is_http_url`.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""
    candidate = candidate.replace("\n", "").replace("\r", "").replace("\t", "")

    if base:
        try:
            candidate = urljoin(base, candidate)
        except ValueError:  # pragma: no cover - urljoin is tolerant, but never trust input
            logger.debug("url.join_failed", url=url, base=base)

    try:
        parts = urlsplit(candidate)
    except ValueError:
        logger.debug("url.unparseable", url=url)
        return candidate

    scheme = parts.scheme.lower()
    if scheme and scheme not in FETCHABLE_SCHEMES:
        # Opaque schemes have no authority or path to canonicalise; drop only the fragment.
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, "")).strip()

    authority = _authority(parts)
    path = _remove_dot_segments(parts.path)
    if authority and path and not path.startswith("/"):
        path = f"/{path}"
    path = path.rstrip("/")
    return urlunsplit((scheme, authority, path, parts.query, ""))


def is_http_url(url: str) -> bool:
    """Return whether *url* is something a crawler may fetch.

    Args:
        url: Any URL or URL-shaped string.

    Returns:
        ``True`` for absolute ``http``/``https`` URLs with a syntactically plausible host.
        ``mailto:``, ``tel:``, ``javascript:``, ``data:``, bare fragments and anything whose
        "host" contains a character a host cannot contain all return ``False``.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme.lower() not in FETCHABLE_SCHEMES or not host:
        return False
    return _INVALID_HOST_CHARS.search(host) is None


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Return the ``(scheme, host, port)`` triple identifying a URL's origin.

    Args:
        url: Any URL.

    Returns:
        The triple with the host's :data:`WWW_PREFIX` removed and the default port resolved,
        or ``None`` when *url* names no host.
    """
    try:
        parts = urlsplit(normalize_url(url))
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    host = host.removeprefix(WWW_PREFIX)
    scheme = parts.scheme.lower()
    return (scheme, host, port if port is not None else DEFAULT_PORTS.get(scheme))


def same_origin(a: str, b: str) -> bool:
    """Return whether two URLs belong to the same site.

    Scheme, host and port must all match, with two normalisations applied first: the default
    port is filled in, so ``https://x.com`` and ``https://x.com:443`` agree, and a leading
    ``www.`` is ignored, so ``example.com`` and ``www.example.com`` agree. The second is a
    deliberate relaxation of the browser's origin rule — see :data:`WWW_PREFIX`.

    Args:
        a: The first URL.
        b: The second URL.

    Returns:
        ``True`` when both name a host and the hosts are the same site. A URL with no host
        never matches anything, including another hostless URL.
    """
    origin_a = _origin(a)
    if origin_a is None:
        return False
    return origin_a == _origin(b)


def is_asset_url(url: str) -> bool:
    """Return whether *url* points at a file rather than a page.

    Args:
        url: Any URL; the query string and fragment are ignored, so
            ``/logo.png?v=3#top`` is still an asset.

    Returns:
        ``True`` when the path's final extension is in :data:`ASSET_EXTENSIONS`. A path with
        no extension, or one ending in ``.html``/``.php``/``.aspx``, is a page.
    """
    if not url:
        return False
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    _, separator, extension = path.rpartition(".")
    if not separator or "/" in extension:
        return False
    return extension.lower() in ASSET_EXTENSIONS
