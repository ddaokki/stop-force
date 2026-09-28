"""Critical Streamlit approval paths, tested without API access."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


class UITests(unittest.TestCase):
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
