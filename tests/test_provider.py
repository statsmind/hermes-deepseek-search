"""Offline tests for the deepseek-search provider.

Runs the provider against a local mock of api.deepseek.com — no network, no real key.

    cd ~/.hermes/hermes-agent
    ~/.hermes/.venv/bin/python -m unittest discover -s ~/.hermes/plugins/deepseek-search/tests -v

The provider module is imported from the plugin directory directly (its only
Hermes dependency is ``agent.web_search_provider``, which needs the repo root on
sys.path).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import provider  # noqa: E402


class MockDeepSeek:
    """Tiny HTTP server standing in for https://api.deepseek.com."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.search_response: tuple[int, dict] = (200, {"results": []})
        self.search_responses: list[tuple[int, dict]] = []
        self.fetch_responses: dict[str, tuple[int, dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(
                    {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
                )
                if self.path.endswith("/v1/tools/search"):
                    status, payload = (
                        outer.search_responses.pop(0) if outer.search_responses else outer.search_response
                    )
                elif self.path.endswith("/v1/tools/fetch"):
                    url = body.get("url", "")
                    status, payload = outer.fetch_responses.get(url, (404, {"error": {"message": "no page"}}))
                else:
                    status, payload = 404, {"error": {"message": "unknown path"}}
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockDeepSeek().__enter__()
        self._env = dict(os.environ)
        # The provider resolves secrets through Hermes' config-aware lookup,
        # which also sees ~/.hermes/.env. Pin it to this test's os.environ so a
        # developer's real DEEPSEEK_API_KEY cannot leak into the assertions.
        self._orig_get_provider_env = provider.get_provider_env
        provider.get_provider_env = lambda name, default=None: os.environ.get(name, default)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        os.environ["DEEPSEEK_SEARCH_BASE_URL"] = f"http://127.0.0.1:{self.mock.port}"
        for key in ("DEEPSEEK_SEARCH_NEED_CONTENT", "DEEPSEEK_SEARCH_SUMMARY",
                    "DEEPSEEK_SEARCH_LOCALIZATION", "DEEPSEEK_SEARCH_API_KEY"):
            os.environ.pop(key, None)
        self.provider = provider.DeepSeekWebSearchProvider()

    def tearDown(self) -> None:
        provider.get_provider_env = self._orig_get_provider_env
        os.environ.clear()
        os.environ.update(self._env)
        self.mock.__exit__(None, None, None)

    # -- identity / availability -------------------------------------------------
    def test_identity(self):
        self.assertEqual(self.provider.name, "deepseek")
        self.assertTrue(self.provider.supports_search())
        self.assertTrue(self.provider.supports_extract())
        self.assertTrue(self.provider.is_available())

    def test_unavailable_without_key(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        self.assertFalse(provider.DeepSeekWebSearchProvider().is_available())
        out = provider.DeepSeekWebSearchProvider().search("hello")
        self.assertFalse(out["success"])
        self.assertIn("DEEPSEEK_API_KEY", out["error"])

    def test_alternate_key_env(self):
        os.environ.pop("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_SEARCH_API_KEY"] = "sk-alt"
        self.assertTrue(provider.DeepSeekWebSearchProvider().is_available())

    # -- search ------------------------------------------------------------------
    def test_search_maps_result_fields(self):
        self.mock.search_response = (200, {"results": [{
            "rerank_score": 2.4,
            "title": "DeepSeek V4 模型发布",
            "url": "https://example.com/v4",
            "snippet": "短摘要",
            "summary": "长摘要" * 20,
            "content": "",
            "site_name": "Example News",
            "site_icon": "https://example.com/favicon.ico",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        }]})
        out = self.provider.search("DeepSeek V4", limit=5)
        self.assertTrue(out["success"], out)
        row = out["data"]["web"][0]
        self.assertEqual(row["title"], "DeepSeek V4 模型发布")
        self.assertEqual(row["url"], "https://example.com/v4")
        self.assertEqual(row["description"], "短摘要")
        self.assertEqual(row["position"], 1)
        self.assertTrue(row["summary"].startswith("长摘要"))
        self.assertEqual(row["date"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(row["site_name"], "Example News")
        self.assertEqual(row["authority"], 3)
        self.assertEqual(row["rerank_score"], 2.4)
        self.assertNotIn("content", row)

        sent = self.mock.requests[0]
        self.assertEqual(sent["path"], "/v1/tools/search")
        self.assertEqual(sent["auth"], "Bearer sk-test")
        self.assertEqual(sent["body"], {"query": "DeepSeek V4", "count": 5, "need_content": False})

    def test_query_syntax_and_count_clamp(self):
        self.mock.search_response = (200, {"results": []})
        self.provider.search("DeepSeek site:github.com lang:zh after:2025", limit=999)
        self.assertEqual(self.mock.requests[0]["body"]["count"], 50)
        self.assertEqual(
            self.mock.requests[0]["body"]["query"], "DeepSeek site:github.com lang:zh after:2025"
        )

    def test_need_content_and_localization(self):
        os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = "true"
        os.environ["DEEPSEEK_SEARCH_LOCALIZATION"] = "1.2.3.4"
        self.mock.search_response = (200, {"results": [{"title": "t", "url": "u", "snippet": "s",
                                                        "summary": "sum", "content": "# body"}]})
        out = self.provider.search("q", limit=1)
        body = self.mock.requests[0]["body"]
        self.assertTrue(body["need_content"])
        self.assertEqual(body["localization"], "1.2.3.4")
        self.assertEqual(out["data"]["web"][0]["content"], "# body")

    def test_summary_can_be_disabled(self):
        os.environ["DEEPSEEK_SEARCH_SUMMARY"] = "0"
        self.mock.search_response = (200, {"results": [{"title": "t", "url": "u", "snippet": "s", "summary": "sum"}]})
        row = self.provider.search("q", limit=1)["data"]["web"][0]
        self.assertNotIn("summary", row)
        self.assertEqual(row["description"], "s")

    def test_empty_query_rejected_without_request(self):
        out = self.provider.search("   ")
        self.assertFalse(out["success"])
        self.assertEqual(self.mock.requests, [])

    def test_search_http_error_is_surfaced(self):
        self.mock.search_response = (401, {"error": {"message": "Authentication Fails", "code": "invalid_request_error"}})
        out = self.provider.search("q", limit=3)
        self.assertFalse(out["success"])
        self.assertIn("401", out["error"])
        self.assertIn("Authentication Fails", out["error"])

    def test_transient_429_is_retried(self):
        self.mock.search_responses = [
            (429, {"error": {"message": "rate limited"}}),
            (200, {"results": [{"title": "t", "url": "u", "snippet": "s"}]}),
        ]
        out = self.provider.search("q", limit=1)
        self.assertTrue(out["success"], out)
        self.assertEqual(len(self.mock.requests), 2)

    def test_base_url_with_v1_suffix(self):
        os.environ["DEEPSEEK_SEARCH_BASE_URL"] = f"http://127.0.0.1:{self.mock.port}/v1"
        self.mock.search_response = (200, {"results": []})
        self.assertTrue(self.provider.search("q", limit=1)["success"])
        self.assertEqual(self.mock.requests[0]["path"], "/v1/tools/search")

    def test_empty_results_is_success(self):
        self.mock.search_response = (200, {"results": []})
        out = self.provider.search("nothing", limit=5)
        self.assertEqual(out, {"success": True, "data": {"web": []}})

    # -- fetch -------------------------------------------------------------------
    def test_extract_maps_documents_and_per_url_errors(self):
        self.mock.fetch_responses = {
            "https://ok.example/a": (200, {"title": "Page A", "content": "# Page A\n\nBody."}),
        }
        docs = self.provider.extract(["https://ok.example/a", "https://bad.example/b"])
        self.assertEqual(len(docs), 2)
        ok, bad = docs
        self.assertEqual(ok["url"], "https://ok.example/a")
        self.assertEqual(ok["title"], "Page A")
        self.assertEqual(ok["content"], "# Page A\n\nBody.")
        self.assertEqual(ok["raw_content"], ok["content"])
        self.assertEqual(ok["metadata"]["sourceURL"], "https://ok.example/a")
        self.assertNotIn("error", ok)
        self.assertEqual(bad["url"], "https://bad.example/b")
        self.assertIn("404", bad["error"])
        self.assertEqual(bad["content"], "")

    def test_extract_empty_body_is_an_error(self):
        self.mock.fetch_responses = {"https://empty.example/": (200, {"title": "", "content": ""})}
        doc = self.provider.extract(["https://empty.example/"])[0]
        self.assertIn("no page content", doc["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
