# hermes-deepseek-search

DeepSeek's own web search and page fetch, wired into Hermes Agent's built-in
`web_search` and `web_extract` tools.

- Search — `POST https://api.deepseek.com/v1/tools/search`
- Fetch — `POST https://api.deepseek.com/v1/tools/fetch` (one URL per request, returns Markdown)
- Auth — the same `Bearer` API key you already use for DeepSeek chat
- Upstream docs — https://api-docs.deepseek.com/zh-cn/guides/search/

`kind: backend` plugin; registers the web provider named `deepseek`, selectable
for `web.search_backend`, `web.extract_backend` or `web.backend`.

## Install

```bash
hermes plugins install deepseek-search
hermes plugins enable deepseek-search

hermes config set web.search_backend deepseek
hermes config set web.extract_backend deepseek
```

Switch only one half if you prefer — e.g. DeepSeek for search, Firecrawl for
extract. Revert to automatic selection with `hermes config unset web.search_backend`.
Changes apply on the next session. You can also pick it interactively via
`hermes tools` (shown as `DeepSeek · paid`).

## Configure

The key belongs in `~/.hermes/.env`, never in `config.yaml`:

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_SEARCH_API_KEY` | — | alternate name for `DEEPSEEK_API_KEY` |
| `DEEPSEEK_SEARCH_BASE_URL` | `https://api.deepseek.com` | point at a self-hosted proxy; a trailing `/v1` is fine |
| `DEEPSEEK_SEARCH_NEED_CONTENT` | `false` | `true` asks DeepSeek for page bodies inside search rows (much longer results) |
| `DEEPSEEK_SEARCH_SUMMARY` | `true` | attach DeepSeek's 600–800 character per-result summary; `0` disables |
| `DEEPSEEK_SEARCH_LOCALIZATION` | unset | IP address used to prefer localized results |

Secrets are read through Hermes' config-aware provider lookup, so a value in
`~/.hermes/.env` or the active profile's secret scope is visible, and a
multiplexed profile never borrows another profile's key.

## Behaviour

- `query` is passed through untouched, so DeepSeek's search syntax
  (`site:`, `-site:`, `-"phrase"`, `lang:`, `after:`, `before:`) works. `count`
  is clamped to 1–50.
- Results are mapped onto Hermes' fixed envelope — `title`, `url`,
  `description` (the `snippet`, falling back to `summary`), `position` — plus
  `summary`, `date`, `site_name`, `authority` and `rerank_score` so the model
  can judge freshness and authority. When `snippet` is empty the description
  falls back to `summary`.
- Extract calls `/v1/tools/fetch` per URL. One failing URL returns an `error`
  on that document only; the rest of the batch is unaffected.
- 408 / 429 / 5xx are retried once (honouring `Retry-After`, capped at 5s);
  other 4xx are surfaced verbatim, including DeepSeek's OpenAI-shaped error
  message and code.
- Without a key `is_available()` is `False`, so the plugin never wins the
  default backend by accident. Explicitly configured but keyless, the tools
  return a clear "DEEPSEEK_API_KEY is not set" error.

## Billing and rate limits

- Search and Fetch are billed per **successful request** (about ¥5 per 1,000).
  Failures, empty result sets and timeouts are not charged; `count` changes
  how many rows come back, not the price.
- Each account gets 5 QPS for Search and 5 QPS for Fetch; beyond that the API
  answers 429.
- Hermes caches web results (`tools/web_result_cache.py`), so repeated
  identical queries are not re-billed.

## Disclosure

What this plugin touches, so you can decide before installing:

- **Network**: every `web_search` sends your query to `api.deepseek.com`; every
  `web_extract` sends the URL and DeepSeek fetches that page server-side, so
  page content passes through DeepSeek. A custom `DEEPSEEK_SEARCH_BASE_URL`
  sends the same traffic wherever you point it.
- **Credentials**: reads `DEEPSEEK_API_KEY` through Hermes' secret route only.
  It never reads `.env` files, `auth.json`, keychains or other tools' logins,
  and never writes or refreshes a token.
- **No** file access, shell commands, background processes, telemetry or usage
  reporting. No self-updater: installs are pinned to an exact commit and update
  only via a catalog SHA bump plus `hermes plugins update deepseek-search`.
- Not affiliated with DeepSeek. Unofficial plugin.

## Overlap with other DeepSeek web backends

`deepseek-search-fetch` in the catalog covers the same two DeepSeek endpoints.
Differences here: the 600–800 character `summary`, `authority` / `rerank_score`
/ `date` passthrough, `need_content` and `localization` knobs, a configurable
base URL for proxies, per-URL error isolation and a built-in single retry on
transient statuses. Pick one; they register different provider names
(`deepseek` here).

## Verify

```bash
hermes plugins validate ~/.hermes/plugins/deepseek-search
hermes plugins doctor  ~/.hermes/plugins/deepseek-search

# unit tests — local mock server, no network, no real key
cd ~/.hermes/hermes-agent
~/.hermes/.venv/bin/python -m unittest discover -s ~/.hermes/plugins/deepseek-search/tests -v
```

A real end-to-end smoke test (spends one billed request):

```bash
cd ~/.hermes/hermes-agent
~/.hermes/.venv/bin/python - <<'PY'
from hermes_cli.plugins import _ensure_plugins_discovered
_ensure_plugins_discovered(force=True)
from agent.web_search_registry import get_provider
p = get_provider("deepseek")
print(p.is_available())
print(p.search("DeepSeek V4 release", 3))
PY
```

## 中文说明

把 DeepSeek 自研搜索引擎接入 Hermes 的 `web_search` / `web_extract`。密钥放
`~/.hermes/.env` 的 `DEEPSEEK_API_KEY`；搜索与抓取按成功请求计费，各自 5 QPS。
配置 `web.search_backend: deepseek` 后下一个会话生效。

## License

Apache-2.0 — see [LICENSE](LICENSE).
