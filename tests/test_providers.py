import base64
import http.client
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from local_computer_use.providers import (
    MAX_DIRECTIVE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_PROMPT_BYTES,
    MAX_RESPONSE_BYTES,
    CodexProvider,
    DecisionUsage,
    GeminiProvider,
    LlamaProvider,
    ProviderCancelled,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeout,
    ProviderTransportError,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQAB"
    "DQottAAAAABJRU5ErkJggg=="
)
DONE = {"type": "done", "result": "Complete"}


def llama_reply(content, *, reason="stop"):
    return json.dumps({
        "choices": [{"finish_reason": reason, "message": {"content": content}}],
    }).encode()


def sse(event):
    return b"event: " + event["type"].encode() + b"\r\ndata: " + json.dumps(event).encode() + b"\r\n\r\n"


def complete_event(*output):
    return sse({"type": "response.completed", "response": {"status": "completed", "output": output}})


class FakeModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, dict(self.headers), payload))
        self.server.received.set()
        if self.server.stall_headers:
            self.server.release.wait(3)
        self.send_response(self.server.status)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Content-Length", str(len(self.server.reply) + self.server.extra_length))
        if self.server.location:
            self.send_header("Location", self.server.location)
        self.end_headers()
        try:
            self.wfile.write(self.server.reply)
            self.wfile.flush()
            self.server.sent.set()
            if self.server.observe_disconnect:
                self.connection.settimeout(2)
                if self.connection.recv(1) == b"":
                    self.server.disconnected.set()
            elif self.server.stall_body:
                self.server.release.wait(3)
        except OSError:
            pass


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelHandler)
        self.server.requests = []
        self.server.received = threading.Event()
        self.server.sent = threading.Event()
        self.server.disconnected = threading.Event()
        self.server.release = threading.Event()
        self.server.status = 200
        self.server.content_type = "application/json"
        self.server.reply = llama_reply(json.dumps(DONE))
        self.server.location = None
        self.server.extra_length = 0
        self.server.stall_headers = False
        self.server.stall_body = False
        self.server.observe_disconnect = False
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.local_url = f"http://127.0.0.1:{self.port}/v1"
        self.local = LlamaProvider("fixture", self.local_url, timeout=1)
        self.cloud_hosts = []

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def cloud_fixture(self):
        def connect(host, port, *, timeout):
            self.cloud_hosts.append((host, port))
            return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

        return patch("local_computer_use.providers.http.client.HTTPSConnection", side_effect=connect)

    def test_llama_sends_bounded_typed_png_and_schema(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1"}, clear=False):
            self.assertEqual(self.local.decide("Observe this frame", PNG, self.cancel), DONE)
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["model"], "fixture")
        text, image = body["messages"][1]["content"]
        self.assertEqual(text, {"type": "text", "text": "Observe this frame"})
        self.assertEqual(image["type"], "image_url")
        prefix, encoded = image["image_url"]["url"].split(",", 1)
        self.assertEqual(prefix, "data:image/png;base64")
        self.assertEqual(base64.b64decode(encoded), PNG)
        self.assertIn("done", body["response_format"]["schema"]["properties"]["type"]["enum"])
        self.assertNotIn("tools", body)

    def test_llama_reports_usage_and_sets_optional_turn_limits(self):
        response = json.loads(llama_reply(json.dumps(DONE)))
        response["usage"] = {"prompt_tokens": 65, "completion_tokens": 12}
        response["timings"] = {"prompt_n": 65, "cache_n": 220, "prompt_ms": 7.5,
                               "predicted_ms": 29.5}
        self.server.reply = json.dumps(response).encode()
        provider = LlamaProvider("fixture", self.local_url, max_output_tokens=400, reasoning_budget=0)
        action, usage = provider.decide_with_usage("Read", PNG, self.cancel, directives=("done",))
        self.assertEqual(action, DONE)
        self.assertEqual(usage, DecisionUsage(285, 12, 220, 7.5, 29.5))
        request = self.server.requests[0][2]
        self.assertEqual(request["max_tokens"], 400)
        self.assertEqual(request["reasoning_budget_tokens"], 0)
        self.assertEqual(request["response_format"]["schema"]["properties"]["type"]["enum"], ["done"])
        with self.assertRaises(ProviderResponseError) as error:
            provider.decide_with_usage("Plan", None, self.cancel, directives=("plan",))
        self.assertEqual(error.exception.usage, usage)

    def test_llama_standard_usage_without_timings(self):
        usage = DecisionUsage.from_llama({"usage": {"prompt_tokens": 77, "completion_tokens": 9,
                                                     "prompt_tokens_details": {"cached_tokens": 19}}})
        self.assertEqual(usage, DecisionUsage(77, 9, 19))
        self.assertEqual(DecisionUsage.from_llama({"usage": [], "timings": {"prompt_ms": True}}), DecisionUsage())

    def test_local_endpoint_only_accepts_loopback(self):
        for endpoint in (
            "https://example.com/v1", "http://127.0.0.1.example.com/v1",
            "http://localhost@example.com/v1", "http://user@127.0.0.1/v1",
            "file:///v1", "http://127.0.0.1/v1?q=anything",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                LlamaProvider("fixture", endpoint)
        self.assertEqual(
            LlamaProvider("fixture", f"http://localhost:{self.port}").decide("test", None, self.cancel), DONE,
        )
        self.assertEqual(len(self.server.requests[0][2]["messages"][1]["content"]), 1)

    def test_rejects_input_before_sending(self):
        for prompt, image in (
            ("", None), ("x" * (MAX_PROMPT_BYTES + 1), None),
            ("é" * MAX_PROMPT_BYTES, None), ("\ud800", None),
            ("test", b""), ("test", b"file:///tmp/example.png"),
            ("test", PNG + b"\0" * MAX_IMAGE_BYTES),
        ):
            with self.subTest(prompt_length=len(prompt)), self.assertRaises(ProviderError):
                self.local.decide(prompt, image, self.cancel)
        self.assertEqual(self.server.requests, [])

    def test_transport_rejects_invalid_directives(self):
        invalid = (
            "{}", "[]", "hello",
            '{"type":"shell","command":"arbitrary command"}',
            '{"type":"done","type":"blocked"}',
            '{"type":"done","result":1e999}',
            '{"type":"done","result":NaN}',
            '{"type":"done","result":"\\ud800"}',
            '{"type":"done","result":"' + "x" * MAX_DIRECTIVE_BYTES + '"}',
        )
        for output in invalid:
            with self.subTest(output=output[:50]), self.assertRaises(ProviderResponseError):
                self.server.reply = llama_reply(output)
                self.local.decide("test", None, self.cancel)

    def test_transport_rejects_incomplete_or_oversized_body(self):
        original = self.server.reply
        self.server.extra_length = 30
        with self.assertRaises(ProviderResponseError):
            self.local.decide("test", None, self.cancel)
        self.server.extra_length = 0
        self.server.reply = original + b" " * MAX_RESPONSE_BYTES
        with self.assertRaisesRegex(ProviderResponseError, "size limit"):
            self.local.decide("test", None, self.cancel)
        self.server.reply = llama_reply(json.dumps(DONE), reason="length")
        with self.assertRaises(ProviderResponseError):
            self.local.decide("test", None, self.cancel)

    def test_reports_transport_failure(self):
        with patch.object(http.client.HTTPConnection, "connect", side_effect=ConnectionRefusedError):
            with self.assertRaises(ProviderTransportError):
                self.local.decide("test", None, self.cancel)

    def test_cancel_before_submission(self):
        self.cancel.set()
        with self.assertRaises(ProviderCancelled):
            self.local.decide("test", PNG, self.cancel)
        self.assertEqual(self.server.requests, [])

    def test_cancel_interrupts_waiting_on_response_headers(self):
        self.server.stall_headers = True
        cancelled = threading.Event()

        def cancel_when_received():
            if self.server.received.wait(2):
                self.cancel.set()
                cancelled.set()

        trigger = threading.Thread(target=cancel_when_received, daemon=True)
        trigger.start()
        try:
            with self.assertRaises(ProviderCancelled):
                LlamaProvider("fixture", self.local_url, timeout=2).decide("test", None, self.cancel)
            self.assertTrue(cancelled.wait(1))
            self.assertFalse(self.server.release.is_set())
        finally:
            trigger.join(2)

    def test_timeout_while_body_waits(self):
        self.server.extra_length = 30
        self.server.stall_body = True
        with self.assertRaises(ProviderTimeout):
            LlamaProvider("fixture", self.local_url, timeout=0.25).decide("test", None, self.cancel)
        self.assertTrue(self.server.sent.is_set())

    def test_cancellation_closes_an_open_chatgpt_stream(self):
        self.server.content_type = "text/event-stream"
        self.server.reply = sse({"type": "response.output_text.delta", "delta": "partial"})
        self.server.extra_length = 30
        self.server.observe_disconnect = True

        def cancel_when_streamed():
            if self.server.sent.wait(2):
                self.cancel.set()

        trigger = threading.Thread(target=cancel_when_streamed, daemon=True)
        trigger.start()
        try:
            with self.cloud_fixture(), self.assertRaises(ProviderCancelled):
                CodexProvider("plan-fixture", lambda: "fixture-token", timeout=2).decide(
                    "test", None, self.cancel,
                )
            self.assertTrue(self.server.disconnected.wait(1))
        finally:
            trigger.join(2)

    def test_codex_can_cancel_while_waiting_for_an_app_credential(self):
        started = threading.Event()
        release = threading.Event()

        def token_supplier():
            started.set()
            release.wait(2)
            return "fixture-token"

        def cancel_when_started():
            if started.wait(2):
                self.cancel.set()

        trigger = threading.Thread(target=cancel_when_started, daemon=True)
        trigger.start()
        try:
            with self.cloud_fixture(), self.assertRaises(ProviderCancelled):
                CodexProvider("plan-fixture", token_supplier, timeout=2).decide("test", None, self.cancel)
            self.assertTrue(started.is_set())
            self.assertFalse(release.is_set())
            self.assertEqual(self.cloud_hosts, [])
        finally:
            release.set()
            trigger.join(2)

    def test_gemini_request_and_thought_filter(self):
        self.server.reply = json.dumps({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"text": "internal", "thought": True},
                    {"text": '{"type":"message",'},
                    {"text": '"target":"coordinator","text":"Hello"}'},
                ]},
            }],
        }).encode()
        with self.cloud_fixture():
            output = GeminiProvider("gemini-fixture", "fixture-key").decide("test", PNG, self.cancel)
        self.assertEqual(output, {"type": "message", "target": "coordinator", "text": "Hello"})
        self.assertEqual(self.cloud_hosts, [("generativelanguage.googleapis.com", None)])
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1beta/models/gemini-fixture:generateContent")
        self.assertEqual(headers["x-goog-api-key"], "fixture-key")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseJsonSchema"]["required"], ["type"])
        data = body["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(data["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(data["data"]), PNG)
        self.assertNotIn("tools", body)

    def test_cloud_credentials_do_not_follow_redirects_or_echo_error_bodies(self):
        self.server.status = 307
        self.server.location = "https://example.com/accept-credentials"
        self.server.reply = b"fixture-key"
        with self.cloud_fixture(), self.assertRaises(ProviderHTTPError) as raised:
            GeminiProvider("gemini-fixture", "fixture-key").decide("test", None, self.cancel)
        self.assertEqual(raised.exception.status, 307)
        self.assertNotIn("fixture-key", str(raised.exception))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.cloud_hosts), 1)

    def test_codex_sends_only_text_and_requires_completed_stream(self):
        self.server.content_type = "text/event-stream; charset=utf-8"
        self.server.reply = (
            b": heartbeat\r\n\r\n"
            + sse({"type": "response.output_text.delta", "delta": "partial"})
            + complete_event(
                {"type": "reasoning"},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": json.dumps(DONE)}]},
            )
        )
        self.server.extra_length = 100
        self.server.stall_body = True
        tokens = iter(["first-app-token", "next-app-token"])
        with self.cloud_fixture():
            codex = CodexProvider("plan-fixture", lambda: next(tokens), timeout=1)
            self.assertEqual(codex.decide("test", None, self.cancel), DONE)
            self.assertEqual(codex.decide("another test", None, self.cancel), DONE)
        self.assertEqual(self.cloud_hosts, [("api.openai.com", None)] * 2)
        for (path, headers, body), token in zip(self.server.requests, ["first-app-token", "next-app-token"]):
            self.assertEqual(path, "/v1/responses")
            self.assertEqual(headers["Authorization"], f"Bearer {token}")
            self.assertEqual(headers["Accept"], "text/event-stream")
            self.assertEqual(body["input"][0]["role"], "user")
            self.assertTrue(body["stream"])
            self.assertFalse(body["store"])
            self.assertEqual(set(body), {"model", "instructions", "input", "store", "stream"})

    def test_codex_rejects_partial_output_and_hosted_tools(self):
        codex = CodexProvider("plan-fixture", lambda: "fixture-token")
        self.server.content_type = "text/event-stream"
        for reply in (
            sse({"type": "response.output_text.delta", "delta": json.dumps(DONE)}),
            complete_event({"type": "function_call", "name": "shell"}),
            complete_event({"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}),
        ):
            with self.subTest(reply=reply), self.cloud_fixture(), self.assertRaises(ProviderResponseError):
                self.server.reply = reply
                codex.decide("test", None, self.cancel)

    def test_codex_quota_and_invalid_credentials(self):
        codex = CodexProvider("plan-fixture", lambda: "valid-fixture-token")
        self.server.content_type = "text/event-stream"
        self.server.reply = sse({
            "type": "response.failed",
            "response": {"error": {
                "code": "subscription_sharing_usage_limit_exceeded", "message": "valid-fixture-token",
            }},
        })
        with self.cloud_fixture(), self.assertRaisesRegex(ProviderError, "plan usage is unavailable"):
            codex.decide("test", None, self.cancel)
        with self.assertRaisesRegex(ProviderError, "text only"):
            codex.decide("test", PNG, self.cancel)
        with self.assertRaisesRegex(ProviderError, "access token is required"):
            CodexProvider("plan-fixture", lambda: "bad\r\n").decide("test", None, self.cancel)
        with self.assertRaisesRegex(ProviderError, "API key is required"):
            GeminiProvider("gemini-fixture", "")
        self.assertEqual(len(self.server.requests), 1)


if __name__ == "__main__":
    unittest.main()
