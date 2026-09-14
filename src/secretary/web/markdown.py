"""A small, safe Markdown subset for the PO head's answers on the session page.

The input is escaped with `html.escape` before anything else looks at it, and only then is a fixed
subset turned into a fixed set of tags: `h3`-`h6`, `p`, `br`, `strong`, `em`, `code`, `pre`, `ul`,
`ol`, `li`, `blockquote`, `hr` and `a`. No raw HTML survives, because there is no raw HTML left by
the time the markers are read. The only attributes ever written are a `class` on code and tables
and `href`, `rel` and `target` on a link, and a link is written only for `http:`, `https:` or
`mailto:`; any other target stays the text it was.

Nothing here backtracks over the whole message. Emphasis is paired by splitting a line on its
delimiter, a fence finds its close from a table computed once, and blockquotes nest at most
`_MAX_QUOTE_DEPTH` deep, so a hostile answer (ten thousand `*`, deeply nested `>`) costs about as
much as a friendly one of the same length. A marker that does not close is shown as typed and ends
nothing but itself.
"""

from __future__ import annotations

import html
import re

_MAX_QUOTE_DEPTH = 3

_FENCE = re.compile(r" {0,3}```\s*([A-Za-z0-9_+-]*)\s*$")
_FENCE_CLOSE = re.compile(r" {0,3}```\s*$")
_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*)$")
_RULE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE = re.compile(r" {0,3}&gt; ?(.*)$")
_ITEM = re.compile(r"( *)([-*]|\d{1,9}[.)])[ \t]+(.*)$")
_TABLE = re.compile(r" {0,3}\|.*\|[ \t]*$")

# Inline tokens, matched on the escaped text: a code span, a link, a bare http(s) URL. A URL stops
# at whitespace and at an escaped `<`, `>`, `"` or `'`, so it never carries a neighbour's markup.
# A link target may carry an escaped quote (it stays escaped in the attribute) but stops at `[`, `]`
# and `(`, so an unclosed `[x](` scans only to the next one.
_URL_CHAR = r"(?:(?!&lt;|&gt;|&quot;|&#x27;)[^\s])"
_TARGET_CHAR = r"(?:(?!&lt;|&gt;)[^\s\[\]()])"
_INLINE = re.compile(
    r"`([^`]+)`"
    r"|\[([^\[\]]+)\]\((" + _TARGET_CHAR + r"+)\)"
    r"|(https?://" + _URL_CHAR + r"+)",
    re.IGNORECASE,
)
_SAFE_SCHEME = re.compile(r"(?:https?|mailto):", re.IGNORECASE)
_TRAILING = ".,;:!?)"


def render(text: str) -> str:
    """The message as HTML: escaped first, then the supported Markdown subset."""
    escaped = html.escape(text.replace("\r\n", "\n").replace("\r", "\n"), quote=True)
    return _blocks(escaped.split("\n"), 0)


def _blocks(lines: list[str], depth: int) -> str:
    closes = _fence_closes(lines)
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence and closes[i + 1] is not None:
            flush()
            end = closes[i + 1]
            language = fence.group(1)
            css = f' class="language-{language}"' if language else ""
            out.append(f"<pre><code{css}>" + "\n".join(lines[i + 1 : end]) + "</code></pre>")
            i = end + 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(_heading_text(heading.group(2)))}</h{level}>")
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if _QUOTE.match(line):
            flush()
            quoted: list[str] = []
            while i < len(lines) and (match := _QUOTE.match(lines[i])):
                quoted.append(match.group(1))
                i += 1
            if depth + 1 < _MAX_QUOTE_DEPTH:
                inner = _blocks(quoted, depth + 1)
            else:
                inner = "<p>" + "<br>".join(_inline(part) for part in quoted) + "</p>"
            out.append(f"<blockquote>{inner}</blockquote>")
            continue
        if _ITEM.match(line):
            flush()
            html_list, i = _list(lines, i)
            out.append(html_list)
            continue
        if _TABLE.match(line) and i + 1 < len(lines) and _TABLE.match(lines[i + 1]):
            flush()
            rows: list[str] = []
            while i < len(lines) and _TABLE.match(lines[i]):
                rows.append(lines[i])
                i += 1
            out.append('<pre class="table">' + "\n".join(rows) + "</pre>")
            continue
        paragraph.append(line)
        i += 1
    flush()
    return "".join(out)


def _heading_text(text: str) -> str:
    """The heading without trailing spaces and an optional closing run of `#`."""
    text = text.rstrip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1] in " \t"):
        return bare.rstrip()
    return text


def _fence_closes(lines: list[str]) -> list[int | None]:
    """For each position, the index of the first bare closing fence at or after it, or None."""
    closes: list[int | None] = [None] * (len(lines) + 1)
    following: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if _FENCE_CLOSE.match(lines[index]):
            following = index
        closes[index] = following
    return closes


def _list(lines: list[str], start: int) -> tuple[str, int]:
    """A list starting at `start`, with one nested level by indentation; returns HTML and next index."""
    first = _ITEM.match(lines[start])
    assert first is not None
    base = len(first.group(1))
    tag = _list_tag(first.group(2))
    # Each item: its text lines, and its nested items as (tag, text lines).
    items: list[tuple[list[str], list[tuple[str, list[str]]]]] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            following = i + 1
            while following < len(lines) and not lines[following].strip():
                following += 1
            nxt = _ITEM.match(lines[following]) if following < len(lines) else None
            if nxt and (len(nxt.group(1)) > base or _list_tag(nxt.group(2)) == tag):
                i = following
                continue
            break
        item = _ITEM.match(line)
        indent = len(line) - len(line.lstrip(" "))
        if item and indent < base + 2:
            if _list_tag(item.group(2)) != tag:
                break
            items.append(([item.group(3)], []))
        elif item and items:
            items[-1][1].append((_list_tag(item.group(2)), [item.group(3)]))
        elif indent >= base + 2 and items:
            nested = items[-1][1]
            (nested[-1][1] if nested else items[-1][0]).append(line.strip())
        else:
            break
        i += 1
    parts = [f"<{tag}>"]
    for text, nested in items:
        parts.append("<li>" + "<br>".join(_inline(part) for part in text))
        open_tag: str | None = None
        for nested_tag, nested_text in nested:
            if nested_tag != open_tag:
                if open_tag:
                    parts.append(f"</{open_tag}>")
                parts.append(f"<{nested_tag}>")
                open_tag = nested_tag
            parts.append("<li>" + "<br>".join(_inline(part) for part in nested_text) + "</li>")
        if open_tag:
            parts.append(f"</{open_tag}>")
        parts.append("</li>")
    parts.append(f"</{tag}>")
    return "".join(parts), i


def _list_tag(marker: str) -> str:
    return "ul" if marker in "-*" else "ol"


def _inline(text: str) -> str:
    """One escaped line: code spans, links and bare URLs as tokens, emphasis on the text between."""
    out: list[str] = []
    position = 0
    for match in _INLINE.finditer(text):
        code, label, target, bare = match.groups()
        if bare is not None:
            url = bare.rstrip(_TRAILING)
            if url.count("://") != 1 or len(url) <= len(url.split("://", 1)[0]) + 3:
                continue
            out.append(_emphasis(text[position : match.start()]))
            out.append(_link(url, url))
            position = match.start() + len(url)
            continue
        out.append(_emphasis(text[position : match.start()]))
        if code is not None:
            out.append(f"<code>{code}</code>")
        elif _safe(target):
            out.append(_link(target, _emphasis(label)))
        else:
            out.append(match.group(0))
        position = match.end()
    out.append(_emphasis(text[position:]))
    return "".join(out)


def _safe(target: str) -> bool:
    original = html.unescape(target)
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in original):
        return False
    return _SAFE_SCHEME.match(original) is not None


def _link(target: str, label: str) -> str:
    # `target` is already escaped (& < > " '); a backtick is the one quote html.escape leaves.
    href = target.replace("`", "&#96;")
    return f'<a href="{href}" rel="noopener noreferrer" target="_blank">{label}</a>'


def _emphasis(text: str) -> str:
    text = _pair(text, "**", "strong", word_bound=False)
    text = _pair(text, "*", "em", word_bound=False)
    return _pair(text, "_", "em", word_bound=True)


def _pair(text: str, marker: str, tag: str, *, word_bound: bool) -> str:
    """Pair `marker` delimiters left to right in one pass; an unpaired one stays literal.

    An opener must be followed by a non-space and a closer preceded by one; with `word_bound` an
    opener may not follow a word character and a closer may not precede one, so `snake_case` stays.
    A pair never crosses a tag an earlier marker produced: the span between must be balanced.
    """
    if marker not in text:
        return text
    parts = text.split(marker)
    out: list[str] = [parts[0]]
    opener: int | None = None
    balance = low = 0
    for index in range(1, len(parts)):
        before, after = parts[index - 1], parts[index]
        if (
            opener is not None
            and balance == 0
            and low == 0
            and before
            and not before[-1].isspace()
            and (not word_bound or not after or not _word(after[0]))
        ):
            out[opener] = f"<{tag}>"
            out.append(f"</{tag}>")
            opener = None
        elif after and not after[0].isspace() and (not word_bound or not before or not _word(before[-1])):
            opener = len(out)
            out.append(marker)
            balance = low = 0
        else:
            out.append(marker)
        out.append(after)
        if opener is not None:
            for piece in re.findall(r"</?", after):
                balance += -1 if piece == "</" else 1
                low = min(low, balance)
    return "".join(out)


def _word(char: str) -> bool:
    return char.isalnum() or char == "_"
