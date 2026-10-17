"""DeepSeek web search + page fetch backend for Hermes Agent.

Backed by the DeepSeek raw JSON tools API (Bearer auth, same key as the chat API):

    POST {base}/v1/tools/search   {"query", "count", "need_content", ...} -> {"results": [...]}
    POST {base}/v1/tools/fetch    {"url"} -> {"title", "content"}

Docs: https://api-docs.deepseek.com/zh-cn/guides/search/

Environment (resolved through Hermes' config-aware secret lookup, so keys set in
``~/.hermes/.env`` or the profile secret scope are visible):

    DEEPSEEK_API_KEY              required (DEEPSEEK_SEARCH_API_KEY also accepted)
    DEEPSEEK_SEARCH_BASE_URL      default https://api.deepseek.com (a ``/v1`` suffix is fine)
    DEEPSEEK_SEARCH_NEED_CONTENT  "1"/"true" -> ask DeepSeek for full page text in search rows
    DEEPSEEK_SEARCH_SUMMARY       "0"/"false" -> drop the long ``summary`` field from search rows
    DEEPSEEK_SEARCH_LOCALIZATION  optional IP address used to prefer localized results

Billing: Search and Fetch are billed per successful request (results non-empty /
body returned), not per token. Both are capped at 5 QPS per account.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_MAX_COUNT = 50          # API: count is 1..50
SEARCH_TIMEOUT = 30.0
FETCH_TIMEOUT = 45.0
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env(name: str, default: str = "") -> str:
    """Config-aware lookup; returns *default* on any resolution failure.

    ``get_provider_env`` raises when a multiplexed profile has no bound secret
    scope — failing closed (empty) is correct there: a routed profile must not
    silently search on another profile's key.
    """
    try:
        return (get_provider_env(name) or default).strip()
    except Exception:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _api_key() -> str:
    return _env("DEEPSEEK_API_KEY") or _env("DEEPSEEK_SEARCH_API_KEY")


def _base_url() -> str:
    return (_env("DEEPSEEK_SEARCH_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _endpoint(base: str, path: str) -> str:
    """Join base + API path without doubling ``/v1`` when the user set it."""
    if base.endswith("/v1"):
        return base + path[len("/v1"):]
    return base + path


def _error_detail(response: httpx.Response) -> str:
    """DeepSeek errors are OpenAI-shaped: ``{"error": {"message": ..., "code": ...}}``."""
    message = ""
    code = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            code = str(error.get("code") or "")
    except Exception:
        pass
    if not message:
        message = (response.text or "").strip()[:500] or response.reason_phrase or "request failed"
    suffix = f" [{code}]" if code and code not in message else ""
    return message + suffix


class DeepSeekWebSearchProvider(WebSearchProvider):
    """Search + fetch via DeepSeek's ``/v1/tools/*`` endpoints."""

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def display_name(self) -> str:
        return "DeepSeek Search"

    def is_available(self) -> bool:
        # Cheap, no network: the key is the only gate.
        return bool(_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    # ------------------------------------------------------------------ HTTP
    def _request(self, path: str, payload: dict[str, Any], *, action: str, timeout: float) -> dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            raise ValueError(
                f"DEEPSEEK_API_KEY is not set — cannot {action} with DeepSeek. "
                "Add it to ~/.hermes/.env (https://platform.deepseek.com/api_keys)."
            )
        url = _endpoint(_base_url(), path)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise ValueError(f"DeepSeek {action} request failed: {exc}") from exc
            if response.status_code < 400:
                try:
                    data = response.json()
                except Exception as exc:
                    raise ValueError(f"DeepSeek {action} returned invalid JSON: {exc}") from exc
                if not isinstance(data, dict):
                    raise ValueError(f"DeepSeek {action} returned an unexpected payload")
                return data
            detail = _error_detail(response)
            if response.status_code in _RETRY_STATUS and attempt == 0:
                # 408/429/5xx are transient per the API docs; one short retry.
                try:
                    delay = min(float(response.headers.get("Retry-After", "")), 5.0)
                except (TypeError, ValueError):
                    delay = 1.5
                logger.info("DeepSeek %s HTTP %d, retrying in %.1fs", action, response.status_code, delay)
                time.sleep(delay)
                last_error = ValueError(f"HTTP {response.status_code}: {detail}")
                continue
            hint = " (insufficient balance)" if response.status_code == 402 else ""
            raise ValueError(f"DeepSeek {action} failed (HTTP {response.status_code}){hint}: {detail}")
        raise ValueError(f"DeepSeek {action} failed: {last_error}")

    # ----------------------------------------------------------------- search
    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"success": False, "error": "DeepSeek search requires a non-empty query"}
        try:
            count = max(1, min(int(limit), SEARCH_MAX_COUNT))
        except (TypeError, ValueError):
            count = 5
        payload: dict[str, Any] = {
            "query": q,
            "count": count,
            "need_content": _flag("DEEPSEEK_SEARCH_NEED_CONTENT", False),
        }
        localization = _env("DEEPSEEK_SEARCH_LOCALIZATION")
        if localization:
            payload["localization"] = localization
        try:
            data = self._request("/v1/tools/search", payload, action="search", timeout=SEARCH_TIMEOUT)
        except Exception as exc:
            logger.warning("DeepSeek search failed: %s", exc)
            return {"success": False, "error": str(exc)}
        rows = [
            self._search_row(item, index + 1)
            for index, item in enumerate(data.get("results") or [])
            if isinstance(item, dict)
        ]
        return {"success": True, "data": {"web": rows}}

    @staticmethod
    def _search_row(item: dict[str, Any], position: int) -> dict[str, Any]:
        snippet = str(item.get("snippet") or "").strip()
        summary = str(item.get("summary") or "").strip()
        row: dict[str, Any] = {
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "description": snippet or summary,
            "position": position,
        }
        # DeepSeek's summary (600-800 chars, query-relevant) is what the model should read;
        # the snippet is front-end oriented. Keep it unless explicitly disabled.
        if summary and summary != row["description"] and _flag("DEEPSEEK_SEARCH_SUMMARY", True):
            row["summary"] = summary
        if item.get("date"):
            row["date"] = str(item["date"])
        if item.get("site_name"):
            row["site_name"] = str(item["site_name"])
        authority = item.get("authority")
        if isinstance(authority, int) and not isinstance(authority, bool) and authority >= 0:
            row["authority"] = authority
        score = item.get("rerank_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            row["rerank_score"] = score
        content = str(item.get("content") or "")
        if content:
            row["content"] = content
        return row

    # ------------------------------------------------------------------ fetch
    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch each URL via ``/v1/tools/fetch`` (one request per page; 5 QPS account cap)."""
        documents: list[dict[str, Any]] = []
        for url in urls or []:
            documents.append(self._fetch_one(str(url)))
        return documents

    def _fetch_one(self, url: str) -> dict[str, Any]:
        try:
            data = self._request("/v1/tools/fetch", {"url": url}, action="fetch", timeout=FETCH_TIMEOUT)
        except Exception as exc:
            logger.warning("DeepSeek fetch failed for %s: %s", url, exc)
            return {"url": url, "title": "", "content": "", "error": str(exc)}
        title = str(data.get("title") or "")
        content = str(data.get("content") or "")
        if not content.strip():
            return {"url": url, "title": title, "content": "", "error": "DeepSeek fetch returned no page content"}
        return {
            "url": url,
            "title": title,
            "content": content,
            "raw_content": content,
            "metadata": {"sourceURL": url, "title": title},
        }

    # ------------------------------------------------------------- picker row
    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "DeepSeek",
            "badge": "paid",
            "tag": "DeepSeek's own search engine: web search + Markdown page fetch over "
                   "api.deepseek.com/v1/tools/*. Billed per successful request, 5 QPS. "
                   "Requires DEEPSEEK_API_KEY.",
            "env_vars": [
                {
                    "key": "DEEPSEEK_API_KEY",
                    "prompt": "DeepSeek API key (search + fetch, same key as chat)",
                    "url": "https://platform.deepseek.com/api_keys",
                }
            ],
        }
