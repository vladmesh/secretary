"""The shared bottom bar: every page carries it, it costs no provider read, and it never invents a number.

Hermetic. Every layer is a recording fake and the one real object is
:class:`~secretary.web.provider_usage.ProviderUsageLayer`, driven over a temporary home with a
counting `fetch_json` and a clock this test moves — which is the only way to ask the question
criterion 4 asks: does *rendering* cost a provider read. It does not; the cache decides.

The route list is never written out here. :data:`secretary.web.app.ROUTES` is walked, so a page
route added tomorrow enters these assertions with it and a shell that stopped rendering the bar
fails on every one of them at once.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from secretary.web import pages
from secretary.web.app import ROUTES, WebApp
from secretary.web.provider_usage import CACHE_SECONDS, CLAUDE_USAGE_URL, CODEX_USAGE_URL, ProviderUsageLayer
from secretary.webproto.errors import InstallationUnavailable
from tests.web_fakes import Recording, system_snapshot

#: What a path placeholder is filled with when this test drives the route it belongs to. A route
#: added with a placeholder nobody listed here fails :meth:`RouteFixture.concrete` rather than being
#: quietly skipped: an untested page route is exactly what this suite exists to prevent.
PLACEHOLDERS = {"ref": "secretary-9", "project": "secretary", "session": "s-1"}

NOW = 1_800_000_000.0

#: The moment every countdown in this suite is measured from. It is the render clock, not any
#: reading's `observed_at`, which is the whole of criterion 4.
RENDERED_AT = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

#: Where a percentage would be, if there were one: what a stale or unavailable provider must not show.
PERCENTAGE = re.compile(r"\d+(?:\.\d+)?%")


def available() -> dict[str, Any]:
    return {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-13T12:00:00Z",
    }


def snapshot_with_a_project() -> dict[str, Any]:
    document = system_snapshot()
    document["projects"]["items"] = [
        {
            "id": "secretary",
            "repo": "/srv/secretary",
            "adapter": "kanboard",
            "default_branch": "main",
            "enabled": True,
        }
    ]
    return document


def usage_document(providers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "provider_usage", "observed_at": "2026-09-20T12:00:00Z", "providers": providers}


def provider(
    provider_id: str,
    label: str,
    *,
    status: str = "available",
    reason: str | None = None,
    age_seconds: float | None = 0.0,
    windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": provider_id,
        "label": label,
        "status": status,
        "reason": reason,
        "observed_at": "2026-09-20T12:00:00Z",
        "age_seconds": age_seconds,
        "windows": windows or [],
    }


def window(name: str, remaining: float, resets_at: str) -> dict[str, Any]:
    return {"name": name, "window_minutes": 300, "remaining_percent": remaining, "resets_at": resets_at}


def bar_of(page: str) -> str:
    """The bar out of a rendered page, and the proof there is exactly one."""
    found = re.findall(r'<footer class="statusbar".*?</footer>', page, re.DOTALL)
    if len(found) != 1:
        raise AssertionError(f"a page carries one status bar, this one carries {len(found)}")
    return found[0]


def provider_places(bar: str) -> list[str]:
    """Each provider's place on the bar, as its own markup."""
    return [f'<span class="provider">{part}' for part in bar.split('<span class="provider">')[1:]]


class RouteFixture(unittest.TestCase):
    """The application over recording fakes for every layer, including the PO half."""

    def setUp(self) -> None:
        self.enterContext(pages.render_clock(lambda: RENDERED_AT))
        unreadable = InstallationUnavailable("not part of this test")
        self.reads = Recording(
            system_snapshot=snapshot_with_a_project(),
            task_snapshot={
                "ref": "secretary-9",
                "observed_at": "2026-09-20T12:00:00Z",
                "card": {
                    "source": available(),
                    "value": {"title": "a card", "state": "validate", "project": "secretary"},
                },
                "project": {"id": "secretary", "registered": True},
                "attempt": {},
                "agents": {"source": available(), "items": []},
                "work": {},
                "events": {"source": available(), "items": [], "next_cursor": "c"},
            },
        )
        self.sprint_reads = Recording(
            sprint_list={"kind": "sprint_list", "sprints": {"source": available(), "items": []}},
            sprint_state={
                "ref": "secretary-9",
                "sprint": {
                    "source": available(),
                    "value": {"ref": "sprint:7", "status": "open", "goal": "a goal"},
                },
                "observer": {},
                "work": {},
            },
        )
        self.command_reads = Recording(
            command_history={
                "kind": "command_history",
                "observed_at": "2026-09-20T12:00:00Z",
                "limit": 25,
                "commands": {
                    "source": available(),
                    "items": [],
                    "has_more": False,
                    "next_cursor": None,
                },
            }
        )
        self.po = Recording(
            po_running_count={"kind": "po_running", "running": 0},
            po_overview={"kind": "po_overview", "sessions": [], "models": {}, "running": 0},
            po_session={
                "kind": "po_session",
                "session": {"session_id": "s-1", "state": "idle", "cli": "claude", "model": "opus"},
                "turns": [],
                "feed": [],
                "running": False,
            },
        )
        # The pause refuses, so the dashboard draws that section as a marked block: this suite is
        # about the bar, and a page that also has an unreadable section is the harder case for it.
        self.layers = [
            self.reads,
            Recording(run_list={"items": []}),
            self.sprint_reads,
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            self.command_reads,
            Recording(),
        ]
        self.usage = Recording(
            usage_snapshot=usage_document(
                [
                    provider(
                        "claude",
                        "Claude",
                        windows=[window("5-hour", 74.0, "2026-09-20T18:00:00Z")],
                    ),
                    provider("codex", "Codex", windows=[window("weekly", 91.0, "2026-09-25T00:00:00Z")]),
                ]
            )
        )

    def app(self, *, provider_usage: Any = ..., po: Any = ...) -> WebApp:
        return WebApp(
            *self.layers,
            provider_usage=self.usage if provider_usage is ... else provider_usage,
            po_auth=Recording(po_admits={"admitted": True}),
            po=self.po if po is ... else po,
        )

    def page_routes(self) -> list[Any]:
        """Every route that answers a person with a page on a GET, out of the table itself."""
        return [route for route in ROUTES if route.page and route.method == "GET"]

    def concrete(self, pattern: str) -> str:
        def fill(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in PLACEHOLDERS:
                raise AssertionError(
                    f"the route {pattern} has a placeholder {{{name}}} this suite does not know how "
                    "to drive; add it to PLACEHOLDERS so its page is walked like every other"
                )
            return PLACEHOLDERS[name]

        return re.sub(r"\{([a-z_]+)\}", fill, pattern)

    def get(self, path: str, *, app: WebApp | None = None) -> str:
        response = (app or self.app()).handle("GET", path)
        self.assertEqual(response.status, 200, f"{path} answered {response.status}")
        return response.body.decode("utf-8")


# -- criterion 1: the bar is the shell's, so every page route has it -----------------------------


class EveryPageCarriesTheBarTests(RouteFixture):
    def test_the_walked_table_really_covers_the_pages_this_card_names(self) -> None:
        """The walk is the assertion; this pins that the walk is not empty or narrowed."""
        walked = {self.concrete(route.pattern) for route in self.page_routes()}
        self.assertEqual(
            walked,
            {
                "/",
                "/tasks/secretary-9",
                "/sprints",
                "/sprints/new",
                "/sprints/secretary-9",
                "/projects",
                "/projects/secretary",
                "/history",
                "/po",
                "/po/sessions/s-1",
            },
        )

    def test_every_get_page_route_renders_one_status_bar_with_both_providers(self) -> None:
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path))
                self.assertIn("<b>Claude</b>", bar)
                self.assertIn("<b>Codex</b>", bar)
                self.assertIn("74.0%", bar)
                self.assertIn("91.0%", bar)

    def test_a_refusal_page_carries_the_bar_too(self) -> None:
        response = self.app().handle("GET", "/projects/absent")
        self.assertEqual(response.status, 404)
        self.assertIn("<b>Claude</b>", bar_of(response.body.decode("utf-8")))

    def test_a_json_route_carries_no_bar_and_costs_no_provider_read(self) -> None:
        response = self.app().handle("GET", "/api/system")
        self.assertNotIn("statusbar", response.body.decode("utf-8"))
        self.assertEqual(self.usage.calls, [])


# -- criterion 2: it is fixed to the bottom and the space under the page is reserved -------------


class BarLayoutTests(RouteFixture):
    def test_the_bar_is_the_only_fixed_element_and_the_page_reserves_its_height(self) -> None:
        fixed = set(re.findall(r"^(\S+) \{[^}]*position: fixed", pages.STYLE, re.MULTILINE))
        self.assertEqual(fixed, {".statusbar"}, "only the bar is lifted out of the page's flow")
        self.assertIn(".statusbar { position: fixed; left: 0; right: 0; bottom: 0;", pages.STYLE)
        self.assertIn("--bar-height:", pages.STYLE)
        self.assertIn("body { padding-bottom: var(--bar-height); }", pages.STYLE)
        self.assertIn("height: var(--bar-height)", pages.STYLE)

    def test_the_bar_keeps_one_line_at_any_width_so_the_reserved_height_stays_true(self) -> None:
        row = re.search(r"\.statusbar \.row \{([^}]*)\}", pages.STYLE)
        assert row is not None
        self.assertIn("white-space: nowrap", row.group(1))
        self.assertIn("overflow-x: auto", row.group(1))
        # The phone-width rule narrows the gutters; it must not reintroduce wrapping or a height.
        phone = re.search(r"@media \(max-width: 600px\) \{ \.statusbar[^}]*\}", pages.STYLE)
        assert phone is not None
        self.assertNotIn("white-space", phone.group(0))

    def test_the_po_composer_sits_above_the_bar_rather_than_under_it(self) -> None:
        page = self.get("/po/sessions/s-1")
        self.assertIn('<textarea id="po-text" name="text" required>', page)
        # The composer is inside <main>, which the reserved padding sits below; the bar follows it.
        self.assertLess(page.index('id="po-text"'), page.index("</main>"))
        self.assertLess(page.index("</main>"), page.index('<footer class="statusbar"'))
        self.assertNotIn("position: fixed", re.search(r"form\.sprint \{[^}]*\}", pages.STYLE).group(0))


# -- criterion 3 and 5: a reading, a reading that is not current, and no reading at all ----------


class BarReadingTests(RouteFixture):
    def bar(self, section: dict[str, Any] | None) -> str:
        return bar_of(f'<main></main>{pages._limits_bar_of(section)}')

    def section(self, providers: list[dict[str, Any]]) -> dict[str, Any]:
        return {"available": True, "reason": None, "document": usage_document(providers)}

    def test_an_available_provider_shows_every_window_its_reading_carries(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider(
                        "claude",
                        "Claude",
                        windows=[
                            window("5-hour", 74.0, "2026-09-20T18:00:00Z"),
                            window("weekly", 91.0, "2026-09-25T00:00:00Z"),
                        ],
                    ),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "2026-09-20T15:00:00Z")]),
                ]
            )
        )
        claude, codex = provider_places(bar)[:2]
        self.assertIn("5-hour", claude)
        self.assertIn("74.0%", claude)
        # The reset is the time left until it, and the moment itself is the hover title.
        self.assertIn('<span class="resets" title="2026-09-20T18:00:00Z">6 h 0 m left</span>', claude)
        self.assertIn("weekly", claude)
        self.assertIn("91.0%", claude)
        self.assertIn('<span class="resets" title="2026-09-25T00:00:00Z">4 d 12 h left</span>', claude)
        self.assertIn("41.0%", codex)

    def test_an_available_reading_that_is_not_new_says_how_old_it_is(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider("claude", "Claude", age_seconds=1800.0, windows=[window("5-hour", 74.0, "x")]),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "x")]),
                ]
            )
        )
        claude, codex = provider_places(bar)[:2]
        self.assertIn("30m old", claude)
        self.assertNotIn("old", codex)

    def test_a_stale_provider_shows_its_status_and_reason_and_no_percentage(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider(
                        "claude",
                        "Claude",
                        status="stale",
                        reason="Latest Codex usage observation is stale",
                        age_seconds=3600.0,
                        # A stale reading still carries the last windows. They are not drawn: a
                        # percentage on the bar is read as what is left now, and this one is not.
                        windows=[window("5-hour", 12.0, "x")],
                    ),
                    provider("codex", "Codex", windows=[window("5-hour", 41.0, "x")]),
                ]
            )
        )
        claude = provider_places(bar)[0]
        self.assertIn("stale", claude)
        self.assertIn("Latest Codex usage observation is stale", claude)
        self.assertIn("60m old", claude)
        self.assertIn("no current reading", claude)
        self.assertIsNone(PERCENTAGE.search(claude), claude)
        self.assertNotIn("12.0", claude)

    def test_an_unavailable_provider_shows_its_status_and_reason_and_no_percentage(self) -> None:
        bar = self.bar(
            self.section(
                [
                    provider("claude", "Claude", windows=[window("5-hour", 74.0, "x")]),
                    provider(
                        "codex",
                        "Codex",
                        status="unavailable",
                        reason="Codex login is unavailable",
                        age_seconds=None,
                    ),
                ]
            )
        )
        codex = provider_places(bar)[1]
        self.assertIn("unavailable", codex)
        self.assertIn("Codex login is unavailable", codex)
        self.assertIn("no current reading", codex)
        self.assertIsNone(PERCENTAGE.search(codex), codex)

    def test_a_provider_the_reading_does_not_carry_keeps_its_place_and_says_so(self) -> None:
        bar = self.bar(self.section([provider("claude", "Claude", windows=[window("5-hour", 74.0, "x")])]))
        codex = provider_places(bar)[1]
        self.assertIn("<b>Codex</b>", codex)
        self.assertIn(pages.LIMITS_NOT_IN_READING, codex)
        self.assertIsNone(PERCENTAGE.search(codex), codex)

    def test_a_provider_beyond_the_two_is_shown_rather_than_hidden(self) -> None:
        bar = self.bar(self.section([provider("other", "Other", status="unavailable", reason="no login")]))
        self.assertEqual(len(provider_places(bar)), 3)
        self.assertIn("<b>Other</b>", provider_places(bar)[2])

    def test_a_process_built_without_the_layer_still_serves_every_page(self) -> None:
        app = self.app(provider_usage=None, po=self.po)
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path, app=app))
                self.assertIn(pages.LIMITS_NOT_BUILT, bar)
                self.assertIsNone(PERCENTAGE.search(bar), bar)

    def test_a_provider_read_that_refuses_still_serves_every_page_with_the_reason(self) -> None:
        app = self.app(provider_usage=Recording(usage_snapshot=InstallationUnavailable("no instance here")))
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                bar = bar_of(self.get(path, app=app))
                self.assertIn("no instance here", bar)
                self.assertIsNone(PERCENTAGE.search(bar), bar)


# -- criterion 4: rendering pages adds no provider call beyond the cache's own cadence -----------


class BarCostsNoExtraReadTests(RouteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.write(self.home / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "c"}})
        self.write(self.home / ".codex/auth.json", {"tokens": {"access_token": "x", "account_id": "a"}})
        self.clock = NOW
        self.fetched: list[str] = []
        self.layer = ProviderUsageLayer(
            home=self.home, fetch_json=self.fetch, now=lambda: self.clock, timeout=3.0
        )

    def write(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        self.fetched.append(url)
        if url == CLAUDE_USAGE_URL:
            return {
                "five_hour": {"utilization": 0.26, "resets_at": self.clock + 100},
                "seven_day": {"utilization": 0.09, "resets_at": self.clock + 200},
            }
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 59, "window_minutes": 300, "reset_at": self.clock + 300}
            }
        }

    def test_many_renders_inside_one_cache_window_ask_each_provider_once(self) -> None:
        app = self.app(provider_usage=self.layer)
        paths = [self.concrete(route.pattern) for route in self.page_routes()]
        for _ in range(5):
            for path in paths:
                bar = bar_of(self.get(path, app=app))
                self.assertIn("74.0%", bar)
                self.assertIn("41.0%", bar)
        self.assertEqual(len(paths) * 5, 50, "the walk really did render many pages")
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL])

    def test_the_next_cache_window_asks_once_more_and_not_once_per_page(self) -> None:
        app = self.app(provider_usage=self.layer)
        paths = [self.concrete(route.pattern) for route in self.page_routes()]
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(len(self.fetched), 2)

        self.clock += CACHE_SECONDS - 1
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(len(self.fetched), 2, "still inside the window the layer decided")

        self.clock += 2
        for path in paths:
            self.get(path, app=app)
        self.assertEqual(self.fetched.count(CLAUDE_USAGE_URL), 2)
        self.assertEqual(self.fetched.count(CODEX_USAGE_URL), 2)

    def test_the_dashboard_panel_and_the_bar_are_one_read_rather_than_two(self) -> None:
        app = self.app(provider_usage=self.layer)
        page = self.get("/", app=app)
        self.assertEqual(self.fetched, [CLAUDE_USAGE_URL, CODEX_USAGE_URL])
        # Both presentations of the same document are on the page: the panel and the bar.
        self.assertIn("Usage limits", page)
        self.assertIn("74.0%", bar_of(page))


# -- criterion 6: what keeps the bar current never discards what somebody is typing --------------


class BarRefreshTests(RouteFixture):
    def test_every_page_carries_the_refresh_and_the_bar_carries_its_switch(self) -> None:
        for route in self.page_routes():
            path = self.concrete(route.pattern)
            with self.subTest(path=path):
                page = self.get(path)
                self.assertIn("data-refresh-toggle", bar_of(page))
                self.assertIn(pages._REFRESH_SCRIPT, page)

    def test_the_dashboard_keeps_its_own_switch_and_it_is_the_same_switch(self) -> None:
        page = self.get("/")
        self.assertIn('<input type="checkbox" id="auto-refresh" data-refresh-toggle>', page)
        self.assertEqual(page.count('<input type="checkbox"'), 2, "the two switches, and no third")

    def test_the_reload_is_refused_while_a_form_has_focus_or_holds_typed_text(self) -> None:
        """The rule the /po composer depends on, read out of the one script that reloads a page.

        Nothing here runs JavaScript, so what is pinned is the shape of the guard: both clauses are
        present, both are `return`s, and both stand before the only `reload` in the script.
        """
        script = pages._REFRESH_SCRIPT
        focus = script.index(
            "if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' "
            "|| active.tagName === 'SELECT')) return;"
        )
        typed = script.index("for (const field of document.querySelectorAll(")
        selector = re.search(r"querySelectorAll\('([^']+)'\)\) if \(field\.value\) return;", script)
        assert selector is not None
        self.assertIn("textarea", selector.group(1))
        self.assertIn("input:not([type])", selector.group(1))
        self.assertEqual(script.count("window.location.reload()"), 1)
        reload_at = script.index("window.location.reload()")
        self.assertLess(focus, reload_at)
        self.assertLess(typed, reload_at)
        # And the switch itself can turn the whole thing off.
        self.assertIn("if (!on) return;", script)

    def test_the_po_composer_is_a_field_the_guard_covers(self) -> None:
        """The composer is a textarea, which is what makes the rule above cover a half-written message."""
        page = self.get("/po/sessions/s-1")
        self.assertIn('<textarea id="po-text" name="text" required>', page)
        self.assertIn("for (const field of document.querySelectorAll('textarea,", page)


# -- a reset is how long is left, drawn once for both places -------------------------------------


class ResetRenderingTests(unittest.TestCase):
    """The one renderer, asked directly, at every distance the card names."""

    def left(self, resets_at: Any, *, minutes: float = 0.0) -> str:
        """What the renderer draws for `resets_at`, `minutes` after :data:`RENDERED_AT`."""
        now = RENDERED_AT + timedelta(minutes=minutes)
        with pages.render_clock(lambda: now):
            return pages._reset(resets_at)

    def test_more_than_a_day_away_is_whole_days_and_hours(self) -> None:
        drawn = self.left("2026-09-22T23:30:00Z")
        self.assertIn(">2 d 11 h left<", drawn)

    def test_under_a_day_is_hours_and_minutes(self) -> None:
        self.assertIn(">6 h 45 m left<", self.left("2026-09-20T18:45:00Z"))

    def test_under_an_hour_is_minutes(self) -> None:
        self.assertIn(">42 m left<", self.left("2026-09-20T12:42:00Z"))

    def test_under_a_minute_is_said_as_less_than_a_minute_and_never_as_zero(self) -> None:
        drawn = self.left("2026-09-20T12:00:40Z")
        self.assertIn(">less than a minute left<", drawn)
        self.assertNotIn("0 m left", drawn)

    def test_a_moment_already_past_is_said_as_past_and_never_as_a_negative_or_a_zero(self) -> None:
        drawn = self.left("2026-09-20T11:00:00Z")
        self.assertIn(">reset already passed<", drawn)
        text = drawn.split(">")[1].split("<")[0]
        self.assertNotIn("-", text, "never a negative duration")
        self.assertNotIn("0", text, "and never a zero that could read as live")

    def test_the_exact_moment_at_the_reset_is_past_rather_than_a_live_zero(self) -> None:
        self.assertIn(">reset already passed<", self.left("2026-09-20T12:00:00Z"))

    def test_a_reading_with_no_moment_says_so_and_carries_no_title(self) -> None:
        for absent in (None, "", "   ", "not a moment", 17):
            with self.subTest(absent=absent):
                drawn = self.left(absent)
                self.assertIn(pages.NO_RESET_RECORDED, drawn)
                self.assertNotIn("title=", drawn)

    def test_the_moment_is_kept_as_the_hover_title(self) -> None:
        self.assertIn('title="2026-09-20T18:00:00Z"', self.left("2026-09-20T18:00:00Z"))

    def test_the_countdown_is_measured_against_the_render_clock_and_not_the_reading(self) -> None:
        """Criterion 4: the same reading, rendered later, has less time left -- by exactly the wait.

        A cached reading can be up to `CACHE_SECONDS` old, so counting from its `observed_at` would
        keep repeating the time that was left when it was observed.
        """
        self.assertIn(">6 h 0 m left<", self.left("2026-09-20T18:00:00Z"))
        self.assertIn(">5 h 0 m left<", self.left("2026-09-20T18:00:00Z", minutes=60))

    def test_a_moment_without_an_offset_is_read_as_utc_rather_than_as_local_time(self) -> None:
        self.assertIn(">6 h 0 m left<", self.left("2026-09-20T18:00:00"))


class BothPlacesDrawTheSameResetTests(RouteFixture):
    """Criterion 1: the bar and the dashboard panel are one rule, not two spellings of one."""

    def test_the_bar_and_the_panel_render_the_reset_identically(self) -> None:
        page = self.get("/")
        drawn = '<span class="resets" title="2026-09-20T18:00:00Z">6 h 0 m left</span>'
        self.assertIn(drawn, bar_of(page))
        panel = page[page.index("Usage limits") : page.index('<footer class="statusbar"')]
        self.assertIn(drawn, panel)
        self.assertIn('<span class="resets" title="2026-09-25T00:00:00Z">4 d 12 h left</span>', panel)

    def test_neither_place_prints_an_iso_moment_as_the_text_of_the_reset(self) -> None:
        page = self.get("/")
        self.assertNotIn(">2026-09-20T18:00:00Z<", page)
        self.assertNotIn("resets 2026-09-20T18:00:00Z", page)

    def test_a_panel_window_with_no_moment_says_so_in_the_same_words_as_the_bar(self) -> None:
        self.usage = Recording(
            usage_snapshot=usage_document(
                [provider("claude", "Claude", windows=[{"name": "5-hour", "remaining_percent": 74.0}])]
            )
        )
        page = self.get("/")
        self.assertEqual(page.count(pages.NO_RESET_RECORDED), 2, "once on the bar, once in the panel")


# -- the two findings folded in from secretary-1645's review -------------------------------------


class RefreshGuardCoversTypedSecretsTests(RouteFixture):
    """A password field holds typed content like any other, and the /po token is typed into one."""

    def selector(self) -> str:
        found = re.search(
            r"querySelectorAll\('([^']+)'\)\) if \(field\.value\) return;", pages._REFRESH_SCRIPT
        )
        assert found is not None
        return found.group(1)

    def test_the_typed_content_guard_matches_a_password_field(self) -> None:
        self.assertIn("input[type=password]", self.selector())

    def test_the_po_login_page_carries_a_password_field_the_guard_now_covers(self) -> None:
        app = self.app(po=self.po)
        app.po_auth = Recording(po_admits={"admitted": False})
        response = app.handle("GET", "/po")
        self.assertEqual(response.status, 401)
        page = response.body.decode("utf-8")
        self.assertIn('<input id="po-token" name="token" type="password"', page)
        self.assertIn("input[type=password]", page)


class APostResultDoesNotReloadItselfTests(RouteFixture):
    """A page answering a submission must not tick into the browser's resubmission prompt."""

    def refused_login(self) -> str:
        app = self.app(po=self.po)
        app.po_auth = Recording(po_login={"admitted": False})
        response = app.handle("POST", "/po/login", body=b"token=wrong")
        self.assertEqual(response.status, 401)
        return response.body.decode("utf-8")

    def test_a_page_rendered_from_a_post_carries_no_auto_reload(self) -> None:
        page = self.refused_login()
        self.assertNotIn("window.location.reload()", page)
        self.assertNotIn(pages._REFRESH_SCRIPT, page)

    def test_that_same_page_reached_by_a_get_does_carry_it(self) -> None:
        app = self.app(po=self.po)
        app.po_auth = Recording(po_admits={"admitted": False})
        page = app.handle("GET", "/po").body.decode("utf-8")
        self.assertIn(pages._REFRESH_SCRIPT, page)

    def test_the_post_result_still_carries_the_bar_it_simply_does_not_reload(self) -> None:
        self.assertIn("<b>Claude</b>", bar_of(self.refused_login()))

    def test_the_mark_is_unset_again_once_the_request_is_answered(self) -> None:
        self.refused_login()
        self.assertIn(pages._REFRESH_SCRIPT, self.get("/"))


# -- the shell holds nothing between two requests ------------------------------------------------


class BarSourceIsPerRequestTests(RouteFixture):
    def test_the_source_is_unset_again_once_the_request_is_answered(self) -> None:
        app = self.app()
        self.assertIn("74.0%", bar_of(self.get("/", app=app)))
        # Rendered outside any request, the same shell says it was fed nothing rather than
        # repeating what the last request happened to see.
        self.assertIn(pages.LIMITS_NOT_BUILT, bar_of(pages.error(404, "not_found", "no route here")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
