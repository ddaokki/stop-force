"""Complete Bearer masking and recoverable CLI path errors; synthetic inputs only."""
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli import main
from stopforce.llm import NvidiaLLM
from stopforce.policy import Policy
from test_agent import FINISH, FakeLLM, call, make_agent
from test_llm import response

ROOT = Path(__file__).resolve().parents[1]


class BearerRedactionTests(unittest.TestCase):
    def test_extended_bearer_characters_are_fully_redacted(self):
        for enabled in (True, False):
            policy = Policy(ROOT / "policy.yaml", enabled=enabled)
            for token in ("alpha+secret/tail==", "alpha~secret+tail/", "alpha._-XYZ0123=="):
                with self.subTest(enabled=enabled, token=token):
                    text = "Authorization: Bearer " + token + " status=ok"
                    masked, count = policy.redact(text)
                    self.assertEqual(masked, "Authorization: [REDACTED:bearer] status=ok")
                    self.assertEqual(count, 1)
                    self.assertEqual(policy.redact(masked), (masked, 0))

    def test_extended_token_stays_out_of_model_messages_and_exports(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                llm = FakeLLM([call("read_logs", {"service": "nginx"}), call("finish", FINISH)])
                agent = make_agent(llm, enabled=enabled)
                agent.cluster.logs["nginx"].append("Authorization: Bearer alpha+private/tail==")
                agent.run()
                serialized = json.dumps({"history": llm.received, "export": agent.export()})
                self.assertNotIn("private/tail", serialized)
                self.assertIn("[REDACTED:bearer]", serialized)

    def test_provider_boundary_redacts_extended_bearer_tokens(self):
        with patch("openai.OpenAI"):
            llm = NvidiaLLM(api_key="test-api-secret")
        llm.client.chat.completions.create.return_value = response()
        llm.chat([{"role": "user", "content": "Bearer alpha+private/tail=="}], [])
        sent = json.dumps(llm.client.chat.completions.create.call_args.kwargs)
        self.assertNotIn("private/tail", sent)


class CLIPathRegressionTests(unittest.TestCase):
    def test_null_byte_path_is_rejected_before_model_setup(self):
        error = io.StringIO()
        with patch("sys.argv", ["cli.py", "--json", "private-path\x00/report.json"]), \
                patch("cli.NvidiaLLM") as constructor, redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)
        constructor.assert_not_called()
        self.assertNotIn("private-path", error.getvalue())

    def test_symlink_loop_is_rejected_before_model_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-loop"
            try:
                path.symlink_to(path.name)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable on this platform")
            error = io.StringIO()
            with patch("sys.argv", ["cli.py", "--json", str(path)]), \
                    patch("cli.NvidiaLLM") as constructor, redirect_stderr(error):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 2)
            constructor.assert_not_called()
            self.assertNotIn("private-loop", error.getvalue())


if __name__ == "__main__":
    unittest.main()
