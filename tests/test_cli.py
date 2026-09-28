"""Terminal approval deadlines, without an API request or real stdin."""
import threading
import unittest
from unittest.mock import patch

from cli import read_approval
from test_agent import FakeLLM, command, make_agent


class ApprovalInputTests(unittest.TestCase):
    def pending_agent(self, **kwargs):
        agent = make_agent(FakeLLM([command("kubectl rollout undo deploy/payment-worker")]), **kwargs)
        agent.run()
        self.assertIsNotNone(agent.pending)
        return agent

    def test_yes_no_and_eof_keep_input_semantics(self):
        for text, expected in ((" Y ", True), ("n", False), ("", False)):
            with self.subTest(text=text), patch("cli.input", return_value=text):
                agent = self.pending_agent()
                self.assertIs(read_approval(agent), expected)
                self.assertEqual(agent.cluster.executed, [])
        with patch("cli.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                read_approval(self.pending_agent())

    def test_blocked_input_expires_and_late_yes_cannot_execute(self):
        release, completed = threading.Event(), threading.Event()

        def late_yes(_):
            release.wait(2)
            completed.set()
            return "y"

        agent = self.pending_agent(max_seconds=0.15)
        request_id = agent.pending["id"]
        try:
            with patch("cli.input", side_effect=late_yes):
                self.assertIsNone(read_approval(agent))
                self.assertEqual(agent.report["termination"], "timeout")
                self.assertIsNone(agent.pending)
                release.set()
                self.assertTrue(completed.wait(1))
            self.assertFalse(agent.resolve(True, request_id=request_id))
            self.assertEqual(agent.cluster.executed, [])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
