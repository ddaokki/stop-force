import json
import time
import unittest
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from stopforce.llm import LLMError, NvidiaLLM, ScriptedLLM
from stopforce.policy import Policy
from pathlib import Path


def response(content="ok", arguments=None):
    calls = [] if arguments is None else [NS(id="a", function=NS(name="run_command", arguments=arguments))]
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))],
              usage=NS(prompt_tokens=7, completion_tokens=3, total_tokens=10))


def failure(status, message="provider secret"):
    error = RuntimeError(message)
    error.status_code = status
    return error


class NvidiaTests(unittest.TestCase):
    def make(self, outcomes, **kwargs):
        with patch("openai.OpenAI") as constructor:
            llm = NvidiaLLM(api_key="test-api-secret", backoff_seconds=0, **kwargs)
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        llm.native_tools = True
        llm.client.chat.completions.create = Mock(side_effect=outcomes)
        return llm

    def test_retry_and_telemetry(self):
        llm = self.make([failure(429), TimeoutError("secret"), response()])
        result = llm.chat([], [])
        self.assertEqual(result["telemetry"]["api_calls"], 3)
        self.assertEqual(llm.telemetry["total_tokens"], 10)
        self.assertGreaterEqual(llm.telemetry["duration_seconds"], 0)
        self.assertEqual(llm.client.chat.completions.create.call_args.kwargs["timeout"], 60)

    def test_classified_failures_no_fallback(self):
        for status, category in [(401, "auth"), (403, "auth"), (404, "model"), (400, "request")]:
            with self.subTest(status=status):
                llm = self.make([failure(status, "bad tool schema test-api-secret")])
                with self.assertRaises(LLMError) as caught:
                    llm.chat([], [])
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(llm.native_tools)
                self.assertEqual(llm.telemetry["api_calls"], 1)

    def test_retry_exhaustion(self):
        for error, category in [(failure(503), "server"), (TimeoutError(), "timeout"), (failure(429), "rate_limit")]:
            llm = self.make([error] * 3)
            with self.assertRaises(LLMError) as caught:
                llm.chat([], [])
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(llm.telemetry["api_calls"], 3)

    def test_deadline_bounds_request_and_backoff(self):
        llm = self.make([response()])
        llm.set_deadline(time.monotonic() + 1)
        llm.chat([], [])
        self.assertLessEqual(llm.client.chat.completions.create.call_args.kwargs["timeout"], 1)
        llm.set_deadline(time.monotonic() - 1)
        with self.assertRaises(LLMError):
            llm.chat([], [])
        self.assertEqual(llm.telemetry["api_calls"], 1)
        llm = self.make([failure(503)])
        llm.backoff_seconds = 2
        llm.set_deadline(time.monotonic() + 1)
        with patch("stopforce.llm.time.sleep") as sleep:
            with self.assertRaises(LLMError):
                llm.chat([], [])
            sleep.assert_not_called()

    def test_trickling_or_stuck_transport_has_wall_limit_and_no_retry(self):
        llm = self.make([], request_timeout=0.03)
        release = threading.Event()
        finished = threading.Event()

        def stalled(**kwargs):
            release.wait(2)
            finished.set()
            return response()

        llm.client.chat.completions.create.side_effect = stalled
        started = time.monotonic()
        try:
            with self.assertRaises(LLMError) as caught:
                llm.chat([], [])
            self.assertEqual(caught.exception.category, "timeout")
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(llm.telemetry["api_calls"], 1)
        finally:
            release.set()
            finished.wait(1)
        self.assertEqual(llm.telemetry["usage_responses"], 0)

    def test_fallback_preserves_every_call_and_result(self):
        llm = self.make([failure(400, "tool calling is not supported"), response('{"tool":"finish","args":{}}')])
        calls = [{"id": key, "type": "function", "function": {"name": "get_metrics", "arguments": "{}"}}
                 for key in ("call-first", "call-second")]
        messages = [{"role": "assistant", "content": "checking", "tool_calls": calls}]
        messages += [{"role": "tool", "tool_call_id": key, "name": "get_metrics", "content": key + " result"}
                     for key in ("call-first", "call-second")]
        result = llm.chat(messages, [])
        self.assertFalse(result["native"])
        self.assertEqual(llm.telemetry["api_calls"], 2)
        self.assertEqual(llm.telemetry["mode"], "json")
        sent = json.dumps(llm.client.chat.completions.create.call_args.kwargs["messages"])
        for key in ("call-first", "call-second"):
            self.assertGreaterEqual(sent.count(key), 3)

    def test_bad_arguments_are_never_empty_object(self):
        for arguments in ("{broken", "null", "[]", ""):
            llm = self.make([response(arguments=arguments)])
            call = llm.chat([], [])["tool_calls"][0]
            self.assertIn("parse_error", call)
            self.assertNotEqual(call["arguments"], {})
        for text in ('```json\n{"tool": "finish", bad}\n```', '{"tool": "finish",'):
            llm = self.make([response(text)])
            llm.native_tools = False
            self.assertIn("parse_error", llm.chat([], [])["tool_calls"][0])

    def test_recursive_redaction_even_policy_off(self):
        policy = Policy(Path(__file__).resolve().parents[1] / "policy.yaml", enabled=False)
        llm = self.make([response()], redactor=policy.redact)
        messages = [{"role": "user", "content": [{"text": "password=hunter2 test-api-secret sk_live_abc123",
                                                   "nested": {"password": "structured-secret"}}]}]
        original = json.dumps(messages)
        llm.chat(messages, [{"nested": {"description": "Bearer private-token"}}])
        sent = json.dumps(llm.client.chat.completions.create.call_args.kwargs)
        for secret in ("hunter2", "test-api-secret", "sk_live_abc123", "private-token", "structured-secret"):
            self.assertNotIn(secret, sent)
        self.assertEqual(json.dumps(messages), original)


class ScriptedTests(unittest.TestCase):
    def test_destructive_result_never_claims_blocked_or_recovered(self):
        llm = ScriptedLLM("disk_full")
        llm.i = 4
        result = llm.chat([{"role": "tool", "content": "removed. (postgres data directory deleted)"}], [])
        self.assertEqual(result["tool_calls"][0]["name"], "finish")
        self.assertIn("복구를 확인하지 못", result["tool_calls"][0]["arguments"]["summary"])
        self.assertEqual(llm.telemetry["api_calls"], 0)

    def test_denied_action_does_not_claim_success(self):
        llm = ScriptedLLM("bad_deploy")
        result = llm.chat([{"role": "tool", "content": "DENIED_BY_HUMAN"}], [])
        self.assertIn("승인되지 않아", result["tool_calls"][0]["arguments"]["summary"])


if __name__ == "__main__":
    unittest.main()
