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
from stopforce.reporting import write_report
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

    def test_early_wait_timeout_rechecks_deadline_and_waits_again(self):
        agent = self.pending_agent()
        class EarlyEvent:
            def __init__(self):
                self.waits = []
            def set(self):
                pass
            def wait(self, timeout):
                self.waits.append(timeout)
                return len(self.waits) > 1
        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        ready = EarlyEvent()
        with patch('cli.threading.Event', return_value=ready), \
                patch('cli.threading.Thread', InlineThread), patch('cli.input', return_value='y'), \
                patch.object(agent, 'check_deadline', side_effect=[False, False, False]) as deadline:
            self.assertIs(read_approval(agent), True)
        self.assertEqual(len(ready.waits), 2)
        self.assertGreater(ready.waits[0], 0)
        self.assertGreater(ready.waits[1], 0)
        self.assertLessEqual(ready.waits[1], ready.waits[0])
        self.assertEqual(deadline.call_count, 3)
        self.assertIsNotNone(agent.pending)
        self.assertEqual(agent.cluster.executed, [])

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
    def test_output_file_cannot_also_be_other_reports_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child = root / "not-created", root / "not-created" / "report.md"
            for json_path, markdown_path in ((parent, child), (child, parent)):
                error = io.StringIO()
                with self.subTest(json_path=json_path), patch("sys.argv", ["cli.py", "--json", str(json_path), "--markdown", str(markdown_path)]), \
                        patch("cli.NvidiaLLM") as constructor, redirect_stderr(error):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                self.assertEqual(caught.exception.code, 2)
                constructor.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])
                self.assertIn("상위 폴더", error.getvalue())

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
            markdown = md_path.read_text(encoding="utf-8")
            self.assertTrue(markdown.startswith("# Stop-Force 장애 보고서"))
            self.assertIn("원인", markdown)
            self.assertEqual(set(json_path.parent.iterdir()), {json_path, md_path})

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


class AtomicReportTests(unittest.TestCase):
    def test_success_replaces_existing_utf8_report_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '보고서.json'
            target.write_text('previous report', encoding='utf-8')
            content = json.dumps({'결과': '복구 완료'}, ensure_ascii=False)
            write_report(target, content)
            self.assertEqual(target.read_bytes(), content.encode('utf-8'))
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'결과': '복구 완료'})
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_partial_write_failure_preserves_existing_report_and_removes_temp(self):
        original_write = Path.write_text
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            target.write_bytes(b'previous complete report')
            def interrupted(path, content, **kwargs):
                self.assertNotEqual(path, target)
                self.assertEqual(path.parent, target.parent)
                original_write(path, content[:5], **kwargs)
                raise OSError('simulated partial write')
            with patch('pathlib.Path.write_text', interrupted):
                with self.assertRaises(OSError):
                    write_report(target, 'new content interrupted midway')
            self.assertEqual(target.read_bytes(), b'previous complete report')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_replace_failure_preserves_existing_report_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.md'
            target.write_bytes(b'previous markdown report')
            def reject(source, destination):
                self.assertEqual(destination, target)
                self.assertEqual(Path(source).read_text(encoding='utf-8'), '# 새로운 보고서')
                raise PermissionError('simulated replacement failure')
            with patch('stopforce.reporting.os.replace', side_effect=reject):
                with self.assertRaises(PermissionError):
                    write_report(target, '# 새로운 보고서')
            self.assertEqual(target.read_bytes(), b'previous markdown report')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cli_partial_write_failure_preserves_existing_report(self):
        original_write = Path.write_text
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            target.write_bytes(b'previous valid report')
            def interrupted(path, content, **kwargs):
                original_write(path, content[:8], **kwargs)
                raise OSError('private-storage-detail')
            error = io.StringIO()
            with patch('sys.argv', ['cli.py', 'bad_deploy', '--demo', '--auto-approve', '--json', str(target)]), \
                    patch('pathlib.Path.write_text', interrupted), \
                    redirect_stdout(io.StringIO()), redirect_stderr(error):
                self.assertEqual(main(), 1)
            self.assertEqual(target.read_bytes(), b'previous valid report')
            self.assertEqual(list(target.parent.iterdir()), [target])
            self.assertIn('보고서 저장에 실패', error.getvalue())
            self.assertNotIn('private-storage-detail', error.getvalue())


if __name__ == "__main__":
    unittest.main()
