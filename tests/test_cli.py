"""Terminal approval deadlines, without an API request or real stdin."""
import threading
import unittest
import io
import json
import os
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from cli import main, read_approval
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


class CLIValidationTests(unittest.TestCase):
    def test_conflicting_or_invalid_export_paths_fail_before_model_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            report.write_text("original report", encoding="utf-8")
            cases = [
                ["--json", str(report), "--markdown", str(root / "." / "report.json")],
                ["--json", str(root)],
                ["--json", str(report / "child.json")],
            ]
            for options in cases:
                with self.subTest(options=options), patch("sys.argv", ["cli.py", *options]), \
                        patch("cli.NvidiaLLM") as constructor, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                    self.assertEqual(caught.exception.code, 2)
                    constructor.assert_not_called()
                    self.assertEqual(report.read_text(encoding="utf-8"), "original report")

    def test_configuration_error_is_explained_without_traceback_or_values(self):
        error = io.StringIO()
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "test-key"}), patch("sys.argv", ["cli.py"]), \
                patch("cli.NvidiaLLM", side_effect=ValueError("private-config-value")), redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("실행 설정", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())
        self.assertNotIn("private-config-value", error.getvalue())

    def test_distinct_exports_keep_both_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = Path(tmp) / "nested/report.json", Path(tmp) / "nested/report.md"
            with patch("sys.argv", ["cli.py", "bad_deploy", "--demo", "--auto-approve", "--json", str(json_path), "--markdown", str(md_path)]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            report = json.loads(json_path.read_text(encoding="utf-8"))["report"]
            self.assertEqual(report["recovery_status"], "recovered")
            self.assertEqual(report["telemetry"]["api_calls"], 0)
            self.assertTrue(md_path.read_text(encoding="utf-8").startswith("# Stop-Force"))

    def test_export_write_failure_returns_error_without_traceback(self):
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            with patch("sys.argv", ["cli.py", "bad_deploy", "--demo", "--auto-approve", "--json", str(output)]), \
                    patch("pathlib.Path.write_text", side_effect=PermissionError("private-storage-detail")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(error):
                self.assertEqual(main(), 1)
        self.assertIn("보고서 저장에 실패", error.getvalue())
        self.assertNotIn("private-storage-detail", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())


if __name__ == "__main__":
    unittest.main()
