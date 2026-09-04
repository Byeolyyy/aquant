from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from quant_agent_harness.models import HarnessEvent
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository
from quant_agent_harness.web import WebApp, WebHandler


RAW = """生成时间: 2026-08-20 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""

WEB_ENV = {
    "QUANT_AGENT_ACCESS_PASSWORD": "open-sesame",
    "QUANT_AGENT_SECRET_BACKEND": "env",
    "QUANT_AGENT_COOKIE_SECURE": "0",
}


def build_app(temp_dir: str, **extra_env) -> WebApp:
    env = {**WEB_ENV, **extra_env}
    with mock.patch.dict("os.environ", env, clear=False):
        return WebApp(Repository(Path(temp_dir) / "test.sqlite"))


def rpc(method: str, payload: dict | None = None) -> dict:
    return {
        "type": "request",
        "protocol_version": 1,
        "request_id": "r1",
        "method": method,
        "payload": payload or {},
    }


class LoginTests(unittest.TestCase):
    def test_wrong_password_is_rejected_and_right_one_issues_a_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            self.assertIsNone(app.login("wrong", "1.1.1.1"))
            token = app.login("open-sesame", "1.1.1.1")
            self.assertTrue(token)
            self.assertTrue(app.sessions.validate(token or ""))

    def test_repeated_failures_are_throttled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            for _ in range(5):
                app.login("wrong", "9.9.9.9")
            with self.assertRaises(PermissionError):
                app.login("open-sesame", "9.9.9.9")
            # 另一个来源不受影响
            self.assertTrue(app.login("open-sesame", "8.8.8.8"))

    def test_dropped_session_stops_validating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            token = app.login("open-sesame", "1.1.1.1") or ""
            app.sessions.drop(token)
            self.assertFalse(app.sessions.validate(token))

    def test_missing_password_env_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict("os.environ", {"QUANT_AGENT_ACCESS_PASSWORD": ""}, clear=False):
                with self.assertRaises(SystemExit):
                    WebApp(Repository(Path(temp_dir) / "test.sqlite"))


class MethodPolicyTests(unittest.TestCase):
    def test_settings_and_prompt_writes_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            for method in (
                "save_settings",
                "save_agent_config",
                "create_prompt_draft",
                "publish_prompt_version",
                "rollback_prompt_version",
            ):
                with self.assertRaises(PermissionError, msg=method):
                    app.dispatch(rpc(method), "session-a")

    def test_read_only_methods_still_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            for method in ("ping", "get_settings", "get_agents", "get_workflows", "list_runs"):
                response = app.dispatch(rpc(method), "session-a")
                self.assertTrue(response["ok"], f"{method}: {response.get('error')}")

    def test_settings_report_the_server_side_backend_and_hide_the_database_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            storage = app.dispatch(rpc("get_settings"), "session-a")["result"]["settings"]["storage"]
            self.assertFalse(storage["writable"])
            self.assertEqual(storage["database"], "")
            self.assertIn("环境变量", storage["secret_backend"])


class QuotaTests(unittest.TestCase):
    def test_daily_limit_is_enforced_per_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir, QUANT_AGENT_DAILY_RUN_LIMIT="2")
            self.assertEqual(app.quota.limit, 2)
            app.quota.check_and_consume("session-a")
            app.quota.check_and_consume("session-a")
            with self.assertRaises(ValueError):
                app.quota.check_and_consume("session-a")
            # 另一个会话有自己的额度
            app.quota.check_and_consume("session-b")

    def test_concurrency_cap_refunds_the_quota_it_did_not_use(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir, QUANT_AGENT_MAX_CONCURRENT_RUNS="0")
            with self.assertRaises(ValueError):
                app.dispatch(rpc("start_run", {"report_id": "whatever"}), "session-a")
            # 并发被拦下时这次调用没有真的发生，额度要还回去
            app.quota.check_and_consume("session-a")


class EventIsolationTests(unittest.TestCase):
    def _event(self, run_id: str, seq: int = 1) -> HarnessEvent:
        return HarnessEvent(seq=seq, run_id=run_id, kind="agent.message", payload={"content": "机密"})

    def test_events_only_reach_the_session_that_owns_the_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            app.broker.register_run("run-a", "session-a")
            channel_a = app.broker.subscribe("session-a")
            channel_b = app.broker.subscribe("session-b")
            assert channel_a is not None and channel_b is not None

            app.broker.publish(self._event("run-a"))

            self.assertEqual(channel_a.get_nowait()["run_id"], "run-a")
            self.assertTrue(channel_b.empty())

    def test_events_for_unowned_runs_are_not_broadcast(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            channel = app.broker.subscribe("session-a")
            assert channel is not None
            app.broker.publish(self._event("orphan-run"))
            self.assertTrue(channel.empty())

    def test_reconnecting_replays_recent_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            app.broker.register_run("run-a", "session-a")
            first = app.broker.subscribe("session-a")
            assert first is not None
            app.broker.publish(self._event("run-a", seq=1))
            app.broker.unsubscribe("session-a", first)

            reconnected = app.broker.subscribe("session-a")
            assert reconnected is not None
            self.assertEqual(reconnected.get_nowait()["seq"], 1)

    def test_stream_count_per_session_is_capped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            channels = [app.broker.subscribe("session-a") for _ in range(3)]
            self.assertTrue(all(item is not None for item in channels))
            self.assertIsNone(app.broker.subscribe("session-a"))


class RunOwnershipTests(unittest.TestCase):
    def test_another_session_cannot_read_or_control_the_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            report = parse_ptrade_report(RAW)
            app.repository.save_report(report)
            app.repository.create_run("run-a", report.report_id, owner_session="session-a")

            owned = app.dispatch(rpc("get_run_snapshot", {"run_id": "run-a"}), "session-a")
            self.assertTrue(owned["ok"])

            for method in ("get_run_snapshot", "pause_run", "cancel_run", "steer_run"):
                response = app.dispatch(rpc(method, {"run_id": "run-a"}), "session-b")
                self.assertFalse(response["ok"], method)
                self.assertIn("RunAccessDenied", response["error"], method)

    def test_run_history_is_scoped_to_the_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            report = parse_ptrade_report(RAW)
            app.repository.save_report(report)
            app.repository.create_run("run-a", report.report_id, owner_session="session-a")

            mine = app.dispatch(rpc("list_runs"), "session-a")["result"]["runs"]
            theirs = app.dispatch(rpc("list_runs"), "session-b")["result"]["runs"]
            self.assertEqual([item["run_id"] for item in mine], ["run-a"])
            self.assertEqual(theirs, [])

    def test_mail_reports_are_shared_but_pasted_ones_are_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = build_app(temp_dir)
            shared = parse_ptrade_report(RAW).model_copy(update={"source": "mail"})
            app.repository.save_report(shared)  # 邮件来的报告归属为空 = 共享
            app.dispatch(rpc("parse_report", {"raw_text": RAW + "\n"}), "session-a")

            for_a = app.dispatch(rpc("list_reports"), "session-a")["result"]["reports"]
            for_b = app.dispatch(rpc("list_reports"), "session-b")["result"]["reports"]
            self.assertEqual(len(for_a), 2)
            self.assertEqual([item["source"] for item in for_b], ["mail"])


class HttpEndpointTests(unittest.TestCase):
    """真的起一个 HTTP 服务，验证 cookie、状态码与鉴权拦截。"""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.app = build_app(self._temp.name)
        handler = type("BoundHandler", (WebHandler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._temp.cleanup()

    def _post(self, path: str, body: dict, cookie: str = ""):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_healthz_needs_no_login(self):
        with urllib.request.urlopen(self.base + "/api/healthz", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["ok"])

    def test_rpc_without_a_session_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/rpc", rpc("ping"))
        self.assertEqual(caught.exception.code, 401)

    def test_login_then_rpc_round_trip(self):
        with self._post("/api/login", {"password": "open-sesame"}) as response:
            self.assertEqual(response.status, 200)
            cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

        token = cookie.split(";", 1)[0]
        with self._post("/api/rpc", rpc("ping"), cookie=token) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["service"], "quant-agent-harness")

    def test_bad_password_returns_401(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/login", {"password": "nope"})
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
