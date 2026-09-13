"""`/po` over the transport: the token gate, the login cookie, the model list and the dashboard indicator.

The PO layer is a recording fake here, so "reaches no PO layer" is an assertion about its calls; the
token layer is the real one over a token file in a temporary data directory. No database.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import urlencode

from secretary.config import validate
from secretary.po import token as po_token
from secretary.po.models import DEFAULT_MODELS, models_from_instance
from secretary.web.app import PO_OPEN_ROUTES, ROUTES, WebApp, requires_po_token
from secretary.web.server import build_server
from secretary.webproto.errors import InstallationUnavailable, RuntimeUnavailable
from secretary.webproto.po_auth import PoTokenLayer
from tests.web_fakes import Recording, system_snapshot

PO_ROUTES = {
    ("POST", "/po/login"),
    ("GET", "/po"),
    ("POST", "/po/sessions"),
    ("GET", "/po/sessions/{session}"),
    ("POST", "/po/sessions/{session}/messages"),
    ("POST", "/po/sessions/{session}/stop"),
    ("GET", "/po/api/sessions/{session}"),
}
#: A form body carrying every field any /po POST takes; the gate answers before any field is read.
ANY_FORM = urlencode(
    [("request_id", "r"), ("text", "hi"), ("cli", "claude"), ("model", "opus"), ("seq", "1")]
)


def concrete(pattern: str) -> str:
    return pattern.replace("{session}", "s-1")


class PoGateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.tmp / "data"
        self.data.mkdir()
        po_token.ensure_token(self.data)
        self.token = po_token.read_token(self.data)
        # The dashboard's pause and sprint sections refuse, which it draws as marked sections.
        unreadable = InstallationUnavailable("not part of this test")
        self.layers = [
            Recording(system_snapshot=system_snapshot()),
            Recording(),
            Recording(sprint_list=unreadable),
            Recording(),
            Recording(pause_state=unreadable),
            Recording(),
            Recording(),
            Recording(),
        ]
        self.po = Recording(po_running_count={"kind": "po_running", "running": 0})
        self.web = WebApp(*self.layers, po_auth=PoTokenLayer(self.tmp, data_dir=self.data), po=self.po)

    def request(self, method: str, path: str, *, body: bytes = b"", cookie: str | None = None, headers=None):
        sent = dict(headers or {})
        if cookie is not None:
            sent["Cookie"] = f"{po_token.COOKIE_NAME}={cookie}"
        return self.web.handle(method, path, body=body, headers=sent)

    def login(self, token: str, headers=None):
        return self.request("POST", "/po/login", body=urlencode([("token", token)]).encode(), headers=headers)

    def po_calls(self) -> list[str]:
        return [name for name, _ in self.po.calls]


class PoTokenGateTests(PoGateFixture):
    def test_the_route_table_lists_the_po_routes_and_all_but_the_login_are_under_the_token(self) -> None:
        under_po = {
            (route.method, route.pattern)
            for route in ROUTES
            if route.pattern == "/po" or route.pattern.startswith("/po/")
        }
        self.assertEqual(under_po, PO_ROUTES)
        self.assertEqual(PO_OPEN_ROUTES, {("POST", "/po/login")})
        guarded = {(route.method, route.pattern) for route in ROUTES if requires_po_token(route)}
        self.assertEqual(guarded, PO_ROUTES - PO_OPEN_ROUTES)

    def test_every_po_route_without_a_valid_cookie_is_refused_and_reaches_no_po_layer(self) -> None:
        wrong = po_token.cookie_value(self.token + "-old")
        for route in ROUTES:
            if not requires_po_token(route):
                continue
            for cookie in (None, "", "garbage", self.token, wrong):
                with self.subTest(route=route.pattern, method=route.method, cookie=cookie):
                    body = ANY_FORM.encode() if route.method == "POST" else b""
                    response = self.request(route.method, concrete(route.pattern), body=body, cookie=cookie)
                    self.assertEqual(response.status, 401)
                    self.assertNotIn("Set-Cookie", response.headers)
                    if route.page:
                        self.assertIn('action="/po/login"', response.body.decode())
                    else:
                        self.assertEqual(json.loads(response.body)["error"]["code"], "po_token_required")
        self.assertEqual(self.po.calls, [])

    def test_a_wrong_token_is_refused_and_sets_no_cookie(self) -> None:
        for token in ("", "nope", self.token + "x", self.token[:-1]):
            with self.subTest(token=token):
                response = self.login(token)
                self.assertEqual(response.status, 401)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn("not this installation&#x27;s PO token", response.body.decode())
        self.assertEqual(self.po.calls, [])

    def test_the_right_token_sets_a_derived_http_only_strict_cookie_on_path_po(self) -> None:
        response = self.login(self.token)

        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/po")
        cookie = response.headers["Set-Cookie"]
        first, *attributes = [part.strip() for part in cookie.split(";")]
        name, value = first.split("=", 1)
        self.assertEqual(name, po_token.COOKIE_NAME)
        self.assertNotIn(self.token, cookie)
        self.assertEqual(value, po_token.cookie_value(self.token))
        self.assertIn("HttpOnly", attributes)
        self.assertIn("SameSite=Strict", attributes)
        self.assertIn("Path=/po", attributes)
        self.assertNotIn("Secure", attributes)

        page = self.request("GET", "/po", cookie=value)
        self.assertEqual(page.status, 200)
        self.assertEqual(self.po_calls(), ["po_overview"])

    def test_the_cookie_is_secure_when_the_request_came_through_the_tls_front(self) -> None:
        response = self.login(self.token, headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status, 303)
        self.assertIn("Secure", [part.strip() for part in response.headers["Set-Cookie"].split(";")])

    def test_a_replaced_token_file_invalidates_the_old_cookie(self) -> None:
        old = po_token.cookie_value(self.token)
        self.assertEqual(self.request("GET", "/po", cookie=old).status, 200)

        po_token.token_path(self.data).unlink()
        self.assertTrue(po_token.ensure_token(self.data))

        self.assertEqual(self.request("GET", "/po", cookie=old).status, 401)
        self.assertEqual(self.login(self.token).status, 401)
        fresh = self.login(po_token.read_token(self.data))
        self.assertEqual(fresh.status, 303)
        value = fresh.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(self.request("GET", "/po", cookie=value).status, 200)

    def test_a_missing_token_file_refuses_the_login_and_every_route_without_reaching_the_po_layer(
        self,
    ) -> None:
        cookie = po_token.cookie_value(self.token)
        po_token.token_path(self.data).unlink()

        self.assertEqual(self.login(self.token).status, 503)
        self.assertEqual(self.request("GET", "/po", cookie=cookie).status, 503)
        self.assertEqual(self.request("GET", "/po/api/sessions/s-1", cookie=cookie).status, 503)
        self.assertEqual(self.po.calls, [])

    def test_a_login_posted_from_another_origin_is_refused(self) -> None:
        response = self.login(
            self.token, headers={"Origin": "https://attacker.example", "Host": "front.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_a_process_built_without_the_po_layers_does_not_serve_po(self) -> None:
        response = WebApp(*self.layers).handle("GET", "/po")
        self.assertEqual(response.status, 503)

    def test_the_gate_sees_the_cookie_a_real_request_arrives_with(self) -> None:
        server = build_server(self.web, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=10)
        self.addCleanup(connection.close)

        connection.request("GET", "/po")
        refused = connection.getresponse()
        refused.read()
        self.assertEqual(refused.status, 401)

        connection.request(
            "POST",
            "/po/login",
            body=urlencode([("token", self.token)]),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        admitted = connection.getresponse()
        admitted.read()
        self.assertEqual(admitted.status, 303)
        cookie = admitted.getheader("Set-Cookie").split(";")[0]

        connection.request("GET", "/po", headers={"Cookie": f"other=1; {cookie}"})
        page = connection.getresponse()
        page.read()
        self.assertEqual(page.status, 200)
        self.assertEqual(self.po_calls(), ["po_overview"])


class PoIndicatorTests(PoGateFixture):
    def test_the_dashboard_counts_running_po_turns_and_links_to_po_without_a_token(self) -> None:
        self.po.answers["po_running_count"] = {"kind": "po_running", "running": 2}

        response = self.request("GET", "/")

        self.assertEqual(response.status, 200)
        page = response.body.decode()
        self.assertIn('<a href="/po" id="po-indicator">2 PO turns running</a>', page)
        self.assertEqual(self.po_calls(), ["po_running_count"])

    def test_a_po_store_that_does_not_answer_leaves_the_dashboard_standing(self) -> None:
        self.po.answers["po_running_count"] = RuntimeUnavailable("the PO session store is not available")

        response = self.request("GET", "/")

        self.assertEqual(response.status, 200)
        page = response.body.decode()
        self.assertNotIn("po-indicator", page)
        self.assertIn("running turns could not be counted", page)
        self.assertIn("<h1>Dashboard</h1>", page)


class PoModelListTests(unittest.TestCase):
    INSTANCE: ClassVar[dict] = {
        "version": 1,
        "name": "n",
        "data_dir": "data",
        "offsite": {"instance_remote": "git@x:y.git"},
    }

    def test_without_a_po_section_the_product_default_applies(self) -> None:
        self.assertEqual(models_from_instance(self.INSTANCE), DEFAULT_MODELS)
        self.assertEqual(models_from_instance({**self.INSTANCE, "po": {}}), DEFAULT_MODELS)

    def test_a_configured_list_replaces_one_cli_and_an_empty_one_offers_nothing(self) -> None:
        models = models_from_instance({**self.INSTANCE, "po": {"models": {"claude": ["opus"], "codex": []}}})
        self.assertEqual(models, {"claude": ("opus",), "codex": ()})
        only_claude = models_from_instance({**self.INSTANCE, "po": {"models": {"claude": ["sonnet"]}}})
        self.assertEqual(only_claude["codex"], DEFAULT_MODELS["codex"])

    def test_the_schema_takes_the_section_and_refuses_another_cli(self) -> None:
        good = {**self.INSTANCE, "po": {"models": {"claude": ["opus"], "codex": ["gpt-5.6-sol"]}}}
        self.assertEqual(validate(good, "instance", "instance.yaml"), [])
        for bad in (
            {"models": {"gemini": ["x"]}},
            {"models": {"claude": [""]}},
            {"models": {"claude": "opus"}},
            {"other": True},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(validate({**self.INSTANCE, "po": bad}, "instance", "instance.yaml"))


if __name__ == "__main__":
    unittest.main()
