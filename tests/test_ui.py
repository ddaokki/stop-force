"""Critical Streamlit approval paths, tested without API access."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


class UITests(unittest.TestCase):
    def test_restarting_pending_run_changes_approval_buttons_and_rejects_old_id(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
            app.button[0].click().run()
            previous = app.session_state["agent"]
            old_id = previous.pending["id"]
            old_executed = list(previous.cluster.executed)
            old_keys = {b.key for b in app.button if b.label in {"✅ 승인하고 실행", "⛔ 거부", "🛑 실행 중단"}}
            self.assertEqual(old_keys, {f"approve_{old_id}", f"deny_{old_id}", f"cancel_{old_id}"})
            app.selectbox[0].set_value("bad_deploy").run()
            next(b for b in app.button if b.label == "🚨 에이전트 출동").click().run()
            self.assertFalse(app.exception)
            current = app.session_state["agent"]
            self.assertIsNot(current, previous)
            self.assertEqual(previous.report["termination"], "cancelled")
            self.assertEqual(previous.cluster.executed, old_executed)
            self.assertIsNone(previous.pending)
            new_id = current.pending["id"]
            self.assertNotEqual(old_id, new_id)
            self.assertFalse(old_keys & {b.key for b in app.button})
            self.assertFalse(current.resolve(True, request_id=old_id))
            self.assertEqual(current.cluster.executed, [])
            self.assertEqual(current.pending["id"], new_id)
            next(b for b in app.button if b.key == f"approve_{new_id}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(current.report["termination"], "finish")
            self.assertEqual(current.report["recovery_status"], "recovered")
            self.assertEqual(current.report["run_id"], current.run_id)
            self.assertEqual(current.report["approvals"][0]["id"], new_id)
            self.assertEqual(len(app.get("download_button")), 2)

    def test_policy_setup_failure_does_not_crash_or_expose_details(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}), \
                patch("stopforce.policy.Policy", side_effect=ValueError("private-policy-fixture")) as policy:
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
            app.button[0].click().run()
        self.assertFalse(app.exception)
        policy.assert_called_once()
        self.assertTrue(any("실행 설정" in e.value for e in app.error))
        self.assertFalse(any("private-policy-fixture" in e.value for e in app.error))
        self.assertNotIn("agent", app.session_state)

    def test_unreadable_policy_preview_does_not_crash_page(self):
        original_read = Path.read_text

        for error in (FileNotFoundError("private-path-fixture"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-path-fixture")):
            def read(path, *args, **kwargs):
                if path.name == "policy.yaml":
                    raise error
                return original_read(path, *args, **kwargs)

            with self.subTest(error=type(error).__name__), patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}), \
                    patch("pathlib.Path.read_text", read):
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
                self.assertFalse(app.exception)
                self.assertTrue(any("정책 파일" in e.value for e in app.warning))
                app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertNotIn("agent", app.session_state)
            self.assertFalse(any("private-path-fixture" in e.value for e in app.error))

    def test_failed_restart_preserves_pending_run_without_exception_details(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": "fixture-key"}), \
                patch("stopforce.llm.NvidiaLLM", side_effect=ValueError("private-provider-fixture")) as model:
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
            app.radio[0].set_value("데모 모드 (사전 작성 시나리오)").run()
            app.button[0].click().run()
            previous = app.session_state["agent"]
            request_id = previous.pending["id"]
            app.radio[0].set_value("NVIDIA Nemotron (실제 LLM)").run()
            next(b for b in app.button if b.label == "🚨 에이전트 출동").click().run()
            self.assertFalse(app.exception)
            self.assertIs(app.session_state["agent"], previous)
            self.assertEqual(previous.pending["id"], request_id)
            self.assertFalse(previous.done)
            self.assertTrue(any("실행 설정" in e.value for e in app.error))
            self.assertFalse(any("private-provider-fixture" in e.value for e in app.error))
            next(b for b in app.button if b.label == "⛔ 거부").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(previous.report["termination"], "finish")
            self.assertEqual(previous.report["recovery_status"], "mitigated")
            self.assertEqual(len(app.get("download_button")), 2)
        model.assert_called_once()

    def test_invalid_demo_delay_rejected_before_agent_creation(self):
        for value in ("NaN", "Infinity", "-0.1", "1.1", "private-delay-fixture"):
            with self.subTest(value=value), patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": value, "NVIDIA_API_KEY": ""}), \
                    patch("stopforce.agent.Agent") as constructor:
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
                app.button[0].click().run()
                self.assertFalse(app.exception)
                constructor.assert_not_called()
                self.assertNotIn("agent", app.session_state)
                self.assertTrue(any("실행 설정" in e.value for e in app.error))
                self.assertFalse(any("private-delay-fixture" in e.value for e in app.error))

    def test_scenario_approval_matrix(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}):
            for scenario in ("db_leak", "bad_deploy", "disk_full"):
                for mode in ("approve", "deny", "off"):
                    with self.subTest(scenario=scenario, mode=mode):
                        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
                        self.assertFalse(app.exception)
                        app.selectbox[0].set_value(scenario)
                        if mode == "off":
                            app.toggle[0].set_value(False)
                        app.button[0].click().run()
                        self.assertFalse(app.exception)
                        while app.session_state["agent"].pending:
                            label = "✅ 승인하고 실행" if mode == "approve" else "⛔ 거부"
                            next(b for b in app.button if b.label == label).click().run()
                            self.assertFalse(app.exception)
                        report = app.session_state["agent"].report
                        expected = "recovered"
                        if mode == "deny":
                            expected = "mitigated" if scenario == "db_leak" else "unresolved"
                        elif mode == "off" and scenario == "disk_full":
                            expected = "destroyed"
                        self.assertEqual(report["recovery_status"], expected)
                        self.assertEqual(len(app.get("download_button")), 2)

    def test_live_without_key_cannot_silently_start_demo(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
            app.radio[0].set_value("NVIDIA Nemotron (실제 LLM)").run()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("API 키가 없어" in e.value for e in app.error))

    def test_approval_deadline_and_cancel_show_exportable_report(self):
        with patch.dict(os.environ, {"STOPFORCE_DEMO_DELAY": "0", "NVIDIA_API_KEY": ""}):
            for outcome in ("timeout", "cancelled"):
                with self.subTest(outcome=outcome):
                    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
                    app.button[0].click().run()
                    agent = app.session_state["agent"]
                    self.assertIsNotNone(agent.pending)
                    self.assertTrue(any("남은 승인 가능 시간" in c.value for c in app.caption))
                    executed = list(agent.cluster.executed)
                    chats = agent.stats["chat_calls"]
                    if outcome == "timeout":
                        agent._deadline = 0
                        app.run()
                    else:
                        next(b for b in app.button if b.label == "🛑 실행 중단").click().run()
                    self.assertFalse(app.exception)
                    agent = app.session_state["agent"]
                    self.assertIsNone(agent.pending)
                    self.assertEqual(agent.report["termination"], outcome)
                    self.assertEqual(agent.report["recovery_status"], "mitigated")
                    self.assertEqual(agent.cluster.executed, executed)
                    self.assertEqual(agent.stats["chat_calls"], chats)
                    self.assertEqual(len(app.get("download_button")), 2)
                    self.assertFalse(any(b.label == "✅ 승인하고 실행" for b in app.button))


if __name__ == "__main__":
    unittest.main()
