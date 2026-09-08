"""Verify the release frontend over real loopback HTTP without Docker."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

import rehearse


class FrontendSmokeTest(unittest.TestCase):
    def setUp(self):
        self.files = {"assets/app-123.js": b"document.querySelector('#app');",
                      "assets/app-456.css": b"body { color: black; }"}
        self.html = (b'<html><head><script nonce="runtime" type="module" src="/assets/app-123.js"></script>'
                     b'<link rel="stylesheet" href="/assets/app-456.css"></head><body><div id="app"></div>'
                     b'<script>window.__APP_CONFIG__={"site_name":"dynamic"}</script></body></html>')
        self.responses = {"/": (200, "text/html", self.html)}
        self.responses.update({"/" + name: (200, "application/octet-stream", body)
                               for name, body in self.files.items()})
        self.seen = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.seen.append(self.path)
                status, content_type, body = test.responses.get(self.path, (404, "text/plain", b"missing"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if status == 302:
                    self.send_header("Location", "/redirected.js")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        entries = {"index.html": b"Build-time HTML has no runtime settings", **self.files}
        self.manifest_path = Path(self.temp.name) / "frontend.json"
        self.manifest_path.write_text(json.dumps({"files": [
            {"path": "dist/" + name, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            for name, body in entries.items()]}), encoding="utf-8")
        self.manifest = rehearse.load_frontend_manifest(self.manifest_path)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_accepts_dynamic_homepage_and_checks_served_asset_bytes(self):
        result = rehearse.frontend_smoke(self.base, self.manifest)
        self.assertTrue(result["asset_hashes_verified"])
        self.assertEqual({entry["path"] for entry in result["assets"]}, set(self.files))
        self.assertEqual(set(self.seen), {"/", "/assets/app-123.js", "/assets/app-456.css"})

    def test_rejects_corrupt_asset_and_spa_fallback_with_http_200(self):
        for body in (b"!" * len(self.files["assets/app-123.js"]), self.html):
            with self.subTest(body=body[:10]):
                self.responses["/assets/app-123.js"] = (200, "text/html", body)
                with self.assertRaises(RuntimeError):
                    rehearse.frontend_smoke(self.base, self.manifest)

    def test_rejects_unembedded_html_missing_manifest_asset_and_redirect(self):
        self.responses["/"] = (200, "text/html", b"Frontend not embedded")
        with self.assertRaisesRegex(RuntimeError, "embedded"):
            rehearse.frontend_smoke(self.base, self.manifest)
        self.responses["/"] = (200, "text/html", self.html)
        manifest = {**self.manifest, "files": {"index.html": self.manifest["files"]["index.html"]}}
        with self.assertRaisesRegex(RuntimeError, "missing from manifest"):
            rehearse.frontend_smoke(self.base, manifest)
        self.responses["/assets/app-123.js"] = (302, "text/plain", b"")
        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            rehearse.frontend_smoke(self.base, self.manifest)
        self.assertNotIn("/redirected.js", self.seen)

    def test_rejects_external_asset_before_following_it(self):
        self.responses["/"] = (200, "text/html", self.html.replace(b"/assets/app-123.js", b"https://example.invalid/app.js"))
        with self.assertRaisesRegex(RuntimeError, "local paths"):
            rehearse.frontend_smoke(self.base, self.manifest)
        self.assertEqual(self.seen, ["/"])

    def test_manifest_requires_index_and_rejects_traversal(self):
        for path in ("assets/app.js", "../index.html"):
            self.manifest_path.write_text(json.dumps({"files": [{"path": path, "size": 0, "sha256": "0" * 64}]}))
            with self.assertRaises(RuntimeError):
                rehearse.load_frontend_manifest(self.manifest_path)


if __name__ == "__main__":
    unittest.main()
