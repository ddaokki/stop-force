import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from streamlit.runtime.download_data_util import convert_data_to_bytes_and_infer_mime

from stopforce.agent import Agent
from stopforce.cluster import make_cluster
from stopforce.policy import Policy
from stopforce.reporting import report_json, report_markdown, write_report
from stopforce.skills import SkillLibrary


ROOT = Path(__file__).resolve().parents[1]
TEXT = "한글 😀 " + json.loads('"\\ud800"') + " " + "\ud83d\ude00" + " " + "\udc00"
NORMALIZED = "한글 😀 � 😀 �"


def reply(name, arguments, content=""):
    return {"content": content, "tool_calls": [{"name": name, "arguments": arguments}]}


def finish():
    return reply("finish", {"root_cause": TEXT, "actions_taken": [], "summary": TEXT, "follow_ups": [TEXT]}, TEXT)


class Replies:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.received = []

    def chat(self, messages, tools):
        self.received.append(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        return next(self.responses)


class UnicodeTests(unittest.TestCase):
    def make_agent(self, responses):
        cluster = make_cluster("db_leak")
        return Agent(cluster, Policy(ROOT / "policy.yaml"), SkillLibrary(ROOT / "skills"),
                     Replies(responses), TEXT)

    def test_incident_model_and_tool_output_are_safe_for_ui_and_exports(self):
        agent = self.make_agent([reply("read_logs", {"service": "payment-worker"}, TEXT), finish()])
        agent.cluster.logs["payment-worker"] = [TEXT]
        observed = []

        def render(event):
            observed.append(json.dumps(asdict(event), ensure_ascii=False).encode("utf-8"))

        agent.run(render)
        self.assertTrue(agent.done)
        self.assertEqual(agent.report["termination"], "finish")
        self.assertEqual(agent.report["incident"], NORMALIZED)
        self.assertEqual(agent.report["root_cause"], NORMALIZED)
        self.assertEqual(agent.report["follow_ups"], [NORMALIZED])
        self.assertTrue(any(event.body == NORMALIZED for event in agent.events if event.kind == "result"))
        self.assertTrue(observed)
        self.assertEqual(len(agent.llm.received), 2)
        with tempfile.TemporaryDirectory() as directory:
            for name, renderer in (("report.json", report_json), ("report.md", report_markdown)):
                with self.subTest(name=name):
                    text = renderer(agent)
                    self.assertIn(NORMALIZED, text)
                    path = Path(directory) / name
                    write_report(path, text)
                    self.assertEqual(path.read_text(encoding="utf-8"), text)
                    data, _ = convert_data_to_bytes_and_infer_mime(text, unsupported_error=ValueError("unsupported"))
                    self.assertEqual(data, text.encode("utf-8"))
            exported = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(exported["report"]["root_cause"], NORMALIZED)

    def test_dictionary_keys_are_unicode_safe_and_secret_fields_stay_masked(self):
        agent = self.make_agent([])
        safe = agent._safe({TEXT: TEXT, "password": "private-secret", "API_KEY": "private-key"})
        self.assertEqual(safe[NORMALIZED], NORMALIZED)
        self.assertEqual(safe["password"], "[REDACTED:field]")
        self.assertEqual(safe["API_KEY"], "[REDACTED:field]")
        self.assertNotIn("private", json.dumps(safe, ensure_ascii=False))
        json.dumps(safe, ensure_ascii=False).encode("utf-8")

    def test_invalid_surrogates_do_not_turn_into_supported_tools_or_commands(self):
        bad = json.loads('"\\ud800"')
        for first in (
            reply("run_command", {"command": "kubectl get pods" + bad, "reason": "test"}),
            reply("run_command" + bad, {"command": "kubectl get pods", "reason": "test"}),
        ):
            with self.subTest(first=ascii(first)):
                agent = self.make_agent([first, finish()])
                agent.run()
                self.assertEqual(agent.report["termination"], "finish")
                self.assertEqual(agent.cluster.executed, [])
                self.assertFalse(any(action["status"] == "succeeded" for action in agent.actions))
                report_json(agent).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
