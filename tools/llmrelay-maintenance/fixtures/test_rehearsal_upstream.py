"""Real HTTP regressions for customer attempts interleaved with account probes."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request

import rehearsal_upstream as fixture


class RehearsalUpstreamTest(unittest.TestCase):
    def setUp(self):
        with fixture.LOCK:
            fixture.CALLS.clear()
        self.server = fixture.ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, case, side, body):
        req = urllib.request.Request(self.base + "/" + case + "/" + side + "/v1/responses",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with self.http.open(req, timeout=10) as response:
                return response.status, response.headers.get_content_type(), response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get_content_type(), error.read().decode()

    def probe(self, case, side):
        status, content_type, body = self.post(case, side, {
            "model": "gpt-5.4", "stream": False, "tool_choice": "required",
            "input": [{"role": "user", "content": [{"type": "input_text", "text":
                "Call the probe_ping function with ok=true to acknowledge readiness. You must use the tool."}]}],
            "tools": [{"type": "function", "name": "probe_ping", "parameters": {
                "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}],
        })
        self.assertEqual((status, content_type), (200, "application/json"))
        result = json.loads(body)
        self.assertEqual(result["status"], "completed")
        call = next(item for item in result["output"] if item["type"] == "function_call")
        self.assertEqual(call["name"], "probe_ping")
        self.assertEqual(json.loads(call["arguments"]), {"ok": True})

    def test_background_probes_do_not_change_customer_failures_or_attempts(self):
        for case in ("native-http", "native-sse", "passthrough-http", "passthrough-sse"):
            with self.subTest(case=case), ThreadPoolExecutor(max_workers=4) as workers:
                pending = [workers.submit(self.probe, case, side) for side in ("a", "b") * 8]
                for side, sequence in (("a", "first"), ("b", "first"), ("b", "next")):
                    status, content_type, body = self.post(case, side, {
                        "model": "gpt-5.4", "input": "Rehearsal only", "stream": True,
                        "prompt_cache_key": hashlib.sha256((case + "-" + sequence).encode()).hexdigest(),
                    })
                    if side == "a":
                        self.assertEqual(status, 502 if case.endswith("-http") else 200)
                        if case.endswith("-sse"):
                            self.assertIn("response.failed", body)
                    else:
                        self.assertEqual((status, content_type), (200, "text/event-stream"))
                        self.assertIn("B rehearsal answer", body)
                        self.assertIn("response.completed", body)
                for future in pending:
                    future.result()
                with self.http.open(self.base + "/calls", timeout=10) as response:
                    self.assertEqual(json.load(response)[case], ["a", "b", "b"])

    def test_customer_input_forms_accept_hashed_or_absent_cache_key(self):
        inputs = ["Rehearsal only", [{"role": "user", "content": "Rehearsal only"}],
                  [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Rehearsal only"}]}]]
        for value in inputs:
            for cache_key in (None, hashlib.sha256(b"namespaced-by-gateway").hexdigest()):
                with self.subTest(input=value, cache_key=cache_key):
                    payload = {"model": "gpt-5.4", "stream": True, "input": value}
                    if cache_key is not None:
                        payload["prompt_cache_key"] = cache_key
                    status, _, body = self.post("native-http", "b", payload)
                    self.assertEqual(status, 200)
                    self.assertIn("B rehearsal answer", body)
        with self.http.open(self.base + "/calls", timeout=10) as response:
            self.assertEqual(json.load(response), {"native-http": ["b"] * 6})

    def test_capability_probe_does_not_count_as_customer_attempt(self):
        for side in ("a", "b"):
            self.probe("native-http", side)
        with self.http.open(self.base + "/calls", timeout=10) as response:
            self.assertEqual(json.load(response), {})

    def test_unknown_payload_is_rejected_without_counting(self):
        valid = {"model": "gpt-5.4", "stream": True, "input": "Rehearsal only", "prompt_cache_key": "native-http-first"}
        for changes in ({"model": "other-model"}, {"stream": False}, {"input": None},
                        {"input": "Rehearsal only "}, {"input": []},
                        {"input": [{"role": "assistant", "content": "Rehearsal only"}]},
                        {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Different request"}]}]},
                        {"stream": False, "tool_choice": "required", "tools": 1}):
            with self.subTest(changes=changes):
                status, _, _ = self.post("native-http", "a", dict(valid, **changes))
                self.assertEqual(status, 400)
        for case, side in (("unknown-case", "a"), ("native-http", "unknown-account")):
            status, _, _ = self.post(case, side, valid)
            self.assertEqual(status, 404)
        with self.http.open(self.base + "/calls", timeout=10) as response:
            self.assertEqual(json.load(response), {})


if __name__ == "__main__":
    unittest.main()
