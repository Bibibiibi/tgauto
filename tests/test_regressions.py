"""Offline regressions. All persistence is redirected to a temporary directory."""
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx


class Regressions(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        paths = ["TG_CONFIG", "TG_SESSION", "TG_SETTINGS", "TG_SCHEDULE_STATE",
                 "TG_CHECKIN_HISTORY", "TG_WEBSITE_SETTINGS", "TG_WEBSITE_HISTORY",
                 "TG_WEBSITE_SCHEDULE_STATE", "TG_PROXY_SETTINGS", "MIHOMO_CONFIG_PATH"]
        with patch.dict(os.environ, {key: str(Path(cls.temp.name) / key) for key in paths}):
            cls.server = importlib.import_module("web_server")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.s = self.server
        self.s.run_lock = asyncio.Lock()
        self.s.website_run_lock = asyncio.Lock()
        self.s.auth_future = None
        self.s.website_settings = {"enabled": True, "schedule_enabled": True,
                                   "cookie": "fake-cookie", "reward_mode": "fixed",
                                   "checkin_url": self.s.NODESEEK_CHECKIN_URL}

    def test_save_failure_preserves_memory(self):
        previous = self.s.website_settings.copy()
        with patch.object(self.s.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.s.write_website_settings({"reward_mode": "random"})
        self.assertEqual(self.s.website_settings, previous)

    def test_reward_mode_persists_and_cookie_remains_private(self):
        self.s.write_website_settings({"reward_mode": "random"})
        saved = json.loads(self.s.WEBSITE_SETTINGS_PATH.read_text())
        self.assertEqual(saved["reward_mode"], "random")
        self.assertEqual(saved["cookie"], "fake-cookie")
        self.assertTrue(saved["checkin_url"].endswith("random=true"))
        self.assertEqual(self.s.WEBSITE_SETTINGS_PATH.stat().st_mode & 0o777, 0o600)

    def test_invalid_cookie_rejected(self):
        with self.assertRaises(RuntimeError):
            self.s.write_website_settings({"cookie": {"bad": "value"}})

    def test_malformed_saved_mode_does_not_crash(self):
        with patch.object(Path, "read_text", return_value='{"reward_mode": []}'):
            self.assertEqual(self.s.load_website_settings()["reward_mode"], "fixed")

    async def test_already_signed_in_with_http_500(self):
        response = {"status_code": 500, "already": True, "success": True,
                    "message": "今天已完成签到，请勿重复操作", "body": "test"}
        with patch.object(self.s, "perform_website_request", return_value=response):
            await self.s.execute_website_checkin()
        self.assertEqual(self.s.website_history["status"], "success")
        response.update(already=False, success=False, message="server error")
        with patch.object(self.s, "perform_website_request", return_value=response):
            await self.s.execute_website_checkin()
        self.assertEqual(self.s.website_history["status"], "failed")

    async def test_auth_timeout_cleans_up(self):
        with patch.object(self.s, "AUTH_INPUT_TIMEOUT", 0.001):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                await self.s.wait_for_auth_input("code", "test")
        self.assertIsNone(self.s.auth_future)
        self.assertIsNone(self.s.state["auth_required"])

    async def test_auth_cancel_cleans_up(self):
        task = asyncio.create_task(self.s.wait_for_auth_input("code", "test"))
        await asyncio.sleep(0)
        await self.s.cancel_auth()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            await task
        self.assertIsNone(self.s.auth_future)

    async def test_busy_slot_retries_without_consuming_day(self):
        now = datetime(2026, 9, 10, 10, 0, tzinfo=self.s.SCHEDULE_TIMEZONE)
        with patch.object(self.s, "schedule_now", return_value=now), \
             patch.object(self.s.asyncio, "sleep") as sleep:
            busy = iter([True, False])
            self.assertTrue(await self.s.wait_for_schedule_slot(
                now, self.s.WEBSITE_SCHEDULE_WINDOW_END, lambda: next(busy)))
            sleep.assert_awaited_once_with(30)

    async def test_expired_or_disabled_slot_never_runs(self):
        target = datetime(2026, 9, 10, 10, 0, tzinfo=self.s.SCHEDULE_TIMEZONE)
        with patch.object(self.s, "schedule_now", return_value=target + timedelta(hours=3)):
            self.assertFalse(await self.s.wait_for_schedule_slot(
                target, self.s.WEBSITE_SCHEDULE_WINDOW_END, lambda: False))
        with patch.object(self.s, "schedule_now", return_value=target):
            self.assertFalse(await self.s.wait_for_schedule_slot(
                target, self.s.WEBSITE_SCHEDULE_WINDOW_END, lambda: False, lambda: False))

    async def test_queued_job_does_not_consume_day_before_worker_entry(self):
        schedule = {}
        response = {"status_code": 200, "already": False, "success": True,
                    "message": "ok", "body": "test"}
        with patch.object(self.s, "website_schedule_state", schedule), \
             patch.object(self.s, "perform_website_request", return_value=response), \
             patch.object(self.s, "write_website_schedule_state") as persist:
            self.s.launch_website_run("scheduled")
            self.assertNotIn("last_run_date", schedule)
            await self.s.website_run_task_handle
            self.assertEqual(schedule["last_run_date"], self.s.schedule_now().date().isoformat())
            persist.assert_called_once()

    async def test_failed_worker_does_not_stop_scheduler_waiter(self):
        async def fail():
            raise OSError("test failure")
        with patch.object(self.s.asyncio, "sleep") as sleep, \
             patch.object(self.s.LOGGER, "exception"):
            await self.s.await_scheduled_run(asyncio.create_task(fail()))
            sleep.assert_awaited_once_with(30)

    def test_existing_future_schedule_and_time_windows_are_preserved(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=self.s.SCHEDULE_TIMEZONE)
        target = now.replace(hour=11, minute=24)
        schedule = {"scheduled_at": target.isoformat(), "last_scheduled_time": "11:24:00"}
        actual, changed = self.s.prepare_schedule_target(
            schedule, now, self.s.WEBSITE_SCHEDULE_WINDOW_START, self.s.WEBSITE_SCHEDULE_WINDOW_END)
        self.assertEqual(actual, target)
        self.assertFalse(changed)
        self.assertEqual(self.s.WEBSITE_SCHEDULE_WINDOW_START.isoformat(), "10:00:00")
        self.assertEqual(self.s.WEBSITE_SCHEDULE_WINDOW_END.isoformat(), "13:00:00")
        self.assertEqual(self.s.TG_SCHEDULE_WINDOW_START.isoformat(), "00:00:00")
        self.assertEqual(self.s.TG_SCHEDULE_WINDOW_END.isoformat(), "00:10:00")

    def test_schedule_write_failure_rolls_back(self):
        schedule = {"last_run_date": "2026-09-09"}
        def fail():
            raise OSError("disk full")
        with self.assertRaises(OSError):
            self.s.record_schedule_attempt(schedule, fail)
        self.assertEqual(schedule, {"last_run_date": "2026-09-09"})

    async def test_management_auth_protects_read_and_write(self):
        transport = httpx.ASGITransport(app=self.s.app)
        with patch.object(self.s, "WEB_ADMIN_PASSWORD", "test-password"), \
             patch.object(self.s, "WEB_ADMIN_USER", "admin"):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for method, path in [("GET", "/"), ("GET", "/api/status"), ("POST", "/api/run")]:
                    self.assertEqual((await client.request(method, path)).status_code, 401)
                self.assertEqual((await client.get("/healthz")).status_code, 200)
                token = base64.b64encode(b"admin:test-password").decode()
                self.assertEqual((await client.get("/api/status", headers={"Authorization": "Basic " + token})).status_code, 200)
                self.assertEqual((await client.get("/api/status", headers={"Authorization": "Basic invalid"})).status_code, 401)

    async def test_manual_proxy_delay_uses_fixed_healthcheck(self):
        transport = httpx.ASGITransport(app=self.s.app)
        with patch.object(self.s, "MIHOMO_HEALTHCHECK_URL", "https://telegram.org"), \
             patch.object(self.s, "mihomo_call", return_value={"delay": 86}) as call:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/proxy/delay", json={"name": "香港 节点/1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "name": "香港 节点/1", "delay": 86})
        method, path = call.await_args.args[:2]
        self.assertEqual(method, "GET")
        self.assertIn(
            "providers/proxies/tg-subscription/"
            "%E9%A6%99%E6%B8%AF%20%E8%8A%82%E7%82%B9%2F1/healthcheck",
            path,
        )
        self.assertIn("timeout=10000", path)
        self.assertIn("url=https%3A%2F%2Ftelegram.org", path)
        self.assertEqual(call.await_args.kwargs["timeout"], 12.0)

    async def test_manual_proxy_delay_rejects_invalid_or_failed_result(self):
        transport = httpx.ASGITransport(app=self.s.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            self.assertEqual((await client.post("/api/proxy/delay", json={"name": "DIRECT"})).status_code, 422)
            with patch.object(self.s, "mihomo_call", return_value={}):
                response = await client.post("/api/proxy/delay", json={"name": "offline-node"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("节点测速失败", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
