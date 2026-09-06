"""Reading a rendered front configuration back, and answering which routes it leaves unguarded.

This is the half of the card that has to survive people. A template that happens to contain
`basicauth` today proves nothing about a file after somebody adds a `handle` block for a new route,
and a test that lists the protected paths by hand is a list that goes stale the first time the
route table grows. So the question is asked the other way round: parse the configuration, and for
every route the transport publishes -- read from `secretary.web.app.ROUTES`, the same table
`docs/PROTOCOLS.md` documents -- decide whether a request for it reaches something that answers
before a password was checked.

The parser is small and deliberately not a general Caddyfile implementation. It understands the
grammar this project generates and the grammar somebody would plausibly hand-edit it into: site
blocks, named matchers, path matchers, nested `handle`/`handle_path`/`route` blocks, and the
directives that end a request. Anything it does not recognise as a guard is not a guard, and
anything it does not recognise as terminal is asked about again inside its own block -- both
defaults fail towards reporting a route as unguarded rather than towards silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Directives that check the password. Caddy renamed the directive in 2.8 and kept the old spelling
#: working; the archive build this installation runs is 2.6 and spells it `basicauth`.
GUARD_DIRECTIVES = frozenset({"basicauth", "basic_auth"})

#: Directives that answer a request. Reaching one of these without a guard is the finding.
TERMINAL_DIRECTIVES = frozenset(
    {
        "abort",
        "acme_server",
        "error",
        "file_server",
        "forward_proxy",
        "php_fastcgi",
        "respond",
        "reverse_proxy",
        "static_response",
        "templates",
    }
)

#: Directives that carry their own block of directives, scoped to their matcher.
NESTING_DIRECTIVES = frozenset({"handle", "handle_path", "route"})


class CaddyfileSyntaxError(ValueError):
    """The text handed in is not a Caddyfile this reader can account for."""


@dataclass(frozen=True)
class Directive:
    name: str
    args: tuple[str, ...] = ()
    block: tuple[Directive, ...] = ()


@dataclass(frozen=True)
class Site:
    """One site block: the addresses in its header and the directives in its body."""

    addresses: tuple[str, ...]
    directives: tuple[Directive, ...] = ()
    #: Matchers the block named, by their `@name`.
    matchers: dict[str, tuple[str, ...]] = field(default_factory=dict)


def parse(text: str) -> tuple[Site, ...]:
    """Site blocks, in order. The global options block carries no addresses and is dropped."""
    tokens = _tokenize(text)
    position = 0
    sites: list[Site] = []
    while position < len(tokens):
        while position < len(tokens) and tokens[position] == "\n":
            position += 1
        if position >= len(tokens):
            break
        header: list[str] = []
        while position < len(tokens) and tokens[position] not in ("{", "}"):
            if tokens[position] != "\n":
                header.append(tokens[position])
            position += 1
        if position >= len(tokens):
            raise CaddyfileSyntaxError(f"the block headed {' '.join(header)!r} never opens")
        if tokens[position] == "}":
            raise CaddyfileSyntaxError("a closing brace appears where a block header was expected")
        directives, position = _block(tokens, position + 1)
        addresses = tuple(part for part in " ".join(header).replace(",", " ").split() if part)
        if addresses:
            sites.append(Site(addresses, directives, _named_matchers(directives)))
    return tuple(sites)


def _named_matchers(directives: tuple[Directive, ...]) -> dict[str, tuple[str, ...]]:
    """`@name path /a /b*` and its block form, flattened to the paths they match.

    A named matcher this reader cannot reduce to paths is recorded as matching everything, because
    a matcher it does not understand must never be the reason a route is called guarded.
    """
    matchers: dict[str, tuple[str, ...]] = {}
    for directive in directives:
        if not directive.name.startswith("@"):
            continue
        if directive.args and directive.args[0] == "path":
            matchers[directive.name] = tuple(directive.args[1:])
            continue
        paths: list[str] = []
        for inner in directive.block:
            if inner.name == "path":
                paths.extend(inner.args)
        matchers[directive.name] = tuple(paths) if paths else ("*",)
    return matchers


def unguarded_routes(text: str, routes) -> tuple[str, ...]:
    """Every published route this configuration would answer without checking the password.

    ``routes`` is the transport's own route table: each entry carries a `method` and a `pattern`,
    and a pattern's `{placeholder}` stands for one path segment. The answer names the route and the
    site that would serve it, so a finding says where to look.
    """
    findings: list[str] = []
    sites = parse(text)
    for site in sites:
        if _only_redirects_to_https(site):
            continue
        for route in routes:
            pattern = getattr(route, "pattern", route)
            method = getattr(route, "method", "GET")
            if not _guards(site, _concrete(pattern)):
                findings.append(f"{method} {pattern} is answered unguarded by {' '.join(site.addresses)}")
    return tuple(findings)


def upstreams(text: str) -> tuple[str, ...]:
    """Every address this configuration proxies to, in order. Used to pin it to loopback."""
    found: list[str] = []

    def walk(directives: tuple[Directive, ...]) -> None:
        for directive in directives:
            if directive.name == "reverse_proxy":
                found.extend(argument for argument in directive.args if not _is_matcher(argument))
            walk(directive.block)

    for site in parse(text):
        walk(site.directives)
    return tuple(found)


# -- deciding one path ---------------------------------------------------------------------------


def _guards(site: Site, path: str) -> bool:
    """Whether every answer this site would give for ``path`` comes after a password check."""
    return _walk(site, site.directives, path, guarded=False)


def _walk(site: Site, directives: tuple[Directive, ...], path: str, *, guarded: bool) -> bool:
    for directive in directives:
        if directive.name.startswith("@"):
            continue
        matcher = directive.args[0] if directive.args and _is_matcher(directive.args[0]) else "*"
        if not _matches(site, matcher, path):
            continue
        if directive.name in GUARD_DIRECTIVES:
            guarded = True
            continue
        if directive.name in NESTING_DIRECTIVES:
            # `handle` is exclusive: the first one that matches decides the request, so the answer
            # for this path is whatever happens inside it and nothing after it matters.
            return _walk(site, directive.block, path, guarded=guarded)
        if directive.name == "redir":
            return guarded or _is_https_redirect(directive)
        if directive.name in TERMINAL_DIRECTIVES:
            return guarded
    # Nothing in this site answers this path: Caddy has nothing to serve, so nothing leaks.
    return True


def _only_redirects_to_https(site: Site) -> bool:
    """An `http://` block whose whole body is a redirect to https serves no content."""
    body = [directive for directive in site.directives if not directive.name.startswith("@")]
    return bool(body) and all(
        directive.name == "redir" and _is_https_redirect(directive) for directive in body
    )


def _is_https_redirect(directive: Directive) -> bool:
    targets = [argument for argument in directive.args if not _is_matcher(argument)]
    return bool(targets) and all(target.startswith("https://") for target in targets)


def _matches(site: Site, matcher: str, path: str) -> bool:
    if matcher == "*":
        return True
    if matcher.startswith("@"):
        patterns = site.matchers.get(matcher)
        if patterns is None:
            # A matcher this file never defined is not something to guess about.
            raise CaddyfileSyntaxError(f"{matcher} is used but never defined")
        return any(_path_matches(pattern, path) for pattern in patterns)
    if matcher.startswith("/"):
        return _path_matches(matcher, path)
    # Not a matcher this reader understands; treat it as matching so it can never be the reason a
    # route was called guarded, and so a terminal directive behind it is still reported.
    return True


def _path_matches(pattern: str, path: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


def _is_matcher(token: str) -> bool:
    return token == "*" or token.startswith(("@", "/"))


def _concrete(pattern: str) -> str:
    """One concrete path a route pattern stands for: `/tasks/{ref}` is a request for `/tasks/x`."""
    parts = []
    for part in pattern.split("/"):
        parts.append("sample" if part.startswith("{") and part.endswith("}") else part)
    return "/".join(parts)


# -- tokens --------------------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = ""
        quote = ""
        for character in line:
            if quote:
                if character == quote:
                    quote = ""
                else:
                    current += character
                continue
            if character in "\"'":
                quote = character
                continue
            if character == "#" and not current:
                break
            if character in "{}":
                if current:
                    tokens.append(current)
                    current = ""
                tokens.append(character)
                continue
            if character.isspace():
                if current:
                    tokens.append(current)
                    current = ""
                continue
            current += character
        if current:
            tokens.append(current)
        tokens.append("\n")
    return tokens


def _block(tokens: list[str], position: int) -> tuple[tuple[Directive, ...], int]:
    """Read directives until the brace that closes this block."""
    directives: list[Directive] = []
    while True:
        if position >= len(tokens):
            raise CaddyfileSyntaxError("a block is never closed")
        token = tokens[position]
        if token == "\n":
            position += 1
            continue
        if token == "}":
            return tuple(directives), position + 1
        name = token
        position += 1
        args: list[str] = []
        block: tuple[Directive, ...] = ()
        while position < len(tokens) and tokens[position] not in ("{", "}", "\n"):
            args.append(tokens[position])
            position += 1
        if position < len(tokens) and tokens[position] == "{":
            block, position = _block(tokens, position + 1)
        directives.append(Directive(name, tuple(args), block))
    # unreachable
