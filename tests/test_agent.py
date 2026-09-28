import json
import unittest
from pathlib import Path
from unittest.mock import patch

from stopforce.agent import Agent
from stopforce.cluster import make_cluster
from stopforce.llm import ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary

ROOT = Path(__file__).resolve().parents[1]
FINISH = {"root_cause": "모델 추정", "actions_taken": ["실행하지 않은 조치"],
          "summary": "모델의 근거 없는 복구 주장", "follow_ups": []}


class FakeLLM:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.received = []

    def chat(self, messages, tools):
        self.received.append(json.loads(json.dumps(messages)))
        response = next(self.calls)
        if isinstance(response, Exception):
            raise response
        return response


def call(name, arguments, id=None, **extra):
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments, **({"id": id} if id else {}), **extra}]}


def command(cmd):
    return call("run_command", {"command": cmd, "reason": "regression"})


def make_agent(llm=None, scenario="db_leak", enabled=True, **kwargs):
    return Agent(make_cluster(scenario), Policy(ROOT / "policy.yaml", enabled),
                 SkillLibrary(ROOT / "skills"), llm or ScriptedLLM(scenario),
                 'incident password="test secret value"', **kwargs)


class AgentTests(unittest.TestCase):
    def test_all_approved_scenarios(self):
        for scenario in ("db_leak", "bad_deploy", "disk_full"):
            with self.subTest(scenario=scenario):
                agent = make_agent(scenario=scenario)
                agent.run()
                while agent.pending:
                    agent.resolve(True, request_id=agent.pending["id"])
                self.assertEqual(agent.report["recovery_status"], "recovered")
                self.assertEqual(agent.report["telemetry"]["api_calls"], 0)
                ids = {e.id for e in agent.events}
                self.assertTrue(all(a["event_id"] in ids for a in agent.report["actions"]))

    def test_denial_preserves_restart_and_partial_recovery(self):
        agent = make_agent()
        agent.run()
        agent.resolve(False)
        self.assertEqual(agent.report["recovery_status"], "mitigated")
        self.assertTrue(agent.report["symptoms_resolved"])
        self.assertFalse(agent.report["root_cause_resolved"])
        self.assertTrue(agent.report["human_handoff"])
        self.assertTrue(any("restart" in s for s in agent.report["actions_taken"]))
        self.assertFalse(any("undo" in s for s in agent.report["actions_taken"]))

    def test_policy_off_accident_never_reports_recovery(self):
        agent = make_agent(scenario="disk_full", enabled=False)
        agent.run()
        self.assertEqual(agent.report["recovery_status"], "destroyed")
        self.assertFalse(agent.report["verified"]["healthy"])
        self.assertIn("파괴", agent.report["summary"])
        self.assertFalse(any(a["status"] == "blocked" for a in agent.actions))

    def test_fabricated_actions_and_summary_not_trusted(self):
        agent = make_agent(FakeLLM([call("finish", FINISH)]))
        agent.run()
        self.assertEqual(agent.report["actions_taken"], [])
        self.assertEqual(agent.report["recovery_status"], "unresolved")
        self.assertNotEqual(agent.report["summary"], FINISH["summary"])

    def test_invalid_tool_arguments_are_repairable(self):
        invalids = [call("read_logs", {"service": "nginx", "lines": "abc"}),
                    call("read_logs", []), call("read_logs", None, parse_error="invalid JSON"),
                    call("missing_tool", {}), call("read_logs", {"service": "nginx", "lines": True})]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                agent = make_agent(FakeLLM([invalid, call("finish", FINISH)]))
                agent.run()
                self.assertEqual(agent.report["termination"], "finish")
                self.assertEqual(agent.stats["tool_errors"], 1)
                error = next(m for m in agent.messages if m["role"] == "tool")
                self.assertFalse(json.loads(error["content"])["ok"])

    def test_error_limits_always_report(self):
        agent = make_agent(FakeLLM([call("read_logs", [])] * 3))
        agent.run()
        self.assertEqual(agent.report["termination"], "tool_errors")
        failed = make_agent(FakeLLM([RuntimeError('password="exception secret"')]))
        failed.run()
        self.assertEqual(failed.report["termination"], "llm_error")
        self.assertNotIn("exception secret", json.dumps(failed.export()))
        limited = make_agent(FakeLLM([]), max_steps=0)
        limited.run()
        self.assertEqual(limited.report["termination"], "max_steps")

    def test_total_timeout_prevents_action_after_slow_model(self):
        agent = make_agent(FakeLLM([command("kubectl rollout restart deployment/payment-worker")]))
        with patch("stopforce.agent.time.monotonic", side_effect=[agent._started, agent._deadline + 1, agent._deadline + 2]):
            agent.run()
        self.assertEqual(agent.report["termination"], "timeout")
        self.assertEqual(agent.cluster.executed, [])

    def test_stale_approval_duplicate_and_denial_alias(self):
        undo = "kubectl rollout undo deployment/payment-worker"
        agent = make_agent(FakeLLM([command(undo), command("kubectl rollout undo deploy/payment-worker"), call("finish", FINISH)]))
        self.assertFalse(agent.resolve(True))
        agent.run()
        request_id = agent.pending["id"]
        self.assertFalse(agent.resolve(True, request_id="stale"))
        self.assertTrue(agent.resolve(False, request_id=request_id))
        self.assertFalse(agent.resolve(True, request_id=request_id))
        self.assertEqual(agent.cluster.executed, [])
        self.assertEqual([a["status"] for a in agent.actions], ["denied", "denied"])

    def test_duplicate_successful_mutation_not_run_twice(self):
        undo = command("kubectl rollout undo deployment/checkout-api")
        agent = make_agent(FakeLLM([undo, undo, call("finish", FINISH)]), scenario="bad_deploy")
        agent.run()
        agent.resolve(True)
        self.assertEqual(len(agent.cluster.executed), 1)
        self.assertEqual(agent.report["recovery_status"], "recovered")
        self.assertEqual(agent.actions[-1]["status"], "duplicate_skipped")

    def test_pending_command_tampering_invalidates_approval(self):
        agent = make_agent()
        agent.run()
        agent.pending["command"] += "; rm -rf /"
        agent.resolve(True)
        self.assertEqual(agent.report["recovery_status"], "mitigated")
        self.assertFalse(agent.cluster.destroyed)
        self.assertEqual(agent.report["approvals"][0]["status"], "invalidated")

    def test_redaction_on_and_off_covers_external_and_export_boundaries(self):
        for enabled in (True, False):
            llm = FakeLLM([call("read_logs", {"service": "payment-worker"}), call("finish", FINISH)])
            agent = make_agent(llm, enabled=enabled)
            agent.run()
            text = json.dumps([agent.export(), llm.received], ensure_ascii=False)
            for secret in ["test secret value", "Pg!Sup3rS3cret", "sk_live_51Hx9QeFAKEFAKEFAKE0000"]:
                self.assertNotIn(secret, text)

    def test_execution_failure_is_recorded(self):
        agent = make_agent(FakeLLM([command("kubectl rollout undo deployment/nginx"), call("finish", FINISH)]))
        agent.run()
        agent.resolve(True)
        self.assertTrue(agent.report["execution_failed"])
        self.assertEqual(agent.report["actions_taken"], [])
        self.assertEqual(agent.cluster.executed, [])

    def test_tool_limit_and_cancel_report(self):
        agent = make_agent(FakeLLM([{"tool_calls": [{"name": "list_services", "arguments": {}}] * 3}]), max_tool_calls=2)
        agent.run()
        self.assertEqual(agent.report["termination"], "max_tool_calls")
        agent = make_agent()
        agent.run()
        agent.cancel()
        self.assertEqual(agent.report["termination"], "cancelled")
        self.assertFalse(agent.resolve(True))

    def test_idle_approval_expiration_does_not_call_model_or_execute(self):
        for expire in (lambda a: a.run(), lambda a: a.check_deadline()):
            with self.subTest(expire=expire):
                agent = make_agent()
                agent.run()
                request_id = agent.pending["id"]
                executed = list(agent.cluster.executed)
                chats = agent.stats["chat_calls"]
                with patch("stopforce.agent.time.monotonic", return_value=agent._deadline + 1):
                    expire(agent)
                self.assertIsNone(agent.pending)
                self.assertEqual(agent.report["termination"], "timeout")
                self.assertEqual(agent.report["approvals"][0]["status"], "expired")
                self.assertEqual(agent.report["actions"][-1]["status"], "not_executed")
                self.assertEqual(agent.stats["chat_calls"], chats)
                self.assertEqual(agent.cluster.executed, executed)
                self.assertFalse(agent.resolve(True, request_id=request_id))
                count = len(agent.events)
                self.assertFalse(agent.check_deadline())
                self.assertEqual(len(agent.events), count)
                self.assertEqual(agent.remaining_seconds, 0)

    def test_checking_pending_deadline_preserves_unexpired_request(self):
        agent = make_agent()
        agent.run()
        request_id = agent.pending["id"]
        with patch("stopforce.agent.time.monotonic", return_value=agent._deadline - 12):
            self.assertFalse(agent.check_deadline())
            self.assertEqual(agent.remaining_seconds, 12)
        self.assertEqual(agent.pending["id"], request_id)
        self.assertIsNone(agent.report)


if __name__ == "__main__":
    unittest.main()
