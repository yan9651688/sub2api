#!/usr/bin/env python3
"""Unbilled, deterministic Responses fixture for the isolated Docker rehearsal."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import threading

CALLS = {}
LOCK = threading.Lock()
CASES = ("native-http", "native-sse", "passthrough-http", "passthrough-sse")
PRICING = json.dumps({"gpt-5.4": {"input_cost_per_token": 0.000001,
                                "output_cost_per_token": 0.000003,
                                "litellm_provider": "openai", "mode": "chat"}}).encode()


def is_rehearsal_input(value):
    """Match the controlled customer text, including Responses normalization."""
    if isinstance(value, str):
        return value == "Rehearsal only"
    if not isinstance(value, list) or len(value) != 1:
        return False
    message = value[0]
    if not isinstance(message, dict) or message.get("role") != "user" or message.get("type", "message") != "message":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return content == "Rehearsal only"
    if not isinstance(content, list) or len(content) != 1:
        return False
    part = content[0]
    return isinstance(part, dict) and part.get("type") == "input_text" and part.get("text") == "Rehearsal only"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/pricing.json":
            self.reply(200, PRICING)
            return
        if self.path == "/pricing.sha256":
            self.reply(200, (hashlib.sha256(PRICING).hexdigest() + "\n").encode(), "text/plain")
            return
        if self.path == "/calls":
            with LOCK:
                value = dict(CALLS)
        else:
            value = {"status": "ok"}
        self.reply(200, json.dumps(value).encode())

    def do_POST(self):
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, TypeError):
            self.reply(400, b'{"error":{"message":"Invalid fixture JSON"}}')
            return
        if not isinstance(payload, dict):
            self.reply(400, b'{"error":{"message":"Expected fixture JSON object"}}')
            return
        parts = self.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] not in CASES or parts[1] not in ("a", "b"):
            self.reply(404, b'{"error":{"message":"Unknown fixture route"}}')
            return
        case, account = parts[:2]
        # Account creation schedules a separate, asynchronous Responses capability
        # probe. It must succeed without entering the customer attempt counter.
        tools = payload.get("tools")
        is_probe = payload.get("stream") is False and payload.get("tool_choice") == "required" and isinstance(tools, list) and any(
            isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") == "probe_ping"
            for tool in tools)
        if is_probe and "prompt_cache_key" not in payload:
            probe = {"id": "resp_probe", "object": "response", "status": "completed",
                     "model": payload.get("model", "gpt-5.4"),
                     "output": [{"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                                 "name": "probe_ping", "arguments": '{"ok":true}', "status": "completed"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            self.reply(200, json.dumps(probe).encode())
            return
        # The gateway namespaces/hashes prompt_cache_key before forwarding it.
        # Identify our controlled request by stable content, never that key.
        if payload.get("stream") is not True or payload.get("model") != "gpt-5.4" or not is_rehearsal_input(payload.get("input")):
            self.reply(400, b'{"error":{"message":"Unrecognized rehearsal request"}}')
            return
        with LOCK:
            CALLS.setdefault(case, []).append(account)
        if account == "a":
            error = {"type": "server_error", "message": "Rehearsal upstream unavailable"}
            if case.endswith("-http"):
                self.reply(502, json.dumps({"error": error}).encode())
                return
            error["status_code"] = 503
            event = {"type": "response.failed", "response": {"id": "resp_fake_a", "status": "failed", "error": error}}
            self.reply(200, ("data: " + json.dumps(event) + "\n\n").encode(), "text/event-stream")
            return
        final = {"id": "resp_fake_b", "object": "response", "status": "completed", "model": "gpt-5.4",
                 "output": [{"type": "message", "id": "msg_fake_b", "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": "B rehearsal answer", "annotations": []}]}],
                 "usage": {"input_tokens": 1, "output_tokens": 3, "total_tokens": 4}}
        events = [{"type": "response.output_text.delta", "item_id": "msg_fake_b", "output_index": 0,
                   "content_index": 0, "delta": "B rehearsal answer"},
                  {"type": "response.completed", "response": final}]
        self.reply(200, "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode(), "text/event-stream")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
