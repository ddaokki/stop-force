import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stopforce.agent import Agent
from stopforce.cluster import make_cluster
from stopforce.llm import NvidiaLLM, ScriptedLLM
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


def provider_reply(name, args, native, *, finish_reason=None, refusal=None):
    tool = SimpleNamespace(id="provider_" + name + "_" + str(len(json.dumps(args))),
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    message = SimpleNamespace(content="" if native else json.dumps({"tool": name, "args": args}),
                              tool_calls=[tool] if native else [], refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message,
                           finish_reason=finish_reason or ("tool_calls" if native else "stop"))],
                           usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))


class AgentTests(unittest.TestCase):
    def test_approval_from_another_run_cannot_resolve_current_request(self):
        previous = make_agent(FakeLLM([command("kubectl rollout undo deployment/payment-worker")]))
        current = make_agent(FakeLLM([command("kubectl rollout undo deployment/checkout-api"), call("finish", FINISH)]),
                             scenario="bad_deploy")
        previous.run()
        current.run()
        old_id, current_id = previous.pending["id"], current.pending["id"]
        self.assertNotEqual(previous.run_id, current.run_id)
        self.assertNotEqual(old_id, current_id)
        for approved in (True, False):
            self.assertFalse(current.resolve(approved, request_id=old_id))
            self.assertEqual(current.pending["id"], current_id)
            self.assertEqual(current.cluster.attempted, [])
            self.assertEqual(current.stats["chat_calls"], 1)
        self.assertTrue(current.resolve(True, request_id=current_id))
        self.assertEqual(current.report["termination"], "finish")
        self.assertEqual(current.report["recovery_status"], "recovered")
        self.assertEqual(current.report["run_id"], current.run_id)
        self.assertEqual(current.report["approvals"][0]["id"], current_id)
        self.assertEqual(current.report["actions"][0]["approval_id"], current_id)
        self.assertEqual(len(current.cluster.executed), 1)

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

    def test_log_or_file_contents_cannot_set_command_failure(self):
        for cmd in ("kubectl logs nginx", "cat /etc/app/config.yaml"):
            for body in ("ERROR expected log data", "error is a setting", "unsupported is a setting"):
                with self.subTest(command=cmd, body=body):
                    agent = make_agent(FakeLLM([command(cmd), call("finish", FINISH)]))
                    agent.cluster.logs["nginx"] = [body]
                    agent.cluster.files["/etc/app/config.yaml"] = body
                    agent.run()
                    self.assertEqual(agent.actions[0]["status"], "succeeded")
                    self.assertEqual(agent.actions[0]["result"], body)
                    self.assertFalse(agent.report["execution_failed"])
                    self.assertEqual(agent.stats["tool_errors"], 0)
                    self.assertEqual(agent.cluster.executed, [cmd])

    def test_command_failures_count_toward_tool_error_limit(self):
        agent = make_agent(FakeLLM([command("kubectl rollout undo deploy/nginx")] * 4 + [call("finish", FINISH)]))
        agent.run()
        while agent.pending:
            agent.resolve(True)
        self.assertEqual(agent.report["termination"], "tool_errors")
        self.assertEqual(agent.stats["tool_errors"], 3)
        self.assertEqual(len(agent.actions), 3)
        self.assertEqual(len(agent.cluster.attempted), 3)
        self.assertEqual(agent.cluster.executed, [])
        self.assertTrue(all(a["status"] == "failed" for a in agent.actions))
        errors = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
        self.assertTrue(all(e["ok"] is False and e["error"]["code"] for e in errors))

    def test_exception_during_command_is_repairable_and_counted(self):
        agent = make_agent(FakeLLM([command("df -h"), call("finish", FINISH)]))
        with patch.object(agent.cluster, "execute_result", side_effect=RuntimeError('password="private failure"')):
            agent.run()
        self.assertEqual(agent.report["termination"], "finish")
        self.assertEqual(agent.stats["tool_errors"], 1)
        self.assertEqual(agent.actions[0]["status"], "failed")
        self.assertNotIn("private failure", json.dumps(agent.export()))

    def test_approved_but_expired_action_is_terminal_and_has_evidence(self):
        agent = make_agent(FakeLLM([command("kubectl rollout undo deploy/payment-worker")]))
        agent.run()

        def expire_after_approval(event):
            if event.kind == "approval":
                agent._deadline = 0

        agent.resolve(True, expire_after_approval)
        self.assertEqual(agent.report["termination"], "timeout")
        self.assertEqual(agent.cluster.executed, [])
        action = agent.report["actions"][0]
        approval = agent.report["approvals"][0]
        self.assertEqual(action["status"], "not_executed")
        self.assertEqual(action["not_executed_reason"], "timeout")
        self.assertTrue(action["approved"])
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["closure_event_id"], action["event_id"])
        event = next(e for e in agent.events if e.id == action["event_id"])
        self.assertEqual(event.title, "실행하지 않음")
        self.assertEqual(agent.report["actions_taken"], [])

    def test_cancellation_while_preparing_or_approving_prevents_execution(self):
        for cancel_at in ("run_command 요청", "온콜 엔지니어 승인"):
            with self.subTest(cancel_at=cancel_at):
                agent = make_agent(FakeLLM([command("kubectl rollout undo deploy/payment-worker")]))

                def cancel(event):
                    if event.title == cancel_at:
                        agent.cancel("작업 취소")

                agent.run(cancel)
                if agent.pending:
                    agent.resolve(True, cancel)
                self.assertEqual(agent.report["termination"], "cancelled")
                self.assertEqual(agent.cluster.attempted, [])
                self.assertEqual(agent.actions[0]["status"], "not_executed")
                self.assertEqual(agent.report["actions"][0]["status"], "not_executed")
                self.assertEqual(agent.report["actions"], agent.actions)
                self.assertEqual(agent.report["approvals"], list(agent.approvals.values()))
                self.assertIsNone(agent.pending)

    def test_ambiguous_model_json_cannot_execute_before_repair(self):
        duplicate = ('{"tool":"run_command","args":{"command":"kubectl get pods",'
                     '"command":"kubectl rollout restart deploy/payment-worker","reason":"ambiguous"}}')
        first = json.dumps({"tool": "run_command", "args": {
            "command": "kubectl rollout restart deploy/payment-worker", "reason": "first"}})
        second = json.dumps({"tool": "read_logs", "args": {"service": "nginx"}})
        multiple = "```json\n" + first + "\n```\n정정\n```json\n" + second + "\n```"
        for payload in (duplicate, multiple):
            with self.subTest(payload=payload), patch("openai.OpenAI"):
                llm = NvidiaLLM(api_key="test-only-key", backoff_seconds=0)
                llm.native_tools = False
                llm.client.chat.completions.create.side_effect = [
                    SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))])
                    for text in (payload, json.dumps({"tool": "finish", "args": FINISH}))]
                agent = make_agent(llm)
                agent.run()
                self.assertEqual(agent.cluster.attempted, [])
                self.assertEqual(agent.report["actions_taken"], [])
                self.assertEqual(agent.report["termination"], "finish")
                self.assertEqual(agent.stats["tool_errors"], 1)

    def test_deep_invalid_arguments_are_rejected_before_recursive_processing(self):
        nested = "untrusted payload"
        for _ in range(1500):
            nested = [nested]
        for args in ({"service": nested}, {"extra": nested}):
            with self.subTest(field=next(iter(args))):
                agent = make_agent(FakeLLM([call("read_logs", args), call("finish", FINISH)]))
                agent.run()
                self.assertEqual(agent.report["termination"], "finish")
                self.assertEqual(agent.stats["tool_errors"], 1)
                self.assertEqual(agent.cluster.attempted, [])
                self.assertNotIn("untrusted payload", json.dumps(agent.export()))

    def test_unused_call_metadata_is_not_copied_into_history(self):
        nested = "unused metadata"
        for _ in range(1500):
            nested = [nested]
        agent = make_agent(FakeLLM([call("list_services", {}, metadata=nested), call("finish", FINISH)]))
        agent.run()
        self.assertEqual(agent.report["termination"], "finish")
        self.assertEqual(agent.stats["tool_errors"], 0)
        self.assertEqual(len(agent.observations), 1)

    def test_normalized_call_ids_are_unique_and_results_remain_linked(self):
        cases = [
            ([("duplicate_1_1", {}), ("duplicate_1_1", {})], 1),
            ([("call_1_1", {}), (None, {})], 0),
            ([("nvapi-fixture-one", {}), ("nvapi-fixture-two", {})], 0),
        ]
        for supplied, expected_errors in cases:
            with self.subTest(ids=[item[0] for item in supplied]):
                batch = {"tool_calls": [{"id": identifier, "name": "list_services", "arguments": args}
                                        for identifier, args in supplied]}
                agent = make_agent(FakeLLM([batch, call("finish", FINISH)]))
                agent.run()
                assistant = next(m for m in agent.messages if m.get("tool_calls"))
                ids = [c["id"] for c in assistant["tool_calls"]]
                results = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(results, ids)
                self.assertEqual(agent.stats["tool_errors"], expected_errors)
                self.assertNotIn("nvapi-fixture", json.dumps(agent.export()))
                self.assertNotIn("nvapi-fixture", json.dumps(agent.messages))

    def test_incomplete_or_refused_provider_response_cannot_execute(self):
        args = {"command": "kubectl rollout restart deployment/payment-worker", "reason": "test"}
        for native in (True, False):
            for reason, refusal, expected in (("length", None, "incomplete_response"),
                                              ("content_filter", None, "refusal"),
                                              ("stop", "private-refusal-body", "refusal")):
                with self.subTest(native=native, reason=reason, refusal=bool(refusal)), patch("openai.OpenAI"):
                    llm = NvidiaLLM(api_key="test-only-key")
                    llm.native_tools = native
                    llm.client.chat.completions.create.return_value = provider_reply(
                        "run_command", args, native, finish_reason=reason, refusal=refusal)
                    agent = make_agent(llm)
                    agent.run()
                    self.assertEqual(agent.cluster.attempted, [])
                    self.assertEqual(agent.report["termination"], expected)
                    self.assertEqual(agent.report["recovery_status"], "unresolved")
                    self.assertTrue(agent.report["human_handoff"])
                    self.assertEqual(agent.report["telemetry"]["api_calls"], 1)
                    self.assertEqual(agent.report["telemetry"]["total_tokens"], 10)
                    self.assertNotIn("private-refusal-body", json.dumps(agent.export()))
                    self.assertEqual(llm.native_tools, native)

    def test_provider_adapter_core_flow_preserves_approval_and_report(self):
        plan = [
            ("list_services", {}),
            ("read_logs", {"service": "payment-worker"}),
            ("run_command", {"command": "kubectl rollout restart deployment/payment-worker", "reason": "mitigate"}),
            ("run_command", {"command": "kubectl rollout undo deployment/payment-worker", "reason": "repair"}),
            ("get_metrics", {"service": "payment-api"}),
            ("finish", FINISH),
        ]
        for native in (True, False):
            for approved in (True, False):
                with self.subTest(native=native, approved=approved), patch("openai.OpenAI"):
                    llm = NvidiaLLM(api_key="test-only-key")
                    llm.native_tools = native
                    llm.client.chat.completions.create.side_effect = [provider_reply(name, args, native) for name, args in plan]
                    agent = make_agent(llm)
                    agent.run()
                    self.assertIsNotNone(agent.pending)
                    self.assertEqual(agent.cluster.executed, [plan[2][1]["command"]])
                    self.assertEqual(agent.cluster.health()["recovery_status"], "mitigated")
                    self.assertTrue(agent.resolve(approved, request_id=agent.pending["id"]))
                    report = agent.report
                    self.assertEqual(report["termination"], "finish")
                    self.assertEqual(report["recovery_status"], "recovered" if approved else "mitigated")
                    self.assertEqual(report["approvals"][0]["status"], "approved" if approved else "denied")
                    self.assertEqual(len(agent.cluster.executed), 2 if approved else 1)
                    self.assertEqual(report["telemetry"]["api_calls"], len(plan))
                    self.assertEqual(report["telemetry"]["total_tokens"], len(plan) * 10)
                    self.assertEqual(len(report["observations"]), 3)
                    events = {event.id: event for event in agent.events}
                    self.assertTrue(all(events[action["event_id"]].kind == "execution"
                                        for action in report["actions"] if action["status"] == "succeeded"))

    def test_interrupt_at_approval_request_keeps_terminal_report(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                agent = make_agent(FakeLLM([command("kubectl rollout undo deployment/payment-worker")]))

                def stop(event):
                    if event.title == "사람 승인 필요":
                        if interrupt:
                            raise KeyboardInterrupt
                        agent.cancel("작업 취소")

                if interrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        agent.run(stop)
                    agent.cancel("터미널 중단")
                else:
                    agent.run(stop)
                self.assertEqual(agent.report["termination"], "cancelled")
                self.assertEqual(agent.report["actions"], agent.actions)
                self.assertEqual(agent.report["approvals"], list(agent.approvals.values()))
                self.assertEqual(agent.report["approvals"][0]["status"], "cancelled")
                self.assertEqual(agent.report["actions"][0]["status"], "not_executed")
                self.assertEqual(agent.report["approvals"][0]["closure_event_id"], agent.actions[0]["event_id"])
                self.assertEqual(agent.cluster.attempted, [])
                self.assertIsNone(agent.pending)
                self.assertEqual(agent.queue, [])
                self.assertFalse(agent.resolve(True))

    def test_cancel_on_result_preserves_evidence_already_recorded(self):
        for title in ("시뮬레이터 실행 완료", "실행 실패", "read_logs 결과"):
            with self.subTest(title=title):
                response = call("read_logs", {"service": "payment-worker"}) if title == "read_logs 결과" else command("df -h")
                agent = make_agent(FakeLLM([response, call("finish", FINISH)]))
                if title == "실행 실패":
                    agent.cluster.execute_result = Mock(side_effect=RuntimeError("test failure"))

                def cancel(event):
                    if event.title == title:
                        agent.cancel("작업 취소")

                agent.run(cancel)
                self.assertEqual(agent.report["termination"], "cancelled")
                self.assertEqual(agent.report["actions"], agent.actions)
                self.assertEqual(agent.report["observations"], agent.observations)
                self.assertEqual(agent.report["stats"]["tool_errors"], agent.stats["tool_errors"])
                self.assertEqual(agent.stats["chat_calls"], 1)
                self.assertEqual(agent.events[-1].kind, "final")
                if agent.actions:
                    action = agent.report["actions"][0]
                    event = next(e for e in agent.events if e.id == action["event_id"])
                    self.assertEqual(event.kind, "execution")
                    self.assertEqual(action["status"], "failed" if title == "실행 실패" else "succeeded")
                else:
                    self.assertEqual(len(agent.report["observations"]), 1)

    def test_report_exists_before_final_notifications_can_interrupt(self):
        for title in ("실행하지 않음", "최종 독립 검증", "장애 보고서"):
            with self.subTest(title=title):
                agent = make_agent(FakeLLM([command("kubectl rollout undo deployment/payment-worker")]))
                agent.run()

                def interrupt(event):
                    if event.title == title:
                        raise KeyboardInterrupt

                agent._on_event = interrupt
                with self.assertRaises(KeyboardInterrupt):
                    agent.cancel("터미널 중단")
                self.assertEqual(agent.report["termination"], "cancelled")
                self.assertEqual(agent.report["actions"], agent.actions)
                self.assertEqual(agent.events[-1].kind, "final")
                self.assertIsNone(agent.pending)

    def test_cancel_during_thought_or_lookup_does_not_resume_work(self):
        for title in ("모델 판단 (실행 사실은 하네스에서 검증)", "read_logs"):
            with self.subTest(title=title):
                response = call("read_logs", {"service": "payment-worker"})
                response["content"] = "로그를 확인합니다."
                agent = make_agent(FakeLLM([response, call("finish", FINISH)]))

                def cancel(event):
                    if event.title == title:
                        agent.cancel("작업 취소")

                with patch.object(agent.cluster, "read_logs") as read:
                    agent.run(cancel)
                read.assert_not_called()
                self.assertEqual(agent.report["termination"], "cancelled")
                self.assertEqual(agent.queue, [])
                self.assertEqual(agent.events[-1].kind, "final")


if __name__ == "__main__":
    unittest.main()
