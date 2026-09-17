"""Regression coverage for the Caddy handler order that mints the persistent front cookie."""

from __future__ import annotations

import unittest

from secretary.webfront.caddyfile import FrontConfig, render

SAMPLE_HASH = "$2a$14$" + "x" * 53
SAMPLE_SESSION_SECRET = "fixture-session-secret-" + "x" * 32


class WebFrontCookieOrderTests(unittest.TestCase):
    def test_basic_auth_runs_before_cookie_minting_and_proxy(self) -> None:
        text = render(
            FrontConfig(
                sites=("https://front.example",),
                password_hash=SAMPLE_HASH,
                session_secret=SAMPLE_SESSION_SECRET,
            )
        )
        fallback = text.split("\thandle {\n", 1)[1]

        # Caddy normally sorts `header` before `basicauth`; only a route block preserves the
        # literal order below. Without it an unauthenticated 401 could mint the bearer it protects.
        self.assertIn("\t\troute {\n", fallback)
        auth = fallback.index("\t\t\tbasicauth {")
        cookie = fallback.index("\t\t\theader +Set-Cookie")
        proxy = fallback.index("\t\t\treverse_proxy")
        self.assertLess(auth, cookie)
        self.assertLess(cookie, proxy)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
