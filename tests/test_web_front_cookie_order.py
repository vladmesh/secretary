"""Regression coverage for persistent web-front browser sessions."""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest.mock import call, patch

from secretary.webfront import commands
from secretary.webfront.caddyfile import (
    HASH_SECRET_ID,
    PASSWORD_SECRET_ID,
    SESSION_SECRET_ID,
    FrontConfig,
    render,
)

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


class WebFrontSessionSecretTests(unittest.TestCase):
    @patch.object(commands, "read_secret", return_value=SAMPLE_SESSION_SECRET.encode())
    @patch.object(commands, "list_secrets", return_value=[{"id": SESSION_SECRET_ID}])
    @patch.object(commands, "set_secret")
    def test_render_reuses_an_existing_session_secret(self, set_secret, _list, read_secret) -> None:
        value = commands._read_or_create_session_secret(Path("/instance"))
        self.assertEqual(value, SAMPLE_SESSION_SECRET)
        read_secret.assert_called_once_with(Path("/instance"), SESSION_SECRET_ID)
        set_secret.assert_not_called()

    @patch.object(commands.pysecrets, "token_urlsafe", return_value=SAMPLE_SESSION_SECRET)
    @patch.object(commands, "list_secrets", return_value=[])
    @patch.object(commands, "set_secret")
    def test_first_render_migrates_an_old_installation_once(self, set_secret, _list, token) -> None:
        value = commands._read_or_create_session_secret(Path("/instance"))
        self.assertEqual(value, SAMPLE_SESSION_SECRET)
        token.assert_called_once_with(commands.SESSION_SECRET_BYTES)
        set_secret.assert_called_once_with(
            Path("/instance"),
            secret_id=SESSION_SECRET_ID,
            value=SAMPLE_SESSION_SECRET.encode("utf-8"),
            scope="installation",
            purpose="web front browser session signing secret, rotated with owner password",
            actor=commands.DEFAULT_ACTOR,
        )

    @patch.object(commands, "print_json")
    @patch.object(commands, "set_secret")
    @patch.object(commands, "hash_password", return_value=SAMPLE_HASH)
    @patch.object(
        commands.pysecrets,
        "token_urlsafe",
        side_effect=["generated-owner-password", SAMPLE_SESSION_SECRET],
    )
    def test_set_password_rotates_session_before_password_and_hash(
        self, _token, _hash, set_secret, _print
    ) -> None:
        args = argparse.Namespace(
            instance="/instance",
            generate=True,
            caddy="caddy",
            actor="operator",
        )
        self.assertEqual(commands.run_set_password(args), 0)
        ids = [item.kwargs["secret_id"] for item in set_secret.call_args_list]
        self.assertEqual(ids, [SESSION_SECRET_ID, PASSWORD_SECRET_ID, HASH_SECRET_ID])
        self.assertEqual(
            set_secret.call_args_list[0],
            call(
                Path("/instance"),
                secret_id=SESSION_SECRET_ID,
                value=SAMPLE_SESSION_SECRET.encode("utf-8"),
                scope="installation",
                purpose="web front browser session signing secret, rotated with owner password",
                actor="operator",
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
